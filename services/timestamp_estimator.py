"""
Timestamp Estimator — Word Timestamp Estimation from Audio Duration

Two-stage strategy using tools already in this project (ffmpeg):

  1. SILENCE DETECTION (ffmpeg silencedetect filter)
     Finds actual pauses in the audio so words are never placed inside a
     silent gap. This is the single biggest driver of "text and audio drift"
     when doing pure math-based estimation.

  2. CHARACTER-WEIGHTED DISTRIBUTION
     Within each speech segment, words are given duration proportional to
     their length (longer words take longer to say than short ones) instead
     of splitting the segment evenly.

If ffmpeg or silence detection fails for any reason, this falls back to a
simple character-weighted uniform distribution across the whole audio
duration (still smarter than pure word-count division, just without the
pause-awareness).

Since we use Gemini 3.1 Flash (which doesn't provide per-word timing),
this is the primary method for generating word-level timestamps in the
sync player flow.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile


# ---------------------------------------------------------------------------
# Tunables — adjust if results feel off for your audio style
# ---------------------------------------------------------------------------

SILENCE_NOISE_THRESHOLD = "-30dB"   # quieter than this = silence
SILENCE_MIN_DURATION = 0.3          # seconds; ignore blips shorter than this
MIN_WORD_WEIGHT = 2                 # floor so very short words (e.g. "I", "a") still get some time
PAUSE_PADDING = 0.05                # small buffer so a word's end doesn't touch a detected silence exactly


def _find_tool(name: str) -> str | None:
    """Same lookup pattern as the rest of the app — bare command if on PATH."""
    path = shutil.which(name)
    if path:
        return name
    return None


def _find_silence_gaps(audio_path: str) -> list[tuple[float, float]]:
    """
    Run ffmpeg's silencedetect filter and parse silence_start/silence_end
    pairs from stderr. Returns a list of (start, end) tuples in seconds,
    sorted by start time. Returns [] if detection fails or finds nothing.
    """
    ffmpeg_cmd = _find_tool("ffmpeg")
    if not ffmpeg_cmd:
        return []

    try:
        result = subprocess.run(
            [
                ffmpeg_cmd, "-v", "quiet", "-stats",
                "-i", audio_path,
                "-af", f"silencedetect=noise={SILENCE_NOISE_THRESHOLD}:d={SILENCE_MIN_DURATION}",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return []

    # ffmpeg writes silencedetect output to stderr even in quiet mode
    output = result.stderr or ""

    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", output)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", output)]

    # silencedetect can occasionally emit an unmatched trailing start
    # (silence running to end-of-file) — pair what we can, drop the rest
    pairs = list(zip(starts, ends))
    pairs.sort(key=lambda p: p[0])
    return pairs


def _speech_segments(audio_duration: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """
    Given the full audio duration and detected silence gaps, return the
    complementary list of (start, end) speech segments — i.e. everything
    that ISN'T silence. Always returns at least one segment.
    """
    if not silences:
        return [(0.0, audio_duration)]

    segments = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor:
            segments.append((cursor, s_start))
        cursor = max(cursor, s_end)
    if cursor < audio_duration:
        segments.append((cursor, audio_duration))

    # Filter out degenerate/near-zero segments
    segments = [(a, b) for a, b in segments if (b - a) > 0.05]
    return segments if segments else [(0.0, audio_duration)]


def _word_weight(word: str) -> float:
    """
    Character-length weight for a word, with a floor so short words still
    get a reasonable minimum slice of time. Strips punctuation before
    counting so trailing commas/periods don't inflate the weight.
    """
    stripped = re.sub(r"[^\w\u0980-\u09FF]", "", word)  # keep Bengali unicode range + word chars
    return max(len(stripped), MIN_WORD_WEIGHT)


def _distribute_words_over_segments(
    words: list[str], segments: list[tuple[float, float]]
) -> list[dict]:
    """
    Walk through speech segments in order, filling each with words
    proportional to their character-weight, until segment capacity is
    reached, then move to the next segment (skipping the silence gap
    between them). This is what keeps words OUT of silent pauses.
    """
    if not words:
        return []

    weights = [_word_weight(w) for w in words]
    total_weight = sum(weights)
    total_speech_duration = sum(end - start for start, end in segments)

    if total_speech_duration <= 0:
        total_speech_duration = 1.0  # degenerate guard

    seconds_per_weight_unit = total_speech_duration / total_weight

    results: list[dict] = []
    seg_idx = 0
    seg_start, seg_end = segments[0]
    cursor = seg_start

    for i, word in enumerate(words):
        word_duration = weights[i] * seconds_per_weight_unit

        # If this word would overflow the current segment, jump to the next one
        if cursor + word_duration > seg_end and seg_idx < len(segments) - 1:
            seg_idx += 1
            seg_start, seg_end = segments[seg_idx]
            cursor = seg_start

        start = cursor
        end = min(cursor + word_duration, seg_end) if seg_idx < len(segments) else cursor + word_duration
        end = max(end - PAUSE_PADDING, start + 0.01)

        results.append({
            "word": word,
            "start": round(start, 3),
            "end": round(end, 3),
            "index": i,
            "unchanged": False,
        })

        cursor = end + PAUSE_PADDING

    return results


def estimate_word_timestamps(
    text: str,
    audio_duration: float,
    audio_path: str | None = None,
) -> list[dict]:
    """
    Main entry point. Estimates word-level timestamps for `text` spread
    across `audio_duration` seconds.

    Parameters
    ----------
    text : str
        The transcription text to estimate timestamps for.
    audio_duration : float
        Total audio duration in seconds (or chunk duration for chunked mode).
    audio_path : str | None
        Path to the audio file on disk, used for silence detection with ffmpeg.
        If None, or if silence detection fails/finds nothing, falls back to
        character-weighted uniform distribution across the full duration
        (no pause-awareness, but still better than naive equal-division).

    Returns
    -------
    list[dict]
        Estimated word array: [{word, start, end, index, unchanged}]
        All words have `unchanged: False` since these are estimated.
        Shape matches what word_aligner.py and sync-player.js already expect.
    """
    words = text.split()
    if not words:
        return []

    segments: list[tuple[float, float]] = [(0.0, audio_duration)]

    if audio_path and os.path.exists(audio_path):
        silences = _find_silence_gaps(audio_path)
        if silences:
            segments = _speech_segments(audio_duration, silences)

    return _distribute_words_over_segments(words, segments)


def estimate_word_timestamps_from_bytes(
    text: str,
    audio_duration: float,
    audio_bytes: bytes,
    audio_format: str = "mp3",
) -> list[dict]:
    """
    Convenience wrapper for callers that only have audio bytes in memory
    rather than a file already on disk. Writes to a temp file just long
    enough to run silence detection, then cleans up.

    Parameters
    ----------
    text : str
        The transcription text.
    audio_duration : float
        Audio duration in seconds.
    audio_bytes : bytes
        Raw audio file bytes.
    audio_format : str
        Audio format extension (e.g. "mp3", "wav", "flac").

    Returns
    -------
    list[dict]
        Estimated word array: [{word, start, end, index, unchanged}]
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        return estimate_word_timestamps(text, audio_duration, audio_path=tmp_path)
    except Exception:
        # Last-resort fallback: no silence detection at all
        return estimate_word_timestamps(text, audio_duration, audio_path=None)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
