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

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# configure .env (see .env.example) — JWT_SECRET is REQUIRED
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

On Termux the same steps work as-is; no compiler or Rust toolchain is
needed (every dependency ships pure-Python wheels — see requirements.txt).

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
| `OUTPUT_DIR` / `TEMP_DIR` | data dir under `~/.local/share/audiobook-transcriber` | VTT cache / chunked audio temp |
| `MAX_CONCURRENT_JOBS` | `2` | ffmpeg workers |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | |
| `LOG_DIR` | data dir `logs/` | Rotating `server.log` |

## Data layout

Two independent SQLite databases, one process:

- `metadata.db` — `users`, `book_progress`, `chapter_done`, `chapter_position`
- `transcriptions.db` — transcription job history (`vtt_cache/` holds the
  generated `.vtt` files keyed by `abs_item_id`)

## Tests

```bash
# end-to-end smoke against a running server (register → login → progress CRUD)
BASE=http://127.0.0.1:8001 bash tests/smoke_metadata.sh
```

## Security

- LAN-service posture: CORS is open (`*`) by design. If you expose this
  beyond your LAN, restrict `allow_origins` in `main.py` and put TLS in
  front (Caddy/nginx).
- JWTs are HS256-signed with `JWT_SECRET`; passwords are salted
  PBKDF2-HMAC-SHA256 (200k iterations, stdlib only).

## History

This repo is the merge of the former `metadata-server` and
`transcription_server` projects. Route paths are unchanged, so existing
clients only need to point `metadataUrl` and `backendUrl` at the same
host:port.
