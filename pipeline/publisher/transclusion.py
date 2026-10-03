"""Source-scoped embed and footnote planning, without touching source files."""
from __future__ import annotations

import hashlib
import re

from .model import Note
from .syntax import heading_matches, inline_positions, parser


def line_offsets(body: str) -> tuple[list[str], list[int]]:
    lines = body.splitlines(keepends=True)
    starts, size = [], 0
    for line in lines:
        starts.append(size)
        size += len(line)
    starts.append(size)
    return lines, starts


def scope(note: Note, fragment: str, body: str) -> tuple[int, int]:
    _, starts = line_offsets(body)
    if not fragment:
        return 0, len(body)
    if fragment.startswith("^"):
        found = note.block_ranges.get(fragment[1:], [])
        if len(found) != 1:
            raise ValueError("Embed block is missing or ambiguous")
        begin, end = found[0]
    else:
        found = heading_matches(note, fragment)
        if len(found) != 1:
            raise ValueError("Embed heading is missing or ambiguous")
        heading = found[0]
        begin = heading.line
        end = min((h.line for h in note.headings if h.line > begin and h.level <= heading.level),
                  default=note.body_line + len(starts) - 1)
    return starts[begin - note.body_line], starts[end - note.body_line]


def footnotes(note: Note, body: str, bounds: tuple[int, int], namespace: str) -> tuple[list[tuple[int, int]], list[tuple[int, int, str]]]:
    """Append only necessary definitions and namespace actual AST footnote tokens.

    Definitions outside a heading/block scope are dependencies, not arbitrary
    extra note text. The dependency closure is finite even for mutual citations.
    """
    lines, starts = line_offsets(body)
    definitions, references, cursors = {}, [], {}
    for token in parser().parse(body):
        if token.type == "footnote_reference_open":
            label = token.meta["label"]
            if label.casefold() in {s.casefold() for s in definitions}:
                raise ValueError("Duplicate/case-conflicting footnote definition")
            begin, end = starts[token.map[0]], starts[token.map[1]]
            marker = "[^" + label + "]:"
            at = body.find(marker, begin, starts[token.map[0] + 1])
            if at < 0:
                raise ValueError("Cannot prove footnote definition span")
            definitions[label] = (begin, end, at, at + len(marker) - 1)
        elif token.type == "inline":
            refs = [c for c in token.children or [] if c.type == "footnote_ref"]
            if not refs:
                continue
            positions = inline_positions(token, lines, starts, cursors)
            for child in refs:
                if "label" not in child.meta or "origin" not in child.meta:
                    raise ValueError("Inline ^[footnote] syntax requires a separate adapter")
                src, begin, end = child.meta["origin"]
                if src != token.content:
                    raise ValueError("Cannot prove footnote reference span")
                references.append((positions[begin], positions[end - 1] + 1, child.meta["label"]))
    ranges = [bounds]
    needed = set()
    while True:
        found = {label for begin, end, label in references if any(a <= begin < end <= b for a, b in ranges)} - needed
        if not found:
            break
        for label in sorted(found):
            if label not in definitions:
                raise ValueError("Missing footnote definition in embedded scope")
            begin, end, _, _ = definitions[label]
            if not any(a <= begin < end <= b for a, b in ranges):
                if any(begin < b and end > a for a, b in ranges):
                    raise ValueError("Footnote definition overlaps selected scope boundary")
                ranges.append((begin, end))
        needed.update(found)
    suffix = lambda label: "[^f-" + hashlib.sha256((namespace + "\0" + note.note_id + "\0" + label).encode()).hexdigest()[:24] + "]"
    edits = [(begin, end, suffix(label)) for begin, end, label in references
             if any(a <= begin < end <= b for a, b in ranges)]
    edits.extend((at, finish, suffix(label)) for label, (begin, end, at, finish) in definitions.items()
                 if any(a <= begin < end <= b for a, b in ranges))
    return ranges, edits
