# memory.py — facts Jarvis remembers between sessions (Phase 5)

import json
from pathlib import Path

# Stored next to this file, so it's found no matter which folder you run jarvis.py from
MEMORY_FILE = Path(__file__).with_name("memory.json")


def load_memory() -> dict:
    """Read saved facts from disk, or start empty if there's no file yet."""
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    return {"facts": []}


def remember(fact: str) -> str:
    """Save one fact permanently (exact duplicates are skipped)."""
    fact = fact.strip()
    data = load_memory()
    if fact and fact not in data["facts"]:
        data["facts"].append(fact)
        MEMORY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"Noted: {fact}"


def memory_as_text() -> str:
    """All facts as a bulleted list, ready to drop into the system prompt."""
    facts = load_memory()["facts"]
    if not facts:
        return "(nothing remembered yet)"
    return "\n".join(f"- {f}" for f in facts)