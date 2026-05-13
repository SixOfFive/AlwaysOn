"""Per-client WebSocket session.

State machine: HELLO -> idle -> (wake -> audio frames -> end_utterance ->
STT -> router -> say)*

Audio frames between Wake and EndUtterance are raw PCM (s16le, 16 kHz,
mono). They get accumulated into a single bytes buffer that's handed to
faster-whisper at end-of-utterance.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from jarvis_server.active_session import ActiveSession
from jarvis_server.conversation import Conversation
from jarvis_server.dictation import log_utterance
from jarvis_server.router import Router
from jarvis_server.stt import STT
from jarvis_server.trigger import extract as extract_trigger
from jarvis_shared import (
    PROTOCOL_VERSION,
    Command,
    ContextCleared,
    EndUtterance,
    ErrorMsg,
    Hello,
    Pong,
    ResetContext,
    Say,
    Thinking,
    Transcript,
    Wake,
    Welcome,
    parse_control,
)

log = logging.getLogger(__name__)


class Session:
    def __init__(
        self,
        ws: WebSocket,
        *,
        stt: STT,
        router: Router,
        idle_reset_sec: int = 300,
        trigger_phrase: str = "computer",
    ) -> None:
        self.ws = ws
        self.stt = stt
        self.router = router
        self.idle_reset_sec = idle_reset_sec
        self.trigger_phrase = trigger_phrase
        self.client_id: str | None = None
        self.hostname: str | None = None
        self.session_id: str | None = None
        self._audio = bytearray()
        self._in_utterance = False
        # If the client did its own wake-word check, this holds the keyword
        # it matched on for the *currently-accumulating* utterance. Empty
        # string means "no client-side check — server should apply a
        # transcript-based trigger before routing". Captured into the
        # queue entry at EndUtterance time, then reset.
        self._wake_keyword: str = ""
        # Running multi-turn history. Auto-reset after idle_reset_sec.
        self.conv = Conversation()
        # FIFO queue of completed utterances awaiting STT + routing. The
        # main loop pushes; a dedicated worker task drains. This is what
        # lets the user fire off "computer, X" while a previous reply is
        # still being thought-through — neither the receive loop nor
        # subsequent utterances block on each other.
        self._utterance_queue: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
        self._utterance_worker: asyncio.Task[None] | None = None

    async def run(self) -> None:
        try:
            await self._handshake()
            ActiveSession.set(self)
            self._utterance_worker = asyncio.create_task(self._utterance_loop())
            await self._main_loop()
        except WebSocketDisconnect:
            log.info("client disconnected: %s", self.client_id)
        finally:
            ActiveSession.clear(self)
            if self._utterance_worker is not None:
                self._utterance_worker.cancel()
                try:
                    await self._utterance_worker
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._utterance_worker = None

    async def _handshake(self) -> None:
        raw = await asyncio.wait_for(self.ws.receive_text(), timeout=5.0)
        try:
            msg = parse_control(raw)
        except ValidationError as exc:
            await self._send(ErrorMsg(code="bad_hello", message=str(exc)))
            await self.ws.close()
            raise

        if not isinstance(msg, Hello):
            await self._send(ErrorMsg(code="expected_hello", message="first frame must be hello"))
            await self.ws.close()
            raise RuntimeError("first frame was not hello")

        if msg.version != PROTOCOL_VERSION:
            await self._send(ErrorMsg(
                code="version_mismatch",
                message=f"server speaks protocol v{PROTOCOL_VERSION}",
            ))
            await self.ws.close()
            raise RuntimeError(f"client speaks v{msg.version}")

        self.client_id = msg.client_id
        self.hostname = msg.hostname
        log.info("client connected: id=%s host=%s", self.client_id, self.hostname)

        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")[:-3]
        await self._send(Welcome(session_id=self.session_id))

    async def _main_loop(self) -> None:
        while True:
            event = await self.ws.receive()
            if event["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()

            if (data := event.get("text")) is not None:
                await self._on_control(data)
            elif (data := event.get("bytes")) is not None and self._in_utterance:
                self._audio.extend(data)

    async def _on_control(self, raw: str) -> None:
        try:
            msg = parse_control(raw)
        except ValidationError as exc:
            log.warning("bad control frame: %s", exc)
            await self._send(ErrorMsg(code="bad_frame", message=str(exc)))
            return

        if isinstance(msg, Wake):
            self._audio.clear()
            self._in_utterance = True
            self._wake_keyword = msg.keyword
            log.info("wake: keyword=%r conf=%.2f", msg.keyword, msg.confidence)

        elif isinstance(msg, EndUtterance):
            self._in_utterance = False
            buf = bytes(self._audio)
            self._audio.clear()
            # Snapshot the wake_keyword for *this* utterance and reset
            # for the next one, so a queued utterance doesn't get its
            # trigger semantics overwritten by a later Wake.
            keyword = self._wake_keyword
            self._wake_keyword = ""
            await self._utterance_queue.put((buf, keyword))
            qsize = self._utterance_queue.qsize()
            if qsize > 1:
                log.info("utterance queued (%d pending)", qsize)

        elif isinstance(msg, Command):
            # Client transcribed locally; just route the text.
            log.info("command: %r", msg.text)
            await self._on_text(msg.text)

        elif isinstance(msg, ResetContext):
            # User asked for a clean slate. Wipe history immediately and
            # ack so the client can clear its UI. Don't touch in-flight
            # audio buffers — those are tied to the next utterance, not
            # to history.
            had = len(self.conv.messages)
            self.conv.messages.clear()
            self.conv.touch()
            log.info(
                "session %s: conversation reset by client (was %d msg)",
                self.session_id, had,
            )
            await self._send(ContextCleared())

        elif msg.type == "ping":
            await self._send(Pong())

        elif msg.type == "cancel":
            self._in_utterance = False
            self._audio.clear()
            # Drain any queued utterances too — user wants a clean stop,
            # not "process the backlog after a moment".
            drained = 0
            while not self._utterance_queue.empty():
                try:
                    self._utterance_queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                log.info("cancel: dropped %d queued utterance(s)", drained)
            else:
                log.info("cancel")

        else:
            log.warning("unexpected control frame from client: %s", msg.type)

    async def _utterance_loop(self) -> None:
        """Drain the utterance queue, processing one at a time in FIFO
        order. Cancellation (websocket disconnect) ends it cleanly."""
        while True:
            buf, keyword = await self._utterance_queue.get()
            try:
                await self._on_utterance(buf, keyword)
            except Exception:  # noqa: BLE001
                log.exception("utterance processing crashed; continuing")

    async def _on_utterance(self, pcm: bytes, wake_keyword: str) -> None:
        if len(pcm) < 4_000:  # < 0.125s — discard tap/noise
            log.info("utterance too short (%d bytes), discarding", len(pcm))
            await self._send(Say(text=""))
            return

        await self._send(Thinking(note="transcribing"))
        text = await self.stt.transcribe(pcm)
        await self._send(Transcript(text=text, final=True))

        # If the client streamed audio without doing its own wake-word check
        # (Wake.keyword == ""), require the literal trigger phrase in the
        # transcript. This is the Android "always-listening" path. The
        # Python desktop client, which runs openWakeWord on-device, sends
        # a non-empty keyword and we route the whole transcript as-is.
        routable: str | None
        pre_trigger: str = ""
        if wake_keyword:
            routable = text
        else:
            split = extract_trigger(text, self.trigger_phrase)
            if split is None:
                routable = None
            else:
                pre_trigger, routable = split

        # Log every transcript (commands and ambient chat alike) to the
        # daily dictation file. The [cmd] marker comes from whether the
        # wake-word check decided this was addressed to the assistant.
        await log_utterance(text, is_command=routable is not None)

        if not text.strip():
            # Nothing audible — don't touch the conversation history.
            return

        # Every utterance counts as activity, so idle-reset is decided
        # here before either branch records its turn.
        self._maybe_reset_idle()

        if routable is None:
            # The mic heard someone — but not addressing us. Record it
            # as overheard context so a later triggered turn can refer
            # back to it ("hey computer, what was she just saying about
            # the weather?"). Do not route.
            self.conv.add_overheard(text)
            log.info("overheard (not routed): %r", text)
            return

        # Pre-trigger speech in the SAME utterance is preserved as
        # overheard context so phrases like "Tokyo sucks. Computer,
        # what's the weather there?" can resolve "there" to Tokyo.
        if pre_trigger:
            self.conv.add_overheard(pre_trigger)
            log.info("pre-trigger context recorded: %r", pre_trigger)

        # Triggered command. Record the cleaned command (post-trigger
        # extraction) so the LLM sees what was actually asked, not the
        # whole "computer, ..." preamble.
        self.conv.add_user(routable)
        await self._route(routable)

    async def _on_text(self, text: str) -> None:
        """Client transcribed locally and sent us a `Command`. There's
        no overheard track here — these are always addressed to us."""
        if not text.strip():
            await self._send(Say(text="I didn't catch that."))
            return
        self._maybe_reset_idle()
        self.conv.add_user(text)
        await self._route(text)

    def _maybe_reset_idle(self) -> None:
        if self.conv.reset_if_idle(self.idle_reset_sec):
            log.info(
                "session %s: conversation reset (idle >= %d seconds)",
                self.session_id, self.idle_reset_sec,
            )

    async def _route(self, text: str) -> None:
        await self._send(Thinking(note="routing"))
        try:
            reply = await self.router.handle(text, self.conv)
        except Exception as exc:  # noqa: BLE001
            log.exception("router crashed")
            reply = f"Something went wrong: {exc}"
            # Still record an assistant turn so the next prompt sees we
            # said *something*, even if it's an error string.
            self.conv.add_assistant_text(reply)
        await self._send(Say(text=reply))

    async def _send(self, msg: object) -> None:
        await self.ws.send_text(msg.model_dump_json())  # type: ignore[attr-defined]
