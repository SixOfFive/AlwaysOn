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

import json
import logging
import math
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from jarvis_server.conversation import Conversation
from jarvis_server.tools import ToolRegistry

if TYPE_CHECKING:
    from jarvis_server.ollama_router import OllamaRouter

log = logging.getLogger(__name__)

# Module-load time — used by the "uptime" handler. Monotonic so it's
# stable across wall-clock adjustments.
_START_TIME = time.monotonic()


# --- entry point ------------------------------------------------------

async def try_all(
    text: str,
    conversation: Conversation,
    registry: ToolRegistry,
    llm: "OllamaRouter | None",
) -> tuple[str, str] | None:
    """Run every fast-path matcher in priority order. Returns
    (handler_name, reply) on first match, else None.

    Lazy if-chain so an early hit doesn't pay for the later handlers'
    regex matches."""
    text = text.strip()
    if not text:
        return None

    # Conversation control / identity / status.
    if (r := _try_reset_context(text, conversation)) is not None: return "reset_context", r
    if (r := _try_chitchat(text)) is not None: return "chitchat", r
    if (r := _try_identity(text)) is not None: return "identity", r
    if (r := _try_are_you_ai(text)) is not None: return "are_you_ai", r
    if (r := _try_help(text)) is not None: return "help", r
    if (r := _try_repeat(text, conversation)) is not None: return "repeat", r
    if (r := _try_model_info(text, llm)) is not None: return "model_info", r
    if (r := _try_uptime(text)) is not None: return "uptime", r
    if (r := _try_wol_hosts_list(text)) is not None: return "wol_hosts", r

    # Date / time.
    if (r := _try_unix_time(text)) is not None: return "unix_time", r
    if (r := _try_utc_time(text)) is not None: return "utc_time", r
    if (r := _try_yesterday_tomorrow(text)) is not None: return "yesterday_tomorrow", r
    if (r := _try_day_of_week(text)) is not None: return "day_of_week", r
    if (r := _try_weekend(text)) is not None: return "weekend", r
    if (r := _try_month_year(text)) is not None: return "month_year", r
    if (r := _try_date_math(text)) is not None: return "date_math", r

    # Math — specific operators before the generic binop handler.
    if (r := _try_math_constants(text)) is not None: return "math_constants", r
    if (r := _try_math_abs(text)) is not None: return "math_abs", r
    if (r := _try_math_factorial(text)) is not None: return "math_factorial", r
    if (r := _try_math_log(text)) is not None: return "math_log", r
    if (r := _try_math_trig(text)) is not None: return "math_trig", r
    if (r := _try_math_round(text)) is not None: return "math_round", r
    if (r := _try_math_modulo(text)) is not None: return "math_modulo", r
    if (r := _try_math_is_prime(text)) is not None: return "math_is_prime", r
    if (r := _try_math(text)) is not None: return "math", r

    # Format / unit / conversion.
    if (r := _try_base_conversion(text)) is not None: return "base_conversion", r
    if (r := _try_unit_conversion(text)) is not None: return "unit_conversion", r

    # Games, random, fun.
    if (r := _try_dice(text)) is not None: return "dice", r
    if (r := _try_rock_paper_scissors(text)) is not None: return "rock_paper_scissors", r
    if (r := _try_pick_from_list(text)) is not None: return "pick_from_list", r
    if (r := _try_joke(text)) is not None: return "joke", r

    # Echo / spelling.
    if (r := _try_echo(text)) is not None: return "echo", r
    if (r := _try_spell(text)) is not None: return "spell", r

    # Network last.
    if (r := await _try_wikipedia(text, registry)) is not None: return "wikipedia", r
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


# --- identity ---------------------------------------------------------

_IDENTITY_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+your\s+name|who\s+are\s+you|"
    r"who'?s\s+this|introduce\s+yourself)[\s.!?]*$",
    re.I,
)
_AI_PAT = re.compile(
    r"^are\s+you\s+(?:an?\s+|a\s+real\s+|really\s+)?"
    r"(?:ai|robot|computer|human|person|real|alive|sentient|conscious|smart)"
    r"[\s.!?]*$",
    re.I,
)


