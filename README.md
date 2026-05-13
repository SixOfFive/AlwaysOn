# AO — "Hey Jarvis" voice assistant

Always-listening voice assistant. Wake-word triggered, local STT on the
4070, dispatches to built-in tools or to Claude for the open-ended cases.

## Architecture

Two processes, one WebSocket between them.

```
┌──────────────────────────┐         ┌──────────────────────────┐
│  jarvis-client           │   ws    │  jarvis-server           │
│  (mic, wake, VAD, TTS)   │ ──────► │  (STT, router, Claude)   │
│                          │ ◄────── │                          │
└──────────────────────────┘         └──────────────────────────┘
```

Wake word and VAD live on the client so audio only crosses the wire
during an active utterance. Idle cost is near zero.

For this rig, both run on `192.168.15.103` (the 4070 box). The split is
preserved so a phone or second machine can join later as just another
client.

## Layout

```
ao/
├── shared/        # protocol — message schemas, imported by both
├── server/        # jarvis-server — FastAPI + websocket, STT, tools, Claude, MCP
└── client/        # jarvis-client — mic, wake word, VAD, TTS, ws client
```

Each module has its own `pyproject.toml`. `shared` is a path dependency
of the other two.

## First-time setup

```powershell
# from repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -e .\shared
pip install -e .\server
pip install -e .\client
```

## Run

Two terminals, both with the venv activated.

```powershell
# terminal 1 — server
python -m jarvis_server

# terminal 2 — client (synthetic-utterance mode until audio lands)
python -m jarvis_client
```

Expected output: client connects, server welcomes, client fires a
synthetic wake → silence-frames → end_utterance, server logs and
replies with a placeholder Say.

## Status

Skeleton. Protocol defined, WebSocket handshake works end-to-end, audio
frames counted server-side. Audio capture, wake word, STT, and tools
land next.
