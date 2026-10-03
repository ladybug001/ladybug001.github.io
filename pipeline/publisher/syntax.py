"""Markdown AST inspection plus small Obsidian token rules, never conversion."""
from __future__ import annotations

import re
from collections import Counter
from functools import wraps
from functools import partial

from markdown_it import MarkdownIt
from markdown_it.rules_inline import backtick, image, link
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.dollarmath.index import math_block_dollar, math_inline_dollar
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.footnote.index import footnote_ref

from .identity import key
from .model import Heading, Note, Reference, Report


def _capture(rule):
    @wraps(rule)
    def wrapped(state, silent):
        start, count = state.pos, len(state.tokens)
        matched = rule(state, silent)
        if matched and not silent:
            for token in state.tokens[count:]:
                if token.type in {"link_open", "image", "code_inline", "math_inline", "math_inline_double", "footnote_ref"}:
                    # Do not replace a nested token's more precise position.
                    token.meta.setdefault("origin", (state.src, start, state.pos))
        return matched
    return wrapped


def _wiki(state, silent):
    start = state.pos
    embed = state.src.startswith("![[", start)
    opener = 3 if embed else 2
    if not embed and not state.src.startswith("[[", start):
        return False
    end = state.src.find("]]", start + opener)
    if end < 0 or "\n" in state.src[start:end]:
        # A Markdown label can start with a bracket: [[原创]title](https://...).
        # Give a valid standard link to the Markdown rule, not a malformed wiki token.
        if not embed:
            valid_markdown = link(state, True)
            state.pos = start
            if valid_markdown:
                return False
        if not silent:
            token = state.push("obs_invalid", "", 0)
            token.meta["origin"] = (state.src, start, start + opener)
            token.meta["kind"] = "markdown" if not embed and "](" in state.src[start:].split("\n")[0] else "wiki"
        state.pos = start + opener
        return True
    spec = state.src[start + opener:end].replace("\\|", "|")
    target, _, label = spec.partition("|")
    if not silent:
        token = state.push("obs_wiki", "", 0)
        token.content = label or target
        token.meta.update(target=target.strip(), embed=embed, label=label,
                          origin=(state.src, start, end + 2))
    state.pos = end + 2
    return True


def _tex_delimiter(state, silent):
    start = state.pos
    opener = state.src[start:start + 2]
    if opener not in {"\\(", "\\["}:
        return False
    end = state.src.find("\\)" if opener == "\\(" else "\\]", start + 2)
    if end < 0:
        return False
    if not silent:
        token = state.push("math_inline", "math", 0)
        token.content = state.src[start + 2:end]
        token.meta["origin"] = (state.src, start, end + 2)
    state.pos = end + 2
    return True


_dollar_block_rule = math_block_dollar(allow_labels=False, allow_blank_lines=True)


def _interrupting_math(state, start, end, silent):
    # The pinned plugin's rule does not implement a read-only silent probe.
    # Protect tokenizer state when CommonMark asks whether math ends a paragraph.
    if not silent:
        return _dollar_block_rule(state, start, end, False)
    previous_line, previous_count = state.line, len(state.tokens)
    try:
        return _dollar_block_rule(state, start, end, True)
    finally:
        state.line = previous_line
        del state.tokens[previous_count:]


def parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True}).enable("table").use(
        dollarmath_plugin, allow_labels=False, double_inline=True
    ).use(footnote_plugin, move_to_end=False)
    md.inline.ruler.before("link", "obs_wiki", _wiki)
    md.inline.ruler.before("escape", "tex_delimiter", _tex_delimiter)
    md.inline.ruler.at("backticks", _capture(backtick))
    md.inline.ruler.at("link", _capture(link))
    md.inline.ruler.at("image", _capture(image))
    md.inline.ruler.at("math_inline", _capture(math_inline_dollar(allow_double=True)))
    md.inline.ruler.at("footnote_ref", _capture(partial(footnote_ref, always_match=False)))
    md.block.ruler.at("math_block", _interrupting_math,
                      {"alt": ["paragraph", "reference", "blockquote", "list"]})
    return md


def _offset(token, inline, lines: list[str], starts: list[int]) -> tuple[int, int, int]:
    src, pos, end = token.meta.get("origin", (inline.content, 0, 0))
    index = (inline.map or [0])[0] + src[:pos].count("\n")
    index = min(index, max(0, len(lines) - 1))
    src_line_start = src.rfind("\n", 0, pos) + 1
    src_line_end = src.find("\n", pos)
    fragment = src[src_line_start:src_line_end if src_line_end >= 0 else len(src)]
    # Markdown strips list/blockquote prefixes; table cells have their own inline source.
    column_base = lines[index].find(fragment) if lines else 0
    column_base = max(column_base, 0)
    column = column_base + pos - src_line_start
    absolute = (starts[index] if starts else 0) + column
    return index, column, absolute