def _try_identity(text: str) -> str | None:
    if _IDENTITY_PAT.match(text):
        return "I'm Jarvis. I'm the local voice assistant running on this network."
    return None


def _try_are_you_ai(text: str) -> str | None:
    if not _AI_PAT.match(text):
        return None
    return random.choice([
        "I'm software, running locally on your machine.",
        "I'm a program. No body, no consciousness, just text and tools.",
        "I'm a local voice assistant. Definitely not human.",
    ])


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
    "roll dice, tell jokes, and answer general questions through the "
    "local model. Just ask normally and I'll figure it out."
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
    spoken = llm.model.replace(":", " ").replace("/", " slash ")
    return f"I'm using {spoken}."


# --- uptime -----------------------------------------------------------

_UPTIME_PAT = re.compile(
    r"^(?:how\s+long\s+(?:have\s+you\s+been\s+(?:running|up|on)|has\s+(?:the\s+)?server\s+been\s+(?:running|up))|"
    r"(?:server|your)\s+uptime|what(?:'s|\s+is)\s+(?:your|the\s+server)\s+uptime)[\s.!?]*$",
    re.I,
)


def _try_uptime(text: str) -> str | None:
    if not _UPTIME_PAT.match(text):
        return None
    seconds = int(time.monotonic() - _START_TIME)
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}."
    if seconds < 3600:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}."
    if seconds < 86400:
        h, m = divmod(seconds // 60, 60)
        return f"{h} hour{'s' if h != 1 else ''} and {m} minute{'s' if m != 1 else ''}."
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d} day{'s' if d != 1 else ''} and {h} hour{'s' if h != 1 else ''}."


# --- reset context (voice) -------------------------------------------

_RESET_PAT = re.compile(
    r"^(?:clear\s+(?:the\s+)?(?:context|conversation|history|chat)|"
    r"forget\s+(?:everything|all\s+of\s+(?:this|that)|what\s+we\s+(?:said|talked\s+about))|"
    r"reset\s+(?:the\s+)?(?:context|conversation|history|chat)|"
    r"start\s+over|new\s+(?:conversation|chat|session))[\s.!?]*$",
    re.I,
)


def _try_reset_context(text: str, conversation: Conversation) -> str | None:
    if not _RESET_PAT.match(text):
        return None
    n = len(conversation.messages)
    conversation.messages.clear()
    conversation.touch()
    log.info("conversation cleared by voice command (was %d msg)", n)
    return "Okay, fresh start. I've forgotten what we were just talking about."


# --- WoL hosts list --------------------------------------------------

_WOL_HOSTS_PAT = re.compile(
    r"^(?:what\s+(?:hosts|machines|computers|devices)\s+can\s+you\s+wake|"
    r"list\s+(?:your\s+)?(?:wake[\s-]on[\s-]lan|wol)\s+(?:hosts|targets)|"
    r"what(?:'s|\s+is)\s+(?:on\s+)?(?:my\s+)?wake[\s-]on[\s-]lan\s+list)[\s.!?]*$",
    re.I,
)


def _try_wol_hosts_list(text: str) -> str | None:
    if not _WOL_HOSTS_PAT.match(text):
        return None
    raw = os.getenv("JARVIS_HOSTS")
    if not raw:
        return "No hosts configured. Add some to the WoL hosts table in jarvis.toml."
    try:
        hosts = json.loads(raw)
    except json.JSONDecodeError:
        return "The hosts configuration is set but couldn't be parsed."
    if not isinstance(hosts, dict) or not hosts:
        return "No hosts configured."
    names = sorted(hosts.keys())
    if len(names) == 1:
        return f"I can wake {names[0]}."
    if len(names) == 2:
        return f"I can wake {names[0]} and {names[1]}."
    return f"I can wake {', '.join(names[:-1])}, and {names[-1]}."


# --- time / date -----------------------------------------------------

