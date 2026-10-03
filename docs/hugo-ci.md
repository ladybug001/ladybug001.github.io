# Build-only Hugo CI

This repository is a migration pipeline, not a deployed Hugo site. No theme,
navigation, home page or visual layout has been chosen. Its existing minimal
HTML harness is only a content/link test.

## Tracked sources and safety

The Git root uses a fail-closed allowlist. It tracks pipeline code, tests,
dependency/toolchain locks, this document, one build-only workflow, and a
synthetic snapshot. Quartz has been retired; its files and old Pages workflow
have been removed from the active tree to a private local recovery archive.
Local reports and machine-specific operating documents,
private identity state, real generated snapshots and build output are ignored.

The repository gate verifies the **tracked Git index**, not every file on the
local disk. It refuses paths outside the reviewed allowlist. `.gitignore` is
not a substitute for this check: an explicitly force-added file must still fail.
The current allowlist admits only the generated synthetic fixture; a real note
snapshot requires a separate reviewed change and content/privacy approval.

## Reproducible validation

`pipeline/toolchain.toml` pins Python 3.14.4, publisher 0.6.0 and standard Hugo
0.167.0. `pipeline/requirements.lock` pins all four Python distributions and
accepted wheel hashes. There is no theme, Node dependency or global install.

Hugo archives use platform-specific pinned SHA-256 values from the upstream
release checksums. The installer verifies the archive before reading only the
regular Hugo binary; it never extracts arbitrary archive paths. It installs
under project `.local/tools/hugo/`, refuses a conflicting existing binary and
checks the executable's reported version. Download/cache bytes are bounded.

The Actions workflow uses exact reviewed commit SHA pins, read-only contents
permission and non-persisted checkout credentials. It runs on `ubuntu-24.04`
and `windows-2022`, for pull requests, main pushes and manual dispatch. It does
not use `pull_request_target`, credentials/secrets, a deploy environment,
artifact upload, Pages tokens, a deployment job or a site theme.

The labels pin runner families, not immutable OS image digests; GitHub may
update hosted images. Python/Hugo/dependencies are pinned, and output is tested
twice **within each job**. Cross-platform hashes are not compared automatically
between jobs; that remains a separate future reproducibility check.

Validated chain:

1. Verify runtime, tracked repository files and workflow policy.
2. Verify the synthetic fixture matches its deterministic generator.
3. Run converter and snapshot unit/integration tests. Missing Hugo must fail,
   not quietly skip required integration tests. Linux may skip only the
   explicitly Windows-specific junction test.
4. Materialize Hugo bundles from the snapshot alone, without a Vault or private
   registry, into a fresh generated directory.
5. Build twice to separate new output directories, strictly verify all pages,
   anchors, resources, hashes, graph links and output file inventory.
6. Require byte-identical repeat builds. Save local output/report only.

Input validation failures or build errors exit nonzero. Each run has a new
`.build/ci/run-*/` directory; failed output never replaces a successful release.
Nothing is recursively cleaned or published.

## Local commands

In the repository root, use an isolated environment with the exact pinned
Python version. Example commands below use that environment's interpreter:

```text
python -m pip install --require-hashes --only-binary=:all: -r pipeline/requirements.lock
python -B pipeline/install_hugo.py
python -B pipeline/fixture.py
python -B pipeline/ci.py --snapshot publication/fixture
```

The repository allowlist must already be staged or committed for the gate to
inspect it. Do not add old Quartz, real snapshots or private state. Fixture
generation is deterministic and refuses changed existing files; fixture JSON
is generated, not manually maintained.

The local report is `.local/reports/publisher/ci.json`; complete output stays
in `.build/ci/run-*/`. Neither is uploaded by this workflow. It exercises two
synthetic pages, not the complete public note set.

## Status and next authority boundary

The workflow and Windows local parity gate have been prepared and tested.
No remote push or GitHub run is implied by local success. Linux execution and
hosted-runner setup remain unverified until an explicitly approved first push
triggers GitHub Actions. A test pass is not authorization to publish real notes
or switch GitHub Pages.

The first upload has been authorized for technical code and synthetic data only.
Before any push, recheck remote history and confirm the upload scope. Do not
force push or overwrite an existing repository.
Before real content export or deployment, implement/review publication-state
transactions and obtain content approval; enable one production workflow only
after a separate Pages authorization.

Upstream references: [checkout v6.0.2](https://github.com/actions/checkout/releases/tag/v6.0.2),
[setup-python v6.2.0](https://github.com/actions/setup-python/releases/tag/v6.2.0),
[Python version manifest](https://github.com/actions/python-versions/blob/main/versions-manifest.json),
[Hugo v0.167.0](https://github.com/gohugoio/hugo/releases/tag/v0.167.0).
