#!/usr/bin/env python3
"""Build-only CI/local parity gate. No Vault, upload, deployment, or Git writes."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
sys.dont_write_bytecode = True

import yaml
from fixture import fixture_files
from publisher.safety import within
from publisher.snapshot import _tree, inspect_snapshot, materialize_snapshot, verify_snapshot_html
from publisher.storage import atomic_write
from publisher.tooling import PROJECT, local_hugo, runtime_check

SITE_FILES = {
    'site/about.md', 'site/hugo.toml', 'site/theme.lock.toml',
    'site/assets/img/avatar.jpg', 'site/assets/jsconfig.json',
    'site/assets/scss/custom.scss', 'site/assets/ts/custom.ts',
    'site/assets/vendor/photoswipe-5.4.4/LICENSE',
    'site/assets/vendor/photoswipe-5.4.4/photoswipe.css',
    'site/assets/vendor/photoswipe-5.4.4/photoswipe.esm.min.js',
    'site/assets/vendor/photoswipe-5.4.4/photoswipe-lightbox.esm.min.js',
    *{'site/layouts/' + n + '.html' for n in ('about', 'home', 'mocs', 'recent', 'all-notes')},
    *{'site/layouts/_markup/render-' + n + '.html' for n in
      ('blockquote', 'passthrough', 'image', 'heading', 'codeblock', 'codeblock-mermaid')},
    *{'site/layouts/_partials/' + n + '.html' for n in
      ('helper/paginator', 'widget/taxonomy', 'head/custom-font', 'footer/custom',
       'article/components/photoswipe', 'article-list/default', 'article-list/compact',
       'article-list/recent', 'article-list/tags')},
}


def allowed_repository_path(name):
    if name in SITE_FILES or name == '.github/workflows/hugo-pages.yml':
        return True
    if name in {'publication/current/release.json', 'publication/current/presentation.json',
                'publication/current/snapshot/manifest.json'}:
        return True
    if re.fullmatch(r'publication/current/snapshot/(?:pages/[a-f0-9-]{36}\.json|assets/[a-f0-9]{64}\.[a-z0-9]{1,12})', name):
        return True
    if name in {"preview.ps1", "docs/hugo-stack.md", "site/hugo.toml", "site/theme.lock.toml",
                "site/layouts/_markup/render-passthrough.html", "site/layouts/_markup/render-codeblock.html",
                "site/layouts/_markup/render-codeblock-mermaid.html",
                "site/layouts/_markup/render-blockquote.html",
                "site/layouts/home.html", "site/layouts/mocs.html", "site/layouts/all-notes.html",
                "site/layouts/_partials/article-list/compact.html",
                "site/layouts/_partials/helper/paginator.html",
                "site/layouts/_partials/widget/taxonomy.html",
                "site/layouts/_partials/head/custom-font.html",
                "site/layouts/_partials/article/components/photoswipe.html"}:
        return True
    if name in {".gitignore", ".gitattributes", "README.md", ".github/README.md", ".github/workflows/hugo-ci.yml", "docs/hugo-ci.md", "docs/hugo-publication.md", "publication/README.md"}:
        return True
    if name.startswith("pipeline/"):
        return (name != "pipeline/README.md" and not any(p.startswith(".") or p == "__pycache__" for p in name.split("/"))
                and name.endswith((".py", ".toml", ".json", ".html", ".in", ".lock")))
    if name.startswith("tests/"):
        return bool(re.fullmatch(r"tests/test_[a-z_]+\.py", name))
    return name.startswith("publication/fixture/") and name[len("publication/fixture/"):] in fixture_files()


def repository_check(project=PROJECT):
    project = Path(project)
    result = subprocess.run(["git", "-c", "safe.directory=" + str(project), "-C", str(project), "ls-files", "-z"],
                            capture_output=True, check=True, timeout=15)
    paths = result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []
    if not paths:
        raise ValueError("Repository has no tracked files; stage the explicit public allowlist first")
    unexpected = [name for name in paths if not allowed_repository_path(name)]
    if unexpected:
        raise ValueError("Tracked files exceed migration allowlist: " + ", ".join(unexpected))
    if ".github/workflows/hugo-ci.yml" not in paths or "publication/fixture/manifest.json" not in paths:
        raise ValueError("Build workflow/synthetic snapshot is not tracked")
    workflow = yaml.load((project / ".github/workflows/hugo-ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    if set(workflow["on"]) != {"pull_request", "push", "workflow_dispatch"} or workflow["permissions"] != {"contents": "read"}:
        raise ValueError("Workflow events/permissions differ from build-only policy")
    if set(workflow["jobs"]) != {"validate"}:
        raise ValueError("Unexpected CI job (deployment is not authorized)")
    job = workflow["jobs"]["validate"]
    if any(name in job for name in ("permissions", "environment", "secrets")):
        raise ValueError("CI must not elevate permissions or access a deployment environment")
    data = runtime_check(project)
    expected_actions = {"actions/checkout@" + data["actions"]["checkout"],
                        "actions/setup-python@" + data["actions"]["setup_python"]}
    actions = [step["uses"] for step in job["steps"] if "uses" in step]
    if len(actions) != 2 or set(actions) != expected_actions:
        raise ValueError("CI actions must match exact reviewed SHA pins; no upload/deploy actions")
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    python = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
    if checkout["with"]["persist-credentials"] != "false" or python["with"]["python-version"] != data["python"]:
        raise ValueError("CI checkout credentials/Python do not match policy")
    if '.github/workflows/hugo-pages.yml' in paths:
        production_workflow_check(project)
    if any(name.startswith('publication/current/') for name in paths):
        from production import inspect_release
        inspect_release(project)
    return len(paths)


def production_workflow_check(project=PROJECT):
    workflow = yaml.load((Path(project) / '.github/workflows/hugo-pages.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    data = runtime_check(project)
    if (set(workflow['on']) != {'push', 'workflow_dispatch'} or
            workflow['on']['push']['branches'] != ['main'] or
            workflow['permissions'] != {'contents': 'read'} or
            set(workflow['jobs']) != {'build', 'deploy'}):
        raise ValueError('Production workflow events/jobs/permissions differ from reviewed policy')
    build, deploy = workflow['jobs']['build'], workflow['jobs']['deploy']
    if any(k in build for k in ('permissions', 'environment', 'secrets')):
        raise ValueError('Production build must not acquire deployment privileges')
    if (deploy['permissions'] != {'contents': 'read', 'pages': 'write', 'id-token': 'write'} or
            deploy['needs'] != 'build' or deploy['if'] != "github.ref == 'refs/heads/main'" or
            deploy['environment']['name'] != 'github-pages'):
        raise ValueError('Deployment must use verified main artifact and github-pages environment')
    actual = [s['uses'] for job in (build, deploy) for s in job['steps'] if 'uses' in s]
    expected = ['actions/checkout@' + data['actions']['checkout'],
                'actions/setup-python@' + data['actions']['setup_python'],
                'actions/upload-pages-artifact@' + data['actions']['upload_pages'],
                'actions/deploy-pages@' + data['actions']['deploy_pages']]
    if actual != expected:
        raise ValueError('Production Actions differ from reviewed commit pins')
    if (build['steps'][0]['with']['persist-credentials'] != 'false' or
            build['steps'][1]['with']['python-version'] != data['python'] or
            not any(s.get('run') == 'python -B pipeline/production.py build --verify-repeat' for s in build['steps'])):
        raise ValueError('Production checkout/runtime/build differs from policy')


def run_tests():
    suite = unittest.defaultTestLoader.discover(str(PROJECT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    # The only platform-specific skip allowed in Linux CI is the Windows
    # junction test. Missing Hugo must never silently skip integration tests.
    bad_skips = [(str(test), reason) for test, reason in result.skipped if reason != "Windows junction safety test"]
    if not result.wasSuccessful() or bad_skips:
        raise ValueError("Unit/integration tests failed or required integration tests were skipped")
    return result.testsRun, len(result.skipped)


def build_snapshot(snapshot, hugo, work):
    """Fresh output per run; compare two builds and enforce full HTML closure."""
    generated, manifest = materialize_snapshot(snapshot, work / "generated")
    config = work / "hugo.generated.json"
    atomic_write(config, json.dumps({"staticDir": [str(generated / "static")]}) + "\n")
    outputs, verification = [], None
    for name in ("first", "repeat"):
        output = work / name / "public"
        output.mkdir(parents=True)
        subprocess.run([str(hugo), "--source", str(PROJECT / "pipeline/hugo-test"),
                        "--contentDir", str(generated / "content"), "--destination", str(output),
                        "--config", str(PROJECT / "pipeline/hugo-test/hugo.toml") + "," + str(config),
                        "--buildFuture",
                        "--cacheDir", str(work / "cache"), "--noBuildLock", "--noChmod", "--noTimes"],
                       check=True, timeout=45)
        verification = verify_snapshot_html(snapshot, generated, output)
        if not verification["passed"]:
            raise ValueError("Strict HTML validation failed: " + json.dumps(verification["errors"]))
        outputs.append(_tree(output))
    if outputs[0] != outputs[1]:
        raise ValueError("Repeated Hugo builds differ")
    return {"passed": True, "validation_only": True, "deployable": False,
            "built_files": len(outputs[0]), "reproducible": True, "html": verification}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=PROJECT / "publication/fixture")
    args = parser.parse_args()
    runtime_check()
    hugo = local_hugo()
    if not hugo.is_file():
        raise ValueError("Pinned local Hugo missing; run pipeline/install_hugo.py first")
    output = subprocess.run([str(hugo), "version"], capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
    if not re.search(r"\bhugo v" + re.escape(runtime_check()["hugo"]) + r"(?:-|\s)", output.stdout):
        raise ValueError("Hugo executable version differs from lock")
    tracked = repository_check()
    snapshot = args.snapshot.absolute()
    if not within(PROJECT / "publication", snapshot) and not within(PROJECT / ".generated/snapshots", snapshot):
        raise ValueError("CI snapshot must be project-publication or isolated generated snapshot")
    inspect_snapshot(snapshot)
    # A checked-in synthetic fixture must remain exactly reproducible from code.
    if _tree(PROJECT / "publication/fixture") != fixture_files():
        raise ValueError("Synthetic fixture differs from its generator")
    tests, skipped = run_tests()
    root = PROJECT / ".build/ci"
    for path in (root, *root.parents):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("CI output must not follow symlinks/junctions")
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    result = build_snapshot(snapshot, hugo, work)
    result.update({"tests": tests, "platform_specific_skips": skipped, "tracked_public_files": tracked})
    report = PROJECT / ".local/reports/publisher/ci.json"
    for path in report.parents:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("CI report must not follow symlinks/junctions")
    atomic_write(report, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Build-only local output (not uploaded): " + str(work))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print("CI_FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
