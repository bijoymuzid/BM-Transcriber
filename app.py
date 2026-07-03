import base64
import os
import sys
import tempfile
import time
import mimetypes
from io import BytesIO
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session

load_dotenv()

app = Flask(__name__)

# --- Authentication System Configuration ---
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or os.urandom(64).hex()
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "False").lower() in ("true", "1", "yes")
app.config["PERMANENT_SESSION_LIFETIME"] = 86400  # 24 hours

# --- Mail (SMTP) Configuration ---
app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", "587"))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "true").lower() in ("true", "1", "yes")
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME", "")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", "")

# ---------------------------------------------------------------------------
# Model Configuration
# ---------------------------------------------------------------------------
# Gemini 3.1 Flash — Handles transcription with Bengali language
# understanding, speaker identification, and custom prompting.
# Word-level timestamps are estimated mathematically from audio duration.

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_MODEL = "google/gemini-3.1-flash-lite"

MAX_CHUNK_BYTES = 10 * 1024 * 1024  # 10 MB — strict per-chunk limit

TRANSCRIPTION_PROMPT = (
    "You are a precise Bengali speech-to-text system. Transcribe the following "
    "Bengali audio to text exactly as spoken.\n\n"
    "Rules:\n"
    "- Output ONLY the transcription text — no preamble, no commentary, no markdown.\n"
    '- If there are multiple speakers, identify each distinct speaker and label them '
    'as "Speaker 1:", "Speaker 2:", etc. at the start of each segment.\n'
    "- When a single speaker is speaking continuously, only label at the first line. "
    "Label each line when speakers alternate.\n"
    "- If the audio contains mixed Bengali and English, preserve the original "
    "language of each segment.\n"
    "- Use standard Bengali script (বাংলা লিপি) for Bengali portions.\n"
    "- Preserve proper nouns, numbers, and technical terms as spoken.\n"
    "- If the audio is unintelligible or silent, return exactly: [unintelligible]"
)


# ---------------------------------------------------------------------------
# In-memory session stores (no database, no filesystem persistence)
# ---------------------------------------------------------------------------

SESSION_STORE: dict[str, dict] = {}       # sync_session_id -> {words, text, duration, edited_text}
AUDIO_STORE: dict[str, dict] = {}         # sync_session_id -> {audio_bytes, audio_format, mime_type, timestamp}

SESSION_TTL = 7200       # 2 hours (soft TTL)
SESSION_HARD_TTL = 86400 # 24 hours (hard cleanup for memory management)
MAX_SESSION_STORE_SIZE = 200  # max entries before forced hard cleanup
_last_session_cleanup = time.time()


def _cleanup_stale_sessions():
    """Remove stale session entries to prevent memory leaks."""
    global _last_session_cleanup
    now = time.time()
    if now - _last_session_cleanup < SESSION_TTL:
        return
    _last_session_cleanup = now

    stale = [sid for sid, data in AUDIO_STORE.items()
             if now - data.get("_timestamp", 0) > SESSION_TTL]

    if len(AUDIO_STORE) > MAX_SESSION_STORE_SIZE:
        hard_stale = [sid for sid, data in AUDIO_STORE.items()
                      if now - data.get("_timestamp", 0) > SESSION_HARD_TTL]
        stale = list(set(stale + hard_stale))

    for sid in stale:
        AUDIO_STORE.pop(sid, None)
        SESSION_STORE.pop(sid, None)
    if stale:
        print(f"[cleanup] Removed {len(stale)} stale session(s)")


# ---------------------------------------------------------------------------
# Audio format detection
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma", ".m4a", ".opus", ".webm"}


def detect_audio_format(filename: str) -> str:
    """Extract audio format from filename extension. Defaults to 'mp3'."""
    ext = os.path.splitext(filename)[1].lower()
    fmt = ext.lstrip(".")
    if fmt == "m4a":
        return "mp4"
    if fmt in ("opus", "webm"):
        return "ogg"
    return fmt if fmt in ("mp3", "wav", "flac", "aac", "ogg") else "mp3"


# ---------------------------------------------------------------------------
# STAGE 1: Gemini 3.1 Flash — Transcription (via Chat Completions API)
# ---------------------------------------------------------------------------
# Sends audio as base64 data URL inside a chat messages array.
# Gemini handles Bengali transcription, speaker labeling, and custom rules.

