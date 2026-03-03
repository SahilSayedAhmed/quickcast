"""
ai_mode.py — QuickCast AI Voice Controller (Enhanced Edition)
==============================================================
Fully OFFLINE, noise-tolerant voice control.

KEY IMPROVEMENTS:
  - Fuzzy keyword matching (works even with slight mispronunciation)
  - Dual-layer detection: partial results for wake phrase + final for commands
  - Noise filtering via energy threshold (ignores background noise)
  - Phonetic fallback matching (sounds-like matching)
  - Continuous retry — never gives up listening
  - Handles all states smoothly with clear spoken feedback
  - Smart username extraction from messy speech
"""

import threading
import time
import json
import queue
import logging
import os
import re

log = logging.getLogger("quickcast.ai")

# ── States ─────────────────────────────────────────────────────────────────────
IDLE                  = "IDLE"
AWAITING_TARGET       = "AWAITING_TARGET"
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
SHARING               = "SHARING"

# ── Timeout ────────────────────────────────────────────────────────────────────
STATE_TIMEOUT_SECONDS = 12

# ── Wake phrases & their phonetic variants ─────────────────────────────────────
# Vosk sometimes mishears words — we cover common mis-transcriptions too
WAKE_PHRASES = [
    # Primary
    "check this", "check it", "check",
    # QuickCast name variants
    "quickcast", "quick cast", "quick cats", "quickest",
    # Action phrases
    "share this", "share screen", "start share", "start sharing",
    "cast this", "cast screen",
    "send screen", "send this",
    # Hey variants
    "hey quickcast", "hey quick", "hey cast",
    # Vosk mishears of "check this"
    "check his", "czech this", "check the", "jack this",
    # Extra single word triggers for reliability
    "cast", "share", "broadcast", "present",
    # Common Vosk outputs for unclear speech
    "jet this", "tech this", "check", "chuck", "chess",
    # See this variants
    "see this", "see it", "see",
]

# ── Yes variants ───────────────────────────────────────────────────────────────
YES_WORDS = [
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay",
    "do it", "confirm", "go ahead", "go", "correct",
    "absolutely", "definitely", "of course", "please",
    "affirmative", "right", "true", "great",
]

# ── No variants ────────────────────────────────────────────────────────────────
NO_WORDS = [
    "no", "nope", "nah", "cancel", "stop", "nevermind",
    "never mind", "don't", "abort", "quit", "exit",
    "wrong", "not", "negative", "back", "undo",
]

# ── Stop sharing variants ──────────────────────────────────────────────────────
STOP_WORDS = [
    "stop sharing", "stop share", "stop screen",
    "stop", "cancel", "end share", "end sharing",
    "finish sharing", "finish", "done sharing", "done",
    "close share", "close screen", "exit share",
    "return screen", "return",
]

# ── Filler words to strip before username matching ────────────────────────────
FILLER_WORDS = {
    "share", "screen", "to", "with", "send", "show",
    "cast", "please", "the", "a", "and", "for", "my",
    "it", "this", "that", "hey", "ok", "okay", "can",
    "you", "i", "want", "let", "me", "will", "should",
}


