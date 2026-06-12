"""Windows Task Scheduler (schtasks) adapter."""

from __future__ import annotations

import os
import subprocess

from .base import DaemonAdapter

_TASK_NAME = "LarkACPBridge"


class SchtasksAdapter(DaemonAdapter):
    """Manage lark-acp-bridge via Windows Task Scheduler (schtasks.exe).

    The task is registered to run at user logon. schtasks has no native
    "restart on failure" support; failures are logged but not auto-retried.
    """

    SERVICE_NAME = _TASK_NAME

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["schtasks", *args],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )

    def install(self, exe: str, args: list[str], work_dir: str) -> None:
        # Build the command line — schtasks /TR takes a single quoted string.
        tr = f'"{exe}" {" ".join(args)}'
        username = os.environ.get("USERNAME", "")
        result = self._run(
            "/Create",
            "/TN", _TASK_NAME,
            "/TR", tr,
            "/SC", "ONLOGON",
            "/RL", "LIMITED",
            "/RU", username,
            "/F",  # force overwrite if exists
        )
        if result.returncode != 0:
            raise RuntimeError(f"schtasks /Create failed: {result.stderr.strip()}")

    def uninstall(self) -> None:
        self.stop()
        self._run("/Delete", "/TN", _TASK_NAME, "/F")

    def start(self) -> None:
        result = self._run("/Run", "/TN", _TASK_NAME)
        if result.returncode != 0:
            raise RuntimeError(f"schtasks /Run failed: {result.stderr.strip()}")

    def stop(self) -> None:
        self._run("/End", "/TN", _TASK_NAME)

    def status(self) -> str:
        result = self._run("/Query", "/TN", _TASK_NAME, "/FO", "CSV", "/NH")
        if result.returncode != 0:
            return "not-installed"
        # CSV output: "TaskName","Next Run Time","Status"
        for line in result.stdout.strip().splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 3 and parts[0] == _TASK_NAME:
                state = parts[2].lower()
                if "running" in state:
                    return "running"
                return "stopped"
        return "not-installed"
