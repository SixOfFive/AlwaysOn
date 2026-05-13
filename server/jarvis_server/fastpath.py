"""Server-side fast-path handlers — deterministic, sub-100 ms answers
that skip the LLM round-trip entirely.

Every handler here is a stateless function that takes the user
transcript (already stripped of the trigger word, where applicable)
plus whatever extra context it needs, and returns either a reply
string (matched + handled) or None (not my problem, try the next one).

`try_all()` runs them in priority order and is what Router.handle()
calls before falling back to the existing tool fast-paths and then
the LLM. The order matters — more specific patterns first, so
"square root of 9" doesn't get caught by some generic math handler.

When you add a new handler:
- Anchor patterns to ^/$ so a partial match doesn't fire on a
  free-form sentence
- Put the heavier / network-y handlers last (Wikipedia is the only
  one here that hits a remote API)
- Return None liberally — false positives are way worse than false
  negatives, because the LLM is the safety net
"""

from __future__ import annotations

import logging
import math
import random
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from jarvis_server.conversation import Conversation
from jarvis_server.tools import ToolRegistry

if TYPE_CHECKING:
    from jarvis_server.ollama_router import OllamaRouter

log = logging.getLogger(__name__)


# --- entry point ------------------------------------------------------

async def try_all(
    text: str,
    conversation: Conversation,
    registry: ToolRegistry,
    llm: "OllamaRouter | None",
) -> tuple[str, str] | None:
    """Run every fast-path matcher in order. Returns (handler_name, reply)
    on first match, else None."""
    text = text.strip()
    if not text:
        return None

    # Order: static / pure / cheap → date math → math / units → spell →
    # Wikipedia (only one that hits the network).
    attempts = (
        ("chitchat",        _try_chitchat(text)),
        ("help",            _try_help(text)),
        ("repeat",          _try_repeat(text, conversation)),
        ("model_info",      _try_model_info(text, llm)),
        ("dice",            _try_dice(text)),
        ("math",            _try_math(text)),
        ("unit_conversion", _try_unit_conversion(text)),
        ("spell",           _try_spell(text)),
        ("date_math",       _try_date_math(text)),
    )
    for name, reply in attempts:
        if reply is not None:
            return name, reply

    # Network-touching handler last so we don't pay its cost on every
    # transcript that didn't match anything cheap.
    reply = await _try_wikipedia(text, registry)
    if reply is not None:
        return "wikipedia", reply

    return None


# --- chit-chat --------------------------------------------------------

_CHITCHAT: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"^(?:hi|hello|hey|yo|howdy)[\s.!]*$", re.I),
     ["Hi.", "Hello.", "Hey there."]),
    (re.compile(r"^good\s+morning[\s.!]*$", re.I),
     ["Good morning."]),
    (re.compile(r"^good\s+(?:afternoon|evening)[\s.!]*$", re.I),
     ["Good day to you too."]),
    (re.compile(r"^good\s+night[\s.!]*$", re.I),
     ["Good night."]),
    (re.compile(r"^(?:thanks|thank\s+you|thx|cheers|appreciate\s+it)[\s.!]*$", re.I),
     ["You're welcome.", "Anytime.", "Sure thing.", "No problem."]),
    (re.compile(r"^(?:never\s*mind|forget\s+(?:it|that)|cancel\s+that)[\s.!]*$", re.I),
     ["Okay.", "Got it."]),
    (re.compile(r"^(?:stop|shut\s+up|be\s+quiet|quiet|hush)[\s.!]*$", re.I),
     ["Okay."]),
    (re.compile(r"^(?:are\s+you\s+there|you\s+there|hello\?)[\s.!?]*$", re.I),
     ["I'm here.", "Yep, listening."]),
    (re.compile(r"^how\s+(?:are\s+you|'?s\s+it\s+going|is\s+it\s+going|'?re\s+you\s+doing|are\s+you\s+doing)[\s.!?]*$", re.I),
     ["I'm just code, but I'm working.", "Running fine.", "All systems nominal."]),
    (re.compile(r"^(?:goodbye|bye|see\s+you|see\s+ya|catch\s+you\s+later)[\s.!]*$", re.I),
     ["Goodbye.", "See you."]),
    (re.compile(r"^(?:i\s+love\s+you|love\s+you)[\s.!]*$", re.I),
     ["Likewise."]),
    (re.compile(r"^(?:cool|nice|awesome|great|amazing)[\s.!]*$", re.I),
     ["Glad you think so.", "Sure."]),
    (re.compile(r"^(?:okay|ok|alright|got\s+it|understood)[\s.!]*$", re.I),
     ["Okay."]),
    (re.compile(r"^(?:yes|yeah|yep|sure)[\s.!]*$", re.I),
     ["Okay."]),
    (re.compile(r"^(?:no|nope|nah)[\s.!]*$", re.I),
     ["Okay."]),
]


