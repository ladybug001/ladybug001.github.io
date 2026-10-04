from __future__ import annotations
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
from publisher.identity import initialize_private, load_private
from publisher.lifecycle import make_plan, apply_plan
from publisher.publication import prepare, apply, current_release, conversion_review
from publisher.snapshot import inspect_snapshot, _tree, _io_path, restore, validate
from publisher.storage import atomic_write, publication_head
from publisher.tooling import local_hugo
from ci import build_snapshot

POLICY = {"schema_version": 1, "note_roots": ["notes"], "attachment_roots": ["assets"], "exclude": [], "unpublished_target": "plain_text"}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="full-publication-synthetic-")
        self.root = Path(self.temp.name)
        self.vault, self.state = self.root / "vault", self.root / "state"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "assets").mkdir()
        self.note("A", "# A\n")
        initialize_private(check(self.vault, POLICY), self.state)
        self.original = (self.state / "identity-registry.json").read_bytes()

    def tearDown(self):
        # Hugo/Go can create long Windows paths even where Python's ordinary
        # temporary-tree cleanup cannot reopen them. Keep the same exact target.
        self.temp.name = str(_io_path(self.root))
        self.temp.cleanup()

    def note(self, name, body, publish=True, extra=""):
        path = self.vault / "notes" / (name + ".md")
        path.write_text(f"---\npublish: {str(publish).lower()}\n{extra}---\n{body}", encoding="utf-8")
        return path

    def plan(self, ops=None, time="2026-10-03T00:00:00+00:00"):
        return prepare(self.state, self.vault, POLICY, ops, observed_at=time)[0]

    def commit(self, plan):
        return apply(self.state, self.vault, POLICY, plan, plan["plan_sha256"], build_root=self.root / "build-checks")

    def registry(self):
        return load_private(self.state / "identity-registry.json", self.vault)

    def test_full_snapshot_more_than_twelve_pages_new_ids_privacy_and_noop(self):
        for i in range(15):
            self.note("Note" + str(i), "[[A]]\n", extra="private_field: MUST_NOT_EXPORT\n")
        self.note("Private", "DO_NOT_EXPORT", publish=False)
        plan = self.plan()
        self.assertEqual(plan["summary"]["pages"], 16)
        self.assertEqual(len(plan["proposed_operations"]), 15)
        self.assertEqual((self.state / "identity-registry.json").read_bytes(), self.original)
        result = self.commit(plan)
        snapshot = Path(result["snapshot"])
        files, manifest, pages = inspect_snapshot(snapshot)
        self.assertEqual(len(pages), 16)
        self.assertNotIn(b"MUST_NOT_EXPORT", b"\n".join(files.values()))
        self.assertNotIn(b"DO_NOT_EXPORT", b"\n".join(files.values()))
        self.assertEqual(len(self.registry().bindings), 16)
        self.assertFalse(self.commit(plan)["changed"])
        next_plan = self.plan(time="2026-10-04T00:00:00+00:00")
        self.assertEqual(next_plan["target_head"], plan["target_head"])
        self.assertFalse(self.commit(next_plan)["changed"])

    def test_withdraw_all_restores_same_url_no_old_body_or_asset(self):
        self.note("A", "![[only.png]]\n")
        (self.vault / "assets/only.png").write_bytes(b"synthetic asset")
        first = self.commit(self.plan())
        old = self.registry().get("notes/A.md")
        self.note("A", "private after withdrawal", publish=False)
        plan = self.plan(time="2026-10-04T00:00:00+00:00")
        self.assertEqual(plan["summary"]["pages"], 0)
        result = self.commit(plan)
        files, manifest, pages = inspect_snapshot(result["snapshot"])
        self.assertEqual(pages, {})
        self.assertEqual(manifest["redirects"], [])
        self.assertEqual(manifest["reservations"][0]["status"], "withdrawn")
        self.assertEqual(set(files), {"manifest.json"})
        self.assertTrue(Path(first["snapshot"]).exists()) # history is retained, not magically erased.
        self.note("A", "# Restored\n")
        self.commit(self.plan(time="2026-10-05T00:00:00+00:00"))
        self.assertEqual(self.registry().get("notes/A.md").permalink, old.permalink)

    def test_move_and_historical_redirect_offline_hugo_build(self):
        self.commit(self.plan())
        binding = self.registry().get("notes/A.md")
        self.note("A", "# Edited title\n").rename(self.vault / "notes/移动.md")
        with self.assertRaisesRegex(ValueError, "Missing bound source"):
            self.plan()
        ops = [{"kind": "move", "id": binding.id, "new_path": "notes/移动.md"}, {"kind": "route", "id": binding.id, "slug": "中文路径"}]
        result = self.commit(self.plan(ops, time="2026-10-04T00:00:00+00:00"))
        snapshot = Path(result["snapshot"])
        manifest = inspect_snapshot(snapshot)[1]
        self.assertEqual(manifest["redirects"], [{"id": binding.id, "from": binding.permalink, "to": "/notes/中文路径/"}])
        self.assertTrue(build_snapshot(snapshot, local_hugo(), self.root / "build")["passed"])
        redirect = self.root / "build/first/public" / binding.permalink.lstrip("/") / "index.html"
        self.assertIn("location.hash", redirect.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "Paired publication"):
            p = make_plan(self.state, self.vault, POLICY, [{"kind": "route", "id": binding.id, "slug": "other"}])
            apply_plan(self.state, self.vault, POLICY, p, p["plan_sha256"])

    def test_stale_plan_changed_sources_and_bad_digest_do_not_switch_head(self):
        self.commit(self.plan())
        original_head = (self.state / "publication-head.json").read_bytes()
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, "reviewed digest"):
            apply(self.state, self.vault, POLICY, plan, "0" * 64)
        self.note("A", "new content\n")
        # A no-op replay cannot switch to a different release or publish edits.
        self.assertFalse(self.commit(plan)["changed"])
        newer = self.plan(time="2026-10-04T00:00:00+00:00")
        self.note("A", "second edit\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.commit(newer)
        self.assertEqual((self.state / "publication-head.json").read_bytes(), original_head)

    def test_failure_before_paired_promotion_and_same_plan_recovery(self):
        plan = self.plan()
        def interrupted(path, value):
            data = json.loads(value)
            if path.name == "publication-head.json" and data["release"] is not None:
                raise OSError("simulated crash before switching whole release")
            atomic_write(path, value)
        with patch("publisher.publication.atomic_write", side_effect=interrupted), self.assertRaises(OSError):
            self.commit(plan)
        self.assertIsNone(publication_head(self.state)["release"])
        self.assertEqual(self.registry().to_dict()["notes"][0]["source_path"], "notes/A.md")
        self.assertTrue(self.commit(plan)["changed"])
        self.assertFalse(self.commit(plan)["changed"])

    def test_semantic_updated_is_observed_once_not_file_mtime_or_migration_date(self):
        result = self.commit(self.plan())
        page = next(iter(inspect_snapshot(result["snapshot"])[2].values()))
        self.assertNotIn("date", page["metadata"])
        self.assertNotIn("updated", page["metadata"])
        self.note("A", "# Changed\n")
        result = self.commit(self.plan(time="2026-10-04T00:00:00+00:00"))
        page = next(iter(inspect_snapshot(result["snapshot"])[2].values()))
        self.assertEqual(page["metadata"]["updated"], "2026-10-04T00:00:00+00:00")
        again = self.plan(time="2026-10-05T00:00:00+00:00")
        self.assertEqual(again["target_head"], publication_head(self.state))

    def test_missing_pointer_after_publication_is_not_silently_reinitialized(self):
        self.commit(self.plan())
        (self.state / "publication-head.json").unlink()
        with self.assertRaisesRegex(ValueError, "pointer missing"):
            self.registry()

    def test_renderer_failure_preserves_last_good_release_and_vault_bytes(self):
        self.commit(self.plan())
        head = publication_head(self.state)
        path = self.note("A", "# New body\n")
        source = path.read_bytes()
        plan = self.plan(time="2026-10-04T00:00:00+00:00")
        with patch("ci.build_snapshot", side_effect=ValueError("renderer failed")), self.assertRaisesRegex(ValueError, "renderer failed"):
            self.commit(plan)
        self.assertEqual(publication_head(self.state), head)
        self.assertEqual(path.read_bytes(), source)
        self.assertTrue(self.commit(plan)["changed"])

    def test_change_during_build_preserves_last_good_release(self):
        self.commit(self.plan())
        head = publication_head(self.state)
        self.note("A", "first edit\n")
        plan = self.plan(time="2026-10-04T00:00:00+00:00")
        def concurrent_save(*args):
            self.note("A", "saved during renderer\n")
            return {"passed": True}
        with patch("ci.build_snapshot", side_effect=concurrent_save), self.assertRaisesRegex(ValueError, "before paired"):
            self.commit(plan)
        self.assertEqual(publication_head(self.state), head)

    def test_mathml_real_renderer_and_invalid_latex_fail_before_promotion(self):
        self.note("A", "# Math\n\nInline $x^2$.\n\n$$\n\\frac{a}{b}\n$$\n\n$A \\times [B]_补$\n")
        result = self.commit(self.plan())
        binding = self.registry().get("notes/A.md")
        html = (Path(result["verified_build"]) / "first/public" / binding.permalink.lstrip("/") / "index.html").read_text(encoding="utf-8")
        self.assertIn("<math", html)
        self.assertIn("<mfrac>", html)
        self.assertIn("补", html)
        head = publication_head(self.state)
        self.note("A", "$\\notarealcommand{x}$\n")
        with self.assertRaises(Exception):
            self.commit(self.plan(time="2026-10-04T00:00:00+00:00"))
        self.assertEqual(publication_head(self.state), head)

    def test_withdrawal_drops_history_redirects_without_releasing_routes(self):
        binding = self.registry().get("notes/A.md")
        self.commit(self.plan([{"kind": "route", "id": binding.id, "slug": "new-route"}]))
        self.note("A", "secret", publish=False)
        result = self.commit(self.plan(time="2026-10-04T00:00:00+00:00"))
        manifest = inspect_snapshot(result["snapshot"])[1]
        self.assertEqual(manifest["redirects"], [])
        self.assertEqual(manifest["reservations"][0]["history"], [binding.permalink])
        self.note("B", "# Collision\n", extra="slug: " + binding.slug + "\n")
        with self.assertRaises(ValueError):
            self.plan(time="2026-10-05T00:00:00+00:00")

    def test_broken_link_and_unpublished_embed_fail_without_state_writes(self):
        for body in ("[[Missing]]\n", "![[Private]]\n"):
            self.note("Private", "private", publish=False)
            self.note("A", body)
            with self.assertRaises(ValueError):
                self.plan()
            self.assertIsNone(publication_head(self.state))
            self.assertEqual((self.state / "identity-registry.json").read_bytes(), self.original)

    def test_backwards_clock_and_release_tampering_fail_closed(self):
        result = self.commit(self.plan())
        self.note("A", "changed")
        with self.assertRaisesRegex(ValueError, "clock moved backwards"):
            self.plan(time="2026-10-02T00:00:00+00:00")
        snapshot = Path(result["snapshot"])
        (snapshot / "manifest.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            current_release(self.state)

    def test_nested_quote_paragraph_anchor_real_hugo_and_portable_round_trip(self):
        self.note("A", "[[B#^p]]\n\n![[B#^p]]\n")
        self.note("B", "> [!NOTE] Outer\n> first paragraph\n>\n> second paragraph ^p\n>\n> third paragraph\n")
        result = self.commit(self.plan())
        files, manifest, pages = inspect_snapshot(result["snapshot"])
        page = next(p for p in pages.values() if p["metadata"]["title"] == "B")
        self.assertEqual([a["kind"] for a in page["anchor_insertions"]], ["quote-block"])
        html = (Path(result["verified_build"]) / "first/public" / page["url"].lstrip("/") / "index.html").read_text(encoding="utf-8")
        self.assertIn('<p id="b-p">second paragraph', html)
        self.assertNotIn('<blockquote id="b-p">', html)

    def test_deep_conversion_review_is_read_only_and_collects_all_html_rejections(self):
        self.note("A", "<script>alert('never render')</script>\n")
        self.note("B", "<div>unreviewed</div>\n")
        self.note("C", "plain new note")
        report = conversion_review(check(self.vault, POLICY, self.registry()))
        self.assertEqual(sum(i.code == "CONVERSION_REJECTED" for i in report.issues), 1)
        self.assertEqual(sum(i.code == "CONVERSION_IDENTITY_PENDING" for i in report.issues), 2)
        self.assertEqual((self.state / "identity-registry.json").read_bytes(), self.original)
        self.assertIsNone(publication_head(self.state))

    def test_cli_complete_plan_apply_build_without_vault_on_build(self):
        policy = self.root / "pipeline/policy.toml"
        policy.parent.mkdir()
        policy.write_text('schema_version=1\nnote_roots=["notes"]\nattachment_roots=["assets"]\nexclude=[]\nunpublished_target="plain_text"\n', encoding="utf-8")
        reports = self.root / "reports"
        before = (self.vault / "notes/A.md").read_bytes()
        code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import publish; "
                "publish.PROJECT=Path(sys.argv[2]); publish.STATE_ROOT=Path(sys.argv[3]); "
                "publish.REPORT_ROOT=Path(sys.argv[4]); raise SystemExit(publish.main(sys.argv[5:]))")
        def cli(arguments):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(PROJECT / "pipeline"),
                                     str(self.root), str(self.state), str(reports), *arguments],
                                    capture_output=True, encoding="utf-8", timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        common = ["--vault", str(self.vault), "--policy", str(policy)]
        cli(["publication-plan", *common, "--observed-at", "2026-10-03T00:00:00+00:00"])
        path = reports / "publisher/publication-plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(publication_head(self.state))
        cli(["publication-apply", *common, "--plan", str(path), "--expect-plan", plan["plan_sha256"]])
        _, release, _ = current_release(self.state)
        cli(["build", "--snapshot", str(release / "snapshot")])
        self.assertEqual((self.vault / "notes/A.md").read_bytes(), before)

    def test_table_line_breaks_survive_but_attribute_html_is_rejected(self):
        self.note("A", "| X |\n|---|\n| a<br>b<br/>c<br />d |\n")
        result = self.commit(self.plan())
        page = next(iter(inspect_snapshot(result["snapshot"])[2].values()))
        html = (Path(result["verified_build"]) / "first/public" / page["url"].lstrip("/") / "index.html").read_text(encoding="utf-8")
        self.assertIn("a<br>b<br/>c<br />d", html)
        for content in ('<br onclick="bad()">', '<br style="display:none">', '<img src="secret">', '<!-- hidden -->', '<div>x</div>'):
            self.note("A", "| X |\n|---|\n| " + content + " |\n")
            with self.assertRaises(ValueError):
                self.plan(time="2026-10-04T00:00:00+00:00")

    def test_quote_anchor_prefix_cannot_inject_html_or_unsupported_container(self):
        for prefix in ('<script>', '> <br onclick="bad()">', '- ', '>\n', '>' * 9):
            with self.assertRaises(ValueError):
                restore('text\n', [{"offset": 0, "kind": "quote-block", "id": "b-p", "prefix": prefix}])
        with self.assertRaises(ValueError):
            restore('text\n', [None])

    def test_in_memory_snapshot_limits_are_checked_before_install(self):
        from fixture import fixture_files
        files = fixture_files()
        with patch("publisher.snapshot.MAX_FILE", 1), self.assertRaisesRegex(ValueError, "budget"):
            validate(files)
        with patch("publisher.snapshot.MAX_TOTAL", 1), self.assertRaisesRegex(ValueError, "budget"):
            validate(files)

    def test_interleaved_heading_and_block_anchors_use_source_order(self):
        self.note("A", "# First\n\nparagraph ^p\n\n# Second\n\n> [!NOTE] callout\n>\n> inner paragraph ^q\n\n# Third\n")
        result = self.commit(self.plan())
        _, manifest, pages = inspect_snapshot(result["snapshot"])
        insertions = next(iter(pages.values()))["anchor_insertions"]
        self.assertEqual(manifest["routes"][0]["anchors"], [item["id"] for item in insertions])
        self.assertEqual([item["kind"] for item in insertions], ["heading", "block", "heading", "quote-block", "heading"])

    def test_marker_only_paragraph_continuation_preserves_exact_anchor(self):
        self.note("A", "[[B#^p]] [[B#^q]]\n\n![[B#^q]]\n")
        self.note("B", "paragraph\n^p\n\n> [!NOTE] Callout\n>\n> inner paragraph\n>  ^q\n")
        result = self.commit(self.plan())
        _, manifest, pages = inspect_snapshot(result["snapshot"])
        page = next(p for p in pages.values() if p["metadata"]["title"] == "B")
        html = (Path(result["verified_build"]) / "first/public" / page["url"].lstrip("/") / "index.html").read_text(encoding="utf-8")
        self.assertIn('<p id="b-p">paragraph', html)
        self.assertIn('<p id="b-q">inner paragraph', html)


if __name__ == "__main__":
    unittest.main()
