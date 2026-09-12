# brain.py — the conversation with Claude: tools, memory, web search, files, visuals, links and screenshots
#
# One Brain holds one conversation. jarvis.py keeps a single Brain in memory; gui.py keeps one per chat
# and saves Brain.messages to jarvis.db after every reply.

import json
import re
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
import config
import memory
import tools

MAX_TOOL_ROUNDS = 25   # a coding task can take many steps; this still stops a tool that keeps failing
MAX_LINKS = 8          # most links shown under one reply
HISTORY_CHARS = {True: 60_000, False: 400_000}   # conversation sent to Claude: spoken ~15k tokens, typed ~100k
STORED_CHARS = 800_000                           # the most any chat keeps at all
IMAGE_CHARS = 6_000    # an image counts as this much toward those budgets, whatever its base64 size

# Models that think "adaptively" (Claude decides how much). Older ones, like Haiku 4.5, take a token budget.
ADAPTIVE_THINKING = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5", "claude-opus-4-6",
                     "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6")

URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]*[^\s<>\"'()\[\].,;:!?]", re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

SPOKEN_MODE = """
This message was spoken. What you say is read aloud — but the dashboard is open in front of him, and
the chat window is the better place for anything he needs to READ rather than hear.

- Before answering a problem, explaining anything at length, or writing code, maths, steps, a table or a
  list: call answer_in_chat FIRST. Then write the full answer in Markdown, as long as it needs to be. It
  appears on screen and is not read out; only your one short line is spoken. Speech is slow and he can't
  re-read it, so this is almost always the kinder choice for real answers.
- Quick facts, confirmations, and ordinary conversation are just spoken, in the one or two sentences the
  "How you speak" rules describe.
- Links go in the chat with show_links, diagrams and charts with create_visual. Never read an address out.
""".strip()

TYPED_MODE = """
This message was typed in the dashboard's chat window, and your reply will be shown as text, not read aloud.
The "How you speak" rules above are for speech, so for this reply:
- Use Markdown freely: headings, lists, tables, fenced code blocks with a language, and math as $...$ or $$...$$.
- Be as thorough as the task needs. Keep Jarvis's voice, but completeness beats brevity here.
- When a diagram, chart or small interactive demo would explain something better than words, use create_visual.
  You can also put a ```mermaid code block straight into your reply.
- For coding: look at the relevant files first (read_file, find_files, search_text), change them with
  write_file or edit_file, and check your work with run_command or run_python before saying it's done.
""".strip()


class Stopped(Exception):
    """The user pressed Stop."""


def for_speech(text: str) -> str:
    """The reply as something worth hearing: no web addresses, code or Markdown symbols read aloud.
    None of it is lost: the chat window shows the full reply, and think() collects links into last_links."""
    text = re.sub(r"```.*?(```|$)", " (the code is in the chat) ", text, flags=re.DOTALL)
    text = re.sub(r"\$\$.*?\$\$", " (the formula is in the chat) ", text, flags=re.DOTALL)
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.MULTILINE)          # table rows
    text = re.sub(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+", "", text, flags=re.MULTILINE)   # headings, quotes, bullets
    text = re.sub(r"(\*\*|__|\*|`)", "", text)
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = URL.sub("", text)
    text = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", text)   # brackets an address was in
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)     # "it's at ." -> "it's at."
    return re.sub(r"[ \t]{2,}", " ", text).strip()


ON_SCREEN = re.compile(r"```|\$\$|^[ \t]*\|.*\|[ \t]*$|^[ \t]{0,3}(?:\d+[.)]|[-*+])[ \t]+", re.MULTILINE)


def belongs_on_screen(text: str, limit: int = 350) -> bool:
    """True if a reply is meant to be read rather than heard: too long to sit through, or full of code,
    maths, tables or steps that speech would only mangle. The GUI keeps these in the chat window."""
    return len(for_speech(text)) > limit or bool(ON_SCREEN.search(text or ""))


