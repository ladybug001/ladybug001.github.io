from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "pipeline"))

from publisher.checker import check, load_policy
from publisher.frontmatter import FrontmatterError, parse, validate
from publisher.identity import Binding, Registry
from publisher.identity import initialize_private, load_private, prepare_initial_registry
from publisher.compatibility import legacy_targets, validate_overrides
from publisher.storage import writer_lock
from publisher.presentation import UNAVAILABLE_MESSAGE, unavailable_text


ID_A = "00000000-0000-4000-8000-000000000001"
ID_B = "00000000-0000-4000-8000-000000000002"
POLICY = {"schema_version": 1, "note_roots": ["notes"], "attachment_roots": ["assets"],
          "exclude": [], "unpublished_target": "error"}


class VaultFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="publisher-test-")
        self.vault = Path(self.temp.name)
        (self.vault / "notes").mkdir()
        (self.vault / "assets").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def note(self, name, body="", header="publish: true", folder="notes"):
        path = self.vault / folder / (name + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{header}\n---\n{body}", encoding="utf-8")
        return path

    def run_check(self, registry=None):
        return check(self.vault, POLICY, registry)

    def codes(self, report):
        return [i.code for i in report.issues]


class FrontmatterTests(unittest.TestCase):
    def test_strict_boolean(self):
        for value in ('"true"', "yes", "1", "null"):
            data, _, _ = parse(f"---\npublish: {value}\n---\n")
            self.assertTrue(validate(data))
        self.assertEqual(validate(parse("---\npublish: true\n---\n")[0]), [])

    def test_duplicates_unsafe_tags_and_yaml_aliases(self):
        for value in ("publish: true\npublish: false", "x: !!python/object:evil {}", "x: &a [1]\ny: *a"):
            with self.assertRaises(FrontmatterError):
                parse(f"---\n{value}\n---\n")

    def test_bom_crlf_date_and_lists(self):
        data, body, line = parse("\ufeff---\r\npublish: true\r\ndate: 2020-02-29\r\ntags: [os/windows]\r\n---\r\n# 文本")
        self.assertEqual(body, "# 文本")
        self.assertEqual(line, 6)
        self.assertEqual(validate(data), [])
        self.assertTrue(validate({"date": "2023-02-29"}))
        self.assertTrue(validate({"updated": "2024-01-01T10:00:00"}))
        self.assertTrue(validate({"tags": "tag"}))

    def test_unclosed_and_nonmapping(self):
        for raw in ("---\ntitle: a", "---\n- a\n---"):
            with self.assertRaises(FrontmatterError):
                parse(raw)

    def test_safe_loader_does_not_change_global_yaml_boolean_rules(self):
        import yaml
        self.assertEqual(yaml.safe_load("value: yes"), {"value": True})
        self.assertEqual(parse("---\nvalue: yes\n---\n")[0], {"value": "yes"})


class LinkTests(VaultFixture):
    def test_heading_block_alias_unicode_and_relative(self):
        self.note("中文", "# 标题\n## 子标题\n内容 ^block-1\n", "publish: true\naliases: [别名]")
        self.note("A", "[[中文]] [[别名|名称]] [[中文#标题]] [[中文#标题#子标题|子]]\n"
                  "[[中文#^block-1]] [[notes/中文]] [[./中文]]\n")
        report = self.run_check()
        self.assertFalse(report.failed, [asdict(i) for i in report.issues if i.severity == "error"])
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertEqual(len(refs), 7)
        self.assertTrue(all(r.status == "resolved_note" for r in refs))

    def test_protected_literals_and_comments(self):
        self.note("A", "`[[missing]]`\n\n```md\n[[missing2]]\n```\n\n"
                  "    [[missing3]]\n\n$[[missing4]]$\n\n$$\n[[missing5]]\n$$\n\n"
                  "\\([[missing6]]\\)\n\n%% [[hidden]]\n\n[[hidden2]] %%\n\n"
                  "`%%` [[B]]\n\n\\[[escaped]]\n")
        self.note("B")
        report = self.run_check()
        self.assertNotIn("BROKEN_LINK", self.codes(report))
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertEqual([r.target for r in refs], ["B"])

    def test_distinguish_broken_private_draft_outside(self):
        self.note("A", "[[missing]] [[private]] [[draft]] [[Diary]]")
        self.note("private", header="publish: true\nprivate: true")
        self.note("draft", header="publish: true\ndraft: true")
        self.note("Diary", "private body", folder="diary")
        report = self.run_check()
        for code in ("BROKEN_LINK", "UNPUBLISHED_TARGET", "OUT_OF_SCOPE_TARGET", "PRIVATE_PUBLISH_CONFLICT"):
            self.assertIn(code, self.codes(report))
        diary = next(n for n in report.notes if n.name == "Diary")
        self.assertEqual(diary.body, "")

    def test_same_name_explicit_path_and_alias_ambiguity(self):
        self.note("B", "# one", folder="notes/first")
        self.note("B", "# two", folder="notes/second")
        self.note("C", header="publish: true\naliases: [B]")
        self.note("A", "[[B]] [[notes/first/B]]")
        report = self.run_check()
        self.assertIn("AMBIGUOUS_TARGET", self.codes(report))
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertEqual(refs[1].status, "resolved_note")

    def test_heading_and_block_validation(self):
        self.note("B", "# same\n# same\ntext ^dup\n\ntext ^dup\n")
        self.note("A", "[[B#missing]] [[B#same]] [[B#^missing]] [[B#^dup]]")
        report = self.run_check()
        for code in ("INVALID_HEADING", "AMBIGUOUS_HEADING", "INVALID_BLOCK", "DUPLICATE_BLOCK_ID", "AMBIGUOUS_BLOCK"):
            self.assertIn(code, self.codes(report))

    def test_markdown_relative_links_and_reference_style(self):
        self.note("中文")
        self.note("A", "[普通](%E4%B8%AD%E6%96%87.md) [引用][id]\n\n[id]: 中文.md\n")
        report = self.run_check()
        self.assertNotIn("BROKEN_LINK", self.codes(report))
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertEqual(len(refs), 2)
        self.assertEqual([r.status for r in refs], ["resolved_note", "resolved_note"])

    def test_table_wikilink(self):
        self.note("B")
        self.note("A", "a | b\n---|---\n[[B\\|显示]] | `[[ignore]]`\n")
        report = self.run_check()
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertEqual([r.target for r in refs], ["B"])
        self.assertFalse(report.failed)

    def test_embed_cycle_but_link_cycle_allowed(self):
        a = self.note("A", "![[B]]")
        b = self.note("B", "![[A]]")
        self.assertIn("EMBED_CYCLE", self.codes(self.run_check()))
        a.write_text("---\npublish: true\n---\n[[B]]", encoding="utf-8")
        b.write_text("---\npublish: true\n---\n[[A]]", encoding="utf-8")
        self.assertNotIn("EMBED_CYCLE", self.codes(self.run_check()))

    def test_invalid_target_and_unclosed_wiki(self):
        self.note("A", "[[../..//escape]] [[file:C:/secret]] [[broken\n")
        codes = self.codes(self.run_check())
        self.assertIn("INVALID_TARGET", codes)
        self.assertIn("INVALID_WIKILINK", codes)

    def test_source_locations(self):
        self.note("A", "text\n\n[[missing]]\n")
        issue = next(i for i in self.run_check().issues if i.code == "BROKEN_LINK")
        self.assertEqual((issue.line, issue.column), (6, 1))

    def test_bracketed_markdown_label_and_footnotes(self):
        self.note("A", "[[原创]文章](https://example.org/)\n\n正文[^ref2]，再次[^ref2]。\n\n"
                  "[^ref2]: 参考资料  \n    https://example.org/\n")
        report = self.run_check()
        self.assertFalse(report.failed, [asdict(i) for i in report.issues if i.severity == "error"])
        self.assertNotIn("ref2", report.notes[0].blocks)

    def test_heading_code_formatting(self):
        self.note("B", "# 核心函数：`SwapContext`\n")
        self.note("A", "[[B#核心函数：`SwapContext`]]")
        self.assertFalse(self.run_check().failed)

    def test_self_block_and_heading_embed_not_false_cycle(self):
        self.note("A", "# 内容\n文字 ^piece\n\n# 引用\n![[A#^piece]]\n![[A#内容]]\n")
        self.assertNotIn("EMBED_CYCLE", self.codes(self.run_check()))

    def test_self_block_recursion_is_cycle(self):
        self.note("A", "![[A#^piece]] ^piece\n")
        self.assertIn("EMBED_CYCLE", self.codes(self.run_check()))

    def test_note_name_with_dot_is_not_asset(self):
        self.note("v1.2")
        self.note("A", "[[v1.2]]")
        self.assertFalse(self.run_check().failed)

    def test_malformed_markdown_not_reported_as_wikilink(self):
        self.note("A", "[[原创]文章](https://example.org/\n")
        codes = self.codes(self.run_check())
        self.assertIn("INVALID_MARKDOWN_LINK", codes)
        self.assertNotIn("INVALID_WIKILINK", codes)

    def test_unpublished_notes_do_not_add_dependencies(self):
        self.note("A", "[[missing]] ![[missing.png]]", "publish: false")
        report = self.run_check()
        self.assertFalse(report.failed)
        self.assertEqual(report.assets, {})
        self.assertEqual(report.notes[0].references, [])

    def test_callout_mermaid_math_are_inspected_not_rendered(self):
        self.note("A", "> [!note] title\n> [[B]]\n\n```mermaid\ngraph TD; A-->B\n```\n\n$x^2$\n")
        self.note("B")
        report = self.run_check()
        features = next(n.features for n in report.notes if n.name == "A")
        self.assertEqual((features["callout"], features["mermaid"], features["math"]), (1, 1, 1))
        self.assertFalse(report.failed)

    def test_dynamic_blocks_and_unclosed_comments_fail(self):
        self.note("A", "```dataview\nTABLE x\n```\n\n%% hidden\n")
        codes = self.codes(self.run_check())
        self.assertIn("UNSUPPORTED_DYNAMIC_BLOCK", codes)
        self.assertIn("UNCLOSED_COMMENT", codes)


class AttachmentTests(VaultFixture):
    def test_only_referenced_and_no_copy(self):
        (self.vault / "assets/pic.png").write_bytes(b"test-image")
        (self.vault / "assets/unused.png").write_bytes(b"unused")
        self.note("A", "![[pic.png]] ![pic](../assets/pic.png)")
        report = self.run_check()
        self.assertFalse(report.failed)
        self.assertEqual(list(report.assets), ["assets/pic.png"])
        self.assertEqual(report.assets["assets/pic.png"]["sha256"], hashlib.sha256(b"test-image").hexdigest())

    def test_missing_and_ambiguous_attachment(self):
        for folder in ("assets/a", "assets/b"):
            (self.vault / folder).mkdir()
            (self.vault / folder / "pic.png").write_bytes(b"image")
        self.note("A", "![[pic.png]] ![[missing.png]] ![[assets/a/pic.png]]")
        report = self.run_check()
        self.assertIn("AMBIGUOUS_ATTACHMENT", self.codes(report))
        self.assertIn("MISSING_ATTACHMENT", self.codes(report))
        self.assertEqual(len(report.assets), 1)


class UnavailablePresentationTests(VaultFixture):
    def plain_check(self):
        return check(self.vault, {**POLICY, "unpublished_target": "plain_text"})

    def test_ordinary_unpublished_and_outside_links_become_warning_text(self):
        self.note("A", "[[B|别名]] [[diary/D]] [未公开](B.md)")
        self.note("B", header="publish: false")
        self.note("D", folder="diary")
        report = self.plain_check()
        self.assertFalse(report.failed)
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertEqual([r.presentation["text"] for r in refs], ["别名", "D", "未公开"])
        self.assertEqual(report.to_dict()["summary"]["nonclickable_references"], 3)
        self.assertTrue(all(r.presentation["message"] == UNAVAILABLE_MESSAGE for r in refs))
        self.assertTrue(all("href" not in r.presentation for r in refs))
        self.assertTrue(all(i.severity == "warning" for i in report.issues))

    def test_explicit_private_note_does_not_expose_its_label_or_metadata(self):
        self.note("A", "[[B|秘密名称]]")
        self.note("B", "秘密正文 ![[secret.png]]", "publish: false\nprivate: true\ntitle: 秘密标题")
        report = self.plain_check()
        ref = next(n for n in report.notes if n.name == "A").references[0]
        self.assertEqual(ref.presentation["text"], "未公开笔记")
        self.assertNotIn("秘密", json.dumps(ref.presentation, ensure_ascii=False))
        self.assertEqual(report.assets, {})

    def test_softened_references_never_expand_target_or_publish_it(self):
        self.note("A", "[[B#does-not-exist|原文文字]]")
        self.note("B", "secret", "publish: false")
        report = self.plain_check()
        self.assertFalse(report.failed)
        self.assertEqual(sum(n.published for n in report.notes), 1)
        self.assertEqual(next(n for n in report.notes if n.name == "A").references[0].presentation["text"], "原文文字")

    def test_embed_broken_ambiguous_and_missing_asset_remain_errors(self):
        self.note("A", "![[B]] [[missing]] [[same]] ![[missing.png]]")
        self.note("B", header="publish: false")
        self.note("same", folder="notes/one")
        self.note("same", folder="notes/two")
        report = self.plain_check()
        codes = self.codes(report)
        for code in ("UNPUBLISHED_TARGET", "BROKEN_LINK", "AMBIGUOUS_TARGET", "MISSING_ATTACHMENT"):
            self.assertIn(code, codes)
        self.assertTrue(report.failed)
        self.assertEqual(report.to_dict()["summary"]["nonclickable_references"], 0)

    def test_alias_is_data_not_html_and_no_paths_fragments_or_ids_are_added(self):
        self.note("A", '[[diary/B#private-heading|<script>alert(1)</script>]] [[diary/B#private-heading]]')
        self.note("B", folder="diary")
        report = self.plain_check()
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertEqual(refs[0].presentation["action"], "plain_text")
        self.assertEqual(refs[1].presentation["text"], "B")
        for ref in refs:
            self.assertEqual(set(ref.presentation), {"action", "text", "message"})
            self.assertNotIn("diary/", json.dumps(ref.presentation))
            self.assertNotIn("private-heading", json.dumps(ref.presentation))

    def test_policy_validation_supports_only_implemented_choices(self):
        policy = self.vault / "policy.toml"
        prefix = 'schema_version=1\nnote_roots=["notes"]\nattachment_roots=["assets"]\nexclude=[]\n'
        for choice in ("plain_text", "error"):
            policy.write_text(prefix + f'unpublished_target="{choice}"\n', encoding="utf-8")
            self.assertEqual(load_policy(policy)["unpublished_target"], choice)
        policy.write_text(prefix + 'unpublished_target="explanation_page"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_policy(policy)


class IdentityTests(VaultFixture):
    def test_title_changes_and_explicit_move_keep_route(self):
        registry = Registry([Binding(ID_A, "notes/A.md", "stable", "/notes/stable/")])
        self.note("A", header="publish: true\ntitle: new title")
        note = next(n for n in self.run_check(registry).notes if n.name == "A")
        self.assertEqual((note.note_id, note.permalink), (ID_A, "/notes/stable/"))
        moved = registry.bind_move(ID_A, "notes/folder/A.md")
        self.assertEqual(moved.get("notes/folder/A.md").permalink, "/notes/stable/")
        self.assertIsNotNone(registry.get("notes/A.md"))  # Pure, no mutation.

    def test_duplicate_id_slug_and_history_conflict(self):
        self.note("A", header=f"publish: true\nid: {ID_A}\nslug: taken")
        self.note("B", header=f"publish: true\nid: {ID_A}\nslug: taken")
        registry = Registry([Binding(ID_B, "notes/Old.md", "old", "/notes/old/", ["/notes/taken/"], "withdrawn")])
        codes = self.codes(self.run_check(registry))
        for code in ("DUPLICATE_ID", "DUPLICATE_SLUG", "URL_CONFLICT"):
            self.assertIn(code, codes)

    def test_registry_route_and_move_collisions(self):
        for paths in (["/notes/a/", "/notes/A/"], ["/notes/中文/", "/notes/%E4%B8%AD%E6%96%87/"]):
            with self.assertRaises(ValueError):
                Registry([Binding(ID_A, "notes/A.md", permalink=paths[0]),
                          Binding(ID_B, "notes/B.md", permalink=paths[1])])
        for value in ("https://x/", "/notes/../private/", "/notes/%2E%2E/private/"):
            with self.assertRaises(ValueError):
                Registry([Binding(ID_A, "notes/A.md", permalink=value)])

    def test_no_id_allocation_and_repeatable_check(self):
        self.note("A", "# title")
        first = self.run_check().to_dict()
        second = self.run_check().to_dict()
        self.assertEqual(first, second)
        self.assertIsNone(first["note_index"][0]["id"])
        self.assertIn("UNBOUND_IDENTITY", first["summary"]["issue_codes"])

    def test_id_conflict_slug_change_and_withdrawal_are_not_silent(self):
        self.note("A", header=f"publish: true\nid: {ID_B}\nslug: changed")
        registry = Registry([Binding(ID_A, "notes/A.md", "old", "/notes/old/", status="withdrawn")])
        codes = self.codes(self.run_check(registry))
        for code in ("IDENTITY_CONFLICT", "ROUTE_CHANGE_REQUIRED", "REACTIVATION_REQUIRED"):
            self.assertIn(code, codes)

    def test_explicit_move_to_occupied_source_is_rejected(self):
        registry = Registry([Binding(ID_A, "notes/A.md"), Binding(ID_B, "notes/B.md")])
        with self.assertRaises(ValueError):
            registry.bind_move(ID_A, "notes/B.md")


class SafetyTests(VaultFixture):
    def test_vault_hashes_unchanged_and_outside_body_unread(self):
        self.note("A", "[[B]]")
        self.note("B")
        private = self.note("Diary", "do not read", folder="diary")
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian/app.json").write_text('{"value":1}', encoding="utf-8")
        def snapshot():
            return {p.relative_to(self.vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.vault.rglob("*") if p.is_file()}
        before = snapshot()
        from unittest.mock import patch
        read_bytes = Path.read_bytes
        def guarded_read(path):
            if path == private:
                raise AssertionError("Out-of-scope body was read")
            return read_bytes(path)
        with patch.object(Path, "read_bytes", guarded_read):
            report = self.run_check()
        self.assertTrue(report.read_verified)
        self.assertEqual(before, snapshot())

    def test_runtime_guard_blocks_write_delete_and_rename(self):
        self.note("A")
        script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from publisher.safety import install_vault_write_guard
root = Path(sys.argv[2])
install_vault_write_guard(root)
blocked = 0
for op in [lambda: (root/'new.md').write_text('x'), lambda: (root/'notes/A.md').unlink(),
           lambda: (root/'notes/A.md').rename(root/'moved.md'), lambda: (root/'folder').mkdir()]:
    try: op()
    except PermissionError: blocked += 1
assert blocked == 4, blocked
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(PROJECT / "pipeline"), str(self.vault)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.vault / "notes/A.md").exists())

    def test_cli_rejects_reports_inside_vault(self):
        result = subprocess.run([sys.executable, "-B", str(PROJECT / "pipeline/publish.py"), "check",
                                 "--vault", str(self.vault), "--no-local-state", "--report-dir", str(self.vault / "report")],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.vault / "report").exists())

    def test_detect_concurrent_source_change(self):
        self.note("A")
        from unittest.mock import patch
        with patch("publisher.checker._hash", return_value="changed"):
            report = self.run_check()
        self.assertIn("SOURCE_CHANGED", self.codes(report))
        self.assertFalse(report.read_verified)

    def test_no_report_cli_does_not_create_files(self):
        self.note("A")
        policy = self.vault / "policy.toml"
        policy.write_text('schema_version=1\nnote_roots=["notes"]\nattachment_roots=["assets"]\nexclude=[]\n', encoding="utf-8")
        before = {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        result = subprocess.run([sys.executable, "-B", str(PROJECT / "pipeline/publish.py"), "check",
                                 "--vault", str(self.vault), "--policy", str(policy), "--no-local-state", "--no-report"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_unsafe_policy_root_rejected(self):
        policy = self.vault / "policy.toml"
        policy.write_text('schema_version=1\nnote_roots=["../private"]\nattachment_roots=["assets"]\nexclude=[]\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_policy(policy)


class CompatibilityTests(VaultFixture):
    def test_legacy_app_uri_requires_explicit_mapping(self):
        self.note("New")
        self.note("A", "[old](app://obsidian.md/Old)")
        data = {"legacy_note_targets": [{"old_target": "Old", "source_path": "notes/New.md", "reason": "verified rename"}]}
        report = check(self.vault, POLICY, legacy_targets=legacy_targets(data))
        self.assertFalse(report.failed)
        self.assertEqual(next(n for n in report.notes if n.name == "A").references[0].target_path, "notes/New.md")
        self.assertIn("BROKEN_LINK", self.codes(self.run_check()))

    def test_reused_legacy_name_is_ambiguous_not_silently_redirected(self):
        self.note("New")
        self.note("Old")
        self.note("A", "[[Old]]")
        report = check(self.vault, POLICY, legacy_targets={"old": "notes/New.md"})
        self.assertIn("LEGACY_LINK_CONFLICT", self.codes(report))

    def test_missing_legacy_target_remains_error(self):
        self.note("A", "[[Old]]")
        report = check(self.vault, {**POLICY, "unpublished_target": "plain_text"}, legacy_targets={"old": "notes/New.md"})
        self.assertIn("BROKEN_LEGACY_TARGET", self.codes(report))

    def test_app_uri_is_resolved_globally_not_relative_or_launched(self):
        self.note("中文 B", folder="notes/target")
        self.note("A", "[目标](app://obsidian.md/%E4%B8%AD%E6%96%87%20B)", folder="notes/source")
        report = self.run_check()
        self.assertFalse(report.failed)
        ref = next(n for n in report.notes if n.name == "A").references[0]
        self.assertEqual(ref.target_path, "notes/target/中文 B.md")
        self.assertEqual(ref.status, "resolved_note")

    def test_app_uri_never_weakens_scheme_or_path_validation(self):
        self.note("A", "[a](app://evil.example/B) [b](app://obsidian.md/../B) "
                  "[c](app://obsidian.md/B?q=x) [d](file:///secret)")
        report = self.run_check()
        # CommonMark rejects file:// before it can become a link token; remaining
        # app forms must be rejected by our resolver, never marked external.
        self.assertEqual(self.codes(report).count("INVALID_TARGET"), 3)
        refs = next(n for n in report.notes if n.name == "A").references
        self.assertTrue(all(r.status == "invalid_target" for r in refs))

    def test_hash_bound_repair_changes_memory_only(self):
        path = self.note("A", "[[原创]标题](https://example.org/\n")
        before = path.read_bytes()
        repair = {"source_path": "notes/A.md", "source_sha256": hashlib.sha256(before).hexdigest(),
                  "expected": "[[原创]标题](https://example.org/\n",
                  "replacement": "[[原创]标题](https://example.org/)\n", "reason": "fix closing parenthesis"}
        repairs = validate_overrides({"schema_version": 1, "repairs": [repair]})
        report = check(self.vault, POLICY, repairs=repairs)
        self.assertFalse(report.failed)
        self.assertIn("SOURCE_REPAIR_APPLIED", self.codes(report))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(report.read_verified)

    def test_stale_or_ambiguous_repair_is_not_guessed(self):
        path = self.note("A", "[[原创]标题](https://example.org/\n")
        repair = {"source_path": "notes/A.md", "source_sha256": "0" * 64,
                  "expected": "[[原创]标题](https://example.org/\n",
                  "replacement": "[[原创]标题](https://example.org/)\n", "reason": "fix"}
        report = check(self.vault, POLICY, repairs=[repair])
        self.assertIn("STALE_SOURCE_REPAIR", self.codes(report))
        before = path.read_bytes()
        repair["source_sha256"] = hashlib.sha256(before).hexdigest()
        repair["expected"] = "not present"
        report = check(self.vault, POLICY, repairs=[repair])
        self.assertIn("STALE_SOURCE_REPAIR", self.codes(report))
        self.assertEqual(path.read_bytes(), before)


class PersistentIdentityTests(VaultFixture):
    def setUp(self):
        super().setUp()
        self.state_temp = tempfile.TemporaryDirectory(prefix="publisher-state-test-")
        self.state = Path(self.state_temp.name)

    def tearDown(self):
        self.state_temp.cleanup()
        super().tearDown()

    def test_initialization_is_idempotent_and_source_is_unchanged(self):
        source = self.note("A", "# content")
        before = source.read_bytes()
        registry, created = initialize_private(self.run_check(), self.state)
        current = self.state / "identity-registry.json"
        state_before = current.read_bytes()
        second, created_again = initialize_private(self.run_check(), self.state)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(registry.to_dict(), second.to_dict())
        self.assertEqual(state_before, current.read_bytes())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(len(list((self.state / "revisions").glob("*.json"))), 1)
        bound = check(self.vault, POLICY, load_private(current, self.vault))
        self.assertEqual(bound.to_dict()["summary"]["bound_published_notes"], 1)

    def test_failed_check_does_not_create_state(self):
        self.note("A", "[[missing]]")
        with self.assertRaises(ValueError):
            initialize_private(self.run_check(), self.state / "new")
        self.assertFalse((self.state / "new").exists())

    def test_state_inside_vault_is_rejected(self):
        self.note("A")
        with self.assertRaises(ValueError):
            initialize_private(self.run_check(), self.vault / "state")
        self.assertFalse((self.vault / "state").exists())

    def test_revision_survives_failed_current_write_without_reallocation(self):
        self.note("A")
        from unittest.mock import patch
        from publisher.storage import atomic_write
        def interrupted(path, value):
            if path.name == "identity-registry.json":
                raise OSError("simulated interrupted current write")
            atomic_write(path, value)
        with patch("publisher.identity.atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                initialize_private(self.run_check(), self.state)
        self.assertEqual(len(list((self.state / "revisions").glob("*.json"))), 1)
        with self.assertRaises(ValueError):
            initialize_private(self.run_check(), self.state)

    def test_unknown_move_stops_instead_of_rebinding_by_hash(self):
        path = self.note("A")
        registry = prepare_initial_registry(self.run_check())
        path.rename(path.with_name("Moved.md"))
        report = check(self.vault, POLICY, registry)
        self.assertIn("MISSING_IDENTITY_SOURCE", self.codes(report))
        self.assertTrue(report.failed)
        self.assertIsNone(next(n for n in report.notes if n.name == "Moved").note_id)

    def test_embedded_id_can_carry_move_without_changing_route(self):
        path = self.note("A", header=f"publish: true\nid: {ID_A}")
        registry = prepare_initial_registry(self.run_check())
        old_url = registry.get("notes/A.md").permalink
        path.rename(path.with_name("Moved.md"))
        report = check(self.vault, POLICY, registry)
        self.assertFalse(report.failed)
        self.assertEqual(report.notes[0].permalink, old_url)

    def test_registry_vault_ownership_is_enforced(self):
        self.note("A")
        initialize_private(self.run_check(), self.state)
        with self.assertRaises(ValueError):
            load_private(self.state / "identity-registry.json", self.state)

    def test_writer_lock_prevents_concurrent_initialization(self):
        self.note("A")
        with writer_lock(self.state):
            with self.assertRaises(ValueError):
                initialize_private(self.run_check(), self.state)
        self.assertFalse((self.state / "identity-registry.json").exists())


if __name__ == "__main__":
    unittest.main()
