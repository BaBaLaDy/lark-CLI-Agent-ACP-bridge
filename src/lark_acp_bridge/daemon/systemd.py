"""Linux systemd --user service adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import DaemonAdapter

_UNIT_TEMPLATE = """\
[Unit]
Description=Lark ACP Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={work_dir}
ExecStart={exe} {args}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


class SystemdAdapter(DaemonAdapter):
    """Manage lark-acp-bridge as a systemd --user service."""

    @property
    def _unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{self.SERVICE_NAME}.service"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True,
        )

    def install(self, exe: str, args: list[str], work_dir: str) -> None:
        self._unit_path.parent.mkdir(parents=True, exist_ok=True)
        content = _UNIT_TEMPLATE.format(
            work_dir=work_dir,
            exe=exe,
            args=" ".join(args),
        )
        self._unit_path.write_text(content, encoding="utf-8")
        self._run("daemon-reload")
        self._run("enable", self.SERVICE_NAME)

    def uninstall(self) -> None:
        self._run("stop", self.SERVICE_NAME)
        self._run("disable", self.SERVICE_NAME)
        if self._unit_path.exists():
            self._unit_path.unlink()
        self._run("daemon-reload")

    def start(self) -> None:
        self._run("start", self.SERVICE_NAME)

    def stop(self) -> None:
        self._run("stop", self.SERVICE_NAME)

    def status(self) -> str:
        if not self._unit_path.exists():
            return "not-installed"
        result = self._run("is-active", self.SERVICE_NAME)
        output = result.stdout.strip()
        if output == "active":
            return "running"
        return "stopped"
