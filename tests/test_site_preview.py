import io
import html
import json
from pathlib import Path
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from publisher.snapshot import inspect_snapshot, payload, sha
from publisher.callouts import normalize_callouts
from site_preview import SitePage, build, current_snapshot, file_modified, first_image, moc_ids, presentation_metadata, utility_pages, verify_site
from site_tools import download, site_lock, theme_files


class StackPreviewTests(unittest.TestCase):
    def test_recent_is_second_menu_entry_and_ten_notes_without_intro_or_pagination(self):
        pages = utility_pages()
        recent = json.loads(pages['content/site-pages/recent/_index.md'])
        mocs = json.loads(pages['content/site-pages/mocs/index.md'])
        self.assertEqual(recent['menu']['main']['weight'], 2)
        self.assertEqual(mocs['menu']['main']['weight'], 3)
        project = Path(__file__).resolve().parents[1]
        template = (project / 'site/layouts/recent.html').read_text(encoding='utf-8')
        self.assertIn('range first 10 (sort $pages "Params.file_modified" "desc")', template)
        row = (project / 'site/layouts/_partials/article-list/recent.html').read_text(encoding='utf-8')
        self.assertIn('修改：', row)
        self.assertNotIn('file_created', row + template)
        for removed in ('<header>', '<h1', '<p>', 'pagination.html', 'helper/paginator', 'section-count'):
            self.assertNotIn(removed, template)

    def test_list_tags_use_theme_switch_and_separate_clickable_links(self):
        project = Path(__file__).resolve().parents[1]
        settings = tomllib.loads((project / 'site/hugo.toml').read_text(encoding='utf-8'))
        self.assertFalse(settings['params']['article']['readingTime'])
        self.assertTrue(settings['params']['article']['list']['showTags'])
        tags = (project / 'site/layouts/_partials/article-list/tags.html').read_text(encoding='utf-8')
        self.assertIn('with .GetTerms "tags"', tags)
        self.assertIn('href="{{ .RelPermalink }}"', tags)
        for name in ('compact', 'recent'):
            content = (project / f'site/layouts/_partials/article-list/{name}.html').read_text(encoding='utf-8')
            self.assertIn('</a>\n  {{ partial "article-list/tags" . }}', content)

    def test_modification_time_uses_mtime_not_birth_or_changed_time(self):
        details = SimpleNamespace(st_mode=0o100644, st_birthtime=999999, st_mtime=0, st_ctime=999999)
        with patch('site_preview.safe_path') as path:
            path.return_value.stat.return_value = details
            self.assertEqual(file_modified('note.md'), '1970-01-01T08:00:00.000000+08:00')
            del details.st_birthtime
            self.assertEqual(file_modified('note.md'), '1970-01-01T08:00:00.000000+08:00')
            details.st_mode = 0o040755
            with self.assertRaisesRegex(ValueError, 'regular note file'):
                file_modified('folder')
            path.return_value.stat.side_effect = FileNotFoundError
            self.assertIsNone(file_modified('missing.md'))

    def test_modification_metadata_uses_only_published_ids_and_exact_bound_paths(self):
        from publisher.identity import Binding, Registry
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            first = '00000000-0000-0000-0000-000000000001'
            second = '00000000-0000-0000-0000-000000000002'
            private = '00000000-0000-0000-0000-000000000003'
            data = {**Registry([Binding(first, '03-MOCs/导航.md'),
                Binding(second, 'Other/笔记.md'), Binding(private, 'Private.md')]).to_dict(),
                'vault_root': str(vault)}
            with patch('site_preview.paired_registry', return_value=data), \
                 patch('site_preview.file_modified', side_effect=['2026-10-04T08:00:00.000000+08:00', None]) as metadata:
                result = presentation_metadata(vault, b'manifest', {first, second})
                self.assertEqual(result, {first: {'moc': True, 'file_modified': '2026-10-04T08:00:00.000000+08:00'},
                                          second: {'moc': False}})
                self.assertEqual([c.args[0] for c in metadata.call_args_list],
                                 [vault / '03-MOCs/导航.md', vault / 'Other/笔记.md'])
                self.assertNotIn('date', result[first])
                self.assertNotIn('file_created', result[first])
                self.assertNotIn('source_path', result[first])
            with patch('site_preview.paired_registry', return_value=None):
                self.assertEqual(presentation_metadata(vault, b'manifest', {first}), {})
            data['vault_root'] = 'relative-vault'
            with patch('site_preview.paired_registry', return_value=data), self.assertRaisesRegex(ValueError, 'absolute Vault'):
                presentation_metadata(vault, b'manifest', {first})

    def test_moc_membership_uses_paired_folder_paths_not_names_or_tags(self):
        from publisher.identity import Binding, Registry
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            parent = project / '.local/state/publisher/publication-releases'
            parent.mkdir(parents=True)
            identifiers = ['00000000-0000-0000-0000-00000000000' + str(n) for n in range(1, 5)]
            registry = Registry([
                Binding(identifiers[0], '03-MOCs/sub/Example.md'),
                Binding(identifiers[1], '04-知识卡片/Example (MOC).md'),
                Binding(identifiers[2], '03-MOCs2/Example.md'),
                Binding(identifiers[3], '03-MOCs/Private.md', status='withdrawn'),
            ])
            raw_registry = payload(registry.to_dict())
            raw_snapshot = payload({'example': 'snapshot'})
            raw_manifest = payload({'schema_version': 1, 'kind': 'private-publication-release',
                'files': {'registry.json': sha(raw_registry), 'snapshot/manifest.json': sha(raw_snapshot)}})
            release = parent / ('release-' + sha(raw_manifest)[:24])
            snapshot = release / 'snapshot'
            snapshot.mkdir(parents=True)
            (release / 'manifest.json').write_bytes(raw_manifest)
            (release / 'registry.json').write_bytes(raw_registry)
            self.assertEqual(moc_ids(snapshot, raw_snapshot, project), {identifiers[0]})
            with self.assertRaisesRegex(ValueError, 'paired snapshot'):
                moc_ids(snapshot, b'changed', project)
            (release / 'registry.json').write_bytes(raw_registry + b' ')
            with self.assertRaisesRegex(ValueError, 'registry changed'):
                moc_ids(snapshot, raw_snapshot, project)

    def test_mocs_do_not_infer_folder_membership_for_unpaired_fixture(self):
        project = Path(__file__).resolve().parents[1]
        self.assertEqual(moc_ids(project / 'publication/fixture', b'fixture', project), set())

    def test_callouts_preserve_body_title_links_and_block_anchor(self):
        body = '> [!danger]- 危险命令\n> [资料](/notes/a/#h-one)\n\n{#b-risk}\n'
        expected = body.replace('[!danger]-', '[!CAUTION]')
        self.assertEqual(normalize_callouts(body), expected)
        self.assertEqual(normalize_callouts(expected), expected)

    def test_callouts_only_convert_real_quote_headers_and_keep_unknown_type(self):
        body = ('```md\n> [!danger]- 不转换\n```\n\n'
                '    > [!tip] 不转换缩进代码\n\n'
                '> 普通引用\n> [!warning] 不转换正文\n\n'
                '> [!custom]+\n> 内容\n\n'
                '> [!success]\n> 成功\n\n'
                '> > [!info] 自定义标题\n> > 内容\n')
        output = normalize_callouts(body)
        self.assertIn('```md\n> [!danger]- 不转换\n```', output)
        self.assertIn('    > [!tip] 不转换缩进代码', output)
        self.assertIn('> 普通引用\n> [!warning] 不转换正文', output)
        self.assertIn('> [!NOTE] custom\n', output)
        self.assertIn('> [!TIP] success\n', output)
        self.assertIn('> > [!NOTE] 自定义标题\n', output)

    def test_callouts_preserve_crlf_and_list_prefixes(self):
        body = '- item\r\n\r\n  > [!warning]+ 标题\r\n  > 内容\r\n'
        self.assertEqual(normalize_callouts(body), body.replace('[!warning]+', '[!WARNING]'))

    def test_view_counter_is_separate_and_never_uses_current_full_url(self):
        project = Path(__file__).resolve().parents[1]
        script = (project / 'site/assets/ts/custom.ts').read_text(encoding='utf-8')
        self.assertIn("location.protocol === 'https:'", script)
        self.assertIn('location.hostname === settings.dataset.host', script)
        self.assertIn("location.port === ''", script)
        self.assertIn("canonical.search = ''", script)
        self.assertIn("canonical.hash = ''", script)
        self.assertIn("referrer: ''", script)
        self.assertIn("referrerPolicy: 'no-referrer'", script)
        self.assertNotIn('location.href', script)
        self.assertIn('if (allowed)', script)
        self.assertIn('暂不可用', script)
        hook = (project / 'site/layouts/_partials/footer/custom.html').read_text(encoding='utf-8')
        self.assertIn('.Params.note_id', hook)

    def test_preview_builds_once_unless_repeat_check_is_requested(self):
        for repeat, expected in ((False, 1), (True, 2)):
            with self.subTest(repeat=repeat), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                snapshot = project / 'publication/fixture'
                snapshot.mkdir(parents=True)
                with patch('site_preview.runtime_check'), \
                     patch('site_preview.install', return_value=(project / 'hugo', project / 'themes')), \
                     patch('site_preview.renderer_input', return_value=(project / 'generated', {}, {})), \
                     patch('site_preview._tree', return_value={'same': b'bytes'}), \
                     patch('site_preview.verify_site', return_value={'passed': True}), \
                     patch('site_preview.site_lock', return_value={'version': '4.0.3'}), \
                     patch('site_preview.subprocess.run') as process:
                    public, report = build(snapshot, project=project, verify_repeat=repeat)
                    self.assertEqual(process.call_count, expected)
                    self.assertEqual(report['repeat_build_checked'], repeat)
                    self.assertTrue(public.is_dir())

    def archive(self, entries):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            for name, raw in entries:
                archive.writestr(name, raw)
        return data.getvalue()

    def test_lock_pins_theme_and_extended_archives(self):
        data = site_lock()
        self.assertEqual(data['version'], '4.0.3')
        self.assertEqual(len(data['commit']), 40)
        for key in ('archive_sha256', 'hugo_windows_amd64_sha256', 'hugo_linux_amd64_sha256'):
            self.assertRegex(data[key], r'^[a-f0-9]{64}$')

    def test_theme_installs_runtime_not_demo_or_workflow(self):
        root = 'hugo-theme-stack-' + 'a' * 40 + '/'
        raw = self.archive([(root + 'LICENSE', 'license'), (root + 'theme.toml', 'name="Stack"'),
                            (root + 'layouts/index.html', '{{ .Title }}'),
                            (root + 'demo/content/private.md', 'not installed'),
                            (root + '.github/workflows/deploy.yml', 'not installed')])
        files = theme_files(raw, 'a' * 40)
        self.assertEqual(set(files), {'LICENSE', 'theme.toml', 'layouts/index.html'})

    def test_theme_traversal_backslash_and_wrong_root_rejected(self):
        root = 'hugo-theme-stack-' + 'a' * 40 + '/'
        for name in (root + '../escape', root + 'assets/../../escape', root + 'C:/escape',
                     root + 'assets\\escape', 'wrong/LICENSE'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                theme_files(self.archive([(name, 'bad')]), 'a' * 40)

    def test_theme_symlink_rejected(self):
        entry = zipfile.ZipInfo('hugo-theme-stack-' + 'a' * 40 + '/layouts/link')
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        with self.assertRaises(ValueError):
            theme_files(self.archive([(entry, 'target')]), 'a' * 40)

    def test_missing_runtime_metadata_rejected(self):
        with self.assertRaises(ValueError):
            theme_files(self.archive([]), 'a' * 40)

    def test_changed_cache_fails_without_network_or_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / 'archive'
            cache.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                download('https://invalid.example/', sha(b'expected'), cache)
            self.assertEqual(cache.read_bytes(), b'changed')

    def test_verified_cache_is_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / 'archive'
            cache.write_bytes(b'expected')
            self.assertEqual(download('https://invalid.example/', sha(b'expected'), cache), b'expected')

    def test_generated_utilities_do_not_add_note_metadata(self):
        pages = utility_pages()
        self.assertEqual(len(pages), 5)
        for raw in pages.values():
            data, _ = json.JSONDecoder().raw_decode(raw.decode('utf-8'))
            self.assertEqual(data['type'], 'page')
            self.assertFalse(data['params']['comments'])
            self.assertNotIn('date', data)
            self.assertNotIn('publish', data)
        self.assertEqual(json.loads(pages['content/site-pages/search/index.md'])['outputs'], ['HTML', 'JSON'])
        self.assertEqual(json.loads(pages['content/site-pages/all-notes/_index.md'])['aliases'], ['/archives/'])
        self.assertNotIn('menu', json.loads(pages['content/site-pages/all-notes/_index.md']))
        recent = json.loads(pages['content/site-pages/recent/_index.md'])
        self.assertEqual(recent['title'], '最近')
        self.assertEqual(recent['menu']['main']['params']['icon'], 'clock')
        about = pages['content/site-pages/about/index.md'].decode('utf-8')
        self.assertIn('"title": "关于"', about)
        header, _ = json.JSONDecoder().raw_decode(about)
        self.assertNotIn('note_id', header['params'])
        source = Path(__file__).resolve().parents[1] / 'site/about.md'
        self.assertTrue(about.endswith(source.read_bytes().decode('utf-8')))

    def test_first_image_uses_first_body_image_not_code_remote_or_unused_assets(self):
        asset = 'a' * 64 + '.png'
        later = 'b' * 64 + '.svg'
        url = '/notes/test/'
        page = {'url': url, 'resources': [later, asset],
                'body': '```md\n![code](' + url + 'media/' + later + ')\n```\n' +
                        '![remote](https://example.com/image.png)\n' +
                        '![first](' + url + 'media/' + asset + ')\n' +
                        '![later](' + url + 'media/' + later + ')\n'}
        self.assertEqual(first_image(page), 'media/' + asset)
        page['body'] = 'No images, only [attachment](' + url + 'media/' + asset + ')'
        self.assertIsNone(first_image(page))

    def test_first_image_supports_svg_and_reference_images_but_only_published_resources(self):
        asset = 'c' * 64 + '.svg'
        page = {'url': '/notes/test/', 'resources': [asset],
                'body': '![diagram][img]\n\n[img]: /notes/test/media/' + asset + '\n'}
        self.assertEqual(first_image(page), 'media/' + asset)
        page['resources'] = []
        self.assertIsNone(first_image(page))

    def test_html_inspects_srcset_as_well_as_graph_links(self):
        page = SitePage()
        page.feed('<a href="/notes/a/#b-x">A</a><img src="a.png" srcset="b.png 2x, c.png 3x">')
        self.assertEqual(page.links, ['/notes/a/#b-x', 'a.png', 'b.png', 'c.png'])

    def test_missing_completed_head_is_not_a_vault_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                current_snapshot(Path(temporary))

    def synthetic_output(self, root):
        snapshot = Path(__file__).resolve().parents[1] / 'publication/fixture'
        source, manifest, pages = inspect_snapshot(snapshot)
        for identifier, page in pages.items():
            output = root / page['url'].lstrip('/')
            output.mkdir(parents=True)
            body = ''.join('<p id="' + a['id'] + '"></p>' for a in page['anchor_insertions'])
            body += ''.join('<a href="' + html.escape(e['url'], quote=True) + '">link</a>'
                            for e in manifest['link_graph'] if e.get('rendered', True)
                            and e.get('rendered_on_id', e['source_id']) == identifier)
            (output / 'index.html').write_text(body, encoding='utf-8')
            for asset in page['resources']:
                target = output / 'media' / asset
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(source['assets/' + asset])
        for name in ('index.html', 'archives/index.html', 'mocs/index.html', 'recent/index.html',
                     'all-notes/index.html', 'search/index.html', 'about/index.html'):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('<a href="/">Home</a>', encoding='utf-8')
        (root / 'search/index.json').write_text('[]', encoding='utf-8')
        return snapshot

    def test_renderer_validates_all_note_routes_anchors_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary)
            snapshot = self.synthetic_output(public)
            report = verify_site(snapshot, public, 'http://127.0.0.1:8085/')
            self.assertTrue(report['passed'], report['errors'])
            self.assertEqual(report['notes'], 2)
            self.assertFalse(report['deployable'])

    def test_renderer_rejects_missing_anchors_and_converted_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary)
            snapshot = self.synthetic_output(public)
            (public / 'notes/fixture-a/index.html').write_text('missing content', encoding='utf-8')
            report = verify_site(snapshot, public, 'http://127.0.0.1:8085/')
            self.assertFalse(report['passed'])
            self.assertTrue(any('Missing note anchor' in e for e in report['errors']))
            self.assertTrue(any('Missing converted link' in e for e in report['errors']))

    def test_renderer_rejects_changed_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary)
            snapshot = self.synthetic_output(public)
            next(public.rglob('*.svg')).write_bytes(b'changed')
            report = verify_site(snapshot, public, 'http://127.0.0.1:8085/')
            self.assertFalse(report['passed'])
            self.assertTrue(any('Missing/changed note asset' in e for e in report['errors']))

    def test_renderer_rejects_broken_srcset_and_fragment(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary)
            snapshot = self.synthetic_output(public)
            (public / 'index.html').write_text('<img srcset="lost.png 2x"><a href="/archives/#lost">x</a>', encoding='utf-8')
            report = verify_site(snapshot, public, 'http://127.0.0.1:8085/')
            self.assertFalse(report['passed'])
            self.assertTrue(any('Broken local resource/link' in e for e in report['errors']))
            self.assertTrue(any('Broken local fragment' in e for e in report['errors']))

    def test_renderer_rejects_extra_output_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary)
            snapshot = self.synthetic_output(public)
            (public / 'private.txt').write_text('not allowed', encoding='utf-8')
            (public / 'index.html').write_text('<p id="duplicate"></p><p id="duplicate"></p>', encoding='utf-8')
            report = verify_site(snapshot, public, 'http://127.0.0.1:8085/')
            self.assertFalse(report['passed'])
            self.assertTrue(any('Unexpected renderer output' in e for e in report['errors']))
            self.assertTrue(any('Duplicate HTML ID' in e for e in report['errors']))


if __name__ == '__main__':
    unittest.main()