_UNIX_PAT = re.compile(
    r"^what(?:'s|\s+is)\s+(?:the\s+)?(?:unix\s+(?:time|timestamp)|epoch\s+(?:time|timestamp))[\s.!?]*$",
    re.I,
)
_UTC_PAT = re.compile(
    r"^what(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?"
    r"(?:utc|gmt|zulu)\s+time[\s.!?]*$",
    re.I,
)
_YESTERDAY_TOMORROW_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?(?:(?:yesterday|tomorrow)'?s?\s+date|"
    r"what\s+(?:day|date)\s+(?:was|is)\s+(yesterday|tomorrow))[\s.!?]*$",
    re.I,
)
_YESTERDAY_TOMORROW_WORD_RE = re.compile(r"(yesterday|tomorrow)", re.I)
_DAY_OF_WEEK_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?(?:what\s+)?(?:day\s+of\s+the\s+week\s+(?:is\s+(?:it|today))|"
    r"weekday\s+is\s+(?:it|today)|"
    r"day\s+is\s+(?:it|today))[\s.!?]*$",
    re.I,
)
_WEEKEND_PAT = re.compile(
    r"^is\s+(?:it|today)\s+(?:the\s+)?(weekend|a\s+weekday|a\s+weekend|a\s+workday)[\s.!?]*$",
    re.I,
)
_MONTH_YEAR_PAT = re.compile(
    r"^what\s+(month|year)\s+(?:is\s+(?:it|this|this\s+(?:month|year)))?[\s.!?]*$",
    re.I,
)


def _try_unix_time(text: str) -> str | None:
    if _UNIX_PAT.match(text):
        return str(int(time.time())) + "."
    return None


def _try_utc_time(text: str) -> str | None:
    if not _UTC_PAT.match(text):
        return None
    now = datetime.now(tz=timezone.utc)
    return now.strftime("It's %I:%M %p UTC.").lstrip("0")


def _try_yesterday_tomorrow(text: str) -> str | None:
    if not _YESTERDAY_TOMORROW_PAT.match(text):
        return None
    word = _YESTERDAY_TOMORROW_WORD_RE.search(text)
    if not word:
        return None
    today = date.today()
    if word.group(1).lower() == "yesterday":
        target = today - timedelta(days=1)
        return f"Yesterday was {target.strftime('%A, %B %d')}."
    target = today + timedelta(days=1)
    return f"Tomorrow is {target.strftime('%A, %B %d')}."


def _try_day_of_week(text: str) -> str | None:
    if not _DAY_OF_WEEK_PAT.match(text):
        return None
    return f"Today is {date.today().strftime('%A')}."


def _try_weekend(text: str) -> str | None:
    m = _WEEKEND_PAT.match(text)
    if not m:
        return None
    asked = m.group(1).lower()
    is_weekend = date.today().weekday() >= 5
    if "weekend" in asked:
        return "Yes, it's the weekend." if is_weekend else "No, it's a weekday."
    return "Yes, it's a weekday." if not is_weekend else "No, it's the weekend."


def _try_month_year(text: str) -> str | None:
    m = _MONTH_YEAR_PAT.match(text)
    if not m:
        return None
    asked = m.group(1).lower()
    today = date.today()
    if asked == "month":
        return f"It's {today.strftime('%B')}."
    return f"It's {today.year}."


# --- dice / coin / random --------------------------------------------

_COIN_PAT = re.compile(r"^(?:flip(?:\s+a)?\s+coin|coin\s+flip|heads\s+or\s+tails)[\s.!?]*$", re.I)
_DIE_PAT = re.compile(r"^roll(?:\s+a)?\s+(?:die|dice|d6)[\s.!?]*$", re.I)
_DICE_NDS_PAT = re.compile(r"^(?:roll\s+)?(\d+)\s*d\s*(\d+)[\s.!?]*$", re.I)
_DICE_SINGLE_PAT = re.compile(r"^(?:roll(?:\s+a)?\s+)?d\s*(\d+)[\s.!?]*$", re.I)
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


# --- rock paper scissors ---------------------------------------------

