# jarvis.py — text chat version (Phase 1)

from brain import Brain

def main():
    jarvis = Brain()
    print("Jarvis is online. Type 'quit' to exit.\n")
    while True:
        you = input("You: ")
        if you.strip().lower() in {"quit", "exit"}:
            print("Jarvis: Goodbye.")
            break
        reply = jarvis.think(you)
        print(f"Jarvis: {reply}\n")

if __name__ == "__main__":
    main()