def _try_chitchat(text: str) -> str | None:
    for pat, replies in _CHITCHAT:
        if pat.match(text):
            return random.choice(replies)
    return None


# --- help / capabilities ---------------------------------------------

_HELP_PAT = re.compile(
    r"^(?:what\s+can\s+you\s+do|help(?:\s+me)?|"
    r"list\s+(?:your\s+)?(?:tools|commands|capabilities|features)|"
    r"capabilities|what\s+(?:tools|commands)\s+do\s+you\s+have)[\s.!?]*$",
    re.I,
)

_HELP_REPLY = (
    "I can tell you the time, date, and weather, do math and unit "
    "conversions, set timers, save notes, search the web, look things "
    "up on Wikipedia, wake computers on your network, flip coins and "
    "roll dice, and answer general questions through the local model. "
    "Just ask normally and I'll figure it out."
)


def _try_help(text: str) -> str | None:
    if _HELP_PAT.match(text):
        return _HELP_REPLY
    return None


# --- repeat last reply ------------------------------------------------

_REPEAT_PAT = re.compile(
    r"^(?:say\s+that\s+again|repeat(?:\s+(?:that|please|it))?|"
    r"what\s+did\s+you\s+(?:just\s+)?say|come\s+again|one\s+more\s+time|"
    r"once\s+more)[\s.!?]*$",
    re.I,
)


