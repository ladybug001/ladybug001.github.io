# Build-only Hugo CI

The build-only workflow remains an independent converter test. A separate
`hugo-pages.yml` deploys approved `publication/current/` on main pushes or manual
dispatch, after pinned Stack/Hugo builds and strict HTML validation.
Stack was selected on 2026-10-04; see
[preview instructions](hugo-stack.md). The existing minimal HTML harness remains
an independent content/link test, not the selected site's renderer.

## Tracked sources and safety

The Git root uses a fail-closed allowlist. It tracks pipeline code, tests,
dependency/toolchain locks, this document, one build-only workflow, and a
synthetic snapshot. Quartz has been retired; its files and old Pages workflow
have been removed. Its obsolete local recovery archive was also deleted during cleanup.
Local reports and machine-specific operating documents,
private identity state, real generated snapshots and build output are ignored.

The repository gate verifies the **tracked Git index**, not every file on the
local disk. It refuses paths outside the reviewed allowlist. `.gitignore` is
not a substitute for this check: an explicitly force-added file must still fail.
The reviewed allowlist also admits exact site source files and the generated
public hand-off. Snapshot/presentation hashes and exact inventories are validated;
private state, reports, Vault paths and local output remain excluded.

## Reproducible validation

`pipeline/toolchain.toml` pins Python 3.14.4, publisher 0.6.0 and Hugo Extended
0.167.0. `pipeline/requirements.lock` pins all four Python distributions and
accepted wheel hashes. Tests share Stack's Extended Hugo executable; no Node
dependency or global installation is needed.

Hugo archives use platform-specific pinned SHA-256 values from the upstream
release checksums. The installer verifies the archive before reading only the
regular Hugo binary; it never extracts arbitrary archive paths. It installs
under project `.local/tools/hugo-extended/`, refuses a conflicting existing binary and
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

## Read-only pre-push check

Before a technical/synthetic-only push, first review and commit the intended
allowlisted changes locally, then run:

```text
python -B pipeline/prepush.py --repo ladybug001/ladybug001.github.io
```

Use `--gh <existing-cli-path>` if the already authenticated GitHub CLI is not on
PATH. This separate network check does not belong in the Vault converter or
CI. It uses GitHub **GET** requests only; it does not log in, handle tokens,
change Pages settings, upload anything, push, or read the Vault. A configured
process-local proxy may be needed on this machine; no global proxy is changed.

It checks the main branch, exact origin push destination, committed HEAD file
allowlist, and the exact reviewed build-only workflow, both in HEAD and on the
remote main branch. Changes to that workflow require a new explicit review.
It requires administrator visibility of Pages settings, rejects legacy/unknown
Pages publication modes and other remote workflows, and blocks any queued,
running, requested, waiting or pending unreviewed workflow. Incomplete API lists,
unreadable responses or authentication/network failures fail closed. The old
historical dynamic Pages workflow may remain listed even in workflow mode;
pending runs of it still block the check.

The result covers the reported **committed HEAD**, not staged/uncommitted files
or another ref. It is an explicit command, not an installed Git hook: direct
pushes do not automatically run it. It is not an upload/content authorization,
nor proof that an existing site is offline. Settings can change after any check;
rerun immediately before pushing this exact revision. Full build/content review
is still required. Real snapshots and production deployment remain out of scope.

## Status and next authority boundary

### Local private identity lifecycle (not production publication)

`publish.py identity-plan` supports explicit `move`, `withdraw`, `restore`,
`route` and `register` operations. It writes a private review plan,
not the Vault or current registry. `identity-apply` requires that plan and its
exact reviewed `--expect-plan` SHA-256. Example interfaces:

```text
python -B pipeline/publish.py identity-plan --vault <vault> --operation move --id <existing-uuid> --new-path <exact-moved-note-path>
python -B pipeline/publish.py identity-apply --vault <vault> --plan <private-plan-path> --expect-plan <reviewed-plan-sha256>
```

An explicit batch may use `--operations-file` with a JSON list in project
`.local/reports/`. Plans, identity revisions and transaction journals are private
and must never be committed. A move requires the old source to be absent and an
exact new valid in-scope source, not a basename/content-hash guess. Withdrawal
requires opting out in the source first (or explicit missing-source approval).
Restoring publication is a separate operation. Route changes reserve the old
path permanently; withdrawn paths are never reassigned. A source Frontmatter
slug conflicting with the new locked slug fails validation rather than being
silently ignored or edited.

Applying rechecks the candidate registry and source/attachment inventory,
configuration, plan digest and current immutable revision. It stages an immutable
revision and journal before atomically promoting the single current registry.
A failure before promotion preserves last-good state; a failure after promotion
supports receipt recovery via the same exact plan without applying it twice.
Stale plans, tampering, route collisions and symlink/junction paths fail closed.
Actual process crashes leave a lock for manual investigation, not automatic
unlocking or rollback. Concurrent Vault saves are detected at read barriers;
the Vault is not locked, and its state may change after the final read.

