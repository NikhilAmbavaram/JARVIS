# brain.py — the conversation with Claude

from anthropic import Anthropic
import config

class Brain:
    def __init__(self):
        self.client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.messages = []  # running conversation history

    def think(self, user_text: str) -> str:
        """Send the user's text to Claude, get a reply, remember both."""
        self.messages.append({"role": "user", "content": user_text})

        response = self.client.messages.create(
            model=config.BRAIN_MODEL,
            max_tokens=1000,
            system=config.PERSONALITY,
            messages=self.messages,
        )

        # Claude replies with a list of "content blocks"; for plain text
        # there's just one text block.
        reply = response.content[0].text
        self.messages.append({"role": "assistant", "content": reply})
        return reply