_RPS_PAT = re.compile(
    r"^(?:let'?s\s+play\s+|play\s+|i\s+(?:pick|choose)\s+)?"
    r"(?:rock\s+paper\s+scissors|rps)[\s.!?]*$",
    re.I,
)
_RPS_USER_PAT = re.compile(
    r"^(?:i\s+(?:pick|choose|play|throw)\s+)?(rock|paper|scissors)[\s.!?]*$",
    re.I,
)
_RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def _try_rock_paper_scissors(text: str) -> str | None:
    if _RPS_PAT.match(text):
        return f"I throw {random.choice(list(_RPS_BEATS))}."
    m = _RPS_USER_PAT.match(text)
    if not m:
        return None
    user = m.group(1).lower()
    bot = random.choice(list(_RPS_BEATS))
    if user == bot:
        return f"I picked {bot}. Tie."
    if _RPS_BEATS[user] == bot:
        return f"I picked {bot}. You win."
    return f"I picked {bot}. I win."


# --- pick from list --------------------------------------------------

_PICK_PAT = re.compile(
    r"^(?:pick|choose|select|decide)(?:\s+(?:one|me\s+one))?"
    r"\s+(?:between|from|out\s+of)?\s+(.+?)[\s.!?]*$",
    re.I,
)


def _try_pick_from_list(text: str) -> str | None:
    m = _PICK_PAT.match(text)
    if not m:
        return None
    body = m.group(1).strip()
    # Don't shadow the "pick a number between 1 and 10" path.
    if re.match(r"^a\s+(?:random\s+)?number\b", body, re.I):
        return None
    # Split on ",", " or ", " and ".
    parts = re.split(r"\s*,\s*|\s+(?:or|and)\s+", body)
    parts = [p.strip(" .?!") for p in parts if p.strip(" .?!")]
    if len(parts) < 2:
        return None
    if len(parts) > 20:
        return None  # something else; let the LLM handle it
    return f"{random.choice(parts)}."


# --- joke -------------------------------------------------------------

_JOKE_PAT = re.compile(
    r"^(?:tell\s+me\s+a\s+joke|joke\s+me|say\s+something\s+funny|"
    r"do\s+you\s+know\s+(?:any\s+)?jokes|make\s+me\s+laugh)[\s.!?]*$",
    re.I,
)
_JOKES = [
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my computer I needed a break. It said: no problem, I'll go to sleep.",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "I would tell you a UDP joke, but you might not get it.",
    "There are 10 kinds of people in the world. Those who understand binary and those who don't.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "How many software engineers does it take to change a light bulb? None. That's a hardware problem.",
    "Why did the developer go broke? Because he used up all his cache.",
    "I'd tell you a joke about TCP, but I'd have to keep repeating it until you got it.",
]


def _try_joke(text: str) -> str | None:
    if _JOKE_PAT.match(text):
        return random.choice(_JOKES)
    return None


# --- echo / say X ----------------------------------------------------

_ECHO_PAT = re.compile(
    r"^(?:say|repeat\s+after\s+me|echo)\s+(.+)[\s.!?]*$",
    re.I,
)


def _try_echo(text: str) -> str | None:
    # Skip if it overlaps with the repeat handler (which already covered
    # bare "say that again" type phrases — those don't have a payload
    # after "say").
    m = _ECHO_PAT.match(text)
    if not m:
        return None
    payload = m.group(1).strip().rstrip(".!?")
    if not payload:
        return None
    # Defensive: ignore meta words that the repeat handler covers.
    if re.match(r"^that\s+again|something\s+funny", payload, re.I):
        return None
    return payload + "."


# --- math: constants -------------------------------------------------

_MATH_CONST_PAT = re.compile(
    r"^what(?:'s|\s+is)\s+(?:the\s+(?:value\s+of\s+)?)?"
    r"(pi|tau|e|euler'?s?\s+number|golden\s+ratio|phi)[\s.!?]*$",
    re.I,
)


