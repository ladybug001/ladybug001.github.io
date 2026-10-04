from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from publisher.checker import check
from publisher.identity import prepare_initial_registry
from publisher.hugo import convert, export_sample, reference_spans, validate_svg
from publisher.htmlcheck import verify_html
from publisher.hugo import Context
from publisher.svg import transform_svg
from publisher.compatibility import svg_rules
from publisher.tooling import local_hugo


POLICY = {"schema_version": 1, "note_roots": ["notes"], "attachment_roots": ["assets"],
          "exclude": [], "unpublished_target": "plain_text"}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hugo-adapter-test-")
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "assets").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def note(self, name, body, header="publish: true"):
        path = self.vault / "notes" / (name + ".md")
        path.write_text("---\n" + header + "\n---\n" + body, encoding="utf-8")
        return path

    def report(self):
        initial = check(self.vault, POLICY)
        self.assertFalse(initial.failed, [i for i in initial.issues if i.severity == "error"])
        registry = prepare_initial_registry(initial)
        return check(self.vault, POLICY, registry)

    def test_table_repeated_cells_and_escaped_pipe_exact_spans(self):
        body = "| A | A |\n|---|---|\n| [[B\\|alias]] | [[B\\|alias]] |\n"
        spans = reference_spans(body)
        self.assertEqual([body[a:b] for a, b in spans], ["[[B\\|alias]]"] * 2)
        self.assertNotEqual(spans[0], spans[1])

    def test_table_literal_hidden_marker_protection(self):
        self.note("A", "| X | X |\n|---|---|\n| `a\\|%% [[missing]] %%` | `a\\|%% [[missing]] %%` |\n")
        report = self.report()
        result, _, _ = convert(report.notes[0], report)
        self.assertEqual(result.count("%% [[missing]] %%"), 2)

    def test_tab_indented_list_literal_and_link_spans(self):
        self.note("A", "1. First `code`\n\tcontinuation `%% [[missing]] %%` and [[B]]\n")
        self.note("B", "# B\n")
        report = self.report()
        result, _, graph = convert(next(n for n in report.notes if n.name == "A"), report)
        self.assertIn("`%% [[missing]] %%`", result)
        self.assertEqual(len(graph), 1)
        self.assertNotIn("and [[B]]", result)

    def test_links_assets_literals_hidden_comments_plain_text_and_schema(self):
        self.note("A", "# 标题\n[[B#段落|显示]] [[私有]] [[未发布|[危险](https://secret.test)]]\n"
                  "![[图.png]]\n\n`[[B]]`\n\n```md\n[[B]]\n```\n\n%% [[hidden]] %%\n",
                  "publish: true\naliases: [note-alias]\nupdated: 2020-01-02")
        self.note("B", "# 段落\ntext ^block\n")
        self.note("私有", "DO NOT EXPORT", "private: true")
        self.note("未发布", "DO NOT EXPORT", "publish: false")
        (self.vault / "assets" / "图.png").write_bytes(b"image-test")
        report = self.report()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.vault.rglob("*") if p.is_file()}
        note = next(n for n in report.notes if n.name == "A")
        markdown, assets, graph = convert(note, report)
        self.assertIn("未公开笔记", markdown)
        self.assertNotIn("[危险](https://secret.test)", markdown)
        self.assertNotIn("hidden", markdown)
        self.assertIn("`[[B]]`", markdown)
        self.assertIn("```md\n[[B]]", markdown)
        self.assertEqual(len(assets), 1)
        self.assertEqual(len(graph), 1)
        self.assertIn('"lastmod": "2020-01-02"', markdown)
        self.assertNotIn('"aliases":', markdown)
        destination, manifest = export_sample(report, ["notes/A.md", "notes/B.md"], self.root / "generated")
        self.assertFalse(manifest["deployable"])
        self.assertNotIn("source_path", json.dumps(manifest))
        self.assertNotIn("私有", json.dumps(manifest))
        again, _ = export_sample(report, ["notes/B.md", "notes/A.md"], self.root / "generated")
        self.assertEqual(destination, again)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        assetfile = next(destination.rglob("*.png"))
        assetfile.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "immutable sample files"):
            export_sample(report, ["notes/A.md", "notes/B.md"], self.root / "generated")

    def test_fail_closed_embed_raw_html_and_changed_source(self):
        self.note("A", "![[B]]\n")
        self.note("B", "# 标题\n")
        report = self.report()
        result, _, graph = convert(next(n for n in report.notes if n.name == "A"), report)
        self.assertIn("# 标题", result)
        self.assertTrue(any(e["kind"] == "wiki_embed" and not e["rendered"] for e in graph))
        self.note("A", "<script>alert(1)</script>\n")
        report = self.report()
        with self.assertRaisesRegex(ValueError, "Raw HTML"):
            convert(next(n for n in report.notes if n.name == "A"), report)
        self.note("A", "changed\n")
        report = self.report()
        self.note("A", "concurrent change\n")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            export_sample(report, ["notes/B.md"], self.root / "generated")
        self.assertFalse((self.root / "generated").exists())

    def test_no_output_inside_vault_or_parent(self):
        self.note("A", "plain\n")
        report = self.report()
        for target in (self.vault / "generated", self.root):
            with self.assertRaisesRegex(ValueError, "disjoint"):
                export_sample(report, ["notes/A.md"], target)

    def test_embed_scopes_assets_and_unavailable_dependencies(self):
        self.note("A", "# Host\n![[B#Section]]\n\n![[B#^code]]\n\n![[B#Section]]\n")
        self.note("B", "# Outside\nMUST NOT INCLUDE\n\n# Section\n正文[^same] [[未发布|保留文字]]\n![[../assets/图.png]]\n"
                  "\n```sh\necho BLOCK\n```\n^code\n\n# After\nMUST NOT INCLUDE AFTER\n\n[^same]: Footnote [[C]]\n")
        self.note("C", "# C\n")
        self.note("未发布", "NEVER EXPORT", "publish: false")
        (self.vault / "assets" / "图.png").write_bytes(b"relative-image")
        report = self.report()
        host = next(n for n in report.notes if n.name == "A")
        context = Context(host, report)
        result, assets, graph = convert(host, report, context)
        self.assertNotIn("MUST NOT INCLUDE", result)
        self.assertNotIn("NEVER EXPORT", result)
        self.assertIn("保留文字", result)
        self.assertEqual(result.count("echo BLOCK"), 3)
        self.assertEqual(result.count("Footnote"), 2)
        self.assertEqual(len(assets), 1)
        self.assertEqual(len(context.anchors), len(set(context.anchors)))
        self.assertTrue(all(e["rendered_on_id"] == host.note_id for e in graph))
        self.assertEqual(len([e for e in graph if e["kind"] == "wiki_embed"]), 3)
        self.assertTrue(all(host.permalink + name in result for name in assets))

    def test_embed_self_block_cycle_private_inline_and_budget_guards(self):
        self.note("A", "```sh\necho SAFE\n```\n^code\n\n![[#^code]]\n")
        report = self.report()
        result, _, _ = convert(report.notes[0], report)
        self.assertEqual(result.count("echo SAFE"), 2)
        self.note("A", "prefix ![[B]] suffix\n")
        self.note("B", "safe\n")
        report = self.report()
        with self.assertRaisesRegex(ValueError, "standalone"):
            convert(next(n for n in report.notes if n.name == "A"), report)
        self.note("A", "![[B]]\n")
        report = self.report()
        host = next(n for n in report.notes if n.name == "A")
        context = Context(host, report)
        context.expansions = 128
        with self.assertRaisesRegex(ValueError, "limit"):
            convert(host, report, context)
        context = Context(host, report)
        context.stack = [(host.path, "")]
        with self.assertRaisesRegex(ValueError, "cycle"):
            convert(host, report, context)
        self.note("B", "SECRET", "publish: false")
        checked = check(self.vault, POLICY)
        self.assertTrue(checked.failed)
        self.assertIn("UNPUBLISHED_TARGET", [i.code for i in checked.issues])

    def test_footnote_namespace_protects_literals_and_detects_duplicate_definitions(self):
        self.note("A", "![[B]]\n\n![[B]]\n\nHost[^1]\n\n[^1]: Host footnote\n")
        self.note("B", "Embed[^1]\n\n`[^1]`\n\n[^1]: Child footnote\n")
        report = self.report()
        result, _, _ = convert(next(n for n in report.notes if n.name == "A"), report)
        self.assertEqual(result.count("`[^1]`"), 2)
        import re
        definitions = re.findall(r"\[\^(f-[a-f0-9]+)\]:", result)
        self.assertEqual(len(definitions), 3)
        self.assertEqual(len(set(definitions)), 3)
        self.note("A", "Text[^x]\n\n[^x]: first\n\n[^X]: conflicting\n")
        report = self.report()
        with self.assertRaisesRegex(ValueError, "conflicting"):
            convert(next(n for n in report.notes if n.name == "A"), report)

    def test_block_and_heading_attributes(self):
        self.note("A", "# 标题 ##\n段落第一行\n段落第二行 ^p\n\n```sh\necho hello\n```\n^code\n")
        report = self.report()
        self.assertEqual(report.notes[0].blocks["p"], [6])
        result, _, _ = convert(report.notes[0], report)
        self.assertIn("{#h-", result)
        self.assertIn("{#b-p}", result)
        self.assertIn('```sh {id="b-code"}', result)
        self.assertNotIn("^p", result)

    def test_math_interrupts_paragraph_and_setext_range_is_real_heading(self):
        self.note("A", "# A\nparagraph\n$$\nx\n=\ny\n$$\n\nSetext first\nsecond line\n---\n")
        report = self.report()
        note = report.notes[0]
        self.assertEqual(len(note.headings), 2)
        self.assertEqual(note.features["math"], 1)
        result, _, _ = convert(note, report)
        self.assertIn("second line {#h-", result)
        self.assertNotIn("paragraph {#", result)

    def test_embedded_reference_definitions_do_not_conflict_or_leak(self):
        self.note("A", "[Host][same]\n\n![[B#Section]]\n\n[same]: https://host.test\n")
        self.note("B", "# Section\n[Child][same] ![Original alt][picture]\n\n# Other\n[same]: https://child.test\n[picture]: https://images.test/a.png\n")
        report = self.report()
        result, _, _ = convert(next(n for n in report.notes if n.name == "A"), report)
        self.assertIn("[Host](https://host.test)", result)
        self.assertIn("[Child](https://child.test)", result)
        self.assertIn("![Original alt](https://images.test/a.png)", result)
        self.assertNotIn("[same]:", result)

    def test_conservative_svg_admission(self):
        validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>')
        for inside in (b'<script/>', b'<image href="https://x"/>', b'<path onclick="x"/>',
                       b'<use href="https://x"/>', b'<path style="fill:url(https://x)"/>'):
            with self.assertRaises(ValueError):
                validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg">' + inside + b'</svg>')
        with self.assertRaises(ValueError):
            validate_svg(b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>')
        with self.assertRaises(ValueError):
            validate_svg(b'<?xml-stylesheet href="https://x"?><svg xmlns="http://www.w3.org/2000/svg"/>')

    def test_reviewed_svg_static_profile_tail_css_determinism_and_source_hash(self):
        raw = b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">' + b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:v="http://schemas.microsoft.com/visio/2003/SVGExtensions/"><style type="text/css">.st1 {fill:#fff;font-size:1em}</style><g class="st1"><text><v:paragraph/>VISIBLE<v:tabList/> TAIL</text></g></svg>'
        rule = {"source_path": "assets/legacy.svg", "source_sha256": hashlib.sha256(raw).hexdigest(),
                "profile": "visio-static-v1", "reason": "reviewed test fixture"}
        self.assertEqual(svg_rules({"svg_profiles": [rule]}), {"assets/legacy.svg": rule})
        output, receipt = transform_svg(raw, rule)
        validate_svg(output)
        self.assertNotIn(b"DOCTYPE", output)
        self.assertNotIn(b"SVGExtensions", output)
        self.assertNotIn(b"style", output)
        self.assertIn(b'fill="#fff"', output)
        self.assertIn(b"VISIBLE TAIL", output)
        self.assertEqual(receipt["output_sha256"], hashlib.sha256(output).hexdigest())
        self.assertEqual((output, receipt), transform_svg(raw, rule))
        with self.assertRaisesRegex(ValueError, "Stale"):
            transform_svg(raw + b" ", rule)
        for bad in (raw.replace(b"fill:#fff", b"fill:url(https://evil.test)"),
                    raw.replace(b"<g class", b'<script/><g class'),
                    raw.replace(b"<g class", b'<g onclick="evil" class')):
            bad_rule = {**rule, "source_sha256": hashlib.sha256(bad).hexdigest()}
            with self.assertRaises(ValueError):
                transform_svg(bad, bad_rule)

    def test_stale_svg_rule_blocks_check_and_only_output_asset_hash_is_published(self):
        self.note("A", "![[legacy.svg]]\n")
        raw = b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        (self.vault / "assets" / "legacy.svg").write_bytes(raw)
        rule = {"source_path": "assets/legacy.svg", "source_sha256": hashlib.sha256(raw).hexdigest(),
                "profile": "visio-static-v1", "reason": "reviewed fixture"}
        initial = check(self.vault, POLICY)
        registry = prepare_initial_registry(initial)
        report = check(self.vault, POLICY, registry, svg_rules={"assets/legacy.svg": rule})
        output, manifest = export_sample(report, ["notes/A.md"], self.root / "generated")
        receipt = manifest["svg_transformations"][0]
        self.assertNotEqual(receipt["source_sha256"], receipt["output_sha256"])
        self.assertTrue(any(receipt["output_sha256"] in p.name for p in output.rglob("*.svg")))
        (self.vault / "assets" / "legacy.svg").write_bytes(raw + b" ")
        stale = check(self.vault, POLICY, registry, svg_rules={"assets/legacy.svg": rule})
        self.assertTrue(stale.failed)
        self.assertIn("STALE_SVG_PROFILE", [i.code for i in stale.issues])

    def test_svg_duplicate_ids_and_broken_local_references_fail(self):
        for content in (b'<rect id="x"/><circle id="x"/>', b'<use href="#missing"/>',
                        b'<path style="fill:u/**/rl(//evil.test)"/>'):
            with self.assertRaises(ValueError):
                validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg">' + content + b'</svg>')

    def test_html_detects_missing_resources_duplicate_ids_and_missing_links(self):
        self.note("A", "# 标题\n")
        report = self.report()
        _, manifest = export_sample(report, ["notes/A.md"], self.root / "generated")
        route = manifest["routes"][0]
        output = self.root / "public"
        page = output / route["url"].lstrip("/") / "index.html"
        page.parent.mkdir(parents=True)
        aid = route["anchors"][0]
        page.write_text(f'<h1 id="{aid}">标题</h1>', encoding="utf-8")
        self.assertTrue(verify_html(output, manifest)["passed"])
        page.write_text(f'<h1 id="{aid}"></h1><p id="{aid}"></p><img src="missing.png">', encoding="utf-8")
        failures = verify_html(output, manifest)
        self.assertFalse(failures["passed"])
        self.assertEqual(len(failures["errors"]), 2)

    @unittest.skipUnless(local_hugo().is_file(),
                         "Install the checksum-verified pinned local Hugo to run integration")
    def test_real_hugo_closed_fixture(self):
        project = Path(__file__).resolve().parents[1]
        self.note("A", "# A\n[[B#中文标题|中文段落]] [[B#^p]] [[B#^code]] [[B#^q]] [[未发布|不可点击]]\n"
                  "![[图.png]] ![[图.svg]]\n\n`[[literal]]`\n\n```mermaid\ngraph LR; A-->B;\n```\n\n"
                  "$x_1$\n\n$$\nx^2\n$$\n\n| 列 | 列 |\n|---|---|\n| [[B\\|名]] | [[B\\|名]] |\n\n"
                  "脚注[^1]\n\n![[B#中文标题]]\n\n![[B#中文标题]]\n\n![[B#^code]]\n\n[^1]: [[B]]\n")
        self.note("B", "# 中文标题\n段落第一行[^1]\n第二行 ^p\n\n```sh\necho example\n```\n^code\n\n> [!NOTE] Quote\n> body ^q\n\n"
                  "![[../assets/图.png]]\n\n# After\nOutside scoped embed\n\n[^1]: Child footnote [[A]]\n")
        self.note("未发布", "NEVER EXPORT", "publish: false")
        (self.vault / "assets" / "图.png").write_bytes(b"test-image")
        (self.vault / "assets" / "图.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>')
        report = self.report()
        generated, manifest = export_sample(report, ["notes/A.md", "notes/B.md"], self.root / "generated")
        output = self.root / "build"
        result = subprocess.run([str(local_hugo()),
                                 "--source", str(project / "pipeline/hugo-test"),
                                 "--contentDir", str(generated / "content"), "--destination", str(output),
                                 "--cacheDir", str(self.root / "cache"), "--noBuildLock", "--noChmod", "--noTimes"],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        verified = verify_html(output, manifest)
        self.assertTrue(verified["passed"], verified)
        self.assertEqual(verified["outside_sample_not_verified"], [])
        apage = next(r for r in manifest["routes"] if r["id"] == next(n.note_id for n in report.notes if n.name == "A"))
        html = (output / apage["url"].lstrip("/") / "index.html").read_text(encoding="utf-8")
        self.assertIn("language-mermaid", html)
        self.assertIn("<math", html)
        self.assertIn('<annotation encoding="application/x-tex">x_1</annotation>', html)
        self.assertIn("x^2", html)
        self.assertNotIn("NEVER EXPORT", html)
        self.assertNotIn('href="未发布', html)
        self.assertIn("不可点击", html)
        built_hashes = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in output.rglob("*") if p.is_file()}
        repeat = subprocess.run([str(local_hugo()),
                                 "--source", str(project / "pipeline/hugo-test"),
                                 "--contentDir", str(generated / "content"), "--destination", str(self.root / "repeat"),
                                 "--cacheDir", str(self.root / "cache"), "--noBuildLock", "--noChmod", "--noTimes"],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
        self.assertEqual(built_hashes, {p.relative_to(self.root / "repeat").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in (self.root / "repeat").rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