Identity-only transactions do not generate snapshots or redirects. Once paired
publication starts, they are deliberately refused: use `publication-plan` and
`publication-apply` so identity and content cannot diverge. These newer commands
build a complete opt-in `portable-2` snapshot (including zero pages), stage the
private identity registry and public snapshot in one immutable release, build
twice with the pinned Hugo and strictly validate output, then atomically switch
one authoritative head. New registrations, withdrawals and restorations are
visible in the digest-reviewed plan; moves remain explicit. A replay cannot
silently export later edits. Active historical routes get verified client-side
redirects preserving fragments; withdrawn routes stay reserved but do not
redirect. Old local releases/history are not erased. No push/deployment is part
of this transaction. See [full operating contract](hugo-publication.md).

Tests use synthetic temporary Vaults. A complete real opt-in release has now
passed local paired apply and actual repeat builds; its snapshot/private state
remain ignored and were not uploaded. On 2026-10-04 the user authorized pushing
this next technical/synthetic-only revision for hosted validation. At preparation
time, the historical hosted CI runs below do not validate the new changes;
the new revision must pass its own Windows/Linux Actions run.

Latest local verification (2026-10-04): 149 tests passed on Windows with no
skips; the synthetic snapshot passed strict HTML checks and byte-identical
repeat builds. All 51 staged/tracked technical files passed the repository
path allowlist during that complete local run. The
build-only workflow bytes and its reviewed SHA remain unchanged. This is not
an assertion of new hosted/Linux validation or production deployment. The
complete local real snapshot contains 249 opted-in pages and 1585 referenced
hashed assets. Both actual Hugo builds passed strict page/anchor/resource checks
with identical output; private identity and content were promoted together only
after the final source-consistency barrier. No source notes were edited. CJK
math convenience forms emit warnings; invalid TeX still fails. The remaining
future production/privacy/renderer approvals are unchanged.
An additional standalone `publish.py build --snapshot <local-release>` succeeded
without a Vault argument/private registry: 249 pages, 2675 validated internal
links/resources and 1840 byte-identical output files. Replaying the reviewed
paired plan returned `changed: false`, without a second publication update.

The first authorized technical/synthetic-only push passed both hosted jobs:
[initial validation run](https://github.com/ladybug001/ladybug001.github.io/actions/runs/37118336518)
at commit `19670bf4bb9d24ab2d5ebf304b2a5432fe7d1c5d`.
Windows ran 100 tests with no skips; Linux ran the same 100-test suite with only
the explicitly Windows-specific junction test skipped. Both jobs installed
the checksum-pinned standard Hugo 0.167.0, validated the two-page snapshot and
HTML, and confirmed identical repeat builds within the job.

No real-note snapshot, private recovery archive or identity state was uploaded.
The Hugo workflow itself does not upload artifacts or deploy Pages. However,
after the initial pushes a **separate repository-level legacy Pages mechanism**
was discovered: it publishes the main branch root and triggered GitHub's dynamic
`pages build and deployment` workflow. The resulting site uses only the already
authorized technical/synthetic repository data, not real notes.
With explicit user approval, Pages was switched from `legacy` branch publishing
to `workflow` on 2026-10-03. The current Hugo workflow has no deployment steps,
so main pushes no longer select the legacy branch-publication mechanism.
GitHub rejected the site-deletion API with HTTP 422 (`Deactivating GitHub pages
for this repository is not allowed.`), before and after that change. The Pages
API still reports the existing site as `built`; disabling future automatic
publishing must not be described as taking the existing site offline.
The browser settings page could not be opened by the available automation.
The current deployment still needs the repository administrator to use
**Settings → Pages → Unpublish site** if confirmed site removal is required.
On the subsequent read-only check, the public site returned HTTP 404 while
the Pages API still reported `build_type=workflow` and `status=built`.
This establishes observed public unavailability, not confirmed removal of the
deployment or of Pages configuration; no further settings changes were made.
The Pages source should have been checked before the first push; CI success
does not prove that repository-level publishing is disabled.

The next upload has been authorized for technical code and synthetic data only.
Before any push, recheck remote history and confirm the upload scope. Do not
force push or overwrite an existing repository.
Before real complete export, resolve current diagnostic failures and review the
digest-bound local plan. Before upload, obtain explicit content/privacy approval
and review the snapshot-only repository allowlist. Enable one production
workflow only after a separate Pages/deployment authorization and selected
site-renderer review. Current snapshots are intentionally non-deployable.

Upstream references: [checkout v6.0.2](https://github.com/actions/checkout/releases/tag/v6.0.2),
[setup-python v6.2.0](https://github.com/actions/setup-python/releases/tag/v6.2.0),
[Python version manifest](https://github.com/actions/python-versions/blob/main/versions-manifest.json),
[Hugo v0.167.0](https://github.com/gohugoio/hugo/releases/tag/v0.167.0).
