#!/usr/bin/env python3
"""Read-only technical-only push preflight; never pushes or changes Pages."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
from ci import allowed_repository_path
from publisher.tooling import PROJECT

WORKFLOW = ".github/workflows/hugo-ci.yml"
LEGACY_WORKFLOW = "dynamic/pages/pages-build-deployment"
# Reviewed build-only workflow. Any change requires review, not just a new hash.
WORKFLOW_SHA256 = "be549819ca019918ec7d5f3e9947c4627506ed9cb2e4788e99d2c1bd71623988"
ACTIVE_STATUSES = ("queued", "in_progress", "requested", "waiting", "pending")


def git(project, *arguments):
    result = subprocess.run(["git", "-c", "safe.directory=" + str(project),
                             "-C", str(project), *arguments],
                            capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError("Read-only Git check failed; inspect repository state locally")
    return result.stdout


def local_revision(project, repository):
    """Check the committed HEAD being considered, not uncommitted/staged files."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository) or repository.split("/")[-1] in {".", ".."}:
        raise ValueError("Expected repository must be owner/name")
    if git(project, "symbolic-ref", "--short", "HEAD").decode().strip() != "main":
        raise ValueError("Technical push preflight supports main only")
    urls = git(project, "remote", "get-url", "--push", "--all", "origin").decode().splitlines()
    accepted = {f"https://github.com/{repository}", f"https://github.com/{repository}.git",
                f"git@github.com:{repository}.git", f"ssh://git@github.com/{repository}.git"}
    if len(urls) != 1 or urls[0] not in accepted:
        raise ValueError("origin push destination differs from the expected GitHub repository")
    revision = git(project, "rev-parse", "--verify", "HEAD").decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Unexpected commit identity")
    paths = git(project, "ls-tree", "-r", "--name-only", "-z", revision).decode().rstrip("\0").split("\0")
    if not paths or any(not allowed_repository_path(path) for path in paths):
        raise ValueError("Committed HEAD exceeds the technical/synthetic allowlist")
    if WORKFLOW not in paths or "publication/fixture/manifest.json" not in paths:
        raise ValueError("Committed HEAD lacks the reviewed workflow or synthetic fixture")
    workflow = git(project, "show", revision + ":" + WORKFLOW)
    if hashlib.sha256(workflow).hexdigest() != WORKFLOW_SHA256:
        raise ValueError("Committed workflow differs from the reviewed build-only workflow")
    return revision


def parse_response(output, returncode):
    """Only an actual Pages HTTP 404 may mean absence; errors fail closed."""
    match = re.fullmatch(r"HTTP/\S+ (\d{3})[^\n]*\n(?:[^\n]+\n)*\n(.*)",
                         output.replace("\r\n", "\n"), re.DOTALL)
    if not match:
        raise ValueError("GitHub response status could not be verified")
    status = int(match[1])
    if status not in {200, 404} or (status == 200 and returncode):
        raise ValueError(f"GitHub read-only request failed (HTTP {status})")
    try:
        data = json.loads(match[2])
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("GitHub returned an unexpected response shape")
    return status, data


def github_get(executable, endpoint):
    # No token handling, settings writes, upload, deploy, or automatic login.
    result = subprocess.run([str(executable), "api", "--hostname", "github.com",
                             "--method", "GET", "--include", endpoint,
                             "-H", "Accept: application/vnd.github+json",
                             "-H", "X-GitHub-Api-Version: 2022-11-28"],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    # Never print subprocess stderr or response headers/metadata (may be private).
    return parse_response(result.stdout, result.returncode)


def require_ok(api, endpoint):
    status, data = api(endpoint)
    if status != 200:
        raise ValueError("A required GitHub resource was not readable")
    return data


def complete_list(data, key):
    items = data.get(key)
    count = data.get("total_count")
    if type(count) is not int or not isinstance(items, list) or count != len(items) or count > 100:
        raise ValueError("GitHub list is incomplete or too large; no partial preflight is accepted")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("Invalid GitHub list entry")
    return items


def remote_policy(repository, api):
    root = "repos/" + repository
    metadata = require_ok(api, root)
    if (metadata.get("full_name") != repository or metadata.get("default_branch") != "main"
            or metadata.get("permissions", {}).get("admin") is not True):
        raise ValueError("Repository identity/default branch/admin visibility could not be verified")
    status, pages = api(root + "/pages")
    if status == 404:
        if metadata.get("has_pages") is not False:
            raise ValueError("Pages absence is inconsistent with repository metadata")
        mode = "absent"
    elif status == 200 and pages.get("build_type") == "workflow":
        mode = "workflow"
    else:
        raise ValueError("Pages branch publishing or unknown settings can publish on push; stop")
    workflows = complete_list(require_ok(api, root + "/actions/workflows?per_page=100"), "workflows")
    approved = False
    for workflow in workflows:
        path, state = workflow.get("path"), workflow.get("state")
        if not isinstance(path, str) or not isinstance(state, str):
            raise ValueError("Workflow metadata is incomplete")
        # The historical dynamic workflow remains active in the API even after
        # switching Pages to workflow. It is not proof of a new deployment.
        if path == LEGACY_WORKFLOW:
            continue
        if path != WORKFLOW or state != "active" or approved:
            raise ValueError("Unreviewed or unexpected remote workflow; inspect before pushing")
        approved = True
    if not approved:
        raise ValueError("Reviewed remote build-only workflow is not active")
    content = require_ok(api, root + "/contents/" + WORKFLOW + "?ref=main")
    if content.get("encoding") != "base64" or content.get("path") != WORKFLOW:
        raise ValueError("Remote workflow content is unavailable")
    try:
        raw = base64.b64decode("".join(content["content"].split()), validate=True)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid remote workflow content") from exc
    if hashlib.sha256(raw).hexdigest() != WORKFLOW_SHA256:
        raise ValueError("Remote workflow differs from the reviewed build-only workflow")
    for state in ACTIVE_STATUSES:
        runs = complete_list(require_ok(api, root + f"/actions/runs?status={state}&per_page=100"), "workflow_runs")
        for run in runs:
            if run.get("path") != WORKFLOW or run.get("status") != state:
                raise ValueError("Pending/active unreviewed workflow could deploy; stop")
    return {"pages_mode": mode,
            "existing_site_offline": "not_established_by_this_check",
            "remote_workflow": "reviewed_build_only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Explicit expected owner/name")
    parser.add_argument("--gh", default="gh", help="Existing authenticated GitHub CLI executable")
    args = parser.parse_args()
    revision = local_revision(PROJECT, args.repo)
    result = remote_policy(args.repo, lambda endpoint: github_get(args.gh, endpoint))
    if git(PROJECT, "rev-parse", "HEAD").decode().strip() != revision:
        raise ValueError("HEAD changed during preflight; retry")
    result.update({"passed": True, "scope": "technical_and_synthetic_only",
                   "repository": args.repo, "checked_commit": revision})
    print(json.dumps(result, indent=2))
    print("Read-only observation, not a push/deployment authorization or an automatic Git hook.")
    print("Recheck immediately before a normal push of this exact commit; settings may change afterward.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        print("PREPUSH_FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