def build_gemini_payload(model_slug: str, b64_audio: str, audio_format: str = "mp3") -> dict:
    """Build the Chat Completions payload for Gemini with base64 audio."""
    return {
        "model": model_slug,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": TRANSCRIPTION_PROMPT},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64_audio, "format": audio_format},
                    },
                ],
            }
        ],
    }


def call_gemini(api_key: str, payload: dict, segment_label: str = "") -> str:
    """POST to OpenRouter Chat Completions (Gemini), return transcription text."""
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=300,
    )
    if resp.status_code != 200:
        detail = resp.text[:500]
        raise RuntimeError(
            f"Transcription service error{segment_label}: HTTP {resp.status_code} – {detail}"
        )
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Empty response from transcription service for segment{segment_label}"
        )


def transcribe_with_gemini(audio_bytes: bytes, audio_format: str, api_key: str) -> str:
    """Transcribe audio with Gemini 3.1 Flash."""
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    payload = build_gemini_payload(GEMINI_MODEL, b64, audio_format)
    return call_gemini(api_key, payload)


# ---------------------------------------------------------------------------
# SYNC PIPELINE: Gemini Transcription + Estimated Word Timestamps
# ---------------------------------------------------------------------------
# For the sync player:
#   1. Gemini 3.1 Flash → transcription text (with Bengali prompt + speaker ID)
#   2. timestamp_estimator → word timestamps from audio duration, with silence
#      detection (ffmpeg silencedetect) so words don't drift into silent pauses
#   3. Word Aligner → maps Gemini words to estimated timestamps
#
# For large files, processing is split into ≤10 MB chunks with ffmpeg.


def transcribe_sync_inline(
    audio_bytes: bytes,
    audio_format: str,
    api_key: str,
    audio_duration: float = 0.0,
) -> tuple[str, list[dict]]:
    """
    Sync pipeline for ≤10 MB files.
    1. Gemini → transcription text
    2. timestamp_estimator → estimated word timestamps (with silence detection)
    3. Word Aligner → map Gemini words to timestamps

    Parameters
    ----------
    audio_bytes : bytes
        Raw audio file bytes.
    audio_format : str
        Audio format extension.
    api_key : str
        OpenRouter API key.
    audio_duration : float
        Total audio duration in seconds (for timestamp estimation).

    Returns (text, aligned_words)
    """
    from services.word_aligner import align_edited_words
    from services.timestamp_estimator import estimate_word_timestamps_from_bytes

    # Stage 1: Gemini gets the full transcription text
    text = transcribe_with_gemini(audio_bytes, audio_format, api_key)
    text = text.strip()

    if not text:
        return "", []

    # Stage 2: Estimate word timestamps with silence detection (uses audio bytes)
    estimated_words = estimate_word_timestamps_from_bytes(
        text, audio_duration, audio_bytes, audio_format,
    )
    if not estimated_words:
        return text, []

    # Stage 3: Align Gemini's text words with estimated timestamps
    aligned = align_edited_words(estimated_words, text)

    if aligned:
        return text, aligned
    else:
        return text, estimated_words


