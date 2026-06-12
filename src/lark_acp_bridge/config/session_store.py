"""Lightweight session index: tracks session IDs so /resume can reconnect.

The actual conversation content is stored by the agent subprocess itself.
This store only records the session_id → metadata mapping so we know which
sessions to offer for resume and can call ``load_session(session_id)`` on
the agent to reconnect.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


def _get_sessions_json_path() -> Path:
    return Path.home() / ".lark-acp-bridge" / "sessions.json"


class SessionStore:
    """Persistent index of ACP sessions grouped by working directory.

    Persisted to ``~/.lark-acp-bridge/sessions.json``:
    ```json
    {
      "sessions": [
        {
          "session_id": "abc-123",
          "agent_name": "claude",
          "cwd": "/path/to/project",
          "created_at": "2025-01-15T10:30:00+00:00",
          "preview": "帮我重构 auth 模块"
        }
      ]
    }
    ```
    """

    MAX_ENTRIES_PER_CWD = 20

    def __init__(self, path: Path | None = None):
        self._path = path or _get_sessions_json_path()
        self._sessions: list[dict[str, Any]] = []
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
            self._sessions = list(data.get("sessions", []))
            logger.debug("session-store-loaded", path=str(self._path), count=len(self._sessions))
        except Exception as exc:
            logger.warning("session-store-load-failed", error=str(exc), exc_info=True)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump({"sessions": self._sessions}, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #

    def record(
        self,
        session_id: str,
        agent_name: str = "",
        cwd: str = "",
        preview: str = "",
    ) -> None:
        """Record a new session entry. Deduplicates by session_id."""
        # Remove any existing entry with the same session_id
        self._sessions = [s for s in self._sessions if s.get("session_id") != session_id]
        entry: dict[str, Any] = {
            "session_id": session_id,
            "agent_name": agent_name,
            "cwd": cwd,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "preview": preview[:80] if preview else "",
        }
        # Insert at the front (most recent first)
        self._sessions.insert(0, entry)
        # Cap total entries
        max_total = self.MAX_ENTRIES_PER_CWD * 10
        if len(self._sessions) > max_total:
            self._sessions = self._sessions[:max_total]
        self._save()

    # ------------------------------------------------------------------ #
    # Querying
    # ------------------------------------------------------------------ #

    def list_for_cwd(self, cwd: str) -> list[dict[str, Any]]:
        """Return session entries matching the given working directory."""
        cwd_norm = str(Path(cwd).resolve()) if cwd else ""
        results = []
        for s in self._sessions:
            s_cwd = s.get("cwd", "")
            if s_cwd and cwd_norm and str(Path(s_cwd).resolve()) == cwd_norm:
                results.append(s)
        return results[:self.MAX_ENTRIES_PER_CWD]

    def remove(self, session_id: str) -> bool:
        """Remove a session entry by ID. Returns True if it existed."""
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s.get("session_id") != session_id]
        if len(self._sessions) < before:
            self._save()
            return True
        return False
