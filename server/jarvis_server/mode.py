"""Voice-toggleable always-listening mode.

When the user is in "always mode" the server skips the trigger filter
and treats every transcribed utterance as a command. They opt in by
saying a phrase like "computer, always mode on" and opt out with
"always mode off" (no trigger word needed for the off-switch since
always-mode is in effect).

`parse_mode_toggle()` recognizes a small grammar around the word
"always" plus on/off and "trigger only" as the explicit return-to-
default phrase. The trigger phrase is parameterized so users who set
the wake word to "jarvis" can say "always jarvis mode" if they like.
"""

from __future__ import annotations

import functools
import re


@functools.lru_cache(maxsize=8)
def _patterns(trigger_phrase: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    phrase = re.escape(trigger_phrase.strip().lower())
    enable = re.compile(
        rf"^(?:enable\s+|turn\s+on\s+)?always"
        rf"(?:\s+(?:{phrase}\s+)?(?:mode|listen(?:ing)?))?"
        rf"(?:\s+on)?[\s.!?]*$",
        re.IGNORECASE,
    )
    disable = re.compile(
        rf"^(?:"
        rf"(?:disable|stop|turn\s+off|exit|end)\s+always"
        rf"(?:\s+(?:{phrase}\s+)?(?:mode|listen(?:ing)?))?"
        rf"|always(?:\s+(?:{phrase}\s+)?(?:mode|listen(?:ing)?))?\s+off"
        rf"|trigger(?:\s+word)?\s+only"
        rf")[\s.!?]*$",
        re.IGNORECASE,
    )
    return enable, disable


def parse_mode_toggle(text: str, trigger_phrase: str = "computer") -> bool | None:
    """Returns True to enable always-mode, False to disable, or None if
    `text` isn't a mode-toggle command."""
    text = text.strip()
    if not text:
        return None
    enable, disable = _patterns(trigger_phrase)
    # Check disable first — "always mode off" otherwise matches the
    # enable pattern's optional tail before reaching the "off".
    if disable.match(text):
        return False
    if enable.match(text):
        return True
    return None


def reply_for_toggle(enabled: bool, trigger_phrase: str) -> str:
    """Spoken confirmation after a successful toggle."""
    if enabled:
        return "Always mode on. I'll treat anything you say as a command."
    return f"Always mode off. Say {trigger_phrase} before commands."
