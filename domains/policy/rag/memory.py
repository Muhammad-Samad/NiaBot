"""In-memory, per-session conversation history with expiry.

Good enough for a single-process deployment. When this is merged into
NiaBot, swap it for NiaBot's own session/conversation store."""
from __future__ import annotations

import threading
import time
from collections import deque

from config.policy import settings


class ConversationMemory:
    def __init__(self, max_turns: int | None = None, ttl_minutes: int | None = None):
        self._max_messages = 2 * (max_turns or settings.history_turns)
        self._ttl = 60 * (ttl_minutes or settings.session_ttl_minutes)
        self._sessions: dict[str, tuple[float, deque]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> list[dict]:
        with self._lock:
            self._evict_expired()
            entry = self._sessions.get(session_id)
            return list(entry[1]) if entry else []

    def append(self, session_id: str, user_msg: str, bot_msg: str) -> None:
        with self._lock:
            _, history = self._sessions.get(session_id, (0.0, deque(maxlen=self._max_messages)))
            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": bot_msg})
            self._sessions[session_id] = (time.time(), history)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _evict_expired(self) -> None:
        now = time.time()
        for sid in [s for s, (ts, _) in self._sessions.items() if now - ts > self._ttl]:
            del self._sessions[sid]
