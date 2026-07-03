# BM Transcriber — Complete Application Overview

**Creator:** BIJOY MOZID  
**Contact:** 01303548685 / niabijoy123@gmail.com

---

## 1. Project Overview

BM Transcriber is a **Bengali-focused audio transcription web application** that uses a **two-stage AI pipeline**:

1. **Stage 1 — Gemini 3.1 Flash**: Transcribes audio with full Bengali language support, speaker identification, and custom prompting
2. **Stage 2 — Whisper-1**: Provides precise word-level timestamps via the audio transcriptions API

The app supports two modes:
- **Basic transcription** — plain text output (Gemini only)
- **Sync transcription** — Gemini text + Whisper word timestamps, aligned with a Needleman-Wunsch word aligner, displayed in a real-time audio player

The app also includes a full authentication system with IP-based account locking, role-based admin panel, a contact form, and an in-memory session-based sync player.

---

## 2. Technology Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.10+ / Flask 3.0 |
| Database | SQLite via SQLAlchemy 3.0 (stored in `instance/app.db`) |
| Auth | Flask-Login 0.6, Flask-Bcrypt 1.0, Flask-WTF CSRF |
| Rate Limit | Flask-Limiter 3.0 |
| Email | Flask-Mail 0.9 (SMTP via Gmail App Password) |
| Audio Processing | ffmpeg/ffprobe (chunking large files, duration detection) |
| Transcription AI | **Gemini 3.1 Flash** (via OpenRouter Chat Completions) |
| Timestamp AI | **Whisper-1** (via OpenRouter Audio Transcriptions, `verbose_json` + `timestamp_granularities[]=word`) |
| Word Alignment | Needleman-Wunsch sequence alignment (`services/word_aligner.py`) |
| Frontend | Jinja2 Templates + Vanilla JS + Premium CSS (glassmorphism) |
| Config | `.env` file via python-dotenv |

---

## 3. File & Directory Structure

```
Local LLM/
├── app.py                        # Main Flask application (all routes + pipeline)
├── auth_models.py                # SQLAlchemy models
├── auth_routes.py                # Blueprint: login, register, admin routes
├── auth_utils.py                 # Decorators + log_action + notifications
├── extensions.py                 # Flask-Mail instance
├── requirements.txt              # Python dependencies
├── .env                          # Environment variables
├── .env.example                  # Template for .env
├── run.ps1                       # PowerShell launcher
├── start.bat                     # Windows CMD launcher
├── ffmpeg.exe                    # Bundled ffmpeg binary
│
├── services/
│   ├── __init__.py               # Package marker
│   └── word_aligner.py           # Needleman-Wunsch alignment for edited text
│
├── static/
│   ├── css/premium.css           # Design system (glassmorphism, dark)
│   └── js/sync-player.js         # Client-side sync player
│
├── templates/
│   ├── base.html                 # Base layout
│   ├── index.html                # Home page
│   ├── player.html               # Sync player page
│   ├── login.html / register.html / profile.html / contact.html
│   └── admin/
│       ├── dashboard.html        # Stats + config status
│       ├── users.html            # User management
│       ├── logs.html             # Login activity
│       ├── unlock_requests.html  # Unlock management
│       └── contact_messages.html # Contact submissions
│
├── instance/app.db               # SQLite database
└── plans/                        # Planning documents
```

---

## 4. Two-Stage Pipeline — How It Works

### 4.1 Architecture Diagram

```
                    AUDIO FILE
                        │
                        ▼
              ┌─────────────────┐
              │  ≤ 10 MB?       │
              └────────┬────────┘
                       │
            ┌──────────┴──────────┐
            │                     │
            ▼                     ▼
    ┌──────────────┐    ┌──────────────────┐
    │ transcribe_  │    │ transcribe_sync_ │
    │ sync_inline  │    │   chunked        │
    └──────┬───────┘    └────────┬─────────┘
           │                     │
           │          ffmpeg split into
           │          ≤10 MB chunks
           │                     │
           └──────────┬──────────┘
                      │
         ┌────────────┴────────────┐
         │                         │
         ▼                         ▼
  ┌──────────────┐        ┌──────────────┐
  │  GEMINI 3.1  │        │   WHISPER-1  │
  │    Flash     │        │              │
  │              │        │              │
  │ Chat Complet.│        │ Audio Transc.│
  │ JSON+base64  │        │ multipart/   │
  │ audio upload │        │ form-data    │
  │              │        │ file upload  │
  │ RETURNS:     │        │              │
  │ plain text   │        │ RETURNS:     │
  │ (Bengali,    │        │ [{word,      │
  │  speaker ID) │        │   start,     │
  └──────┬───────┘        │   end}, ...] │
         │                └──────┬───────┘
         │                       │
         └──────────┬────────────┘
                    │
                    ▼
         ┌──────────────────┐
         │  WORD ALIGNER    │
         │  Needleman-      │
         │  Wunsch          │
         │                  │
         │ Maps Gemini text │
         │ to Whisper's     │
         │ precise times    │
         └────────┬─────────┘
                  │
                  ▼
         ┌──────────────────┐
         │  {text, words[], │
         │   duration}      │
         └──────────────────┘
```

