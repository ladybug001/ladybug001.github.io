#!/usr/bin/env python3
"""Generate deterministic synthetic portable-1 fixtures, never read a Vault."""
from __future__ import annotations
from pathlib import Path
import sys
sys.dont_write_bytecode = True

from publisher.snapshot import payload, separate_anchors, sha, validate

PROJECT = Path(__file__).resolve().parent.parent
A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"


def fixture_files():
    """This source defines test content only; it is not an alternate note library."""
    asset = b'<svg xmlns="http://www.w3.org/2000/svg"><title>Synthetic test asset</title><rect width="10" height="10" fill="#888"/></svg>'
    asset_name = sha(asset) + ".svg"
    bodies = {
        A: '# 构建测试 A {#h-fixture-a}\n\n此内容为合成测试数据，不来自笔记库。\n\n'
           '[第二页](/notes/fixture-b/#h-fixture-b)\n\n'
           '![](/notes/fixture-a/media/' + asset_name + ')\n\n'
           '> [!NOTE]\n> 不决定主题或页面样式。\n\n未公开笔记（不可点击）\n\n'
           '| 项目 | 内容 |\n| --- | --- |\n| 目标 | [第二页](/notes/fixture-b/#h-fixture-b) |\n\n'
           '```mermaid\ngraph LR; A-->B;\n```\n\n`[[Literal]]`\n\n$x_1$\n\n$$\nx^2\n$$\n',
        B: '# 构建测试 B {#h-fixture-b}\n\n[第一页](/notes/fixture-a/#h-fixture-a)\n\n'
           '[代码块](/notes/fixture-b/#b-fixture-code)\n\n'
           '```text {id="b-fixture-code"}\n[[Literal]] is code, not a note link.\n```\n',
    }
    anchors = {A: ["h-fixture-a"], B: ["h-fixture-b", "b-fixture-code"]}
    routes, files = [], {"assets/" + asset_name: asset}
    for identifier in (A, B):
        route = "/notes/fixture-" + ("a" if identifier == A else "b") + "/"
        body, insertions = separate_anchors(bodies[identifier], anchors[identifier])
        page = {"id": identifier, "url": route, "metadata": {"title": "Synthetic fixture " + ("A" if identifier == A else "B")},
                "body": body, "anchor_insertions": insertions, "resources": [asset_name] if identifier == A else []}
        files["pages/" + identifier + ".json"] = payload(page)
        routes.append({"id": identifier, "url": route, "anchors": anchors[identifier]})
    graph = [{"source_id": source, "target_id": target, "rendered_on_id": source, "rendered": True,
              "kind": "markdown_link", "url": url} for source, target, url in
             [(A, B, "/notes/fixture-b/#h-fixture-b"), (B, A, "/notes/fixture-a/#h-fixture-a"),
              (B, B, "/notes/fixture-b/#b-fixture-code")]]
    manifest = {"schema_version": 1, "format": "portable-1", "validation_only": True, "deployable": False,
                "routes": routes, "identities": [{"id": r["id"], "url": r["url"]} for r in routes],
                "link_graph": graph, "files": {n: sha(r) for n, r in sorted(files.items())}}
    files["manifest.json"] = payload(manifest)
    validate(files)
    return files


def generate():
    root = PROJECT / "publication/fixture"
    for parent in (root, *root.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("Fixture output must not follow reparse points")
    files = fixture_files()
    if root.exists():
        actual = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        if actual != files:
            raise ValueError("Existing fixture changed; refusing overwrite")
        return root
    # For initial source-controlled fixture generation only; no real content.
    for name, raw in sorted(files.items()):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return root


if __name__ == "__main__":
    print(generate())
