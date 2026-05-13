"""LLM router backed by a local Ollama server.

Drop-in replacement for the old Claude router. Talks to Ollama's
`/api/chat` endpoint with OpenAI-style `tools=[...]`, runs the
tool-execution loop against the existing ToolRegistry, and returns the
final spoken text.

Why /api/chat and not /v1/chat/completions:
- /api/chat is Ollama-native, exposes `options.num_ctx` for explicit
  context-length control. The OpenAI compat layer doesn't.
- Tool-call response shape is identical in both, just JSON paths differ.

Pull-on-startup is handled by `ensure_pulled` so the router doesn't have
to stall on the first request waiting for a 4 GB download.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from jarvis_server import banlist
from jarvis_server.conversation import Conversation
from jarvis_server.tools import ToolRegistry

log = logging.getLogger(__name__)

def _build_system_prompt(trigger_phrase: str) -> str:
    """The trigger-word avoidance rule is interpolated from config so the
    LLM gets told about the actual configured phrase, not a hardcoded one."""
    return (
        "You are Jarvis, a voice assistant. Replies will be spoken aloud by "
        "text-to-speech, so:\n"
        "- Answer in one or two short sentences. Conversational, not formal.\n"
        "- No markdown, no bullet points, no code blocks.\n"
        "- Spell out numbers and units the way a person would say them.\n"
        "- If you don't know, say so plainly. Don't invent facts.\n"
        "- When the user asks about their own past work, projects, or where they "
        "left off, use the vault_* tools — that is the source of truth for "
        "their history.\n"
        "- Use tools when they fit. Do not narrate that you are using a tool; "
        "just call it and use the result.\n"
        "- Conversation history may include lines prefixed `[overheard]`. "
        "Those are things the room's microphone caught but that were NOT "
        "addressed to you — treat them as background context only. Do not "
        "answer them, do not acknowledge them. Reply only to the most recent "
        "non-overheard user turn.\n"
        f"- NEVER use the literal word \"{trigger_phrase}\" in any reply. "
        "Your voice is played back through the same room's speaker; saying "
        "the wake word would risk re-triggering you. Use a synonym (machine, "
        "system, PC, device, …) if you must refer to one."
    )


class OllamaRouter:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        context_length: int = 16384,
        max_tool_iterations: int = 5,
        request_timeout: float = 120.0,
        trigger_phrase: str = "computer",
    ) -> None:
        self.registry = registry
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.context_length = context_length
        self.max_iter = max_tool_iterations
        self.client = httpx.AsyncClient(timeout=request_timeout)
        self.system_prompt = _build_system_prompt(trigger_phrase)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def ask(self, user_text: str, conversation: Conversation) -> str:
        """Run the tool-using chat loop. The session has already added
        `user_text` to `conversation.messages`; we use that prefix as
        the request history (plus a system prompt) and append every
        intermediate assistant + tool message back into `conversation`
        as the loop runs, so the next turn sees the full thread."""
        tools = self.registry.as_ollama_tools()
        # Snapshot the index just before the LLM-produced turns so the
        # `tool_calls` / `tool` frames we add land back on `conversation`.
        history_prefix_len = len(conversation.messages)
        del user_text  # already in conversation.messages — kept as parameter for API clarity

        for i in range(self.max_iter):
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt},
                *conversation.messages,
            ]
            payload = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                # keep_alive: -1 tells Ollama to never auto-unload this
                # model. Otherwise it evicts from VRAM after ~5 min of
                # idle and the next utterance pays a multi-second cold-
                # load before STT-routed text turns into a reply.
                "keep_alive": -1,
                "options": {
                    "num_ctx": self.context_length,
                    # Slight bias toward sticking to instructions / tool use
                    # over creative riffs. Voice replies want predictability.
                    "temperature": 0.3,
                },
            }
            try:
                r = await self.client.post(f"{self.base_url}/api/chat", json=payload)
                r.raise_for_status()
            except httpx.TimeoutException as exc:
                # Timeouts almost always mean the model overflowed VRAM
                # and is grinding partially on CPU. Auto-ban so the next
                # server restart picks something smaller / quantized.
                log.error("ollama call timed out (%s) — auto-banning %s",
                          exc, self.model)
                banlist.add(self.model, reason="chat call timed out")
                fail = (
                    f"The local model timed out and was just banned from "
                    f"future picks. Restart the server to load a smaller "
                    f"model."
                )
                conversation.add_assistant_text(fail)
                return fail
            except httpx.HTTPError as exc:
                log.exception("ollama call failed")
                fail = f"The local model didn't respond: {exc}"
                conversation.add_assistant_text(fail)
                return fail

            data = r.json()
            msg = data.get("message") or {}
            log.debug("ollama turn %d: keys=%s eval_count=%s history_len=%d",
                      i, list(msg.keys()), data.get("eval_count"),
                      len(conversation.messages) - history_prefix_len)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                text = (msg.get("content") or "").strip() or "(no response)"
                conversation.add_assistant_text(text)
                return text

            # Append the assistant tool-call turn + each tool result.
            # Ollama's next call needs to see these as history; future
            # turns benefit too ("you just looked up X").
            conversation.extend([msg])
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments")
                args = _parse_args(raw_args)
                tool = self.registry.get(name)
                if tool is None:
                    out = f"(unknown tool: {name})"
                else:
                    try:
                        out = await tool.handler(args)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("tool %s failed", name)
                        out = f"(tool error: {exc})"
                conversation.extend([{
                    "role": "tool",
                    "name": name,
                    "content": out,
                }])

        log.warning("hit tool-use iteration cap (%d)", self.max_iter)
        stuck = "Sorry, I got stuck thinking about that."
        conversation.add_assistant_text(stuck)
        return stuck


def _parse_args(raw: Any) -> dict[str, Any]:
    """Ollama returns `arguments` as either a dict (newer builds) or a
    JSON-string (older builds, OpenAI-style). Normalize both."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def ensure_pulled(
    base_url: str,
    model: str,
    *,
    timeout: float = 60.0 * 30,  # large models can take a while
) -> None:
    """Make sure Ollama has the given model locally. If not, pull it,
    streaming progress logs. Then warm-load it into VRAM with
    keep_alive=-1 so the first user utterance doesn't pay a cold-load."""
    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as client:
        # /api/show 200 = present, 404 = need to pull.
        show = await client.post(f"{base}/api/show", json={"name": model})
        if show.status_code == 200:
            log.info("ollama model already present: %s", model)
        elif show.status_code == 404:
            log.info("pulling ollama model: %s (this may take a while)", model)
            async with client.stream(
                "POST", f"{base}/api/pull",
                json={"name": model, "stream": True},
            ) as resp:
                resp.raise_for_status()
                last_pct = -1
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total = msg.get("total")
                    completed = msg.get("completed")
                    status = msg.get("status", "")
                    if isinstance(total, int) and isinstance(completed, int) and total > 0:
                        pct = int(completed * 100 / total)
                        if pct != last_pct and pct % 10 == 0:
                            log.info("pull %s: %d%% (%s)", model, pct, status)
                            last_pct = pct
                    elif status:
                        log.info("pull %s: %s", model, status)
            log.info("pull complete: %s", model)
        else:
            show.raise_for_status()

        # Warm-load: a no-op generate with empty prompt is enough to
        # force the weights into VRAM. keep_alive=-1 pins them there
        # so subsequent /api/chat calls hit a hot model.
        log.info("warm-loading %s into VRAM (keep_alive=-1)", model)
        try:
            r = await client.post(
                f"{base}/api/generate",
                json={"model": model, "prompt": "", "keep_alive": -1, "stream": False},
            )
            r.raise_for_status()
            log.info("ollama model resident: %s", model)
        except httpx.HTTPError as exc:
            # Non-fatal: the first real request will load instead.
            log.warning("warm-load failed (%s); first request will be cold", exc)
