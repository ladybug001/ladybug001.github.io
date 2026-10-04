"""Crash-safe writes to local publisher state, never to the Vault."""
from __future__ import annotations

from contextlib import contextmanager
import os
import hashlib
import json
import re
from pathlib import Path
import tempfile


def publication_head(state_dir):
    """Read the one authoritative publication pointer; never guess recovery."""
    state_dir = Path(state_dir).absolute()
    path = state_dir / "publication-head.json"
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("Publication state must not follow reparse paths")
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or set(data) != {"schema_version", "kind", "release", "registry_revision", "snapshot_sha256"}
            or data["schema_version"] != 1 or data["kind"] != "publication-head"
            or (data["release"] is not None and (not isinstance(data["release"], str) or not re.fullmatch(r"release-[a-f0-9]{24}", data["release"])))
            or not isinstance(data["registry_revision"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["registry_revision"])
            or (data["release"] is None and data["snapshot_sha256"] is not None)
            or (data["release"] is not None and (not isinstance(data["snapshot_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["snapshot_sha256"])) )):
        raise ValueError("Invalid authoritative publication pointer")
    return data


def registry_bytes(path):
    """Once paired publishing starts, the old current file is only a bootstrap.

    A lost publication pointer with existing releases is never treated as a new
    publication: recovery must restore the pointer before identities can change.
    """
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("Private state must not follow reparse paths")
    if path.name == "identity-registry.json":
        head = publication_head(path.parent)
        if head is not None:
            source = (path.parent / "publication-releases" / head["release"] / "registry.json" if head["release"] is not None
                      else path.parent / "revisions" / (head["registry_revision"] + ".json"))
            for part in (source, *source.parents):
                if part.is_symlink() or part.is_junction():
                    raise ValueError("Publication registry must not follow reparse paths")
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != head["registry_revision"]:
                raise ValueError("Publication registry revision mismatch")
            return raw
        releases = path.parent / "publication-releases"
        if releases.exists() and any(releases.glob("release-*")):
            raise ValueError("Publication pointer missing but releases exist; explicit recovery required")
    return path.read_bytes()


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".publisher-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def writer_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / ".identity.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("Identity state is locked; inspect interrupted/concurrent writer before recovery") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()
