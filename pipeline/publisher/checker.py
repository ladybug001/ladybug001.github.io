from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import tomllib
import uuid
from urllib.parse import unquote, urlsplit

from . import compatibility, frontmatter, syntax
from .identity import Registry, key, route_key
from .model import Note, Reference, Report
from .presentation import unavailable_text
from .safety import within


DEFAULT_NOTE_ROOTS = ["04-知识卡片", "03-MOCs", "02-记录", "07-工具使用"]
ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp",
                    ".pdf", ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".m4a", ".mov"}


def load_policy(path: Path) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Policy requires schema_version:1")
    for name in ("note_roots", "attachment_roots", "exclude"):
        if not isinstance(data.get(name), list) or any(not isinstance(s, str) for s in data[name]):
            raise ValueError(f"Policy {name} must be a string list")
    for name in ("note_roots", "attachment_roots"):
        for root in data[name]:
            if (not root or root.startswith(("/", ".")) or "\\" in root or ":" in root
                    or any(part in {".", ".."} for part in root.split("/"))):
                raise ValueError(f"Unsafe policy root: {root}")
    if data.get("unpublished_target", "error") not in {"error", "plain_text"}:
        raise ValueError("unpublished_target must be error or plain_text")
    return data


def _walk(vault: Path, report: Report):
    def walk_error(exc: OSError):
        report.add("DIRECTORY_READ_ERROR", str(exc), source=str(exc.filename or ""))

    for parent, dirs, files in os.walk(vault, followlinks=False, onerror=walk_error):
        safe_dirs = []
        for name in sorted(dirs):
            candidate = Path(parent) / name
            if name.startswith("."):
                continue
            if candidate.is_symlink() or candidate.is_junction() or not within(vault, candidate):
                report.add("SKIPPED_REPARSE_PATH", "Symlink/junction directory is not scanned",
                           source=candidate.relative_to(vault).as_posix(), severity="warning")
            else:
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in sorted(files):
            if name.startswith("."):
                continue
            candidate = Path(parent) / name
            if candidate.is_symlink() or not within(vault, candidate):
                report.add("SKIPPED_REPARSE_PATH", "Symlink file is not scanned",
                           source=candidate.relative_to(vault).as_posix(), severity="warning")
                continue
            yield candidate


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _hash(path: Path) -> str:
    # Streaming prevents a large attachment from using unbounded memory.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under(relative: str, roots: list[str]) -> bool:
    return any(relative == root or relative.startswith(root + "/") for root in roots)


def _load_notes(vault: Path, policy: dict, report: Report) -> list[Path]:
    assets = []
    for path in _walk(vault, report):
        relative = path.relative_to(vault).as_posix()
        if _under(relative, policy["attachment_roots"]):
            assets.append(path)
        if path.suffix.casefold() != ".md":
            continue
        in_scope = _under(relative, policy["note_roots"]) and not any(
            fnmatch.fnmatchcase(relative, pattern) for pattern in policy["exclude"]
        )
        note = Note(relative, path, in_scope=in_scope)
        report.notes.append(note)
        if not in_scope:
            # Only path/name stubs: do not read diary/private bodies to inspect public notes.
            continue
        try:
            note.stat_signature = _signature(path)
            raw = path.read_bytes()
            note.content_hash = hashlib.sha256(raw).hexdigest()
            if note.stat_signature != _signature(path):
                raise ValueError("Source changed while reading")
            note.metadata, note.body, note.body_line = frontmatter.parse(raw.decode("utf-8-sig"))
            errors = frontmatter.validate(note.metadata)
            if errors:
                note.valid = False
                for error in errors:
                    report.add("FRONTMATTER_SCHEMA", error, source=relative)
            if note.metadata.get("private") is True and note.metadata.get("publish") is True:
                report.add("PRIVATE_PUBLISH_CONFLICT", "private:true overrides publish:true", source=relative)
            note.published = (note.valid and note.metadata.get("publish") is True
                              and note.metadata.get("draft") is not True
                              and note.metadata.get("private") is not True)
        except (OSError, UnicodeError, ValueError) as exc:
            note.valid = False
            report.add("SOURCE_READ_OR_FRONTMATTER", str(exc), source=relative)
    return assets


