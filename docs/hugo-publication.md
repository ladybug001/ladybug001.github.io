# Local publication contract

This is a content/conversion/build architecture, not a Theme, page layout,
navigation or visual design. Stack now has an isolated local renderer in `site/`
(see [preview instructions](hugo-stack.md)). Production uses a separate reviewed
Pages workflow and `publication/current/` public hand-off.

## Source and directory ownership

| Location | Responsibility | Human edits? |
| --- | --- | --- |
| External Obsidian Vault | Sole source of note bodies and content metadata | Yes, by its owner only |
| `pipeline/` | Central Python parser, checks, conversion, snapshot and Hugo adapter | Code only |
| `.local/state/publisher/` | Private identities, overrides, revisions and publication heads | Through reviewed commands; back up privately |
| `.local/reports/` | Private diagnostics and review plans | Review, never upload |
| `.local/state/publisher/publication-releases/release-*/snapshot/` | Completed portable local snapshot paired with an identity revision | Never; generated |
| `.generated/` | Disposable adapter/materialization results | Never; generated |
| `.build/` | Isolated test/build output, including `public/` | Never; generated |
| `publication/fixture/` | Deterministic synthetic two-page fixture | Rebuild with its generator |
| `publication/current/` | Approved portable snapshot, public presentation metadata and receipt for CI | Replace by reviewed generation, never hand-maintain |
| Future project `config/`, `layouts/`, `assets/`, `static/`, `data/` | Site configuration, render adapters, processed assets, copied assets and generated public indices | Project-level extension points; not a selected design |

Hugo `content/` contains generated Leaf Page Bundles, not a second note library.
Generated public data must occupy a separate data mount/directory from hand-
maintained configuration. A future Theme is a pinned dependency; never edit
`themes/<theme>/` in place. The current `pipeline/hugo-test/` is only a rendering
test harness and does not define the future site's architecture or appearance.

Safe-to-regenerate does not mean safe-to-delete arbitrarily: completed releases
must be retained while referenced by a head or review plan. Cache/output cleanup
is a separate exact-target operation. Private identity/history state is **not**
disposable; losing it can change URLs, route ownership and comment associations.

## Content eligibility and metadata

Only in-scope notes with literal YAML boolean `publish: true`, `draft != true`
and `private != true` are eligible. Publication never recursively opts in a
dependency. Missing/quoted booleans do not silently become true. YAML duplicate
keys, invalid dates, aliases/anchors, unsafe slugs and invalid metadata fail.
Unknown source fields are allowed in the Vault but never copied wholesale.

| Field | Ownership and default |
| --- | --- |
| `title` | Optional owner field; filename stem supplies display title only |
| `publish` | Owner's explicit opt-in; absent means false |
| `draft`, `private` | Owner exclusions; absent means false |
| `id` | Optional owner UUID; otherwise permanent private sidecar identity, never written into the Vault |
| `slug` | Optional initial owner segment, including Unicode; otherwise `n-<UUID hex>`; locked in state afterwards |
| `date` | Optional owner ISO date/time; never invent creation time from file mtime or migration time |
| `updated` | Optional owner ISO date/time; otherwise observed semantic-change time after the first complete release |
| `tags`, `categories`, `series` | Optional owner string lists; absent means no values |
| `aliases` | Optional note-name aliases for resolution; **not** redirect URLs |

The snapshot whitelists title/date/updated/tags/categories/series/aliases only.
Hugo `url`, `lastmod` and `params.note_id` are adapter-generated. No Theme-specific
fields are required. Missing dates remain missing; the Hugo adapter prevents
filesystem/lastmod fallback from manufacturing a creation date. Automatic
`updated` is an observation, not a claim of the historical edit time. Repeated
exports without semantic changes retain it; a backwards observation clock fails.

## Identities, resolution and stable routes

The full Note Index scans metadata/names/paths needed to detect duplicate names
and private targets, but reads published bodies only within policy. Exact paths,
source-relative paths, declared aliases, names, immutable UUID bindings and
explicit legacy-name mappings participate in resolution. Ambiguity is an error,
not a heuristic choice. Paths/names use normalized Unicode/case conflict checks.
The private registry reserves ID, exact source path, slug, permalink, status and
all historical public paths. Public graph entries expose IDs/URLs, not Vault paths.

