"""Conservative SVG admission plus a hash-bound static Visio compatibility profile.

Not a general-purpose SVG sanitizer. Unknown capabilities fail closed.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
import xml.etree.ElementTree as ET

SVG = "{http://www.w3.org/2000/svg}"
VISIO = "{http://schemas.microsoft.com/visio/2003/SVGExtensions/}"
ALLOWED = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
           "text", "tspan", "defs", "clipPath", "mask", "linearGradient", "radialGradient",
           "stop", "title", "desc", "use", "symbol"}


def validate_svg(raw: bytes) -> None:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
        raise ValueError("SVG DTD/entities require an explicit reviewed compatibility rule")
    without_declaration = re.sub(br"^\s*<\?xml\s[^?]*\?>", b"", raw, count=1)
    if b"<?" in without_declaration:
        raise ValueError("SVG processing instructions are not allowed")
    root = ET.fromstring(raw)
    if root.tag != SVG + "svg":
        raise ValueError("SVG root must be svg in the standard namespace")
    identifiers = Counter(node.attrib["id"] for node in root.iter() if "id" in node.attrib)
    if any(count > 1 for count in identifiers.values()):
        raise ValueError("Duplicate SVG ID")
    local_references = set()
    for node in root.iter():
        if not node.tag.startswith(SVG) or node.tag[len(SVG):] not in ALLOWED:
            raise ValueError("SVG element is outside the conservative allowlist")
        for name, value in node.attrib.items():
            local = name.rsplit("}", 1)[-1].lower()
            if name.startswith("{") and name not in {"{http://www.w3.org/XML/1998/namespace}space",
                                                     "{http://www.w3.org/XML/1998/namespace}lang",
                                                     "{http://www.w3.org/1999/xlink}href"}:
                raise ValueError("Unknown SVG attribute namespace")
            if local.startswith("on") or local in {"src", "base"}:
                raise ValueError("Active SVG attribute")
            if local == "href" and not re.fullmatch(r"#[A-Za-z_][\w.-]*", value):
                raise ValueError("SVG reference must be fragment-local")
            if local == "href":
                local_references.add(value[1:])
            if local == "style":
                _declarations(value)
            if "\\" in value or re.search(r"@|expression\s*\(|(?:javascript|data|https?|file):", value, re.I):
                raise ValueError("SVG external/active expression")
            for match in re.finditer(r"url\s*\((.*?)\)", value, re.I):
                if not re.fullmatch(r"\s*['\"]?#[A-Za-z_][\w.-]*['\"]?\s*", match[1]):
                    raise ValueError("SVG URL must be fragment-local")
                local_references.add(match[1].strip().strip("'\"")[1:])
    if local_references - identifiers.keys():
        raise ValueError("SVG local reference is missing")


PROPERTIES = {"fill", "stroke", "stroke-linecap", "stroke-linejoin", "stroke-width", "font-family",
              "font-size", "font-weight", "fill-rule", "overflow", "stroke-miterlimit"}
VISIO_TAGS = {"documentProperties", "pageProperties", "textBlock", "textRect", "paragraph",
              "tabList", "userDefs", "ud"}


def _declarations(source: str) -> dict[str, str]:
    values = {}
    for declaration in source.split(";"):
        if not declaration.strip():
            continue
        name, separator, value = declaration.partition(":")
        name, value = name.strip(), value.strip()
        if not separator or name not in PROPERTIES or not re.fullmatch(r"[A-Za-z0-9#.,% -]+", value):
            raise ValueError("Visio CSS is outside the static property/value allowlist")
        values[name] = value
    return values


def transform_svg(raw: bytes, rule: dict | None = None) -> tuple[bytes, dict | None]:
    if rule is None:
        validate_svg(raw)
        return raw, None
    source_hash = hashlib.sha256(raw).hexdigest()
    if rule.get("source_sha256") != source_hash or rule.get("profile") != "visio-static-v1":
        raise ValueError("Stale or unsupported SVG compatibility profile; re-review required")
    if len(raw) > 1024 * 1024:
        raise ValueError("Reviewed Visio SVG exceeds the 1 MiB profile limit")
    # Never resolve DTDs. Strip ONLY this literal standard public declaration.
    source = raw.decode("utf-8-sig").encode("utf-8")
    source, count = re.subn(br'<!DOCTYPE\s+svg\s+PUBLIC\s+"-//W3C//DTD SVG 1\.1//EN"\s+"http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd"\s*>', b"", source)
    if count != 1 or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", source, re.I):
        raise ValueError("Visio profile requires exactly the reviewed standard DTD, without entities/internal subsets")
    if b"<?" in re.sub(br"^\s*<\?xml\s[^?]*\?>", b"", source, count=1):
        raise ValueError("Visio processing instructions are not allowed")
    root = ET.fromstring(source)
    styles = {}
    for node in root.iter():
        if node.tag.startswith(VISIO):
            if node.tag[len(VISIO):] not in VISIO_TAGS or (node.text or "").strip():
                raise ValueError("Unknown or textual Visio metadata")
            if any(not child.tag.startswith(VISIO) for child in node.iter()):
                raise ValueError("Foreign content inside Visio metadata")
        elif node.tag == SVG + "style":
            css = node.text or ""
            if len(node) or node.attrib not in ({}, {"type": "text/css"}):
                raise ValueError("Unsupported Visio stylesheet structure")
            previous = 0
            for match in re.finditer(r"\.(st[0-9]+)\s*\{([^{}]*)\}", css):
                if css[previous:match.start()].strip() or match[1] in styles:
                    raise ValueError("Unsupported/duplicate Visio CSS selector")
                styles[match[1]] = _declarations(match[2])
                previous = match.end()
            if css[previous:].strip():
                raise ValueError("Unsupported Visio CSS syntax")
        elif not node.tag.startswith(SVG) or node.tag[len(SVG):] not in ALLOWED:
            raise ValueError("Unsupported active/foreign SVG content")
    for node in list(root.iter()):
        if node.tag.startswith(VISIO) or node.tag == SVG + "style":
            continue
        properties = {}
        for name in node.attrib.get("class", "").split():
            if name not in styles:
                raise ValueError("Undefined Visio CSS class")
            properties.update(styles[name])
        # CSS overrides authored presentation attributes; inline style wins last.
        properties.update(_declarations(node.attrib.get("style", "")))
        node.attrib.pop("class", None)
        node.attrib.pop("style", None)
        for name in list(node.attrib):
            if name.startswith(VISIO):
                del node.attrib[name]
        node.attrib.update(properties)
        for child in list(node):
            if child.tag.startswith(VISIO) or child.tag == SVG + "style":
                # A metadata child inside <text> often has the visible label as
                # its tail. Preserve that text, rather than dropping the label.
                index = list(node).index(child)
                if index:
                    sibling = list(node)[index - 1]
                    sibling.tail = (sibling.tail or "") + (child.tail or "")
                else:
                    node.text = (node.text or "") + (child.tail or "")
                node.remove(child)
    output = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    validate_svg(output)
    return output, {"profile": "visio-static-v1", "source_sha256": source_hash,
                    "output_sha256": hashlib.sha256(output).hexdigest()}
