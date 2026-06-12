"""Abstract base class for daemon/service adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DaemonAdapter(ABC):
    """Platform-agnostic interface for installing and managing a background service."""

    SERVICE_NAME = "lark-acp-bridge"

    @abstractmethod
    def install(self, exe: str, args: list[str], work_dir: str) -> None:
        """Generate and install the service unit file.

        ``exe`` is the Python executable path.
        ``args`` are the CLI arguments to pass (e.g. ``["-m", "lark_acp_bridge.cli.main", "start"]``).
        ``work_dir`` is the working directory for the service process.
        """

    @abstractmethod
    def uninstall(self) -> None:
        """Remove the service unit file and disable the service."""

    @abstractmethod
    def start(self) -> None:
        """Start the background service."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the background service."""

    @abstractmethod
    def status(self) -> str:
        """Return a human-readable status string: 'running', 'stopped', or 'not-installed'."""