Title edits do not change URLs. A moved source requires an explicit `move`
binding; equal basenames or equal body hashes do not prove identity. New opt-in
notes are proposed for registration in the private review plan. Once assigned,
their ID remains in the registry. Explicit route changes reserve old paths
permanently, including after withdrawal. Route history cannot be reassigned to
another note. A source slug disagreeing with a locked slug fails rather than
silently changing either the source or URL.

`[[A]]`, aliases and heading/block fragments are converted to regular Markdown
links to locked public routes and separately generated anchors. Inline code,
fenced code and math literals are protected through AST source positions, not
global regex replacement. Duplicate heading names, missing fragments, unresolved
targets and ambiguous targets block publication. Existing unpublished/out-of-
scope note links become safe nonclickable display text; private targets use a
generic unavailable label. Missing files are not automatically presented as
merely unpublished. Unpublished embeds and unresolved embeds are blocking errors.

Published standalone note/heading/block embeds expand with scoped dependencies,
namespaced anchors and footnotes; cycles and expansion budgets fail. Unsupported
inline/list/table embeds, lazy/mixed quote forms and dynamic Dataview are rejected,
not silently flattened or dropped. Supported quoted paragraph anchors preserve
the quote prefix in independent anchor IR and are verified in real Hugo HTML.
Marker-only paragraph continuation lines are replaced in generated text rather
than turned into blank lines that detach an anchor. Anchor IR follows physical
Markdown order even when headings and blocks were inspected in separate passes.

Only referenced attachments enter the snapshot. Each source path is resolved
and checked for ambiguity/missing files and stable bytes, then copied under a
content hash. The adapter creates per-page Leaf Bundles and rewrites references;
Obsidian attachment organization is unchanged. SVG is validated, with reviewed
hash-bound compatibility transformations centralized outside the Vault. No remote
attachment fetching or EXIF/privacy stripping is silently performed.

