# audiobook-server

Self-hosted backend for the E-Reader / Transcriber Flutter app — **one
FastAPI process, two halves**:

| Half | Endpoints | Purpose |
|---|---|---|
| Metadata | `/api/auth/*`, `/api/progress/*` | User accounts (JWT) + per-book/per-chapter reading & listening progress |
| Transcription | `/api/metadata/*`, `/api/transcribe`, `/api/jobs/*`, `/api/vtt/*` | Audiobookshelf proxy, Groq Whisper transcription jobs, VTT output |

Plus shared: `GET /api/health`, `GET /api/check/groq`.

Introspect the full API at `http://<host>:8001/docs` (Swagger UI, served
by FastAPI automatically).

## Setup

### 1. Prerequisites

- Python 3.10+
- `ffmpeg` (and `ffprobe`, which ships alongside it) on `PATH`, or set
  `FFMPEG_PATH`/`FFPROBE_PATH` explicitly
- A Groq API key (free tier is fine) for transcription
- Your Audiobookshelf server's base URL + an API token

**On Termux:**

```bash
pkg update && pkg install python ffmpeg git
```

No other native packages are needed — see "Why no compiler is required"
below for why every Python dependency installs from a pure-Python wheel.

### 2. Install

```bash
git clone https://github.com/secureman/audiobook-server.git
cd audiobook-server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

### 3. Configure `.env`

Open `.env` and set, at minimum:

| Variable | Why |
|---|---|
| `JWT_SECRET` | **Required to boot at all** — `assert_jwt_secret_configured()` refuses to start otherwise. This gates startup even if you never call `/api/auth/*` (see "Why JWT?" below) — generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `GROQ_API_KEY` | Transcription won't run without it |
| `ABS_BASE_URL` / `ABS_API_TOKEN` | Needed to fetch book/chapter metadata and audio from Audiobookshelf |

Everything else (`OUTPUT_DIR`, `METADATA_DB_PATH`, `TRANSCRIPTION_DB_PATH`,
`LOG_DIR`, `TEMP_DIR`) has a working default — `OUTPUT_DIR` and
`METADATA_DB_PATH` default next to this repo, the rest under
`~/.local/share/audiobook-transcriber` (Termux's own private app home,
**not** the shared `/storage` mount `termux-setup-storage` exposes — no
storage permission needed for the server itself). Override only if you
want these somewhere else.

### 4. Run it

```bash
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

Visit `http://<host>:8001/docs` for interactive Swagger UI, or
`http://<host>:8001/api/health` for a plain liveness check.

### 5. Keep it running on Termux

Termux kills background processes and the phone's battery optimizer
throttles CPU/network once the screen is off — both will silently stall a
foreground `uvicorn` run. Two things fix that:

```bash
# Inside the same Termux session, before or after starting uvicorn:
termux-wake-lock       # stop Android from suspending Termux in the background
```

and run the server itself in a session that survives you closing the
Termux app — either `tmux`/`screen`:

```bash
pkg install tmux
tmux new -s audiobook-server
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8001
# detach: Ctrl-b then d — reattach later with: tmux attach -t audiobook-server
```

or plain `nohup` + `disown` if you don't want tmux:

```bash
nohup .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8001 \
  > server.log 2>&1 &
disown
```

To start it automatically on boot, install the
[Termux:Boot](https://wiki.termux.com/wiki/Termux:Boot) add-on app and
drop a script at `~/.termux/boot/start-audiobook-server` that runs the
`tmux new -d -s ...` (or `nohup`) command above plus `termux-wake-lock`.

### 6. Point the Absorb app at it

In the app's read-along server settings, use `http://127.0.0.1:8001` if
Absorb runs on the same phone as Termux, or `http://<phone's-LAN-IP>:8001`
(`ip addr` / Termux's `ifconfig` shows it) if it's a different device on
the same network. CORS is wide open by design for this (see Security,
below) — no extra config needed on the server side either way.

### Why no compiler is required

Every dependency is pure Python (see the comments in `requirements.txt`):
`pydantic` is pinned to the v1 line (v2 needs the Rust `pydantic-core`),
`uvicorn` is installed without its `[standard]` extras (uvloop/httptools/
watchfiles all need compilation), and JWTs/passwords use `pyjwt` +
stdlib `hashlib.pbkdf2_hmac` instead of `bcrypt`/`argon2-cffi`. `ffmpeg`
itself is the one native binary required, installed via Termux's own
`pkg` (prebuilt, no compiler needed on your end either).

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8001` | One process serves everything |
| `JWT_SECRET` | — | **Required.** Server refuses to boot without it |
| `JWT_EXPIRY_DAYS` | `30` | Login token lifetime |
| `PBKDF2_ITERATIONS` | `200000` | Password hashing |
| `METADATA_DB_PATH` | `./metadata.db` | Users + progress (SQLite) |
| `TRANSCRIPTION_DB_PATH` | `~/.local/share/audiobook-transcriber/transcriptions.db` | Job history; override to keep it repo-local |
| `ABS_BASE_URL` / `ABS_API_TOKEN` | — | Audiobookshelf connection (shared by both halves) |
| `GROQ_API_KEY` | — | Whisper transcription |
| `OUTPUT_DIR` | `./transcriptions` (repo root) | Generated `.vtt` files, one folder per `abs_item_id` |
| `TEMP_DIR` | data dir under `~/.local/share/audiobook-transcriber` | Chunked audio temp |
| `MAX_CONCURRENT_JOBS` | `2` | ffmpeg workers |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | |
| `LOG_DIR` | data dir `logs/` | Rotating `server.log` |

## Data layout

Two independent SQLite databases, one process:

- `metadata.db` — `users`, `book_progress`, `chapter_done`, `chapter_position`
- `transcriptions.db` — transcription job history (`transcriptions/` at the
  repo root holds the generated `.vtt` files, one subfolder per
  `abs_item_id`, e.g. `transcriptions/<abs_item_id>/chapter_0.vtt`)

## Tests

```bash
# end-to-end smoke against a running server (register → login → progress CRUD)
BASE=http://127.0.0.1:8001 bash tests/smoke_metadata.sh
```

## Security

- LAN-service posture: CORS is open (`*`) by design. If you expose this
  beyond your LAN, restrict `allow_origins` in `main.py` and put TLS in
  front (Caddy/nginx).
- `/api/transcribe`, `/api/jobs/*`, and `/api/vtt/*` — the endpoints the
  Absorb client actually calls — have **no auth check at all**. This is
  intentional for a single-household, LAN-only box; see "Why JWT?" below
  for the two auth mechanisms that exist elsewhere in this server but
  aren't wired into that traffic.
- JWTs are HS256-signed with `JWT_SECRET`; passwords are salted
  PBKDF2-HMAC-SHA256 (200k iterations, stdlib only). This protects
  `/api/auth/*` only (see below).

### Why JWT? (it isn't protecting the endpoints you use)

This server actually has *three* auth-adjacent things, and the Absorb
client currently uses none of them for transcription:

1. **JWT + password accounts** (`/api/auth/register`, `/login`,
   `/logout`, `/me`, guarded by `security.get_current_user`) — inherited
   from the former standalone `metadata-server` (see History, below).
   `security.py` says outright: *"The Flutter client no longer creates
   accounts on this server."* Confirmed by grep: nothing in
   `lib/services/readalong_api_client.dart` or `readalong_config_sheet.dart`
   calls any `/auth/*` route. This is fully dead code from the client's
   side today.
2. **`X-API-Key` / ABS-token validation** (`security.get_api_key_user`,
   cached 1h) — the mechanism that replaced JWT, meant to trust whatever
   Audiobookshelf token the client already has instead of separate
   accounts. It guards `routers/progress.py` only. The Absorb client
   never sends an `X-API-Key` header anywhere either, so this reading-
   progress-sync API is currently unreachable too.
3. **Nothing** — `routers/transcribe.py` and `routers/vtt.py`, the routes
   read-along actually calls, use neither dependency. This is the "no
   auth" you're seeing.

`JWT_SECRET` is still a **hard boot requirement**
(`assert_jwt_secret_configured()` in `config.py`) even though nothing
currently exercises it — the metadata half of this merged server still
needs it to come up. If you're certain you'll never want separate
accounts or the progress-sync API, the JWT auth system (and possibly
`routers/progress.py`) is safe to delete outright; otherwise it's inert
and low-cost to leave in place.

## History

This repo is the merge of the former `metadata-server` and
`transcription_server` projects. Route paths are unchanged, so existing
clients only need to point `metadataUrl` and `backendUrl` at the same
host:port. `metadata-server` originally had its own JWT-based user
accounts; that model was later replaced (client-side) by trusting the
caller's Audiobookshelf token directly, which is why JWT still exists
here but nothing outside `/api/auth/*` depends on it.