def transcribe_sync_chunked(
    audio_bytes: bytes,
    audio_format: str,
    api_key: str,
    audio_duration: float = 0.0,
) -> tuple[str, list[dict]]:
    """
    Sync pipeline for files >10 MB.
    Splits into ≤10 MB chunks, processes each with Gemini, then estimates
    timestamps per chunk with correct offset adjustment.

    Uses estimate_word_timestamps_from_bytes for each chunk so silence
    detection still works per-chunk.

    Parameters
    ----------
    audio_bytes : bytes
        Raw audio file bytes.
    audio_format : str
        Audio format extension.
    api_key : str
        OpenRouter API key.
    audio_duration : float
        Total audio duration in seconds.

    Returns (text, aligned_words)
    """
    import subprocess

    tmp_path: str | None = None
    ext = f".{audio_format}"

    ffmpeg_cmd = _find_tool("ffmpeg")
    if not ffmpeg_cmd:
        raise RuntimeError(
            "ffmpeg is required for files over 10 MB. "
            "Install ffmpeg and try again.",
        )

    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        total_sec = get_audio_duration(tmp_path)
    except Exception:
        _cleanup(tmp_path)
        raise RuntimeError("Could not process audio file. The file may be corrupted.")

    if audio_duration <= 0:
        audio_duration = total_sec

    file_size = len(audio_bytes)
    if total_sec > 0 and file_size > 0:
        chunk_sec = (MAX_CHUNK_BYTES / file_size) * total_sec
    else:
        chunk_sec = 60.0
    chunk_sec = max(chunk_sec, 30.0)

    from services.word_aligner import align_edited_words
    from services.timestamp_estimator import estimate_word_timestamps_from_bytes

    all_words: list[dict] = []
    full_text_parts: list[str] = []
    global_index = 0
    pos = 0.0

    try:
        while pos < total_sec:
            end = min(pos + chunk_sec, total_sec)
            chunk_duration = end - pos

            # Extract chunk with ffmpeg
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as chunk_tmp:
                chunk_path = chunk_tmp.name

            subprocess.run(
                [ffmpeg_cmd, "-y", "-v", "quiet",
                 "-ss", str(pos), "-to", str(end),
                 "-i", tmp_path, "-c", "copy", chunk_path],
                capture_output=True, timeout=120, check=True,
            )

            with open(chunk_path, "rb") as f:
                chunk_bytes = f.read()
            os.unlink(chunk_path)

            # Stage 1: Gemini transcribes this chunk
            chunk_text = transcribe_with_gemini(chunk_bytes, audio_format, api_key)
            chunk_text = chunk_text.strip()

            if not chunk_text:
                pos = end
                continue

            # Stage 2: Estimate timestamps for this chunk (uses chunk bytes for silence detection)
            estimated = estimate_word_timestamps_from_bytes(
                chunk_text, chunk_duration, chunk_bytes, audio_format,
            )

            # Adjust timestamps by chunk offset
            for w in estimated:
                w["start"] = round(w["start"] + pos, 3)
                w["end"] = round(w["end"] + pos, 3)
                w["index"] = global_index
                global_index += 1

            all_words.extend(estimated)
            full_text_parts.append(chunk_text)

            pos = end

    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError("Could not process audio file. The file may be corrupted.") from e
    finally:
        _cleanup(tmp_path)

    full_text = " ".join(full_text_parts)
    return full_text, all_words


def get_audio_duration(file_path: str) -> float:
    """Return duration in seconds via ffprobe (with ffmpeg fallback)."""
    import json
    import re
    import subprocess

    ffprobe_cmd = _find_tool("ffprobe")
    if ffprobe_cmd:
        try:
            result = subprocess.run(
                [ffprobe_cmd, "-v", "quiet",
                 "-print_format", "json",
                 "-show_entries", "format=duration",
                 file_path],
                capture_output=True, text=True, timeout=30,
            )
            result.check_returncode()
            data = json.loads(result.stdout)
            return float(data["format"]["duration"])
        except Exception:
            pass

    ffmpeg_cmd = _find_tool("ffmpeg")
    if ffmpeg_cmd:
        try:
            result = subprocess.run(
                [ffmpeg_cmd, "-i", file_path],
                capture_output=True, text=True, timeout=30,
            )
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
            if match:
                h, m, s = match.groups()
                return float(h) * 3600 + float(m) * 60 + float(s)
        except Exception:
            pass

    raise RuntimeError("Could not determine audio duration.")


def get_audio_duration_from_bytes(audio_bytes: bytes, audio_format: str) -> float:
    """Get audio duration from raw bytes via ephemeral temp file."""
    ext = f".{audio_format}"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(audio_bytes)
            tmp = f.name
        return get_audio_duration(tmp)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _find_tool(name: str) -> str | None:
    """Return the full path of a tool (ffmpeg/ffprobe) or None."""
    import shutil
    path = shutil.which(name)
    if path:
        return name
    exe_name = f"{name}.exe"
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), exe_name)
    if os.path.isfile(local_path):
        return local_path
    common_locations = [
        os.path.join(r"C:\ffmpeg\bin", exe_name),
        os.path.join(os.path.expanduser(r"~\ffmpeg\bin"), exe_name),
    ]
    for cp in common_locations:
        if os.path.isfile(cp):
            return cp
    return None


