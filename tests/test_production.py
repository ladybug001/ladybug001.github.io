from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'pipeline'))
from ci import allowed_repository_path, production_workflow_check
from fixture import fixture_files
from production import BASE_URL, inspect_release, validate_presentation
from publisher.snapshot import payload, sha, validate
from site_preview import renderer_input


class ProductionTests(unittest.TestCase):
    def fixture(self, project):
        source = fixture_files()
        _, pages = validate(source)
        presentation = payload({'schema_version': 1, 'snapshot_sha256': sha(source['manifest.json']),
                                'notes': {identifier: {'moc': False, 'file_modified': '2026-10-04T20:00:00+08:00'} for identifier in pages}})
        files = {'presentation.json': presentation,
                 'release.json': payload({'schema_version': 1, 'kind': 'public-pages-release', 'base_url': BASE_URL,
                    'snapshot_sha256': sha(source['manifest.json']), 'presentation_sha256': sha(presentation)}),
                 **{'snapshot/' + n: raw for n, raw in source.items()}}
        root = project / 'publication/current'
        for name, raw in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        return root

    def test_exact_public_handoff_and_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            root = self.fixture(project)
            self.assertEqual(inspect_release(project)[3], 2)
            (root / 'registry.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'unexpected files'):
                inspect_release(project)
            (root / 'registry.json').unlink()
            (root / 'presentation.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                inspect_release(project)

    def test_presentation_rejects_paths_private_fields_unbound_ids_and_bad_dates(self):
        data = {'schema_version': 1, 'snapshot_sha256': 'digest', 'notes': {'id': {'moc': True}}}
        self.assertEqual(validate_presentation(data, 'digest', {'id'}), data['notes'])
        for field, value in [('source_path', 'private.md'), ('file_modified', '2026-10-04'), ('moc', 'true')]:
            invalid = copy.deepcopy(data)
            invalid['notes']['id'][field] = value
            with self.assertRaises(ValueError):
                validate_presentation(invalid, 'digest', {'id'})
        for digest, ids in [('other', {'id'}), ('digest', {'another-id'})]:
            with self.assertRaises(ValueError):
                validate_presentation(data, digest, ids)

    def test_cloud_renderer_never_uses_private_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.fixture(project)
            snapshot, metadata, _, _ = inspect_release(project)
            with patch('site_preview.presentation_metadata', side_effect=AssertionError('Cloud read Vault')), \
                 patch('site_preview.utility_pages', return_value={}):
                generated, _, _ = renderer_input(snapshot, project / 'work', project, metadata)
                self.assertTrue(generated.is_dir())

    def test_production_workflow_is_pinned_and_privilege_separated(self):
        production_workflow_check()

    def test_generated_allowlist_still_rejects_private_data(self):
        for path in ['publication/current/registry.json', 'publication/current/snapshot/private.md',
                     'site/private.txt', '.local/state/publisher/registry.json', 'public/index.html']:
            self.assertFalse(allowed_repository_path(path))
        self.assertTrue(allowed_repository_path('publication/current/snapshot/manifest.json'))
        self.assertTrue(allowed_repository_path('site/about.md'))


if __name__ == '__main__':
    unittest.main()
