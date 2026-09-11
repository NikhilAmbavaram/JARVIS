# brain.py — the conversation with Claude: tools, long-term memory, and web search

from anthropic import Anthropic
import config
import memory
import tools

MAX_HISTORY = 20       # keep roughly the last 20 messages so long sessions stay cheap
MAX_TOOL_ROUNDS = 10   # safety cap so a tool that keeps failing can't loop forever


class Brain:
    def __init__(self):
        self.client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.messages = []  # running conversation history

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

    def think(self, user_text: str) -> str:
        """Send the user's text to Claude, run any tools it asks for, return the spoken reply."""
        tools.new_turn()  # lets folder permission tell the user's real "yes" apart from Claude answering itself
        self.messages.append({"role": "user", "content": user_text})
        self._trim_history()

        # Build the system prompt fresh each call, so a fact saved a moment ago is already known
        system_prompt = (
            config.PERSONALITY
            + "\n\nThings you know about the user:\n"
            + memory.memory_as_text()
        )

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

            # Show web activity in the terminal (it runs on Anthropic's side, not here)
            for block in response.content:
                if block.type == "server_tool_use":
                    print(f"🌐 {block.name}({block.input})")

            if response.stop_reason == "pause_turn":
                continue  # a long search paused mid-turn: send it back so Claude can finish

            if response.stop_reason == "max_tokens" and any(b.type == "tool_use" for b in response.content):
                # Cut off in the middle of a tool call (say, a very long note). A half-written tool
                # request can't stay in the history, or every later call gets rejected.
                self.messages.pop()
                reply = "That was too long to finish in one go, sir. Shall I try a shorter version?"
                self.messages.append({"role": "assistant", "content": reply})
                return reply

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
                return text.strip() or "Done, sir."

            # Claude asked for one or more of OUR tools (git, files, remember...). Run them.
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"🔧 Running {block.name}({block.input})")
                    func = tools.TOOL_FUNCTIONS.get(block.name)
                    try:
                        output = func(**block.input) if func else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                    print(f"   ↳ {str(output)[:150]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })

            # Feed the results back so Claude can form its final answer
            self.messages.append({"role": "user", "content": tool_results})

        # Safety cap reached: close the turn so the history stays valid for next time
        reply = "I'm going in circles on that one, sir."
        self.messages.append({"role": "assistant", "content": reply})
        return reply