def _identity(report: Report, registry: Registry):
    ids: dict[str, list[str]] = {}
    slugs: dict[str, list[str]] = {}
    occupied: dict[str, str] = {}
    for binding in registry.bindings:
        for route in ([binding.permalink] if binding.permalink else []) + binding.historical_paths:
            occupied[route_key(route)] = binding.id
    for note in report.notes:
        if not note.in_scope or not note.valid:
            continue
        binding = registry.get(note.path)
        source_id = note.metadata.get("id")
        explicit_id = str(uuid.UUID(source_id)) if source_id else None
        if binding and explicit_id and binding.id != explicit_id:
            report.add("IDENTITY_CONFLICT", "Frontmatter id differs from registry binding", source=note.path)
        if explicit_id and not binding:
            # An explicit immutable ID can carry a move, unlike a basename/hash guess.
            binding = registry.get_id(explicit_id)
        note.note_id = explicit_id or (binding.id if binding else None)
        if note.note_id:
            ids.setdefault(note.note_id, []).append(note.path)
        if not note.published:
            continue
        if binding and binding.status == "withdrawn":
            report.add("REACTIVATION_REQUIRED", "Withdrawn binding requires an explicit re-publication decision",
                       source=note.path)
        if note.note_id is None:
            report.add("UNBOUND_IDENTITY", "No permanent identity; check does not allocate one",
                       severity="warning", source=note.path)
        if "date" not in note.metadata:
            report.add("MISSING_DATE", "No reliable content date; no filesystem timestamp is substituted",
                       severity="warning", source=note.path)
        slug = note.metadata.get("slug") or (binding.slug if binding else None)
        if binding and binding.slug and note.metadata.get("slug") and binding.slug != note.metadata["slug"]:
            report.add("ROUTE_CHANGE_REQUIRED", "slug differs from locked binding; explicit migration required",
                       source=note.path)
        if slug:
            slugs.setdefault(key(slug), []).append(note.path)
        note.permalink = binding.permalink if binding else None
        # A slug without a binding is only a proposed route, not a persistent assignment.
        route = note.permalink or (f"/notes/{slug}/" if slug else None)
        if route:
            route_owner = note.note_id or f"unbound:{note.path}"
            normalized = route_key(route)
            if normalized in occupied and occupied[normalized] != route_owner:
                report.add("URL_CONFLICT", "Route is already occupied (including history/withdrawals)",
                           source=note.path, target=route)
            occupied[normalized] = route_owner
    for code, index in (("DUPLICATE_ID", ids), ("DUPLICATE_SLUG", slugs)):
        for value, paths in sorted(index.items()):
            if len(paths) > 1:
                report.add(code, "Identity/slug is not unique", target=value, candidates=paths)
    source_paths = {key(n.path) for n in report.notes}
    explicit_ids = {n.note_id for n in report.notes if n.in_scope and n.valid}
    for binding in registry.bindings:
        if binding.status == "active" and key(binding.source_path) not in source_paths and binding.id not in explicit_ids:
            report.add("MISSING_IDENTITY_SOURCE", "Bound source is missing; confirm move/withdrawal instead of allocating another ID",
                       source=binding.source_path)


