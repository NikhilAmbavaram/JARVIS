// app.js — the dashboard page. It connects to gui.py over a WebSocket, draws the events Python
// sends (state, chat, stats, weather, Spotify), and sends back what you type and click.
"use strict";

const $ = (id) => document.getElementById(id);

const TOOL_NAMES = {
  git_status: "Git status", git_commit_all: "Git commit", git_push: "Git push", git_pull: "Git pull",
  read_file: "Read file", list_files: "List files", open_app: "Open app", open_path: "Open file",
  run_program: "Run program", create_note: "New note", add_to_note: "Add to note",
  spotify_play: "Spotify", spotify_queue: "Spotify queue", spotify_control: "Spotify",
  remember: "Memory", web_search: "Web search", web_fetch: "Read web page",
  show_links: "Links", screenshot: "Screenshot",
};

const STATUS_TEXT = {
  offline: "Connecting to Jarvis…",
  asleep: "Listening for “Hey Jarvis”…",
  awake: "Awake",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
  muted: "Microphone off",
  "voice-off": "Voice off · chat only",
  error: "Microphone problem",
};

const WEATHER_ICONS = new Set(["clear", "night", "partly", "cloudy", "fog", "rain", "snow", "storm"]);

const app = {
  socket: null,
  connected: false,
  retries: 0,
  state: "offline",
  micOn: false,
  voice: true,
  settings: {},
  messages: [],
  started: null,
  spotify: null,
  spotifyAt: 0,
  level: 0,
  targetLevel: 0,
  activity: null,
};

// ---------- small helpers ----------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${name}`);
  svg.append(use);
  return svg;
}

function setIcon(svg, name) {
  svg.querySelector("use").setAttribute("href", `#${name}`);
}

function safeUrl(text) {
  try {
    const url = new URL(text);
    return url.protocol === "https:" || url.protocol === "http:" ? url : null;
  } catch {
    return null;
  }
}

const pad = (n) => String(n).padStart(2, "0");

function duration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  return `${pad(Math.floor(seconds / 3600))}:${pad(Math.floor(seconds / 60) % 60)}:${pad(seconds % 60)}`;
}

function minutes(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

const timeOf = (unixSeconds) =>
  new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

const gib = (bytes) => bytes / 1024 ** 3;

function setBar(id, percent) {
  $(id).style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
}

// ---------- connection ----------

function connect() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  app.socket = socket;

  socket.addEventListener("open", () => {
    app.connected = true;
    app.retries = 0;
    resetChat();   // the server replays the conversation right after connecting
    setConnected(true);
  });

  socket.addEventListener("message", (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    handle(event);
  });

  socket.addEventListener("close", () => {
    if (app.socket !== socket) return;
    app.connected = false;
    const wait = Math.min(5000, 400 * 2 ** app.retries++);
    setConnected(false);
    renderState({ state: "offline", mic_on: app.micOn, voice: app.voice });
    setTimeout(connect, wait);
  });
}

function send(command) {
  if (app.connected) app.socket.send(JSON.stringify(command));
}

function setConnected(online) {
  $("link-badge").classList.toggle("offline", !online);
  $("link-text").textContent = online ? "Online" : app.retries ? "Offline" : "Connecting";
  $("chat-text").disabled = !online;
  $("send-btn").disabled = !online;
}

function handle(event) {
  switch (event.type) {
    case "state": return renderState(event);
    case "message": return addMessage(event);
    case "clear": return resetChat();
    case "activity": return renderActivity(event.tool);
    case "heard": return showCaption(event.text);
    case "level": app.targetLevel = Math.max(app.targetLevel, event.value || 0); return;
    case "stats": return renderStats(event);
    case "counters": return renderCounters(event);
    case "weather": return renderWeather(event);
    case "spotify": return renderSpotify(event);
    case "settings": return renderSettings(event);
  }
}

// ---------- state: orb, status line, buttons ----------

