# jarvis.py — hands-free with conversation mode (Phase 3+)
#
#   ASLEEP:  wait for "Hey Jarvis" -> "Sir?" -> wake up
#   AWAKE:   listen -> think -> speak -> listen again (no wake word needed)
#            say a sleep phrase, or stay quiet for a while -> back to ASLEEP

import random
import string
import time

from brain import Brain, for_speech
from voice import listen, speak
from wake import wait_for_wake_word

# --- Tuning knobs ---
SLEEP_PHRASES = ("go offline", "stand down")   # say one of these to put Jarvis to sleep
SKIP_PHRASES = ("never mind", "nevermind")     # "ignore that", keep listening
IDLE_LIMIT = 2            # empty listens in a row before he dozes off (~8 s each)
POST_SPEAK_PAUSE = 0.3    # seconds to wait after speaking so he doesn't hear himself

WAKE_REPLIES = ("Sir?", "Sir?", "Yes, sir?")   # "Sir?" twice = picked most of the time
SLEEP_REPLIES = ("Going offline, sir.", "As you wish, sir.")

# Speech models occasionally "hear" these in background noise. Treat them as silence.
PHANTOM_TRANSCRIPTS = {"you", "thank you", "thanks for watching"}


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces: 'Go offline.' -> 'go offline'."""
    text = text.lower().translate(str.maketrans("", "", string.punctuation))
    return " ".join(text.split())


def said_any(clean: str, phrases) -> bool:
    """True if any phrase appears as whole words. Padding with spaces stops
    'stand down' from matching inside 'stand downstairs'."""
    padded = f" {clean} "
    return any(f" {p} " in padded for p in phrases)


def conversation(jarvis: Brain):
    """Stay awake and keep talking until a sleep phrase or a long silence."""
    idle = 0
    while True:
        time.sleep(POST_SPEAK_PAUSE)
        text = listen()
        clean = normalize(text)

        # 1. Nobody said anything (or it was just noise)
        if not clean or clean in PHANTOM_TRANSCRIPTS:
            idle += 1
            if idle >= IDLE_LIMIT:
                speak("Standing by, sir.")
                return                      # back to ASLEEP
            continue
        idle = 0

        # 2. Dismissed
        if said_any(clean, SLEEP_PHRASES):
            speak(random.choice(SLEEP_REPLIES))
            return                          # back to ASLEEP

        # 3. Changed your mind
        if clean in SKIP_PHRASES:
            continue

        # 4. A real request
        try:
            reply = jarvis.think(text)
        except Exception as e:              # API hiccup, no internet, etc. — don't crash the loop
            print(f"⚠️ {e}")
            reply = "Something went wrong on my end, sir."
        speak(for_speech(reply) or "It's on screen, sir.")   # addresses are printed, not read out


def main():
    jarvis = Brain()
    speak("Jarvis online.")
    try:
        while True:
            wait_for_wake_word()                    # ASLEEP: local and free
            speak(random.choice(WAKE_REPLIES))
            conversation(jarvis)                    # AWAKE until dismissed
    except KeyboardInterrupt:
        print("\nJarvis shutting down.")


if __name__ == "__main__":
    main()