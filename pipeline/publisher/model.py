from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "error"
    source: str = ""
    line: int | None = None
    column: int | None = None
    target: str = ""
    candidates: list[str] = field(default_factory=list)


@dataclass
class Reference:
    target: str
    kind: str
    line: int
    column: int
    label: str = ""
    status: str = "pending"
    target_path: str | None = None
    target_id: str | None = None
    fragment: str = ""
    presentation: dict[str, str] | None = None


@dataclass
class Heading:
    text: str
    level: int
    line: int
    path: list[str] = field(default_factory=list)


@dataclass
class Note:
    path: str
    file: Path
    in_scope: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    body_line: int = 1
    published: bool = False
    valid: bool = True
    content_hash: str | None = None
    stat_signature: tuple[int, int] | None = None
    headings: list[Heading] = field(default_factory=list)
    blocks: dict[str, list[int]] = field(default_factory=dict)
    block_ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    references: list[Reference] = field(default_factory=list)
    features: dict[str, int] = field(default_factory=dict)
    note_id: str | None = None
    permalink: str | None = None

    @property
    def name(self) -> str:
        return Path(self.path).stem


@dataclass
class Report:
    vault: str
    notes: list[Note] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    read_verified: bool = False
    svg_rules: dict[str, dict[str, str]] = field(default_factory=dict)

    def add(self, code: str, message: str, **kwargs: Any) -> None:
        self.issues.append(Issue(code, message, **kwargs))

    @property
    def failed(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        codes: dict[str, int] = {}
        for issue in self.issues:
            codes[issue.code] = codes.get(issue.code, 0) + 1
        return {
            "schema_version": 1,
            "mode": "check-only",
            "privacy": "local-only: contains Vault paths and unpublished target names",
            "vault": self.vault,
            "passed": not self.failed,
            "source_read_verified": self.read_verified,
            "summary": {
                "indexed_notes": len(self.notes),
                "scoped_notes": sum(n.in_scope for n in self.notes),
                "published_notes": sum(n.published for n in self.notes),
                "bound_published_notes": sum(n.published and n.note_id is not None for n in self.notes),
                "references": sum(len(n.references) for n in self.notes if n.published),
                "referenced_assets": len(self.assets),
                "attachment_bytes": sum(a["bytes"] for a in self.assets.values()),
                "errors": sum(i.severity == "error" for i in self.issues),
                "warnings": sum(i.severity == "warning" for i in self.issues),
                "nonclickable_references": sum(r.presentation is not None for n in self.notes
                                                if n.published for r in n.references),
                "issue_codes": dict(sorted(codes.items())),
            },
            "issues": [asdict(i) for i in self.issues],
            "note_index": [{
                "source_path": n.path, "in_scope": n.in_scope,
                "published": n.published, "valid": n.valid,
                "id": n.note_id, "permalink": n.permalink,
                "content_hash": n.content_hash,
                "headings": [asdict(h) for h in n.headings],
                "blocks": n.blocks, "block_ranges": n.block_ranges, "features": n.features,
            } for n in self.notes],
            "link_graph": [{"source_path": n.path, "source_id": n.note_id, **asdict(r)}
                           for n in self.notes if n.published for r in n.references],
            "assets": self.assets,
            "limitations": [
                "No permanent IDs or URLs are allocated by check.",
                "check validates heading/block existence, not rendered HTML; verify-sample separately checks selected build artifacts.",
                "check does not convert or render content. The isolated sample adapter is not a public snapshot/deployment pipeline.",
                "Out-of-scope notes are name/path stubs; their bodies and frontmatter are not read.",
            ],
        }
