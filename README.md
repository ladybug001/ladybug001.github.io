# Obsidian → Hugo publication pipeline

The Obsidian Vault is the only human-maintained content source. The Python
pipeline reads opt-in notes without writing to the Vault, resolves stable
identities/URLs, links and referenced attachments, and emits portable snapshots.
A separate adapter generates disposable Hugo content and builds HTML.

This repository publishes the owner's opt-in notes with Hugo and Stack to
https://ladybug001.github.io/. Original Vault files remain read-only to the
publisher. `publication/current/` is generated public input, not another manually
maintained note library. Private identities, overrides and reports stay local.

The [build-only workflow](.github/workflows/hugo-ci.yml) runs converter tests,
strict snapshot/HTML checks and two reproducible Hugo builds on Windows/Linux.
Python/Hugo and Actions versions are pinned; Python dependencies have locked
wheel hashes. CI has read-only permissions, no artifact upload and no Pages
deployment. A separate [Pages workflow](.github/workflows/hugo-pages.yml) builds
the approved public snapshot with pinned Stack/Hugo, validates links/resources
and uploads only fresh build output. Only its deploy job has Pages permissions.

See [publication architecture and local transactions](docs/hugo-publication.md),
[CI instructions](docs/hugo-ci.md) and [snapshot hand-off](publication/README.md).
The older `pipeline/prepush.py` is for historical technical-only pushes and does
not authorize production deployments. Run `pipeline/production.py check` for
the current public hand-off. Private identity state and reports, the Vault,
local recovery archives, generated samples and build output must never be committed.

## Updates

1. Edit notes in Obsidian; publish only notes with `publish: true`.
2. Run `pipeline/publish.py publication-plan --vault <vault>`; review changes.
3. Apply with `publication-apply --vault <vault> --plan <plan> --expect-plan <digest>`.
4. Run `pipeline/production.py export --vault <vault>`, then `production.py build`.
5. Commit reviewed changes including `publication/current/` and push `main`.
   Actions rebuilds and deploys. Never commit a local `public/` directory.

Renames require an explicit identity move, not a guessed filename match.
The exporter replaces the generated hand-off, removing withdrawn notes and
unreferenced assets. Git history and previously public copies are not erased.

The Quartz implementation and its obsolete local recovery archive have been
removed. Stack and converter tests share one Hugo Extended installation;
normal local preview builds once, with repeat validation available on request.
