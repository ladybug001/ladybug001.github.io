from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "pipeline"))
from publisher.checker import check
from publisher.identity import prepare_initial_registry
from publisher.hugo import export_sample
from publisher.snapshot import (SnapshotError, inspect_snapshot, materialize_snapshot,
                                pack_snapshot, payload, restore, separate_anchors,
                                sha, validate, verify_snapshot_html, _tree)
from publisher.htmlcheck import verify_html
from publisher.tooling import local_hugo


POLICY = {"schema_version": 1, "note_roots": ["notes"], "attachment_roots": ["assets"],
          "exclude": [], "unpublished_target": "plain_text"}
HUGO = local_hugo()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="snapshot-test-")
        self.root = Path(self.temp.name)
        self.vault = self.root / "PRIVATE-VAULT"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "assets").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def note(self, name, body, header="publish: true"):
        path = self.vault / "notes" / (name + ".md")
        path.write_text("---\n" + header + "\n---\n" + body, encoding="utf-8")

    def fixture(self, selected=None):
        self.note("A", "# A\n[[B#中文标题|标题链接]] [[B#^p]] [[Private|SECRET_LABEL]] [[Unpublished|不可点击]]\n"
                  "![[图.png]]\n\n![[B#中文标题]]\n\n脚注[^1]\n\n[^1]: [[B]]\n\n"
                  "| 列 |\n|---|\n| [[B\\|别名]] |\n",
                  "publish: true\nprivate_detail: SECRET_FRONTMATTER\naliases: [公开别名]\nupdated: 2020-01-02\ntags: [技术]")
        self.note("B", "# 中文标题\n段落 ^p\n\n```sh\necho hi\n```\n^code\n\n[[A]]\n"
                  "\n`[[Literal]] %% {#example}`\n\n```md\n[[Literal]] %% {#example}\n```\n\n"
                  "$$\nx^2\n$$\n")
        self.note("Private", "SECRET_PRIVATE_BODY", "private: true")
        self.note("Unpublished", "SECRET_UNPUBLISHED_BODY", "publish: false")
        (self.vault / "assets" / "图.png").write_bytes(b"fixture-image")
        initial = check(self.vault, POLICY)
        registry = prepare_initial_registry(initial)
        report = check(self.vault, POLICY, registry)
        self.assertFalse(report.failed, report.issues)
        return export_sample(report, selected or ["notes/A.md", "notes/B.md"], self.root / "samples")

    def packed(self):
        sample, _ = self.fixture()
        snapshot, manifest = pack_snapshot(sample, self.root / "snapshots")
        return sample, snapshot, manifest

    def updated(self, files, name, value):
        files = dict(files)
        files[name] = payload(value)
        manifest = json.loads(files["manifest.json"])
        manifest["files"][name] = sha(files[name])
        files["manifest.json"] = payload(manifest)
        return files

    def test_snapshot_without_vault_private_state_and_portable_roundtrip(self):
        sample, _ = self.fixture()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.vault.rglob("*") if p.is_file()}
        original_open = Path.open
        def guarded(path, *args, **kwargs):
            if path.is_relative_to(self.vault) or ".local/state" in path.as_posix():
                raise AssertionError("Snapshot operation read Vault/private state")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            snapshot, manifest = pack_snapshot(sample, self.root / "snapshots")
            files, _, pages = inspect_snapshot(snapshot)
            generated, _ = materialize_snapshot(snapshot, self.root / "hugo")
            again, _ = pack_snapshot(sample, self.root / "snapshots")
            self.assertEqual(snapshot, again)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        combined = b"\n".join(files.values()).decode("utf-8")
        for secret in ("PRIVATE-VAULT", "SECRET_FRONTMATTER", "SECRET_PRIVATE_BODY", "SECRET_UNPUBLISHED_BODY", "SECRET_LABEL", "notes/A.md", "source_path"):
            self.assertNotIn(secret, combined)
        self.assertFalse(manifest["deployable"])
        self.assertTrue(manifest["validation_only"])
        for identifier, page in pages.items():
            markdown = "content/notes/" + identifier + "/index.md"
            self.assertEqual((sample / markdown).read_bytes(), (generated / markdown).read_bytes())
            self.assertNotIn("{#h-", page["body"])
            self.assertNotIn('{id="b-', page["body"])
        self.assertIn("updated", combined)
        self.assertNotIn('"lastmod"', combined)
        self.assertNotIn('"params"', combined)
        self.assertEqual(len(manifest["identities"]), 2)

    def test_missing_selected_target_blocks_before_any_output(self):
        sample, _ = self.fixture(["notes/A.md"])
        with self.assertRaises(SnapshotError) as error:
            pack_snapshot(sample, self.root / "snapshots")
        self.assertTrue(any("MISSING_SNAPSHOT_TARGET" in e for e in error.exception.errors))
        self.assertFalse((self.root / "snapshots").exists())

    def test_selecting_closed_explicit_subset_does_not_expand(self):
        self.note("A", "# A\n[[A#A]]\n")
        self.note("B", "# B\n[[C]]\n")
        self.note("C", "# C\n")
        initial = check(self.vault, POLICY)
        report = check(self.vault, POLICY, prepare_initial_registry(initial))
        sample, manifest = export_sample(report, ["notes/A.md", "notes/B.md"], self.root / "samples")
        with self.assertRaises(SnapshotError):
            pack_snapshot(sample, self.root / "snapshots")
        a = next(n.note_id for n in report.notes if n.name == "A")
        snapshot, result = pack_snapshot(sample, self.root / "snapshots", [a])
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(len(result["identities"]), 1)
        self.assertEqual(len(result["link_graph"]), 1)
        with self.assertRaisesRegex(ValueError, "Select"):
            pack_snapshot(sample, self.root / "snapshots", [a, a])

    def test_sample_hash_inventory_and_existing_snapshot_tampering(self):
        sample, snapshot, _ = self.packed()
        page = next((sample / "content").rglob("index.md"))
        original = page.read_bytes()
        page.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            pack_snapshot(sample, self.root / "snapshots")
        page.write_bytes(original)
        (snapshot / "private-report.txt").write_text("sensitive", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            inspect_snapshot(snapshot)
        with self.assertRaisesRegex(ValueError, "Immutable"):
            pack_snapshot(sample, self.root / "snapshots")

    def test_unknown_private_fields_duplicate_json_and_deployable_flag(self):
        _, snapshot, _ = self.packed()
        files = _tree(snapshot)
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        for modified in ({**page, "source_path": "private"}, {**page, "metadata": {**page["metadata"], "private": True}}):
            with self.assertRaisesRegex(ValueError, "fields"):
                validate(self.updated(files, name, modified))
        manifest = json.loads(files["manifest.json"])
        manifest["deployable"] = True
        with self.assertRaisesRegex(ValueError, "non-deployable"):
            validate({**files, "manifest.json": payload(manifest)})
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            validate({**files, "manifest.json": b'{"schema_version":1,"schema_version":1}'})

    def test_duplicate_routes_and_invalid_metadata(self):
        _, snapshot, _ = self.packed()
        files = _tree(snapshot)
        manifest = json.loads(files["manifest.json"])
        manifest["routes"].append(manifest["routes"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate page"):
            validate({**files, "manifest.json": payload(manifest)})
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        for metadata in ({"title": "A", "date": "yesterday"}, {"title": "A", "tags": "string"}):
            with self.assertRaisesRegex(ValueError, "Invalid page metadata"):
                validate(self.updated(files, name, {**page, "metadata": metadata}))

    def test_unsafe_schemes_raw_html_wiki_hidden_comments_and_shortcodes(self):
        _, snapshot, _ = self.packed()
        files = _tree(snapshot)
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        for addition in ("<script>alert(1)</script>", "[[Missing]]", "%% SECRET %%",
                         "[evil](javascript:alert)", "[local](file:///private)",
                         "```md\n{{< readfile >}}\n```", "{{% include %}}", "{#unreviewed}"):
            with self.subTest(addition=addition), self.assertRaises(ValueError):
                validate(self.updated(files, name, {**page, "body": page["body"] + "\n\n" + addition + "\n"}))

    def test_missing_anchor_and_graph_not_trusted_over_markdown(self):
        _, snapshot, _ = self.packed()
        files = _tree(snapshot)
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        destination = page["url"] + "#missing"
        with self.assertRaises(SnapshotError) as error:
            validate(self.updated(files, name, {**page, "body": page["body"] + "\n[bad](" + destination + ")\n"}))
        self.assertTrue(any("MISSING_SNAPSHOT_ANCHOR" in e for e in error.exception.errors))
        manifest = json.loads(files["manifest.json"])
        manifest["link_graph"] = [e for e in manifest["link_graph"] if not e["rendered"]]
        with self.assertRaisesRegex(SnapshotError, "GRAPH_MARKDOWN_MISMATCH"):
            validate({**files, "manifest.json": payload(manifest)})

    def test_anchor_ir_literal_protection_and_structural_offsets(self):
        body = '# Heading {#h-one}\n\n```md {id="b-code"}\n{#h-one}\n[[literal]]\n```\n\ntext\n{#b-paragraph}\n'
        neutral, insertions = separate_anchors(body, ["h-one", "b-code", "b-paragraph"])
        self.assertIn("\n{#h-one}\n[[literal]]", neutral)
        self.assertEqual(restore(neutral, insertions), body)
        with self.assertRaisesRegex(ValueError, "exactly once"):
            separate_anchors(body, ["h-one", "b-code", "b-paragraph", "missing"])
        _, snapshot, _ = self.packed()
        files = _tree(snapshot)
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        page["anchor_insertions"][0]["offset"] = 0
        with self.assertRaises(ValueError):
            validate(self.updated(files, name, page))

    def test_svg_validation_and_unrelated_asset_refused_even_with_new_hash(self):
        _, snapshot, _ = self.packed()
        files = _tree(snapshot)
        raw = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
        resource = sha(raw) + ".svg"
        files["assets/" + resource] = raw
        manifest = json.loads(files["manifest.json"])
        manifest["files"]["assets/" + resource] = sha(raw)
        files["manifest.json"] = payload(manifest)
        with self.assertRaisesRegex(ValueError, "Unreferenced"):
            validate(files)
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        page["resources"] = sorted(page["resources"] + [resource])
        page["body"] += "\n![](" + page["url"] + "media/" + resource + ")\n"
        with self.assertRaises(ValueError):
            validate(self.updated(files, name, page))

    def test_local_url_traversal_query_unknown_assets_and_unused_resources(self):
        _, snapshot, _ = self.packed()
        files = _tree(snapshot)
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        for url in ("../outside/", "/notes/%252e%252e/", page["url"] + "?download=1", "media/missing.png"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate(self.updated(files, name, {**page, "body": page["body"] + "\n[bad](" + url + ")\n"}))
        with self.assertRaisesRegex(SnapshotError, "UNUSED_PAGE_RESOURCE"):
            validate(self._unused_asset_files(files))

    def _unused_asset_files(self, files):
        # Add an otherwise valid, unused resource without moving existing anchors.
        raw = b"unused"
        resource = sha(raw) + ".png"
        files = dict(files)
        files["assets/" + resource] = raw
        manifest = json.loads(files["manifest.json"])
        manifest["files"]["assets/" + resource] = sha(raw)
        files["manifest.json"] = payload(manifest)
        name = next(n for n in files if n.startswith("pages/"))
        page = json.loads(files[name])
        page["resources"] = sorted(page["resources"] + [resource])
        return self.updated(files, name, page)

    def test_junction_input_and_output_not_followed(self):
        if sys.platform != "win32":
            self.skipTest("Windows junction safety test")
        sample, snapshot, _ = self.packed()
        junction = self.root / "linked"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(snapshot)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with self.assertRaisesRegex(ValueError, "symlinks?/junctions?"):
            inspect_snapshot(junction)
        output_target = self.root / "output-target"
        output_target.mkdir()
        output_link = self.root / "output-link"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(output_link), str(output_target)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with self.assertRaisesRegex(ValueError, "symlinks?/junctions?"):
            materialize_snapshot(snapshot, output_link / "output")

    def test_changed_input_during_pack_cannot_promote(self):
        sample, _ = self.fixture()
        original = _tree
        count = 0
        def changed(path):
            nonlocal count
            files = original(path)
            if Path(path) == sample:
                count += 1
                if count == 2:
                    return {**files, "concurrent.txt": b"changed"}
            return files
        with patch("publisher.snapshot._tree", changed), self.assertRaisesRegex(ValueError, "changed during packing"):
            pack_snapshot(sample, self.root / "snapshots")
        self.assertFalse((self.root / "snapshots").exists())

    def test_input_output_overlap_and_changed_hugo_release_refused(self):
        sample, snapshot, _ = self.packed()
        with self.assertRaisesRegex(ValueError, "disjoint"):
            pack_snapshot(sample, sample / "nested")
        with self.assertRaisesRegex(ValueError, "disjoint"):
            materialize_snapshot(snapshot, snapshot / "nested")
        generated, _ = materialize_snapshot(snapshot, self.root / "hugo")
        page = next((generated / "content").rglob("index.md"))
        page.write_bytes(page.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "Immutable"):
            materialize_snapshot(snapshot, self.root / "hugo")

    @unittest.skipUnless(HUGO.is_file(), "Checksum-verified pinned Hugo required")
    def test_real_hugo_offline_snapshot_rebuild_and_strict_html(self):
        sample, snapshot, _ = self.packed()
        # Make original source unavailable. This is a temporary synthetic fixture,
        # never the real Vault. Snapshot operations cannot rely on these paths.
        self.vault.rename(self.root / "unavailable-vault")
        sample.rename(self.root / "unavailable-sample")
        generated, manifest = materialize_snapshot(snapshot, self.root / "hugo")
        def build(destination):
            result = subprocess.run([str(HUGO), "--source", str(PROJECT / "pipeline/hugo-test"),
                                     "--contentDir", str(generated / "content"), "--destination", str(destination),
                                     "--cacheDir", str(self.root / "cache"), "--noBuildLock", "--noChmod", "--noTimes"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        public = self.root / "public"
        build(public)
        verified = verify_snapshot_html(snapshot, generated, public)
        self.assertTrue(verified["passed"], verified)
        self.assertTrue(verified["strict"])
        self.assertEqual(verified["outside_sample_not_verified"], [])
        build(self.root / "repeat")
        self.assertEqual(_tree(public), _tree(self.root / "repeat"))
        with self.assertRaisesRegex(ValueError, "forbids"):
            verify_html(public, {**manifest, "outside_sample_urls": ["/any/"]}, strict=True)
        (public / "stale-private.html").write_text("stale", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale/unexpected"):
            verify_snapshot_html(snapshot, generated, public)


if __name__ == "__main__":
    unittest.main()
