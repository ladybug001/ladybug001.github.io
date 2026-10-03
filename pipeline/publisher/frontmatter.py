from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

import yaml


class FrontmatterError(ValueError):
    pass


class StrictLoader(yaml.SafeLoader):
    """Safe YAML with duplicate-key rejection and YAML 1.2-style booleans."""


StrictLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values
          if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF")
)


def _mapping(loader: StrictLoader, node: yaml.MappingNode) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise FrontmatterError("Frontmatter keys must be strings")
        if key in result:
            raise FrontmatterError(f"Duplicate Frontmatter key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor("tag:yaml.org,2002:map", _mapping)


def parse(raw: str) -> tuple[dict[str, Any], str, int]:
    raw = raw.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, raw, 1
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise FrontmatterError("Unclosed YAML Frontmatter")
    header = "".join(lines[1:end])
    if len(header) > 256_000:
        raise FrontmatterError("Frontmatter exceeds size limit")
    try:
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
               for token in yaml.scan(header)):
            raise FrontmatterError("YAML anchors/merge aliases are not supported in publication metadata")
        data = yaml.load(header, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise FrontmatterError(str(exc)) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FrontmatterError("Frontmatter must be a mapping")
    return data, "".join(lines[end + 1:]), end + 2


def validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("publish", "draft", "private"):
        if key in data and type(data[key]) is not bool:
            errors.append(f"{key} must be a YAML boolean true/false")
    for key in ("title", "slug", "description", "id"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            errors.append(f"{key} must be a nonempty string")
    for key in ("tags", "categories", "series", "aliases"):
        if key in data and (not isinstance(data[key], list)
                            or any(not isinstance(item, str) or not item.strip() for item in data[key])):
            errors.append(f"{key} must be a list of nonempty strings")
    for key in ("date", "updated"):
        if key not in data:
            continue
        value = data[key]
        try:
            if not isinstance(value, str):
                raise ValueError()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                dt.date.fromisoformat(value)
            else:
                parsed = dt.datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError()
        except ValueError:
            errors.append(f"{key} must be ISO date or timezone-qualified datetime")
    if isinstance(data.get("id"), str):
        try:
            uuid.UUID(data["id"])
        except ValueError:
            errors.append("id must be a UUID")
    if isinstance(data.get("slug"), str):
        if not re.fullmatch(r"[\w-]+", data["slug"], flags=re.UNICODE):
            errors.append("slug must be a single URL segment containing letters, numbers, '_' or '-'")
    return errors
