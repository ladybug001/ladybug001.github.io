from __future__ import annotations

import os
import sys
from pathlib import Path


def within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def install_vault_write_guard(vault: Path) -> None:
    """Fail closed on filesystem mutation attempts inside Vault in this process.

    This is a defense for this checker, not an OS sandbox for arbitrary programs.
    The checker does not start subprocesses or native plugins.
    """
    root = vault.resolve()
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def protect(value: object) -> None:
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value))
            if within(root, path):
                raise PermissionError(f"Vault is read-only: {path}")

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event == "open":
            path, mode, flags = args[:3]
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & write_flags
            ):
                protect(path)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            protect(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            protect(args[0])
            protect(args[1])

    sys.addaudithook(audit)