class Resolver:
    def __init__(self, report: Report, vault: Path, assets: list[Path], policy: dict, legacy_targets: dict[str, str] | None = None):
        self.report, self.vault = report, vault
        self.policy = policy
        self.legacy_targets = legacy_targets or {}
        self.paths: dict[str, list[Note]] = {}
        self.names: dict[str, list[Note]] = {}
        self.aliases: dict[str, list[Note]] = {}
        self.assets: dict[str, list[Path]] = {}
        self.asset_paths: dict[str, list[Path]] = {}
        for note in report.notes:
            self.paths.setdefault(key(note.path), []).append(note)
            self.names.setdefault(key(note.name), []).append(note)
            if note.valid:
                for alias in note.metadata.get("aliases", []):
                    self.aliases.setdefault(key(alias), []).append(note)
        for asset in assets:
            relative = asset.relative_to(vault).as_posix()
            self.assets.setdefault(key(asset.name), []).append(asset)
            self.asset_paths.setdefault(key(relative), []).append(asset)

    def issue(self, note: Note, ref: Reference, code: str, message: str, candidates=None):
        ref.status = code.lower()
        self.report.add(code, message, source=note.path, line=ref.line, column=ref.column,
                        target=ref.target, candidates=candidates or [])

    def unavailable(self, note: Note, ref: Reference, target: Note, code: str, message: str):
        # The human authorized softening ordinary unavailable-note links, not embeds
        # or broken/ambiguous targets. Preserve diagnostic cause in the local report.
        ordinary = ref.kind in {"wiki_link", "markdown_link"}
        if not ordinary or self.policy.get("unpublished_target", "error") != "plain_text":
            return self.issue(note, ref, code, message)
        ref.status = code.lower()
        ref.presentation = unavailable_text(ref, private=target.metadata.get("private") is True)
        self.report.add(code, message + "; publish as non-clickable text", severity="warning",
                        source=note.path, line=ref.line, column=ref.column, target=ref.target)

    def resolve(self, note: Note, ref: Reference):
        raw = ref.target
        app_note_link = False
        if not raw:
            return self.issue(note, ref, "INVALID_TARGET", "Empty reference target")
        # Handle wiki fragments separately; ordinary Markdown URL #fragments may be encoded.
        if ref.kind.startswith("markdown"):
            parts = urlsplit(raw)
            if parts.scheme == "app" and parts.netloc == "obsidian.md":
                # Narrow compatibility for URIs actually present in this source corpus.
                # Do not launch an app or accept arbitrary hosts, credentials or queries.
                if parts.query:
                    return self.issue(note, ref, "INVALID_TARGET", "Unsupported Obsidian app URI query")
                app_note_link = True
                path, fragment = unquote(parts.path).lstrip("/"), unquote(parts.fragment)
                if not path or any(segment in {".", ".."} for segment in path.split("/")):
                    return self.issue(note, ref, "INVALID_TARGET", "Empty Obsidian app URI")
                self.report.add("APP_NOTE_LINK_NORMALIZED", "Local app URI resolved through Note Index, never launched",
                                severity="warning", source=note.path, line=ref.line, target=raw)
            elif parts.scheme or parts.netloc:
                if parts.scheme.casefold() not in {"", "http", "https", "mailto", "tel"}:
                    return self.issue(note, ref, "INVALID_TARGET", "Unsupported URL scheme")
                ref.status = "external"
                return
            elif parts.query:
                return self.issue(note, ref, "INVALID_TARGET", "Local query-string links need explicit adapter support")
            else:
                path, fragment = unquote(parts.path), unquote(parts.fragment)
        else:
            path, _, fragment = raw.partition("#")
        if "\\" in path:
            path = path.replace("\\", "/")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path) or "\x00" in path:
            return self.issue(note, ref, "INVALID_TARGET", "Absolute/system paths are not publishable")
        ref.fragment = fragment
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix in {".canvas", ".excalidraw"}:
            return self.issue(note, ref, "UNSUPPORTED_TARGET", "Canvas/Excalidraw requires a separate exporter")
        if path and suffix in ASSET_EXTENSIONS:
            if app_note_link:
                return self.issue(note, ref, "UNSUPPORTED_TARGET", "App URI compatibility supports note targets only")
            return self.resolve_asset(note, ref, path)
        if not path:
            matches = [note]
        elif path.startswith(("./", "../")) or (ref.kind.startswith("markdown") and not path.startswith("/") and not app_note_link):
            normalized = posixpath.normpath(posixpath.join(posixpath.dirname(note.path), path))
            if normalized.startswith("../"):
                return self.issue(note, ref, "INVALID_TARGET", "Reference escapes Vault")
            if not normalized.casefold().endswith(".md"):
                normalized += ".md"
            matches = self.paths.get(key(normalized), [])
        elif "/" in path:
            normalized = posixpath.normpath(path.lstrip("/"))
            if normalized.startswith("../"):
                return self.issue(note, ref, "INVALID_TARGET", "Reference escapes Vault")
            if not normalized.casefold().endswith(".md"):
                normalized += ".md"
            matches = self.paths.get(key(normalized), [])
        else:
            name = path[:-3] if path.casefold().endswith(".md") else path
            # Basename + alias candidate union: never silently select one over a collision.
            matches = list({n.path: n for n in self.names.get(key(name), [])
                            + self.aliases.get(key(name), [])}.values())
            if key(name) in self.legacy_targets:
                target_path = self.legacy_targets[key(name)]
                legacy_matches = self.paths.get(key(target_path), [])
                if matches and {n.path for n in matches} != {n.path for n in legacy_matches}:
                    return self.issue(note, ref, "LEGACY_LINK_CONFLICT", "Old name was reused; review historical mapping")
                if not legacy_matches:
                    return self.issue(note, ref, "BROKEN_LEGACY_TARGET", "Explicit legacy mapping target no longer exists")
                matches = legacy_matches
                self.report.add("LEGACY_NOTE_LINK_RESOLVED", "Reference resolved through explicit, evidence-backed legacy mapping",
                                severity="warning", source=note.path, line=ref.line, target=raw)
        if not matches:
            return self.issue(note, ref, "BROKEN_LINK", "Target note does not exist")
        if len(matches) > 1:
            return self.issue(note, ref, "AMBIGUOUS_TARGET", "Multiple note candidates", [n.path for n in matches])
        target = matches[0]
        ref.target_path, ref.target_id = target.path, target.note_id
        if not target.valid:
            return self.issue(note, ref, "INVALID_TARGET", "Target has invalid Frontmatter")
        if not target.in_scope:
            return self.unavailable(note, ref, target, "OUT_OF_SCOPE_TARGET", "Target is outside publication scope")
        if not target.published:
            return self.unavailable(note, ref, target, "UNPUBLISHED_TARGET", "Target is private, draft or not opted in")
        if fragment.startswith("^"):
            occurrences = target.blocks.get(fragment[1:], [])
            if not occurrences:
                return self.issue(note, ref, "INVALID_BLOCK", "Block ID does not exist")
            if len(occurrences) > 1:
                return self.issue(note, ref, "AMBIGUOUS_BLOCK", "Block ID is duplicated")
        elif fragment:
            headings = syntax.heading_matches(target, fragment)
            # Markdown fragments conventionally refer to rendered IDs, not literal heading text.
            if ref.kind.startswith("markdown") and not headings:
                return self.issue(note, ref, "UNVERIFIED_MARKDOWN_ANCHOR",
                                  "Rendered Markdown anchor requires the future Hugo HTML verification stage")
            if not headings:
                return self.issue(note, ref, "INVALID_HEADING", "Heading does not exist")
            if len(headings) > 1:
                return self.issue(note, ref, "AMBIGUOUS_HEADING", "Heading text is duplicated; qualify its path")
        ref.status = "resolved_note"

    def resolve_asset(self, note: Note, ref: Reference, path: str):
        relative_link = path.startswith(("./", "../")) or ref.kind.startswith("markdown")
        if relative_link:
            normalized = posixpath.normpath(posixpath.join(posixpath.dirname(note.path), path))
            if path.startswith("/"):
                normalized = path.lstrip("/")
            if normalized.startswith("../"):
                return self.issue(note, ref, "INVALID_TARGET", "Attachment escapes Vault")
            matches = self.asset_paths.get(key(normalized), [])
        elif "/" in path:
            normalized = posixpath.normpath(path.lstrip("/"))
            if normalized.startswith("../"):
                return self.issue(note, ref, "INVALID_TARGET", "Attachment escapes Vault")
            matches = self.asset_paths.get(key(normalized), [])
        else:
            matches = self.assets.get(key(path), [])
        if not matches:
            return self.issue(note, ref, "MISSING_ATTACHMENT", "Attachment not found in allowed attachment roots")
        if len(matches) > 1:
            return self.issue(note, ref, "AMBIGUOUS_ATTACHMENT", "Multiple attachment candidates",
                              [p.relative_to(self.vault).as_posix() for p in matches])
        asset = matches[0]
        relative = asset.relative_to(self.vault).as_posix()
        ref.target_path = relative
        ref.status = "resolved_asset"
        if relative not in self.report.assets:
            try:
                signature = _signature(asset)
                checksum = _hash(asset)
                if signature != _signature(asset):
                    raise ValueError("Attachment changed while reading")
                self.report.assets[relative] = {"sha256": checksum, "bytes": signature[1],
                                                "mtime_ns": signature[0]}
                if asset.suffix.casefold() == ".svg":
                    reviewed = relative in self.report.svg_rules
                    self.report.add("SVG_REVIEWED_PROFILE" if reviewed else "SVG_REQUIRES_REVIEW",
                                    "Hash-bound static profile reserved; actual output validated during export" if reviewed
                                    else "SVG requires conservative validation/review during export",
                                    severity="warning", source=note.path, target=relative)
            except (OSError, ValueError) as exc:
                self.issue(note, ref, "ATTACHMENT_READ_ERROR", str(exc))