### 4.2 Home Page Flow

1. User opens `http://127.0.0.1:5000`
2. Page checks `sessionStorage` for previous session ("Resume" banner)
3. User drags & drops audio file (or browses)
4. Two buttons:
   - **"Transcribe"** → uses Gemini 3.1 Flash only → returns plain text
   - **"Transcribe & Sync"** → uses BOTH Gemini + Whisper + Aligner → returns word timestamps

### 4.3 Basic Transcription (`/transcribe`)

```
POST /transcribe
├── Gemini 3.1 Flash
│   ├── Audio → base64 → Chat Completions API
│   ├── Bengali prompt with speaker identification
│   └── Returns: {"transcription": "..."}
└── No Whisper call (faster, cheaper)
```

### 4.4 Sync Transcription (`/transcribe-with-words`)

```
POST /transcribe-with-words
│
├── Stage 1: Gemini 3.1 Flash
│   ├── Audio → base64 → Chat Completions API
│   ├── Bengali prompt → transcription text
│   └── Returns: "speaker 1: ... speaker 2: ..."
│
├── Stage 2: Whisper-1
│   ├── Audio → multipart upload → Audio Transcriptions API
│   ├── response_format=verbose_json
│   ├── timestamp_granularities[]=word
│   └── Returns: [{word, start, end}, ...]
│
├── Stage 3: Word Aligner (Needleman-Wunsch)
│   ├── Takes Gemini's text words as "query"
│   ├── Takes Whisper's timestamped words as "reference"
│   ├── Matches similar words (fuzzy, case-insensitive)
│   ├── Preserves Whisper's precise timestamps for matched words
│   └── Interpolates timestamps for unmatched words
│
└── Returns: {id, words[], text, duration, audio_url}
```

### 4.5 Sync Player Page (`/player/<id>`)

Powered by [`static/js/sync-player.js`](Local LLM/static/js/sync-player.js):
- Real-time word highlighting via `requestAnimationFrame` (~60fps)
- Click-to-seek with seek-guard against race conditions
- Play/pause, seek bar, time display, speed control (0.5x–2x)
- Editable transcript → server saves via Needleman-Wunsch alignment
- Reset to original, copy transcript text
- Keyboard: Space (play/pause), ← (rewind 5s), → (forward 5s)

### 4.6 Chunking (files > 10 MB)

ffmpeg splits audio into **≤10 MB chunks**. Each chunk goes through the full two-stage pipeline. Word timestamps from each chunk are adjusted by the chunk's start offset, producing globally correct timestamps.

---

## 5. API Details

### 5.1 Gemini 3.1 Flash (Chat Completions)

| Detail | Value |
|---|---|
| Endpoint | `https://api.openrouter.ai/api/v1/chat/completions` |
| Model | `google/gemini-3.1-flash-lite` |
| Auth | Bearer token in `Authorization` header |
| Request | JSON with base64 audio in `input_audio` field |
| Response | `choices[0].message.content` (plain text) |
| Timeout | 300 seconds |

### 5.2 Whisper-1 (Audio Transcriptions)

| Detail | Value |
|---|---|
| Endpoint | `https://api.openrouter.ai/api/v1/audio/transcriptions` |
| Model | `openai/whisper-1` |
| Auth | Bearer token in `Authorization` header |
| Request | Multipart/form-data with binary audio file |
| Response | `verbose_json` with `words[]` array |
| Word Timestamps | `{word, start, end}` — precise from API |

### 5.3 Key Functions in [`app.py`](Local LLM/app.py)

| Function | Purpose |
|---|---|
| `transcribe_with_gemini()` | Stage 1: Gemini transcription (base64 in chat) |
| `transcribe_whisper()` | Stage 2: Whisper timestamp upload (multipart) |
| `get_whisper_word_timestamps()` | Stage 2 wrapper: returns `[{word, start, end, index}]` |
| `transcribe_sync_inline()` | Full pipeline for ≤10 MB |
| `transcribe_sync_chunked()` | Full pipeline for >10 MB with ffmpeg splitting |
| `call_gemini()` | Low-level HTTP POST to Chat Completions |
| `build_gemini_payload()` | Builds the JSON payload with base64 audio |

