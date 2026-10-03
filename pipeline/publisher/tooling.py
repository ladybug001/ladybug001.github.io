"""Pinned project-local Hugo installer and runtime checks, never global installs."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import io
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.request import urlopen
import zipfile

from . import __version__
from .safety import within

PROJECT = Path(__file__).resolve().parents[2]


def lock(project=PROJECT):
    data = tomllib.loads((Path(project) / "pipeline/toolchain.toml").read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", data["hugo"]):
        raise ValueError("Invalid toolchain lock")
    return data


def local_hugo(project=PROJECT):
    return Path(project) / ".local/tools/hugo" / lock(project)["hugo"] / "bin" / ("hugo.exe" if os.name == "nt" else "hugo")


def runtime_check(project=PROJECT):
    data = lock(project)
    if platform.python_version() != data["python"] or __version__ != data["publisher"]:
        raise ValueError("Python/publisher version differs from toolchain.toml")
    text = (Path(project) / "pipeline" / data["dependencies"]).read_text(encoding="utf-8")
    dependencies = dict(re.findall(r"^([a-zA-Z0-9_-]+)==([^\s\\]+)", text, re.MULTILINE))
    if not dependencies:
        raise ValueError("Dependency lock has no exact versions")
    for name, expected in dependencies.items():
        if version(name) != expected:
            raise ValueError("Installed dependency differs from lock: " + name)
    return data


def _safe_output(project, path):
    if not within(Path(project) / ".local/tools/hugo", path):
        raise ValueError("Hugo tool output must remain project-local")
    for value in (path, *path.parents):
        if value.is_symlink() or (hasattr(value, "is_junction") and value.is_junction()):
            raise ValueError("Tool output must not follow symlinks/junctions")


def _binary(raw, windows):
    """Read only the expected regular binary; never extract an archive tree."""
    if windows:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = [e for e in archive.infolist() if e.filename == "hugo.exe"]
            if len(entries) != 1 or entries[0].file_size > 128 * 1024 * 1024 or entries[0].is_dir():
                raise ValueError("Archive must contain one bounded Hugo executable")
            return archive.read(entries[0])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        entries = [e for e in archive.getmembers() if e.name == "hugo"]
        if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 128 * 1024 * 1024:
            raise ValueError("Archive must contain one regular bounded Hugo executable")
        with archive.extractfile(entries[0]) as stream:
            return stream.read()


def install_hugo(project=PROJECT):
    project = Path(project).absolute()
    data = lock(project)
    if platform.machine().casefold() not in {"amd64", "x86_64"} or sys.platform not in {"win32", "linux"}:
        raise ValueError("Pinned installer supports Windows/Linux amd64 only")
    windows = os.name == "nt"
    system = "windows" if windows else "linux"
    expected = data["hugo_" + system + "_amd64_sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Invalid pinned archive SHA-256")
    filename = f"hugo_{data['hugo']}_{system}-amd64." + ("zip" if windows else "tar.gz")
    folder = project / ".local/tools/hugo" / data["hugo"]
    # Retain the existing Windows cache name; do not redownload a verified cache.
    archive_path = folder / ("hugo.zip" if windows else filename)
    _safe_output(project, archive_path)
    folder.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if archive_path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Hugo archive exceeds download limit")
        raw = archive_path.read_bytes()
    else:
        url = f"https://github.com/gohugoio/hugo/releases/download/v{data['hugo']}/{filename}"
        with urlopen(url, timeout=30) as response:
            if not response.geturl().startswith("https://"):
                raise ValueError("Hugo release download lost HTTPS")
            raw = response.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError("Hugo archive exceeds download limit")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Hugo archive SHA-256 mismatch")
    executable = local_hugo(project)
    _safe_output(project, executable)
    binary = _binary(raw, windows)
    executable.parent.mkdir(parents=True, exist_ok=True)
    if executable.exists():
        if executable.read_bytes() != binary:
            raise ValueError("Existing Hugo binary differs from verified archive; refusing overwrite")
    else:
        descriptor, pending = tempfile.mkstemp(prefix=".pending-hugo-", dir=executable.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(binary)
        if not windows:
            os.chmod(pending, 0o755)
        Path(pending).rename(executable)
    if not archive_path.exists():
        descriptor, pending = tempfile.mkstemp(prefix=".pending-archive-", dir=folder)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        Path(pending).rename(archive_path)
    result = subprocess.run([str(executable), "version"], capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
    if not re.search(r"\bhugo v" + re.escape(data["hugo"]) + r"(?:-|\s)", result.stdout):
        raise ValueError("Installed Hugo reports a different version")
    return executable
