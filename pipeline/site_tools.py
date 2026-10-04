#!/usr/bin/env python3
"""Install pinned Stack and Extended Hugo locally; no global changes or upload."""
from __future__ import annotations

import io
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import tomllib
from urllib.request import urlopen
import zipfile

from publisher.lifecycle import safe_path
from publisher.snapshot import _install, payload, sha
from publisher.tooling import PROJECT, install_hugo, lock


def site_lock(project=PROJECT):
    data = tomllib.loads((Path(project) / 'site/theme.lock.toml').read_text(encoding='utf-8'))
    if (data.get('schema_version') != 1 or data.get('theme') != 'stack'
            or not re.fullmatch(r'[a-f0-9]{40}', data['commit'])
            or not re.fullmatch(r'\d+\.\d+\.\d+', data['hugo'])
            or not re.fullmatch(r'\d+\.\d+\.\d+', data['version'])):
        raise ValueError('Invalid site toolchain lock')
    return data


def download(url, expected, cache):
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('Invalid archive checksum')
    cache = safe_path(cache)
    if cache.exists():
        if cache.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Cached download exceeds budget')
        raw = cache.read_bytes()
    else:
        with urlopen(url, timeout=30) as response:
            if not response.geturl().startswith('https://'):
                raise ValueError('Download lost HTTPS')
            raw = response.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or sha(raw) != expected:
        raise ValueError('Archive size/checksum mismatch')
    if not cache.exists():
        atomic_write_bytes(cache, raw)
    return raw


def atomic_write_bytes(path, raw):
    import tempfile
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(prefix='.pending-tool-', dir=path.parent)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
    Path(pending).rename(path)


def theme_files(raw, commit):
    root = 'hugo-theme-stack-' + commit + '/'
    result = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if len(archive.infolist()) > 4096:
            raise ValueError('Theme archive file budget exceeded')
        for entry in archive.infolist():
            name = entry.filename
            parts = PurePosixPath(name).parts
            if (not name.startswith(root) or '\\' in name or '..' in parts
                    or any(':' in p for p in parts) or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe theme archive entry')
            relative = name[len(root):]
            if entry.is_dir():
                continue
            if not (relative in {'LICENSE', 'theme.toml'} or relative.split('/')[0] in
                    {'assets', 'config', 'data', 'i18n', 'layouts', 'static'}):
                continue # Never install the demo, CI workflows, or source content.
            if entry.file_size > 16 * 1024 * 1024 or relative in result:
                raise ValueError('Theme archive entry exceeds budget or is duplicate')
            result[relative] = archive.read(entry)
            if sum(map(len, result.values())) > 64 * 1024 * 1024:
                raise ValueError('Expanded theme exceeds budget')
    if not {'theme.toml', 'LICENSE'} <= result.keys():
        raise ValueError('Incomplete Stack archive')
    return result


def install(project=PROJECT):
    project = Path(project).absolute()
    data = site_lock(project)
    if platform.machine().casefold() not in {'amd64', 'x86_64'} or sys.platform not in {'win32', 'linux'}:
        raise ValueError('Site tools support Windows/Linux amd64 only')
    cache = safe_path(project / '.local/theme-downloads')
    raw = download('https://codeload.github.com/CaiJimmy/hugo-theme-stack/zip/' + data['commit'],
                   data['archive_sha256'], cache / ('stack-v' + data['version'] + '.zip'))
    files = theme_files(raw, data['commit'])
    files['manifest.json'] = payload({'commit': data['commit'], 'files': {n: sha(r) for n, r in files.items()}})
    # Hugo expects themesDir/stack. Store the fixed package as a named child.
    # Keep the existing named package path without installing a second copy.
    theme_root = project / '.local/themes' / ('stack-' + sha(files['manifest.json'])[:24] + '-hugo')
    themed = {'stack/' + name: value for name, value in files.items()}
    themed['manifest.json'] = payload({'commit': data['commit'], 'files': {n: sha(r) for n, r in themed.items()}})
    themes = _install(themed, theme_root, 'themes-')
    toolchain = lock(project)
    for key in ('hugo', 'hugo_windows_amd64_sha256', 'hugo_linux_amd64_sha256'):
        if data[key] != toolchain[key]:
            raise ValueError('Theme and shared Hugo toolchain pins disagree')
    executable = install_hugo(project)
    return executable, themes


if __name__ == '__main__':
    try:
        executable, themes = install()
        print('Pinned Extended Hugo: ' + str(executable))
        print('Pinned Stack package: ' + str(themes))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('SITE_TOOLS_FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
