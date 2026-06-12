"""macOS launchd agent adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import DaemonAdapter

_LABEL = "com.lark-acp-bridge"

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
{args_xml}
  </array>
  <key>WorkingDirectory</key>
  <string>{work_dir}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>{log_dir}/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/stderr.log</string>
</dict>
</plist>
"""


class LaunchdAdapter(DaemonAdapter):
    """Manage lark-acp-bridge as a macOS launchd user agent."""

    SERVICE_NAME = _LABEL

    @property
    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"

    @property
    def _log_dir(self) -> Path:
        return Path.home() / ".lark-acp-bridge" / "logs"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True)

    def install(self, exe: str, args: list[str], work_dir: str) -> None:
        self._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        args_xml = "\n".join(f"    <string>{a}</string>" for a in args)
        content = _PLIST_TEMPLATE.format(
            label=_LABEL,
            exe=exe,
            args_xml=args_xml,
            work_dir=work_dir,
            log_dir=str(self._log_dir),
        )
        self._plist_path.write_text(content, encoding="utf-8")
        self._run("launchctl", "load", str(self._plist_path))

    def uninstall(self) -> None:
        self._run("launchctl", "unload", str(self._plist_path))
        if self._plist_path.exists():
            self._plist_path.unlink()

    def start(self) -> None:
        self._run("launchctl", "start", _LABEL)

    def stop(self) -> None:
        self._run("launchctl", "stop", _LABEL)

    def status(self) -> str:
        if not self._plist_path.exists():
            return "not-installed"
        result = self._run("launchctl", "list", _LABEL)
        if result.returncode != 0:
            return "stopped"
        # launchctl list output: PID lines look like "12345\t0\tcom.lark-acp-bridge"
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 1 and parts[0].strip().isdigit():
                return "running"
        return "stopped"