def _try_repeat(text: str, conversation: Conversation) -> str | None:
    if not _REPEAT_PAT.match(text):
        return None
    # Walk backward, skipping the just-added user turn ("repeat") and
    # any other repeat triggers, looking for the most recent real reply.
    for msg in reversed(conversation.messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return "I haven't said anything yet."


# --- model info -------------------------------------------------------

_MODEL_PAT = re.compile(
    r"^(?:what\s+(?:model|llm|model\s+name)\s+(?:are\s+you\s+(?:running|using))?|"
    r"which\s+(?:model|llm)(?:\s+are\s+you\s+(?:running|using))?|"
    r"model\s+name|what(?:'s|\s+is)\s+your\s+model)[\s.!?]*$",
    re.I,
)


def _try_model_info(text: str, llm: "OllamaRouter | None") -> str | None:
    if not _MODEL_PAT.match(text):
        return None
    if llm is None:
        return "I'm running with no language model loaded."
    # TTS pronounces ":" as "colon" — strip for a cleaner readout.
    spoken = llm.model.replace(":", " ").replace("/", " slash ")
    return f"I'm using {spoken}."


# --- dice / coin / random --------------------------------------------

_COIN_PAT = re.compile(
    r"^(?:flip(?:\s+a)?\s+coin|coin\s+flip|heads\s+or\s+tails)[\s.!?]*$",
    re.I,
)
_DIE_PAT = re.compile(
    r"^roll(?:\s+a)?\s+(?:die|dice|d6)[\s.!?]*$",
    re.I,
)
_DICE_NDS_PAT = re.compile(
    r"^(?:roll\s+)?(\d+)\s*d\s*(\d+)[\s.!?]*$",
    re.I,
)
_DICE_SINGLE_PAT = re.compile(
    r"^(?:roll(?:\s+a)?\s+)?d\s*(\d+)[\s.!?]*$",
    re.I,
)
_RANDOM_RANGE_PAT = re.compile(
    r"^(?:pick(?:\s+me)?\s+a\s+(?:random\s+)?number\s+(?:between\s+)?|"
    r"random\s+number\s+(?:between\s+)?|"
    r"give\s+me\s+a\s+(?:random\s+)?number\s+(?:between\s+)?)"
    r"(-?\d+)\s+(?:and|to)\s+(-?\d+)[\s.!?]*$",
    re.I,
)


def _try_dice(text: str) -> str | None:
    if _COIN_PAT.match(text):
        return random.choice(["Heads.", "Tails."])
    if _DIE_PAT.match(text):
        return f"{random.randint(1, 6)}."
    m = _DICE_NDS_PAT.match(text)
    if m:
        n, sides = int(m.group(1)), int(m.group(2))
        if 1 <= n <= 20 and 2 <= sides <= 1000:
            rolls = [random.randint(1, sides) for _ in range(n)]
            if n == 1:
                return f"{rolls[0]}."
            return f"{', '.join(str(r) for r in rolls)}, for a total of {sum(rolls)}."
    m = _DICE_SINGLE_PAT.match(text)
    if m:
        sides = int(m.group(1))
        if 2 <= sides <= 1000:
            return f"{random.randint(1, sides)}."
    m = _RANDOM_RANGE_PAT.match(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return f"{random.randint(lo, hi)}."
    return None


# --- math -------------------------------------------------------------

# Spelled-out single-word numbers Whisper might emit. Voice users
# generally say "twenty-five" but Whisper transcribes "25" most of the
# time, so this is a thin safety net for the obvious small cases.
_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000,
}
_NUM_WORD_RE = re.compile(r"\b(" + "|".join(_NUM_WORDS.keys()) + r")\b", re.I)


def _normalize_numbers(text: str) -> str:
    """Replace single-word number words with digits. Multi-word
    compounds ("twenty-five") aren't handled — voice users generally
    get digits from Whisper anyway."""
    return _NUM_WORD_RE.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), text)


def _fmt(x: float) -> str:
    """Format a numeric result for spoken output."""
    if math.isnan(x) or math.isinf(x):
        return "an undefined number"
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{round(x, 4):g}"


_MATH_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # Square root
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+the\s+)?(?:square\s+root\s+of|sqrt\s+(?:of\s+)?)"
        r"\s*(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: math.sqrt(float(m.group(1)))),
    # Percent of: "X percent of Y" = X/100 * Y
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+"
        r"(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: (float(m.group(1)) / 100.0) * float(m.group(2))),
    # X squared
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s+squared[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) ** 2),
    # X cubed
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s+cubed[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) ** 3),
    # X to the power of Y
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*"
        r"(?:\*\*|to\s+the\s+power\s+of|to\s+the\s+(\d+)(?:st|nd|rd|th)|\^)\s*"
        r"(-?\d+(?:\.\d+)?)?[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) ** float(m.group(2) or m.group(3))),
    # X plus Y
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*(?:\+|plus|and)\s+"
        r"(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) + float(m.group(2))),
    # X minus Y
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*(?:-|minus|less)\s+"
        r"(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) - float(m.group(2))),
    # X times Y
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*"
        r"(?:\*|x|×|times|multiplied\s+by)\s+(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) * float(m.group(2))),
    # X divided by Y
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*"
        r"(?:/|÷|divided\s+by|over)\s+(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     "divide"),
]


def _try_math(text: str) -> str | None:
    text = _normalize_numbers(text)
    for pat, op in _MATH_PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        try:
            if op == "divide":
                divisor = float(m.group(2))
                if divisor == 0:
                    return "I can't divide by zero."
                result = float(m.group(1)) / divisor
            else:
                result = op(m)
        except (ValueError, ZeroDivisionError, OverflowError):
            return None
        return f"{_fmt(result)}."
    return None


# --- unit conversion --------------------------------------------------

