# AO — "Hey Jarvis" voice assistant

Always-listening voice assistant. Wake-word triggered, local STT on CPU,
dispatches to built-in tools or to Claude for the open-ended cases.
Hooks into the user's Obsidian vault MCP for "where did I leave off"
style queries.

## Architecture

Two processes, one WebSocket between them.

```
┌──────────────────────────────────┐        ┌────────────────────────────────┐
│  jarvis-client                   │   ws   │  jarvis-server                 │
│  ────────────────────────────    │ ─────▶ │  ────────────────────────────  │
│  sounddevice mic capture         │        │  per-client Session            │
│  openwakeword "hey_jarvis"       │        │  faster-whisper STT  (CPU)     │
│  silero-vad end-of-utterance     │ ◀───── │  Router (regex fast path)      │
│  pyttsx3 TTS (Windows SAPI)      │        │   ├─ builtin tools             │
│                                  │        │   ├─ vault tools (MCP)         │
│                                  │        │   └─ Claude (tool-use loop)    │
└──────────────────────────────────┘        └────────────────────────────────┘
```

Wake word and VAD live on the client so audio only crosses the wire
during an active utterance. STT runs on CPU (`small.en`, int8) so the
4070 stays free for LLM inference and other GPU work.

## Layout

```
ao/
├── shared/        # protocol — pydantic message schemas, imported by both
├── server/        # jarvis-server — STT, router, tools, Claude
└── client/        # jarvis-client — mic, wake word, VAD, TTS
```

## First-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -e .\shared
pip install -e .\server
pip install -e .\client
```

The first server boot downloads `faster-whisper-small.en` from Hugging
Face (~470 MB). The first client boot downloads the `hey_jarvis`
openWakeWord model (~tens of MB). After that it's all local.

## Environment

Optional, but Claude fallback needs `ANTHROPIC_API_KEY`. Vault tools
need the `obsidian-vault` MCP server reachable (defaults to
`python %USERPROFILE%\.claude\scripts\vault-server.py`).

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Other knobs (all optional, env-driven, see `server/jarvis_server/config.py`):

| Var | Default | Purpose |
|---|---|---|
| `JARVIS_STT_MODEL` | `small.en` | faster-whisper model name |
| `JARVIS_STT_DEVICE` | `cpu` | `cpu` or `cuda` |
| `JARVIS_STT_COMPUTE_TYPE` | `int8` | precision |
| `JARVIS_CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Claude model id |
| `JARVIS_VAULT_DISABLED` | _(unset)_ | set to `1` to skip vault MCP |
| `JARVIS_VAULT_CMD` | `python` | command that starts the vault MCP server |
| `JARVIS_VAULT_ARGS` | _(see config)_ | args for the vault command |

## Run

Two terminals, both with the venv activated.

```powershell
# terminal 1 — server (host 0.0.0.0 if you want clients from other machines)
python -m jarvis_server

# terminal 2 — client (live = real mic; synthetic = wire smoke test)
python -m jarvis_client
python -m jarvis_client --list-audio-devices
python -m jarvis_client --audio-device 9 --wake-threshold 0.6
python -m jarvis_client --mode synthetic   # silent round-trip
```

Say "**Hey Jarvis**" and pause. The client streams the next sentence,
VAD trims the silence, the server transcribes and replies, and the
client speaks the response.

Try:
- "Hey Jarvis, what time is it?" → fast path, no Claude call
- "Hey Jarvis, what's the date?" → fast path
- "Hey Jarvis, what was I working on last?" → routes to Claude →
  Claude calls `vault_list_recent` → spoken summary

## Status

- ✓ Protocol, transport, handshake
- ✓ Wake word ("hey_jarvis"), VAD, audio capture
- ✓ STT (faster-whisper CPU)
- ✓ Builtin tools (time, date)
- ✓ Vault MCP integration (list recent, semantic search, read topic)
- ✓ Claude tool-use loop with prompt caching
- ✓ TTS playback (pyttsx3)

Future polish: tray icon, reconnect on disconnect, barge-in (interrupt
TTS with a new wake), Piper/Coqui TTS for a more natural voice, model
size auto-pick based on hardware.
