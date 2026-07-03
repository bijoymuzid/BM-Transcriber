"""
Word Aligner Service

Implements Needleman-Wunsch sequence alignment to map edited words
back to original timestamps. This allows users to edit the transcript
text while maintaining audio sync for unchanged words.

Strategy:
- Original words are the "reference sequence" with known timestamps
- Edited words are the "query sequence"
- Matched words → keep original timestamps
- Inserted words → interpolate timestamps from neighbors
- Deleted words → removed from final array
"""

from typing import TypedDict


class AlignedWord(TypedDict):
    """A word with its final aligned timestamp."""
    word: str
    start: float
    end: float
    index: int
    unchanged: bool  # True if this word's timestamp came from original


# ---------------------------------------------------------------------------
# Sequence alignment (Needleman-Wunsch)
# ---------------------------------------------------------------------------

def _align_sequences(original: list[str], edited: list[str]) -> list[tuple[int | None, int | None]]:
    """
    Align two sequences of words. Returns a list of (orig_idx, edit_idx) pairs.
    None indicates a gap (insertion or deletion).

    Uses Needleman-Wunsch with:
    - Match score: +2
    - Mismatch score: -1
    - Gap penalty: -1

    The matching is case-insensitive and punctuation-insensitive
    for better handling of minor corrections.
    """
    n, m = len(original), len(edited)

    # Scoring matrix
    dp = [[0] * (m + 1) for _ in range(n + 1)]

    # Initialise gaps
    for i in range(n + 1):
        dp[i][0] = -i
    for j in range(m + 1):
        dp[0][j] = -j

    # Fill matrix
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            match = dp[i - 1][j - 1] + _score(original[i - 1], edited[j - 1])
            delete = dp[i - 1][j] - 1
            insert = dp[i][j - 1] - 1
            dp[i][j] = max(match, delete, insert)

    # Traceback
    alignment: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + _score(original[i - 1], edited[j - 1]):
            alignment.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] - 1:
            alignment.append((i - 1, None))  # deletion in edited text
            i -= 1
        else:
            alignment.append((None, j - 1))  # insertion in edited text
            j -= 1

    alignment.reverse()
    return alignment


def _score(a: str, b: str) -> int:
    """
    Compare two words for alignment scoring.
    Strips punctuation and lowercases for fuzzy matching.
    """
    clean_a = a.strip(".,!?;:\"'()[]{}").lower()
    clean_b = b.strip(".,!?;:\"'()[]{}").lower()
    if clean_a == clean_b:
        return 2
    if clean_a and clean_b and (clean_a in clean_b or clean_b in clean_a):
        return 1  # partial match (e.g. "world" vs "world,")
    return -1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def align_edited_words(
    original_words: list[dict],
    edited_text: str,
) -> list[AlignedWord]:
    """
    Align edited text back to original word timestamps.

    Parameters
    ----------
    original_words : list[dict]
        Original word array from DB: [{word, start, end, index}, ...]
    edited_text : str
        The user's edited version of the transcript.

    Returns
    -------
    list[AlignedWord]
        New word array with realigned timestamps.
        Each entry: {word, start, end, index, unchanged}
        'unchanged' is True if the timestamp came from the original.
    """
    if not original_words:
        # No original reference — distribute uniformly
        tokens = edited_text.strip().split()
        if not tokens:
            return []
        # We need duration context, so fall back gracefully
        return []  # caller should handle this case

    original_tokens = [w["word"] for w in original_words]
    edited_tokens = edited_text.strip().split()
    if not edited_tokens:
        return []

    alignment = _align_sequences(original_tokens, edited_tokens)

    # Build result preserving timestamps for matched words
    result: list[AlignedWord] = []
    used_original_indices: set[int] = set()
    inserted_indices: list[int] = []  # positions in result that are insertions

    for orig_idx, edit_idx in alignment:
        if orig_idx is not None and edit_idx is not None:
            # Match — preserve original timestamp
            ow = original_words[orig_idx]
            result.append({
                "word": edited_tokens[edit_idx],
                "start": ow["start"],
                "end": ow["end"],
                "index": len(result),
                "unchanged": True,
            })
            used_original_indices.add(orig_idx)
        elif edit_idx is not None:
            # Insertion — will interpolate after
            result.append({
                "word": edited_tokens[edit_idx],
                "start": 0.0,
                "end": 0.0,
                "index": len(result),
                "unchanged": False,
            })
            inserted_indices.append(len(result) - 1)
        # Deletions (orig_idx not None, edit_idx None) are simply skipped

    # Interpolate timestamps for inserted words
    _interpolate_insertions(result, inserted_indices, original_words)

    # Re-number indices sequentially
    for i, w in enumerate(result):
        w["index"] = i

    return result


def _interpolate_insertions(
    words: list[AlignedWord],
    inserted_indices: list[int],
    original_words: list[dict],
) -> None:
    """
    For inserted words, interpolate timestamps from the nearest
    unchanged neighbors. If inserted at the beginning, derive from
    the first unchanged word. If at the end, derive from the last.
    """
    if not inserted_indices:
        return

    for idx in inserted_indices:
        # Find nearest unchanged before
        before = None
        for j in range(idx - 1, -1, -1):
            if words[j]["unchanged"]:
                before = words[j]
                break

        # Find nearest unchanged after
        after = None
        for j in range(idx + 1, len(words)):
            if words[j]["unchanged"]:
                after = words[j]
                break

        if before and after:
            dur = after["start"] - before["end"]
            words[idx]["start"] = before["end"]
            words[idx]["end"] = before["end"] + dur * 0.5
        elif before:
            # Inserted after last unchanged word
            avg_word_dur = (before["end"] - before["start"]) * 0.5
            words[idx]["start"] = before["end"]
            words[idx]["end"] = before["end"] + avg_word_dur
        elif after:
            # Inserted before first unchanged word
            avg_word_dur = (after["end"] - after["start"]) * 0.5
            words[idx]["end"] = after["start"]
            words[idx]["start"] = max(0, after["start"] - avg_word_dur)
        else:
            # No reference at all — assign zero
            words[idx]["start"] = 0.0
            words[idx]["end"] = 0.0

        words[idx]["start"] = round(words[idx]["start"], 3)
        words[idx]["end"] = round(words[idx]["end"], 3)
