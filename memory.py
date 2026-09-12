# memory.py — facts Jarvis remembers between sessions (Phase 5). Every chat in the dashboard shares these.

import json
from pathlib import Path

# Stored next to this file, so it's found no matter which folder you run jarvis.py from
MEMORY_FILE = Path(__file__).with_name("memory.json")


def load_memory() -> dict:
    """Read saved facts from disk, or start empty if there's no file yet."""
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    return {"facts": []}


def _save(data: dict):
    MEMORY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def remember(fact: str) -> str:
    """Save one fact permanently (exact duplicates are skipped)."""
    fact = fact.strip()
    data = load_memory()
    if fact and fact not in data["facts"]:
        data["facts"].append(fact)
        _save(data)
    return f"Noted: {fact}"


def forget(fact: str) -> str:
    """Remove a fact: the one with this exact wording, or the only one containing it."""
    data = load_memory()
    wanted = fact.strip().lower()
    matches = [f for f in data["facts"] if f.lower() == wanted] or [f for f in data["facts"] if wanted and wanted in f.lower()]
    if not matches:
        return f"Nothing in memory matches '{fact}'."
    if len(matches) > 1:
        return "Several facts match: " + "; ".join(matches) + ". Use the exact wording of the one to forget."
    data["facts"].remove(matches[0])
    _save(data)
    return f"Forgot: {matches[0]}"


def facts() -> list:
    return load_memory()["facts"]


def delete_fact(index: int) -> bool:
    """The dashboard's memory list: delete by position."""
    data = load_memory()
    if not 0 <= index < len(data["facts"]):
        return False
    del data["facts"][index]
    _save(data)
    return True


def memory_as_text() -> str:
    """All facts as a bulleted list, ready to drop into the system prompt."""
    facts = load_memory()["facts"]
    if not facts:
        return "(nothing remembered yet)"
    return "\n".join(f"- {f}" for f in facts)