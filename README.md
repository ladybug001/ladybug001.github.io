# Obsidian → Hugo publication pipeline

The Obsidian Vault is the only human-maintained content source. The Python
pipeline reads opt-in notes without writing to the Vault, resolves stable
identities/URLs, links and referenced attachments, and emits portable snapshots.
A separate adapter generates disposable Hugo content and builds HTML.

This repository currently contains **technical code and synthetic test data
only**, not real notes. No theme, visual design or home-page layout has been
selected. The minimal HTML harness is solely for validation.

The [build-only workflow](.github/workflows/hugo-ci.yml) runs converter tests,
strict snapshot/HTML checks and two reproducible Hugo builds on Windows/Linux.
Python/Hugo and Actions versions are pinned; Python dependencies have locked
wheel hashes. CI has read-only permissions, no artifact upload and no Pages
deployment.

See [publication architecture and local transactions](docs/hugo-publication.md),
[CI instructions](docs/hugo-ci.md) and [snapshot hand-off](publication/README.md).
Technical pushes should first pass the read-only `pipeline/prepush.py` check
described in the CI instructions; it is not an automatic Git hook.
Private identity state and reports, the Vault, local recovery archives,
generated real-note samples and build output must never be committed here.

The Quartz implementation has been retired from the active project. Its local
recovery archive is private and excluded from Git; it is not a second build path.