# Linear-scale units: stored as canonical_key → (display_name, [aliases], factor_to_canonical_base)
# The canonical base is meters for length, kilograms for weight.
_LENGTH = {
    "m":  ("meter",       ["m", "meter", "meters", "metre", "metres"], 1.0),
    "km": ("kilometer",   ["km", "kilometer", "kilometers", "kilometre", "kilometres"], 1000.0),
    "cm": ("centimeter",  ["cm", "centimeter", "centimeters", "centimetre", "centimetres"], 0.01),
    "mm": ("millimeter",  ["mm", "millimeter", "millimeters"], 0.001),
    "mi": ("mile",        ["mi", "mile", "miles"], 1609.344),
    "ft": ("foot",        ["ft", "foot", "feet"], 0.3048),
    "in": ("inch",        ["in", "inch", "inches"], 0.0254),
    "yd": ("yard",        ["yd", "yard", "yards"], 0.9144),
}

_WEIGHT = {
    "kg":  ("kilogram",   ["kg", "kilogram", "kilograms", "kilo", "kilos"], 1.0),
    "g":   ("gram",       ["g", "gram", "grams"], 0.001),
    "mg":  ("milligram",  ["mg", "milligram", "milligrams"], 1e-6),
    "lb":  ("pound",      ["lb", "lbs", "pound", "pounds"], 0.453592),
    "oz":  ("ounce",      ["oz", "ounce", "ounces"], 0.0283495),
    "ton": ("metric ton", ["ton", "tons", "tonne", "tonnes"], 1000.0),
}


def _build_alias_map() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for category, table in (("length", _LENGTH), ("weight", _WEIGHT)):
        for canonical, (_, aliases, _factor) in table.items():
            for a in aliases:
                out[a.lower()] = (category, canonical)
    return out


_ALIAS_MAP = _build_alias_map()
_ALL_ALIASES = "|".join(sorted(_ALIAS_MAP.keys(), key=len, reverse=True))
_CONVERT_PAT = re.compile(
    rf"^(?:convert\s+|how\s+many\s+|what(?:'s|\s+is)\s+)?"
    rf"(-?\d+(?:\.\d+)?)\s*({_ALL_ALIASES})\s+(?:to|in|into|are\s+in)\s+"
    rf"({_ALL_ALIASES})[\s.!?]*$",
    re.I,
)
_TEMP_PAT = re.compile(
    r"^(?:convert\s+|what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*"
    r"(?:degrees\s+)?(fahrenheit|f|celsius|c|kelvin|k)\s+"
    r"(?:to|in|into)\s+(?:degrees\s+)?"
    r"(fahrenheit|f|celsius|c|kelvin|k)[\s.!?]*$",
    re.I,
)


def _try_unit_conversion(text: str) -> str | None:
    # Temperature first — non-linear, can't be unified with the others.
    m = _TEMP_PAT.match(text)
    if m:
        value = float(m.group(1))
        src = m.group(2)[0].upper()
        dst = m.group(3)[0].upper()
        # to celsius
        if src == "F":
            celsius = (value - 32.0) * 5.0 / 9.0
        elif src == "K":
            celsius = value - 273.15
        else:
            celsius = value
        # from celsius
        if dst == "F":
            result, unit_name = celsius * 9.0 / 5.0 + 32.0, "fahrenheit"
        elif dst == "K":
            result, unit_name = celsius + 273.15, "kelvin"
        else:
            result, unit_name = celsius, "celsius"
        return f"{_fmt(result)} {unit_name}."

    m = _CONVERT_PAT.match(text)
    if not m:
        return None
    value = float(m.group(1))
    from_alias = m.group(2).lower()
    to_alias = m.group(3).lower()
    from_cat, from_key = _ALIAS_MAP[from_alias]
    to_cat, to_key = _ALIAS_MAP[to_alias]
    if from_cat != to_cat:
        return None  # apples to oranges; LLM can sort it out
    table = _LENGTH if from_cat == "length" else _WEIGHT
    canonical_value = value * table[from_key][2]
    result = canonical_value / table[to_key][2]
    name = table[to_key][0]
    plural = "" if result == 1 else "s"
    return f"{_fmt(result)} {name}{plural}."


