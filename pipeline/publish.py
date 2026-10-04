#!/usr/bin/env python3
"""Read-only Vault inspection, private identities and isolated Hugo sample export."""
from __future__ import annotations

import sys

# In particular, never place Python bytecode in a user-supplied Vault path.
sys.dont_write_bytecode = True

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from publisher.checker import check, load_policy
from publisher.compatibility import legacy_targets, svg_rules, validate_overrides
from publisher.identity import Registry, initialize_private, load_private
from publisher.safety import install_vault_write_guard, within
from publisher.storage import atomic_write
from publisher.hugo import export_sample
from publisher.htmlcheck import verify_html
from publisher.snapshot import pack_snapshot, inspect_snapshot, materialize_snapshot, verify_snapshot_html, SnapshotError
from publisher.lifecycle import make_plan, apply_plan, safe_path
from publisher.publication import prepare as prepare_publication, apply as apply_publication, conversion_review
from publisher.tooling import local_hugo, runtime_check


PROJECT = Path(__file__).resolve().parent.parent
REPORT_ROOT = PROJECT / ".local" / "reports"
STATE_ROOT = PROJECT / ".local" / "state" / "publisher"


def snapshot_input(path):
    absolute = safe_path(path)
    permitted = (absolute in {PROJECT / "publication/fixture", PROJECT / "publication/snapshot"}
                 or (within(PROJECT / ".generated/snapshots", absolute) and absolute.name.startswith("snapshot-"))
                 or (within(STATE_ROOT / "publication-releases", absolute) and absolute.name == "snapshot"
                     and absolute.parent.name.startswith("release-")))
    if not within(PROJECT, absolute) or not permitted:
        raise ValueError("Snapshot must be a completed project publication/fixture or isolated snapshot release")
    return absolute


