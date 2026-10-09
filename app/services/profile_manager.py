from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from app.models import SSHHost


class ProfileManager:
    def __init__(self, ssh_config_path: str = "~/.ssh/config") -> None:
        self.ssh_config_path = Path(ssh_config_path).expanduser()

    def discover_ssh_hosts(self) -> list[SSHHost]:
        if not self.ssh_config_path.exists():
            return []
        hosts: dict[str, SSHHost] = {}
        self._parse_file(self.ssh_config_path, hosts, depth=0)
        aliases = []
        for alias, host in hosts.items():
            if not any(char in alias for char in "*?!") and alias != "*":
                aliases.append(host)
        return sorted(aliases, key=lambda item: item.alias)

    def _parse_file(self, path: Path, hosts: dict[str, SSHHost], depth: int) -> None:
        if depth > 5 or not path.exists():
            return
        current: SSHHost | None = None
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(" ")
            value = value.strip().strip('"')
            key = key.lower()
            if key == "host":
                current = None
                for alias in value.split():
                    if alias.startswith("-") or "*" in alias or "?" in alias:
                        continue
                    current = SSHHost(alias=alias)
                    hosts[alias] = current
            elif key == "include" and current is None:
                for pattern in value.split():
                    if not pattern:
                        continue
                    include = Path(pattern).expanduser()
                    if not include.is_absolute():
                        include = path.parent / include
                    for item in sorted(include.parent.glob(include.name)) if any(char in include.name for char in "*?") else [include]:
                        self._parse_file(item, hosts, depth + 1)
            elif current is not None:
                if key == "hostname":
                    current.host = value
                elif key == "user":
                    current.user = value
                elif key == "port":
                    try:
                        current.port = int(value)
                    except ValueError:
                        pass
                elif key == "identityfile":
                    current.identity_file = str(Path(value).expanduser())