def _embed_cycles(report: Report):
    notes = {n.path: n for n in report.notes if n.published}
    visiting: list[tuple[str, str]] = []
    done: set[tuple[str, str]] = set()

    def outgoing(vertex: tuple[str, str]):
        path, fragment = vertex
        note = notes[path]
        begin, end = 0, float("inf")
        if fragment.startswith("^"):
            ranges = note.block_ranges.get(fragment[1:], [])
            if not ranges:
                return []
            begin, end = ranges[0]
        elif fragment:
            heading = syntax.heading_matches(note, fragment)[0]
            begin = heading.line
            following = [h.line for h in note.headings if h.line > begin and h.level <= heading.level]
            end = min(following, default=float("inf"))
        return [(r.target_path, r.fragment) for r in note.references
                if r.kind == "wiki_embed" and r.status == "resolved_note" and begin <= r.line < end]

    def visit(vertex: tuple[str, str]):
        if vertex in visiting:
            cycle = visiting[visiting.index(vertex):] + [vertex]
            labels = [path + ("#" + fragment if fragment else "") for path, fragment in cycle]
            report.add("EMBED_CYCLE", "Public embed expansion forms a cycle", source=vertex[0], candidates=labels)
            return
        if vertex in done:
            return
        if len(visiting) >= 64:
            report.add("EMBED_DEPTH", "Embed dependency chain exceeds 64 scopes", source=vertex[0])
            return
        visiting.append(vertex)
        for target in outgoing(vertex):
            visit(target)
        visiting.pop()
        done.add(vertex)

    for path in notes:
        visit((path, ""))


