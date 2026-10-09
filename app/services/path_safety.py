from __future__ import annotations

import os
import stat
from pathlib import Path


def validate_name(name: str, *, windows: bool = False) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ValueError("Invalid file name")
    if windows and (any(c in name for c in '\\:<>"|?*') or name.endswith((" ", "."))):
        raise ValueError("Invalid Windows file name")
    if windows and name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise ValueError("Reserved Windows file name")


def local_path(root: Path | tuple[Path, ...], path: str | Path, *, base: Path | None = None) -> Path:
    roots = (root,) if isinstance(root, Path) else root
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (base or roots[0]) / candidate
    # Check the original components before resolve() follows any links.
    for component in (*reversed(candidate.parents), candidate):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("Symbolic links and junctions are not allowed")
    try:
        resolved = candidate.resolve(strict=False)
        for allowed in roots:
            if resolved.is_relative_to(allowed):
                relative = resolved.relative_to(allowed)
                break
        else:
            raise ValueError("No matching local root")
    except (ValueError, RuntimeError) as exc:
        raise ValueError(f"Path is outside the allowed local root: {', '.join(map(str, roots))}") from exc
    for name in relative.parts:
        validate_name(name, windows=os.name == "nt")
    return resolved


def remote_kind(attrs) -> tuple[bool, bool]:
    import asyncssh

    permissions = attrs.permissions or 0
    return (
        attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY or stat.S_ISDIR(permissions),
        attrs.type == asyncssh.FILEXFER_TYPE_SYMLINK or stat.S_ISLNK(permissions),
    )
