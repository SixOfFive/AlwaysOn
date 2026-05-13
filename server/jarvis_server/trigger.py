"""Transcript-based wake-phrase filter.

When a client streams audio without doing its own on-device wake-word
detection (e.g. the Android client, which just runs VAD), it sends an
empty `Wake.keyword`. In that case the server transcribes the segment
and then has to decide whether the user was actually addressing the
assistant. The cheapest signal is the literal trigger phrase
("computer", "jarvis", etc.) appearing near the start of the transcript.

When a client did its own wake-word check (`Wake.keyword` non-empty),
we trust it and skip the transcript filter entirely.

The phrase is configurable via Config.trigger_phrase (or
JARVIS_TRIGGER) — see `pattern_for()`.
"""

from __future__ import annotations

import functools
import re


@functools.lru_cache(maxsize=8)
def pattern_for(phrase: str) -> re.Pattern[str]:
    """Compile a trigger regex for `phrase`. Allows common preambles
    ("hey computer", "ok computer", "okay computer", or the bare word)
    and trailing punctuation/whitespace. Cached so the Session can call
    every turn without re-compiling."""
    escaped = re.escape(phrase.strip().lower())
    return re.compile(
        rf"\b(?:hey\s+|ok\s+|okay\s+)?{escaped}\b[\s,.\-:;!?]*",
        re.IGNORECASE,
    )


def extract(text: str, phrase: str = "computer") -> tuple[str, str] | None:
    """Split the utterance around the trigger phrase.

    Returns `(pre_trigger, command)` where:
    - `pre_trigger` is anything the user said *before* the trigger
      ("Tokyo sucks." in "Tokyo sucks. Computer, what's the weather
      there?"). Empty string if the trigger was at the very start.
    - `command` is the text after the trigger.

    Returns None if the trigger isn't present or there's nothing after
    it. Caller is responsible for recording `pre_trigger` as overheard
    context if it's non-empty — that way a follow-up like "what about
    there?" still has the prior context to lean on.
    """
    m = pattern_for(phrase).search(text)
    if not m:
        return None
    pre = text[:m.start()].strip()
    command = text[m.end():].strip()
    if not command:
        return None
    return pre, command
