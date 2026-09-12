// app.js — the dashboard page. It connects to gui.py over a WebSocket, draws the dashboard events
// (state, stats, weather, Spotify, settings), hands everything about the conversation to chat.js,
// and sends back what you type and click. Helpers ($, el, icon…) come from render.js.
"use strict";

const STATUS_TEXT = {
  offline: "Connecting to Jarvis…",
  asleep: "Listening for “Hey Jarvis”…",
  awake: "Awake",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
  approval: "Waiting for your approval…",
  muted: "Microphone off",
  "voice-off": "Voice off · chat only",
  error: "Microphone problem",
};

const SHORT_STATUS = {
  offline: "Offline", asleep: "Asleep", awake: "Awake", listening: "Listening", thinking: "Thinking",
  speaking: "Speaking", approval: "Your call", muted: "Mic off", "voice-off": "Voice off", error: "Mic problem",
};

const WEATHER_ICONS = new Set(["clear", "night", "partly", "cloudy", "fog", "rain", "snow", "storm"]);
const VIEW_KEY = "jarvis.view";

const app = {
  socket: null,
  connected: false,
  retries: 0,
  state: "offline",
  micOn: false,
  voice: true,
  settings: {},
  started: null,
  spotify: null,
  spotifyAt: 0,
  level: 0,
  targetLevel: 0,
  activity: null,
  view: "dashboard",
};

// ---------- small helpers ----------

function duration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  return `${pad(Math.floor(seconds / 3600))}:${pad(Math.floor(seconds / 60) % 60)}:${pad(seconds % 60)}`;
}

function minutes(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

const gib = (bytes) => bytes / 1024 ** 3;

function setBar(id, percent) {
  $(id).style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
}

// ---------- views ----------

function setView(view) {
  app.view = view === "chats" ? "chats" : "dashboard";
  document.body.dataset.view = app.view;
  $(app.view === "chats" ? "slot-chats" : "slot-dashboard").append($("chat-card"));
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("on", tab.dataset.view === app.view);
    tab.setAttribute("aria-pressed", String(tab.dataset.view === app.view));
  }
  if (app.view === "dashboard") closeVisualPanel();
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
  try {
    localStorage.setItem(VIEW_KEY, app.view);
  } catch {
    /* private browsing: the view just won't be remembered */
  }
}

function savedView() {
  try {
    return localStorage.getItem(VIEW_KEY) === "chats" ? "chats" : "dashboard";
  } catch {
    return "dashboard";
  }
}

// ---------- connection ----------

