# chats.py — saved chats, like the Claude app's sidebar
#
# Everything lives in jarvis.db (SQLite, next to this file, gitignored):
#   chats      one row per chat: title, model, "think harder", pinned
#   messages   what the chat window shows (your messages and Jarvis's replies, with links, files, visuals...)
#   histories  what Claude sees for each chat: the full API conversation, tool calls included
#   search     a full-text index of messages, so you (and Jarvis) can search old chats
#
# Memory facts (memory.json) are shared by every chat; this file only keeps conversations apart.

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_FILE = Path(__file__).with_name("jarvis.db")


class ChatStore:
    def __init__(self, path=DB_FILE, default_model="claude-opus-5"):
        self.default_model = default_model
        self.lock = threading.RLock()   # one connection shared by the server's threads, one writer at a time
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New chat',
                model TEXT NOT NULL,
                think INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                body TEXT NOT NULL,
                text TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (chat_id, created);
            CREATE TABLE IF NOT EXISTS histories (
                chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                messages TEXT NOT NULL
            );
        """)
        try:   # full-text search; every normal Python build has FTS5, but fall back to LIKE if not
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5("
                            "message_id UNINDEXED, chat_id UNINDEXED, text, tokenize = 'porter unicode61')")   # porter: "pointers" finds "pointer"
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    # --- chats ---

    def create_chat(self, title="New chat", model=None) -> dict:
        now = time.time()
        chat = {"id": uuid.uuid4().hex[:12], "title": title, "model": model or self.default_model,
                "think": False, "pinned": False, "created": now, "updated": now}
        with self.lock:
            self.db.execute("INSERT INTO chats (id, title, model, think, pinned, created, updated) VALUES (?, ?, ?, 0, 0, ?, ?)",
                            (chat["id"], title, chat["model"], now, now))
            self.db.commit()
        return chat

    def get_chat(self, chat_id) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return self._chat(row) if row else None

    def list_chats(self) -> list:
        with self.lock:
            rows = self.db.execute("SELECT * FROM chats ORDER BY pinned DESC, updated DESC").fetchall()
        return [self._chat(row) for row in rows]

    def update_chat(self, chat_id, **changes) -> dict | None:
        allowed = {key: value for key, value in changes.items() if key in ("title", "model", "think", "pinned")}
        if allowed:
            columns = ", ".join(f"{key} = ?" for key in allowed)
            values = [int(value) if isinstance(value, bool) else value for value in allowed.values()]
            with self.lock:
                self.db.execute(f"UPDATE chats SET {columns} WHERE id = ?", (*values, chat_id))
                self.db.commit()
        return self.get_chat(chat_id)

    def touch(self, chat_id):
        with self.lock:
            self.db.execute("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))
            self.db.commit()

    def delete_chat(self, chat_id):
        with self.lock:
            if self.fts:
                self.db.execute("DELETE FROM search WHERE chat_id = ?", (chat_id,))
            self.db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))   # messages and history go with it
            self.db.commit()

    @staticmethod
    def _chat(row) -> dict:
        return {"id": row["id"], "title": row["title"], "model": row["model"], "think": bool(row["think"]),
                "pinned": bool(row["pinned"]), "created": row["created"], "updated": row["updated"]}

    # --- what the chat window shows ---

    def add_message(self, chat_id, message: dict):
        """message needs id, role, text and time; everything else is kept as-is for the page."""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO messages (id, chat_id, role, body, text, created) VALUES (?, ?, ?, ?, ?, ?)",
                            (message["id"], chat_id, message["role"], json.dumps(message), message.get("text", ""),
                             message.get("time", time.time())))
            if self.fts:
                self.db.execute("DELETE FROM search WHERE message_id = ?", (message["id"],))
                self.db.execute("INSERT INTO search (message_id, chat_id, text) VALUES (?, ?, ?)",
                                (message["id"], chat_id, message.get("text", "")))
            self.db.execute("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))
            self.db.commit()

    def messages(self, chat_id) -> list:
        with self.lock:
            rows = self.db.execute("SELECT body FROM messages WHERE chat_id = ? ORDER BY created, rowid", (chat_id,)).fetchall()
        return [{**json.loads(row["body"]), "chat_id": chat_id} for row in rows]   # the page needs to know whose message it is

    def delete_messages_from(self, chat_id, message_id) -> dict | None:
        """Remove a message and everything after it (for retry and edit). Returns the removed message."""
        with self.lock:
            row = self.db.execute("SELECT rowid, body, created FROM messages WHERE id = ? AND chat_id = ?",
                                  (message_id, chat_id)).fetchone()
            if not row:
                return None
            doomed = [r["id"] for r in self.db.execute(
                "SELECT id FROM messages WHERE chat_id = ? AND (created > ? OR (created = ? AND rowid >= ?))",
                (chat_id, row["created"], row["created"], row["rowid"])).fetchall()]
            for doomed_id in doomed:
                self.db.execute("DELETE FROM messages WHERE id = ?", (doomed_id,))
                if self.fts:
                    self.db.execute("DELETE FROM search WHERE message_id = ?", (doomed_id,))
            self.db.commit()
        return json.loads(row["body"])

    # --- what Claude sees ---

    def history(self, chat_id) -> list:
        with self.lock:
            row = self.db.execute("SELECT messages FROM histories WHERE chat_id = ?", (chat_id,)).fetchone()
        return json.loads(row["messages"]) if row else []

    def save_history(self, chat_id, messages: list):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO histories (chat_id, messages) VALUES (?, ?)",
                            (chat_id, json.dumps(messages)))
            self.db.commit()

    # --- search ---

    def search(self, query: str, limit=8, exclude_chat=None) -> list:
        """Messages matching the words in query, best matches first."""
        words = re.findall(r"\w+", query.lower())
        if not words:
            return []
        with self.lock:
            if self.fts:
                match = " ".join(f'"{word}"' for word in words)   # quoted, so FTS syntax in the query can't break anything
                rows = self.db.execute(
                    """SELECT m.id, m.chat_id, m.role, m.created, c.title,
                              snippet(search, 2, '«', '»', ' … ', 18) AS snippet
                       FROM search JOIN messages m ON m.id = search.message_id JOIN chats c ON c.id = m.chat_id
                       WHERE search MATCH ? AND (? IS NULL OR m.chat_id != ?)
                       ORDER BY rank LIMIT ?""",
                    (match, exclude_chat, exclude_chat, limit)).fetchall()
            else:
                like = " AND ".join("lower(m.text) LIKE ?" for _ in words)
                rows = self.db.execute(
                    f"""SELECT m.id, m.chat_id, m.role, m.created, c.title, substr(m.text, 1, 200) AS snippet
                        FROM messages m JOIN chats c ON c.id = m.chat_id
                        WHERE {like} AND (? IS NULL OR m.chat_id != ?)
                        ORDER BY m.created DESC LIMIT ?""",
                    (*[f"%{word}%" for word in words], exclude_chat, exclude_chat, limit)).fetchall()
        return [{"message_id": r["id"], "chat_id": r["chat_id"], "chat_title": r["title"], "role": r["role"],
                 "time": r["created"], "snippet": r["snippet"]} for r in rows]

    def search_chats(self, query: str, limit=30) -> list:
        """Chats whose title or messages match, for the sidebar's search box."""
        words = re.findall(r"\w+", query.lower())
        if not words:
            return self.list_chats()
        found, seen = [], set()
        with self.lock:
            title_like = " AND ".join("lower(title) LIKE ?" for _ in words)
            for row in self.db.execute(f"SELECT * FROM chats WHERE {title_like} ORDER BY updated DESC",
                                       [f"%{word}%" for word in words]).fetchall():
                seen.add(row["id"])
                found.append(self._chat(row))
        for hit in self.search(query, limit=limit):
            if hit["chat_id"] not in seen:
                seen.add(hit["chat_id"])
                chat = self.get_chat(hit["chat_id"])
                if chat:
                    found.append({**chat, "snippet": hit["snippet"]})
        return found[:limit]