def _try_math_constants(text: str) -> str | None:
    m = _MATH_CONST_PAT.match(text)
    if not m:
        return None
    name = m.group(1).lower()
    if name == "pi":
        return f"Pi is approximately {math.pi:.4f}."
    if name == "tau":
        return f"Tau is approximately {math.tau:.4f}."
    if name == "e" or "euler" in name:
        return f"Euler's number is approximately {math.e:.4f}."
    # Golden ratio φ = (1 + √5) / 2 ≈ 1.6180
    phi = (1 + math.sqrt(5)) / 2
    return f"The golden ratio is approximately {phi:.4f}."


# --- math: abs --------------------------------------------------------

_ABS_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+(?:the\s+)?)?"
    r"(?:absolute\s+value\s+of|abs\s+(?:of\s+)?)\s*(-?\d+(?:\.\d+)?)[\s.!?]*$",
    re.I,
)


def _try_math_abs(text: str) -> str | None:
    m = _ABS_PAT.match(text)
    if not m:
        return None
    return f"{_fmt(abs(float(m.group(1))))}."


# --- math: factorial --------------------------------------------------

_FACT_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?"
    r"(?:(\d+)\s+factorial|factorial\s+of\s+(\d+)|(\d+)!)[\s.!?]*$",
    re.I,
)


def _try_math_factorial(text: str) -> str | None:
    m = _FACT_PAT.match(text)
    if not m:
        return None
    n_str = m.group(1) or m.group(2) or m.group(3)
    n = int(n_str)
    if n < 0 or n > 20:  # 20! fits in a 64-bit int; bigger is silly for voice
        return None
    return f"{math.factorial(n)}."


# --- math: log --------------------------------------------------------

_LN_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+(?:the\s+)?)?"
    r"(?:natural\s+log(?:arithm)?|ln)\s+of\s+(\d+(?:\.\d+)?)[\s.!?]*$",
    re.I,
)
_LOG_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+(?:the\s+)?)?"
    r"log(?:arithm)?\s+(?:base\s+(\d+(?:\.\d+)?)\s+)?of\s+(\d+(?:\.\d+)?)[\s.!?]*$",
    re.I,
)


def _try_math_log(text: str) -> str | None:
    m = _LN_PAT.match(text)
    if m:
        val = float(m.group(1))
        if val <= 0:
            return "I can't take a log of a non-positive number."
        return f"{_fmt(math.log(val))}."
    m = _LOG_PAT.match(text)
    if m:
        base = m.group(1)
        val = float(m.group(2))
        if val <= 0:
            return "I can't take a log of a non-positive number."
        if base is None:
            # Plain "log of X" → base 10, the calculator convention.
            return f"{_fmt(math.log10(val))}."
        base_val = float(base)
        if base_val <= 0 or base_val == 1:
            return None
        return f"{_fmt(math.log(val, base_val))}."
    return None


# --- math: trig -------------------------------------------------------

_TRIG_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+(?:the\s+)?)?"
    r"(sine|cosine|tangent|sin|cos|tan)"
    r"\s+(?:of\s+)?(-?\d+(?:\.\d+)?)"
    r"(?:\s+(degrees|radians|deg|rad))?[\s.!?]*$",
    re.I,
)


def _try_math_trig(text: str) -> str | None:
    m = _TRIG_PAT.match(text)
    if not m:
        return None
    fn_name = m.group(1).lower()[:3]  # "sin"/"cos"/"tan"
    value = float(m.group(2))
    unit = (m.group(3) or "").lower()
    # Default to degrees — most voice users think in degrees, and saying
    # the unit out loud is rare. Power-user override is "radians".
    if unit.startswith("rad"):
        rads = value
    else:
        rads = math.radians(value)
    fn = {"sin": math.sin, "cos": math.cos, "tan": math.tan}[fn_name]
    try:
        result = fn(rads)
    except (ValueError, OverflowError):
        return None
    return f"{_fmt(result)}."


# --- math: round/floor/ceiling ---------------------------------------

_ROUND_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?(round|floor|ceiling|ceil)\s+(?:of\s+)?"
    r"(-?\d+(?:\.\d+)?)[\s.!?]*$",
    re.I,
)


