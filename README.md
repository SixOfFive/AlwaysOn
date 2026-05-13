# AO — always-listening voice assistant

LAN-only voice assistant. Wake-phrase "computer". Server-side STT on
CUDA Whisper, a local Ollama LLM for tool routing, and Obsidian vault
integration for "what was I working on" style queries. Clients on
Windows, Linux, and Android speak the same WebSocket protocol.

> ⚠️ **Work in progress.** This is active development, not a release.
> Expect rough edges, breaking changes between commits, and possibly
> critical or fatal bugs — clients have crashed, the server has hung,
> the LLM has produced wrong tool calls. The audio pipeline writes to
> your filesystem (dictation logs, save_code) and pulls models from the
> network; review what you're running before pointing it at anything
> sensitive. No guarantees; use at your own risk. PRs and bug reports
> welcome but the architecture may change without notice.

## Architecture

```
┌──────────────────────┐                       ┌────────────────────────────┐
│  Windows client      │                       │  jarvis-server (FastAPI)   │
│  Linux client        │       ws audio        │  ────────────────────────  │
│  Android client      │ ────────────────────▶ │  faster-whisper (CUDA)     │
│                      │                       │  router + fast-path regex  │
│  - mic capture       │                       │   ├─ builtin tools         │
│  - Silero VAD        │ ◀──── ws control ──── │   ├─ vault tools (MCP)     │
│  - TTS playback      │                       │   └─ OllamaRouter ─────┐   │
│  - mic muted in TTS  │                       │      (catalog-picked)  │   │
└──────────────────────┘                       └─────────────────┬──────┘   │
                                                                 │          │
                                                                 ▼          │
                                                       Ollama on 4070 box   │
                                                       (configured URL)     │
                                                                            │
                                                                            │
                                                          dictation logs ───┘
                                                          → Obsidian vault
```

**Audio flow per utterance** (Android `ENGINE_SERVER` / Python `stream` mode):
1. Client captures 16 kHz mono PCM via the OS audio stack.
2. Silero VAD runs locally to detect end-of-speech.
3. Client sends `Wake(keyword="")` + raw PCM bytes + `EndUtterance`.
4. Server transcribes on CUDA (Whisper `large-v3` by default), appends
   the transcript to today's dictation log, then checks for the
   "computer" trigger phrase. If present, the command runs through the
   router (fast-path regex first, then Ollama with tool access).
5. Server replies with `Transcript` (always, for the UI/log) and `Say`
   (only when the trigger matched). Client speaks the reply via TTS;
   mic chunks are dropped while TTS is playing so the assistant can't
   hear itself.

**Privacy posture**: audio travels only on the LAN to your server. The
server hits the Anthropic/OpenAI APIs *never* — the LLM is your own
Ollama instance. The catalog is fetched once a day from a public GitHub
URL to pick the best model; no telemetry, no auth.

## Repo layout

```
ao/
├── shared/            # Pydantic protocol schemas (used by both Python sides)
├── server/            # jarvis-server: STT, router, tools, Ollama, dictation
├── client/            # Python desktop client (Windows + Linux)
├── android/           # Kotlin/JNI Android client
├── braindump_ao.md    # state snapshot, read before significant changes
└── README.md
```

## Components

### `server/` — jarvis-server (FastAPI on port 7333)
- **STT**: faster-whisper, CUDA by default. NVIDIA pip-wheel DLLs are
  registered automatically via `_cuda.py` (Windows only).
- **Router**: regex fast-path for `get_time`, `get_date`, `get_weather`,
  `set_timer`, `append_note`, `wake_on_lan`, `web_search`; everything
  else falls through to the LLM.
- **OllamaRouter**: talks `/api/chat` with OpenAI-style `tools=[...]`.
  Auto-pulls the model on startup if missing.