def snapshot_report(name: str, result: dict) -> None:
    path = REPORT_ROOT / "publisher" / (name + ".json")
    if not within(PROJECT, path) or not within(REPORT_ROOT, path):
        raise ValueError("Unsafe snapshot report output")
    for parent in path.parents:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("Snapshot reports must not use symlinks/junctions")
    atomic_write(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def summary_text(data: dict) -> str:
    summary = data["summary"]
    lines = [
        "Obsidian publication: read-only check",
        "LOCAL PRIVATE REPORT — do not upload to the website or a public repository.",
        f"Vault: {data['vault']}",
        f"Passed: {data['passed']} | Source read consistency: {data['source_read_verified']}",
        f"Indexed {summary['indexed_notes']} | Scoped {summary['scoped_notes']} | Published {summary['published_notes']}",
        f"References {summary['references']} | Assets {summary['referenced_assets']} | Bytes {summary['attachment_bytes']}",
        f"Errors {summary['errors']} | Warnings {summary['warnings']}",
        f"Non-clickable unavailable-note references: {summary['nonclickable_references']}",
        "", "Issue counts:",
    ]
    lines.extend(f"  {code}: {count}" for code, count in summary["issue_codes"].items())
    lines.extend(["", "Details:"])
    for issue in data["issues"]:
        source = issue["source"]
        if issue["line"] is not None:
            source += f":{issue['line']}:{issue['column'] or 1}"
        lines.append(f"[{issue['severity']}] {issue['code']} {source} :: {issue['message']}")
        if issue["target"]:
            lines.append(f"  target: {issue['target']}")
        if issue["candidates"]:
            lines.append("  candidates: " + "; ".join(issue["candidates"]))
    lines.extend(["", "Limitations:"] + ["- " + value for value in data["limitations"]])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify_command = commands.add_parser("verify-sample", help="Verify already built isolated sample HTML")
    verify_command.add_argument("--sample", type=Path, required=True)
    pack_command = commands.add_parser("pack-snapshot", help="Pack strict, portable local snapshot from existing sample; no Vault access")
    pack_command.add_argument("--sample", type=Path, required=True)
    pack_command.add_argument("--id", action="append", help="Optional explicit public sample UUID subset; no dependency expansion")
    for name in ("check-snapshot", "materialize-snapshot", "verify-snapshot", "build"):
        command = commands.add_parser(name, help="Validate/build isolated snapshot without Vault or private state")
        command.add_argument("--snapshot", type=Path, required=True)
    for name, help_text in (("check", "Inspect Vault without modifying source or allocating IDs"),
                            ("identity-init", "Reserve stable IDs/URLs in local private state; no content export"),
                            ("identity-plan", "Plan explicit private identity changes; never modify Vault or current state"),
                            ("identity-apply", "Apply one digest-confirmed private identity plan; no export/deployment"),
                            ("publication-plan", "Plan full opt-in snapshot and paired private state; never publish or edit Vault"),
                            ("publication-apply", "Apply a reviewed complete local release; no push or deployment"),
                            ("export-sample", "Convert 1–12 explicit notes to isolated, non-deployable Hugo bundles")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--vault", type=Path, required=True)
        command.add_argument("--policy", type=Path, default=PROJECT / "pipeline" / "policy.toml")
        command.add_argument("--identity-registry", type=Path, help="Optional read-only private identity registry")
        command.add_argument("--overrides", type=Path, help="Optional read-only hash-bound compatibility rules")
        command.add_argument("--state-dir", type=Path, default=STATE_ROOT)
        command.add_argument("--no-local-state", action="store_true", help="Do not auto-load project registry/overrides")
        command.add_argument("--report-dir", type=Path, default=REPORT_ROOT / "publisher")
        command.add_argument("--no-report", action="store_true", help="Do not write diagnostic reports")
        if name == "export-sample":
            command.add_argument("--note", action="append", required=True, help="Exact Vault-relative path; repeat for each sample")
        if name == "check":
            command.add_argument("--conversion", action="store_true", help="Also attempt every opt-in conversion in memory; no snapshot/state writes")
        if name == "publication-plan":
            command.add_argument("--operations-file", type=Path, help="Optional explicit move/route operations in private reports")
        if name == "publication-plan":
            command.add_argument("--observed-at", help="Optional fixed UTC observation time")
        if name == "identity-plan":
            operation = command.add_mutually_exclusive_group(required=True)
            operation.add_argument("--operation", choices=("move", "withdraw", "restore", "route", "register"))
            operation.add_argument("--operations-file", type=Path, help="Private JSON list of explicit operations for a batch")
            command.add_argument("--id", help="Immutable note UUID; unused UUID for register")
            command.add_argument("--new-path", help="Exact new Vault-relative path for an already moved note")
            command.add_argument("--slug", help="New URL segment for explicit route migration")
            command.add_argument("--allow-missing-source", action="store_true", help="Explicit withdrawal of a deleted source")
        if name in {"identity-apply", "publication-apply"}:
            command.add_argument("--plan", type=Path, required=True, help="Previously reviewed private plan inside project .local/reports")
            command.add_argument("--expect-plan", required=True, help="Exact SHA-256 digest confirmed after reviewing the plan")
    args = parser.parse_args(argv)
    try:
        if args.command in {"pack-snapshot", "check-snapshot", "materialize-snapshot", "verify-snapshot", "build"}:
            # These commands have no --vault and do not load identity/overrides.
            def isolated(path, root, prefix):
                absolute = path.absolute()
                if not within(root, absolute) or not absolute.name.startswith(prefix):
                    raise ValueError("Expected an isolated project release under " + str(root))
                return absolute
            snapshot_root = PROJECT / ".generated" / "snapshots"
            hugo_root = PROJECT / ".generated" / "hugo-from-snapshot"
            if args.command == "pack-snapshot":
                sample = isolated(args.sample, PROJECT / ".generated" / "hugo-samples", "sample-")
                destination, manifest = pack_snapshot(sample, snapshot_root, args.id)
                result = {"passed": True, "snapshot": str(destination), "pages": len(manifest["routes"]),
                          "validation_only": True, "deployable": False, "outside_sample_exemptions": 0}
            else:
                snapshot = snapshot_input(args.snapshot)
                _, manifest, pages = inspect_snapshot(snapshot)
                result = {"passed": True, "pages": len(pages), "validation_only": True,
                          "deployable": False, "outside_sample_exemptions": 0}
                if args.command in {"materialize-snapshot", "verify-snapshot"}:
                    destination, _ = materialize_snapshot(snapshot, hugo_root)
                    result["hugo_input"] = str(destination)
                    if args.command == "verify-snapshot":
                        public = PROJECT / ".build" / "hugo-from-snapshot" / destination.name / "public"
                        if not within(PROJECT, public):
                            raise ValueError("Unsafe snapshot build output")
                        result = verify_snapshot_html(snapshot, destination, public)
                if args.command == "build":
                    from ci import build_snapshot
                    lock = runtime_check()
                    hugo = local_hugo()
                    version = subprocess.run([str(hugo), "version"], capture_output=True, text=True, timeout=15, check=True).stdout
                    if not version.startswith("hugo v" + lock["hugo"] + "-"):
                        raise ValueError("Hugo binary differs from the pinned version")
                    root = safe_path(PROJECT / ".build/publication")
                    root.mkdir(parents=True, exist_ok=True)
                    work = Path(tempfile.mkdtemp(prefix="run-", dir=root))
                    result = build_snapshot(snapshot, hugo, work)
                    result["build_root"] = str(work)
                    print("Technical HTML harness only; no selected Theme or production deployment.")
            snapshot_report(args.command, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        if args.command == "verify-sample":
            sample = args.sample.resolve()
            generated_root = PROJECT / ".generated" / "hugo-samples"
            if not within(PROJECT, sample) or not within(generated_root, sample) or not sample.name.startswith("sample-"):
                raise ValueError("Sample must be inside project .generated/hugo-samples")
            manifest = json.loads((sample / "manifest.json").read_text(encoding="utf-8"))
            public = PROJECT / ".build" / "hugo-samples" / sample.name / "public"
            if not within(PROJECT, public):
                raise ValueError("Unsafe sample build root")
            result = verify_html(public, manifest)
            report_path = REPORT_ROOT / "publisher" / "sample-html.json"
            if not within(PROJECT, report_path):
                raise ValueError("Unsafe sample report root")
            atomic_write(report_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"Private HTML report: {report_path}")
            return 0 if result["passed"] else 1
        vault = args.vault.resolve()
        install_vault_write_guard(vault)
        state_dir = safe_path(args.state_dir)
        if not within(PROJECT, state_dir) or not within(STATE_ROOT, state_dir) or within(vault, state_dir):
            raise ValueError("State directory must be inside project .local/state/publisher, and outside Vault")
        report_dir = safe_path(args.report_dir)
        if not args.no_report:
            if not within(PROJECT, report_dir) or not within(REPORT_ROOT, report_dir) or within(vault, report_dir):
                raise ValueError("Report directory must be inside project .local/reports, and outside Vault")
        policy = load_policy(args.policy)
        registry = Registry()
        registry_path = args.identity_registry
        if registry_path is None and not args.no_local_state and ((state_dir / "identity-registry.json").exists() or (state_dir / "publication-head.json").exists()):
            registry_path = state_dir / "identity-registry.json"
        if registry_path:
            registry = load_private(registry_path, vault)
        overrides_path = args.overrides
        if overrides_path is None and not args.no_local_state and (state_dir / "overrides.json").exists():
            overrides_path = state_dir / "overrides.json"
        compatibility_data = json.loads(overrides_path.read_text(encoding="utf-8-sig")) if overrides_path else {"schema_version": 1, "repairs": []}
        repairs = validate_overrides(compatibility_data)
        if args.command in {"publication-plan", "publication-apply"}:
            if args.identity_registry or args.no_local_state:
                raise ValueError("Paired publication must use its own project private state")
            legacy, svg = legacy_targets(compatibility_data), svg_rules(compatibility_data)
            if args.command == "publication-plan":
                if args.no_report:
                    raise ValueError("Publication planning requires a private review report")
                requested = []
                if args.operations_file:
                    operation_path = safe_path(args.operations_file)
                    if not within(REPORT_ROOT, operation_path) or within(vault, operation_path):
                        raise ValueError("Explicit operations must be in project .local/reports")
                    requested = json.loads(operation_path.read_text(encoding="utf-8-sig"))
                plan, _ = prepare_publication(state_dir, vault, policy, requested, repairs, legacy, svg, args.observed_at)
                plan_path = safe_path(report_dir / "publication-plan.json")
                atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
                print(f"Private publication plan: {plan_path}")
                print(f"Reviewed digest required: {plan['plan_sha256']}")
                print(json.dumps(plan["summary"], indent=2))
                print("Full local validation only: no state change, content upload or deployment.")
            else:
                plan_path = safe_path(args.plan)
                if not within(REPORT_ROOT, plan_path) or within(vault, plan_path):
                    raise ValueError("Reviewed publication plan must be in project .local/reports")
                plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
                result = apply_publication(state_dir, vault, policy, plan, args.expect_plan, repairs, legacy, svg)
                print(json.dumps(result, indent=2))
                print("Coordinated private identity/snapshot head updated locally only; no source writes or deployment."
                      if result["changed"] else "Reviewed release is already current; no repeated publication update, source writes or deployment.")
            return 0
        if args.command in {"identity-plan", "identity-apply"}:
            if args.identity_registry or args.no_local_state:
                raise ValueError("Identity transactions must use their own project private state")
            legacy, svg = legacy_targets(compatibility_data), svg_rules(compatibility_data)
            if args.command == "identity-plan":
                if args.no_report:
                    raise ValueError("Identity planning needs a private report for review; do not use --no-report")
                if args.operations_file:
                    if args.id or args.new_path or args.slug or args.allow_missing_source:
                        raise ValueError("Do not mix batch operations with individual operation fields")
                    input_path = safe_path(args.operations_file)
                    if not within(REPORT_ROOT, input_path) or within(vault, input_path):
                        raise ValueError("Operations input must be inside project .local/reports and outside Vault")
                    requested = json.loads(input_path.read_text(encoding="utf-8-sig"))
                else:
                    requested = [{"kind": args.operation, "id": args.id}]
                    if args.new_path:
                        requested[0]["new_path"] = args.new_path
                    if args.slug:
                        requested[0]["slug"] = args.slug
                    if args.allow_missing_source:
                        requested[0]["allow_missing_source"] = True
                plan = make_plan(state_dir, vault, policy, requested, repairs, legacy, svg)
                plan_path = safe_path(report_dir / "identity-plan.json")
                atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                print(f"Private plan (review before applying): {plan_path}")
                print(f"Plan SHA-256: {plan['plan_sha256']} | Operations: {len(plan['operations'])}")
                print("Current identity state and Vault unchanged; no content export or deployment.")
            else:
                plan_path = safe_path(args.plan)
                if not within(REPORT_ROOT, plan_path) or within(vault, plan_path):
                    raise ValueError("Reviewed identity plan must be inside project .local/reports and outside Vault")
                plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
                result = apply_plan(state_dir, vault, policy, plan, args.expect_plan, repairs, legacy, svg)
                print(json.dumps(result, indent=2))
                print("Only private identity state updated. No Vault writes, content export, push or deployment.")
            return 0
        report = check(vault, policy, registry, repairs, legacy_targets(compatibility_data), svg_rules(compatibility_data))
        if args.command == "check" and args.conversion:
            conversion_review(report)
            barrier = check(vault, policy, registry, repairs, legacy_targets(compatibility_data), svg_rules(compatibility_data))
            from publisher.lifecycle import source_inventory
            if not barrier.read_verified or source_inventory(report) != source_inventory(barrier):
                report.add("SOURCE_CHANGED_DURING_CONVERSION", "Source or attachment changed during read-only conversion validation")
        data = report.to_dict()
        if not args.no_report:
            atomic_write(report_dir / "check.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            atomic_write(report_dir / "check.txt", summary_text(data))
        print(f"Published: {data['summary']['published_notes']} | Errors: {data['summary']['errors']}"
              f" | Warnings: {data['summary']['warnings']} | Source consistency: {report.read_verified}")
        print(json.dumps(data["summary"]["issue_codes"], ensure_ascii=False, sort_keys=True))
        if not args.no_report:
            print(f"Private reports: {report_dir}")
        if args.command == "identity-init" and not report.failed:
            if args.identity_registry or args.no_local_state:
                raise ValueError("identity-init must use its own local state; external registry/no-local-state is check-only")
            initialized, created = initialize_private(report, state_dir)
            print(f"Identity state {'created' if created else 'unchanged'}: {len(initialized.bindings)} reserved routes")
            print(f"Private state (back up; never publish): {state_dir}")
        if args.command == "export-sample" and not report.failed:
            output = PROJECT / ".generated" / "hugo-samples"
            if not within(PROJECT, output) or within(vault, output):
                raise ValueError("Unsafe generated output root")
            destination, manifest = export_sample(report, args.note, output)
            print(f"Isolated sample: {destination}")
            print(f"Pages: {len(manifest['routes'])} | Outside-sample links: {len(manifest['outside_sample_urls'])}")
            print("NOT DEPLOYABLE. Outside-sample pages are not generated or HTML-verified.")
        return 1 if report.failed else 0
    except SnapshotError as exc:
        result = {"passed": False, "errors": exc.errors, "validation_only": True, "deployable": False}
        try:
            snapshot_report("snapshot-rejected", result)
        except (OSError, ValueError) as report_exc:
            print(f"Could not safely write local snapshot report: {report_exc}", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"CHECK_SETUP_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