def _try_math_round(text: str) -> str | None:
    m = _ROUND_PAT.match(text)
    if not m:
        return None
    op = m.group(1).lower()
    val = float(m.group(2))
    if op == "round":
        return f"{round(val)}."
    if op == "floor":
        return f"{math.floor(val)}."
    return f"{math.ceil(val)}."


# --- math: modulo ----------------------------------------------------

_MOD_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?(-?\d+)\s+"
    r"(?:mod|modulo|modulus|%|remainder\s+(?:of\s+)?(?:dividing\s+by\s+)?|"
    r"remainder\s+when\s+divided\s+by)\s+(-?\d+)[\s.!?]*$",
    re.I,
)


def _try_math_modulo(text: str) -> str | None:
    m = _MOD_PAT.match(text)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2))
    if b == 0:
        return "I can't take a remainder by zero."
    return f"{a % b}."


# --- math: is prime --------------------------------------------------

_PRIME_PAT = re.compile(
    r"^is\s+(\d+)\s+(?:a\s+)?prime(?:\s+number)?[\s.!?]*$",
    re.I,
)


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0:
        return False
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def _try_math_is_prime(text: str) -> str | None:
    m = _PRIME_PAT.match(text)
    if not m:
        return None
    n = int(m.group(1))
    if n > 10**12:
        return None  # trial division would be too slow; let LLM handle
    return f"Yes, {n} is prime." if _is_prime(n) else f"No, {n} is not prime."


# --- math (generic binop) ---------------------------------------------

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
    return _NUM_WORD_RE.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), text)


def _fmt(x: float) -> str:
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return "an undefined number"
        if x == int(x) and abs(x) < 1e15:
            return str(int(x))
        return f"{round(x, 4):g}"
    return str(x)


_MATH_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+the\s+)?(?:square\s+root\s+of|sqrt\s+(?:of\s+)?)"
        r"\s*(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: math.sqrt(float(m.group(1)))),
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+"
        r"(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: (float(m.group(1)) / 100.0) * float(m.group(2))),
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s+squared[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) ** 2),
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s+cubed[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) ** 3),
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*"
        r"(?:\*\*|to\s+the\s+power\s+of|to\s+the\s+(\d+)(?:st|nd|rd|th)|\^)\s*"
        r"(-?\d+(?:\.\d+)?)?[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) ** float(m.group(2) or m.group(3))),
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*(?:\+|plus|and)\s+"
        r"(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) + float(m.group(2))),
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*(?:-|minus|less)\s+"
        r"(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) - float(m.group(2))),
    (re.compile(
        r"^(?:what(?:'s|\s+is)\s+)?(-?\d+(?:\.\d+)?)\s*"
        r"(?:\*|x|×|times|multiplied\s+by)\s+(-?\d+(?:\.\d+)?)[\s.!?]*$", re.I),
     lambda m: float(m.group(1)) * float(m.group(2))),
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


# --- base conversion --------------------------------------------------

_TO_BASE_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?(-?\d+)\s+in\s+(binary|hex|hexadecimal|octal)"
    r"[\s.!?]*$",
    re.I,
)
_FROM_BASE_PAT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?(?:binary|hex|hexadecimal|octal)\s+"
    r"([0-9a-fA-Fx]+)\s+in\s+(?:decimal|base\s+10)[\s.!?]*$",
    re.I,
)


def _try_base_conversion(text: str) -> str | None:
    m = _TO_BASE_PAT.match(text)
    if m:
        n = int(m.group(1))
        base = m.group(2).lower()
        if base == "binary":
            return f"{bin(n)[2:] if n >= 0 else '-' + bin(-n)[2:]}."
        if base in ("hex", "hexadecimal"):
            s = hex(n)[2:] if n >= 0 else "-" + hex(-n)[2:]
            return f"{s.upper()}."
        if base == "octal":
            return f"{oct(n)[2:] if n >= 0 else '-' + oct(-n)[2:]}."
    m = _FROM_BASE_PAT.match(text)
    if m:
        digits = m.group(1)
        lower = text.lower()
        if "binary" in lower:
            try:
                return f"{int(digits, 2)}."
            except ValueError:
                return None
        if "hex" in lower:
            try:
                return f"{int(digits, 16)}."
            except ValueError:
                return None
        if "octal" in lower:
            try:
                return f"{int(digits, 8)}."
            except ValueError:
                return None
    return None


