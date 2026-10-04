# Stack: local preview and GitHub Pages

The owner selected Stack and authorized deployment on 2026-10-04. The portable
conversion and strict technical harness remain independent of the Theme.
`pipeline/production.py` reuses this renderer for the reviewed public hand-off;
cloud builds use exported timestamps/MOC flags, never private registry/Vault paths.

## Pinned dependencies

`site/theme.lock.toml` pins Stack 4.0.3 to its full upstream commit and downloaded
archive SHA-256, plus Hugo Extended 0.167.0 with official Windows/Linux archive
checksums. Stack and converter tests now share this one Extended Hugo installation. No Node/Go
install, global settings, Theme source edit, or starter deployment workflow is
needed. The installer only admits regular bounded runtime Theme files; the demo,
workflow and example note content are not installed. Fixed archives and unpacked
packages stay ignored under `.local/`, and altered cached packages fail closed.

## Local operation

Use the existing pinned Python environment, from the repository root:

```text
python -B pipeline/site_tools.py
python -B pipeline/site_preview.py --snapshot publication/fixture
python -B pipeline/site_preview.py --serve --port 8085
```

On Windows the existing project environment can be invoked with `preview.ps1`.
The last command defaults to the completed snapshot named by the private local
publication head. Note bodies come only from that snapshot. For the approved
Recent page it additionally reads filesystem modification times at the exact source
paths in the snapshot's paired private registry; no note bodies or Vault files are
changed. It never imports current source edits or updates identities. To incorporate later edits, use the existing
check, reviewed publication-plan/apply workflow first.

Only `127.0.0.1` is bound. A new private `.build/stack-preview/run-*/` contains
immutable generated input and one fresh output (two only with `--verify-repeat`). Validation runs before serving;
an error does not publish partial output. Snapshot inputs are never overwritten.
Normal preview builds once. Add `--verify-repeat` only when diagnosing
reproducibility; it is not required for daily preview.
An occupied port fails instead of killing an unknown process. Ctrl+C stops the
server; it does not remove output or a previous completed release.

The snapshot adapter generates articles as before. Framework-only navigation/search
pages are generated separately and cannot become a second content source. Stack
receives `mainSections = ['notes']`; locked `url` values override Theme defaults.
There is no automatic date backfill, category inference, invented biography
or private-path exposure. Nested taxonomy widgets resolve the explicit
taxonomy root rather than a nondeterministic term. Project CSS only clarifies
body-link underlines and image/heading focus states; the stock palette is retained.

The owner approved a short introductory home page, MOCs using the existing
published knowledge navigation, and a paginated all-notes entry. The introduction
was initially site copy on the home page. At the owner's later request, home now
uses Stack's stock paginated article-card layout, with the first local body image
as a list-only thumbnail and the theme's original sorting (no invented dates).
The introduction was moved to `site/about.md`, alongside a clearly marked empty
self-introduction. This is site/profile copy, not a second manually maintained
note library. The `/about/` menu entry is generated separately from notes and
has no note ID, article view counter or inferred biography. The owner's GitHub
login `ladybug001` supplies the site name (the profile has no separate display
name). `site/assets/img/avatar.jpg` is a local copy of the public GitHub avatar;
it is not fetched by visitors or synchronized automatically. The homepage tag
cloud uses `limit = 0` to show every tag from published notes. Positive limits
still restrict other taxonomy widgets. About copy remains owner-editable.
On desktop, unlimited clouds are marked separately and constrained to the
available viewport height. Only their tag list scrolls; search and the cloud
heading remain visible. The list is keyboard-focusable with a visible focus
outline and a thin scrollbar. Scoped project CSS leaves article TOCs, limited
widgets and mobile sidebar visibility unchanged.
MOCs is now an ungrouped, title-sorted list of published notes whose paired
registry paths are under `03-MOCs/` (including subfolders). Titles, tags and
the content of a navigation note do not determine membership. The private
registry is checked against the exact completed snapshot; only a `params.moc`
boolean is emitted, never a private source path.
The separate `/recent/` entry sits immediately below Home and displays only
the latest ten notes by filesystem modification time, without a title/count
banner, explanatory text, missing-time notice or pagination. It uses
generated `params.file_modified` from `st_mtime`, never invented frontmatter
`date`, creation time or Unix inode-change `ctime`. Missing bound files are
excluded without guessing renamed files. Filesystem modification includes any
file edit, including frontmatter-only changes; it is not a body-change detector. Home keeps
its existing pagination and ordering. These local presentation values are
frozen for each generated input/build; they must be captured as public-only
metadata at a future authorized publication handoff so CI never needs Vault
access or the private registry. This local preview is not a production handoff.
At the owner's request, the duplicate all-notes menu entry was removed after
home became a paginated list. The About page links to home instead. The hidden
title-sorted `/all-notes/` route and `/archives/` redirect remain for compatibility.

