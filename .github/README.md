# Obsidian publication pipeline → Hugo

Build-only migration repository. The Obsidian Vault remains the sole human
content source; it is never committed or edited by the pipeline.

Python resolves opt-in notes, identities, links and referenced attachments into
a portable generated snapshot. A separate adapter materializes Hugo Leaf Page
Bundles. Generated content/build output is disposable; private identity state
is persistent and must be backed up separately.

Currently, CI validates a **synthetic two-page snapshot**, not real notes.
It runs on fixed Ubuntu/Windows runner labels, pinned Python/Hugo, hash-locked
dependencies and commit-pinned Actions. It checks links/resources/anchors,
runs converter tests and compares two complete HTML builds.

CI has read-only repository permissions, no persisted checkout credentials,
no artifact upload and no Pages deployment. A theme, layout and visual design
have not been selected. The minimal HTML harness is a technical test only.

See [build-only CI instructions](../docs/hugo-ci.md) for commands and limits.
Quartz has been retired from the active project. Its private local recovery
archive is excluded from this repository and is not an alternative build path.
