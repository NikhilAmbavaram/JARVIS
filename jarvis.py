# jarvis.py — push-to-talk voice version (Phase 2)

from brain import Brain
from voice import listen, speak

def main():
    jarvis = Brain()
    speak("Jarvis online. How may I help, sir?")
    while True:
        cmd = input("\n[Enter] to talk, or type 'quit': ")
        if cmd.strip().lower() in {"quit", "exit"}:
            break
        user_text = listen()
        if not user_text:
            speak("I didn't catch that.")
            continue
        reply = jarvis.think(user_text)
        speak(reply)

if __name__ == "__main__":
    main()