List-only thumbnails select the first Markdown image backed by that page's
published local resources, including SVG. Code examples, remote images and
unreferenced attachments are ignored; no image means no thumbnail. Selection
lives in the Stack adapter's generated `params.list_image`, not Vault metadata
or the portable snapshot. It does not add an article hero or remove a body image.

Automatic reading-time labels are disabled with Stack's `article.readingTime`
setting. The home/default list uses the upstream `article.list.showTags` switch;
compact lists (including Recent and taxonomy results) share a project partial
using the same tag icon/classes and Hugo taxonomy permalinks. Tag links sit
outside each compact row's article link, avoiding nested anchors. Untagged notes
emit no tag footer. Existing tags are displayed unchanged; no Vault metadata is
created or rewritten, and no estimated word count replaces the removed timer.

Project-level overrides preserve heading/block anchors, escaped code including
Mermaid fences, server MathML, and the existing authored-HTML input gate.
Mermaid **remains code**, not rendered diagrams in this preview. Comments and
remote fonts remain disabled; this does not configure Giscus or Discussions.
Source images retain their exact snapshot bytes.

## Renderer-aware validation

The local Stack checker verifies every snapshot page, required anchor, converted
graph link and referenced asset hash. It also checks all emitted HTML for
duplicate IDs and all local href/src/srcset targets and HTML fragments, plus
home/MOCs/all-notes/search routes. It accepts the explicit Theme-generated output types
instead of relaxing the old harness's exact-output/no-active-HTML contract.
The input snapshot and generated tree must remain unchanged. If explicitly
requested, two fresh builds must match byte for byte. The report stays private in
`.local/reports/publisher/stack-preview.json`.

Pinned Theme JavaScript is trusted renderer code, not permission for arbitrary
Vault HTML. External hyperlinks are not fetched. Generated file extensions are
a local preview output policy, not a production privacy/security certification.
Browser search and display require separate inspection. The build-only Actions
workflow remains unchanged: its hosted success does not claim a hosted Stack
build, and no artifact upload or deployment has been added. New local code has
not been pushed merely because the Theme was selected.

On 2026-10-04 the completed local real snapshot rendered 249 notes into 469 HTML
pages and 2064 output files. All 16783 local links/resources passed, including
required note anchors and exact attachment bytes; both builds were identical.
Browser inspection verified the home, local search and an illustrated article.
The local Python test suite passed all 164 tests, with no skips.
No Vault files, completed snapshot or current private state were changed. These
results do not authorize uploading the real content or activating production.

The list/paginator/blockquote adaptations retain Stack's stock structure and
attribution. Their upstream templates are GPL-3.0; preserve that license and
the Theme credit when distributing those adaptations. The installed package
includes upstream LICENSE and is never edited in place.

Production activation, a Theme-aware CI job and explicit approval of actual
content are separate next steps. Private local output is not a deployable site.

## Cleanup and simpler daily preview

