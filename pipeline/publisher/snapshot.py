"""Portable, strict, local validation snapshots. No Vault/state access or deployment.

Markdown and anchor IR are distinct from the Hugo materialization. Input hashes
are integrity/reproducibility checks, not a signature or a privacy approval.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from urllib.parse import quote, unquote, urljoin, urlsplit

from .frontmatter import validate as validate_metadata
from .htmlcheck import verify_html
from .identity import route_key
from .safety import within
from .svg import validate_svg
from .syntax import _mask_comments, inline_positions, parser, safe_line_break


VERSION = "portable-1"
MAX_TOTAL = 512 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
ASSET = r"[a-f0-9]{64}\.[a-z0-9]{1,12}"
EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "bmp", "pdf",
              "mp4", "webm", "mp3", "wav", "ogg", "m4a", "mov"}
ANCHOR = r"[A-Za-z0-9_-]{1,160}"
FIELDS = {"title", "date", "updated", "tags", "categories", "series", "aliases"}


class SnapshotError(ValueError):
    def __init__(self, errors):
        self.errors = sorted(set(errors))
        super().__init__("Snapshot rejected:\n" + "\n".join(self.errors))


def payload(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        raise ValueError("Unexpected or missing snapshot fields")


def _id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Expected canonical UUID")
    return value


def _route(value):
    # Fixed identity URLs currently occupy /notes/<single slug>/. Reject encoded
    # delimiters, Windows devices, colons, controls and double-encoded traversal.
    if not isinstance(value, str) or not re.fullmatch(r"/notes/[\w-]+/", value):
        raise ValueError("Expected a canonical /notes/<slug>/ route")
    segment = value.split("/")[2]
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", segment):
        raise ValueError("Reserved route segment")
    return route_key(value)


def _io_path(path):
    """Use Windows extended paths for I/O, without changing public inventories."""
    path = Path(path).absolute()
    value = str(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
        return Path(value)
    return path


def _tree(root):
    """Read a bounded regular-file tree; never follow symlinks or junctions."""
    root = _io_path(root)
    for path in (root, *root.parents):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Snapshot trees must not use symlinks/junctions")
    if not root.is_dir():
        raise ValueError("Snapshot input directory missing")
    result, total = {}, 0
    def visit(directory, depth=0):
        nonlocal total
        if depth > 8:
            raise ValueError("Snapshot directory nesting exceeds limit")
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Snapshot tree contains a symlink/junction")
            if not within(root, path):
                raise ValueError("Snapshot file escapes its root")
            if path.is_dir():
                visit(path, depth + 1)
            elif path.is_file():
                stat = path.stat()
                if stat.st_size > MAX_FILE:
                    raise ValueError("Snapshot file exceeds 32 MiB")
                raw = path.read_bytes()
                after = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(raw) != stat.st_size:
                    raise ValueError("Snapshot input changed during read")
                total += len(raw)
                if total > MAX_TOTAL or len(result) >= 16384:
                    raise ValueError("Snapshot tree exceeds size/file budget")
                result[path.relative_to(root).as_posix()] = raw
            else:
                raise ValueError("Snapshot requires regular files")
    visit(root)
    return result


def _verify_files(files, manifest):
    expected = manifest["files"]
    if not isinstance(expected, dict) or set(files) != set(expected) | {"manifest.json"}:
        raise ValueError("Snapshot file inventory mismatch")
    for name, digest in expected.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or sha(files[name]) != digest:
            raise ValueError("Snapshot hash mismatch: " + name)


def _positions(body):
    lines, starts, size = body.splitlines(keepends=True), [], 0
    for line in lines:
        starts.append(size)
        size += len(line)
    return lines, starts


def _protected(body, tokens):
    lines, starts = _positions(body)
    spans, cursors = [], {}
    for token in tokens:
        if token.map and token.type in {"fence", "code_block", "math_block"}:
            a, b = token.map
            spans.append((starts[a], starts[b] if b < len(starts) else len(body)))
        elif token.type == "inline":
            children = [c for c in token.children or [] if c.type in {"code_inline", "math_inline", "math_inline_double"}]
            if not children:
                continue
            positions = inline_positions(token, lines, starts, cursors)
            for child in children:
                src, a, b = child.meta["origin"]
                if src != token.content:
                    raise ValueError("Cannot prove literal source span")
                spans.append((positions[a], positions[b - 1] + 1))
    return spans


def _annotation(item):
    return {"heading": " {#" + item["id"] + "}",
            "fence": ' {id="' + item["id"] + '"}',
            "block": "{#" + item["id"] + "}\n",
            "quote-block": item.get("prefix", "") + "{#" + item["id"] + "}\n"}[item["kind"]]


def restore(body, insertions):
    if not isinstance(insertions, list):
        raise ValueError("Anchor insertions must be a list")
    previous, ids = -1, set()
    for item in insertions:
        _keys(item, {"offset", "kind", "id"}, {"prefix"} if isinstance(item, dict) and item.get("kind") == "quote-block" else set())
        at = item["offset"]
        if type(at) is not int or not previous < at <= len(body) or at < 0:
            raise ValueError("Invalid/non-increasing anchor offset")
        if item["kind"] not in {"heading", "fence", "block", "quote-block"} or not re.fullmatch(ANCHOR, item["id"]):
            raise ValueError("Invalid anchor kind/ID")
        if item["kind"] == "quote-block" and (not isinstance(item.get("prefix"), str) or not re.fullmatch(r"(?: {0,3}> ?){1,8}", item["prefix"])):
            raise ValueError("Invalid quoted-block anchor prefix")
        if item["id"] in ids:
            raise ValueError("Duplicate anchor ID")
        previous = at
        ids.add(item["id"])
    result = body
    for item in reversed(insertions):
        at = item["offset"]
        result = result[:at] + _annotation(item) + result[at:]
    return result


def separate_anchors(body, anchors):
    """Strip only AST-proven generated attributes, not examples in code/math."""
    if not isinstance(anchors, list) or any(not isinstance(a, str) or not re.fullmatch(ANCHOR, a) for a in anchors) or len(set(anchors)) != len(anchors):
        raise ValueError("Invalid or duplicate declared anchors")
    expected, edits = set(anchors), []
    tokens = parser().parse(body)
    lines, starts = _positions(body)
    protected = _protected(body, tokens)
    for token in tokens:
        if token.type == "heading_open":
            first, last = token.map
            row = first if last == first + 1 else last - 2
            match = re.search(r" \{#(" + ANCHOR + r")\}$", lines[row].rstrip("\n"))
            if match and match[1] in expected:
                edits.append((starts[row] + match.start(), starts[row] + match.end(), "heading", match[1], None))
        elif token.type == "fence":
            row = token.map[0]
            match = re.search(r' \{id="(' + ANCHOR + r')"\}$', lines[row].rstrip("\n"))
            if match and match[1] in expected:
                edits.append((starts[row] + match.start(), starts[row] + match.end(), "fence", match[1], None))
    for row, line in enumerate(lines):
        match = re.fullmatch(r"\{#(" + ANCHOR + r")\}\n", line)
        if match and match[1] in expected and not any(a <= starts[row] < b for a, b in protected):
            edits.append((starts[row], starts[row] + len(line), "block", match[1], None))
        quoted = re.fullmatch(r"((?: {0,3}> ?){1,8})\{#(" + ANCHOR + r")\}\n", line)
        if quoted and quoted[2] in expected and not any(a <= starts[row] < b for a, b in protected):
            # Prove the generated attribute line is parsed inside a quote, not
            # a code example; HTML verification also proves the resulting ID.
            if not any(t.type == "blockquote_open" and t.map[0] <= row < t.map[1] for t in tokens):
                raise ValueError("Quoted anchor outside a parsed quote")
            edits.append((starts[row], starts[row] + len(line), "quote-block", quoted[2], quoted[1]))
    if Counter(e[3] for e in edits) != Counter(anchors):
        raise ValueError("Cannot prove every declared anchor exactly once")
    pieces, insertions, end, removed = [], [], 0, 0
    for a, b, kind, identifier, prefix in sorted(edits):
        if a < end:
            raise ValueError("Overlapping anchor edits")
        pieces.append(body[end:a])
        insertions.append({"offset": a - removed, "kind": kind, "id": identifier})
        if prefix is not None:
            insertions[-1]["prefix"] = prefix
        removed += b - a
        end = b
    pieces.append(body[end:])
    neutral = "".join(pieces)
    if restore(neutral, insertions) != body:
        raise ValueError("Anchor IR round trip failed")
    return neutral, insertions


def _page_from_hugo(raw, route, resources):
    text = raw.decode("utf-8")
    header, end = json.JSONDecoder(object_pairs_hook=_pairs).raw_decode(text)
    if text[end:end + 2] != "\n\n":
        raise ValueError("Unexpected sample frontmatter boundary")
    _keys(header, {"title", "url", "draft", "params"}, {"date", "lastmod", "tags", "categories", "series"})
    _keys(header["params"], {"note_id"}, {"note_aliases"})
    if header["url"] != route["url"] or header["params"]["note_id"] != route["id"] or header["draft"] is not False:
        raise ValueError("Sample route/frontmatter identity mismatch")
    metadata = {name: header[name] for name in FIELDS & header.keys()}
    if "lastmod" in header:
        metadata["updated"] = header["lastmod"]
    if "note_aliases" in header["params"]:
        metadata["aliases"] = header["params"]["note_aliases"]
    body, anchors = separate_anchors(text[end + 2:], route["anchors"])
    return {"id": route["id"], "url": route["url"], "metadata": metadata,
            "body": body, "anchor_insertions": anchors, "resources": sorted(resources)}


def _urls(body):
    md = parser()
    # Inspect even destinations rejected by MarkdownIt's default safe-link rule;
    # otherwise unsafe authored URLs could disappear from the validation AST.
    md.validateLink = lambda value: True
    result, tokens = [], md.parse(body)
    for token in tokens:
        if token.type.startswith("html_"):
            raise ValueError("Raw HTML is not admitted in snapshots")
        for child in token.children or []:
            if child.type in {"obs_wiki", "obs_invalid"} or (child.type == "html_inline" and not safe_line_break(child)):
                raise ValueError("Unconverted Wiki/HTML syntax in snapshot")
            if child.type in {"link_open", "image"}:
                result.append(child.attrGet("href") if child.type == "link_open" else child.attrGet("src"))
    masked, unclosed = _mask_comments(body, parser())
    if unclosed or masked != body:
        raise ValueError("Hidden comments remain in snapshot")
    protected = _protected(body, tokens)
    # Hugo recognizes shortcodes independently of Markdown code fences.
    # Do not accept executable shortcode delimiters even in a displayed example.
    if re.search(r"\{\{[<%]", body):
        raise ValueError("Hugo shortcode delimiters require a reviewed literal adapter")
    for match in re.finditer(r"\{#[^}\n]+\}|\{id=", body):
        if not any(a <= match.start() < b for a, b in protected):
            raise ValueError("Framework-specific syntax remains outside literals")
    return result


def _internal(raw, base):
    if not isinstance(raw, str) or any(ord(c) < 32 for c in raw) or "\\" in raw:
        raise ValueError("Unsafe URL")
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        if parts.scheme not in {"http", "https", "mailto", "tel", ""}:
            raise ValueError("Unsafe URL scheme")
        return None
    if parts.query:
        raise ValueError("Local query URLs are not supported")
    decoded = unquote(parts.path)
    if "%" in decoded or "\\" in decoded or any(ord(c) < 32 for c in decoded) or any(p in {".", ".."} for p in decoded.split("/")):
        raise ValueError("Noncanonical local URL")
    absolute = urlsplit(urljoin(base, decoded))
    fragment = unquote(parts.fragment)
    if fragment and not re.fullmatch(ANCHOR, fragment):
        raise ValueError("Invalid fragment")
    return absolute.path, fragment


def validate(files):
    if not isinstance(files, dict) or len(files) > 16384 or any(not isinstance(raw, bytes) or len(raw) > MAX_FILE for raw in files.values()) or sum(map(len, files.values())) > MAX_TOTAL:
        raise ValueError("Snapshot exceeds size/file budget or contains invalid bytes")
    manifest = _json(files["manifest.json"])
    complete = manifest.get("format") == "portable-2"
    extra = {"scope", "reservations", "redirects"} if complete else set()
    _keys(manifest, {"schema_version", "format", "validation_only", "deployable", "routes", "identities", "link_graph", "files"} | extra)
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != (2 if complete else 1) or manifest["format"] != ("portable-2" if complete else VERSION) or manifest["validation_only"] is not True or manifest["deployable"] is not False:
        raise ValueError("Expected non-deployable portable validation snapshot v1/v2")
    _verify_files(files, manifest)
    pages, routes, assets, identities, route_keys = {}, {}, {}, {}, set()
    if not isinstance(manifest["routes"], list) or not (0 if complete else 1) <= len(manifest["routes"]) <= (2048 if complete else 12):
        raise ValueError("Snapshot page count exceeds the selected profile (v1: 1–12; v2: 0–2048)")
    for route in manifest["routes"]:
        _keys(route, {"id", "url", "anchors"})
        identifier, canonical = _id(route["id"]), _route(route["url"])
        if identifier in pages or canonical in route_keys:
            raise ValueError("Duplicate page ID/URL")
        route_keys.add(canonical)
        page = _json(files["pages/" + identifier + ".json"])
        _keys(page, {"id", "url", "metadata", "body", "anchor_insertions", "resources"})
        if page["id"] != identifier or page["url"] != route["url"]:
            raise ValueError("Page identity mismatch")
        _keys(page["metadata"], {"title"}, FIELDS - {"title"})
        errors = validate_metadata(page["metadata"])
        if errors:
            raise ValueError("Invalid page metadata: " + "; ".join(errors))
        if not isinstance(page["body"], str) or len(page["body"].encode()) > 4 * 1024 * 1024 or "\r" in page["body"]:
            raise ValueError("Invalid or oversized normalized Markdown")
        restored = restore(page["body"], page["anchor_insertions"])
        neutral, checked = separate_anchors(restored, route["anchors"])
        if neutral != page["body"] or checked != page["anchor_insertions"] or route["anchors"] != [a["id"] for a in checked]:
            raise ValueError("Anchor IR does not correspond to Markdown structure: " + identifier)
        resources = page["resources"]
        if not isinstance(resources, list) or any(not isinstance(r, str) or not re.fullmatch(ASSET, r) or r.rsplit(".", 1)[1] not in EXTENSIONS for r in resources) or resources != sorted(set(resources)):
            raise ValueError("Invalid resource inventory")
        for name in resources:
            raw = files["assets/" + name]
            if sha(raw) != name.split(".")[0]:
                raise ValueError("Resource name/hash mismatch")
            if name.endswith(".svg"):
                validate_svg(raw)
            assets["assets/" + name] = raw
        pages[identifier] = page
        routes[route["url"]] = page
    expected = {"manifest.json"} | {"pages/" + i + ".json" for i in pages} | assets.keys()
    if set(files) != expected:
        raise ValueError("Unreferenced/unknown snapshot file")
    if not isinstance(manifest["identities"], list):
        raise ValueError("Invalid public identity index")
    identity_routes = set()
    for entry in manifest["identities"]:
        _keys(entry, {"id", "url"})
        identifier, canonical = _id(entry["id"]), _route(entry["url"])
        if identifier in identities or canonical in identity_routes:
            raise ValueError("Duplicate identity ID/URL")
        identity_routes.add(canonical)
        identities[identifier] = entry["url"]
    for identifier, page in pages.items():
        if identities.get(identifier) != page["url"]:
            raise ValueError("Page missing from public identity index")
    failures, actual, used_assets = [], {}, set()
    for identifier, page in pages.items():
        actual[identifier] = set()
        for raw in _urls(page["body"]):
            local = _internal(raw, page["url"])
            if local is None:
                continue
            path, fragment = local
            actual[identifier].add(path + ("#" + fragment if fragment else ""))
            if path in routes:
                target = routes[path]
                if fragment and fragment not in {a["id"] for a in target["anchor_insertions"]}:
                    failures.append("MISSING_SNAPSHOT_ANCHOR: " + path + "#" + fragment)
            else:
                match = re.fullmatch(re.escape(page["url"]) + r"media/(" + ASSET + r")", path)
                if not match or match[1] not in page["resources"] or fragment:
                    failures.append("MISSING_SNAPSHOT_TARGET: " + path + ("#" + fragment if fragment else ""))
                else:
                    used_assets.add((identifier, match[1]))
        if {r for i, r in used_assets if i == identifier} != set(page["resources"]):
            failures.append("UNUSED_PAGE_RESOURCE: " + page["url"])
    used_ids, graph_urls = set(pages), {i: set() for i in pages}
    if not isinstance(manifest["link_graph"], list):
        raise ValueError("Invalid public link graph")
    for edge in manifest["link_graph"]:
        _keys(edge, {"source_id", "target_id", "rendered_on_id", "rendered", "kind", "url"})
        source, target, host = edge["source_id"], edge["target_id"], edge["rendered_on_id"]
        if source not in identities or target not in identities or host not in pages or type(edge["rendered"]) is not bool:
            raise ValueError("Graph contains unindexed identities")
        if edge["kind"] not in {"wiki_link", "wiki_embed", "markdown_link"} or edge["rendered"] != (edge["kind"] != "wiki_embed"):
            raise ValueError("Invalid graph relation")
        local = _internal(edge["url"], pages[host]["url"])
        if local is None or local[0] != identities[target]:
            raise ValueError("Graph target route mismatch")
        used_ids.update((source, target))
        if edge["rendered"]:
            graph_urls[host].add(edge["url"])
    if used_ids != set(identities):
        raise ValueError("Public identity index contains unrelated entries")
    for host in pages:
        page_urls = {v for v in actual[host] if v.split("#")[0] in routes or "/media/" not in v}
        if graph_urls[host] != page_urls:
            failures.append("GRAPH_MARKDOWN_MISMATCH: " + pages[host]["url"])
    if failures:
        raise SnapshotError(failures)
    if complete:
        validate_route_history(manifest, pages)
    return manifest, pages


def validate_route_history(manifest, pages):
    if manifest["scope"] != "complete-opt-in" or not isinstance(manifest["reservations"], list) or not isinstance(manifest["redirects"], list):
        raise ValueError("Invalid complete snapshot route history")
    occupied, indexed, expected = {}, set(), []
    for entry in manifest["reservations"]:
        _keys(entry, {"id", "url", "history", "status"})
        identifier = _id(entry["id"])
        if identifier in indexed or entry["status"] != ("active" if identifier in pages else "withdrawn"):
            raise ValueError("Reservation identity/status conflict")
        indexed.add(identifier)
        if not isinstance(entry["history"], list) or len(entry["history"]) > 100:
            raise ValueError("Invalid reservation history")
        for path in [entry["url"], *entry["history"]]:
            canonical = _route(path)
            if canonical in occupied:
                raise ValueError("Reserved route collision, including withdrawn history")
            occupied[canonical] = identifier
        if identifier in pages:
            if entry["url"] != pages[identifier]["url"]:
                raise ValueError("Reservation/current page mismatch")
            expected.extend({"id": identifier, "from": old, "to": entry["url"]} for old in entry["history"])
    if not set(pages) <= indexed:
        raise ValueError("Public pages missing route reservations")
    if manifest["redirects"] != sorted(expected, key=lambda r: r["from"]):
        raise ValueError("Redirects must be exactly the active history; withdrawn routes never redirect")


def redirect_html(destination):
    _route(destination)
    url = quote(destination, safe="/-._~")
    # Generated technical redirect, not authored HTML or Theme layout. Keep
    # fragments; route validation excludes all script/attribute delimiters.
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="robots" content="noindex"><meta http-equiv="refresh" content="0;url=' + html.escape(url, quote=True) + '">'
            '<link rel="canonical" href="' + html.escape(url, quote=True) + '"></head><body>'
            '<a href="' + html.escape(url, quote=True) + '">Continue</a><script>location.replace('
            + json.dumps(url) + '+location.hash);</script></body></html>\n').encode("utf-8")


def inspect_snapshot(root):
    files = _tree(root)
    manifest, pages = validate(files)
    return files, manifest, pages


def _install(files, output_root, prefix):
    if len(files) > 16384 or sum(len(raw) for raw in files.values()) > MAX_TOTAL or any(len(raw) > MAX_FILE for raw in files.values()):
        raise ValueError("Generated release exceeds snapshot size/file budget")
    root = _io_path(output_root)
    for path in (root, *root.parents):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Output must not use symlinks/junctions")
    destination = root / (prefix + sha(files["manifest.json"])[:24])
    if destination.exists():
        if _tree(destination) != files:
            raise ValueError("Immutable generated release changed; refusing overwrite")
        return Path(output_root).absolute() / destination.name
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pending-" + prefix, dir=root))
    for name, raw in sorted(files.items()):
        target = stage / name
        if not within(stage, target):
            raise ValueError("Unsafe generated path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    if _tree(stage) != files:
        raise ValueError("Generated staging validation failed")
    stage.rename(destination)
    return Path(output_root).absolute() / destination.name


def pack_snapshot(sample, output_root, selected=None):
    if within(Path(sample), Path(output_root)) or within(Path(output_root), Path(sample)):
        raise ValueError("Sample input and snapshot output must be disjoint")
    source = _tree(sample)
    manifest = _json(source["manifest.json"])
    _keys(manifest, {"schema_version", "adapter", "sample_only", "deployable", "routes", "link_graph", "outside_sample_urls", "svg_transformations", "files"})
    if manifest["schema_version"] != 1 or manifest["adapter"] != "sample-3" or manifest["sample_only"] is not True or manifest["deployable"] is not False:
        raise ValueError("Expected sample-3 input")
    _verify_files(source, manifest)
    by_id = {r["id"]: r for r in manifest["routes"]}
    if len(by_id) != len(manifest["routes"]):
        raise ValueError("Duplicate sample page ID")
    selected = list(by_id) if selected is None else selected
    if not 1 <= len(selected) <= 12 or len(set(selected)) != len(selected) or not set(selected) <= by_id.keys():
        raise ValueError("Select 1–12 distinct existing public sample IDs")
    graph = [e for e in manifest["link_graph"] if e.get("rendered_on_id", e["source_id"]) in selected]
    identities = {i: by_id[i]["url"] for i in selected}
    for edge in graph:
        target_url = edge["url"].split("#")[0]
        if edge["target_id"] in identities and identities[edge["target_id"]] != target_url:
            raise ValueError("Conflicting public identity routes")
        identities[edge["target_id"]] = target_url
    files, routes = {}, []
    for identifier in sorted(selected):
        _id(identifier)
        route = by_id[identifier]
        _route(route["url"])
        prefix = "content/notes/" + identifier + "/"
        resources = []
        for name in manifest["files"]:
            if name.startswith(prefix + "media/"):
                asset = name[len(prefix + "media/"):]
                if not re.fullmatch(ASSET, asset):
                    raise ValueError("Invalid sample asset path")
                resources.append(asset)
                files["assets/" + asset] = source[name]
        page = _page_from_hugo(source[prefix + "index.md"], route, resources)
        files["pages/" + identifier + ".json"] = payload(page)
        routes.append({"id": identifier, "url": route["url"], "anchors": [a["id"] for a in page["anchor_insertions"]]})
    snapshot = {"schema_version": 1, "format": VERSION, "validation_only": True, "deployable": False,
                "routes": routes, "identities": [{"id": i, "url": u} for i, u in sorted(identities.items())],
                "link_graph": graph, "files": {n: sha(r) for n, r in sorted(files.items())}}
    files["manifest.json"] = payload(snapshot)
    validate(files)  # No outside-sample exemptions; fails before any promotion.
    if _tree(sample) != source:
        raise ValueError("Sample changed during packing")
    return _install(files, output_root, "snapshot-"), snapshot


def materialize_snapshot(snapshot, output_root):
    if within(Path(snapshot), Path(output_root)) or within(Path(output_root), Path(snapshot)):
        raise ValueError("Snapshot input and Hugo output must be disjoint")
    source, manifest, pages = inspect_snapshot(snapshot)
    files = {}
    for identifier, page in pages.items():
        metadata = page["metadata"]
        header = {"title": metadata["title"], "url": page["url"], "draft": False, "params": {"note_id": identifier}}
        for field in ("date", "tags", "categories", "series"):
            if field in metadata:
                header[field] = metadata[field]
        if "updated" in metadata:
            header["lastmod"] = metadata["updated"]
        if "aliases" in metadata:
            header["params"]["note_aliases"] = metadata["aliases"]
        prefix = "content/notes/" + identifier + "/"
        files[prefix + "index.md"] = (json.dumps(header, ensure_ascii=False, indent=2) + "\n\n" + restore(page["body"], page["anchor_insertions"])).encode("utf-8")
        for resource in page["resources"]:
            files[prefix + "media/" + resource] = source["assets/" + resource]
    for redirect in manifest.get("redirects", []):
        files["static/" + redirect["from"].lstrip("/") + "index.html"] = redirect_html(redirect["to"])
    # Same validator contract, but its boundary is empty and strict=True.
    harness = {"schema_version": 1, "sample_only": True, "deployable": False,
               "snapshot_sha256": sha(source["manifest.json"]), "routes": manifest["routes"],
               "link_graph": manifest["link_graph"], "outside_sample_urls": [],
               "files": {n: sha(r) for n, r in sorted(files.items())}}
    files["manifest.json"] = payload(harness)
    if _tree(snapshot) != source:
        raise ValueError("Snapshot changed during materialization")
    return _install(files, output_root, "hugo-"), harness


def verify_snapshot_html(snapshot, materialized, public):
    source, manifest, pages = inspect_snapshot(snapshot)
    generated = _tree(materialized)
    harness = _json(generated["manifest.json"])
    _verify_files(generated, harness)
    # Recompute rather than trusting hand-edited adapter metadata or exemptions.
    with tempfile.TemporaryDirectory(prefix="snapshot-verify-") as temporary:
        expected, expected_manifest = materialize_snapshot(snapshot, Path(temporary) / "hugo")
        if generated != _tree(expected):
            raise ValueError("Hugo input does not match the strict snapshot")
    built = _tree(public)
    expected_files = set()
    for page in pages.values():
        prefix = page["url"].lstrip("/")
        expected_files.add(prefix + "index.html")
        expected_files.update(prefix + "media/" + name for name in page["resources"])
    for redirect in manifest.get("redirects", []):
        path = redirect["from"].lstrip("/") + "index.html"
        expected_files.add(path)
        if built.get(path) != redirect_html(redirect["to"]):
            raise ValueError("Historical redirect HTML changed or target mismatched")
    if set(built) != expected_files:
        raise ValueError("Strict build contains missing/stale/unexpected files")
    result = verify_html(Path(public), expected_manifest, strict=True)
    if _tree(public) != built:
        raise ValueError("HTML output changed during validation")
    if _tree(snapshot) != source:
        raise ValueError("Snapshot changed during HTML validation")
    return result
