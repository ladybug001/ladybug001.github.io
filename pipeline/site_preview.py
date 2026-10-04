#!/usr/bin/env python3
"""Build/serve the selected Stack renderer locally from a completed snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urljoin, urlsplit

from publisher.htmlcheck import Page
from publisher.identity import Registry
from publisher.callouts import normalize_callouts
from publisher.lifecycle import safe_path
from publisher.safety import within
from publisher.snapshot import _install, _tree, inspect_snapshot, materialize_snapshot, payload, redirect_html, sha
from publisher.storage import atomic_write, publication_head
from publisher.syntax import parser as markdown_parser
from publisher.tooling import PROJECT, runtime_check
from site_tools import install, site_lock


def current_snapshot(project=PROJECT):
    head = publication_head(Path(project) / '.local/state/publisher')
    if head is None or head['release'] is None:
        raise ValueError('No completed local publication; pass --snapshot explicitly')
    snapshot = safe_path(Path(project) / '.local/state/publisher/publication-releases' / head['release'] / 'snapshot')
    files, _, _ = inspect_snapshot(snapshot)
    if sha(files['manifest.json']) != head['snapshot_sha256']:
        raise ValueError('Local head/snapshot mismatch')
    return snapshot


def utility_pages(project=PROJECT):
    result = {}
    for name, title, icon, weight in [('recent', '最近', 'clock', 2),
                                       ('mocs', 'MOCs', 'archives', 3),
                                       ('all-notes', '全部笔记', 'infinity', 3),
                                       ('search', '搜索', 'search', 4),
                                       ('about', '关于', 'user', 5)]:
        header = {'title': title, 'url': '/' + name + '/', 'type': 'page', 'layout': name,
                  'menu': {'main': {'weight': weight, 'params': {'icon': icon}}},
                  'params': {'comments': False}}
        if name == 'search':
            header['outputs'] = ['HTML', 'JSON']
        if name == 'all-notes':
            header['aliases'] = ['/archives/']
            del header['menu']  # Keep old URLs working without a duplicate menu entry.
        filename = '_index.md' if name in {'all-notes', 'recent'} else 'index.md'
        body = (Path(project) / 'site/about.md').read_bytes() if name == 'about' else b''
        result['content/site-pages/' + name + '/' + filename] = payload(header) + b'\n' + body
    return result


def first_image(page):
    """Select the first published local image, ignoring code and remote URLs."""
    images = {page['url'] + 'media/' + name: 'media/' + name
              for name in page['resources']
              if name.rsplit('.', 1)[-1].lower() in {'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'svg', 'bmp', 'tiff', 'tif', 'ico'}}
    for token in markdown_parser().parse(page['body']):
        for child in token.children or []:
            if child.type == 'image':
                target = urlsplit(child.attrGet('src') or '')
                if not target.scheme and not target.netloc and unquote(target.path) in images:
                    return images[unquote(target.path)]
    return None


def paired_registry(snapshot, snapshot_manifest, project=PROJECT):
    """Read only the private identity registry belonging to this exact snapshot."""
    snapshot = safe_path(snapshot)
    releases = safe_path(Path(project) / '.local/state/publisher/publication-releases')
    if not within(releases, snapshot):
        return None
    if snapshot.name != 'snapshot' or snapshot.parent.parent != releases:
        raise ValueError('Invalid paired publication snapshot location')
    release = snapshot.parent
    raw_manifest = safe_path(release / 'manifest.json').read_bytes()
    manifest = json.loads(raw_manifest)
    if (manifest.get('schema_version') != 1 or manifest.get('kind') != 'private-publication-release'
            or release.name != 'release-' + sha(raw_manifest)[:24]
            or manifest['files']['snapshot/manifest.json'] != sha(snapshot_manifest)):
        raise ValueError('Presentation metadata does not match the paired snapshot')
    raw_registry = safe_path(release / 'registry.json').read_bytes()
    if sha(raw_registry) != manifest['files']['registry.json']:
        raise ValueError('Paired identity registry changed')
    data = json.loads(raw_registry)
    Registry.from_dict(data)
    return data


def moc_ids(snapshot, snapshot_manifest, project=PROJECT):
    """Classify by paired folder paths, never by a title or tag guess."""
    data = paired_registry(snapshot, snapshot_manifest, project)
    registry = Registry.from_dict(data) if data else Registry()
    return {binding.id for binding in registry.bindings
            if binding.status == 'active' and binding.source_path.startswith('03-MOCs/')}


def file_modified(path):
    """Read filesystem mtime, never creation time or inode-change ctime."""
    try:
        details = safe_path(path).stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode):
        raise ValueError('Modification-time source is not a regular note file')
    # Explicit UTC+08:00 display, independent of the renderer machine's timezone.
    return datetime.fromtimestamp(details.st_mtime, timezone(timedelta(hours=8))).isoformat(timespec='microseconds')


def presentation_metadata(snapshot, snapshot_manifest, note_ids, project=PROJECT):
    """Local read-only enrichment; only public booleans/timestamps reach Hugo."""
    data = paired_registry(snapshot, snapshot_manifest, project)
    if not data:
        return {}
    vault_root = data.get('vault_root')
    if not isinstance(vault_root, str) or not Path(vault_root).is_absolute():
        raise ValueError('Paired registry has no absolute Vault root')
    vault = safe_path(vault_root)
    result = {}
    for binding in Registry.from_dict(data).bindings:
        if binding.status != 'active' or binding.id not in note_ids:
            continue
        source = safe_path(vault / binding.source_path)
        if not within(vault, source):
            raise ValueError('Modification-time source escapes Vault')
        values = {'moc': binding.source_path.startswith('03-MOCs/')}
        modified = file_modified(source)
        if modified:
            values['file_modified'] = modified
        result[binding.id] = values
    return result


def renderer_input(snapshot, root, project=PROJECT, metadata=None):
    source, manifest, pages = inspect_snapshot(snapshot)
    if metadata is None:
        metadata = presentation_metadata(snapshot, source['manifest.json'], pages, project)
    generated, _ = materialize_snapshot(snapshot, root / 'adapter')
    files = {n: raw for n, raw in _tree(generated).items() if n != 'manifest.json'}
    for identifier, page in pages.items():
        name = 'content/notes/' + identifier + '/index.md'
        raw = files[name].decode('utf-8')
        header, end = json.JSONDecoder().raw_decode(raw)
        header['params'].update(metadata.get(identifier, {'moc': False}))
        body = normalize_callouts(raw[end:])
        image = first_image(page)
        if image:
            header['params']['list_image'] = image
        files[name] = (json.dumps(header, ensure_ascii=False, indent=2) + body).encode('utf-8')
    files.update(utility_pages(project))
    files['manifest.json'] = payload({'snapshot_sha256': sha(source['manifest.json']),
                                     'files': {n: sha(raw) for n, raw in sorted(files.items())}})
    return _install(files, root / 'renderer', 'stack-input-'), manifest, pages


class SitePage(Page):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        # Theme image markup may use srcset in addition to src.
        values = dict(attrs)
        if values.get('srcset'):
            self.links.extend(item.strip().split()[0] for item in values['srcset'].split(',') if item.strip())


def verify_site(snapshot, public, base_url):
    source, manifest, notes = inspect_snapshot(snapshot)
    built = _tree(public)
    pages, errors = {}, []
    for name, raw in built.items():
        if name.endswith('.html'):
            page = SitePage()
            page.feed(raw.decode('utf-8'))
            pages[name] = page
            errors.extend('Duplicate HTML ID: ' + name + '#' + value for value, count in page.ids.items() if count > 1)
        if not (name == '404.html' or name.endswith(('index.html', '.css', '.js', '.map', '.json'))
                or re.fullmatch(r'img/avatar\.(?:svg|png|jpg|jpeg|webp)', name)
                or re.fullmatch(r'notes/[^/]+/media/[a-f0-9]{64}\.[a-z0-9]{1,12}', name)):
            errors.append('Unexpected renderer output: ' + name)
    expected_graph = {}
    for edge in manifest['link_graph']:
        if edge.get('rendered', True):
            expected_graph.setdefault(edge.get('rendered_on_id', edge['source_id']), set()).add(unquote(edge['url']))
    for identifier, note in notes.items():
        name = unquote(note['url']).lstrip('/') + 'index.html'
        page = pages.get(name)
        if page is None:
            errors.append('Missing note route: ' + note['url'])
            continue
        errors.extend('Missing note anchor: ' + note['url'] + '#' + a['id']
                      for a in note['anchor_insertions'] if a['id'] not in page.ids)
        actual = {unquote(urlsplit(urljoin(base_url + name, href)).path) +
                  ('#' + unquote(urlsplit(href).fragment) if urlsplit(href).fragment else '') for href in page.links}
        errors.extend('Missing converted link: ' + value for value in expected_graph.get(identifier, set()) - actual)
        for asset in note['resources']:
            output = unquote(note['url']).lstrip('/') + 'media/' + asset
            if built.get(output) != source['assets/' + asset]:
                errors.append('Missing/changed note asset: ' + output)
    for redirect in manifest.get('redirects', []):
        name = unquote(redirect['from']).lstrip('/') + 'index.html'
        if built.get(name) != redirect_html(redirect['to']):
            errors.append('Changed historical redirect: ' + name)
    checked, external = 0, set()
    for name, page in pages.items():
        for raw in page.links:
            url = urlsplit(urljoin(base_url + name, raw))
            if url.scheme in {'mailto', 'tel'}:
                continue
            if url.scheme not in {'http', 'https'}:
                errors.append('Unsafe output link: ' + name)
                continue
            if url.netloc != urlsplit(base_url).netloc:
                external.add(url.netloc)
                continue
            target = unquote(url.path).lstrip('/')
            if url.path.endswith('/'):
                target += 'index.html'
            elif target not in built and target + '/index.html' in built:
                target += '/index.html'
            if target not in built or not within(public, Path(public) / target):
                errors.append('Broken local resource/link: ' + name + ' -> ' + raw)
            elif url.fragment and target in pages and unquote(url.fragment) not in pages[target].ids:
                errors.append('Broken local fragment: ' + name + ' -> ' + raw)
            else:
                checked += 1
    for name in ('index.html', 'mocs/index.html', 'recent/index.html', 'all-notes/index.html',
                 'search/index.html', 'search/index.json', 'archives/index.html', 'about/index.html'):
        if name not in built:
            errors.append('Missing theme utility route: ' + name)
    if _tree(snapshot) != source or _tree(public) != built:
        errors.append('Input/output changed during validation')
    return {'passed': not errors, 'notes': len(notes), 'html_pages': len(pages),
            'internal_links_resources': checked, 'files': len(built), 'errors': sorted(set(errors)),
            'external_domains_not_fetched': sorted(external), 'validation_only': True, 'deployable': False,
            'limitations': ['Theme scripts are trusted pinned renderer code, not authored Vault HTML.',
                            'Mermaid remains escaped code; diagrams are not yet rendered.',
                            'Browser interactions require separate inspection; no production deployment.']}


def build(snapshot, *, project=PROJECT, base_url='http://127.0.0.1:8085/', verify_repeat=False, metadata=None):
    project = Path(project).absolute()
    snapshot = safe_path(snapshot)
    allowed = (snapshot in {project / 'publication/fixture', project / 'publication/current/snapshot'} or
               within(project / '.local/state/publisher/publication-releases', snapshot) or
               within(project / '.generated/snapshots', snapshot))
    if not allowed:
        raise ValueError('Preview accepts only completed project snapshots, never a Vault directory')
    runtime_check(project)
    executable, themes = install(project)
    root = safe_path(project / '.build/stack-preview')
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=root))
    source_before = _tree(snapshot)
    generated, _, _ = renderer_input(snapshot, work, project, metadata)
    input_before = _tree(generated)
    configuration = {'baseURL': base_url, 'contentDir': str(generated / 'content'),
                     'staticDir': [str(generated / 'static')], 'themesDir': str(themes),
                     'resourceDir': str(work / 'resources')}
    config = work / 'generated.json'
    atomic_write(config, json.dumps(configuration) + '\n')
    outputs = []
    for label in (('first', 'repeat') if verify_repeat else ('first',)):
        public = work / label / 'public'
        public.mkdir(parents=True)
        subprocess.run([str(executable), '--source', str(project / 'site'),
                        '--config', str(project / 'site/hugo.toml') + ',' + str(config),
                        '--destination', str(public), '--cacheDir', str(work / 'cache'),
                        '--buildFuture', '--noBuildLock', '--noChmod', '--noTimes'], check=True, timeout=180)
        report = verify_site(snapshot, public, base_url)
        if not report['passed']:
            raise ValueError('Stack output validation failed: ' + json.dumps(report['errors'], ensure_ascii=False))
        if verify_repeat:
            outputs.append(_tree(public))
    if ((verify_repeat and outputs[0] != outputs[1]) or
            _tree(snapshot) != source_before or _tree(generated) != input_before):
        raise ValueError('Stack builds differ or snapshot/generated input changed')
    report.update({'repeat_build_checked': verify_repeat, 'theme_version': site_lock(project)['version'],
                   'build_root': str(work), 'public': str(work / 'first/public')})
    atomic_write(safe_path(project / '.local/reports/publisher/stack-preview.json'),
                 json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return work / 'first/public', report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, help='Default: completed local publication head')
    parser.add_argument('--serve', action='store_true', help='Serve verified static output on loopback only')
    parser.add_argument('--verify-repeat', action='store_true', help='Optional second build for reproducibility diagnostics')
    parser.add_argument('--port', type=int, default=8085)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError('Invalid preview port')
    base_url = f'http://127.0.0.1:{args.port}/'
    # Bind before building to fail before work if the requested port is occupied.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), SimpleHTTPRequestHandler) if args.serve else None
    try:
        public, report = build(args.snapshot.absolute() if args.snapshot else current_snapshot(),
                               base_url=base_url, verify_repeat=args.verify_repeat)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if server:
            server.RequestHandlerClass = partial(SimpleHTTPRequestHandler, directory=str(public))
            print('Local-only Stack preview: ' + base_url, flush=True)
            server.serve_forever()
    finally:
        if server:
            server.server_close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('STACK_PREVIEW_FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
