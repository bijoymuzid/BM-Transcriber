/**
 * Sync Transcript Player
 * ======================
 *
 * Synchronises audio playback with word-by-word transcript highlighting.
 * Features:
 *   - Real-time word highlighting as audio plays (rAF-driven, ~60fps)
 *   - Click any word to seek to that position (with seek-guard against race conditions)
 *   - Editable transcript with server-side save
 *   - Auto-scroll active word into view
 *   - Playback speed controls
 *
 * Fixes applied (2026-07):
 *   1. Audio Tracking: Removed redundant timeupdate listener; uses only rAF sync loop.
 *      findActiveWord now respects word.end for gap handling.
 *   2. Click-to-Seek: Added seek-guard flag that suppresses onTimeUpdate during
 *      programmatic seeks. Seeks to word.start - 0.05s for browser time-snapping.
 *   3. Session Recovery: Removed client-side estimateWordsFromText() — timestamps
 *      are now estimated server-side from audio duration. No client estimation needed.
 *   4. Copy: Uses textContent instead of innerText for clean plain-text copy.
 */

(function () {
  "use strict";

  // -----------------------------------------------------------------------
  // DOM refs
  // -----------------------------------------------------------------------
  const loadingEl = document.getElementById("sync-loading");
  const audioCard = document.getElementById("sync-audio-card");
  const transcriptCard = document.getElementById("sync-transcript-card");

  const audio = document.getElementById("sync-audio");
  const playBtn = document.getElementById("play-btn");
  const seekBar = document.getElementById("seek-bar");
  const seekProgress = document.getElementById("seek-progress");
  const seekThumb = document.getElementById("seek-thumb");
  const timeDisplay = document.getElementById("time-display");
  const speedSelect = document.getElementById("speed-select");

  const container = document.getElementById("transcript-container");
  const transcriptContent = document.getElementById("transcript-content");
  const wordCount = document.getElementById("word-count");
  const syncStatus = document.getElementById("sync-status");

  const editBtn = document.getElementById("edit-btn");
  const saveBtn = document.getElementById("save-btn");
  const cancelBtn = document.getElementById("cancel-btn");
  const resetBtn = document.getElementById("reset-btn");
  const copyPlayerBtn = document.getElementById("copy-player-btn");

  // -----------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------
  const syncSessionId = container?.dataset.syncSessionId;
  const audioUrl = container?.dataset.audioUrl;

  let words = [];             // [{word, start, end, index}] — from server-side estimation
  let wordElements = [];      // HTMLElement[] in same order as words
  let currentWordIndex = -1;  // index of the currently highlighted word
  let isPlaying = false;
  let isEditing = false;
  let originalText = "";
  let rafId = null;

  // --- Seek guard ---
  // Prevents onTimeUpdate from overriding highlights during programmatic seeks.
  // Set to true before audio.currentTime = ... and released 150ms later via setTimeout.
  let isSeeking = false;
  let seekGuardTimeout = null;

  // Audio metadata loaded flag
  let audioReady = false;

  // -----------------------------------------------------------------------
  // Initialisation
  // -----------------------------------------------------------------------
  async function init() {
    if (!syncSessionId) return;

    try {
      const resp = await fetch(`/api/session-transcriptions/${syncSessionId}`);
      if (!resp.ok) throw new Error("Failed to load transcription");
      const data = await resp.json();

      words = data.words;
      originalText = data.text;

      audio.src = data.audio_url;
      audio.load();

      renderWords();
      showPlayer();

      wireAudioEvents();
      wireControlEvents();
      wireEditEvents();

      wordCount.textContent = `${words.length} words`;
      saveSessionToStorage(data);
    } catch (err) {
      loadingEl.innerHTML = `
        <div style="color:#fca5a5;padding:2rem">
          <h2>⚠️ Error loading transcription</h2>
          <p class="premium-text-muted">${err.message}</p>
          <a href="/" class="premium-btn premium-btn-primary" style="margin-top:1rem">Go back</a>
        </div>`;
    }
  }

  // -----------------------------------------------------------------------
  // Render words into the DOM
  // -----------------------------------------------------------------------
  function renderWords() {
    transcriptContent.innerHTML = "";
    wordElements = [];

    const fragment = document.createDocumentFragment();
    words.forEach(function (w, i) {
      const span = document.createElement("span");
      span.className = "sync-word sync-word-future";
      span.dataset.index = i;
      span.dataset.start = w.start;
      span.dataset.end = w.end;
      span.textContent = w.word;
      span.title = formatTime(w.start) + " \u2013 " + formatTime(w.end);

      // Click to seek (disabled while editing)
      span.addEventListener("click", function () {
        if (!isEditing) seekToWord(i);
      });

      fragment.appendChild(span);
      wordElements.push(span);

      // Add space after each word (except last)
      if (i < words.length - 1) {
        fragment.appendChild(document.createTextNode(" "));
      }
    });

    transcriptContent.appendChild(fragment);
  }

  // -----------------------------------------------------------------------
  // Show player UI, hide loading
  // -----------------------------------------------------------------------
  function showPlayer() {
    loadingEl.style.display = "none";
    audioCard.style.display = "block";
    transcriptCard.style.display = "block";
  }

  // -----------------------------------------------------------------------
  // Audio event wiring
  // -----------------------------------------------------------------------
  function wireAudioEvents() {
    // NOTE: We do NOT use the 'timeupdate' event for sync.
    // The rAF-based syncLoop (below) is more reliable (~60fps vs ~4Hz).
    // timeupdate is also unreliable when tab is backgrounded.

    // Play state changes (UI only — sync logic is in the rAF loop)
    audio.addEventListener("play", function () {
      isPlaying = true;
      updatePlayButton();
      syncStatus.textContent = "\u25B6 Playing";
      // Start the high-frequency sync loop
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(syncLoop);
    });

    audio.addEventListener("pause", function () {
      isPlaying = false;
      updatePlayButton();
      syncStatus.textContent = "\u23F8 Paused";
      // Stop the sync loop (one final update for accuracy)
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      if (!isSeeking) onTimeUpdate(); // final sync at exact pause position
    });

    // Metadata loaded — mark audio as ready
    audio.addEventListener("loadedmetadata", function () {
      audioReady = true;
      updateTimeDisplay();
    });

    audio.addEventListener("canplay", function () {
      audioReady = true;
    });

    audio.addEventListener("ended", function () {
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      if (words.length) {
        currentWordIndex = words.length - 1;
        highlightWord(currentWordIndex);
        updateSeekBar();
        updateTimeDisplay();
      }
    });

    // Speed change
    speedSelect.addEventListener("change", function () {
      audio.playbackRate = parseFloat(this.value);
    });

    // Seek bar click — seek to position (with seek-guard)
    seekBar.addEventListener("click", function (e) {
      if (!audio.duration) return;
      const rect = seekBar.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const seekTime = ratio * audio.duration;

      // Activate seek guard to prevent onTimeUpdate overriding
      isSeeking = true;
      if (seekGuardTimeout) clearTimeout(seekGuardTimeout);

      audio.currentTime = seekTime;

      // Find the nearest word and highlight it
      var nearestIdx = 0;
      if (words.length) {
        for (var i = 0; i < words.length; i++) {
          if (seekTime >= words[i].start && seekTime <= (words[i].end || words[i].start + 0.1)) {
            nearestIdx = i;
            break;
          }
          if (seekTime < words[i].start) {
            nearestIdx = Math.max(0, i - 1);
            break;
          }
          nearestIdx = i;
        }
      }
      currentWordIndex = nearestIdx;
      highlightWord(nearestIdx);

      releaseSeekGuard();
    });

    // --- rAF sync loop ---
    function syncLoop() {
      if (!audio.paused && !audio.ended) {
        if (!isSeeking) {
          onTimeUpdate();
        }
        rafId = requestAnimationFrame(syncLoop);
      }
    }
  }

  /**
   * Update play/pause button icons.
   */
  function updatePlayButton() {
    var playIcon = playBtn.querySelector(".play-icon");
    var pauseIcon = playBtn.querySelector(".pause-icon");
    if (playIcon) playIcon.style.display = isPlaying ? "none" : "inline";
    if (pauseIcon) pauseIcon.style.display = isPlaying ? "inline" : "none";
  }

  // -----------------------------------------------------------------------
  // Sync: find active word → highlight
  // -----------------------------------------------------------------------
  function onTimeUpdate() {
    if (isSeeking) return; // Skip during programmatic seeks

    const currentTime = audio.currentTime;
    const idx = findActiveWord(currentTime);

    if (idx !== currentWordIndex) {
      currentWordIndex = idx;
      highlightWord(idx);
      scrollToWord(idx);
    }

    updateSeekBar();
    updateTimeDisplay();
  }

  /**
   * Find the active word using a pointer-based walking approach.
   *
   * Uses LOCAL pointer state (never mutates currentWordIndex directly).
   * - During normal playback: O(1) amortized
   * - When seeking backward: walks backward from current position
   * - Gap handling: returns the previous word if time falls between words
   *   (i.e., stays on the word that was just spoken during silence/pauses)
   */
  function findActiveWord(time) {
    if (!words.length) return -1;

    // Use LOCAL state — never mutate currentWordIndex inside this function
    var pointer = currentWordIndex;
    if (pointer < 0 || pointer >= words.length) {
      pointer = 0;
    }

    // If past the last word's end, highlight the last word
    if (time >= words[words.length - 1].end) {
      return words.length - 1;
    }

    // If before the first word, highlight the first word
    if (time <= words[0].start) {
      return 0;
    }

    // Walk forward: advance while current time has passed the next word's start
    while (pointer < words.length - 1 && time >= words[pointer + 1].start) {
      pointer++;
    }

    // Walk backward: retreat pointer if user seeked to before this word's start
    while (pointer > 0 && time < words[pointer].start) {
      pointer--;
    }

    return pointer;
  }

  // -----------------------------------------------------------------------
  // DOM highlight updates
  // -----------------------------------------------------------------------
  function highlightWord(activeIdx) {
    wordElements.forEach(function (el, i) {
      el.classList.remove("sync-word-active", "sync-word-played", "sync-word-future");
      if (i === activeIdx) {
        el.classList.add("sync-word-active");
      } else if (i < activeIdx) {
        el.classList.add("sync-word-played");
      } else {
        el.classList.add("sync-word-future");
      }
    });
  }

  function scrollToWord(idx, behavior) {
    if (behavior === undefined) behavior = "smooth";
    const el = wordElements[idx];
    if (!el) return;
    el.scrollIntoView({ behavior: behavior, block: "center" });
  }

  // -----------------------------------------------------------------------
  // Seek bar & time display
  // -----------------------------------------------------------------------
  function updateSeekBar() {
    const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
    seekProgress.style.width = pct + "%";
    seekThumb.style.left = pct + "%";
  }

  function updateTimeDisplay() {
    const cur = formatTime(audio.currentTime);
    const dur = formatTime(audio.duration || 0);
    timeDisplay.textContent = cur + " / " + dur;
  }

  function formatTime(sec) {
    if (!sec || !isFinite(sec)) return "0:00";
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  // -----------------------------------------------------------------------
  // Shared clipboard utility (used by both index and player pages)
  // -----------------------------------------------------------------------
  function copyToClipboard(text, buttonEl, successLabel, resetLabel) {
    if (!text) return;
    if (successLabel === undefined) successLabel = "\u2705 Copied!";
    if (resetLabel === undefined) resetLabel = "\uD83D\uDCCB Copy";

    var doCopy = function () {
      // Try modern clipboard API first
      if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          showCopySuccess(buttonEl, successLabel, resetLabel);
        }).catch(function () {
          fallbackCopy(text, buttonEl, successLabel, resetLabel);
        });
      } else {
        fallbackCopy(text, buttonEl, successLabel, resetLabel);
      }
    };

    // Need user gesture for clipboard API — wrap in microtask if called from non-gesture context
    if (document.hasFocus && !document.hasFocus()) {
      setTimeout(doCopy, 50);
    } else {
      doCopy();
    }
  }

  function fallbackCopy(text, buttonEl, successLabel, resetLabel) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      ta.style.pointerEvents = "none";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      showCopySuccess(buttonEl, successLabel, resetLabel);
    } catch (e) {
      buttonEl.textContent = "\u274C Copy failed";
      setTimeout(function () { buttonEl.textContent = resetLabel; }, 2000);
    }
  }

  function showCopySuccess(buttonEl, successLabel, resetLabel) {
    buttonEl.textContent = successLabel;
    buttonEl.classList.add("copied");
    setTimeout(function () {
      buttonEl.textContent = resetLabel;
      buttonEl.classList.remove("copied");
    }, 2000);
  }

  // -----------------------------------------------------------------------
  // Copy transcript text (player page)
  // -----------------------------------------------------------------------
  function copyTranscriptText() {
    // Use textContent on the child spans for clean plain-text extraction
    // instead of innerText which has rendering artifacts
    var spans = transcriptContent.querySelectorAll(".sync-word");
    var text = "";
    spans.forEach(function (span, i) {
      if (i > 0) text += " ";
      text += span.textContent;
    });
    text = text.trim();
    if (!text) return;

    copyToClipboard(text, copyPlayerBtn);
  }

  // -----------------------------------------------------------------------
  // Persist session data across page navigations
  // -----------------------------------------------------------------------
  function saveSessionToStorage(data) {
    try {
      sessionStorage.setItem("bm_sync_session_id", syncSessionId);
      sessionStorage.setItem("bm_sync_audio_url", data.audio_url);
      sessionStorage.setItem("bm_sync_text", data.text);
      sessionStorage.setItem("bm_sync_duration", String(data.duration));
    } catch (e) {
      // sessionStorage may be full or unavailable — silently ignore
    }
  }

  // -----------------------------------------------------------------------
  // Seek-guard helpers
  // -----------------------------------------------------------------------
  function releaseSeekGuard() {
    if (seekGuardTimeout) clearTimeout(seekGuardTimeout);
    seekGuardTimeout = setTimeout(function () {
      isSeeking = false;
      seekGuardTimeout = null;
      // Do a sync after releasing guard to correct any drift
      onTimeUpdate();
    }, 150); // 150ms should be enough for browser seek to complete
  }

  // -----------------------------------------------------------------------
  // Click-to-seek
  // -----------------------------------------------------------------------
  function seekToWord(idx) {
    if (idx < 0 || idx >= words.length) return;

    // Activate seek guard to prevent onTimeUpdate from overriding
    isSeeking = true;
    if (seekGuardTimeout) clearTimeout(seekGuardTimeout);

    // Seek slightly before the word start (~50ms) to account for
    // browser time-snapping and provide a natural playback flow
    const seekTime = Math.max(0, words[idx].start - 0.05);

    if (!audioReady) {
      var waitForReady = function () {
        if (audioReady) {
          audio.currentTime = seekTime;
          currentWordIndex = idx;
          highlightWord(idx);
          scrollToWord(idx, "instant");
          releaseSeekGuard();
          if (audio.paused) {
            audio.play().catch(function () {});
          }
        } else {
          setTimeout(waitForReady, 50);
        }
      };
      waitForReady();
      return;
    }

    audio.currentTime = seekTime;
    currentWordIndex = idx;
    highlightWord(idx);
    scrollToWord(idx, "instant");
    releaseSeekGuard();

    // Automatically start playback when user clicks a word
    if (audio.paused) {
      audio.play().catch(function () {});
    }
  }

  // -----------------------------------------------------------------------
  // Control event wiring (play/pause, etc.)
  // -----------------------------------------------------------------------
  function wireControlEvents() {
    playBtn.addEventListener("click", function () {
      if (audio.paused) {
        audio.play().catch(function () {});
      } else {
        audio.pause();
      }
    });
  }

  // -----------------------------------------------------------------------
  // Edit system
  // -----------------------------------------------------------------------
  function wireEditEvents() {
    editBtn.addEventListener("click", enterEditMode);
    saveBtn.addEventListener("click", saveEdits);
    cancelBtn.addEventListener("click", cancelEdit);
    resetBtn.addEventListener("click", resetTranscript);
    if (copyPlayerBtn) {
      copyPlayerBtn.addEventListener("click", copyTranscriptText);
    }
  }

  function enterEditMode() {
    if (isEditing) return;
    isEditing = true;
    transcriptContent.contentEditable = "true";
    transcriptContent.classList.add("sync-editing");
    transcriptContent.focus();
    editBtn.style.display = "none";
    saveBtn.style.display = "inline-flex";
    cancelBtn.style.display = "inline-flex";
    resetBtn.style.display = "none";
    syncStatus.textContent = "\u270F\uFE0F Editing";
  }

  function exitEditMode() {
    isEditing = false;
    transcriptContent.contentEditable = "false";
    transcriptContent.classList.remove("sync-editing");
    editBtn.style.display = "inline-flex";
    saveBtn.style.display = "none";
    cancelBtn.style.display = "none";
    resetBtn.style.display = "inline-flex";
    syncStatus.textContent = isPlaying ? "\u25B6 Playing" : "\u23F8 Paused";
  }

  async function saveEdits() {
    const editedText = transcriptContent.innerText.trim();
    if (!editedText) {
      alert("Transcript cannot be empty.");
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = "\u23F3 Saving\u2026";

    try {
      const resp = await fetch("/api/session-transcriptions/" + syncSessionId, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edited_text: editedText }),
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || "Save failed");
      }

      const data = await resp.json();

      // Update local state with realigned words
      words = data.words;
      currentWordIndex = -1;
      originalText = data.text;

      // Re-render DOM with new word spans
      renderWords();

      exitEditMode();
      syncStatus.textContent = "\u2705 Saved";
      setTimeout(function () {
        syncStatus.textContent = isPlaying ? "\u25B6 Playing" : "\u23F8 Paused";
      }, 2000);
    } catch (err) {
      alert("Save error: " + err.message);
      exitEditMode();
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "\uD83D\uDCBE Save";
    }
  }

  function cancelEdit() {
    transcriptContent.innerText = originalText;
    exitEditMode();
    renderWords();
  }

  async function resetTranscript() {
    if (!confirm("Reset transcript to original? Edited text will be lost.")) return;
    try {
      const resp = await fetch("/api/session-transcriptions/" + syncSessionId);
      if (!resp.ok) throw new Error("Failed to load original");
      const data = await resp.json();

      words = data.words;
      originalText = data.text;
      currentWordIndex = -1;
      renderWords();
      syncStatus.textContent = "\u21BA Reset to original";
      setTimeout(function () {
        syncStatus.textContent = isPlaying ? "\u25B6 Playing" : "\u23F8 Paused";
      }, 2000);
    } catch (err) {
      alert("Reset error: " + err.message);
    }
  }

  // -----------------------------------------------------------------------
  // Keyboard shortcuts
  // -----------------------------------------------------------------------
  document.addEventListener("keydown", function (e) {
    if (isEditing) return;

    if (e.code === "Space" && e.target === document.body) {
      e.preventDefault();
      playBtn.click();
    }
    if (e.code === "ArrowLeft") {
      e.preventDefault();
      audio.currentTime = Math.max(0, audio.currentTime - 5);
    }
    if (e.code === "ArrowRight") {
      e.preventDefault();
      audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5);
    }
  });

  // -----------------------------------------------------------------------
  // Start!
  // -----------------------------------------------------------------------
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
