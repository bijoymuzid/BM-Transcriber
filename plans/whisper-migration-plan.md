# Two-Stage Pipeline: Gemini 3.1 Flash + Whisper-1

## Architecture Overview

The app now uses a **two-stage pipeline** for the sync player:

```
┌─────────────────────────────────────────────────────────────┐
│                    TWO-STAGE PIPELINE                        │
│                                                             │
│  Audio File                                                 │
│      │                                                      │
│      ├──► Stage 1: Gemini 3.1 Flash ───► Transcription Text │
│      │              (Chat Completions)    (Bengali-optimized│
│      │                                    with speaker ID)  │
│      │                                                      │
│      └──► Stage 2: Whisper-1 ──────────► Word Timestamps    │
│                     (Audio Transcriptions) (precise start/end│
│                                              per word)      │
│                                                             │
│      ───► Stage 3: Word Aligner ───────► Aligned Words     │
│                     (Needleman-Wunsch)   (Gemini text mapped│
│                                          to Whisper times)  │
└─────────────────────────────────────────────────────────────┘
```

## Why Two Models?

| Aspect | Gemini 3.1 Flash | Whisper-1 |
|---|---|---|
| **Transcription** | ✅ Excellent Bengali + speaker ID | ⚠️ Generic language model |
| **Word Timestamps** | ❌ Returns plain text only | ✅ Native `verbose_json` with `timestamp_granularities[]=word` |
| **Custom Prompting** | ✅ Full prompt control | ❌ No prompt support |
| **API Format** | Chat Completions (JSON + base64) | Audio Transcriptions (multipart upload) |

By using **both**, we get:
- **Gemini's** high-quality Bengali transcription with speaker identification
- **Whisper's** precise word-level timestamps
- **Word Aligner** matching Gemini's words to Whisper's timing data

## Route Behavior

| Route | Model(s) Used | Returns |
|---|---|---|
| `POST /transcribe` | Gemini 3.1 Flash only | `{"transcription": "..."}` |
| `POST /transcribe-with-words` | Gemini + Whisper + Aligner | `{id, words[], text, duration, audio_url}` |

## Files Changed

### [`app.py`](Local LLM/app.py)
- **Kept**: `OPENROUTER_URL`, `TRANSCRIPTION_PROMPT`, `build_gemini_payload()`, `call_gemini()`
- **Added**: `WHISPER_URL`, `transcribe_whisper()`, `get_whisper_word_timestamps()`
- **Added**: `transcribe_sync_inline()`, `transcribe_sync_chunked()` (combined pipeline)
- **Updated**: `/transcribe` uses Gemini only
- **Updated**: `/transcribe-with-words` uses Gemini + Whisper + aligner

### [`services/word_aligner.py`](Local LLM/services/word_aligner.py)
- Used to align Gemini's text output with Whisper's timestamped words
- Needleman-Wunsch matches similar words and preserves Whisper's precise timestamps

### [`services/timestamp_estimator.py`](Local LLM/services/timestamp_estimator.py)
- **Deleted** — no longer needed since Whisper provides precise timestamps

### [`static/js/sync-player.js`](Local LLM/static/js/sync-player.js)
- Removed client-side `estimateWordsFromText()` function

### [`templates/admin/dashboard.html`](Local LLM/templates/admin/dashboard.html)
- Updated model display to show "Gemini 3.1 Flash + Whisper-1 timestamps"

## How It Works (Detailed)

### For `/transcribe-with-words` (sync):

```
1. Receive audio file (multipart upload)
2. Detect format, get duration (ffprobe)
3. If ≤10 MB:
   a. Gemini: audio → base64 → Chat Completions API → transcription text
   b. Whisper: audio → multipart upload → Audio Transcriptions API → [{word, start, end}, ...]
   c. Word Aligner: align Gemini's text words with Whisper's timestamped words
   d. Return {text, words[], duration}
4. If >10 MB:
   a. ffmpeg split into ≤10 MB chunks
   b. For each chunk: repeat steps 3a-3c
   c. Adjust word timestamps by chunk offset
   d. Merge all words and text
   e. Return {text, words[], duration}
```

### For `/transcribe` (basic):

```
1. Same file handling
2. Gemini only (no Whisper call)
3. Return plain text
```
