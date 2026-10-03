"""Validate built sample artifacts, with explicit partial-site boundaries."""
from __future__ import annotations

from collections import Counter
import hashlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from .safety import within


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = Counter()
        self.links = []
        self.active = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids[values["id"]] += 1
        for name in ("href", "src", "poster"):
            if values.get(name):
                self.links.append(values[name])
        if tag in {"script", "iframe", "object", "embed"} or any(k.lower().startswith("on") for k in values):
            self.active.append(tag)


def verify_html(public: Path, manifest: dict, *, strict: bool = False) -> dict:
    if manifest.get("sample_only") is not True or manifest.get("deployable") is not False:
        raise ValueError("Expected an explicitly non-deployable sample manifest")
    public = public.resolve()
    pages, errors = {}, []
    for route in manifest["routes"]:
        source = public / unquote(route["url"]).lstrip("/") / "index.html"
        if not within(public, source) or not source.is_file():
            errors.append("Missing built page: " + route["url"])
            continue
        page = Page()
        page.feed(source.read_text(encoding="utf-8"))
        pages[route["url"]] = page
        errors.extend("Missing anchor: " + route["url"] + "#" + value for value in route["anchors"] if value not in page.ids)
        errors.extend("Duplicate HTML ID: " + route["url"] + "#" + value for value, count in page.ids.items() if count > 1)
        if page.active:
            errors.append("Unexpected active HTML: " + route["url"])
        prefix = "content/notes/" + route["id"] + "/media/"
        for name, expected_hash in manifest["files"].items():
            if name.startswith(prefix):
                target = public / unquote(route["url"]).lstrip("/") / "media" / name[len(prefix):]
                if not within(public, target) or not target.is_file():
                    errors.append("Missing bundle asset: " + name)
                elif hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                    errors.append("Built asset hash mismatch: " + name)
    boundary = set(manifest["outside_sample_urls"])
    if strict and boundary:
        raise ValueError("Strict HTML validation forbids outside-sample exemptions")
    validated, skipped = 0, set()
    for route, page in pages.items():
        for raw in page.links:
            parts = urlsplit(raw)
            if parts.scheme or parts.netloc:
                if parts.scheme not in {"http", "https", "mailto", "tel", ""}:
                    errors.append("Unsafe HTML scheme: " + raw)
                continue
            absolute = unquote(urljoin(route, raw))
            path = absolute.split("#", 1)[0]
            fragment = unquote(urlsplit(absolute).fragment)
            if absolute in boundary:
                skipped.add(absolute)
                continue
            if path in pages:
                if fragment and fragment not in pages[path].ids:
                    errors.append("Broken rendered anchor: " + absolute)
                else:
                    validated += 1
            else:
                candidate = public / path.lstrip("/")
                if not within(public, candidate) or not candidate.is_file():
                    errors.append("Broken rendered resource: " + absolute)
                else:
                    validated += 1
        expected = {e["url"] for e in manifest["link_graph"] if e.get("rendered", True)
                    and e.get("rendered_on_id", e["source_id"]) == next(r["id"] for r in manifest["routes"] if r["url"] == route)}
        actual = {unquote(urljoin(route, value)) for value in page.links}
        errors.extend("Converted link absent from HTML: " + value for value in sorted(expected - actual))
    return {"passed": not errors, "pages": len(pages), "strict": strict, "validated_internal_links_and_resources": validated,
            "outside_sample_not_verified": sorted(skipped), "errors": errors,
            "limitations": ["Every selected snapshot link must resolve; no outside-sample exemptions." if strict else "Only selected pages are rendered. Outside-sample published URLs are NOT HTML-verified.",
                            "External URLs are not fetched. Math/Mermaid visual rendering is not tested.",
                            "The technical harness is not a selected Theme or production website."]}