def _cleanup(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    """
    Basic transcription — uses Gemini 3.1 Flash only.
    Returns plain text with full Bengali language support.
    """
    # --- API key resolution ---
    user_api_key = request.headers.get("X-API-Key", "").strip()
    if user_api_key:
        api_key = user_api_key
    else:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            return jsonify({"error": "Server API key not configured"}), 500

    # --- File validation ---
    if "file" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No audio file selected"}), 400

    audio_format = detect_audio_format(file.filename)
    audio_bytes = file.read()

    # --- Transcribe with Gemini (≤10 MB inline, >10 MB chunked) ---
    try:
        if len(audio_bytes) <= MAX_CHUNK_BYTES:
            text = transcribe_with_gemini(audio_bytes, audio_format, api_key)
        else:
            # For basic transcription of large files, use Gemini chunked
            import re
            text_parts = []
            tmp_path = None
            ffmpeg_cmd = _find_tool("ffmpeg")
            if not ffmpeg_cmd:
                raise RuntimeError("ffmpeg is required for files over 10 MB.")

            with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            total_sec = get_audio_duration(tmp_path)

            file_size = len(audio_bytes)
            chunk_sec = max((MAX_CHUNK_BYTES / file_size) * total_sec, 30.0) if total_sec > 0 else 60.0
            pos = 0.0
            while pos < total_sec:
                end = min(pos + chunk_sec, total_sec)
                with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as ct:
                    chunk_path = ct.name
                subprocess.run(
                    [ffmpeg_cmd, "-y", "-v", "quiet", "-ss", str(pos), "-to", str(end),
                     "-i", tmp_path, "-c", "copy", chunk_path],
                    capture_output=True, timeout=120, check=True,
                )
                with open(chunk_path, "rb") as f:
                    chunk_bytes = f.read()
                os.unlink(chunk_path)
                chunk_text = transcribe_with_gemini(chunk_bytes, audio_format, api_key)
                text_parts.append(chunk_text.strip())
                pos = end
            _cleanup(tmp_path)
            text = " ".join(text_parts)
    except RuntimeError as e:
        status = 502
        msg = str(e)
        if "Install ffmpeg" in msg or "Could not process" in msg:
            status = 400
        return jsonify({"error": msg}), status

    return jsonify({"transcription": text})


# ---------------------------------------------------------------------------
# Sync Player — SESSION-ONLY endpoints
# ---------------------------------------------------------------------------

MIME_MAP = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "wma": "audio/x-ms-wma",
}


@app.route("/transcribe-with-words", methods=["POST"])
def transcribe_with_words():
    """
    Sync pipeline for player with word-level timestamps:
      1. Gemini 3.1 Flash → transcription text (Bengali-optimized)
      2. timestamp_estimator → word timestamps from audio duration
      3. Word Aligner (Needleman-Wunsch) → map text words to timestamps

    Uses only Gemini 3.1 Flash for transcription. Word timestamps are
    estimated uniformly from the audio duration since Gemini doesn't
    provide per-word timing data.
    """
    _cleanup_stale_sessions()

    # --- API key resolution ---
    user_api_key = request.headers.get("X-API-Key", "").strip()
    if user_api_key:
        api_key = user_api_key
    else:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            return jsonify({"error": "Server API key not configured"}), 500

    # --- File validation ---
    if "file" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No audio file selected"}), 400

    audio_format = detect_audio_format(file.filename)
    audio_bytes = file.read()
    mime_type = MIME_MAP.get(audio_format, "application/octet-stream")

    # --- Get duration ---
    try:
        duration = get_audio_duration_from_bytes(audio_bytes, audio_format)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    # --- Pipeline: Gemini for text + timestamp estimation for word timings ---
    try:
        if len(audio_bytes) <= MAX_CHUNK_BYTES:
            text, words = transcribe_sync_inline(audio_bytes, audio_format, api_key, duration)
        else:
            text, words = transcribe_sync_chunked(audio_bytes, audio_format, api_key, duration)
    except RuntimeError as e:
        status = 502
        msg = str(e)
        if "Install ffmpeg" in msg or "Could not process" in msg:
            status = 400
        return jsonify({"error": msg}), status

    # --- Store in SESSION (in-memory) ---
    sync_session_id = uuid.uuid4().hex

    SESSION_STORE[sync_session_id] = {
        "words": words,
        "text": text,
        "duration": duration,
        "edited_text": None,
    }
    AUDIO_STORE[sync_session_id] = {
        "audio_bytes": audio_bytes,
        "audio_format": audio_format,
        "mime_type": mime_type,
        "_timestamp": time.time(),
    }

    session["sync_session_id"] = sync_session_id

    return jsonify({
        "id": sync_session_id,
        "words": words,
        "text": text,
        "duration": duration,
        "audio_url": f"/session-audio/{sync_session_id}",
    })