On 2026-10-04 the owner requested cleanup. Obsolete Quartz recovery files,
old/failed build outputs, old generated samples, the Standard Hugo installation
and the duplicate unpacked Theme were deleted, removing about 10 GiB. The
currently served Stack HTML, completed publication snapshot and URL identity
state were retained. Deleted generated output can be rebuilt; there is no longer
a local recovery copy of the retired Quartz tree.

The shared Extended Hugo installer replaces the separate site binary installer.
Normal preview runs one build plus link/resource checks. The optional
`--verify-repeat` flag enables a second build; converter integration tests still
cover reproducibility without making it mandatory for daily preview.

The approved home/MOCs/all-notes revision rendered 249 notes into 471 HTML
pages; all 17592 local links/resources passed. Browser inspection confirmed
the original MOC groups, working list pagination, loaded local thumbnails and
no duplicate article hero. Temporary failed/old preview output was removed;
only the currently served HTML was retained, with the completed snapshot and
identity state untouched.

Home and MOCs use Stack's `article-page` body class so its stock card background
and light/dark article text colors apply. Handwritten home links use the stock
`link` class for the theme's underline/hover emphasis. No custom color or CSS
palette was introduced for this correction.

## Article reading features and view counts

The owner approved the article-reading improvements and production-only
third-party view counts. The stock TOC, code-copy button, syntax highlighting,
wide-table scrolling and end tags remain in place. Heading links preserve the
converter's explicit IDs and add accessible labels; line numbers stay off by
default. The homepage and navigation structure are unchanged.

`publisher/callouts.py` maps real Obsidian blockquote headers to Markdown alerts
only in the generated renderer input. It leaves code examples, body text, links
and block IDs intact. Standard Note/Tip/Important/Warning/Caution types retain
their style; common Obsidian aliases map to these styles. Unknown types use Note
style with their original type as the default title. Fold markers are removed
in generated output: callouts are always expanded, without discarding content.
Neither the Vault nor the completed portable snapshot is modified.

PhotoSwipe 5.4.4 (the theme's existing viewer) is vendored as three small runtime
files under `site/assets/vendor/`, with its MIT license. The CSS's line endings
were normalized during import; the JavaScript files are byte-identical to the
upstream release. The partial checks their fixed hashes and includes the license
in each served file. This requires no Node install, CDN requests by visitors, or
network during builds. Unlinked body images can be clicked to zoom, including SVG
using browser-known dimensions; authored image links retain their destination.
The image hook resolves locked absolute URLs back to local bundle resources so
raster dimensions are available. SVG loads eagerly to avoid a zero-sized lazy
placeholder; no remote image is fetched during the build.
Image placement and original attachment bytes are unchanged.

The theme's footer/custom-script extension points provide a small per-article
counter. Only `https://ladybug001.github.io` without a non-default port sends a
request to the owner-approved Busuanzi service (`https://cdn.busuanzi.cc/api.php`).
The request carries the canonical origin plus locked article path, never query
parameters, heading fragments, note titles or incoming referrer. This uses the
same endpoint/payload as the official 3.6.9 script, without loading that script
or letting it send the full browsing URL. The service receives the visitor IP
and normal connection information. Repeated visits count again; this is PV, not
unique readers or proof that someone finished reading. Renaming a title does not
change the count key; changing the domain or public path may start a new count.

Local previews display `上线后统计` without contacting the service. Failed or
blocked production requests display `暂不可用` after at most eight seconds and do
not affect article reading. Utility pages/lists do not request note counters.
No production deployment or live production statistics test is implied.

Validation for this revision: 171 Python tests passed; the final 249-note build
passed all 21371 local links/resources and preserved attachment bytes. Browser
checks covered desktop/mobile TOCs, heading jumps, light/dark text and underlines,
and raster/SVG viewer opening. A mock-only check of emitted counter JavaScript
verified five non-production guards, canonical request data, a successful result
and failed/invalid responses without contacting the statistics service. The
temporary desktop viewport was reset and light mode restored. No Vault scan,
Vault write, private identity/head update, Git push or deployment was performed.
