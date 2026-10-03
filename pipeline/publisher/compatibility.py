"""Explicit source-hash-bound in-memory repairs. Never writes the source."""
from __future__ import annotations

import re
import posixpath

from .identity import key
from .model import Note, Report


def validate_overrides(data: dict) -> list[dict[str, str]]:
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("repairs"), list):
        raise ValueError("Overrides require schema_version:1 and repairs:[]")
    repairs = []
    for item in data["repairs"]:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k]
                                             for k in ("source_path", "source_sha256", "expected", "replacement", "reason")):
            raise ValueError("Repair requires source_path, source_sha256, expected, replacement and reason strings")
        if not re.fullmatch(r"[0-9a-f]{64}", item["source_sha256"]):
            raise ValueError("Repair source_sha256 must be a SHA-256 hex digest")
        if item["expected"].count("\n") != item["replacement"].count("\n"):
            raise ValueError("Repairs must preserve line count for diagnostic source positions")
        repairs.append(item)
    return repairs


def legacy_targets(data: dict) -> dict[str, str]:
    entries = data.get("legacy_note_targets", [])
    if not isinstance(entries, list):
        raise ValueError("legacy_note_targets must be a list")
    result = {}
    for item in entries:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k]
                                             for k in ("old_target", "source_path", "reason")):
            raise ValueError("Legacy mapping requires old_target, source_path and reason")
        old, path = item["old_target"], item["source_path"]
        if any(char in old for char in "/\\:#") or any(char in path for char in "\\:"):
            raise ValueError("Legacy names must be basenames and targets must be relative Vault paths")
        if path.startswith("/") or path.startswith("../") or posixpath.normpath(path) != path or not path.endswith(".md"):
            raise ValueError("Invalid legacy target path")
        candidate = key(old.removesuffix(".md"))
        if candidate in result:
            raise ValueError("Duplicate legacy note target")
        result[candidate] = path
    return result


def apply(note: Note, repairs: list[dict[str, str]], report: Report) -> None:
    for repair in repairs:
        if repair["source_path"] != note.path:
            continue
        if repair["source_sha256"] != note.content_hash or note.body.count(repair["expected"]) != 1:
            report.add("STALE_SOURCE_REPAIR", "Source no longer matches approved repair; review instead of guessing",
                       source=note.path)
            continue
        line = note.body_line + note.body[:note.body.index(repair["expected"])].count("\n")
        note.body = note.body.replace(repair["expected"], repair["replacement"], 1)
        report.add("SOURCE_REPAIR_APPLIED", repair["reason"] + "; in memory only", severity="warning",
                   source=note.path, line=line)


def svg_rules(data: dict) -> dict[str, dict[str, str]]:
    entries = data.get("svg_profiles", [])
    if not isinstance(entries, list):
        raise ValueError("svg_profiles must be a list")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k]
                                             for k in ("source_path", "source_sha256", "profile", "reason")):
            raise ValueError("SVG profile requires path, SHA-256, profile and reason")
        path = entry["source_path"]
        if (path.startswith("/") or path.startswith("../") or posixpath.normpath(path) != path
                or "\\" in path or ":" in path or not path.lower().endswith(".svg")):
            raise ValueError("Invalid SVG source path")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["source_sha256"]) or entry["profile"] != "visio-static-v1":
            raise ValueError("Invalid/unsupported SVG profile hash or name")
        if key(path) in {key(p) for p in result}:
            raise ValueError("Duplicate SVG profile path")
        result[path] = entry
    return result
