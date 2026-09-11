# gui.py — Jarvis with a dashboard: the voice loop plus a control panel in its own window
#
#   python .\gui.py              opens the dashboard in an Edge app window
#   python .\gui.py --browser    opens it in a normal browser tab instead
#   python .\gui.py --no-voice   chat only: no microphone or speakers
#
# How it fits together: this file runs a small web server on 127.0.0.1, so only this PC can reach it.
# The page in ui/ connects over a WebSocket. Python pushes events to the page (state, chat messages,
# stats, weather, Spotify) and the page sends commands back (typed messages, buttons, settings).
# The voice loop from jarvis.py runs in a background thread and shares one Brain with the chat box.

import argparse
import asyncio
import json
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

import dashboard
import tools
from brain import Brain, for_speech

HERE = Path(__file__).parent
UI_DIR = HERE / "ui"
SETTINGS_FILE = HERE / "settings.json"
DEFAULT_PORT = 8765

DEFAULT_SETTINGS = {
    "speak_typed": False,           # also say replies to typed messages out loud
    "show_tools": True,             # show which tools Jarvis used under his replies
    "weather_city": "Raleigh, NC",
    "units": "imperial",            # "imperial" (°F, mph) or "metric" (°C, km/h)
}

LEVEL_GAIN = {"wake": 30.0, "listen": 14.0, "speak": 5.0}   # evens out loudness so the orb moves the same for each
STATS_EVERY, WEATHER_EVERY = 2, 600                          # seconds
SPOTIFY_EVERY, SPOTIFY_OFFLINE_EVERY = 5, 30
CLOSE_GRACE = 10   # seconds to wait for the page to come back (a reload) before quitting when its window closes


# ---------- Settings ----------

def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({key: value for key, value in saved.items() if key in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass   # no settings file yet, or a broken one: use the defaults
    return settings


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ---------- Events to the page ----------

class Hub:
    """Delivers events to every open page. publish() is safe to call from any thread."""

    KEEP_LATEST = {"state", "stats", "weather", "spotify", "settings", "counters"}

    def __init__(self):
        self.loop = None               # the web server's event loop, set when the server starts
        self.queues = set()            # one queue per open page
        self.chat = deque(maxlen=200)  # replayed to a page when it connects or reloads
        self.latest = {}               # newest event of each KEEP_LATEST type, for the same reason

    def publish(self, event: dict):
        if self.loop is None:
            self._deliver(event)
        else:
            self.loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: dict):   # runs on the event loop
        kind = event["type"]
        if kind == "message":
            self.chat.append(event)
        elif kind == "clear":
            self.chat.clear()
        elif kind in self.KEEP_LATEST:
            self.latest[kind] = event
        for queue in list(self.queues):
            if kind == "level" and queue.qsize() > 20:
                continue   # a busy page can skip a few animation frames
            queue.put_nowait(event)

    def snapshot(self) -> list:
        return list(self.latest.values()) + list(self.chat)


# ---------- Jarvis himself ----------

class Jarvis:
    """One Brain shared by the voice loop and the chat box, plus everything the dashboard shows."""

    def __init__(self, hub: Hub, settings: dict, voice_enabled: bool, quit_with_window: bool):
        self.hub = hub
        self.settings = settings
        self.voice_enabled = voice_enabled
        self.quit_with_window = quit_with_window
        self.server = None

        self.brain = Brain()
        self.brain.on_event = lambda event: hub.publish({"type": "activity", "tool": event.get("name")})
        self.brain_lock = threading.Lock()     # one request to Claude at a time
        self.speech_lock = threading.Lock()    # one voice at a time

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

    # --- starting and stopping ---

    def start(self):
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
        self.stop_speaking()

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

    # --- what the orb and status line show ---

    def _publish_state(self):
        state = "thinking" if self.thinking else "speaking" if self.speaking else self.voice_state
        self.hub.publish({"type": "state", "state": state, "mic_on": self.mic_on,
                          "voice": self.voice_enabled, "detail": self.detail})

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

    # --- talking to Claude ---

    def _chat(self, role: str, text: str, **extra):
        self.hub.publish({"type": "message", "id": uuid.uuid4().hex, "role": role, "text": text,
                          "time": time.time(), **extra})

    def ask(self, text: str, source: str) -> str:
        """Send one request to Claude and post both sides to the chat. Returns what Jarvis would say."""
        self._chat("user", text, source=source)
        self.commands += 1
        self._publish_counters()
        error = None
        with self.brain_lock:
            self.thinking += 1
            self._publish_state()
            try:
                reply = self.brain.think(text)
                links, shots, used = self.brain.last_links, self.brain.last_screenshots, self.brain.last_tools
            except Exception as e:   # API hiccup, no internet, etc.
                print(f"⚠️ {e}")
                reply, links, shots, used, error = "Something went wrong on my end, sir.", [], [], [], str(e)[:300]
            finally:
                self.thinking -= 1
        said = for_speech(reply) or ("It's in the chat, sir." if links or shots else "Done, sir.")
        self._chat("jarvis", said, source=source, links=links, screenshots=shots,
                   tools=list(dict.fromkeys(used)), error=error)
        self._publish_state()
        return said

    def _typed_worker(self):
        """Handles typed messages one at a time, so the page never waits on Claude."""
        while True:
            text = self.typed.get()
            if text is None:
                return
            try:
                said = self.ask(text, "typed")
                if self.settings["speak_typed"]:
                    self.speak(said)
            except Exception as e:
                print(f"⚠️ {e}")

    def clear_conversation(self):
        with self.brain_lock:
            self.brain.messages.clear()   # Jarvis forgets this conversation (memory.json facts stay)
        self.hub.publish({"type": "clear"})

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
            if key in ("speak_typed", "show_tools") and isinstance(value, bool):
                pass
            elif key == "weather_city" and isinstance(value, str) and value.strip():
                value = value.strip()[:80]
            elif key == "units" and value in ("imperial", "metric"):
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
        for event in hub.snapshot():
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
    run_in_thread = asyncio.to_thread   # anything slow runs off the event loop so the page stays responsive
    if kind == "send":
        text = str(command.get("text", "")).strip()[:4000]
        if text:
            jarvis.typed.put(text)
    elif kind == "talk":
        jarvis.talk_button()
    elif kind == "mic":
        jarvis.set_mic(bool(command.get("on")))
    elif kind == "stop":
        jarvis.stop_speaking()
    elif kind == "clear":
        await run_in_thread(jarvis.clear_conversation)
    elif kind == "settings" and isinstance(command.get("values"), dict):
        await run_in_thread(jarvis.update_settings, command["values"])
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
    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    hub = Hub()
    jarvis = Jarvis(hub, load_settings(), voice_enabled=not args.no_voice, quit_with_window=not args.no_window)
    server = uvicorn.Server(uvicorn.Config(create_app(jarvis, hub, port), host="127.0.0.1", port=port,
                                           log_level="warning"))
    jarvis.server = server
    if not args.no_window:
        threading.Thread(target=open_window, args=(server, url, args.browser), daemon=True).start()
    print(f"Jarvis dashboard: {url}   (Ctrl+C to quit)")
    server.run()
    jarvis.stop()


if __name__ == "__main__":
    main()