function renderState(event) {
  const state = event.state || "offline";
  const previous = app.state;
  app.state = state;
  app.micOn = !!event.mic_on;
  app.voice = event.voice !== false;
  if (state !== "thinking") app.activity = null;

  $("orb").dataset.state = state;
  $("status").dataset.state = state;
  let text = STATUS_TEXT[state] || state;
  if (state === "thinking" && app.activity) text = `${TOOL_NAMES[app.activity] || app.activity}…`;
  if (state === "error" && event.detail) text = event.detail;
  $("status-text").textContent = text;
  $("status").title = event.detail || "";

  const micBtn = $("mic-btn");
  micBtn.setAttribute("aria-pressed", String(!app.micOn));
  micBtn.disabled = !app.voice || !app.connected;
  const micLabel = app.micOn ? "Mute microphone" : "Microphone is off (click to turn it on)";
  micBtn.title = micLabel;
  micBtn.setAttribute("aria-label", micLabel);

  const talk = $("talk-btn");
  let talkLabel = "Wake Jarvis";
  let talkIcon = "i-mic";
  if (state === "speaking") [talkLabel, talkIcon] = ["Stop speaking (Esc)", "i-stop"];
  else if (state === "listening" || state === "awake") talkLabel = "Send Jarvis back to sleep";
  else if (state === "muted") talkLabel = "Turn the microphone on and wake Jarvis";
  talk.title = talkLabel;
  talk.setAttribute("aria-label", talkLabel);
  setIcon(talk.querySelector("svg"), talkIcon);
  talk.classList.toggle("active", state === "listening");
  talk.disabled = !app.connected || !app.voice;

  const settingMic = $("set-mic");
  settingMic.checked = app.micOn;
  settingMic.disabled = !app.voice;
  $("mic-hint").textContent = app.voice
    ? "Turn off to stop using the microphone."
    : "Voice is off (started with --no-voice, or the microphone isn't available).";

  if (state === "thinking" || previous === "thinking") renderTyping();
}

function renderActivity(tool) {
  app.activity = tool;
  if (app.state === "thinking") {
    $("status-text").textContent = `${TOOL_NAMES[tool] || tool}…`;
    renderTyping();
  }
}

let captionTimer = null;
function showCaption(text) {
  const caption = $("caption");
  caption.textContent = `“${text}”`;
  caption.classList.add("show");
  clearTimeout(captionTimer);
  captionTimer = setTimeout(() => caption.classList.remove("show"), 6000);
}

// ---------- the orb animation ----------

const bars = [...document.querySelectorAll(".bars span")];
const BAR_SHAPE = [0.55, 0.82, 1, 0.82, 0.55];
const calm = window.matchMedia("(prefers-reduced-motion: reduce)");

function animate(time) {
  app.level += (app.targetLevel - app.level) * 0.3;
  app.targetLevel *= 0.86;   // falls back to quiet unless new levels keep arriving
  const state = app.state;
  let amount;
  if (state === "thinking") amount = 0.45 + 0.2 * Math.sin(time / 220);
  else if (state === "listening" || state === "speaking") amount = 0.14 + app.level;
  else if (state === "asleep" || state === "awake") amount = 0.08 + app.level * 0.5;
  else amount = 0.04;
  if (calm.matches) amount = Math.min(amount, 0.3);

  bars.forEach((bar, i) => {
    const wave = state === "thinking" ? Math.sin(time / 150 - i * 0.9) : Math.sin(time / 280 + i * 1.4);
    const height = 0.16 + amount * BAR_SHAPE[i] * (0.72 + 0.28 * wave);
    bar.style.transform = `scaleY(${Math.min(1, height).toFixed(3)})`;
  });
  $("orb").style.setProperty("--level", Math.min(1, app.level).toFixed(3));
  requestAnimationFrame(animate);
}

// ---------- conversation ----------

function resetChat() {
  app.messages = [];
  $("chat-log").replaceChildren();
  renderGreeting();
  renderTyping();
}

function renderGreeting() {
  if (app.messages.length) return;
  const hour = new Date().getHours();
  const part = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  const row = el("div", "msg from-jarvis greeting");
  row.id = "greeting";
  const bubble = el("div", "bubble");
  bubble.append(el("p", "msg-text", `Good ${part}, sir. Say “Hey Jarvis”, or type below.`));
  row.append(bubble);
  $("chat-log").append(row);
}

function nearBottom(log) {
  return log.scrollHeight - log.scrollTop - log.clientHeight < 120;
}