---

## 6. Authentication System

### Registration (`/register`)
- Rate-limited: 3/hour, no email verification
- Password ≥ 8 characters
- Email `niabijoy123@gmail.com` → admin role

### Login with IP Locking (`/login`)
- Rate-limited: 5/minute
- First login: stores IP address
- Different IP → auto-lock account
- Admin must manually unlock

### Unlock Request Flow
1. Locked user → `/request-unlock` → submits email + message
2. `UnlockRequest` created (status: "pending")
3. Admin notified via console
4. Admin approves/denies at `/admin/unlock-requests`

### Admin Panel
| Route | Description |
|---|---|
| `/admin/dashboard` | Stats + config status (Gemini + Whisper) |
| `/admin/users` | User table (unlock, API key management) |
| `/admin/unlock-requests` | Pending + history |
| `/admin/logs` | Paginated login logs |
| `/admin/contact-messages` | Contact form submissions |

---

## 7. In-Memory Session System

```python
SESSION_STORE = {}  # sync_session_id → {words, text, duration, edited_text}
AUDIO_STORE = {}    # sync_session_id → {audio_bytes, audio_format, mime_type}
```

- Flask session cookie stores only `sync_session_id`
- Audio served from RAM via `/session-audio/<id>`
- Cleanup: 2h soft TTL, 24h hard TTL (>200 entries)

---

## 8. Word Aligner (`services/word_aligner.py`)

The Needleman-Wunsch algorithm:
- **Match**: +2 | **Mismatch**: -1 | **Gap penalty**: -1
- Case-insensitive, punctuation-insensitive fuzzy matching
- Matched words → keep Whisper's precise timestamps
- Inserted words → interpolate from nearest neighbors
- Deleted words → removed from final array

**Note:** The legacy `services/timestamp_estimator.py` has been **deleted** — Whisper-1 provides all timing data natively.

---

## 9. Security Features

| Feature | Implementation |
|---|---|
| Rate Limiting | Flask-Limiter: 200/day, 5/min login, 3/hr register/contact |
| CSRF | Flask-WTF on all forms, API exempted |
| Cookies | HTTP-only, SameSite=Lax |
| Headers | X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, HSTS |
| IP Locking | Auto-lock on unrecognized IP |
| Password Hashing | bcrypt via Flask-Bcrypt |
| Admin Protection | `@admin_required` decorator (403 for non-admins) |

---

## 10. Configuration (`.env`)

| Variable | Current Value |
|---|---|
| `OPENROUTER_API_KEY` | sk-or-v1-eaab... |
| `SECRET_KEY` | bb9f5d3c... |
| `DATABASE_URL` | sqlite:///app.db |
| `MAIL_USERNAME` | niabijoy123@gmail.com |
| `MAIL_PASSWORD` | pllm eyuj ckam okzr |
| `FLASK_DEBUG` | true |
| `FLASK_PORT` | 5000 |

---

## 11. Database Models

- **User**: email, password_hash, is_admin, is_locked, ip_address, api_key
- **UnlockRequest**: user_id, status (pending/approved/denied), message, admin_response
- **LoginLog**: user_id, action, ip_address, details, timestamp
- **ContactMessage**: name, email, subject, message, is_read
- **Transcription**: user_id, audio_filename, audio_duration, words_json, original_text, edited_text

---

## 12. Design System (`premium.css`)

Dark glassmorphism theme:
- Background: `#080818`, animated radial gradients
- Cards: backdrop-filter blur, semi-transparent backgrounds
- Typography: Playfair Display + Inter + JetBrains Mono
- Accent: Violet `#8b5cf6`, Indigo `#6366f1`, Emerald `#10b981`
- Buttons: Gradient with shimmer hover effect

---

## 13. Running the App

```powershell
# PowerShell
.\run.ps1

# CMD
start.bat

# Manual
pip install -r requirements.txt
python app.py
```

Server: `http://127.0.0.1:5000`

---

## 14. Key Dependencies

| Package | Purpose |
|---|---|
| `flask>=3.0` | Web framework |
| `requests>=2.31` | HTTP client for both Gemini + Whisper APIs |
| `flask-sqlalchemy>=3.0` | ORM |
| `flask-login>=0.6` | Auth |
| `flask-bcrypt>=1.0` | Password hashing |
| `flask-limiter>=3.0` | Rate limiting |
| `flask-wtf>=1.0` | CSRF |
| `flask-mail>=0.9.1` | Email |
| `ffmpeg` | Audio chunking + duration |

---

*Document generated on 2026-07-02*