# --- spell ------------------------------------------------------------

_SPELL_PAT = re.compile(
    r"^(?:how\s+do\s+you\s+spell\s+|spell\s+(?:the\s+word\s+)?)(.+?)[\s.!?]*$",
    re.I,
)


def _try_spell(text: str) -> str | None:
    m = _SPELL_PAT.match(text)
    if not m:
        return None
    word = m.group(1).strip().rstrip(".?!").strip()
    if not word or " " in word or len(word) > 30:
        return None
    letters = ". ".join(c.upper() for c in word if c.isalpha())
    if not letters:
        return None
    return f"{letters}."


# --- date math --------------------------------------------------------

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAYS_RE = "|".join(_DAYS)

_IN_N_DAYS_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)?\s+(?:the\s+)?(?:day|date)\s+(?:is\s+it\s+|will\s+it\s+be\s+))?"
    r"in\s+(\d+)\s+(day|days|week|weeks)[\s.!?]*$",
    re.I,
)
_NEXT_DAY_PAT = re.compile(
    rf"^(?:what(?:'s|\s+is)\s+(?:the\s+)?date\s+(?:on\s+|of\s+)?)?"
    rf"(?:next\s+)?({_DAYS_RE})[\s.!?]*$",
    re.I,
)
_DAYS_UNTIL_PAT = re.compile(
    rf"^how\s+many\s+days\s+(?:until|till|to|'?til)\s+({_DAYS_RE})[\s.!?]*$",
    re.I,
)


def _try_date_math(text: str) -> str | None:
    today = date.today()

    m = _IN_N_DAYS_PAT.match(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        days = n * (7 if unit.startswith("week") else 1)
        target = today + timedelta(days=days)
        return f"{target.strftime('%A, %B %d')}."

    m = _DAYS_UNTIL_PAT.match(text)
    if m:
        target_dow = _DAYS.index(m.group(1).lower())
        days_ahead = (target_dow - today.weekday() + 7) % 7
        if days_ahead == 0:
            return "Today."
        return f"{days_ahead} day{'s' if days_ahead != 1 else ''}."

    # "next Monday" / "Monday" → next occurrence (today doesn't count
    # if it's the same day; that becomes a week from now).
    m = _NEXT_DAY_PAT.match(text)
    if m:
        # Avoid matching bare "monday" with no context — too eager.
        # Only fire if the text starts with "next" OR is the full
        # "what's the date on/of <day>".
        lowered = text.lower().strip()
        if lowered.startswith("next ") or "date" in lowered:
            target_dow = _DAYS.index(m.group(1).lower())
            days_ahead = (target_dow - today.weekday() + 7) % 7
            if days_ahead == 0:
                days_ahead = 7
            target = today + timedelta(days=days_ahead)
            return f"{target.strftime('%A, %B %d')}."

    return None


# --- Wikipedia --------------------------------------------------------

_WIKI_PATS = (
    re.compile(r"^(?:tell\s+me\s+about|tell\s+me\s+who)\s+(.+?)[\s.!?]*$", re.I),
    re.compile(r"^(?:who\s+(?:was|is)|who'?s)\s+(.+?)[\s.!?]*$", re.I),
    re.compile(r"^(?:what\s+(?:was|is)\s+a)\s+(.+?)[\s.!?]*$", re.I),
)


async def _try_wikipedia(text: str, registry: ToolRegistry) -> str | None:
    for pat in _WIKI_PATS:
        m = pat.match(text)
        if not m:
            continue
        subject = m.group(1).strip().rstrip(".?!").strip()
        if not subject or len(subject) > 80:
            continue
        tool = registry.get("wikipedia_summary")
        if tool is None:
            return None
        try:
            return await tool.handler({"topic": subject})
        except Exception as exc:  # noqa: BLE001
            log.warning("fast-path wikipedia failed: %s", exc)
            return None
    return None
