"""Map Obsidian callout headers to portable Markdown alerts; no source writes."""
from __future__ import annotations

import re

from .syntax import parser


KINDS = {
    'note': 'note', 'info': 'note', 'todo': 'note',
    'abstract': 'note', 'summary': 'note', 'tldr': 'note',
    'tip': 'tip', 'hint': 'tip', 'success': 'tip', 'check': 'tip', 'done': 'tip',
    'important': 'important', 'question': 'note', 'help': 'note', 'faq': 'note',
    'warning': 'warning', 'caution': 'caution', 'attention': 'warning',
    'failure': 'caution', 'fail': 'caution', 'missing': 'caution',
    'danger': 'caution', 'error': 'caution', 'bug': 'caution',
    'example': 'note', 'quote': 'note', 'cite': 'note',
}
STANDARD = {'note', 'tip', 'important', 'warning', 'caution'}
HEADER = re.compile(r'^\[!([A-Za-z][\w-]*)\]([+-]?)(?:[ \t]+(.*))?$')


def normalize_callouts(body: str) -> str:
    """Only edit the first paragraph of a real blockquote, never fenced examples.

    Keep links, body, nested quote prefixes and generated block IDs untouched.
    Fold markers become expanded alerts; unsupported/custom types use Note style
    while retaining the original type as the default title.
    """
    tokens = parser().parse(body)
    lines = body.splitlines(keepends=True)
    for index, token in enumerate(tokens):
        if token.type != 'blockquote_open' or not token.map:
            continue
        following = tokens[index + 1:index + 3]
        if len(following) != 2 or [t.type for t in following] != ['paragraph_open', 'inline']:
            continue
        inline = following[1]
        header = inline.content.split('\n', 1)[0]
        match = HEADER.fullmatch(header)
        if not match or not inline.map:
            continue
        row = inline.map[0]
        raw = lines[row]
        ending = '\r\n' if raw.endswith('\r\n') else '\n' if raw.endswith('\n') else ''
        physical = raw[:-len(ending)] if ending else raw
        if not physical.endswith(header):
            raise ValueError('Cannot prove callout header source span')
        prefix = physical[:-len(header)]
        kind, _, title = match.groups()
        mapped = KINDS.get(kind.casefold(), 'note')
        title = title or (kind if kind.casefold() not in STANDARD else '')
        lines[row] = prefix + '[!' + mapped.upper() + ']' + (' ' + title if title else '') + ending
    return ''.join(lines)