def inline_positions(inline, lines: list[str], starts: list[int], cursors: dict[int, int]) -> list[int]:
    """Prove inline AST-to-source positions, including escaped table pipes."""
    if not inline.map:
        raise ValueError("Inline source has no physical range")
    positions = []
    for offset, piece in enumerate(inline.content.split("\n")):
        row = inline.map[0] + offset
        raw = lines[row].rstrip("\n")
        begin = cursors.get(row, 0)
        found = raw.find(piece, begin)
        if found >= 0:
            mapping = list(range(len(raw)))
        else:
            expanded, expanded_map, column = "", [], 0
            for index, character in enumerate(raw):
                piece_chars = " " * (4 - column % 4) if character == "\t" else character
                expanded += piece_chars
                expanded_map.extend([index] * len(piece_chars))
                column += len(piece_chars)
            normalized, mapping = "", []
            index = 0
            while index < len(expanded):
                if expanded[index:index + 2] == "\\|":
                    index += 1
                normalized += expanded[index]
                mapping.append(expanded_map[index])
                index += 1
            found = normalized.find(piece, next((i for i, p in enumerate(mapping) if p >= begin), len(mapping)))
        if found < 0:
            raise ValueError(f"Cannot prove inline source span at body line {row + 1}")
        mapped = [starts[row] + mapping[i] for i in range(found, found + len(piece))]
        positions.extend(mapped)
        endpoint = mapped[-1] + 1 if mapped else starts[row] + found
        cursors[row] = endpoint - starts[row]
        if offset < inline.content.count("\n"):
            positions.append(starts[row] + len(lines[row]) - 1)
    positions.append(endpoint)
    return positions


def _mask_comments(body: str, md: MarkdownIt) -> tuple[str, bool]:
    """Remove hidden comments for inspection, except in literal AST regions.

    Preserve newlines/length for source locations; never modify the source file.
    """
    lines = body.splitlines(keepends=True)
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)
    protected = bytearray(len(body))
    cursors: dict[int, int] = {}
    for token in md.parse(body):
        if token.type in {"fence", "code_block", "math_block", "math_block_eqno", "html_block"} and token.map:
            begin = starts[token.map[0]]
            finish = starts[token.map[1]] if token.map[1] < len(starts) else len(body)
            protected[begin:finish] = b"\x01" * (finish - begin)
        if token.type == "inline":
            literal = [child for child in token.children or []
                       if child.type in {"code_inline", "math_inline", "math_inline_double"}]
            positions = inline_positions(token, lines, starts, cursors) if literal else None
            for child in token.children or []:
                if child in literal:
                    src, pos, end = child.meta["origin"]
                    if src != token.content:
                        raise ValueError("Cannot prove literal source span")
                    begin, finish = positions[pos], positions[end - 1] + 1
                    protected[begin:finish] = b"\x01" * (finish - begin)
    markers = [m.start() for m in re.finditer("%%", body) if not any(protected[m.start():m.start() + 2])]
    chars = list(body)
    for i in range(0, len(markers), 2):
        begin = markers[i]
        finish = markers[i + 1] + 2 if i + 1 < len(markers) else len(body)
        for p in range(begin, finish):
            if chars[p] != "\n":
                chars[p] = " "
    return "".join(chars), bool(len(markers) % 2)


def _plain(tokens) -> str:
    return "".join(t.content for t in tokens if t.type in {
        "text", "text_special", "code_inline", "obs_wiki", "math_inline", "math_inline_double"
    }).strip()


