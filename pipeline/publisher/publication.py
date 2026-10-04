"""Full opt-in local publication transactions. Never push, deploy or edit Vault."""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import json
import re
import tempfile
from pathlib import Path
import uuid

from .checker import check
from .hugo import Context, convert
from .lifecycle import candidate, checked, operations, read_state, safe_path, source_inventory
from .snapshot import (_id, _install, _json, _keys, _page_from_hugo, _tree, _verify_files,
                       inspect_snapshot, payload, sha, validate)
from .storage import atomic_write, publication_head, writer_lock
from .safety import within
from .tooling import PROJECT, local_hugo, runtime_check


def head_for(release, registry_revision, snapshot_sha256):
    return {"schema_version": 1, "kind": "publication-head", "release": release,
            "registry_revision": registry_revision, "snapshot_sha256": snapshot_sha256}


def current_release(state):
    head = publication_head(state)
    if head is None or head["release"] is None:
        return head, None, {"records": {}, "reservations": []}
    root = safe_path(Path(state) / "publication-releases" / head["release"])
    files = _tree(root)
    manifest = _json(files["manifest.json"])
    _keys(manifest, {"schema_version", "kind", "files"})
    if manifest["schema_version"] != 1 or manifest["kind"] != "private-publication-release":
        raise ValueError("Invalid private publication release")
    _verify_files(files, manifest)
    if head["release"] != "release-" + sha(files["manifest.json"])[:24]:
        raise ValueError("Private release identity mismatch")
    snapshot = {name.removeprefix("snapshot/"): raw for name, raw in files.items() if name.startswith("snapshot/")}
    public, pages = validate(snapshot)
    if sha(snapshot["manifest.json"]) != head["snapshot_sha256"] or sha(files["registry.json"]) != head["registry_revision"]:
        raise ValueError("Publication pointer does not match its complete release")
    observations = _json(files["observations.json"])
    _keys(observations, {"records", "reservations"})
    if observations["reservations"] != public["reservations"] or not isinstance(observations["records"], dict):
        raise ValueError("Publication observation/route mismatch")
    for identifier, record in observations["records"].items():
        _id(identifier)
        _keys(record, {"semantic_sha256", "first_observed_at", "auto_updated"})
        if not isinstance(record["semantic_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", record["semantic_sha256"]):
            raise ValueError("Invalid observation digest")
        first = utc_time(record["first_observed_at"])
        if record["auto_updated"] is not None and utc_time(record["auto_updated"]) < first:
            raise ValueError("Observation update predates first observation")
    if not set(pages) <= set(observations["records"]):
        raise ValueError("Published page missing semantic observation")
    if set(files) != {"manifest.json", "registry.json", "observations.json"} | {"snapshot/" + n for n in snapshot}:
        raise ValueError("Unknown private release files")
    return head, root, observations


def utc_time(value):
    if not isinstance(value, str):
        raise ValueError("Publication observation time must be a UTC string")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("Publication observation time must be timezone-qualified UTC")
    return parsed


def derive_registry(state, vault, policy, requested, repairs, legacy, svg):
    registry, data, revision = read_state(state, vault)
    observed = check(vault, policy, registry, repairs, legacy, svg)
    if not observed.read_verified:
        raise ValueError("Source changed while planning")
    requested = operations(requested) if requested else []
    proposed = candidate(registry, requested, observed)
    by_path = {n.path: n for n in observed.notes}
    inferred = []
    # These are proposed state changes in a digest-reviewed plan, not writes by
    # check. Never infer moves from a title, basename or matching content hash.
    for binding in proposed.bindings:
        note = by_path.get(binding.source_path)
        if note is None:
            if binding.status == "active":
                raise ValueError("Missing bound source: explicitly confirm move or missing-source withdrawal")
            continue
        if binding.status == "active" and not note.published:
            inferred.append({"kind": "withdraw", "id": binding.id})
        elif binding.status == "withdrawn" and note.published:
            inferred.append({"kind": "restore", "id": binding.id})
    proposed = candidate(proposed, operations(inferred), observed) if inferred else proposed
    for note in sorted(observed.notes, key=lambda n: n.path):
        if not note.published or proposed.get(note.path):
            continue
        if note.metadata.get("id") and proposed.get_id(note.metadata["id"]):
            raise ValueError("Existing immutable ID moved: confirm an explicit move binding")
        identifier = note.metadata.get("id") or str(uuid.uuid5(uuid.NAMESPACE_URL, revision + ":" + note.path))
        op = {"kind": "register", "id": identifier, "new_path": note.path}
        proposed = candidate(proposed, [op], observed)
        inferred.append(op)
    validated = checked(vault, policy, proposed, repairs, legacy, svg)
    if source_inventory(observed) != source_inventory(validated):
        raise ValueError("Source inventory changed while planning")
    return {**data, **proposed.to_dict()}, revision, requested, inferred, validated, proposed


def full_snapshot(report, registry, previous, observed_at):
    files, routes, graph = {}, [], []
    records = deepcopy(previous["records"])
    public_ids = set()
    for note in sorted((n for n in report.notes if n.published), key=lambda n: n.note_id or ""):
        context = Context(note, report)
        markdown, assets, edges = convert(note, report, context)
        resources = []
        for name, raw in assets.items():
            name = name.removeprefix("media/")
            resources.append(name)
            files["assets/" + name] = raw
        page = _page_from_hugo(markdown.encode("utf-8"), {"id": note.note_id, "url": note.permalink, "anchors": context.anchors}, resources)
        semantic = sha(payload(page))
        record = records.get(note.note_id)
        if record is None:
            record = {"semantic_sha256": semantic, "first_observed_at": observed_at, "auto_updated": None}
        elif record["semantic_sha256"] != semantic:
            if utc_time(observed_at) < utc_time(record["auto_updated"] or record["first_observed_at"]):
                raise ValueError("Publication observation clock moved backwards")
            record = {**record, "semantic_sha256": semantic, "auto_updated": observed_at}
        records[note.note_id] = record
        if "updated" not in page["metadata"] and record["auto_updated"] is not None:
            page["metadata"]["updated"] = record["auto_updated"]
        files["pages/" + note.note_id + ".json"] = payload(page)
        # Conversion visits headings and block markers in separate passes.
        # Public anchor order is the proven Markdown source order, not visitor
        # order (blocks may occur between multiple headings).
        routes.append({"id": note.note_id, "url": note.permalink,
                       "anchors": [item["id"] for item in page["anchor_insertions"]]})
        graph.extend(edges)
        public_ids.add(note.note_id)
    previous_ids = {r["id"] for r in previous["reservations"]}
    reservations = []
    for binding in sorted(registry.bindings, key=lambda b: b.id):
        if binding.id in public_ids | previous_ids:
            reservations.append({"id": binding.id, "url": binding.permalink, "history": binding.historical_paths,
                                 "status": "active" if binding.id in public_ids else "withdrawn"})
    redirects = sorted(({"id": r["id"], "from": old, "to": r["url"]} for r in reservations
                        if r["status"] == "active" for old in r["history"]), key=lambda r: r["from"])
    manifest = {"schema_version": 2, "format": "portable-2", "scope": "complete-opt-in",
                "validation_only": True, "deployable": False, "routes": routes,
                "identities": [{"id": r["id"], "url": r["url"]} for r in routes],
                "link_graph": graph, "reservations": reservations, "redirects": redirects,
                "files": {n: sha(raw) for n, raw in sorted(files.items())}}
    files["manifest.json"] = payload(manifest)
    validate(files)
    return files, {"records": records, "reservations": reservations}


def conversion_review(report):
    """Read-only deep conversion check; collect failures, retain no exported body."""
    for note in sorted((n for n in report.notes if n.published), key=lambda n: n.path):
        if not note.note_id or not note.permalink:
            report.add("CONVERSION_IDENTITY_PENDING", "New opt-in note needs a reviewed identity plan before conversion", severity="warning", source=note.path)
            continue
        try:
            context = Context(note, report)
            markdown, assets, _ = convert(note, report, context)
            _page_from_hugo(markdown.encode("utf-8"), {"id": note.note_id, "url": note.permalink, "anchors": context.anchors},
                            [name.removeprefix("media/") for name in assets])
        except (ValueError, TypeError, KeyError, OSError) as exc:
            pending = str(exc) == "Link target is missing a locked identity/route"
            report.add("CONVERSION_IDENTITY_PENDING" if pending else "CONVERSION_REJECTED", str(exc),
                       severity="warning" if pending else "error", source=note.path)
    return report


def prepare(state, vault, policy, requested=None, repairs=None, legacy=None, svg=None, observed_at=None):
    if observed_at is None:
        observed_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    utc_time(observed_at)
    head, _, previous = current_release(state)
    target, revision, explicit, inferred, report, registry = derive_registry(state, vault, policy, requested or [], repairs, legacy, svg)
    snapshot, observations = full_snapshot(report, registry, previous, observed_at)
    release = {"registry.json": payload(target), "observations.json": payload(observations),
               **{"snapshot/" + n: raw for n, raw in snapshot.items()}}
    release["manifest.json"] = payload({"schema_version": 1, "kind": "private-publication-release",
                                       "files": {n: sha(raw) for n, raw in sorted(release.items())}})
    target_head = head_for("release-" + sha(release["manifest.json"])[:24], sha(release["registry.json"]), sha(snapshot["manifest.json"]))
    plan = {"schema_version": 1, "kind": "complete-local-publication-plan", "deployable": False,
            "vault_root": str(Path(vault).resolve()), "base_registry_revision": revision,
            "base_release": head["release"] if head else None, "observed_at": observed_at,
            "explicit_operations": explicit, "proposed_operations": inferred, "target_head": target_head,
            "source_inventory": source_inventory(report),
            "configuration_sha256": sha(payload({"policy": policy, "repairs": repairs or [], "legacy": legacy or {}, "svg": svg or {}})),
            "release_manifest_sha256": sha(release["manifest.json"]),
            "summary": {"pages": len(json.loads(snapshot["manifest.json"])["routes"]),
                        "assets": sum(n.startswith("assets/") for n in snapshot), "snapshot_bytes": sum(map(len, snapshot.values())),
                        "redirects": len(json.loads(snapshot["manifest.json"])["redirects"]),
                        "withdrawn_reservations": sum(r["status"] == "withdrawn" for r in observations["reservations"]),
                        "warnings": sum(i.severity == "warning" for i in report.issues)}}
    plan["plan_sha256"] = sha(payload(plan))
    # Recheck after conversion/resource copy: same source and dependency closure.
    again = checked(vault, policy, registry, repairs, legacy, svg)
    if source_inventory(again) != plan["source_inventory"]:
        raise ValueError("Source changed after full snapshot conversion")
    return plan, release


def check_plan(plan, expected):
    _keys(plan, {"schema_version", "kind", "deployable", "vault_root", "base_registry_revision", "base_release", "observed_at",
                 "explicit_operations", "proposed_operations", "target_head", "source_inventory", "configuration_sha256",
                 "release_manifest_sha256", "summary", "plan_sha256"})
    if plan["schema_version"] != 1 or plan["kind"] != "complete-local-publication-plan" or plan["deployable"] is not False:
        raise ValueError("Invalid complete local publication plan")
    if expected != plan["plan_sha256"] or sha(payload({k: v for k, v in plan.items() if k != "plan_sha256"})) != expected:
        raise ValueError("Publication plan does not match the reviewed digest")


def apply(state, vault, policy, plan, expected, repairs=None, legacy=None, svg=None, build_root=None):
    check_plan(plan, expected)
    state = safe_path(state)
    read_state(state, vault)  # Reject Vault overlap before any write/lock.
    with writer_lock(state):
        head, _, _ = current_release(state)
        registry, data, revision = read_state(state, vault)
        if plan["vault_root"] != str(Path(vault).resolve()):
            raise ValueError("Publication plan belongs to another Vault")
        if head == plan["target_head"]:
            return {"changed": False, "release": head["release"], "snapshot": str(state / "publication-releases" / head["release"] / "snapshot")}
        if revision != plan["base_registry_revision"] or (head["release"] if head else None) != plan["base_release"]:
            raise ValueError("Publication state changed; refuse stale plan or historical rollback")
        current_plan, release = prepare(state, vault, policy, plan["explicit_operations"], repairs, legacy, svg, plan["observed_at"])
        if current_plan != plan:
            raise ValueError("Source, configuration or publication plan changed; replan before applying")
        # Activate a bootstrap head with unchanged identity/no public snapshot
        # before staging. Orphan releases after a crash can then be distinguished
        # from a lost pointer, without guessing or rolling back publication.
        if head is None:
            atomic_write(safe_path(state / "publication-head.json"), payload(head_for(None, revision, None)).decode())
        revision_path = safe_path(state / "revisions" / (plan["target_head"]["registry_revision"] + ".json"))
        if revision_path.exists() and revision_path.read_bytes() != release["registry.json"]:
            raise ValueError("Immutable registry revision changed")
        if not revision_path.exists():
            atomic_write(revision_path, release["registry.json"].decode())
        destination = _install(release, safe_path(state / "publication-releases"), "release-")
        if destination.name != plan["target_head"]["release"]:
            raise ValueError("Staged release differs from reviewed plan")
        # Do not switch a complete publication to content that fails the actual
        # pinned renderer, resource/fragment checks or reproducibility check.
        from ci import build_snapshot
        runtime_check()
        root = safe_path(build_root or PROJECT / ".build/publication-validation")
        if within(Path(vault), root) or within(root, Path(vault)):
            raise ValueError("Publication build output must be disjoint from Vault")
        root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="run-", dir=root))
        build_snapshot(destination / "snapshot", local_hugo(), work)
        barrier, _ = prepare(state, vault, policy, plan["explicit_operations"], repairs, legacy, svg, plan["observed_at"])
        if barrier != plan:
            raise ValueError("Source changed before paired publication promotion; previous head preserved")
        atomic_write(safe_path(state / "publication-head.json"), payload(plan["target_head"]).decode())
        current_release(state)
        return {"changed": True, "release": destination.name, "snapshot": str(destination / "snapshot"), "verified_build": str(work)}