function addMessage(message) {
  app.messages.push(message);
  const log = $("chat-log");
  const stick = nearBottom(log) || message.role === "user";
  $("greeting")?.remove();

  const fromUser = message.role === "user";
  const row = el("div", `msg ${fromUser ? "from-user" : "from-jarvis"}`);
  const bubble = el("div", "bubble");
  bubble.append(el("p", "msg-text", message.text));

  const shots = (message.screenshots || []).filter((shot) => /^data:image\/jpeg;base64,/.test(shot.image || ""));
  if (shots.length) {
    const box = el("div", "shots");
    for (const shot of shots) {
      const button = el("button", "shot");
      button.type = "button";
      const img = new Image();
      img.src = shot.image;
      img.alt = `Screenshot of ${shot.label}`;
      button.append(img, el("span", "shot-label", shot.label));
      button.addEventListener("click", () => openLightbox(shot));
      box.append(button);
    }
    bubble.append(box);
  }

  const links = (message.links || []).map((link) => ({ ...link, parsed: safeUrl(link.url) })).filter((l) => l.parsed);
  if (links.length) {
    const box = el("div", "links");
    for (const link of links) {
      const host = link.parsed.hostname.replace(/^www\./, "");
      const a = el("a", "link-chip");
      a.href = link.parsed.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.title = link.parsed.href;
      a.append(icon("i-link"), el("span", "link-title", link.title && link.title !== link.url ? link.title : host));
      a.append(el("span", "link-host", host));
      box.append(a);
    }
    bubble.append(box);
  }

  const meta = el("div", "msg-meta");
  meta.append(el("span", "", timeOf(message.time)));
  if (fromUser && message.source === "voice") meta.append(el("span", "tag", "voice"));
  for (const tool of message.tools || []) meta.append(el("span", "tag tool", TOOL_NAMES[tool] || tool));
  if (message.error) {
    const tag = el("span", "tag error", "error");
    tag.title = message.error;
    meta.append(tag);
  }
  bubble.append(meta);
  row.append(bubble);
  log.append(row);
  renderTyping();
  if (stick) log.scrollTop = log.scrollHeight;
}

function renderTyping() {
  const log = $("chat-log");
  let row = $("typing");
  if (app.state !== "thinking") {
    row?.remove();
    return;
  }
  if (!row) {
    row = el("div", "msg from-jarvis typing");
    row.id = "typing";
    const bubble = el("div", "bubble");
    const dots = el("span", "dots");
    dots.append(el("i"), el("i"), el("i"));
    bubble.append(dots, el("span", "typing-text"));
    row.append(bubble);
  }
  row.querySelector(".typing-text").textContent = app.activity ? `${TOOL_NAMES[app.activity] || app.activity}…` : "Thinking…";
  const stick = nearBottom(log);
  log.append(row);   // always the last thing in the log
  if (stick) log.scrollTop = log.scrollHeight;
}