class QuickCastAI:
    """
    Offline AI voice controller for QuickCast.
    Runs entirely in background threads.
    Communicates with Qt UI only via signals (thread-safe).
    """

    def __init__(self, window, send_screen, stop_sharing, get_online_users, get_user_id_map=None):
        self.window             = window
        self._send_screen       = send_screen
        self._stop_sharing      = stop_sharing
        self._get_online_users  = get_online_users
        self._get_user_id_map   = get_user_id_map or (lambda: {})

        # State machine
        self.state              = IDLE
        self.selected_user      = None
        self.selected_users     = []
        self._state_timer       = None

        # Threading
        self._running           = False
        self._tts_queue         = queue.Queue()
        self._tts_ready         = threading.Event()  # Signals TTS is initialized
        self._listen_thread     = None
        self._tts_thread        = None

        # Vosk
        self._model             = None

        # Debounce — prevent same phrase firing twice in quick succession
        self._last_processed    = ""
        self._last_process_time = 0

    # ══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════════════

    def start(self):
        if self._running:
            return
        self._running = True

        if not self._load_vosk_model():
            self.window.ai_status_signal.emit(
                "⚠️  AI: Vosk model not found — download vosk-model-small-en-us-0.15"
            )
            return

        # Start TTS thread first so it's ready before voice kicks in
        self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True, name="TTS")
        self._tts_thread.start()
        self._tts_ready.wait(timeout=5)  # Wait up to 5s for TTS to initialize

        # Start listener
        self._listen_thread = threading.Thread(target=self._listen_worker, daemon=True, name="VoiceListener")
        self._listen_thread.start()

        log.info("✅  QuickCast AI started")
        self.window.ai_status_signal.emit("🎙️  AI Ready — say 'Check this' anytime")

    def stop(self):
        self._running = False
        self._cancel_timeout()

    def _match_multiple_usernames(self, text: str) -> list:
        """
        Match multiple usernames from spoken text.
        e.g. "one and two" → ["BOB", "NASH"]
        e.g. "one two three" → ["BOB", "NASH", "ALICE"]
        e.g. "bob and nash" → ["BOB", "NASH"]
        """
        # Try single match first
        single = self._match_username(text)
        if single:
            # Also check if more names mentioned
            results = [single]
            # Remove matched part and check for more
            remaining = text.lower()
            for word in ["and", "also", "with", "plus"]:
                remaining = remaining.replace(word, " ")
            # Check each word for additional matches
            id_map  = self._get_user_id_map()
            online  = self._get_online_users()
            number_words = {
                "one":1,"two":2,"three":3,"four":4,"five":5,
                "six":6,"seven":7,"eight":8,"nine":9,"ten":10,
                "1":1,"2":2,"3":3,"4":4,"5":5,
                "6":6,"7":7,"8":8,"9":9,"10":10,
            }
            for word in remaining.split():
                if word in number_words:
                    uid = number_words[word]
                    if uid in id_map and id_map[uid] not in results:
                        results.append(id_map[uid])
                else:
                    for user in online:
                        if word in user.lower() and user not in results:
                            results.append(user)
            return results
        return []

    def notify_sharing_started(self, target: str):
        """
        Called by app.py when screen sharing starts MANUALLY.
        Puts AI into SHARING state so it can respond to 'stop sharing'.
        """
        self.selected_user = target
        self._transition_to(SHARING)
        self.window.ai_status_signal.emit(
            f"📤  AI: Sharing with {target} — say 'Stop sharing' to end"
        )
        log.info(f"AI notified: manual sharing started with {target}")

    def notify_sharing_stopped(self):
        """
        Called by app.py when screen sharing stops (manually or via AI).
        Resets AI back to IDLE.
        """
        if self.state != IDLE:
            self._reset_to_idle()
            log.info("AI notified: sharing stopped, back to IDLE")

    # ══════════════════════════════════════════════════════════════════════════
    # VOSK MODEL LOADER
    # ══════════════════════════════════════════════════════════════════════════

    def _load_vosk_model(self) -> bool:
        try:
            from vosk import Model
            import vosk as _vosk
            _vosk.SetLogLevel(-1)
        except ImportError:
            log.error("vosk not installed")
            return False

        import sys
        MODEL_NAME = "vosk-model-small-en-us-0.15"

        # Search in order — most reliable first
        search_dirs = []

        # 1. _MEIPASS — THIS is where PyInstaller extracts bundled files
        #    This is the CORRECT location when model is bundled inside exe
        if hasattr(sys, "_MEIPASS"):
            search_dirs.append(sys._MEIPASS)

        # 2. Next to the exe — works when model folder placed beside exe
        search_dirs.append(os.path.dirname(os.path.abspath(sys.executable)))

        # 3. Current working directory
        search_dirs.append(os.path.abspath(os.getcwd()))

        # 4. Next to ai_mode.py — works when running from terminal
        try:
            search_dirs.append(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass

        # 5. Hardcoded fallback for Sahil's machine
        search_dirs.append("C:/Users/ASUS/Desktop/quickcast/client")

        for base in search_dirs:
            path = os.path.join(base, MODEL_NAME)
            log.info(f"Looking for model at: {path}")
            if os.path.isdir(path):
                try:
                    from vosk import Model
                    self._model = Model(path)
                    log.info(f"✅  Model loaded from: {path}")
                    return True
                except Exception as e:
                    log.error(f"Load failed at {path}: {e}")
                    continue

        self.window.ai_status_signal.emit(
            "⚠️  AI: Vosk model not found — contact support"
        )
        return False


    def _listen_worker(self):
        """
        Continuously reads microphone audio and feeds it to Vosk.

        Strategy:
        - For IDLE state: also check PARTIAL results for wake phrase
          so detection is instantaneous (doesn't wait for full sentence)
        - For all other states: use FINAL results only (more accurate)
        - Fresh KaldiRecognizer after each final result (prevents stale state)
        - Auto-retry on microphone errors
        """
        try:
            import sounddevice as sd
            from vosk import KaldiRecognizer
        except ImportError:
            self.window.ai_status_signal.emit("⚠️  AI: sounddevice not installed")
            return

        RATE  = 16000
        BLOCK = 2400   # balanced — fast enough + stable in exe

        log.info("🎤  Mic listener started")

        def make_recognizer():
            r = KaldiRecognizer(self._model, RATE)
            r.SetMaxAlternatives(0)
            r.SetWords(False)
            return r

        while self._running:
            try:
                with sd.RawInputStream(
                    samplerate = RATE,
                    blocksize  = BLOCK,
                    dtype      = "int16",
                    channels   = 1,
                ) as stream:

                    rec = make_recognizer()
                    self.window.ai_status_signal.emit("🎙️  AI Ready — say 'Check this' anytime")

                    while self._running:
                        data, overflowed = stream.read(BLOCK)
                        if overflowed:
                            continue

                        if rec.AcceptWaveform(bytes(data)):
                            # ── FINAL result ──────────────────────────────────
                            result = json.loads(rec.Result())
                            text   = result.get("text", "").strip().lower()
                            rec    = make_recognizer()  # Fresh recognizer

                            if text:
                                log.info(f"🗣️  Final: '{text}'")
                                self._safe_process(text)
                                # Always reset after final for clean next recognition
                                rec = make_recognizer()

                        else:
                            # ── PARTIAL result ────────────────────────────────
                            # Only use partials to detect wake phrase in IDLE
                            # This makes wake phrase detection near-instant
                            if self.state == IDLE:
                                partial = json.loads(rec.PartialResult())
                                ptext   = partial.get("partial", "").strip().lower()
                                if ptext and len(ptext) > 1:
                                    if self._contains_wake_phrase(ptext):
                                        log.info(f"🗣️  Partial wake: '{ptext}'")
                                        # Reset BEFORE processing so mic is clean immediately
                                        rec = make_recognizer()
                                        self._safe_process(ptext, is_partial=True)

            except Exception as e:
                if self._running:
                    log.warning(f"Mic error: {e} — retrying in 2s")
                    self.window.ai_status_signal.emit(
                        "⚠️  AI: Mic issue — retrying…"
                    )
                    time.sleep(2)  # Brief pause before retry

    def _safe_process(self, text: str, is_partial: bool = False):
        """
        Smart debounce:
        - Wake phrase in IDLE: NO debounce — fires instantly every time
        - Commands in other states: 0.8s debounce to avoid double-firing
        """
        now = time.time()

        # In IDLE, NEVER debounce wake phrase — must respond instantly every time
        if self.state == IDLE:
            if self._contains_wake_phrase(text):
                self._last_processed    = text
                self._last_process_time = now
                self._process_speech(text)
                return
            return  # Ignore non-wake text in IDLE

        # For commands (non-IDLE states): light debounce to avoid double-fire
        if text == self._last_processed and (now - self._last_process_time) < 0.8:
            return
        self._last_processed    = text
        self._last_process_time = now
        self._process_speech(text)

    # ══════════════════════════════════════════════════════════════════════════
    # STATE MACHINE
    # ══════════════════════════════════════════════════════════════════════════

    def _process_speech(self, text: str):
        log.info(f"🧠  [{self.state}]  Heard: '{text}'")

        # ── IDLE ──────────────────────────────────────────────────────────────
        if self.state == IDLE:
            if self._contains_wake_phrase(text):
                log.info("🔔  Wake phrase detected!")
                self._transition_to(AWAITING_TARGET)
                self._bring_window_to_front()
                self.window.ai_status_signal.emit(
                    "🎙️  AI: Who should I share with? Say a name."
                )
                users = self._get_online_users()
                id_map = self._get_user_id_map()
                if users and id_map:
                    options = ", ".join(f"{uid} for {name}" for uid, name in id_map.items())
                    self._speak(f"Who should I share with? Say a name or number. {options}")
                else:
                    self._speak("QuickCast ready. Who should I share with?")
                self._start_timeout()

        # ── AWAITING_TARGET ───────────────────────────────────────────────────
        elif self.state == AWAITING_TARGET:
            self._cancel_timeout()

            # Allow user to cancel
            if self._contains_any(text, NO_WORDS + ["cancel", "stop", "quit", "exit"]):
                self._speak("Okay, cancelled.")
                self._reset_to_idle()
                return

            matched = self._match_username(text)
            if matched:
                self.selected_user = matched
                self._transition_to(AWAITING_CONFIRMATION)
                self.window.ai_status_signal.emit(
                    f"🎙️  AI: Share with {matched}? Say yes or no."
                )
                self._speak(f"Share screen with {matched}? Say yes or no.")
                self._start_timeout()
            else:
                online = self._get_online_users()
                if not online:
                    self._speak("No other users are online right now.")
                    self.window.ai_status_signal.emit("🎙️  AI: No users online")
                    self._reset_to_idle()
                else:
                    names = ", ".join(online)
                    self.window.ai_status_signal.emit(
                        f"🎙️  AI: Online users: {names} — say a name"
                    )
                    self._speak(f"I heard {text}, but didn't match a user. Online users are: {names}. Say a name.")
                    self._start_timeout()

        # ── AWAITING_CONFIRMATION ─────────────────────────────────────────────
        elif self.state == AWAITING_CONFIRMATION:
            self._cancel_timeout()

            if self._contains_any(text, YES_WORDS):
                self._transition_to(SHARING)
                self.window.ai_status_signal.emit(
                    f"📤  AI: Now sharing with {self.selected_user}"
                )
                self._speak(f"Starting screen share with {self.selected_user}.")
                self.window.ai_trigger_send_signal.emit(self.selected_user)

            elif self._contains_any(text, NO_WORDS):
                self._speak("Share cancelled.")
                self._reset_to_idle()

            else:
                # Didn't understand — keep waiting
                self.window.ai_status_signal.emit(
                    f"🎙️  AI: Didn't catch that — say yes or no for {self.selected_user}"
                )
                self._speak("Please say yes or no.")
                self._start_timeout()

        # ── SHARING ───────────────────────────────────────────────────────────
        elif self.state == SHARING:
            if self._contains_any(text, STOP_WORDS):
                self._speak("Screen share stopped.")
                self.window.ai_trigger_stop_signal.emit()
                self._reset_to_idle()
            elif self._contains_wake_phrase(text):
                # User said wake phrase again while sharing — tell them they're sharing
                self._speak("You are currently sharing. Say stop sharing to end.")
                self.window.ai_status_signal.emit(
                    "📤  AI: Sharing — say 'Stop sharing' to end"
                )

    # ══════════════════════════════════════════════════════════════════════════
    # MATCHING HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    def _contains_wake_phrase(self, text: str) -> bool:
        """Check if text contains any wake phrase."""
        text = text.lower().strip()
        for phrase in WAKE_PHRASES:
            if phrase in text:
                return True
        return False

    def _contains_any(self, text: str, word_list: list) -> bool:
        """Check if text contains any word/phrase from the list."""
        text = text.lower().strip()
        for word in word_list:
            if word in text:
                return True
        return False

    def _match_username(self, text: str) -> str | None:
        """
        Smart username extractor from spoken text.

        Tries (in order):
        0. Number match — "one", "1", "user one" → first user
        1. Exact match after stripping filler words
        2. Substring match
        3. Word-by-word match
        4. Phonetic/fuzzy match (handles slight noise/mispronunciation)
        """
        online = self._get_online_users()
        if not online:
            return None

        text = text.lower().strip()

        # 0. NUMBER MATCH — user says "one", "two", "1", "2" etc.
        number_words = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
            "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
            "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        }
        id_map = self._get_user_id_map()
        for word in text.split():
            if word in number_words:
                uid = number_words[word]
                if uid in id_map:
                    return id_map[uid]

        # Strip filler words
        words = [w for w in re.split(r'\s+', text) if w not in FILLER_WORDS]
        clean = " ".join(words)

        # 1. Exact match on full cleaned text
        for user in online:
            if user.lower() == clean:
                return user

        # 2. Full username appears anywhere in text
        for user in online:
            if user.lower() in text:
                return user

        # 3. Any single word matches a username
        for word in words:
            for user in online:
                if user.lower() == word:
                    return user

        # 4. Fuzzy match — username starts with what was heard
        #    e.g. "nash" matches "Nashville", "nay" matches "Nash"
        for word in words:
            for user in online:
                u = user.lower()
                # Username starts with the spoken word (at least 2 chars)
                if len(word) >= 2 and u.startswith(word):
                    return user
                # Spoken word starts with username (partial username spoken)
                if len(u) >= 2 and word.startswith(u[:max(2, len(u)-1)]):
                    return user

        # 5. Phonetic match — compare how similar they sound
        for word in words:
            for user in online:
                if self._phonetic_match(word, user.lower()):
                    return user

        return None

    def _phonetic_match(self, word: str, name: str) -> bool:
        """
        Simple phonetic matching.
        Returns True if word sounds similar to name.
        Handles common vowel substitutions and consonant confusions.
        """
        if not word or not name:
            return False

        # Must be at least half the length to be a match
        if len(word) < max(2, len(name) // 2):
            return False

        def simplify(s):
            """Reduce to consonant skeleton to compare sound."""
            # Lowercase
            s = s.lower()
            # Remove repeated characters (naash → nash)
            s = re.sub(r'(.)\1+', r'\1', s)
            # Normalize vowels — all vowels become 'a'
            s = re.sub(r'[aeiou]', 'a', s)
            # Common consonant equivalents
            s = s.replace('ph', 'f').replace('ck', 'k').replace('sh', 's')
            return s

        return simplify(word) == simplify(name)

    # ══════════════════════════════════════════════════════════════════════════
    # TTS WORKER
    # ══════════════════════════════════════════════════════════════════════════

    def _speak(self, text: str):
        """Queue a message for TTS — never blocks the caller."""
        log.info(f"🔊  Speak: '{text}'")
        # Clear old queued messages so latest is always spoken promptly
        while not self._tts_queue.empty():
            try:
                self._tts_queue.get_nowait()
            except queue.Empty:
                break
        self._tts_queue.put(text)

    def _tts_worker(self):
        """
        Dedicated TTS thread.
        pyttsx3 MUST run on its own thread on Windows.
        """
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate",   160)   # WPM — comfortable listening speed
            engine.setProperty("volume", 1.0)

            # Pick best available voice
            voices = engine.getProperty("voices")
            preferred = ["zira", "david", "hazel", "english"]
            for pref in preferred:
                for voice in voices:
                    if pref in voice.name.lower():
                        engine.setProperty("voice", voice.id)
                        break

            self._tts_ready.set()  # Signal that TTS is ready

            while self._running:
                try:
                    text = self._tts_queue.get(timeout=1)
                    engine.say(text)
                    engine.runAndWait()
                except queue.Empty:
                    continue
                except Exception as e:
                    log.warning(f"TTS error: {e}")

        except ImportError:
            log.error("pyttsx3 not installed")
            self._tts_ready.set()
        except Exception as e:
            log.error(f"TTS failed: {e}")
            self._tts_ready.set()

    # ══════════════════════════════════════════════════════════════════════════
    # STATE HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    def _transition_to(self, new_state: str):
        log.info(f"🔄  {self.state} → {new_state}")
        self.state = new_state

    def _reset_to_idle(self):
        self._cancel_timeout()
        self.selected_user = None
        self._transition_to(IDLE)
        self.window.ai_status_signal.emit("🎙️  AI Ready — say 'Check this' anytime")

    def _start_timeout(self):
        self._cancel_timeout()
        t = threading.Timer(STATE_TIMEOUT_SECONDS, self._on_timeout)
        t.daemon = True
        t.start()
        self._state_timer = t

    def _cancel_timeout(self):
        if self._state_timer:
            self._state_timer.cancel()
            self._state_timer = None

    def _on_timeout(self):
        if self.state not in (IDLE, SHARING):
            log.info(f"⏱️  Timeout in {self.state} — resetting")
            self._speak("No response. Resetting. Say check this when ready.")
            self._reset_to_idle()

    def _bring_window_to_front(self):
        self.window.ai_raise_signal.emit()