def check(vault: Path, policy: dict, registry: Registry | None = None, repairs: list[dict[str, str]] | None = None,
          legacy_targets: dict[str, str] | None = None, svg_rules: dict[str, dict[str, str]] | None = None) -> Report:
    vault = vault.resolve()
    if not vault.is_dir():
        raise ValueError("Vault directory does not exist")
    report = Report(str(vault))
    report.svg_rules = svg_rules or {}
    for root in policy["note_roots"] + policy["attachment_roots"]:
        path = vault / root
        if not within(vault, path) or path.is_symlink() or path.is_junction():
            raise ValueError(f"Unsafe scan root: {root}")
        if not path.is_dir():
            report.add("MISSING_SOURCE_ROOT", "Configured root does not exist", source=root)
    assets = _load_notes(vault, policy, report)
    for note in report.notes:
        if note.published:
            compatibility.apply(note, repairs or [], report)
    _identity(report, registry or Registry())
    resolver = Resolver(report, vault, assets, policy, legacy_targets)
    for name, notes in sorted(resolver.names.items()):
        if len(notes) > 1:
            report.add("DUPLICATE_NOTE_NAME", "Duplicate basename; qualified links are required when ambiguous",
                       severity="warning", target=name, candidates=[n.path for n in notes])
    for paths in resolver.paths.values():
        if len(paths) > 1:
            report.add("PATH_NORMALIZATION_CONFLICT", "Case/Unicode-equivalent note paths", candidates=[n.path for n in paths])
    for note in report.notes:
        if note.published:
            try:
                syntax.inspect(note, report)
            except Exception as exc:
                report.add("PARSE_ERROR", f"{type(exc).__name__}: {exc}", source=note.path)
    for note in report.notes:
        if note.published:
            for ref in note.references:
                resolver.resolve(note, ref)
    _embed_cycles(report)
    for path, rule in report.svg_rules.items():
        if path in report.assets and report.assets[path]["sha256"] != rule["source_sha256"]:
            report.add("STALE_SVG_PROFILE", "Reviewed SVG source changed; profile requires re-review", source=path)
    before_errors = len(report.issues)
    for note in report.notes:
        if note.content_hash is not None:
            try:
                if _signature(note.file) != note.stat_signature or _hash(note.file) != note.content_hash:
                    report.add("SOURCE_CHANGED", "Source changed during inspection; retry", source=note.path)
            except OSError as exc:
                report.add("SOURCE_CHANGED", str(exc), source=note.path)
    for relative, meta in report.assets.items():
        try:
            asset = vault / relative
            if _signature(asset) != (meta["mtime_ns"], meta["bytes"]) or _hash(asset) != meta["sha256"]:
                report.add("SOURCE_CHANGED", "Attachment changed during inspection; retry", source=relative)
        except OSError as exc:
            report.add("SOURCE_CHANGED", str(exc), source=relative)
    report.read_verified = len(report.issues) == before_errors
    report.issues.sort(key=lambda i: (i.source, i.line or 0, i.column or 0, i.code, i.target))
    return report
