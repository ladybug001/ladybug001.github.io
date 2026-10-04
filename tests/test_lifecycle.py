from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "pipeline"))
from publisher.checker import check
from publisher.identity import initialize_private, load_private, Registry, Binding
from publisher.lifecycle import apply_plan, make_plan, serialized, digest
from publisher.storage import atomic_write, writer_lock
from publisher.hugo import export_sample

ID_A = "00000000-0000-4000-8000-000000000001"
ID_B = "00000000-0000-4000-8000-000000000002"
POLICY = {"schema_version": 1, "note_roots": ["notes"], "attachment_roots": ["assets"],
          "exclude": [], "unpublished_target": "plain_text"}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lifecycle-synthetic-")
        self.root = Path(self.temp.name)
        self.vault, self.state = self.root / "vault", self.root / "state"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "assets").mkdir()
        self.write("A", ID_A)
        self.write("B", ID_B)
        initialize_private(check(self.vault, POLICY), self.state)
        self.current = self.state / "identity-registry.json"
        self.before = self.current.read_bytes()
        self.old_url = self.registry().get_id(ID_A).permalink

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, identifier, publish=True, body="# heading\n", extra=""):
        path = self.vault / "notes" / (name + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\npublish: {str(publish).lower()}\nid: {identifier}\n{extra}---\n{body}", encoding="utf-8")
        return path

    def registry(self):
        return load_private(self.current, self.vault)

    def plan(self, kind, **fields):
        return make_plan(self.state, self.vault, POLICY, [{"kind": kind, "id": ID_A, **fields}])

    def apply(self, plan):
        return apply_plan(self.state, self.vault, POLICY, plan, plan["plan_sha256"])

    def source_bytes(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}

    def test_plan_does_not_change_state_and_move_preserves_identity_url(self):
        (self.vault / "notes/A.md").rename(self.vault / "notes/中文移动.md")
        source = self.source_bytes()
        plan = self.plan("move", new_path="notes/中文移动.md")
        self.assertEqual(self.current.read_bytes(), self.before)
        self.assertEqual(len(list((self.state / "revisions").glob("*.json"))), 1)
        self.assertTrue(self.apply(plan)["changed"])
        binding = self.registry().get("notes/中文移动.md")
        self.assertEqual((binding.id, binding.permalink), (ID_A, self.old_url))
        self.assertEqual(self.source_bytes(), source)
        self.assertFalse(self.apply(plan)["changed"])

    def test_copy_is_not_move_and_unknown_id_is_not_allocated(self):
        self.write("Copy", ID_A)
        with self.assertRaisesRegex(ValueError, "copies are not moves"):
            self.plan("move", new_path="notes/Copy.md")
        with self.assertRaisesRegex(ValueError, "unknown ID"):
            make_plan(self.state, self.vault, POLICY, [{"kind": "withdraw", "id": "00000000-0000-4000-8000-000000000099"}])

    def test_sidecar_move_needs_no_id_field_or_content_hash_guess(self):
        source = self.vault / "notes/A.md"
        source.write_text("---\npublish: true\n---\n# Changed content and title\n", encoding="utf-8")
        source.rename(self.vault / "notes/Moved.md")
        self.apply(self.plan("move", new_path="notes/Moved.md"))
        self.assertEqual((self.registry().get("notes/Moved.md").id, self.registry().get("notes/Moved.md").permalink),
                         (ID_A, self.old_url))

    def test_withdraw_and_restore_keep_url_and_exclude_content_from_new_sample(self):
        self.write("A", ID_A, publish=False)
        source = self.source_bytes()
        self.apply(self.plan("withdraw"))
        binding = self.registry().get_id(ID_A)
        self.assertEqual((binding.status, binding.permalink), ("withdrawn", self.old_url))
        report = check(self.vault, POLICY, self.registry())
        sample, manifest = export_sample(report, ["notes/B.md"], self.root / "generated")
        self.assertEqual([r["id"] for r in manifest["routes"]], [ID_B])
        self.assertFalse((sample / "content/notes" / ID_A).exists())
        self.assertEqual(self.source_bytes(), source)
        self.write("A", ID_A, publish=True)
        self.assertTrue(check(self.vault, POLICY, self.registry()).failed)
        self.apply(self.plan("restore"))
        self.assertEqual(self.registry().get_id(ID_A).permalink, self.old_url)
        self.assertFalse(check(self.vault, POLICY, self.registry()).failed)

    def test_withdraw_requires_optout_and_missing_source_confirmation(self):
        with self.assertRaisesRegex(ValueError, "publication disabled"):
            self.plan("withdraw")
        (self.vault / "notes/A.md").unlink()
        with self.assertRaisesRegex(ValueError, "allow_missing_source"):
            self.plan("withdraw")
        self.apply(self.plan("withdraw", allow_missing_source=True))
        self.assertEqual(self.registry().get_id(ID_A).status, "withdrawn")

    def test_history_remains_reserved_including_after_withdrawal(self):
        self.apply(self.plan("route", slug="中文路径"))
        binding = self.registry().get_id(ID_A)
        self.assertEqual(binding.permalink, "/notes/中文路径/")
        self.assertEqual(binding.historical_paths, [self.old_url])
        self.apply(self.plan("route", slug=binding.historical_paths[0].split("/")[-2]))
        self.assertEqual(self.registry().get_id(ID_A).historical_paths, ["/notes/中文路径/"])
        self.write("A", ID_A, publish=False)
        self.apply(self.plan("withdraw"))
        with self.assertRaisesRegex(ValueError, "Duplicate registry route"):
            Registry(self.registry().bindings + [Binding("00000000-0000-4000-8000-000000000099", "notes/C.md", permalink="/notes/中文路径/")])

    def test_route_collision_and_original_frontmatter_slug_are_not_silent(self):
        taken = self.registry().get_id(ID_B).slug
        with self.assertRaisesRegex(ValueError, "Duplicate registry route"):
            self.plan("route", slug=taken)
        self.write("A", ID_A, extra="slug: original\n")
        with self.assertRaisesRegex(ValueError, "ROUTE_CHANGE_REQUIRED"):
            self.plan("route", slug="changed")

    def test_changed_source_new_file_config_and_stale_plan_block(self):
        plan = self.plan("route", slug="next")
        self.write("B", ID_B, body="changed\n")
        with self.assertRaisesRegex(ValueError, "changed since planning"):
            self.apply(plan)
        self.assertEqual(self.current.read_bytes(), self.before)
        plan = self.plan("route", slug="next")
        self.write("Added", "00000000-0000-4000-8000-000000000003", publish=False)
        with self.assertRaisesRegex(ValueError, "changed since planning"):
            self.apply(plan)
        plan = self.plan("route", slug="next")
        with self.assertRaisesRegex(ValueError, "changed since planning"):
            apply_plan(self.state, self.vault, {**POLICY, "unpublished_target": "error"}, plan, plan["plan_sha256"])
        self.apply(plan)
        later = self.plan("route", slug="later")
        self.apply(later)
        with self.assertRaisesRegex(ValueError, "stale plan"):
            self.apply(plan)

    def test_plan_tampering_or_wrong_confirmation_does_not_write(self):
        plan = self.plan("route", slug="next")
        with self.assertRaisesRegex(ValueError, "confirmed digest"):
            apply_plan(self.state, self.vault, POLICY, plan, "0" * 64)
        changed = deepcopy(plan)
        changed["target_registry"]["notes"][0]["permalink"] = "/notes/evil/"
        with self.assertRaisesRegex(ValueError, "changed"):
            self.apply(changed)
        self.assertEqual(self.current.read_bytes(), self.before)

    def test_forged_rehashed_target_is_recomputed_not_trusted(self):
        plan = self.plan("route", slug="next")
        plan["target_registry"]["notes"][1]["permalink"] = "/notes/other-article/"
        plan["target_revision"] = digest(serialized(plan["target_registry"]))
        plan["plan_sha256"] = digest(serialized({k: v for k, v in plan.items() if k != "plan_sha256"}))
        with self.assertRaisesRegex(ValueError, "proposed state changed"):
            self.apply(plan)
        self.assertEqual(self.current.read_bytes(), self.before)

    def test_broken_links_and_changed_referenced_attachments_block(self):
        self.write("B", ID_B, body="[[missing]]")
        with self.assertRaisesRegex(ValueError, "BROKEN_LINK"):
            self.plan("route", slug="next")
        self.write("B", ID_B, body="![[image.png]]\n")
        asset = self.vault / "assets/image.png"
        asset.write_bytes(b"synthetic asset bytes")
        plan = self.plan("route", slug="next")
        asset.write_bytes(b"edited synthetic asset bytes")
        with self.assertRaisesRegex(ValueError, "changed since planning"):
            self.apply(plan)
        self.assertEqual(self.current.read_bytes(), self.before)

    def test_failed_current_promotion_preserves_last_good_and_retry_is_idempotent(self):
        plan = self.plan("route", slug="next")
        def interrupted(path, value):
            if path == self.current:
                raise OSError("simulated failure before current promotion")
            atomic_write(path, value)
        with patch("publisher.lifecycle.atomic_write", side_effect=interrupted), self.assertRaises(OSError):
            self.apply(plan)
        self.assertEqual(self.current.read_bytes(), self.before)
        self.assertTrue((self.state / "revisions" / (plan["target_revision"] + ".json")).is_file())
        self.assertTrue(self.apply(plan)["changed"])
        self.assertFalse(self.apply(plan)["changed"])

    def test_failure_after_promotion_recovers_receipt_without_reapplying(self):
        plan = self.plan("route", slug="next")
        def interrupted(path, value):
            if path.parent.name == "transactions" and json.loads(value)["status"] == "committed":
                raise OSError("simulated failure after current promotion")
            atomic_write(path, value)
        with patch("publisher.lifecycle.atomic_write", side_effect=interrupted), self.assertRaises(OSError):
            self.apply(plan)
        self.assertEqual(hashlib.sha256(self.current.read_bytes()).hexdigest(), plan["target_revision"])
        self.assertFalse(self.apply(plan)["changed"])
        receipt = json.loads((self.state / "transactions" / (plan["plan_sha256"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "committed")

    def test_source_change_after_staging_keeps_current_unchanged(self):
        plan = self.plan("route", slug="next")
        def source_changed(path, value):
            atomic_write(path, value)
            if path.parent.name == "transactions":
                self.write("B", ID_B, body="Synthetic concurrent editor change\n")
        with patch("publisher.lifecycle.atomic_write", side_effect=source_changed), self.assertRaisesRegex(ValueError, "before promotion"):
            self.apply(plan)
        self.assertEqual(self.current.read_bytes(), self.before)

    def test_batch_resolves_multiple_moves_without_automatic_guessing(self):
        (self.vault / "notes/A.md").rename(self.vault / "notes/First.md")
        (self.vault / "notes/B.md").rename(self.vault / "notes/Second.md")
        plan = make_plan(self.state, self.vault, POLICY, [
            {"kind": "move", "id": ID_A, "new_path": "notes/First.md"},
            {"kind": "move", "id": ID_B, "new_path": "notes/Second.md"}])
        self.apply(plan)
        self.assertEqual(self.registry().get("notes/Second.md").id, ID_B)

    def test_lock_foreign_vault_missing_revision_and_bad_paths_block(self):
        plan = self.plan("route", slug="next")
        with writer_lock(self.state), self.assertRaisesRegex(ValueError, "locked"):
            self.apply(plan)
        with self.assertRaisesRegex(ValueError, "another Vault"):
            make_plan(self.state, self.root / "other-vault", POLICY, plan["operations"])
        (self.state / "revisions" / (hashlib.sha256(self.before).hexdigest() + ".json")).unlink()
        with self.assertRaisesRegex(ValueError, "immutable revision"):
            self.apply(plan)

    def test_reparse_state_paths_and_in_vault_state_are_rejected_before_write(self):
        plan = self.plan("route", slug="next")
        with patch("publisher.lifecycle.Path.is_junction", return_value=True), self.assertRaisesRegex(ValueError, "junctions"):
            self.apply(plan)
        with self.assertRaisesRegex(ValueError, "disjoint"):
            make_plan(self.vault / "state", self.vault, POLICY, plan["operations"])
        self.assertFalse((self.vault / "state").exists())
        self.assertEqual(self.current.read_bytes(), self.before)

    def test_unknown_private_metadata_is_not_silently_discarded(self):
        data = json.loads(self.before.decode("utf-8"))
        data["notes"][0]["future_private_metadata"] = "must not be lost"
        value = serialized(data)
        atomic_write(self.state / "revisions" / (digest(value) + ".json"), value)
        atomic_write(self.current, value)
        with self.assertRaisesRegex(ValueError, "do not discard metadata"):
            self.plan("route", slug="next")
        self.assertEqual(self.current.read_text(encoding="utf-8"), value)

    def test_cli_plan_and_digest_confirmed_apply_touch_only_synthetic_private_state(self):
        policy = self.root / "pipeline/policy.toml"
        policy.parent.mkdir()
        policy.write_text('schema_version=1\nnote_roots=["notes"]\nattachment_roots=["assets"]\nexclude=[]\nunpublished_target="plain_text"\n', encoding="utf-8")
        reports = self.root / "reports"
        before = self.source_bytes()
        # Audit hooks intentionally live for the process lifetime. Keep the real
        # write guard in an isolated child so the parent can clean synthetic data.
        code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import publish; "
                "publish.PROJECT=Path(sys.argv[2]); publish.STATE_ROOT=Path(sys.argv[3]); "
                "publish.REPORT_ROOT=Path(sys.argv[4]); raise SystemExit(publish.main(sys.argv[5:]))")
        def cli(arguments):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(PROJECT / "pipeline"),
                                     str(self.root), str(self.state), str(reports), *arguments],
                                    capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        common = ["--vault", str(self.vault), "--policy", str(policy)]
        cli(["identity-plan", *common, "--operation", "route", "--id", ID_A, "--slug", "next"])
        path = reports / "publisher/identity-plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(self.current.read_bytes(), self.before)
        cli(["identity-apply", *common, "--plan", str(path), "--expect-plan", plan["plan_sha256"]])
        self.assertEqual(self.source_bytes(), before)
        self.assertEqual(self.registry().get_id(ID_A).permalink, "/notes/next/")


if __name__ == "__main__":
    unittest.main()
