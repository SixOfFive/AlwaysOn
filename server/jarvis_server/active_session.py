"""Process-wide hook so tools that produce deferred output (timer ringing,
file-watch notifications, …) can push a Say back to the client without
threading a session reference through every layer.

Single-client assumption: we track one active Session. If a second
client connects, it replaces the first as the recipient of future
deferred Says. Good enough for one user on one box.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from jarvis_shared import Say

if TYPE_CHECKING:
    from jarvis_server.session import Session

log = logging.getLogger(__name__)


class ActiveSession:
    _current: Optional["Session"] = None
    _lock = asyncio.Lock()

    @classmethod
    def set(cls, session: "Session") -> None:
        cls._current = session

    @classmethod
    def clear(cls, session: "Session") -> None:
        if cls._current is session:
            cls._current = None

    @classmethod
    async def push_say(cls, text: str) -> bool:
        """Send a Say to the current session. Returns True if delivered."""
        s = cls._current
        if s is None:
            log.info("push_say: no active session, dropping %r", text)
            return False
        async with cls._lock:
            try:
                await s.ws.send_text(Say(text=text).model_dump_json())
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("push_say failed: %s", exc)
                return False
