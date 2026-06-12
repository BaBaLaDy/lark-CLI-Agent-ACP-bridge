"""Daemon mode adapters for running lark-acp-bridge as a background system service."""

from .base import DaemonAdapter
from .factory import get_daemon_adapter

__all__ = ["DaemonAdapter", "get_daemon_adapter"]
