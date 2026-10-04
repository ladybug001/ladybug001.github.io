"""Explicit private identity changes. No Vault writes, export or deployment."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import uuid

from .checker import check
from .identity import Binding, Registry, route_key
from .frontmatter import validate
from .safety import within
from .storage import atomic_write, writer_lock, registry_bytes, publication_head


def serialized(data):
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("Identity state/plan must not follow symlinks or junctions")
    return path


def read_state(state, vault):
    state, vault = safe_path(state), Path(vault).resolve()
    if within(vault, state) or within(state, vault):
        raise ValueError("Private state and Vault must be disjoint")
    current = safe_path(state / "identity-registry.json")
    raw = registry_bytes(current)
    revision = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "notes", "vault_root", "purpose"}:
        raise ValueError("Unexpected private state format")
    note_fields = {"id", "source_path", "slug", "permalink", "historical_paths", "status", "source_sha256"}
    if (not isinstance(data["purpose"], str) or not isinstance(data["notes"], list)
            or any(not isinstance(n, dict) or set(n) != note_fields for n in data["notes"])):
        raise ValueError("Unknown or incomplete private state fields; do not discard metadata")
    if not isinstance(data["vault_root"], str) or Path(data["vault_root"]).resolve() != vault:
        raise ValueError("Private identity state belongs to another Vault")
    registry = Registry.from_dict(data)
    saved = safe_path(state / "revisions" / (revision + ".json"))
    if not saved.is_file() or saved.read_bytes() != raw:
        raise ValueError("Current identity state has no matching immutable revision; recover first")
    return registry, data, revision


def operations(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 2048:
        raise ValueError("Select 1–2048 explicit identity operations")
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in {"move", "withdraw", "restore", "route", "register"}:
            raise ValueError("Unknown identity operation")
        allowed = {"kind", "id"} | {"move": {"new_path"}, "withdraw": {"allow_missing_source"},
                                     "restore": set(), "route": {"slug"}, "register": {"new_path"}}[item["kind"]]
        if set(item) - allowed or not isinstance(item.get("id"), str):
            raise ValueError("Invalid identity operation fields")
        item = deepcopy(item)
        item["id"] = str(uuid.UUID(item["id"]))
        if item["kind"] in {"move", "register"} and not isinstance(item.get("new_path"), str):
            raise ValueError("Move requires an exact Vault-relative new_path")
        if item["kind"] == "route" and ("slug" not in item or validate({"slug": item["slug"]})):
            raise ValueError("Route migration requires a valid single-segment slug")
        if "allow_missing_source" in item and type(item["allow_missing_source"]) is not bool:
            raise ValueError("allow_missing_source must be boolean")
        if item["kind"] == "withdraw":
            item.setdefault("allow_missing_source", False)
        result.append(item)
    return result


def source_inventory(report):
    # Unscoped notes are path stubs: never read diary/private bodies here.
    return {"notes": [{"path": n.path, "sha256": n.content_hash, "valid": n.valid,
                        "in_scope": n.in_scope, "eligible": n.published}
                       for n in sorted(report.notes, key=lambda n: n.path)],
            "assets": {path: {"sha256": info["sha256"], "bytes": info["bytes"]}
                       for path, info in sorted(report.assets.items())}}


def candidate(registry, requested, report):
    result = Registry(registry.bindings)
    by_path = {n.path: n for n in report.notes}
    for op in requested:
        binding = result.get_id(op["id"])
        if op["kind"] == "register":
            target = by_path.get(op["new_path"])
            if binding or result.get(op["new_path"]) or target is None or not target.published:
                raise ValueError("Register requires an eligible unbound source and an unused UUID")
            if target.metadata.get("id") and str(uuid.UUID(target.metadata["id"])) != op["id"]:
                raise ValueError("Register UUID differs from explicit source ID")
            slug = target.metadata.get("slug") or "n-" + uuid.UUID(op["id"]).hex
            result = Registry([*result.bindings, Binding(op["id"], target.path, slug, "/notes/" + slug + "/", source_sha256=target.content_hash)])
            continue
        if binding is None:
            raise ValueError("Identity changes cannot allocate or guess an unknown ID")
        source = by_path.get(binding.source_path)
        if op["kind"] == "move":
            new_path = op["new_path"]
            target = by_path.get(new_path)
            if source is not None or target is None or not target.in_scope or not target.valid:
                raise ValueError("Move requires old source absent and exact valid new source present; copies are not moves")
            explicit = target.metadata.get("id")
            if explicit and str(uuid.UUID(explicit)) != binding.id:
                raise ValueError("Move target carries a different immutable ID")
            if binding.status == "active" and not target.published:
                raise ValueError("Moving an active identity requires an eligible target; explicitly withdraw first")
            binding.source_path = new_path
            binding.source_sha256 = target.content_hash
        elif op["kind"] == "withdraw":
            if binding.status != "active":
                raise ValueError("Identity is already withdrawn")
            if source is None and not op["allow_missing_source"]:
                raise ValueError("Missing source requires an explicit allow_missing_source withdrawal")
            if source is not None and (not source.valid or source.published):
                raise ValueError("Withdrawal requires a valid source with publication disabled or outside the allowed scope")
            binding.status = "withdrawn"
        elif op["kind"] == "restore":
            if binding.status != "withdrawn" or source is None or not source.published:
                raise ValueError("Restore requires an eligible source and a withdrawn identity")
            binding.status = "active"
            binding.source_sha256 = source.content_hash
        else:
            if binding.status != "active" or source is None or not source.published or not binding.permalink:
                raise ValueError("Route migration requires an eligible active identity with a locked URL")
            new_route = "/notes/" + op["slug"] + "/"
            if route_key(new_route) == route_key(binding.permalink):
                raise ValueError("New route must differ from the current URL")
            # Explicitly revisiting one's own old URL is safe; remove its redirect
            # reservation and reserve the previous current URL instead.
            binding.historical_paths = [p for p in binding.historical_paths if route_key(p) != route_key(new_route)]
            binding.historical_paths.append(binding.permalink)
            binding.permalink, binding.slug = new_route, op["slug"]
        result = Registry(result.bindings)  # Detect path/history/withdrawn collisions after each operation.
    return result


def checked(vault, policy, registry, repairs, legacy, svg):
    report = check(vault, policy, registry, repairs, legacy, svg)
    if report.failed or not report.read_verified:
        codes = sorted({i.code for i in report.issues if i.severity == "error"})
        raise ValueError("Candidate identity state fails validation: " + ", ".join(codes))
    return report


def make_plan(state, vault, policy, requested, repairs=None, legacy=None, svg=None):
    registry, data, revision = read_state(state, vault)
    requested = operations(requested)
    # Current state may be intentionally invalid after a move or opt-in change.
    # It is only the resulting candidate that must pass the full checker.
    observed = check(vault, policy, registry, repairs, legacy, svg)
    if not observed.read_verified:
        raise ValueError("Source changed during identity planning; retry")
    proposed = candidate(registry, requested, observed)
    validated = checked(vault, policy, proposed, repairs, legacy, svg)
    if source_inventory(observed) != source_inventory(validated):
        raise ValueError("Source inventory changed during identity planning; retry")
    target = {**data, **proposed.to_dict()}
    value = {"schema_version": 1, "kind": "private-identity-plan", "content_export": False,
             "vault_root": str(Path(vault).resolve()), "base_revision": revision,
             "target_revision": digest(serialized(target)), "target_registry": target,
             "operations": requested, "source_inventory": source_inventory(validated),
             "configuration_sha256": digest(serialized({"policy": policy, "repairs": repairs or [],
                                                         "legacy": legacy or {}, "svg": svg or {}}))}
    return {**value, "plan_sha256": digest(serialized(value))}


def validate_plan(plan, expected):
    keys = {"schema_version", "kind", "content_export", "vault_root", "base_revision", "target_revision",
            "target_registry", "operations", "source_inventory", "configuration_sha256", "plan_sha256"}
    if (not isinstance(plan, dict) or set(plan) != keys or plan.get("schema_version") != 1
            or plan.get("kind") != "private-identity-plan" or plan.get("content_export") is not False):
        raise ValueError("Invalid private identity plan")
    for name in ("base_revision", "target_revision", "configuration_sha256", "plan_sha256"):
        if not isinstance(plan[name], str) or not re.fullmatch(r"[a-f0-9]{64}", plan[name]):
            raise ValueError("Invalid plan digest")
    payload = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if expected != plan["plan_sha256"] or digest(serialized(payload)) != expected:
        raise ValueError("Identity plan changed or does not match the explicitly confirmed digest")
    operations(plan["operations"])
    if digest(serialized(plan["target_registry"])) != plan["target_revision"]:
        raise ValueError("Target state digest mismatch")


def apply_plan(state, vault, policy, plan, expected, repairs=None, legacy=None, svg=None):
    """CAS one current registry; immutable revisions/journal support same-plan retry.

    This is NOT a registry+public-snapshot publication transaction. A real crash
    leaves the writer lock for manual investigation; locks are never broken.
    """
    validate_plan(plan, expected)
    state = safe_path(state)
    # Check disjointness before writer_lock can create anything.
    read_state(state, vault)
    if publication_head(state) is not None:
        raise ValueError("Paired publication is authoritative; use publication-plan/apply instead of an identity-only write")
    with writer_lock(state):
        registry, data, revision = read_state(state, vault)
        if plan["vault_root"] != str(Path(vault).resolve()):
            raise ValueError("Plan belongs to another Vault")
        journal_path = safe_path(state / "transactions" / (expected + ".json"))
        receipt = {"schema_version": 1, "plan_sha256": expected, "base_revision": plan["base_revision"],
                   "target_revision": plan["target_revision"], "status": "prepared"}
        existing = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else None
        if existing is not None and existing not in (receipt, {**receipt, "status": "committed"}):
            raise ValueError("Identity transaction journal was changed; inspect recovery state")
        if revision == plan["target_revision"]:
            if existing is None:
                raise ValueError("Matching target without transaction journal is not an authorized replay")
            atomic_write(journal_path, serialized({**receipt, "status": "committed"}))
            return {"changed": False, "recovered_or_repeated": True, "revision": revision}
        if revision != plan["base_revision"] or (existing and existing["status"] == "committed"):
            raise ValueError("Identity state changed since planning; refuse stale plan or rollback")
        current_plan = make_plan(state, vault, policy, plan["operations"], repairs, legacy, svg)
        if current_plan != plan:
            raise ValueError("Source, configuration or proposed state changed since planning; generate a new plan")
        target_text = serialized(plan["target_registry"])
        revision_path = safe_path(state / "revisions" / (plan["target_revision"] + ".json"))
        if revision_path.exists():
            if revision_path.read_bytes() != target_text.encode("utf-8"):
                raise ValueError("Immutable identity revision changed; refuse overwrite")
        else:
            atomic_write(revision_path, target_text)
        atomic_write(journal_path, serialized(receipt))
        # Last barrier after staging. Failure preserves the previous current state.
        if make_plan(state, vault, policy, plan["operations"], repairs, legacy, svg) != plan:
            raise ValueError("Source/state changed before promotion; current identity state preserved")
        atomic_write(safe_path(state / "identity-registry.json"), target_text)
        atomic_write(journal_path, serialized({**receipt, "status": "committed"}))
        return {"changed": True, "recovered_or_repeated": False, "revision": plan["target_revision"]}
