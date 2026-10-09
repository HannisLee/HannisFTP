from __future__ import annotations

import fnmatch
import glob
import re
import shlex
from pathlib import Path

from app.models import SSHHost


class ProfileManager:
    def __init__(self, ssh_config_path: str = "~/.ssh/config") -> None:
        self.ssh_config_path = Path(ssh_config_path).expanduser()

    def discover_ssh_hosts(self) -> list[SSHHost]:
        lines = list(self._read(self.ssh_config_path, set()))
        aliases = {
            alias for key, values in lines if key == "host" for alias in values
            if not any(char in alias for char in "*?!")
        }
        result = []
        for alias in sorted(aliases):
            values: dict[str, str] = {}
            matching = True
            for key, args in lines:
                if key == "host":
                    positive = [p for p in args if not p.startswith("!")]
                    negative = [p[1:] for p in args if p.startswith("!")]
                    matching = any(fnmatch.fnmatchcase(alias.lower(), p.lower()) for p in positive) and not any(
                        fnmatch.fnmatchcase(alias.lower(), p.lower()) for p in negative
                    )
                elif key == "match":
                    # Discovery never executes Match exec. AsyncSSH evaluates
                    # the full configuration when the user connects.
                    matching = False
                elif matching and args:
                    values.setdefault(key, args[0])
            port = values.get("port")
            result.append(SSHHost(
                alias=alias, host=values.get("hostname"), user=values.get("user"),
                port=int(port) if port and port.isdigit() else None,
                identity_file=str(Path(values["identityfile"]).expanduser()) if "identityfile" in values else None,
                source=str(self.ssh_config_path),
            ))
        return result

    def _read(self, path: Path, stack: set[Path]):
        path = path.resolve()
        if path in stack or len(stack) >= 16 or not path.is_file():
            return
        stack.add(path)
        try:
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"^\s*([^\s=]+)\s*(?:=\s*)?(.*)$", raw)
                if not match or match[1].startswith("#"):
                    continue
                try:
                    args = shlex.split(match[2], comments=True)
                except ValueError:
                    continue
                key = match[1].lower()
                if key == "include":
                    for pattern in args:
                        include = Path(pattern).expanduser()
                        if not include.is_absolute():
                            include = self.ssh_config_path.parent / include
                        for filename in sorted(glob.glob(str(include))):
                            yield from self._read(Path(filename), stack)
                else:
                    yield key, args
        finally:
            stack.remove(path)
