"""Persistent workspace state: per-scope cwd bindings and named workspace aliases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


def _get_workspaces_json_path() -> Path:
    return Path.home() / ".lark-acp-bridge" / "workspaces.json"


class WorkspaceStore:
    """Manages per-scope cwd bindings and named workspace aliases.

    Persisted to ``~/.lark-acp-bridge/workspaces.json``:
    ```json
    {
      "cwd_by_scope": {"user:xxx": "/path/to/dir"},
      "named": {"my-project": "/path/to/dir"}
    }
    ```
    """

    def __init__(self, path: Path | None = None):
        self._path = path or _get_workspaces_json_path()
        self._cwd_by_scope: dict[str, str] = {}
        self._named: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Loading / saving
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._cwd_by_scope = {str(k): str(v) for k, v in data.get("cwd_by_scope", {}).items()}
            self._named = {str(k): str(v) for k, v in data.get("named", {}).items()}
            logger.info("workspace-store-loaded", path=str(self._path),
                        scopes=len(self._cwd_by_scope), named=len(self._named))
        except Exception as exc:
            logger.warning("workspace-store-load-failed", error=str(exc), exc_info=True)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "cwd_by_scope": self._cwd_by_scope,
            "named": self._named,
        }
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # Per-scope cwd
    # ------------------------------------------------------------------ #

    def get_cwd(self, scope: str, default: str = "") -> str:
        """Return cwd for the given scope, or default if not set."""
        return self._cwd_by_scope.get(scope, default)

    def set_cwd(self, scope: str, path: str) -> None:
        """Set cwd for a scope and persist."""
        self._cwd_by_scope[scope] = path
        self._save()

    def clear_cwd(self, scope: str) -> None:
        """Remove cwd for a scope and persist."""
        self._cwd_by_scope.pop(scope, None)
        self._save()

    # ------------------------------------------------------------------ #
    # Named workspaces
    # ------------------------------------------------------------------ #

    def save_named(self, name: str, path: str) -> None:
        """Save a named workspace alias and persist."""
        self._named[name] = path
        self._save()

    def get_named(self, name: str) -> str | None:
        """Return the path for a named workspace alias, or None."""
        return self._named.get(name)

    def list_named(self) -> dict[str, str]:
        """Return all named workspace aliases as {name: path}."""
        return dict(self._named)

    def remove_named(self, name: str) -> bool:
        """Remove a named workspace alias. Returns True if it existed."""
        if name in self._named:
            del self._named[name]
            self._save()
            return True
        return False