- **Catalog**: downloads
  `https://raw.githubusercontent.com/SixOfFive/TypeCast/main/models-catalog.json`,
  caches under `%LOCALAPPDATA%\jarvis-server\`, picks the best
  tool-capable model that fits the VRAM budget.
- **Tools** (in `server/jarvis_server/tools/`): time, date, weather
  (wttr.in), timer, notes, wikipedia, wake-on-LAN, DuckDuckGo search,
  save-code, obsidian vault (via MCP stdio).
- **Dictation log**: every transcript appended to
  `Dictation/YYYY-MM-DD.md` in the Obsidian vault, commands tagged
  with `` `[cmd]` ``.
- **Trigger filter**: when the client sends `Wake(keyword="")` (i.e.
  did no on-device wake check), the server filters transcripts for the
  literal phrase "computer" before routing.

### `client/` — Python desktop client (Windows + Linux)
Modes (`--mode <name>`):
- **stream** *(default)* — local VAD, server does STT + routing. Matches
  Android `ENGINE_SERVER`. The recommended mode.
- **transcribe** — local faster-whisper, prints transcripts, optionally
  sends commands to the server. Useful for offline debugging.
- **live** — legacy openWakeWord pipeline; audio streamed to server
  only after the wake-word fires locally.
- **synthetic** — wire smoke test, no mic.

### `android/` — Android Kotlin client
Engines (set on start, default is `ENGINE_SERVER`):
- **ENGINE_SERVER** *(default)* — VAD locally, ship audio to the server.
- **ENGINE_WHISPER** — on-device whisper.cpp via JNI. Slow on budget
  phones, but works offline.
- **ENGINE_SYSTEM** — Android's built-in `SpeechRecognizer`.

Foreground service keeps the mic alive with the screen off. Transcripts
land in `/sdcard/Android/data/com.sixoffive.ao.jarvis/files/transcripts/YYYY-MM-DD.log`.

## First-time setup

### Server + Python client (Windows)

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -e .\shared
pip install -e .\server
pip install -e .\client
```

For CUDA STT on Windows, install the NVIDIA pip wheels — `_cuda.py`
registers their DLL directories at import time:
```cmd
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```
First server boot downloads Whisper `large-v3` from Hugging Face (~3 GB).

### Server + Python client (Linux)

```bash
sudo apt install portaudio19-dev espeak    # sounddevice + pyttsx3 fallback
python -m venv .venv
source .venv/bin/activate
pip install -e ./shared
pip install -e ./server
pip install -e ./client
```
For CUDA STT on Linux, use a CUDA-enabled PyTorch wheel or install the
CUDA toolkit system-wide. `_cuda.py` is a no-op on non-Windows.

### Ollama

Code default is `http://localhost:11434`. Set `[ollama].url` in
`jarvis.toml` (or `JARVIS_OLLAMA_URL`) to point at whichever box runs
Ollama. If it's a different machine on your LAN, make Ollama listen on
the LAN address before starting:
```cmd
:: on the Ollama host, allow LAN connections
set OLLAMA_HOST=0.0.0.0
ollama serve
```

### Android

Open `android/` in Android Studio. minSdk 31, Gradle wrapper pinned to
8.10.2. First native build is ~10 minutes (whisper.cpp + llama.cpp
compiled from source). The classifier model is shipped but disabled in
`JarvisService.kt`; on-device whisper is also there as a fallback engine.

See `android/README.md` for details and gotchas.

## Run

Three terminals (or two + your phone):

```cmd
:: Server (binds 0.0.0.0:7333 by default)
.venv\Scripts\python.exe -m jarvis_server

:: Desktop client — stream mode by default
:: Replace the host with wherever your jarvis-server runs.
.venv\Scripts\python.exe -m jarvis_client --server ws://<server-host>:7333/ws

:: Or list mic devices first
.venv\Scripts\python.exe -m jarvis_client --list-audio-devices
.venv\Scripts\python.exe -m jarvis_client --audio-device 9 --server ws://<server-host>:7333/ws
```