function exportChat() {
  if (!app.messages.length) return flashButton($("export-btn"), "Nothing yet");
  const lines = ["# Conversation with Jarvis", "", `Exported ${new Date().toLocaleString()}`, ""];
  for (const m of app.messages) {
    const who = m.role === "user" ? "You" : "Jarvis";
    const how = m.role === "user" && m.source === "voice" ? " (voice)" : "";
    lines.push(`**${who}**${how} · ${timeOf(m.time)}`, "", m.text, "");
    for (const shot of m.screenshots || []) lines.push(`*Screenshot: ${shot.label}*`, "");
    for (const link of m.links || []) lines.push(`- [${String(link.title || link.url).replace(/[[\]]/g, "")}](${link.url})`);
    if ((m.links || []).length) lines.push("");
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const a = el("a");
  const d = new Date();
  a.href = URL.createObjectURL(blob);
  a.download = `jarvis-conversation-${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.md`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function flashButton(button, text) {
  const label = button.querySelector("span");
  const original = label.textContent;
  label.textContent = text;
  setTimeout(() => (label.textContent = original), 1500);
}

let clearTimer = null;
function clearClicked() {
  const button = $("clear-btn");
  if (button.classList.contains("confirm")) {
    resetClear();
    send({ type: "clear" });
    return;
  }
  if (!app.messages.length) return;
  button.classList.add("confirm");
  $("clear-label").textContent = "Clear all?";
  clearTimer = setTimeout(resetClear, 3000);
}

function resetClear() {
  clearTimeout(clearTimer);
  $("clear-btn").classList.remove("confirm");
  $("clear-label").textContent = "Clear";
}

function openLightbox(shot) {
  $("lightbox-img").src = shot.image;
  $("lightbox-img").alt = `Screenshot of ${shot.label}`;
  $("lightbox-label").textContent = shot.label;
  $("lightbox").showModal();
}

// ---------- side cards ----------

function renderStats(s) {
  const cpu = Math.round(s.cpu);
  setBar("cpu-bar", s.cpu);
  $("cpu-text").textContent = `${cpu}%`;
  setBar("ram-bar", s.ram_percent);
  $("ram-text").textContent = `${gib(s.ram_used).toFixed(1)} / ${gib(s.ram_total).toFixed(1)} GB`;
  $("tile-cpu").textContent = `${cpu}%`;
  $("tile-mem").textContent = `${Math.round(s.ram_percent)}%`;
  $("tile-disk").textContent = `${Math.round(gib(s.disk_used))}/${Math.round(gib(s.disk_total))} GB`;
  $("tile-disk").title = `${Math.round(s.disk_percent)}% used`;

  const load = Math.round(s.load ?? s.cpu);
  setBar("load-bar", load);
  $("load-text").textContent = `${load}%`;
  const [label, kind] = load < 30 ? ["Low", "low"] : load < 70 ? ["Moderate", "moderate"] : ["High", "high"];
  $("load-label").textContent = label;
  $("load-label").className = `load-label ${kind}`;
}

function renderCounters(c) {
  app.started = c.started;
  $("count-commands").textContent = c.commands;
  $("count-conversations").textContent = c.conversations;
  tickUptime();
}

function tickUptime() {
  if (!app.started) return;
  const text = duration(Date.now() / 1000 - app.started);
  $("uptime-big").textContent = text;
  $("uptime-small").textContent = text;
}

function renderWeather(w) {
  if (w.error) {
    $("w-desc").textContent = w.error;
    $("w-desc").classList.add("error");
    $("w-city").textContent = w.city || "";
    return;
  }
  $("w-desc").classList.remove("error");
  $("w-temp").textContent = `${w.temp}${w.temp_unit}`;
  $("w-city").textContent = w.city;
  $("w-desc").textContent = w.description;
  $("w-humidity").textContent = `${w.humidity}%`;
  $("w-wind").textContent = `${w.wind} ${w.wind_unit}`;
  $("w-feels").textContent = `${w.feels_like}${w.temp_unit}`;
  setIcon($("w-icon"), `w-${WEATHER_ICONS.has(w.icon) ? w.icon : "cloudy"}`);
  $("chip-temp").textContent = `${w.temp}${w.temp_unit}`;
  $("chip-city").textContent = String(w.city || "").split(",")[0];
  $("weather-chip").title = `${w.description} in ${w.city}`;
}

function renderSpotify(s) {
  app.spotify = s;
  app.spotifyAt = Date.now();
  const note = $("music-note");
  const title = $("music-title-text");
  const art = $("music-art");

  note.hidden = true;
  if (!s.connected) {
    note.hidden = false;
    note.replaceChildren("Spotify isn't connected yet. The buttons still work as media keys. To connect, run ",
      el("code", "", 'python -c "import tools; tools.spotify_login()"'));
  } else if (s.error) {
    note.hidden = false;
    note.textContent = s.error;
  }

  const playing = !!(s.connected && s.title);
  title.textContent = playing ? s.title : "Nothing playing";
  const link = playing && safeUrl(s.url || "");
  if (link) title.href = link.href;
  else title.removeAttribute("href");
  $("music-artist").textContent = playing ? s.artist || "" : s.connected ? "Start something on Spotify" : " ";
  $("music-device").textContent = playing && s.device ? s.device : "";

  const artUrl = playing && safeUrl(s.art || "");
  if (artUrl) {
    let img = art.querySelector("img");
    if (!img) {
      img = new Image();
      img.alt = "";
      art.replaceChildren(img);
    }
    if (img.src !== artUrl.href) img.src = artUrl.href;
  } else if (!art.querySelector("use")) {
    art.replaceChildren(icon("i-music"));
  }

  const toggle = $("music-toggle");
  setIcon(toggle.querySelector("svg"), s.connected && s.playing ? "i-pause" : "i-play");
  toggle.title = s.connected && s.playing ? "Pause" : "Play";
  tickSpotify();
}

function tickSpotify() {
  const s = app.spotify;
  if (!s || !s.connected || !s.title || !s.duration_ms) {
    setBar("music-bar", 0);
    $("music-pos").textContent = "0:00";
    $("music-len").textContent = "0:00";
    return;
  }
  const position = Math.min(s.duration_ms, s.progress_ms + (s.playing ? Date.now() - app.spotifyAt : 0));
  setBar("music-bar", (position / s.duration_ms) * 100);
  $("music-pos").textContent = minutes(position);
  $("music-len").textContent = minutes(s.duration_ms);
}

// ---------- settings ----------

function renderSettings(s) {
  app.settings = s;
  $("set-speak-typed").checked = !!s.speak_typed;
  $("set-show-tools").checked = !!s.show_tools;
  if (document.activeElement !== $("set-city")) $("set-city").value = s.weather_city || "";
  for (const radio of document.querySelectorAll('input[name="units"]')) radio.checked = radio.value === s.units;
  $("chat-log").classList.toggle("hide-tools", !s.show_tools);
}

function saveSetting(values) {
  send({ type: "settings", values });
}

// ---------- clock ----------

function tickClock() {
  const now = new Date();
  $("clock-time").textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  $("clock-date").textContent = now.toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" });
  tickUptime();
  tickSpotify();
}

// ---------- wiring ----------

function wire() {
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("chat-text");
    const text = input.value.trim();
    if (!text || !app.connected) return;
    send({ type: "send", text });
    input.value = "";
  });

  $("talk-btn").addEventListener("click", () => send({ type: "talk" }));
  $("mic-btn").addEventListener("click", () => send({ type: "mic", on: !app.micOn }));
  $("keys-btn").addEventListener("click", () => $("chat-text").focus());
  $("clear-btn").addEventListener("click", clearClicked);
  $("export-btn").addEventListener("click", exportChat);

  for (const button of document.querySelectorAll("[data-refresh]")) {
    button.addEventListener("click", () => {
      send({ type: "refresh", what: button.dataset.refresh });
      button.classList.remove("spin");
      void button.offsetWidth;   // restart the spin animation
      button.classList.add("spin");
    });
  }
  for (const button of document.querySelectorAll("[data-spotify]")) {
    button.addEventListener("click", () => send({ type: "spotify", action: button.dataset.spotify }));
  }

  $("settings-btn").addEventListener("click", () => $("settings-dialog").showModal());
  $("set-mic").addEventListener("change", (e) => send({ type: "mic", on: e.target.checked }));
  $("set-speak-typed").addEventListener("change", (e) => saveSetting({ speak_typed: e.target.checked }));
  $("set-show-tools").addEventListener("change", (e) => saveSetting({ show_tools: e.target.checked }));
  $("city-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const city = $("set-city").value.trim();
    if (city) saveSetting({ weather_city: city });
    $("w-desc").textContent = "Updating…";
  });
  for (const radio of document.querySelectorAll('input[name="units"]')) {
    radio.addEventListener("change", () => saveSetting({ units: radio.value }));
  }

  $("lightbox").addEventListener("click", (event) => {
    if (event.target === $("lightbox")) $("lightbox").close();   // click outside the picture
  });

  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
    if (event.key === "/" && !typing && !document.querySelector("dialog[open]")) {
      event.preventDefault();
      $("chat-text").focus();
    } else if (event.key === "Escape" && app.state === "speaking" && !document.querySelector("dialog[open]")) {
      send({ type: "stop" });
    }
  });
}

wire();
resetChat();
tickClock();
setInterval(tickClock, 1000);
requestAnimationFrame(animate);
connect();
