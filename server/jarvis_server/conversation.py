"""Per-session conversation history.

Holds the running message list (user + assistant + tool turns) that we
replay on every LLM call so follow-ups like "and what about Tokyo?" can
resolve against the previous turn's context. Fast-path replies are also
recorded so a fast-path interaction followed by an LLM-routed follow-up
still flows.

History is unbounded by turn count — only the 5-minute idle reset trims
it. If a single session blows the LLM context window, that's a signal
to either talk less or shorten the gap (we won't silently drop turns).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Conversation:
    messages: list[dict[str, Any]] = field(default_factory=list)
    # monotonic seconds — wall-clock skew safe.
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self.touch()

    def add_overheard(self, text: str) -> None:
        """Record an utterance the mic picked up but the user wasn't
        addressing the assistant (no trigger word). Tagged so the LLM
        treats it as ambient context, not a question to answer."""
        self.messages.append({
            "role": "user",
            "content": f"[overheard] {text}",
        })
        self.touch()

    def add_assistant_text(self, text: str) -> None:
        """Record a final spoken reply (no tool_calls). Used by both
        fast-path returns and the LLM's terminal turn."""
        self.messages.append({"role": "assistant", "content": text})
        self.touch()

    def extend(self, items: list[dict[str, Any]]) -> None:
        """Append intermediate tool-loop messages (assistant with
        tool_calls, then `tool` role results). Called by the LLM router
        as the loop runs so the next turn can see what was inferred and
        what came back."""
        self.messages.extend(items)
        self.touch()

    def reset_if_idle(self, threshold_sec: float) -> bool:
        """If the last activity was >= threshold_sec ago and history is
        non-empty, clear it. Returns True if a reset happened."""
        if not self.messages:
            # Touch so an empty conversation doesn't drift its baseline.
            self.touch()
            return False
        if (time.monotonic() - self.last_activity) >= threshold_sec:
            self.messages.clear()
            self.touch()
            return True
        return False
