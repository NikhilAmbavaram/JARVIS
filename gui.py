# gui.py — Jarvis with a dashboard: the voice loop, saved chats and a control panel in its own window
#
#   python .\gui.py              opens the dashboard in an Edge app window
#   python .\gui.py --browser    opens it in a normal browser tab instead
#   python .\gui.py --no-voice   chat only: no microphone or speakers
#
# How it fits together: this file runs a small web server on 127.0.0.1, so only this PC can reach it.
# The page in ui/ connects over a WebSocket. Python pushes events to the page (state, chats, replies as
# they're written, stats, weather, Spotify, approval requests) and the page sends commands back (messages,
# buttons, approvals, settings). The voice loop from jarvis.py runs in a background thread. Chats are saved
# in jarvis.db (chats.py); memory.json facts are shared by all of them.

import argparse
import asyncio
import base64
import io
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Queue

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
import dashboard
import memory
import tools
from brain import Brain, belongs_on_screen, for_speech
from chats import ChatStore

HERE = Path(__file__).parent
UI_DIR = HERE / "ui"
SETTINGS_FILE = HERE / "settings.json"
DEFAULT_PORT = 8765

MODELS = [   # the chat model picker (prices per million input/output tokens, checked Sept 2026)
    {"id": "claude-opus-5", "label": "Opus 5", "note": "Best for coding and hard problems · $5 / $25"},
    {"id": "claude-sonnet-5", "label": "Sonnet 5", "note": "Strong and cheaper · $2 / $10"},
    {"id": "claude-haiku-4-5", "label": "Haiku 4.5", "note": "Fastest and cheapest · $1 / $5"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "note": "Most capable, most expensive · $10 / $50"},
]
MODEL_IDS = {model["id"] for model in MODELS}

DEFAULT_SETTINGS = {
    "speak_typed": False,             # also say replies to typed messages out loud
    "quiet_answers": True,            # a real answer goes in the chat to be read, not read aloud
    "show_tools": True,               # show which tools Jarvis used under his replies
    "weather_city": "Raleigh, NC",
    "units": "imperial",              # "imperial" (°F, mph) or "metric" (°C, km/h)
    "new_chat_model": config.CHAT_MODEL,
}

# What Jarvis says out loud when the answer itself stays on screen
IN_THE_CHAT = ["It's in the chat, sir.", "The answer's in the chat, sir.", "Written out in the chat, sir.",
               "I've put it in the chat, sir."]

LEVEL_GAIN = {"wake": 30.0, "listen": 14.0, "speak": 5.0}   # evens out loudness so the orb moves the same for each
STATS_EVERY, WEATHER_EVERY = 2, 600                          # seconds
SPOTIFY_EVERY, SPOTIFY_OFFLINE_EVERY = 5, 30
CLOSE_GRACE = 10          # seconds to wait for the page to come back (a reload) before quitting when its window closes
APPROVAL_TIMEOUT = 600    # an unanswered approval counts as "no" after this many seconds
MAX_ATTACHMENT = 30_000_000

YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go ahead", "do it", "run it", "approve", "approved",
             "allow", "allow it", "affirmative", "yes please", "please do", "go for it", "yes sir", "proceed"}
NO_WORDS = {"no", "nope", "nah", "deny", "denied", "cancel", "stop", "dont", "do not", "negative", "no thanks",
            "never mind", "nevermind", "dont do it", "no sir"}


# ---------- Settings ----------