function connect() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  app.socket = socket;

  socket.addEventListener("open", () => {
    app.connected = true;
    app.retries = 0;
    Chat.streams.clear();     // the server replays everything right after connecting
    Chat.approvals.clear();
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
    case "heard": return showCaption(event.text);
    case "level": app.targetLevel = Math.max(app.targetLevel, event.value || 0); return;
    case "stats": return renderStats(event);
    case "counters": return renderCounters(event);
    case "weather": return renderWeather(event);
    case "spotify": return renderSpotify(event);
    case "settings": return renderSettings(event);
    case "models":
      chatHandle(event);
      if (app.settings.new_chat_model) $("set-new-model").value = app.settings.new_chat_model;
      return;
    default: return chatHandle(event);   // chats, messages, deltas, approvals, memory
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
  if (state === "thinking" && app.activity) text = `${toolName(app.activity)}…`;
  if (state === "error" && event.detail) text = event.detail;
  $("status-text").textContent = text;
  $("status").title = event.detail || "";

  const pill = $("voice-pill");
  pill.dataset.state = state;
  $("voice-pill-text").textContent = state === "thinking" && app.activity ? `${toolName(app.activity)}…` : SHORT_STATUS[state] || state;
  setIcon(pill.querySelector("svg"), state === "speaking" ? "i-stop" : app.micOn ? "i-mic" : "i-mic-off");
  pill.disabled = !app.connected || (!app.voice && state !== "speaking");

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

  setGenerating(!!event.generating);
  if (state === "thinking" || previous === "thinking") app.activity = app.activity || null;
}

function renderActivity(tool) {
  app.activity = tool;
  if (app.state === "thinking") {
    $("status-text").textContent = `${toolName(tool)}…`;
    $("voice-pill-text").textContent = `${toolName(tool)}…`;
  }
}

let captionTimer = null;
function showCaption(text) {
  const caption = $("caption");
  caption.textContent = `“${text}”`;
  caption.classList.add("show");
  clearTimeout(captionTimer);
  captionTimer = setTimeout(() => caption.classList.remove("show"), 6000);
  if (app.view === "chats") toast(`Heard: “${text}”`);
}

// an approval is the one thing Jarvis genuinely can't get on with alone, so it's worth a nudge
function notifyApproval(approval) {
  if (app.view === "dashboard" && approval.chat_id !== Chat.activeId) return;   // chat.js already offered to open it
  flashTitle("Jarvis needs an answer");
}

let titleTimer = null;
function flashTitle(text) {
  const original = document.title;
  clearInterval(titleTimer);
  let on = false;
  titleTimer = setInterval(() => {
    document.title = (on = !on) ? text : original;
    if (!document.hidden && !Chat.approvals.size) {
      clearInterval(titleTimer);
      document.title = original;
    }
  }, 1200);
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

  if (app.view === "dashboard") {
    bars.forEach((bar, i) => {
      const wave = state === "thinking" ? Math.sin(time / 150 - i * 0.9) : Math.sin(time / 280 + i * 1.4);
      const height = 0.16 + amount * BAR_SHAPE[i] * (0.72 + 0.28 * wave);
      bar.style.transform = `scaleY(${Math.min(1, height).toFixed(3)})`;
    });
    $("orb").style.setProperty("--level", Math.min(1, app.level).toFixed(3));
  }
  requestAnimationFrame(animate);
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
  $("set-quiet-answers").checked = !!s.quiet_answers;
  $("set-show-tools").checked = !!s.show_tools;
  if (Chat.models.length) $("set-new-model").value = s.new_chat_model;
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
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  }

  $("talk-btn").addEventListener("click", () => send({ type: "talk" }));
  $("voice-pill").addEventListener("click", () => send({ type: "talk" }));
  $("mic-btn").addEventListener("click", () => send({ type: "mic", on: !app.micOn }));
  $("keys-btn").addEventListener("click", () => $("chat-text").focus());

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
  $("set-quiet-answers").addEventListener("change", (e) => saveSetting({ quiet_answers: e.target.checked }));
  $("set-show-tools").addEventListener("change", (e) => saveSetting({ show_tools: e.target.checked }));
  $("set-new-model").addEventListener("change", (e) => saveSetting({ new_chat_model: e.target.value }));
  $("city-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const city = $("set-city").value.trim();
    if (city) saveSetting({ weather_city: city });
    $("w-desc").textContent = "Updating…";
  });
  for (const radio of document.querySelectorAll('input[name="units"]')) {
    radio.addEventListener("change", () => saveSetting({ units: radio.value }));
  }

  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
    const dialogOpen = document.querySelector("dialog[open]");
    if (event.key === "/" && !typing && !dialogOpen) {
      event.preventDefault();
      $("chat-text").focus();
    } else if (event.key === "Escape" && !dialogOpen) {
      if (Chat.generating || app.state === "speaking") send({ type: "stop" });
      else if (!$("visual-panel").hidden) closeVisualPanel();
    } else if (event.key.toLowerCase() === "o" && event.ctrlKey && event.shiftKey) {
      event.preventDefault();
      send({ type: "chat_new" });
      setView("chats");
    }
  });
}

wire();
wireChat();
setView(savedView());
setGenerating(false);
tickClock();
setInterval(tickClock, 1000);
requestAnimationFrame(animate);
connect();
