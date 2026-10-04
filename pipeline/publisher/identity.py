"""Stable identities and routes: pure validation plus explicit local initialization."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import posixpath
import unicodedata
import uuid
from urllib.parse import unquote, urlsplit

from .storage import atomic_write, writer_lock, registry_bytes


def key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def route_key(value: str) -> str:
    parts = urlsplit(value)
    decoded = unquote(parts.path)
    if (parts.scheme or parts.netloc or parts.query or parts.fragment
            or not value.startswith("/") or not value.endswith("/")
            or "\\" in decoded or "//" in decoded
            or any(part in {".", ".."} for part in decoded.split("/"))):
        raise ValueError(f"Invalid site-relative route: {value}")
    return key(decoded)


@dataclass
class Binding:
    id: str
    source_path: str
    slug: str | None = None
    permalink: str | None = None
    historical_paths: list[str] = field(default_factory=list)
    status: str = "active"
    source_sha256: str | None = None


class Registry:
    def __init__(self, bindings: list[Binding] | None = None):
        self.bindings = deepcopy(bindings or [])
        seen_ids: set[str] = set()
        seen_sources: set[str] = set()
        routes: set[str] = set()
        for binding in self.bindings:
            binding.id = str(uuid.UUID(binding.id))
            if binding.id in seen_ids:
                raise ValueError(f"Duplicate registry ID: {binding.id}")
            seen_ids.add(binding.id)
            path = binding.source_path.replace("\\", "/")
            if path.startswith("/") or ":" in path or posixpath.normpath(path) != path or path.startswith("../"):
                raise ValueError(f"Invalid source_path: {path}")
            binding.source_path = path
            if key(path) in seen_sources:
                raise ValueError(f"Duplicate registry source_path: {path}")
            seen_sources.add(key(path))
            if binding.status not in {"active", "withdrawn"}:
                raise ValueError("Registry status must be active or withdrawn")
            if binding.slug is not None:
                from .frontmatter import validate
                if validate({"slug": binding.slug}):
                    raise ValueError(f"Invalid registry slug: {binding.slug}")
            for route in ([binding.permalink] if binding.permalink else []) + binding.historical_paths:
                candidate = route_key(route)
                if candidate in routes:
                    raise ValueError(f"Duplicate registry route (including history/tombstones): {route}")
                routes.add(candidate)

    @classmethod
    def from_dict(cls, data: dict) -> Registry:
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("notes"), list):
            raise ValueError("Registry requires schema_version:1 and notes:[]")
        bindings = []
        for item in data["notes"]:
            if not isinstance(item, dict) or not isinstance(item.get("source_path"), str):
                raise ValueError("Registry entries require source_path and UUID id")
            if not isinstance(item.get("id"), str):
                raise ValueError("Registry id must be a UUID string")
            for name in ("slug", "permalink"):
                if item.get(name) is not None and not isinstance(item[name], str):
                    raise ValueError(f"Registry {name} must be string or null")
            history = item.get("historical_paths", [])
            if not isinstance(history, list) or any(not isinstance(p, str) for p in history):
                raise ValueError("historical_paths must be a string list")
            bindings.append(Binding(item["id"], item["source_path"], item.get("slug"),
                                    item.get("permalink"), history, item.get("status", "active"),
                                    item.get("source_sha256")))
        return cls(bindings)

    def get(self, source_path: str) -> Binding | None:
        return next((b for b in self.bindings if key(b.source_path) == key(source_path)), None)

    def get_id(self, note_id: str) -> Binding | None:
        normalized = str(uuid.UUID(note_id))
        return next((b for b in self.bindings if b.id == normalized), None)

    def to_dict(self) -> dict:
        return {"schema_version": 1, "notes": [asdict(b) for b in sorted(self.bindings, key=lambda b: b.source_path)]}

    def bind_move(self, note_id: str, new_path: str) -> Registry:
        """Explicit caller decision only. No title/hash-based automatic inference."""
        result = deepcopy(self.bindings)
        candidate = next((b for b in result if b.id == str(uuid.UUID(note_id))), None)
        if candidate is None:
            raise ValueError("Cannot move an unknown note ID")
        candidate.source_path = new_path
        return Registry(result)


def load_private(path: Path, vault: Path) -> Registry:
    data = json.loads(registry_bytes(path).decode("utf-8-sig"))
    owner = data.get("vault_root")
    if owner is not None and Path(owner).resolve() != vault.resolve():
        raise ValueError("Private identity registry belongs to a different Vault")
    return Registry.from_dict(data)


def prepare_initial_registry(report) -> Registry:
    """Explicit initialization only, never called by check. No write to note files."""
    if report.failed or not report.read_verified:
        raise ValueError("Identity initialization requires a successful, source-consistent check")
    bindings = []
    for note in report.notes:
        if not note.published:
            continue
        identifier = note.note_id or str(uuid.uuid4())
        slug = note.metadata.get("slug") or "n-" + uuid.UUID(identifier).hex
        bindings.append(Binding(identifier, note.path, slug, f"/notes/{slug}/", source_sha256=note.content_hash))
    if not bindings:
        raise ValueError("No eligible notes to initialize")
    return Registry(bindings)


def initialize_private(report, state_dir: Path) -> tuple[Registry, bool]:
    """Store a local revision before updating current; repeat init never reallocates.

    Caller enforces the allowed project state directory. Unreferenced revisions from
    interrupted writes are harmless; a lock is never automatically broken.
    """
    if report.failed or not report.read_verified:
        raise ValueError("Identity initialization requires a successful, source-consistent check")
    vault = Path(report.vault)
    from .safety import within
    if within(vault, state_dir):
        raise ValueError("Identity state must not be stored inside Vault")
    current = state_dir / "identity-registry.json"
    with writer_lock(state_dir):
        if current.exists():
            return load_private(current, vault), False
        if any((state_dir / "revisions").glob("*.json")):
            raise ValueError("Current registry is missing but revisions exist; recover state instead of allocating new IDs")
        registry = prepare_initial_registry(report)
        data = {**registry.to_dict(), "vault_root": str(vault.resolve()), "purpose": "reserved, not yet deployed URLs"}
        serialized = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        revision = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        atomic_write(state_dir / "revisions" / f"{revision}.json", serialized)
        atomic_write(current, serialized)
        return registry, True
