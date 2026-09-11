# brain.py — the conversation with Claude: tools, long-term memory, web search, links and screenshots

import re

from anthropic import Anthropic
import config
import memory
import tools

MAX_HISTORY = 20       # keep roughly the last 20 messages so long sessions stay cheap
MAX_TOOL_ROUNDS = 10   # safety cap so a tool that keeps failing can't loop forever
MAX_LINKS = 8          # most links shown under one reply

URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]*[^\s<>\"'()\[\].,;:!?]", re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def for_speech(text: str) -> str:
    """The reply with web addresses taken out, so text-to-speech never reads one aloud.
    The addresses aren't lost: think() collects them into last_links for the chat window."""
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = URL.sub("", text)
    text = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", text)   # brackets an address was in
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)     # "it's at ." -> "it's at."
    return re.sub(r"[ \t]{2,}", " ", text).strip()


class Brain:
    def __init__(self):
        self.client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.messages = []          # running conversation history
        self.on_event = None        # gui.py sets this to show tool activity while Jarvis works
        self.last_links = []        # links from the latest reply: web sources, fetched pages, show_links
        self.last_screenshots = []  # screenshots taken for the latest reply (small copies for the chat)
        self.last_tools = []        # tools used for the latest reply

    def _trim_history(self):
        """Drop old messages, but always start the kept history on a plain user message.
        Cutting between a tool request and its tool result makes the API reject the call."""
        if len(self.messages) <= MAX_HISTORY:
            return
        start = len(self.messages) - MAX_HISTORY
        while start < len(self.messages) - 1 and not (
            self.messages[start]["role"] == "user"
            and isinstance(self.messages[start]["content"], str)
        ):
            start += 1
        self.messages = self.messages[start:]

    def _forget_old_screenshots(self):
        """Keep only the newest screenshot in the history. Every image is sent again with each
        request, so old ones would quietly cost tokens for the rest of the conversation."""
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
                        {"type": "text", "text": "(Screenshot removed to save tokens. Take a new one if you need it.)"}
                    ]
                newest_seen = True

    def think(self, user_text: str) -> str:
        """Send the user's text to Claude, run any tools it asks for, return the spoken reply."""
        tools.new_turn()  # lets folder permission tell the user's real "yes" apart from Claude answering itself
        tools.links_to_show.clear()
        tools.screenshots_to_show.clear()
        self.last_links, self.last_screenshots, self.last_tools = [], [], []
        self.messages.append({"role": "user", "content": user_text})
        self._trim_history()

        # Build the system prompt fresh each call, so a fact saved a moment ago is already known
        system_prompt = (
            config.PERSONALITY
            + "\n\nThings you know about the user:\n"
            + memory.memory_as_text()
        )

        links = []  # web sources and fetched pages, gathered across every round
        for _ in range(MAX_TOOL_ROUNDS):  # loop to allow multiple tool calls in a row
            response = self.client.messages.create(
                model=config.BRAIN_MODEL,
                max_tokens=2000,  # room for long notes; spoken replies stay short anyway
                system=system_prompt,
                messages=self.messages,
                tools=tools.TOOL_SCHEMA,
            )

            # Save Claude's turn exactly as received. Web search results carry encrypted
            # content that the API needs back unchanged on later turns.
            self.messages.append({"role": "assistant", "content": response.content})
            links += self._web_links(response.content)

            # Show web activity in the terminal (it runs on Anthropic's side, not here)
            for block in response.content:
                if block.type == "server_tool_use":
                    print(f"🌐 {block.name}({block.input})")
                    self._note_tool(block.name)

            if response.stop_reason == "pause_turn":
                continue  # a long search paused mid-turn: send it back so Claude can finish

            if response.stop_reason == "max_tokens" and any(b.type == "tool_use" for b in response.content):
                # Cut off in the middle of a tool call (say, a very long note). A half-written tool
                # request can't stay in the history, or every later call gets rejected.
                self.messages.pop()
                reply = "That was too long to finish in one go, sir. Shall I try a shorter version?"
                self.messages.append({"role": "assistant", "content": reply})
                return self._finish(reply, links)

            if response.stop_reason != "tool_use":
                # Speak only what Claude said after its last web result, so a
                # "let me look that up" line from before the search isn't read out
                last_result = max(
                    (i for i, b in enumerate(response.content) if b.type.endswith("_tool_result")),
                    default=-1,
                )
                text = "".join(
                    b.text for b in response.content[last_result + 1:] if b.type == "text"
                )
                return self._finish(text.strip() or "Done, sir.", links)

            # Claude asked for one or more of OUR tools (git, files, remember...). Run them.
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"🔧 Running {block.name}({block.input})")
                    self._note_tool(block.name)
                    func = tools.TOOL_FUNCTIONS.get(block.name)
                    try:
                        output = func(**block.input) if func else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                    # Most tools return text. screenshot returns content blocks (text + image) for Claude to see.
                    if isinstance(output, list):
                        content = output
                        summary = " ".join(part.get("text", "") for part in output if part.get("type") == "text")
                    else:
                        content = summary = str(output)
                    print(f"   ↳ {summary[:150]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })

            # Feed the results back so Claude can form its final answer
            self.messages.append({"role": "user", "content": tool_results})
            self._forget_old_screenshots()

        # Safety cap reached: close the turn so the history stays valid for next time
        reply = "I'm going in circles on that one, sir."
        self.messages.append({"role": "assistant", "content": reply})
        return self._finish(reply, links)

    def _note_tool(self, name: str):
        """Remember a tool was used, and tell the GUI right away so it can show what Jarvis is doing."""
        self.last_tools.append(name)
        if self.on_event:
            try:
                self.on_event({"type": "tool", "name": name})
            except Exception:
                pass  # a GUI hiccup must never break a reply

    @staticmethod
    def _web_links(content) -> list:
        """Sources Claude cited from web search, and pages it read with web fetch."""
        found = []
        for block in content:
            if block.type == "text":
                for citation in getattr(block, "citations", None) or []:
                    if getattr(citation, "type", "") == "web_search_result_location":
                        found.append({"title": citation.title, "url": citation.url})
            elif block.type == "web_fetch_tool_result" and getattr(block.content, "type", "") == "web_fetch_result":
                page = block.content
                found.append({"title": getattr(page.content, "title", None), "url": page.url})
        return found

    def _finish(self, reply: str, links: list) -> str:
        """Collect this reply's links and screenshots for the chat window, then return the reply."""
        in_reply = [{"title": None, "url": u if u.lower().startswith("http") else "https://" + u}
                    for u in URL.findall(reply)]
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
        if self.last_links and not tools.CHAT_WINDOW:  # terminal version: print them instead
            for link in self.last_links:
                print(f"🔗 {link['title']}: {link['url']}")
        return reply