def _plain(value):
    """API response objects -> plain dicts and lists, so a history can be saved as JSON and sent back unchanged."""
    if hasattr(value, "to_dict"):
        return value.to_dict(mode="json")
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _fresh_user(message) -> bool:
    """A message the user wrote (not tool results), which is where a trimmed history must start."""
    if message["role"] != "user":
        return False
    content = message["content"]
    return isinstance(content, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _size(content) -> int:
    """Roughly how much a message costs, in characters, with pictures counted at a flat rate."""
    if isinstance(content, str):
        return len(content)
    total = 0
    for block in content:
        if not isinstance(block, dict):
            total += len(str(block))
        elif block.get("type") == "image":
            total += IMAGE_CHARS
        elif block.get("type") == "document" and block.get("source", {}).get("type") == "base64":
            total += len(block["source"].get("data", "")) // 3
        elif isinstance(block.get("content"), list):
            total += _size(block["content"]) + 100
        else:
            total += len(json.dumps(block))
    return total


class Brain:
    def __init__(self, messages=None):
        self.client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.messages = messages if messages is not None else []   # the conversation, as plain JSON-able dicts
        self.on_event = None   # gui.py sets this to see the reply as it's written: text, thinking, tools
        self.stop = None       # gui.py sets this to a threading.Event; setting it stops the reply
        self.chat_id = None    # which chat this is, so "allow for this chat" approvals stick to it
        self._partial = ""
        self._turn_start = 0
        self._reset_extras()

    def _reset_extras(self):
        self.last_text = ""          # everything Claude wrote this turn (the chat window shows this)
        self.last_thinking = ""      # Claude's summarized thinking, when thinking was on
        self.last_links = []         # web sources, fetched pages, show_links, addresses in the reply
        self.last_screenshots = []   # screenshots taken this turn (small copies for the chat)
        self.last_visuals = []       # diagrams, charts and demos from create_visual
        self.last_files = []         # files written, edited or shown this turn
        self.last_tools = []         # tools used this turn
        self.last_spoken = None      # answer_in_chat: the one line to say, with the rest left on screen
        self.stopped = False

    # --- one turn ---

    def think(self, user_text: str, attachments=None, model=None, think_harder=False, spoken=True, stream=False) -> str:
        """Send one message to Claude, run the tools it asks for, and return what Jarvis says.
        spoken=True means the reply is read aloud (short, plain); spoken=False means it's shown as text in the
        chat window (Markdown, code and visuals welcome). model defaults to config.BRAIN_MODEL.
        attachments: [{"kind": "image"|"pdf"|"text", "name", "media_type", "data" (base64) or "text"}]"""
        model = model or config.BRAIN_MODEL
        tools.new_turn()  # lets folder permission tell the user's real "yes" apart from Claude answering itself
        tools.begin_reply(self.chat_id)
        self._reset_extras()
        self._trim_stored()
        self.messages.append({"role": "user", "content": self._user_content(user_text, attachments)})
        self._turn_start = len(self.messages) - 1

        request = {
            "model": model,
            "max_tokens": self._max_tokens(model, think_harder, spoken),
            "system": self._system_prompt(spoken),
            "tools": tools.schema(),
            "cache_control": {"type": "ephemeral"},   # automatic prompt caching: repeated context costs a tenth
            **self._thinking_options(model, think_harder, spoken),
        }
        texts, links = [], []   # everything written this turn, and web sources, across every round
        try:
            for round_number in range(MAX_TOOL_ROUNDS):
                if round_number:
                    self._event(type="round")   # tells the chat window a new stretch of text is starting
                response = self._call({**request, "messages": self._request_messages(spoken)}, stream)
                content = _plain(response.content)

                # Save Claude's turn exactly as received. Web search results carry encrypted
                # content, and thinking carries signatures, that the API needs back unchanged.
                self.messages.append({"role": "assistant", "content": content})
                links += self._web_links(content)
                for block in content:
                    if block["type"] == "text":
                        texts.append(block["text"])
                    elif block["type"] == "thinking":
                        self.last_thinking += block.get("thinking", "")
                    elif block["type"] == "server_tool_use":   # web search/fetch run on Anthropic's side
                        print(f"🌐 {block['name']}({block.get('input')})")
                        self._note_tool(block["name"])

                if response.stop_reason == "pause_turn":
                    continue  # a long search paused mid-turn: send it back so Claude can finish

                if response.stop_reason == "max_tokens" and any(b["type"] == "tool_use" for b in content):
                    # Cut off in the middle of a tool call. A half-written tool request can't stay in the
                    # history, or every later call gets rejected.
                    self.messages.pop()
                    reply = "That was too long to finish in one go, sir. Shall I try a shorter version?"
                    self.messages.append({"role": "assistant", "content": reply})
                    return self._finish(reply, links, texts + [reply])

                if response.stop_reason != "tool_use":
                    # Speak only what Claude said after its last web result, so a
                    # "let me look that up" line from before the search isn't read out
                    last_result = max((i for i, b in enumerate(content) if b["type"].endswith("_tool_result")), default=-1)
                    final = "".join(b["text"] for b in content[last_result + 1:] if b["type"] == "text").strip()
                    return self._finish(final or "Done, sir.", links, texts)

                # Claude asked for one or more of OUR tools. Run them.
                tool_results = []
                for block in content:
                    if block["type"] == "tool_use":
                        tool_results.append(self._run_tool(block))
                # Feed the results back so Claude can carry on
                self.messages.append({"role": "user", "content": tool_results})
                self._forget_old_screenshots()

            reply = "I'm going in circles on that one, sir."
            self.messages.append({"role": "assistant", "content": reply})
            return self._finish(reply, links, texts + [reply])
        except Stopped:
            return self._stopped(links, texts)

    def _run_tool(self, block: dict) -> dict:
        name, arguments = block["name"], block.get("input") or {}
        print(f"🔧 Running {name}({arguments})")
        self._note_tool(name)
        func = tools.TOOL_FUNCTIONS.get(name)
        try:
            output = func(**arguments) if func else f"Unknown tool: {name}"
        except Exception as e:
            output = f"Error: {e}"
        # Most tools return text. screenshot and read_file (for pictures and PDFs) return content blocks.
        if isinstance(output, list):
            content = output
            summary = " ".join(part.get("text", "") for part in output if part.get("type") == "text")
        else:
            content = summary = str(output)
        print(f"   ↳ {summary[:150]}")
        self._event(type="tool_done", name=name, summary=summary[:300])
        return {"type": "tool_result", "tool_use_id": block["id"], "content": content}

    # --- talking to the API ---

    def _call(self, request: dict, stream: bool):
        if self.stop is not None and self.stop.is_set():
            raise Stopped
        if not stream:
            return self.client.messages.create(**request)
        self._partial = ""
        with self.client.messages.stream(**request) as live:
            for event in live:
                if self.stop is not None and self.stop.is_set():
                    raise Stopped   # leaving the with-block closes the connection
                if event.type == "text":
                    self._partial += event.text
                    self._event(type="text", delta=event.text)
                elif event.type == "thinking":
                    self._event(type="thinking", delta=event.thinking)
                elif event.type == "content_block_start" and getattr(event.content_block, "type", "") in ("tool_use", "server_tool_use"):
                    self._event(type="tool_start", name=event.content_block.name)
            message = live.get_final_message()
        self._partial = ""
        return message

    def _system_prompt(self, spoken: bool) -> str:
        today = datetime.now().strftime("%A, %B %d, %Y")   # the date only, so the prompt cache survives all day
        return "\n\n".join([
            config.PERSONALITY.strip(),
            SPOKEN_MODE if spoken else TYPED_MODE,
            f"Today is {today}. Jarvis runs on the user's Windows PC. Jarvis's own code is in {Path(__file__).parent}, "
            f"and its scratch workspace for scripts and new files is {tools.WORKSPACE}.",
            "Standing instructions and facts Nikhil has asked you to remember. They apply in every chat, and "
            "where one of them contradicts the style rules above, the instruction wins:\n" + memory.memory_as_text(),
        ])

    @staticmethod
    def _thinking_options(model: str, think_harder: bool, spoken: bool) -> dict:
        adaptive = model.startswith(ADAPTIVE_THINKING)
        if think_harder:
            if adaptive:
                return {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "max"}}
            return {"thinking": {"type": "enabled", "budget_tokens": 8000, "display": "summarized"}}
        if adaptive and not spoken:   # typed chats on Opus/Sonnet: let Claude think when it helps
            return {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "high"}}
        return {}

    @staticmethod
    def _max_tokens(model: str, think_harder: bool, spoken: bool) -> int:
        if spoken and not think_harder:
            return 2000            # spoken replies stay short anyway; room for a long note
        if model.startswith(ADAPTIVE_THINKING):
            return 32000
        return 16000

    @staticmethod
    def _user_content(text: str, attachments):
        if not attachments:
            return text
        blocks = []
        for item in attachments:
            if item["kind"] == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": item["media_type"], "data": item["data"]}})
            elif item["kind"] == "pdf":
                blocks.append({"type": "document", "title": item["name"],
                               "source": {"type": "base64", "media_type": "application/pdf", "data": item["data"]}})
            else:
                blocks.append({"type": "document", "title": item["name"],
                               "source": {"type": "text", "media_type": "text/plain", "data": item["text"]}})
        blocks.append({"type": "text", "text": text or "Take a look at the attached file."})
        return blocks

    # --- keeping the history valid and affordable ---

    def _request_messages(self, spoken: bool) -> list:
        """The conversation to send: the newest messages that fit the budget, starting at a message the user wrote.
        The stored history isn't cut, so a quick spoken turn doesn't erase a long typed chat."""
        limit = HISTORY_CHARS[spoken]
        start, total = self._turn_start, sum(_size(m["content"]) for m in self.messages[self._turn_start:])
        while start > 0 and total + _size(self.messages[start - 1]["content"]) <= limit:
            start -= 1
            total += _size(self.messages[start]["content"])
        while start < self._turn_start and not _fresh_user(self.messages[start]):
            start += 1
        return self.messages[start:]

    def _trim_stored(self):
        """Drop the oldest messages once a chat gets enormous, starting the kept part at a user message."""
        total = sum(_size(m["content"]) for m in self.messages)
        start = 0
        while total > STORED_CHARS and start < len(self.messages):
            total -= _size(self.messages[start]["content"])
            start += 1
        while start < len(self.messages) and not _fresh_user(self.messages[start]):
            start += 1
        if start:
            self.messages = self.messages[start:]

    def _forget_old_screenshots(self):
        """Keep only the newest tool-result image (screenshot or picture Jarvis opened) in the history. Every image
        is sent again with each request, so old ones would quietly cost tokens for the rest of the conversation."""
        newest_seen = False
        for message in reversed(self.messages):
            if message["role"] != "user" or not isinstance(message["content"], list):
                continue
            for block in reversed(message["content"]):
                content = block.get("content") if isinstance(block, dict) else None
                if not isinstance(content, list) or not any(part.get("type") == "image" for part in content):
                    continue
                if newest_seen:
                    block["content"] = [part for part in content if part.get("type") != "image"] + [
                        {"type": "text", "text": "(Image removed to save tokens. Take a new one if you need it.)"}
                    ]
                newest_seen = True

    def _strip_turn_thinking(self):
        """Thinking only matters within a turn. Dropping it afterwards keeps the history lean, and lets a chat
        switch models (Opus for typing, Haiku for voice) without sending one model's thinking to another."""
        for message in self.messages[self._turn_start:]:
            if message["role"] == "assistant" and isinstance(message["content"], list):
                kept = [b for b in message["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
                message["content"] = kept or "(thinking only)"

    # --- wrapping up ---

    def _note_tool(self, name: str):
        """Remember a tool was used, and tell the GUI right away so it can show what Jarvis is doing."""
        self.last_tools.append(name)
        self._event(type="tool", name=name)

    def _event(self, **event):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass  # a GUI hiccup must never break a reply

    @staticmethod
    def _web_links(content: list) -> list:
        """Sources Claude cited from web search, and pages it read with web fetch."""
        found = []
        for block in content:
            if block["type"] == "text":
                for citation in block.get("citations") or []:
                    if citation.get("type") == "web_search_result_location":
                        found.append({"title": citation.get("title"), "url": citation.get("url")})
            elif block["type"] == "web_fetch_tool_result" and (block.get("content") or {}).get("type") == "web_fetch_result":
                page = block["content"]
                found.append({"title": (page.get("content") or {}).get("title"), "url": page.get("url")})
        return found

    def _finish(self, reply: str, links: list, texts: list) -> str:
        """Collect this turn's text, links, files and visuals for the chat window, then return the reply."""
        self.last_text = "\n\n".join(t.strip() for t in texts if t.strip()) or reply
        in_reply = [{"title": None, "url": u if u.lower().startswith("http") else "https://" + u}
                    for u in URL.findall(self.last_text)]
        unique = {}   # address -> link, in the order first seen; links Claude chose to show come first
        for link in tools.links_to_show + links + in_reply:
            url = str(link.get("url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            key, title = url.rstrip("/").lower(), str(link.get("title") or "")[:120]
            if key not in unique:
                unique[key] = {"title": title or url, "url": url}
            elif title and unique[key]["title"] == unique[key]["url"]:
                unique[key]["title"] = title   # the same page seen again, this time with a proper title
        self.last_links = list(unique.values())[:MAX_LINKS]
        self.last_screenshots = list(tools.screenshots_to_show)
        self.last_visuals = list(tools.visuals_to_show)
        self.last_files = list(tools.files_to_show)
        self.last_spoken = tools.quiet_reply
        self._strip_turn_thinking()
        if self.last_links and not tools.CHAT_WINDOW:  # terminal version: print them instead
            for link in self.last_links:
                print(f"🔗 {link['title']}: {link['url']}")
        return reply

    def _stopped(self, links: list, texts: list) -> str:
        """Close the turn cleanly after Stop, keeping whatever Claude had written so far."""
        self.stopped = True
        partial = self._partial.strip()
        self._partial = ""
        if partial:
            texts.append(partial)
        if self.messages[-1]["role"] == "assistant":   # can't happen mid-stream, but keep the history valid regardless
            self.messages.append({"role": "user", "content": "(The user stopped the reply.)"})
        self.messages.append({"role": "assistant", "content": (partial + "\n\n" if partial else "") + "(Stopped by the user.)"})
        return self._finish("Stopped, sir.", links, texts)

    def make_title(self, user_text: str, reply_text: str) -> str:
        """A short title for a new chat, written by the cheapest model."""
        response = self.client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=24,
            system="Write a title of 2 to 6 words for this conversation. Reply with the title only: no quotes, no final punctuation.",
            messages=[{"role": "user", "content": f"User: {user_text[:1500]}\n\nAssistant: {reply_text[:1500]}"}],
        )
        title = "".join(b.text for b in response.content if b.type == "text").strip().strip('"').strip()
        return title[:60] or "New chat"
