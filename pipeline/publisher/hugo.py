"""Small, fail-closed Hugo adapter. Source Markdown is never written.

This is an isolated sample exporter, NOT the public snapshot/CI hand-off.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

from .checker import _hash, _signature
from .identity import key, route_key
from .model import Note, Report
from .safety import within
from .syntax import _mask_comments, heading_matches, inline_positions, parser
from .transclusion import footnotes, scope
from .svg import transform_svg, validate_svg


ADAPTER_VERSION = "sample-3"


class Context:
    def __init__(self, host: Note, report: Report):
        self.host = host
        self.notes = {n.path: n for n in report.notes}
        self.assets: dict[str, bytes] = {}
        self.graph: list[dict] = []
        self.anchors: list[str] = []
        self.source_assets: set[str] = set()
        self.stack: list[tuple[str, str]] = []
        self.expansions = 0
        self.expanded_characters = 0
        self.svg_receipts: list[dict] = []


def text(value: str) -> str:
    # Entities protect brackets, HTML, emphasis and automatic URL links alike.
    # Goldmark linkify is additionally disabled in the isolated technical harness.
    return "".join(f"&#{ord(c)};" if not c.isalnum() and c not in " \n" else c for c in value)


def anchor(note: Note, heading) -> str:
    preceding = [h for h in note.headings if h.line <= heading.line and h.path == heading.path]
    payload = json.dumps([note.note_id, [key(p) for p in heading.path], len(preceding)], ensure_ascii=False)
    return "h-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def fragment_id(note: Note, fragment: str) -> str:
    if not fragment:
        return ""
    if fragment.startswith("^"):
        return "b-" + fragment[1:]
    matches = heading_matches(note, fragment)
    if len(matches) != 1:
        raise ValueError("Heading did not resolve uniquely")
    return anchor(note, matches[0])


def reference_spans(body: str) -> list[tuple[int, int] | None]:
    lines = body.splitlines(keepends=True)
    starts, size = [], 0
    for line in lines:
        starts.append(size)
        size += len(line)
    spans = []
    cursors: dict[int, int] = {}
    for token in parser().parse(body):
        if token.type != "inline":
            continue
        children = [c for c in token.children or [] if c.type in {"obs_wiki", "link_open", "image"}]
        if not children:
            continue
        positions = inline_positions(token, lines, starts, cursors)
        for child in children:
            origin = child.meta.get("origin")
            if origin is None:
                spans.append(None)  # e.g. an external autolink: leave authored source alone.
                continue
            src, begin, end = origin
            if src != token.content or not 0 <= begin < end <= len(token.content):
                raise ValueError("Nested link source is not supported by this adapter")
            spans.append((positions[begin], positions[end - 1] + 1))
    return spans


def _render(note: Note, report: Report, context: Context, fragment: str = "", namespace: str = "") -> str:
    if not note.published or not note.note_id or not note.permalink:
        raise ValueError("Exporter requires an opted-in note with a locked ID and route")
    if note.features.get("raw_html"):
        raise ValueError("Raw HTML needs a reviewed policy; sample exporter refuses it")
    vertex = (note.path, fragment)
    if vertex in context.stack:
        raise ValueError("Embed cycle detected during expansion")
    if len(context.stack) >= 32 or context.expansions >= 128:
        raise ValueError("Embed depth/count exceeds the sample expansion limit")
    if len(note.body) > 4 * 1024 * 1024:
        raise ValueError("Source Markdown exceeds the sample text limit")
    context.stack.append(vertex)
    context.expansions += 1
    body, unclosed = _mask_comments(note.body, parser())
    if unclosed:
        raise ValueError("Unclosed hidden comment")
    spans = reference_spans(body)
    if len(spans) != len(note.references):
        raise ValueError("AST references no longer match the checked Note Index")
    bounds = scope(note, fragment, body)
    ranges, edits = footnotes(note, body, bounds, namespace or "page-" + note.note_id)
    included = lambda begin, end: any(a <= begin <= end <= b for a, b in ranges)
    environment = {}
    tokens = parser().parse(body, environment)
    notes, assets, graph = context.notes, context.assets, context.graph
    for ref, span in zip(note.references, spans):
        if span is None:
            if ref.status == "external":
                continue
            raise ValueError("Internal reference has no verified source span")
        begin, end = span
        if not included(begin, end):
            continue
        label = ref.label or ref.target.partition("#")[0].rsplit("/", 1)[-1].removesuffix(".md")
        if ref.status == "external":
            # Reference labels from separate notes share one Markdown namespace.
            # Make these links inline before transclusion; do not merge definitions.
            authored = body[begin:end]
            if re.search(r"\]\s*\(", authored):
                continue  # A self-contained inline link can preserve authored formatting/title.
            destination = quote(ref.target, safe=":/?#[]@!$&'*+,;=%-._~")
            replacement = f"{'!' if ref.kind == 'markdown_image' else ''}[{text(label)}]({destination})"
        elif ref.presentation:
            replacement = text(ref.presentation["text"])
        elif ref.status == "resolved_note":
            target = notes[ref.target_path]
            if not target.note_id or not target.permalink:
                raise ValueError("Link target is missing a locked identity/route")
            destination = target.permalink
            target_fragment = fragment_id(target, ref.fragment)
            if target_fragment:
                destination += "#" + target_fragment
            if ref.kind == "wiki_embed":
                line_begin = body.rfind("\n", 0, begin) + 1
                line_end = body.find("\n", end)
                if line_end < 0:
                    line_end = len(body)
                if body[line_begin:begin].strip() or body[end:line_end].strip():
                    raise ValueError("Note embeds must occupy a standalone top-level line; inline/list/quote/table embeds are not silently flattened")
                child_namespace = "e-" + hashlib.sha256(json.dumps([context.host.note_id, namespace, note.note_id, begin, target.note_id, ref.fragment]).encode()).hexdigest()[:20] + "-"
                replacement = "\n\n" + _render(target, report, context, ref.fragment, child_namespace).strip("\n") + "\n\n"
            else:
                replacement = f"[{text(label)}]({quote(destination, safe='/#-._~')})"
            graph.append({"source_id": note.note_id, "target_id": target.note_id,
                          "rendered_on_id": context.host.note_id, "rendered": ref.kind != "wiki_embed",
                          "kind": ref.kind, "url": destination})
        elif ref.status == "resolved_asset":
            source = Path(report.vault) / ref.target_path
            context.source_assets.add(ref.target_path)
            meta = report.assets[ref.target_path]
            if meta["bytes"] > 32 * 1024 * 1024:
                raise ValueError("Sample attachment exceeds the 32 MiB per-file limit")
            if _signature(source) != (meta["mtime_ns"], meta["bytes"]):
                raise ValueError("Attachment changed before copy")
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != meta["sha256"] or _signature(source) != (meta["mtime_ns"], meta["bytes"]):
                raise ValueError("Attachment changed while copying")
            suffix = source.suffix.lower()
            if suffix == ".svg":
                raw, receipt = transform_svg(raw, report.svg_rules.get(ref.target_path))
                if receipt and receipt not in context.svg_receipts:
                    context.svg_receipts.append(receipt)
            filename = "media/" + hashlib.sha256(raw).hexdigest() + suffix
            assets[filename] = raw
            if sum(len(value) for value in assets.values()) > 64 * 1024 * 1024:
                raise ValueError("Sample bundle attachments exceed 64 MiB")
            destination = quote(context.host.permalink + filename, safe="/-._~")
            is_image = ref.kind == "markdown_image" or (ref.kind == "wiki_embed" and suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp"})
            # Obsidian width aliases are presentation-only, not alt text.
            alt = "" if re.fullmatch(r"\d+(?:x\d+)?", ref.label) else label
            replacement = f"{'!' if is_image else ''}[{text(alt)}]({destination})"
        else:
            raise ValueError("Unresolved reference reached exporter")
        edits.append((begin, end, replacement))

    lines = body.splitlines(keepends=True)
    starts, size = [], 0
    for line in lines:
        starts.append(size)
        size += len(line)
    for definition in list(environment.get("references", {}).values()) + environment.get("duplicate_refs", []):
        first, last = definition["map"]
        begin = starts[first]
        end = starts[last] if last < len(starts) else len(body)
        if included(begin, end):
            edits.append((begin, end, ""))
        elif any(begin < b and end > a for a, b in ranges):
            raise ValueError("Markdown reference definition crosses an embed boundary")
    for heading in note.headings:
        first = heading.line - note.body_line
        matching = [t for t in tokens if t.type == "heading_open" and t.map[0] == first]
        if len(matching) != 1:
            raise ValueError("Cannot prove heading source range")
        row = first if matching[0].map[1] == first + 1 else matching[0].map[1] - 2
        line = lines[row].rstrip("\n")
        # Existing IDs/closing ATX hashes must not coexist with generated attributes.
        suffix = re.search(r"(?:\s+\{#[^}]+\})?(?:\s+#+\s*)?$" if row == first else r"(?:\s+\{#[^}]+\})?\s*$", line)
        at = starts[row] + suffix.start()
        if included(starts[row], starts[row] + len(line)):
            identifier = namespace + anchor(note, heading)
            context.anchors.append(identifier)
            edits.append((at, starts[row] + len(line), " {#" + identifier + "}"))
    for block, locations in note.blocks.items():
        begin, end = note.block_ranges[block][0]
        first, last = begin - note.body_line, end - note.body_line
        block_end = starts[last] if last < len(starts) else len(body)
        if not included(starts[first], block_end):
            continue
        row = locations[0] - note.body_line
        marker = re.search(r"\^" + re.escape(block) + r"\s*$", lines[row].rstrip("\n"))
        if not marker:
            raise ValueError("Cannot prove block marker source span")
        if included(starts[row] + marker.start(), starts[row] + marker.end()):
            edits.append((starts[row] + marker.start(), starts[row] + marker.end(), ""))
        identifier = namespace + "b-" + block
        context.anchors.append(identifier)
        matching = [t for t in tokens if t.map == [first, last] and t.type in {"fence", "paragraph_open", "table_open", "bullet_list_open", "ordered_list_open", "blockquote_open"}]
        if not matching:
            raise ValueError("Block anchor requires an unsupported block representation")
        if any(t.type == "fence" for t in matching):
            pos = starts[first] + len(lines[first].rstrip("\n"))
            edits.append((pos, pos, ' {id="' + identifier + '"}'))
        else:
            if lines[first].lstrip().startswith(">") and not any(t.type == "blockquote_open" for t in matching):
                raise ValueError("Nested quote block anchors require a later adapter")
            pos = starts[last] if last < len(starts) else len(body)
            edits.append((pos, pos, ("\n" if pos and body[pos - 1] != "\n" else "") + "{#" + identifier + "}\n"))
    parts = []
    for begin, end in ranges:
        part, previous = body[begin:end], end + 1
        for at, finish, replacement in sorted(edits, reverse=True):
            if not begin <= at <= finish <= end:
                continue
            if finish > previous:
                raise ValueError("Overlapping source edits; conversion aborted")
            part = part[:at - begin] + replacement + part[finish - begin:]
            previous = at
        parts.append(part)
    rendered = "\n\n".join(parts)
    context.expanded_characters += len(rendered)
    if context.expanded_characters > 4 * 1024 * 1024:
        raise ValueError("Expanded Markdown exceeds 4 MiB sample limit")
    context.stack.pop()
    return rendered


def convert(note: Note, report: Report, context: Context | None = None) -> tuple[str, dict[str, bytes], list[dict]]:
    context = context or Context(note, report)
    body = _render(note, report, context)
    metadata = {"title": note.metadata.get("title", note.name), "url": note.permalink,
                "draft": False, "params": {"note_id": note.note_id}}
    for field in ("date", "tags", "categories", "series"):
        if field in note.metadata:
            metadata[field] = note.metadata[field]
    if "updated" in note.metadata:
        metadata["lastmod"] = note.metadata["updated"]
    if "aliases" in note.metadata:
        metadata["params"]["note_aliases"] = note.metadata["aliases"]
    # JSON is Hugo frontmatter; avoids YAML scalar surprises, no Theme fields.
    return json.dumps(metadata, ensure_ascii=False, indent=2) + "\n\n" + body, context.assets, context.graph


def export_sample(report: Report, selected: list[str], output_root: Path) -> tuple[Path, dict]:
    if report.failed or not report.read_verified:
        raise ValueError("Sample export requires a successful, source-consistent check")
    if not selected or len(selected) > 12 or len(set(selected)) != len(selected):
        raise ValueError("Select 1–12 distinct paths explicitly; no automatic dependency expansion")
    vault = Path(report.vault).resolve()
    output_root = output_root.resolve()
    if within(vault, output_root) or within(output_root, vault):
        raise ValueError("Generated output and Vault must be disjoint")
    by_path = {n.path: n for n in report.notes}
    files: dict[str, bytes] = {}
    graph, routes, source_assets, svg_receipts = [], [], set(), []
    for path in sorted(selected):
        if path not in by_path:
            raise ValueError("Sample selection contains an unknown source")
        note = by_path[path]
        context = Context(note, report)
        try:
            markdown, assets, edges = convert(note, report, context)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        route_key(note.permalink)
        prefix = "content/notes/" + note.note_id + "/"
        files[prefix + "index.md"] = markdown.encode("utf-8")
        files.update({prefix + name: raw for name, raw in assets.items()})
        if sum(len(raw) for raw in files.values()) > 128 * 1024 * 1024:
            raise ValueError("Sample output exceeds 128 MiB")
        graph.extend(edges)
        source_assets.update(context.source_assets)
        svg_receipts.extend(context.svg_receipts)
        routes.append({"id": note.note_id, "url": note.permalink,
                       "anchors": context.anchors})
    local_urls = {r["url"] for r in routes}
    boundary = sorted({e["url"] for e in graph if e.get("rendered", True) and e["url"].split("#")[0] not in local_urls})
    manifest = {"schema_version": 1, "adapter": ADAPTER_VERSION, "sample_only": True,
                "deployable": False, "routes": routes, "link_graph": graph,
                "outside_sample_urls": boundary,
                "svg_transformations": svg_receipts,
                "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())}}
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    release = "sample-" + hashlib.sha256(payload).hexdigest()[:24]
    destination = output_root / release
    def verify_sources():
        # Read-consistency barrier, both before and after staging. In particular,
        # copied assets must still match the exact bytes originally checked.
        for note in report.notes:
            if note.content_hash is not None and (_signature(note.file) != note.stat_signature or _hash(note.file) != note.content_hash):
                raise ValueError("Source changed since check; retry")
        for relative in source_assets:
            meta = report.assets[relative]
            source = vault / relative
            if _signature(source) != (meta["mtime_ns"], meta["bytes"]) or _hash(source) != meta["sha256"]:
                raise ValueError("Attachment changed since check; retry")
    verify_sources()
    if destination.exists():
        if (destination / "manifest.json").read_bytes() != payload:
            raise ValueError("Existing immutable sample manifest differs")
        actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
        if actual != set(files) | {"manifest.json"} or any((destination / name).read_bytes() != raw for name, raw in files.items()):
            raise ValueError("Existing immutable sample files changed; do not overwrite")
        return destination, manifest
    output_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pending-sample-", dir=output_root))
    # A failed stage is never considered a finished release. No recursive deletion.
    for name, raw in files.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (stage / "manifest.json").write_bytes(payload)
    verify_sources()
    stage.rename(destination)
    return destination, manifest