@app.route("/player/<sync_session_id>")
def player_page(sync_session_id):
    """Render the synchronised audio-transcript player page."""
    data = SESSION_STORE.get(sync_session_id)
    if data is None:
        return render_template("player.html", error="Session expired or transcription not found. Please upload again.")
    return render_template(
        "player.html",
        sync_session_id=sync_session_id,
        audio_url=f"/session-audio/{sync_session_id}",
        duration=data["duration"],
    )


@app.route("/session-audio/<sync_session_id>")
def serve_session_audio(sync_session_id):
    """Serve audio bytes directly from in-memory AUDIO_STORE."""
    entry = AUDIO_STORE.get(sync_session_id)
    if entry is None:
        return jsonify({"error": "Audio not found (session may have expired)"}), 404
    return (
        entry["audio_bytes"],
        200,
        {"Content-Type": entry["mime_type"]},
    )


@app.route("/api/session-transcriptions/<sync_session_id>", methods=["GET"])
def get_session_transcription(sync_session_id):
    """Return transcription word data from session store as JSON."""
    data = SESSION_STORE.get(sync_session_id)
    if data is None:
        return jsonify({
            "error": "Session not found or expired",
            "recoverable": True,
        }), 404

    display_text = data["edited_text"] if data["edited_text"] else data["text"]

    return jsonify({
        "id": sync_session_id,
        "words": data["words"],
        "text": display_text,
        "duration": data["duration"],
        "audio_url": f"/session-audio/{sync_session_id}",
    })


@app.route("/api/session-transcriptions/<sync_session_id>", methods=["PUT"])
def update_session_transcription(sync_session_id):
    """Save edited transcript and realign word timestamps."""
    from services.word_aligner import align_edited_words

    data = SESSION_STORE.get(sync_session_id)
    if data is None:
        return jsonify({"error": "Session not found or expired"}), 404

    body = request.get_json(silent=True)
    if not body or "edited_text" not in body:
        return jsonify({"error": "Missing edited_text in request body"}), 400

    edited_text = body["edited_text"].strip()
    if not edited_text:
        return jsonify({"error": "Edited text cannot be empty"}), 400

    original_words = data["words"]
    realigned = align_edited_words(original_words, edited_text)

    if not realigned:
        realigned = original_words

    data["edited_text"] = edited_text
    data["words"] = realigned
    data["_updated_at"] = time.time()

    return jsonify({
        "id": sync_session_id,
        "words": realigned,
        "text": edited_text,
        "success": True,
    })


# ---------------------------------------------------------------------------
# Authentication system setup
# ---------------------------------------------------------------------------

from extensions import mail
from auth_models import db
from auth_routes import auth_bp, bcrypt, login_manager, limiter, csrf
import re
import subprocess  # needed for chunked transcription in /transcribe route

db.init_app(app)
bcrypt.init_app(app)
login_manager.init_app(app)
limiter.init_app(app)
csrf.init_app(app)
mail.init_app(app)
csrf.exempt(transcribe)
csrf.exempt(transcribe_with_words)
csrf.exempt(update_session_transcription)

app.register_blueprint(auth_bp)


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _minify_html_content(html: str) -> str:
    """Minify HTML preserving <script> and <pre> blocks."""
    placeholders = {}

    def _preserve(m):
        idx = len(placeholders)
        placeholder = f"__PRESERVED_{idx}__"
        placeholders[placeholder] = m.group(0)
        return placeholder

    html = re.sub(
        r"<script\b[^>]*>.*?</script>",
        _preserve, html, flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(
        r"<pre\b[^>]*>.*?</pre>",
        _preserve, html, flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(r"\s+", " ", html)
    html = re.sub(r">\s+<", "> <", html)
    html = html.strip()
    for placeholder, original in placeholders.items():
        html = html.replace(placeholder, original)
    return html


@app.after_request
def minify_html(response):
    if response.content_type and "text/html" in response.content_type:
        try:
            response.set_data(_minify_html_content(response.get_data(as_text=True)))
        except Exception:
            pass
    return response


@app.before_request
def create_tables():
    from flask import g
    if not hasattr(g, "_tables_created"):
        db.create_all()
        g._tables_created = True


if __name__ == "__main__":
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        print(
            "WARNING: OPENROUTER_API_KEY not set in .env. "
            "Supply it via the web UI instead.",
            file=sys.stderr,
        )
    debug_mode = os.getenv("FLASK_DEBUG", "0").lower() in ("true", "1", "yes")
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    print(f"Starting server on {host}:{port} (debug={debug_mode})")
    app.run(debug=debug_mode, host=host, port=port)