Try:
- "Computer, what time is it?" → fast path, no LLM
- "Computer, weather?" → fast path (wttr.in)
- "Computer, set a five minute timer." → fast path
- "Computer, search the web for current bitcoin price." → fast path snippet
- "Computer, what was I working on last week?" → LLM → vault tool

## Configuration

The server reads `jarvis.toml` at startup. The committed
[`jarvis.example.toml`](jarvis.example.toml) is the schema +
documentation; copy it to `jarvis.toml` (gitignored) and edit for your
setup.

```cmd
copy jarvis.example.toml jarvis.toml
notepad jarvis.toml
```

**Loading order**, first match wins:

1. `$JARVIS_CONFIG` — explicit override path
2. `./jarvis.toml` — cwd
3. `<repo root>/jarvis.toml`
4. `~/.config/jarvis/jarvis.toml`
5. built-in defaults (generic — `localhost`, `~/jarvis-notes`, etc.)

**Env vars always win over TOML.** So you can ship a TOML and override
one knob per launch with `set JARVIS_OLLAMA_MODEL=qwen2.5:7b` (CMD)
without editing the file.

### TOML schema

The file is plain TOML. Top-level scalars live at the root; the rest is
grouped under `[stt]`, `[ollama]`, `[session]`, `[paths]`, `[vault]`,
`[wol]`.

```toml
# Top-level
trigger_phrase = "computer"               # string. wake phrase to filter
                                          # transcripts on. regex auto-
                                          # allows "hey ", "ok ", "okay "
                                          # before this word.

[stt]
model         = "large-v3"                # string. faster-whisper model
device        = "cuda"                    # "cuda" or "cpu"
compute_type  = "float16"                 # "float16" (cuda) or "int8" (cpu)

[ollama]
url             = "http://localhost:11434" # string. Ollama endpoint
vram_budget_gb  = 14                       # number. catalog filter: max
                                           # (model + KV-cache) GB
context_length  = 16384                    # int. num_ctx for chat calls
model_override  = ""                       # string. exact Ollama tag to
                                           # bypass catalog selection.
                                           # empty = let catalog pick.
server_name     = ""                       # string. catalog key for the
                                           # "already pulled" tiebreak —
                                           # match a localServers[].name
                                           # from the catalog. empty
                                           # disables the tiebreak.
catalog_url     = "https://raw.githubusercontent.com/SixOfFive/TypeCast/main/models-catalog.json"

[session]
idle_reset_sec  = 300                      # int. clear conversation
                                           # history after N idle seconds.
                                           # 0 disables auto-reset.

[paths]
notes_dir       = "~/jarvis-notes"         # string. where append_note writes
dictation_dir   = "~/jarvis-dictation"     # string. where transcript log writes
workspace_dir   = "~/jarvis-workspace"     # string. where save_code writes

[vault]
disabled        = false                    # bool. true skips MCP entirely
command         = "python"                 # string. how to launch the MCP
args            = ["~/.claude/scripts/vault-server.py"]  # list[string]

[wol]
broadcast       = "255.255.255.255"        # string. UDP broadcast addr
port            = 9                        # int. UDP port
hosts           = {}                       # table. name → MAC. example:
                                           # hosts = { bigiron = "AA:BB:CC:DD:EE:FF" }
```

`~` is expanded on `notes_dir`, `dictation_dir`, `workspace_dir`, and
each entry of `vault.args`. Windows paths can use single-quoted strings
to avoid backslash-escape headaches: `notes_dir = 'C:\Users\me\Obsidian\Inbox'`.

### Recipes

**Move Ollama to another box**
```toml
[ollama]
url = "http://192.168.15.103:11434"
```

**Land notes + dictation logs in Obsidian** (the daily files get
written here; commands tagged `` `[cmd]` ``)
```toml
[paths]
notes_dir = 'C:\Users\me\Documents\Obsidian\Vault\Inbox'
dictation_dir = 'C:\Users\me\Documents\Obsidian\Vault\Dictation'
```

