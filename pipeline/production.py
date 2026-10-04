#!/usr/bin/env python3
"""Explicit public snapshot hand-off and snapshot-only Stack/Pages build.

Export reads the Vault only for public presentation timestamps. Build/check never
need the Vault, private registry, reports or credentials. No Git/network writes.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

sys.dont_write_bytecode = True
from publisher.lifecycle import safe_path
from publisher.safety import install_vault_write_guard, within
from publisher.snapshot import _json, _keys, _tree, inspect_snapshot, payload, sha
from publisher.storage import atomic_write
from publisher.tooling import PROJECT
from site_preview import build, current_snapshot, paired_registry, presentation_metadata

BASE_URL = 'https://ladybug001.github.io/'


def validate_presentation(data, snapshot_digest, identifiers):
    _keys(data, {'schema_version', 'snapshot_sha256', 'notes'})
    if data['schema_version'] != 1 or data['snapshot_sha256'] != snapshot_digest:
        raise ValueError('Presentation belongs to another snapshot')
    if not isinstance(data['notes'], dict) or set(data['notes']) != set(identifiers):
        raise ValueError('Presentation must contain exactly the published note IDs')
    for values in data['notes'].values():
        _keys(values, {'moc'}, {'file_modified'})
        if type(values['moc']) is not bool:
            raise ValueError('MOC membership must be a boolean')
        if 'file_modified' in values:
            value = values['file_modified']
            if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None:
                raise ValueError('Modification timestamp must have a timezone')
    return data['notes']


def inspect_release(project=PROJECT):
    root = safe_path(Path(project) / 'publication/current')
    files = _tree(root)
    receipt = _json(files['release.json'])
    _keys(receipt, {'schema_version', 'kind', 'base_url', 'snapshot_sha256', 'presentation_sha256'})
    if (receipt['schema_version'] != 1 or receipt['kind'] != 'public-pages-release'
            or receipt['base_url'] != BASE_URL):
        raise ValueError('Invalid public deployment hand-off')
    source, manifest, pages = inspect_snapshot(root / 'snapshot')
    if sha(source['manifest.json']) != receipt['snapshot_sha256'] or sha(files['presentation.json']) != receipt['presentation_sha256']:
        raise ValueError('Public release integrity mismatch')
    expected = {'release.json', 'presentation.json'} | {'snapshot/' + n for n in source}
    if set(files) != expected:
        raise ValueError('Public hand-off contains unexpected files')
    metadata = validate_presentation(_json(files['presentation.json']), receipt['snapshot_sha256'], pages)
    return root / 'snapshot', metadata, receipt, len(pages)


def export_release(vault, project=PROJECT):
    project = safe_path(project)
    vault = safe_path(vault)
    install_vault_write_guard(vault)
    destination = safe_path(project / 'publication/current')
    if within(vault, destination) or within(destination, vault):
        raise ValueError('Public hand-off must be disjoint from Vault')
    snapshot = current_snapshot(project)
    source, _, pages = inspect_snapshot(snapshot)
    registry = paired_registry(snapshot, source['manifest.json'], project)
    if registry is None or safe_path(registry['vault_root']) != vault:
        raise ValueError('Explicit Vault differs from completed publication')
    metadata = presentation_metadata(snapshot, source['manifest.json'], pages, project)
    presentation = payload({'schema_version': 1, 'snapshot_sha256': sha(source['manifest.json']), 'notes': metadata})
    validate_presentation(_json(presentation), sha(source['manifest.json']), pages)
    receipt = payload({'schema_version': 1, 'kind': 'public-pages-release', 'base_url': BASE_URL,
                       'snapshot_sha256': sha(source['manifest.json']), 'presentation_sha256': sha(presentation)})
    files = {'release.json': receipt, 'presentation.json': presentation,
             **{'snapshot/' + name: raw for name, raw in source.items()}}
    # All files here are generated; atomically promote the complete staged folder.
    # Replacement is allowed only for an independently validated former hand-off.
    staging_root = safe_path(project / '.generated/deployment')
    staging_root.mkdir(parents=True, exist_ok=True)
    # tempfile.mkdtemp uses a Windows owner-only ACL. Public generated content
    # must inherit the workspace's ordinary ACL so the owner's Git can read it.
    staging = safe_path(staging_root / ('pending-' + uuid.uuid4().hex))
    staging.mkdir()
    old = None
    try:
        for name, raw in files.items():
            target = safe_path(staging / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        if _tree(staging) != files or _tree(snapshot) != source:
            raise ValueError('Source/staging changed during export')
        if destination.exists():
            inspect_release(project)
            old = safe_path(staging_root / (staging.name + '-old'))
            destination.rename(old)
        try:
            staging.rename(destination)
        except OSError:
            if old is not None:
                old.rename(destination)
                old = None
            raise
        inspect_release(project)
    finally:
        # Only exact owned staging paths, proven disjoint from Vault, are removed.
        for temporary in (staging, old):
            if temporary is not None and temporary.exists():
                temporary = safe_path(temporary)
                if temporary.parent != staging_root or not within(project, temporary):
                    raise ValueError('Unsafe export staging cleanup')
                _tree(temporary)  # Reject nested reparse points before recursive cleanup.
                shutil.rmtree(temporary)
    return {'notes': len(pages), 'snapshot_sha256': sha(source['manifest.json']), 'public_release': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['export', 'check', 'build'])
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--verify-repeat', action='store_true')
    args = parser.parse_args()
    if args.command == 'export':
        if args.vault is None:
            raise ValueError('Export requires an explicit read-only Vault')
        result = export_release(args.vault)
    else:
        if args.vault is not None:
            raise ValueError('Cloud build/check must not access a Vault')
        from ci import repository_check
        repository_check()
        snapshot, metadata, receipt, count = inspect_release()
        result = {'passed': True, 'notes': count, 'base_url': receipt['base_url']}
        if args.command == 'build':
            public, report = build(snapshot, base_url=receipt['base_url'], metadata=metadata,
                                   verify_repeat=args.verify_repeat)
            inspect_release()  # Fail rather than upload changed source/presentation.
            result.update({'public': str(public), 'html_pages': report['html_pages'],
                           'internal_links_resources': report['internal_links_resources']})
            output = os.environ.get('GITHUB_OUTPUT')
            if output:
                with open(output, 'a', encoding='utf-8') as stream:
                    stream.write('public=' + str(public) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('PRODUCTION_FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