# --- unit conversion --------------------------------------------------

# Each table: canonical key → (display name, [aliases], factor to canonical base)
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

_TIME = {
    "s":    ("second",  ["s", "sec", "secs", "second", "seconds"], 1.0),
    "min":  ("minute",  ["min", "mins", "minute", "minutes"], 60.0),
    "hr":   ("hour",    ["h", "hr", "hrs", "hour", "hours"], 3600.0),
    "day":  ("day",     ["day", "days"], 86400.0),
    "week": ("week",    ["wk", "wks", "week", "weeks"], 604800.0),
    "ms":   ("millisecond", ["ms", "millisecond", "milliseconds"], 0.001),
}

_VOLUME = {
    "ml":  ("milliliter", ["ml", "milliliter", "milliliters", "millilitre", "millilitres"], 0.001),
    "l":   ("liter",      ["l", "liter", "liters", "litre", "litres"], 1.0),
    "tsp": ("teaspoon",   ["tsp", "teaspoon", "teaspoons"], 0.00492892),
    "tbsp":("tablespoon", ["tbsp", "tablespoon", "tablespoons"], 0.0147868),
    "cup": ("cup",        ["cup", "cups"], 0.24),
    "pt":  ("pint",       ["pt", "pint", "pints"], 0.473176),
    "qt":  ("quart",      ["qt", "quart", "quarts"], 0.946353),
    "gal": ("gallon",     ["gal", "gallon", "gallons"], 3.78541),
    "floz":("fluid ounce",["fl oz", "fluid ounce", "fluid ounces"], 0.0295735),
}

_SPEED = {
    "mps":  ("meter per second",     ["m/s", "mps", "meters per second", "meter per second"], 1.0),
    "kph":  ("kilometer per hour",   ["kph", "kmh", "km/h", "kilometers per hour", "kilometer per hour"], 1.0 / 3.6),
    "mph":  ("mile per hour",        ["mph", "miles per hour", "mile per hour"], 0.44704),
    "fps":  ("foot per second",      ["fps", "ft/s", "feet per second", "foot per second"], 0.3048),
    "knot": ("knot",                 ["knot", "knots", "kn"], 0.514444),
}


def _build_alias_map() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for category, table in (("length", _LENGTH), ("weight", _WEIGHT),
                            ("time", _TIME), ("volume", _VOLUME), ("speed", _SPEED)):
        for canonical, (_, aliases, _factor) in table.items():
            for a in aliases:
                out[a.lower()] = (category, canonical)
    return out


_ALIAS_MAP = _build_alias_map()
# Sort longest aliases first so "meters per second" beats "meter".
_ALL_ALIASES = "|".join(re.escape(a) for a in sorted(_ALIAS_MAP.keys(), key=len, reverse=True))
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
    m = _TEMP_PAT.match(text)
    if m:
        value = float(m.group(1))
        src = m.group(2)[0].upper()
        dst = m.group(3)[0].upper()
        if src == "F":
            celsius = (value - 32.0) * 5.0 / 9.0
        elif src == "K":
            celsius = value - 273.15
        else:
            celsius = value
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
        return None  # apples to oranges; let the LLM handle it
    table = {"length": _LENGTH, "weight": _WEIGHT, "time": _TIME,
             "volume": _VOLUME, "speed": _SPEED}[from_cat]
    canonical_value = value * table[from_key][2]
    result = canonical_value / table[to_key][2]
    name = table[to_key][0]
    plural = "" if abs(result - 1) < 1e-9 else "s"
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

    m = _NEXT_DAY_PAT.match(text)
    if m:
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