**Change the wake word**
```toml
trigger_phrase = "jarvis"
```
The phrase passes through `re.escape`, so multi-word phrases like
`"hey friday"` work too. (The regex also auto-allows `hey/ok/okay`
prefixes, so don't double up.)

**Add Wake-on-LAN targets**
```toml
[wol]
hosts = { bigiron = "AA:BB:CC:DD:EE:FF", nuc = "11:22:33:44:55:66" }
```
Then "Computer, wake bigiron" works.

**Lighter model on a smaller card**
```toml
[ollama]
vram_budget_gb = 8
context_length = 8192
```
The catalog filter will pick a smaller-VRAM model. Or skip the catalog
entirely:
```toml
[ollama]
model_override = "qwen2.5:3b-instruct-q5_K_M"
```

**Disable the Obsidian vault MCP**
```toml
[vault]
disabled = true
```

**Run STT on CPU** (e.g. headless server, no GPU)
```toml
[stt]
model = "small.en"
device = "cpu"
compute_type = "int8"
```

### Ollama model stays resident

The server passes `keep_alive: -1` on every chat call and does a
warm-load right after `ensure_pulled`, so the model lives in VRAM
permanently — no eviction after 5 min of idle, no cold-load latency
on the first utterance of the day. Look for the line
`ollama model resident: <tag>` in the startup log.

If you ever need to swap models mid-session (e.g. via
`JARVIS_OLLAMA_MODEL`), restart the server: there's no graceful
"unload the previous model" hook because we never want one.

### Model banlist

The catalog reports `estimatedVramGb` but it's an estimate — when a
picked model spills out of VRAM to CPU you'll see chat calls hang for
the full request timeout (~90-120 s). When that happens the router
**auto-appends the running model tag to `model-banlist.txt`** and
replies with "the local model timed out and was just banned from
future picks. Restart the server to load a smaller model."

On the next startup the picker filters out anything in the banlist and
the catalog's next-best fit gets selected automatically. You'll see
this in the log:

```
[server] INFO jarvis_server.app — banlist active: skipping 1 model(s) — gemma4:e4b
[server] INFO jarvis_server.app — model pick: <next-tag> — ...
```

The file is gitignored (it's per-host — your VRAM, your bans). Format:
plain text, one Ollama tag per line, `#` for comments:

```
# Ollama tags the catalog picker should skip on this host.
gemma4:e4b   # spilled to CPU on this 16 GB card
qwen2.5:32b  # too slow even when it fits
```

You can hand-edit anytime. Delete the file (or remove a line) to put a
model back in rotation. Override the path with `JARVIS_BANLIST=<path>`
if you want the banlist somewhere else.

## Environment variables

All optional. CMD examples below; PowerShell uses `$env:NAME = "value"`.

### Server

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_CONFIG` | _(see loading order above)_ | path to a `jarvis.toml` to load |
| `JARVIS_TRIGGER` | `computer` | wake phrase (regex auto-allows "hey/ok/okay " before it) |
| `JARVIS_STT_MODEL` | `large-v3` | faster-whisper model (`tiny.en`, `base.en`, `small.en`, `medium.en`, `large-v3`, …) |
| `JARVIS_STT_DEVICE` | `cuda` | `cuda` or `cpu` |
| `JARVIS_STT_COMPUTE_TYPE` | `float16` | `float16` for CUDA, `int8` for CPU |
| `JARVIS_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `JARVIS_OLLAMA_VRAM_BUDGET` | `14` | max GB the picked model + KV-cache may use |
| `JARVIS_OLLAMA_CONTEXT` | `16384` | `num_ctx` for chat requests |
| `JARVIS_OLLAMA_MODEL` | _(unset)_ | hard override of catalog pick, e.g. `qwen2.5:7b-instruct-q5_K_M` |
| `JARVIS_OLLAMA_SERVER_NAME` | _(empty)_ | catalog key for the "model is already installed locally" tiebreak — set to your `localServers[].name` from the catalog, leave empty to disable the tiebreak |
| `JARVIS_CATALOG_URL` | TypeCast `models-catalog.json` on GitHub | model catalog URL |
| `JARVIS_IDLE_RESET_SEC` | `300` | seconds of inactivity before the per-session conversation history is cleared |
| `JARVIS_VAULT_DISABLED` | _(unset)_ | set to `1` to skip the vault MCP entirely |
| `JARVIS_VAULT_CMD` | `python` | command that starts the vault MCP server |
| `JARVIS_VAULT_ARGS` | `~/.claude/scripts/vault-server.py` | args for the vault command |
| `JARVIS_NOTES_DIR` | `~/jarvis-notes` | where `append_note` writes |
| `JARVIS_DICTATION_DIR` | `~/jarvis-dictation` | where the daily transcript log writes |
| `JARVIS_WORKSPACE` | `~/jarvis-workspace` | where `save_code` drops generated files |
| `JARVIS_HOSTS` | `{}` | JSON map of hostname→MAC for `wake_on_lan`, e.g. `{"bigiron":"AA:BB:CC:DD:EE:FF"}` |
| `JARVIS_WOL_BROADCAST` | `255.255.255.255` | UDP broadcast address for WoL magic packets |
| `JARVIS_WOL_PORT` | `9` | UDP port for WoL magic packets |

### Python client

The desktop client is configured via CLI flags only — no env vars. See
`python -m jarvis_client --help` for the full list. Common ones:

```cmd
.venv\Scripts\python.exe -m jarvis_client ^
    --mode stream ^
    --server ws://<server-host>:7333/ws ^
    --audio-device 9 ^
    --speech-threshold 0.5 ^
    --min-silence-ms 700
```

### Android

No env vars — settings live in the app's UI (server URL persists in
SharedPreferences). Engine defaults to `ENGINE_SERVER`; flip to
`ENGINE_WHISPER` only for offline / dev work.

## Status

### Works
- Server STT on CUDA Whisper large-v3 (~300 ms for a 3 s utterance)
- Server-side trigger filter ("computer") with on-device fallback
- Ollama tool-calling via catalog-picked model
- Built-in tools: time, date, weather, timer, notes, wikipedia,
  wake-on-LAN, web search, save-code
- Obsidian vault MCP integration (list recent, semantic search, read)
- Dictation log: every transcript persists to a daily Markdown file
- Python clients on Windows + Linux in stream mode
- Android client in `ENGINE_SERVER` mode
- Mic mutes during TTS so the assistant doesn't transcribe itself

### Known sore spots
- Local LLM tool-calling is flakier than Claude was. If the picked
  catalog model produces invalid tool JSON, override with
  `JARVIS_OLLAMA_MODEL` to a hand-picked one.
- pyttsx3 on Linux uses `espeak` — sounds robotic. Drop in Piper later.
- On-device Android whisper is ~4× slower than realtime on weak chips;
  `ENGINE_SERVER` is recommended.

### Not yet
- Barge-in (interrupt TTS with a new utterance)
- Spawning the Claude Code CLI as a subprocess for autonomous coding
- Cross-device timer notifications (single-active-session limit)
- iOS client
- Account-bound tools (calendar, email, smart home)

## Diagnostics

- Server log line `model pick: <tag> — top orchestrator score=…, vram=…, ctx=…` confirms catalog selection on startup.
- Server log line `no trigger in transcript; ignoring: <text>` confirms STT works but the wake phrase wasn't matched.
- Daily transcript file: `Dictation/YYYY-MM-DD.md` in the Obsidian vault.
- Android: `adb logcat -s JarvisService AudioCapture VadSegmenter WhisperStt ServerStreamingStt`.

## See also

- `braindump_ao.md` — long-form project state and historical debugging archaeology
- `android/README.md` — Android build gotchas
