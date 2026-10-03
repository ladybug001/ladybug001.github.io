"""Framework-independent presentation decisions; no templates or Vault writes."""
from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .model import Reference


UNAVAILABLE_MESSAGE = "该笔记暂未公开或不可访问。"


def unavailable_text(ref: Reference, *, private: bool = False) -> dict[str, str]:
    """A text node, not a link with '#' or a private-target URL.

    Display text comes only from the already-authored public source reference.
    Do not use the target note's frontmatter title, paths, IDs or fragments.
    A renderer must emit this as a text node, not parse its label as HTML/Markdown.
    """
    if private:
        label = "未公开笔记"
    elif ref.label:
        label = ref.label
    else:
        path = (unquote(urlsplit(ref.target).path) if ref.kind.startswith("markdown")
                else ref.target.partition("#")[0])
        label = PurePosixPath(path.replace("\\", "/")).name
        if label.casefold().endswith(".md"):
            label = label[:-3]
        label = label or "未公开笔记"
    return {"action": "plain_text", "text": label, "message": UNAVAILABLE_MESSAGE}
