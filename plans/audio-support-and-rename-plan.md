# Plan: Audio Format Support & Website Rename

## Overview

Two changes requested:
1. **Allow any audio file type** — Remove the current `.mp3`-only restriction
2. **Rename website to "BM Transcriber"** — Replace all "Bengali Audio Transcriber" / "Bengali Transcriber" references with "BM Transcriber"

---

## Task 1: Allow Any Audio File Type

### 1A — Server-side (`app.py`)

| # | File | Line(s) | Current | Change |
|---|------|---------|---------|--------|
| 1 | [`app.py`](app.py:57) | 57-72 | `build_openrouter_payload(model_slug, b64_audio)` hardcodes `"format": "mp3"` | Add `audio_format: str` parameter. Use it instead of `"mp3"`. |
| 2 | [`app.py`](app.py:100) | 100-103 | `transcribe_inline()` calls `build_openrouter_payload(model_slug, b64)` | Pass detected format: `build_openrouter_payload(model_slug, b64, audio_format)` |
| 3 | [`app.py`](app.py:106) | 106 | `get_audio_duration()` — no changes needed | Works with any audio format via ffprobe already |
| 4 | [`app.py`](app.py:125) | 125-208 | `transcribe_chunked()` hardcodes `.mp3` suffix for temp files: `suffix=".mp3"` (lines 146, 175) | Use dynamic suffix from original filename extension |
| 5 | [`app.py`](app.py:232) | 236-237 | MP3-only filter: `if not file.filename.lower().endswith(".mp3"): return error` | Remove this block entirely — accept any file |
| 6 | [`app.py`](app.py:232) | ~239-250 | No format detection | After reading file bytes, detect format from filename extension. Pass to `transcribe_inline()` / `transcribe_chunked()` |

#### Format detection helper

Add this function in `app.py` (after line 55):

```python
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma", ".m4a", ".opus", ".webm"}

def detect_audio_format(filename: str) -> str:
    """Extract audio format from filename extension. Default to 'mp3'."""
    ext = os.path.splitext(filename)[1].lower()
    # Map extension to format string (remove leading dot)
    fmt = ext.lstrip(".")
    # Handle common aliases
    if fmt in ("m4a",):
        return "mp4"
    if fmt in ("opus", "webm"):
        return "ogg"
    return fmt if fmt in ("mp3", "wav", "flac", "aac", "ogg") else "mp3"
```

#### Updated `build_openrouter_payload`

```python
def build_openrouter_payload(model_slug: str, b64_audio: str, audio_format: str = "mp3") -> dict:
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
```

#### Updated `transcribe` route (lines 232-257)

Remove the MP3-only check. Detect format from the uploaded file's extension:

```python
file = request.files["file"]
if not file.filename:
    return jsonify({"error": "No audio file selected"}), 400

# Detect audio format from extension
audio_format = detect_audio_format(file.filename)

audio_bytes = file.read()
model_slug = "google/gemini-3.1-flash-lite"

try:
    if len(audio_bytes) <= MAX_INLINE_BYTES:
        text = transcribe_inline(api_key, model_slug, audio_bytes, audio_format)
    else:
        text = transcribe_chunked(api_key, model_slug, audio_bytes, audio_format)
except RuntimeError as e:
    ...
```

#### Updated `transcribe_inline` (line 100)

```python
def transcribe_inline(api_key: str, model_slug: str, audio_bytes: bytes, audio_format: str = "mp3") -> str:
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    payload = build_openrouter_payload(model_slug, b64, audio_format)
    return call_openrouter(api_key, payload)
```

#### Updated `transcribe_chunked` (line 125)

Change `suffix=".mp3"` to `suffix=f".{audio_format}"` on both temp files (lines 146 and 175).

#### Validation (`app.py`)

Add a check that the file extension is actually an audio format. If not, return 400 error.

---

### 1B — Client-side (`templates/index.html`)

| # | Line(s) | Current | Change |
|---|---------|---------|--------|
| 1 | [22](templates/index.html:22) | `Upload an MP3 file and get instant transcription` | `Upload an audio file and get instant transcription` |
| 2 | [31](templates/index.html:31) | `Audio File (MP3 only)` | `Audio File` |
| 3 | [35](templates/index.html:35) | `Drag & drop your MP3 here or browse` | `Drag & drop your audio here or browse` |
| 4 | [39](templates/index.html:39) | `accept=".mp3,audio/mpeg"` | `accept="audio/*"` |
| 5 | [97-102](templates/index.html:97) | JS `handleFile()` checks `file.name.endsWith('.mp3')` | Remove the MP3-only check. Allow any file. |
| 6 | [125](templates/index.html:125) | `showError('Please select an MP3 file')` | `showError('Please select an audio file')` |

---

## Task 2: Rename Website to "BM Transcriber"

### 2A — `templates/base.html`

| # | Line | Current | Change |
|---|------|---------|--------|
| 1 | [6](templates/base.html:6) | `<title>{% block title %}Bengali Audio Transcriber{% endblock %}</title>` | `<title>{% block title %}BM Transcriber{% endblock %}</title>` |
| 2 | [32](templates/base.html:32) | `<a href="/" class="brand">🎤 Bengali Transcriber</a>` | `<a href="/" class="brand">🎤 BM Transcriber</a>` |
| 3 | [72](templates/base.html:72) | `<span>© 2026 <strong>Bengali Audio Transcriber</strong></span>` | `<span>© 2026 <strong>BM Transcriber</strong></span>` |

### 2B — `templates/index.html`

| # | Line | Current | Change |
|---|------|---------|--------|
| 1 | [2](templates/index.html:2) | `{% block title %}বাংলা Audio Transcriber{% endblock %}` | `{% block title %}BM Transcriber{% endblock %}` |
| 2 | [20](templates/index.html:20) | `<h1 class="premium-heading-lg">বাংলা Audio Transcriber</h1>` | `<h1 class="premium-heading-lg">BM Transcriber</h1>` |

### 2C — `templates/login.html`

| # | Line | Current | Change |
|---|------|---------|--------|
| 1 | [2](templates/login.html:2) | `{% block title %}Login — Bengali Audio Transcriber{% endblock %}` | `{% block title %}Login — BM Transcriber{% endblock %}` |

### 2D — `templates/register.html`

| # | Line | Current | Change |
|---|------|---------|--------|
| 1 | [2](templates/register.html:2) | `{% block title %}Register — Bengali Audio Transcriber{% endblock %}` | `{% block title %}Register — BM Transcriber{% endblock %}` |

### 2E — `templates/admin/dashboard.html`

| # | Line | Current | Change |
|---|------|---------|--------|
| 1 | [2](templates/admin/dashboard.html:2) | `{% block title %}Admin Dashboard — Bengali Audio Transcriber{% endblock %}` | `{% block title %}Admin Dashboard — BM Transcriber{% endblock %}` |

---

## Summary of files to modify

| File | Changes |
|------|---------|
| [`app.py`](app.py) | Add `detect_audio_format()` helper; update `build_openrouter_payload`, `transcribe_inline`, `transcribe_chunked`, and `transcribe` route |
| [`templates/base.html`](templates/base.html) | Update brand name, default title, footer (3 changes) |
| [`templates/index.html`](templates/index.html) | Update heading, description, dropzone text, accept attribute, JS validation (6 changes) |
| [`templates/login.html`](templates/login.html) | Update title (1 change) |
| [`templates/register.html`](templates/register.html) | Update title (1 change) |
| [`templates/admin/dashboard.html`](templates/admin/dashboard.html) | Update title (1 change) |