def inspect(note: Note, report: Report) -> None:
    md = parser()
    body, unclosed_comment = _mask_comments(note.body, md)
    if unclosed_comment:
        report.add("UNCLOSED_COMMENT", "Unclosed Obsidian %% comment", source=note.path)
    lines = body.splitlines(keepends=True)
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)
    tokens = md.parse(body)
    features: Counter[str] = Counter()
    ancestors: list[Heading] = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[i + 1]
            text = _plain(inline.children or [])
            # Explicit generated/user heading IDs are handled separately in a future adapter.
            text = re.sub(r"\s+\{#[^}]+\}\s*$", "", text)
            level = int(token.tag[1:])
            while ancestors and ancestors[-1].level >= level:
                ancestors.pop()
            heading = Heading(text, level, note.body_line + token.map[0],
                              [h.text for h in ancestors] + [text])
            note.headings.append(heading)
            ancestors.append(heading)
        if token.type == "fence":
            language = token.info.strip().split(" ", 1)[0].casefold()
            if language == "mermaid":
                features["mermaid"] += 1
            if language in {"dataview", "dataviewjs"}:
                report.add("UNSUPPORTED_DYNAMIC_BLOCK", "Dataview requires a separate exporter",
                           source=note.path, line=note.body_line + token.map[0])
        if token.type.startswith("math_"):
            features["math"] += 1
        if token.type == "html_block":
            features["raw_html"] += 1
        if token.type != "inline":
            continue
        for child in token.children or []:
            index, column, _ = _offset(child, token, lines, starts)
            line = note.body_line + index
            if child.type == "obs_invalid":
                code = "INVALID_MARKDOWN_LINK" if child.meta["kind"] == "markdown" else "INVALID_WIKILINK"
                report.add(code, "Malformed/unclosed link", source=note.path, line=line, column=column + 1)
            elif child.type == "obs_wiki":
                note.references.append(Reference(child.meta["target"],
                                                 "wiki_embed" if child.meta["embed"] else "wiki_link",
                                                 line, column + 1, child.meta["label"]))
            elif child.type in {"link_open", "image"}:
                destination = child.attrGet("href" if child.type == "link_open" else "src") or ""
                label = ""
                if child.type == "link_open":
                    # Capture already-authored display text for eventual unavailable-link fallback.
                    # Nested formatting is flattened, never reinterpreted as HTML by presentation.
                    children = token.children or []
                    position = children.index(child)
                    closing = next((j for j in range(position + 1, len(children))
                                    if children[j].type == "link_close"), len(children))
                    label = _plain(children[position + 1:closing])
                else:
                    label = _plain(child.children or []) or child.content
                note.references.append(Reference(destination, "markdown_image" if child.type == "image"
                                                 else "markdown_link", line, column + 1, label))
            elif child.type == "text":
                for match in re.finditer(r"(?:^|\s)\^([A-Za-z0-9-]+)(?=\s*$)", child.content):
                    # A text token may span a multiline paragraph. Its default
                    # offset is the paragraph start, NOT the caret marker line.
                    physical = [row for row in range(token.map[0], token.map[1])
                                if re.search(r"(?<!\\)\^" + re.escape(match[1]) + r"\s*$", lines[row])]
                    if len(physical) != 1:
                        report.add("UNVERIFIED_BLOCK_LOCATION", "Cannot prove physical block marker location",
                                   source=note.path, target=match[1], line=line)
                        continue
                    block_line = note.body_line + physical[0]
                    note.blocks.setdefault(match[1], []).append(block_line)
                    selected_range = (note.body_line + token.map[0], note.body_line + token.map[1])
                    if token.content.strip() == "^" + match[1]:
                        preceding = [t.map for t in tokens[:i]
                                     if t.type in {"paragraph_open", "bullet_list_open", "ordered_list_open",
                                                   "blockquote_open", "table_open", "fence", "code_block"}
                                     and t.map and t.map[1] <= token.map[0]]
                        if preceding:
                            end = max(span[1] for span in preceding)
                            begin = min(span[0] for span in preceding if span[1] == end)
                            selected_range = (note.body_line + begin, note.body_line + end)
                    note.block_ranges.setdefault(match[1], []).append(selected_range)
                features["callout"] += len(re.findall(r"\[![A-Za-z][\w-]*\]", child.content))
            if child.type.startswith("math_"):
                features["math"] += 1
            if child.type == "html_inline":
                features["raw_html"] += 1
    note.features = dict(sorted(features.items()))
    for block, locations in note.blocks.items():
        if len(locations) > 1:
            report.add("DUPLICATE_BLOCK_ID", "Block ID occurs more than once", source=note.path,
                       target=block, line=locations[0])
    if features["raw_html"]:
        report.add("HTML_REQUIRES_REVIEW", "Raw HTML needs a safe rendering policy before export",
                   severity="warning", source=note.path)


def heading_matches(note: Note, fragment: str) -> list[Heading]:
    md = parser()
    parts = [key(_plain(md.parseInline(part.strip())[0].children or [])) for part in fragment.split("#")]
    if len(parts) == 1:
        return [h for h in note.headings if key(h.text) == parts[0]]
    return [h for h in note.headings if [key(p) for p in h.path[-len(parts):]] == parts]