def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({key: value for key, value in saved.items() if key in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass   # no settings file yet, or a broken one: use the defaults
    if settings["new_chat_model"] not in MODEL_IDS:
        settings["new_chat_model"] = config.CHAT_MODEL
    return settings


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ---------- Events to the page ----------

class Hub:
    """Delivers events to every open page. publish() is safe to call from any thread."""

    KEEP_LATEST = {"state", "stats", "weather", "spotify", "settings", "counters"}

    def __init__(self):
        self.loop = None       # the web server's event loop, set when the server starts
        self.queues = set()    # one queue per open page
        self.latest = {}       # newest event of each KEEP_LATEST type, replayed when a page connects

    def publish(self, event: dict):
        if self.loop is None:
            self._deliver(event)
        else:
            self.loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: dict):   # runs on the event loop
        if event["type"] in self.KEEP_LATEST:
            self.latest[event["type"]] = event
        for queue in list(self.queues):
            if event["type"] == "level" and queue.qsize() > 20:
                continue   # a busy page can skip a few animation frames
            queue.put_nowait(event)


# ---------- Jarvis himself ----------

class Jarvis:
    """The chats, the voice loop and everything the dashboard shows."""

    def __init__(self, hub: Hub, settings: dict, voice_enabled: bool, quit_with_window: bool, store: ChatStore = None):
        self.hub = hub
        self.settings = settings
        self.voice_enabled = voice_enabled
        self.quit_with_window = quit_with_window
        self.server = None

        self.store = store or ChatStore(default_model=settings["new_chat_model"])
        self.brains = {}                        # chat id -> Brain, loaded from the store when first needed
        self.client = Brain().client            # one API client shared by every chat
        self.active_chat = None                 # the chat open in the dashboard; voice goes here too
        self.brain_lock = threading.Lock()      # one reply at a time
        self.speech_lock = threading.Lock()     # one voice at a time
        self.stop_reply = threading.Event()     # the Stop button
        self.approvals = {}                     # approval id -> pending request
        self.voice_turn = False                 # the reply in progress was asked by voice
        self.generating_chat = None             # the chat a reply is being written for
        self.stream = None                      # the reply being written, replayed to a page that opens mid-reply

        self.voice_state = "asleep" if voice_enabled else "voice-off"
        self.detail = ""
        self.thinking = 0
        self.speaking = False
        self.mic_on = voice_enabled

        self.wake_request = threading.Event()
        self.sleep_request = threading.Event()
        self.stop_speech = threading.Event()
        self.shutdown = threading.Event()
        self.refresh = {name: threading.Event() for name in ("stats", "weather", "spotify")}

        self.typed = Queue()
        self.pages = 0
        self.started = time.time()
        self.commands = 0
        self.conversations = 0
        self._last_level = 0.0

        tools.APPROVER = self.request_approval
        tools.CHAT_SEARCH = self.store.search

    # --- starting and stopping ---

    def start(self):
        chats = self.store.list_chats()
        self.active_chat = chats[0]["id"] if chats else self.store.create_chat(model=self.settings["new_chat_model"])["id"]
        for job in (self._stats_feed, self._weather_feed, self._spotify_feed, self._typed_worker):
            threading.Thread(target=job, daemon=True, name=job.__name__).start()
        if self.voice_enabled:
            threading.Thread(target=self._voice_loop, daemon=True, name="voice").start()
        self.hub.publish({"type": "settings", **self.settings})
        self._publish_counters()
        self._publish_state()

    def stop(self):
        self.shutdown.set()
        self.typed.put(None)
        self.stop_everything()

    def page_opened(self):
        self.pages += 1

    def page_closed(self):
        self.pages -= 1
        if self.pages == 0 and self.quit_with_window:
            threading.Timer(CLOSE_GRACE, self._quit_if_still_closed).start()

    def _quit_if_still_closed(self):
        if self.pages == 0 and self.server is not None and not self.shutdown.is_set():
            print("Dashboard closed, so Jarvis is shutting down.")
            self.server.should_exit = True

    def snapshot(self) -> list:
        """Everything a page needs when it connects or reloads."""
        events = list(self.hub.latest.values())
        events.append({"type": "models", "models": MODELS})
        events.append(self._chats_event())
        events.append(self._chat_open_event(self.active_chat))
        events.append({"type": "memory", "facts": memory.facts()})
        stream = self.stream   # a reply already being written: the page picks it up where it is
        if stream:
            events.append({"type": "message_start", "chat_id": stream["chat_id"], "id": stream["id"], "model": stream["model"]})
            if stream["text"] or stream["thinking"]:
                events.append({"type": "delta", "chat_id": stream["chat_id"], "id": stream["id"],
                               "text": stream["text"], "thinking": stream["thinking"]})
        for approval in self.approvals.values():
            events.append(self._approval_event(approval))
        return events

    # --- what the orb and status line show ---

    def _publish_state(self):
        if self.approvals:
            state = "approval"
        else:
            state = "thinking" if self.thinking else "speaking" if self.speaking else self.voice_state
        self.hub.publish({"type": "state", "state": state, "mic_on": self.mic_on, "voice": self.voice_enabled,
                          "detail": self.detail, "generating": self.thinking > 0})

    def _set_voice_state(self, state: str, detail: str = ""):
        self.voice_state, self.detail = state, detail
        self._publish_state()

    def _publish_counters(self):
        self.hub.publish({"type": "counters", "started": self.started,
                          "commands": self.commands, "conversations": self.conversations})

    def _level(self, raw: float, source: str):
        now = time.monotonic()
        if now - self._last_level < 0.066:   # about 15 updates a second is plenty for the animation
            return
        self._last_level = now
        self.hub.publish({"type": "level", "value": round(min(1.0, raw * LEVEL_GAIN[source]), 3)})

    # --- chats ---

    def _brain(self, chat_id: str) -> Brain:
        brain = self.brains.get(chat_id)
        if brain is None:
            brain = Brain(messages=self.store.history(chat_id))
            brain.client, brain.chat_id, brain.stop = self.client, chat_id, self.stop_reply
            self.brains[chat_id] = brain
        return brain

    def _chats_event(self) -> dict:
        return {"type": "chats", "chats": self.store.list_chats(), "active": self.active_chat}

    def _chat_open_event(self, chat_id) -> dict:
        return {"type": "chat_open", "chat": self.store.get_chat(chat_id), "messages": self.store.messages(chat_id)}

    def open_chat(self, chat_id: str):
        if self.store.get_chat(chat_id):
            self.active_chat = chat_id
            self.hub.publish(self._chat_open_event(chat_id))
            self.hub.publish(self._chats_event())

    def new_chat(self):
        """Start a fresh chat, unless the open one is still empty."""
        current = self.store.get_chat(self.active_chat) if self.active_chat else None
        if current and not self.store.messages(current["id"]):
            self.store.update_chat(current["id"], model=self.settings["new_chat_model"], think=False)
            chat_id = current["id"]
        else:
            chat_id = self.store.create_chat(model=self.settings["new_chat_model"])["id"]
        self.open_chat(chat_id)

    def rename_chat(self, chat_id: str, title: str):
        title = " ".join(str(title).split())[:80]
        if title:
            self.store.update_chat(chat_id, title=title)
            self.hub.publish(self._chats_event())

    def pin_chat(self, chat_id: str, pinned: bool):
        self.store.update_chat(chat_id, pinned=bool(pinned))
        self.hub.publish(self._chats_event())

    def chat_settings(self, chat_id: str, model=None, think=None):
        changes = {}
        if model in MODEL_IDS:
            changes["model"] = model
        if isinstance(think, bool):
            changes["think"] = think
        if changes and self.store.get_chat(chat_id):
            chat = self.store.update_chat(chat_id, **changes)
            self.hub.publish({"type": "chat_updated", "chat": chat})
            self.hub.publish(self._chats_event())

    def delete_chat(self, chat_id: str):
        if self.generating_chat == chat_id:
            self.stop_everything()
        with self.brain_lock:
            self.store.delete_chat(chat_id)
            self.brains.pop(chat_id, None)
        if chat_id == self.active_chat:
            remaining = self.store.list_chats()
            self.active_chat = remaining[0]["id"] if remaining else self.store.create_chat(model=self.settings["new_chat_model"])["id"]
            self.hub.publish(self._chat_open_event(self.active_chat))
        self.hub.publish(self._chats_event())

    def search_chats(self, query: str):
        results = self.store.search_chats(query) if str(query).strip() else self.store.list_chats()
        self.hub.publish({"type": "chat_search", "query": query, "results": results})

    # --- talking to Claude ---

    def _attachments(self, raw_items, chat_id):
        """Uploaded files -> (what Claude gets, what the chat shows, where they were saved)."""
        for_claude, shown, saved = [], [], []
        folder = tools.WORKSPACE / "uploads" / chat_id
        for item in (raw_items or [])[:10]:
            try:
                name = Path(str(item.get("name") or "file")).name[:120] or "file"
                data = base64.b64decode(item.get("data") or "", validate=True)
            except (ValueError, TypeError, AttributeError):
                continue
            if not data or len(data) > MAX_ATTACHMENT:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            path.write_bytes(data)   # saved, so Jarvis can also work on the file with his tools
            saved.append(str(path))
            mime = str(item.get("type") or "")
            if mime.startswith("image/") and not mime.endswith("svg+xml"):
                try:
                    from PIL import Image
                    with Image.open(io.BytesIO(data)) as picture:
                        picture = picture.convert("RGB")
                        big, small = tools._shrink(picture, tools.SHOT_MAX_SIDE, tools.SHOT_MAX_PIXELS), tools._shrink(picture, 480)
                        for_claude.append({"kind": "image", "name": name, "media_type": "image/jpeg", "data": tools._jpeg_base64(big, 88)})
                        shown.append({"name": name, "kind": "image", "image": "data:image/jpeg;base64," + tools._jpeg_base64(small, 80)})
                except Exception:
                    shown.append({"name": name, "kind": "file", "size": len(data)})
            elif mime == "application/pdf" or name.lower().endswith(".pdf"):
                for_claude.append({"kind": "pdf", "name": name, "data": base64.b64encode(data).decode("ascii")})
                shown.append({"name": name, "kind": "pdf", "size": len(data)})
            else:
                if b"\x00" in data[:8000]:
                    shown.append({"name": name, "kind": "file", "size": len(data)})
                    continue   # not text: Claude can still reach it through the saved copy
                text = data.decode("utf-8", errors="replace")
                if len(text) > 400_000:
                    text = text[:400_000] + "\n… (cut: the file is too long to attach whole; use read_file on the saved copy)"
                for_claude.append({"kind": "text", "name": name, "text": text})
                shown.append({"name": name, "kind": "text", "size": len(data)})
        return for_claude, shown, saved

    def ask(self, text: str, source: str, chat_id: str = None, attachments=None, reuse=None) -> str:
        """Send one message to Claude in a chat, streaming the reply to the page. Returns what Jarvis would say.
        reuse: attachments already in Claude's format (retry and edit pass the originals back in)."""
        chat_id = chat_id or self.active_chat
        chat = self.store.get_chat(chat_id)
        if chat is None:
            return ""
        brain = self._brain(chat_id)
        for_claude, shown, saved = self._attachments(attachments, chat_id)
        for_claude = reuse["for_claude"] if reuse else for_claude
        shown = reuse["shown"] if reuse else shown
        saved = reuse["saved"] if reuse else saved
        prompt = text
        if saved:
            prompt = (text or "Take a look at the attached file.") + "\n\n(The attachments are also saved at: " + "; ".join(saved) + ")"

        user_message = {"id": uuid.uuid4().hex, "chat_id": chat_id, "role": "user", "text": text, "source": source,
                        "time": time.time(), "attachments": shown, "saved": saved, "history_index": len(brain.messages)}
        self.store.add_message(chat_id, user_message)
        self.hub.publish({"type": "message", **user_message})
        self.commands += 1
        self._publish_counters()

        reply_id = uuid.uuid4().hex
        model = config.BRAIN_MODEL if source == "voice" else chat["model"]
        error = None
        with self.brain_lock:
            self.stop_reply.clear()
            self.thinking += 1
            self.voice_turn = source == "voice"
            self.generating_chat = chat_id
            self.stream = {"chat_id": chat_id, "id": reply_id, "model": model, "text": "", "thinking": ""}
            self._publish_state()
            self.hub.publish({"type": "message_start", "chat_id": chat_id, "id": reply_id, "model": model})
            brain.on_event = lambda event: self._brain_event(chat_id, reply_id, event)
            try:
                reply = brain.think(prompt, attachments=for_claude, model=model, stream=True,
                                    think_harder=chat["think"] and source != "voice", spoken=source == "voice")
            except Exception as e:   # API hiccup, no internet, bad key...
                print(f"⚠️ {e}")
                error = str(e)[:500]
                reply = "Something went wrong on my end, sir."
                brain._strip_turn_thinking()
                if brain.messages and brain.messages[-1]["role"] == "user":   # keep the history valid for next time
                    brain.messages.append({"role": "assistant", "content": f"(That reply failed: {error})"})
                brain.last_text = reply
            finally:
                self.thinking -= 1
                self.voice_turn = False
                self.generating_chat = None
                self.stream = None
            self.store.save_history(chat_id, brain.messages)
            said = self._what_to_say(brain, reply)
            reply_message = {
                "id": reply_id, "chat_id": chat_id, "role": "jarvis", "source": source, "time": time.time(),
                "text": brain.last_text or reply,   # always the full written reply; `spoken` is what was read out
                "spoken": said if source == "voice" or self.settings["speak_typed"] else "",
                "links": brain.last_links, "screenshots": brain.last_screenshots, "visuals": brain.last_visuals,
                "files": brain.last_files, "tools": list(dict.fromkeys(brain.last_tools)), "thinking": brain.last_thinking,
                "model": model, "error": error, "stopped": brain.stopped,
            }
            self.store.add_message(chat_id, reply_message)
        self.hub.publish({"type": "message", **reply_message})
        self._publish_state()
        if {"remember", "forget"} & set(brain.last_tools):
            self.hub.publish({"type": "memory", "facts": memory.facts()})
        if chat["title"] == "New chat" and not error:
            self._title_later(chat_id, text or "(attachment)", brain.last_text)
        else:
            self.hub.publish(self._chats_event())
        return said

    def _what_to_say(self, brain: Brain, reply: str) -> str:
        """What actually gets read aloud. A real answer — long, or full of code, maths, steps or a table —
        stays in the chat window to be read, and Jarvis says one line instead. Nikhil asked for this
        (Sept 12) after telling Jarvis five times in memory and getting nowhere: the old code spoke
        every reply no matter what, so no instruction could have worked."""
        if brain.last_spoken:                                   # Claude called answer_in_chat itself
            return brain.last_spoken
        if self.settings["quiet_answers"] and belongs_on_screen(reply):
            return random.choice(IN_THE_CHAT)
        return for_speech(reply) or ("It's in the chat, sir." if brain.last_text else "Done, sir.")

    def _brain_event(self, chat_id: str, reply_id: str, event: dict):
        kind = event.get("type")
        base = {"chat_id": chat_id, "id": reply_id}
        stream = self.stream
        if stream and stream["id"] == reply_id:   # kept so a page opened mid-reply can catch up
            if kind == "text":
                stream["text"] += event["delta"]
            elif kind == "thinking":
                stream["thinking"] += event["delta"]
            elif kind == "round":
                stream["text"] += "\n\n"
        if kind == "text":
            self.hub.publish({"type": "delta", **base, "text": event["delta"]})
        elif kind == "thinking":
            self.hub.publish({"type": "delta", **base, "thinking": event["delta"]})
        elif kind == "round":
            self.hub.publish({"type": "delta", **base, "text": "\n\n"})
        elif kind in ("tool", "tool_start"):
            self.hub.publish({"type": "activity", **base, "tool": event["name"]})
        elif kind == "tool_done":
            self.hub.publish({"type": "activity", **base, "tool": event["name"], "done": True, "summary": event.get("summary", "")})

    def _title_later(self, chat_id: str, user_text: str, reply_text: str):
        def work():
            try:
                title = self._brain(chat_id).make_title(user_text, reply_text)
            except Exception:
                title = " ".join(user_text.split()[:6])[:60] or "New chat"
            self.store.update_chat(chat_id, title=title)
            self.hub.publish(self._chats_event())
        threading.Thread(target=work, daemon=True).start()

    def _typed_worker(self):
        """Handles typed messages (and retries and edits) one at a time, so the page never waits on Claude."""
        while True:
            job = self.typed.get()
            if job is None:
                return
            try:
                said = self.ask(**job)
                if self.settings["speak_typed"] and said:
                    self.speak(said)
            except Exception as e:
                print(f"⚠️ {e}")

    def rerun(self, chat_id: str, message_id: str = None, new_text: str = None):
        """Retry the last reply, or edit one of your messages and continue from there."""
        messages = self.store.messages(chat_id)
        users = [m for m in messages if m["role"] == "user"]
        target = next((m for m in users if m["id"] == message_id), None) if message_id else (users[-1] if users else None)
        if target is None or self.thinking:
            return
        brain = self._brain(chat_id)
        with self.brain_lock:
            index = self._history_index(brain.messages, target)
            original = brain.messages[index]["content"] if index < len(brain.messages) else target["text"]
            reuse = {"for_claude": self._attachments_back(original), "shown": target.get("attachments") or [],
                     "saved": target.get("saved") or []}
            del brain.messages[index:]
            self.store.save_history(chat_id, brain.messages)
            self.store.delete_messages_from(chat_id, target["id"])
        self.hub.publish(self._chat_open_event(chat_id))
        text = target["text"] if new_text is None else str(new_text)
        self.typed.put({"text": text, "source": "typed", "chat_id": chat_id, "reuse": reuse})

    @staticmethod
    def _history_index(history: list, target: dict) -> int:
        """Where a chat-window message starts in Claude's history. Usually its saved position, but a very long
        chat may have had old messages trimmed since, so check and fall back to searching by text."""
        def opens_with(message):
            content = message["content"]
            text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if b.get("type") == "text")
            fresh = isinstance(content, str) or not any(b.get("type") == "tool_result" for b in content)
            return message["role"] == "user" and fresh and text.startswith(target["text"] or "")
        index = int(target.get("history_index", len(history)))
        if index < len(history) and opens_with(history[index]):
            return index
        for index in range(len(history) - 1, -1, -1):
            if opens_with(history[index]):
                return index
        return len(history)

    @staticmethod
    def _attachments_back(content) -> list:
        """Claude-format user content -> the attachment list think() takes (for retry and edit)."""
        found = []
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "image":
                found.append({"kind": "image", "name": "image", "media_type": block["source"]["media_type"], "data": block["source"]["data"]})
            elif block.get("type") == "document" and block.get("source", {}).get("type") == "base64":
                found.append({"kind": "pdf", "name": block.get("title") or "document.pdf", "data": block["source"]["data"]})
            elif block.get("type") == "document":
                found.append({"kind": "text", "name": block.get("title") or "file.txt", "text": block["source"].get("data", "")})
        return found

    def stop_everything(self):
        """The Stop button: end the reply being written, any running command, pending approvals, and speech."""
        self.stop_reply.set()
        tools.stop_running()
        for approval in list(self.approvals.values()):
            approval["decision"] = approval["decision"] or "deny"
            approval["event"].set()
        self.stop_speaking()

    # --- approvals (tools.APPROVER) ---

    @staticmethod
    def _approval_event(approval: dict) -> dict:
        return {"type": "approval", **{key: value for key, value in approval.items() if key != "event"}}

    def request_approval(self, kind: str, title: str, detail: str, chat_id) -> str:
        """Called by a tool on the reply's thread: show an approval card (and ask out loud during a voice turn),
        then wait for a decision. Returns "once", "chat" or "deny"."""
        if self.shutdown.is_set() or self.stop_reply.is_set():
            return "deny"
        approval = {"id": uuid.uuid4().hex[:12], "kind": kind, "title": title, "detail": detail, "chat_id": chat_id,
                    "time": time.time(), "decision": None, "via": None, "event": threading.Event()}
        self.approvals[approval["id"]] = approval
        self.hub.publish(self._approval_event(approval))
        self._publish_state()
        try:
            if self.voice_turn and self.voice_enabled and self.mic_on:
                self._approval_by_voice(approval)
            deadline = time.monotonic() + APPROVAL_TIMEOUT
            while not approval["event"].wait(0.2):
                if self.shutdown.is_set() or self.stop_reply.is_set() or time.monotonic() > deadline:
                    break
        finally:
            self.approvals.pop(approval["id"], None)
        decision = approval["decision"] or "deny"
        self.hub.publish({"type": "approval_done", "id": approval["id"], "chat_id": chat_id, "decision": decision,
                          "via": approval["via"]})
        self._publish_state()
        return decision

    def decide(self, approval_id: str, decision: str):
        """The page's approval buttons."""
        approval = self.approvals.get(approval_id)
        if approval and approval["decision"] is None and decision in ("once", "chat", "deny"):
            approval["decision"], approval["via"] = decision, "click"
            approval["event"].set()

    def _approval_by_voice(self, approval: dict):
        """During a voice turn: say what needs approving and listen for yes or no (a click still works too)."""
        import jarvis as cli
        from voice import listen
        what = "run that" if approval["kind"] == "command" else "use that folder"
        self.speak(f"May I {what}, sir? It's on screen. Yes or no.")
        waiting = lambda: approval["event"].is_set() or self.stop_reply.is_set() or self.shutdown.is_set()
        for _ in range(4):
            if waiting():
                return
            clean = cli.normalize(listen(should_stop=waiting))
            if clean in YES_WORDS:
                decision = "chat" if approval["kind"] == "folder" else "once"
            elif clean in NO_WORDS:
                decision = "deny"
            else:
                if clean and not waiting():
                    self.speak("Yes or no, sir?")
                continue
            if approval["decision"] is None:
                approval["decision"], approval["via"] = decision, "voice"
                approval["event"].set()
            return

    # --- memory ---

    def add_memory(self, fact: str):
        if str(fact).strip():
            memory.remember(str(fact)[:500])
        self.hub.publish({"type": "memory", "facts": memory.facts()})

    def delete_memory(self, index):
        if isinstance(index, int):
            memory.delete_fact(index)
        self.hub.publish({"type": "memory", "facts": memory.facts()})

    # --- files Jarvis made or showed ---

    @staticmethod
    def open_file(path_text: str, reveal: bool):
        path = Path(str(path_text))
        if not path.is_absolute() or not path.exists() or sys.platform != "win32":
            return
        if reveal or path.is_dir() or path.suffix.lower() in tools.RUNS_WHEN_OPENED:
            subprocess.Popen(["explorer.exe", "/select,", str(path)])   # programs and scripts are shown, never run
        else:
            os.startfile(path)

    # --- voice ---

    def speak(self, text: str):
        if not self.voice_enabled or not text:
            return
        import voice
        with self.speech_lock:
            self.stop_speech.clear()
            print(f"Jarvis: {text}")
            try:
                audio, rate = voice.synthesize(text)
                if self.stop_speech.is_set() or self.shutdown.is_set():
                    return
                self.speaking = True
                self._publish_state()
                voice.play(audio, rate, on_level=lambda value: self._level(value, "speak"))
            except Exception as e:
                print(f"⚠️ Speech: {e}")
            finally:
                if self.speaking:
                    self.speaking = False
                    self._publish_state()

    def stop_speaking(self):
        self.stop_speech.set()
        if self.speaking:
            try:
                import voice
                voice.stop()
            except Exception:
                pass

    def set_mic(self, on: bool):
        self.mic_on = bool(on) and self.voice_enabled
        self._publish_state()

    def talk_button(self):
        """The big mic button: stop talking, wake up, or go back to sleep, depending on what he's doing."""
        if self.speaking:
            self.stop_speaking()
        elif not self.voice_enabled:
            return
        elif not self.mic_on:
            self.set_mic(True)
            self.wake_request.set()
        elif self.voice_state == "asleep":
            self.wake_request.set()
        elif self.voice_state in ("listening", "awake"):
            self.sleep_request.set()

    def _voice_loop(self):
        try:
            import jarvis as cli   # the terminal version: same phrases, timings and wake replies
            from voice import listen
            from wake import wait_for_wake_word
        except Exception as e:
            print(f"⚠️ Voice is off: {e}")
            self.voice_enabled = self.mic_on = False
            self._set_voice_state("voice-off", f"Voice unavailable: {e}")
            return

        while not self.shutdown.is_set():
            if not self.mic_on:
                self._set_voice_state("muted")
                while not (self.mic_on or self.shutdown.is_set()):
                    time.sleep(0.1)
                continue
            try:
                self._set_voice_state("asleep")
                heard = wait_for_wake_word(
                    should_stop=lambda: self.wake_request.is_set() or not self.mic_on or self.shutdown.is_set(),
                    on_level=lambda value: self._level(value, "wake"))
                if self.shutdown.is_set() or not self.mic_on or not (heard or self.wake_request.is_set()):
                    continue
                self.wake_request.clear()
                self.sleep_request.clear()
                self.conversations += 1
                self._publish_counters()
                self._set_voice_state("awake")
                self.speak(random.choice(cli.WAKE_REPLIES))
                self._conversation(cli, listen)
            except Exception as e:   # usually the microphone: unplugged, wrong device number, or blocked
                print(f"⚠️ Voice loop: {e}")
                self._set_voice_state("error", f"Microphone problem: {e}")
                self.shutdown.wait(5)

    def _conversation(self, cli, listen):
        """Awake: listen, answer, repeat, until dismissed, muted or left in silence (same rules as jarvis.py)."""
        idle = 0
        while not self.shutdown.is_set() and self.mic_on:
            if self.sleep_request.is_set():
                self.sleep_request.clear()
                self.speak(random.choice(cli.SLEEP_REPLIES))
                return
            time.sleep(cli.POST_SPEAK_PAUSE)
            self._set_voice_state("listening")
            text = listen(on_level=lambda value: self._level(value, "listen"),
                          should_stop=lambda: self.sleep_request.is_set() or not self.mic_on or self.shutdown.is_set())
            self._set_voice_state("awake")
            if self.sleep_request.is_set() or not self.mic_on:
                continue
            clean = cli.normalize(text)
            if not clean or clean in cli.PHANTOM_TRANSCRIPTS:
                idle += 1
                if idle >= cli.IDLE_LIMIT:
                    self.speak("Standing by, sir.")
                    return
                continue
            idle = 0
            self.hub.publish({"type": "heard", "text": text})
            if cli.said_any(clean, cli.SLEEP_PHRASES):
                self.speak(random.choice(cli.SLEEP_REPLIES))
                return
            if clean in cli.SKIP_PHRASES:
                continue
            self.speak(self.ask(text, "voice"))

    # --- dashboard feeds ---

    def _pause(self, seconds: float, name: str) -> bool:
        """Sleep, waking early if the page asked for a refresh. False once Jarvis is shutting down."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.shutdown.is_set():
            if self.refresh[name].is_set():
                self.refresh[name].clear()
                break
            time.sleep(0.1)
        return not self.shutdown.is_set()

    def _stats_feed(self):
        recent_cpu = deque(maxlen=30)   # about a minute of samples, for the "system load" bar
        dashboard.system_stats()        # the first CPU reading is always 0, so throw one away
        time.sleep(0.5)
        while True:
            try:
                stats = dashboard.system_stats()
                recent_cpu.append(stats["cpu"])
                self.hub.publish({"type": "stats", **stats, "load": round(sum(recent_cpu) / len(recent_cpu), 1)})
            except Exception as e:
                print(f"⚠️ Stats: {e}")
            if not self._pause(STATS_EVERY, "stats"):
                return

    def _weather_feed(self):
        while True:
            city, units = self.settings["weather_city"], self.settings["units"]
            try:
                event = {"type": "weather", **dashboard.weather(city, units)}
            except Exception as e:
                event = {"type": "weather", "city": city, "error": f"Weather unavailable ({e})"}
            self.hub.publish(event)
            if not self._pause(WEATHER_EVERY if "error" not in event else 60, "weather"):
                return

    def _spotify_feed(self):
        while True:
            try:
                info = tools.spotify_now_playing()
            except Exception as e:   # no internet, Spotify having a moment...
                info = {"connected": True, "error": f"Spotify unavailable ({e})"}
            self.hub.publish({"type": "spotify", **info})
            if not self._pause(SPOTIFY_EVERY if info.get("connected") else SPOTIFY_OFFLINE_EVERY, "spotify"):
                return

    def spotify_button(self, action: str):
        if action == "toggle":
            playing = (self.hub.latest.get("spotify") or {}).get("playing")
            action = "pause" if playing else "play"
        if action in ("play", "pause", "next", "previous"):
            print(f"🎵 {tools.spotify_control(action)}")
            time.sleep(0.5)   # give Spotify a moment before asking what's on
            self.refresh["spotify"].set()

    def update_settings(self, values: dict):
        weather_changed = False
        for key, value in values.items():
            if key in ("speak_typed", "quiet_answers", "show_tools") and isinstance(value, bool):
                pass
            elif key == "weather_city" and isinstance(value, str) and value.strip():
                value = value.strip()[:80]
            elif key == "units" and value in ("imperial", "metric"):
                pass
            elif key == "new_chat_model" and value in MODEL_IDS:
                pass
            else:
                continue
            if self.settings.get(key) != value:
                self.settings[key] = value
                weather_changed |= key in ("weather_city", "units")
        save_settings(self.settings)
        self.hub.publish({"type": "settings", **self.settings})
        if weather_changed:
            self.refresh["weather"].set()


# ---------- The web server ----------

def create_app(jarvis: Jarvis, hub: Hub, port: int) -> FastAPI:
    # Only pages from this server may connect. Without this check, any website open in your browser
    # could reach 127.0.0.1 and send Jarvis commands.
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @asynccontextmanager
    async def lifespan(app):
        hub.loop = asyncio.get_running_loop()
        jarvis.start()
        yield
        jarvis.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    @app.get("/")
    async def index():
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def socket_endpoint(ws: WebSocket):
        if ws.headers.get("origin") not in allowed_origins:
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = asyncio.Queue()
        for event in await asyncio.to_thread(jarvis.snapshot):
            queue.put_nowait(event)
        hub.queues.add(queue)
        jarvis.page_opened()

        async def send_events():
            while True:
                await ws.send_text(json.dumps(await queue.get()))

        sender = asyncio.create_task(send_events())
        try:
            while True:
                try:
                    command = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                if isinstance(command, dict):
                    await handle_command(jarvis, command)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            hub.queues.discard(queue)
            jarvis.page_closed()

    return app


async def handle_command(jarvis: Jarvis, command: dict):
    kind = command.get("type")
    chat_id = str(command.get("chat_id") or "") or jarvis.active_chat
    in_thread = asyncio.to_thread   # anything slow runs off the event loop so the page stays responsive
    if kind == "send":
        text = str(command.get("text", "")).strip()[:100_000]
        attachments = command.get("attachments") if isinstance(command.get("attachments"), list) else []
        if text or attachments:
            jarvis.typed.put({"text": text, "source": "typed", "chat_id": chat_id, "attachments": attachments})
    elif kind == "stop":
        jarvis.stop_everything()
    elif kind == "approve":
        jarvis.decide(str(command.get("id")), str(command.get("decision")))
    elif kind == "talk":
        jarvis.talk_button()
    elif kind == "mic":
        jarvis.set_mic(bool(command.get("on")))
    elif kind == "chat_new":
        await in_thread(jarvis.new_chat)
    elif kind == "chat_open":
        await in_thread(jarvis.open_chat, chat_id)
    elif kind == "chat_rename":
        await in_thread(jarvis.rename_chat, chat_id, command.get("title", ""))
    elif kind == "chat_pin":
        await in_thread(jarvis.pin_chat, chat_id, bool(command.get("pinned")))
    elif kind == "chat_delete":
        await in_thread(jarvis.delete_chat, chat_id)
    elif kind == "chat_settings":
        await in_thread(jarvis.chat_settings, chat_id, command.get("model"), command.get("think"))
    elif kind == "chat_search":
        await in_thread(jarvis.search_chats, str(command.get("query", ""))[:200])
    elif kind == "retry":
        await in_thread(jarvis.rerun, chat_id)
    elif kind == "edit":
        await in_thread(jarvis.rerun, chat_id, str(command.get("message_id")), str(command.get("text", ""))[:100_000])
    elif kind == "memory_add":
        await in_thread(jarvis.add_memory, command.get("fact", ""))
    elif kind == "memory_delete":
        await in_thread(jarvis.delete_memory, command.get("index"))
    elif kind == "open_file":
        await in_thread(jarvis.open_file, command.get("path", ""), bool(command.get("reveal")))
    elif kind == "settings" and isinstance(command.get("values"), dict):
        await in_thread(jarvis.update_settings, command["values"])
    elif kind == "spotify":
        threading.Thread(target=jarvis.spotify_button, args=(str(command.get("action")),), daemon=True).start()
    elif kind == "refresh" and command.get("what") in jarvis.refresh:
        jarvis.refresh[command["what"]].set()


# ---------- Starting up ----------

def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port between {preferred} and {preferred + 19}.")


def open_window(server: uvicorn.Server, url: str, browser_tab: bool):
    for _ in range(100):   # wait (up to 10 s) for the server to be ready
        if server.started:
            break
        time.sleep(0.1)
    if not browser_tab and sys.platform == "win32":
        try:   # Edge's app mode: its own window with no tabs or address bar. Edge comes with Windows.
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={url}", "--start-maximized"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            return
        except OSError:
            pass
    webbrowser.open(url)


def main():
    parser = argparse.ArgumentParser(description="Jarvis with a dashboard")
    parser.add_argument("--browser", action="store_true", help="open in a normal browser tab")
    parser.add_argument("--no-window", action="store_true", help="don't open anything; visit the address yourself")
    parser.add_argument("--no-voice", action="store_true", help="chat only: no microphone or speakers")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    tools.CHAT_WINDOW = True
    tools.WORKSPACE.mkdir(exist_ok=True)
    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    hub = Hub()
    jarvis = Jarvis(hub, load_settings(), voice_enabled=not args.no_voice, quit_with_window=not args.no_window)
    server = uvicorn.Server(uvicorn.Config(create_app(jarvis, hub, port), host="127.0.0.1", port=port,
                                           log_level="warning", ws_max_size=64 * 1024 * 1024))
    jarvis.server = server
    if not args.no_window:
        threading.Thread(target=open_window, args=(server, url, args.browser), daemon=True).start()
    print(f"Jarvis dashboard: {url}   (Ctrl+C to quit)")
    server.run()
    jarvis.stop()


if __name__ == "__main__":
    main()
