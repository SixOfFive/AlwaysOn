"""Wire protocol between jarvis-client and jarvis-server.

Control messages are JSON text frames with a discriminator "type" field.
Audio is sent as raw binary frames between a Wake message and an
EndUtterance message: 16 kHz, mono, signed 16-bit little-endian PCM.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

PROTOCOL_VERSION = 1
AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS = 1
AUDIO_BITS_PER_SAMPLE = 16


# --- client → server ---

class Hello(BaseModel):
    type: Literal["hello"] = "hello"
    version: int = PROTOCOL_VERSION
    client_id: str
    hostname: str


class Wake(BaseModel):
    type: Literal["wake"] = "wake"
    keyword: str
    confidence: float = 0.0
    unix_millis: int


class EndUtterance(BaseModel):
    type: Literal["end_utterance"] = "end_utterance"


class Command(BaseModel):
    """Client already transcribed locally; this is the parsed command
    text to act on. Use this instead of Wake+audio+EndUtterance when STT
    runs on the client."""
    type: Literal["command"] = "command"
    text: str


class Cancel(BaseModel):
    type: Literal["cancel"] = "cancel"


class ResetContext(BaseModel):
    """Tell the server to drop all conversation history for this session
    immediately. The server replies with ContextCleared once done."""
    type: Literal["reset_context"] = "reset_context"


class Ping(BaseModel):
    type: Literal["ping"] = "ping"


# --- server → client ---

class Welcome(BaseModel):
    type: Literal["welcome"] = "welcome"
    session_id: str
    version: int = PROTOCOL_VERSION


class Transcript(BaseModel):
    """STT output. final=False is interim, final=True is what the router acts on."""
    type: Literal["transcript"] = "transcript"
    text: str
    final: bool = False


class Thinking(BaseModel):
    type: Literal["thinking"] = "thinking"
    note: str = ""


class Say(BaseModel):
    """What the client should speak. If audio_url is set the client fetches and
    plays it; otherwise the client TTSes text locally.

    `mute_mic` tells the client to suppress mic capture (drop chunks
    entirely — don't feed VAD, don't ship to server) for the duration
    of the TTS playback. Prevents the assistant from transcribing its
    own voice. Default true; the server flips it false only in the
    rare "always-hot mic" configurations."""
    type: Literal["say"] = "say"
    text: str
    audio_url: str | None = None
    mute_mic: bool = True


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


class Pong(BaseModel):
    type: Literal["pong"] = "pong"


class ContextCleared(BaseModel):
    """Server confirms that ResetContext was processed; the conversation
    is now empty. Client should clear any UI it was showing for the
    previous turns."""
    type: Literal["context_cleared"] = "context_cleared"


# Tagged union for parsing any inbound control frame.
ControlMessage = Annotated[
    Union[
        Hello, Wake, EndUtterance, Command, Cancel, ResetContext, Ping,
        Welcome, Transcript, Thinking, Say, ErrorMsg, Pong, ContextCleared,
    ],
    Field(discriminator="type"),
]

_control_adapter: TypeAdapter[ControlMessage] = TypeAdapter(ControlMessage)


def parse_control(data: str | bytes) -> ControlMessage:
    """Parse a JSON control frame into its typed model. Raises pydantic
    ValidationError on unknown type or schema mismatch."""
    return _control_adapter.validate_json(data)