Tables and ordinary code remain Markdown. Attribute-free inline `<br>`, `<br/>`
and `<br />` are the only authored HTML exception, preserving table line breaks;
other HTML, event/style attributes, executable shortcodes and unsafe schemes fail.
The test renderer allows HTML only behind this input gate. A future Theme must
retain this gate rather than enabling arbitrary Vault HTML. Callout text is
preserved as blockquotes; no callout appearance is chosen. Mermaid fences are
preserved but diagrams are not yet rendered. LaTeX is retained in Markdown and
the pinned Hugo test adapter produces server-side MathML, failing on invalid TeX.
Convenience/CJK forms use `strict: warn`; genuine parse errors keep
`throwOnError: true`. This preserves authored expressions while surfacing
compatibility diagnostics rather than editing the Vault. See
[Hugo's independent strictness and parse-error options](https://gohugo.io/functions/transform/tomath/).
Browser appearance is not tested or selected.

The graph retains source/target/rendered-on IDs, link kind, rendered status and
URL. Snapshot validation independently derives actual links from Markdown and
checks graph consistency. Future backlinks/search can consume this public graph;
they must not expose private index entries or turn non-rendered unresolved graph
relations into clickable links without validation.

## Reviewed complete local transaction

```text
python -B pipeline/publish.py check --vault <vault> --conversion
python -B pipeline/publish.py publication-plan --vault <vault>
python -B pipeline/publish.py publication-apply --vault <vault> --plan <private-plan> --expect-plan <reviewed-sha256>
python -B pipeline/publish.py build --snapshot <completed-local-snapshot>
```

`check --conversion` collects deep conversion issues in memory, retaining no
exported bodies. Unregistered source/dependency IDs are reported as pending, not
allocated by `check`. Optional `--operations-file` on `publication-plan` supports
explicit moves/routes in a private batch. Never run apply merely because a plan
exists: inspect the exact operations, eligibility, diagnostics and digest first.

Plan/prepare scans, filters, checks identities/links, converts all eligible notes,
copies referenced bytes in memory and validates a complete `portable-2` snapshot.
It exposes proposed registrations, withdrawals and restorations for review.
The digest binds source/attachment inventory, base revision/head, configuration,
observation time and target immutable release. It does not change current state.

Apply acquires a writer lock, rechecks the digest/base/source/configuration,
stages registry + private observations + portable snapshot in **one immutable
release**, builds twice with the pinned Hugo into fresh output, validates all
HTML routes/fragments/assets, checks byte equality, and rechecks the source.
Only then does it replace **one** `publication-head.json` authoritative pointer.
The former `identity-registry.json` is bootstrap state, not a second authority.
Two independent file replacements are not described as one atomic transaction.

A failure preserves the previous complete head. First-run bootstrap points only
to the unchanged existing identity revision until the complete release succeeds.
Interrupted staging may leave private unreferenced candidates; retry the same
reviewed plan, never guess a new current release. Missing/corrupt pointers,
changed immutable files, stale plans, reparse paths, wrong Vault roots, unknown
state fields and missing revisions fail closed. Process crashes may leave a lock;
there is no automatic lock breaking, deletion, rollback or destructive recovery.
Vault saves are not locked; read barriers detect observed changes but cannot
prevent an editor saving after the final read. Later edits never enter an already
completed immutable snapshot through a no-op replay.

Complete snapshots support 0–2048 pages, 32 MiB/file, 512 MiB total, 16384 files
and bounded directory/expansion depth. The older small `portable-1` fixture/sample
profile still requires 1–12 pages. Exceeding a budget fails, never drops content.
Windows snapshot I/O uses extended paths while inventories stay relative/portable.

Withdrawal creates a fresh complete snapshot without the body, its now-unused
attachments or history redirects; withdrawing everything yields an empty build.
IDs and routes stay reserved for restoration. Active historical redirects are
exact generated static HTML with meta refresh + fragment-preserving replacement,
not HTTP 301 on GitHub Pages. Local old releases, Git history, caches and previous
uploads are not automatically erased by withdrawal. Privacy deletion needs an
explicit, separately scoped history/cache operation.

## Production and comments: separate authority boundary

Current snapshots are `validation_only: true`, `deployable: false`. Hashes prove
integrity/repeatability, **not** privacy review or authority to publish. The current
reviewed repository allowlist also admits the selected site source files and
exact generated public hand-off, following the owner's deployment approval.

After content approval and renderer/Theme selection, the production contract is:

1. Export only a reviewed portable snapshot and public graph/assets; never the
   private registry, observations, reports, recovery archives or full Vault.
2. Run snapshot-only validation and materialization in Actions without Vault
   paths, Vault credentials or source-copy jobs. Use pinned Python/Hugo/dependencies;
   only add fixed Node versions + lockfiles if the selected Theme requires them.
3. Add a separately reviewed production workflow. PRs validate only; authorized
   main/manual production runs build fresh output and run renderer-aware strict
   checks before uploading a Pages artifact. Site assets/home routes need a
   reviewed output inventory instead of weakening the current technical gate.
4. Use exact reviewed upload/deploy Action commit pins, separate build and deploy
   jobs, `contents: read` for build, `pages: write`/`id-token: write` only for deploy,
   the `github-pages` environment and serialized Pages concurrency. Never grant
   PR/fork jobs deployment permissions. Confirm Pages Source = GitHub Actions
   before the first production push, and guard stale main revisions from deployment.
5. Deploy through Actions, not by checking in a locally built `public/` tree.

The separate `hugo-pages.yml` now builds and deploys the approved public hand-off;
only its deploy job receives Pages/OIDC rights. Discussions/Giscus remain disabled.
Giscus should be a project-level adapter, not part of note
conversion. Bind it preferably to `specific` stable term `note:<UUID>`; pathname
is acceptable only while routes stay immutable, since redirects do not migrate
Discussions. Title and URL changes must not silently create a different comment
thread. Repo/category IDs and the eventual insertion location require separate
user choices; the converter only supplies the immutable ID.

Backlinks, a Mermaid renderer, search, visual design and production activation
remain explicit extension/review steps. None requires a bulk Vault rewrite.
