// chat.js — the conversation side of the dashboard: the chats sidebar, messages as they stream in, visuals,
// files, approval cards, attachments, export, and the shared memory list.
"use strict";

const TOOL_NAMES = {
  git_status: "Git status", git_commit_all: "Git commit", git_push: "Git push", git_pull: "Git pull",
  read_file: "Read file", write_file: "Write file", edit_file: "Edit file", find_files: "Find files",
  search_text: "Search files", run_command: "Run command", run_python: "Run Python", list_files: "List files",
  open_app: "Open app", open_path: "Open file", run_program: "Run program", create_note: "New note",
  add_to_note: "Add to note", spotify_play: "Spotify", spotify_queue: "Spotify queue", spotify_control: "Spotify",
  remember: "Memory", forget: "Memory", web_search: "Web search", web_fetch: "Read web page", show_links: "Links",
  screenshot: "Screenshot", create_visual: "Visual", show_file: "Show file", search_chats: "Search chats",
  answer_in_chat: "Answer in chat",
};

const Chat = {
  chats: [],
  activeId: null,
  active: null,
  messages: [],
  models: [],
  memory: [],
  streams: new Map(),      // reply id -> the reply being written
  approvals: new Map(),    // approval id -> request waiting for a decision
  attachments: [],         // files waiting to be sent
  generating: false,
  searchQuery: "",
  searchResults: null,
  panelVisual: null,
};

const toolName = (name) => TOOL_NAMES[name] || name;
const modelLabel = (id) => Chat.models.find((m) => m.id === id)?.label || id;

function chatHandle(event) {
  switch (event.type) {
    case "models": return setModels(event.models);
    case "chats": {
      Chat.chats = event.chats;
      const open = Chat.chats.find((c) => c.id === Chat.activeId);
      if (open) {
        Chat.active = open;        // a rename, or the title Jarvis just wrote for a new chat
        renderChatHeader();
      }
      renderChatList();
      renderSwitcher();
      return;
    }
    case "chat_open": return openChat(event.chat, event.messages);
    case "chat_updated":
      if (event.chat?.id === Chat.activeId) {
        Chat.active = event.chat;
        renderChatHeader();
      }
      return;
    case "chat_search":
      if (event.query === Chat.searchQuery) {
        Chat.searchResults = event.results;
        renderChatList();
      }
      return;
    case "message": return receiveMessage(event);
    case "message_start": return startReply(event);
    case "delta": return applyDelta(event);
    case "activity": return showActivity(event);
    case "approval": return showApproval(event);
    case "approval_done": return approvalDone(event);
    case "memory":
      Chat.memory = event.facts || [];
      renderMemory();
      return;
  }
}

// ---------- opening chats ----------

function setModels(models) {
  Chat.models = models;
  for (const select of [$("model-select"), $("set-new-model")]) {
    if (!select) continue;
    const current = select.value;
    select.replaceChildren(...models.map((m) => {
      const option = el("option", "", m.label);
      option.value = m.id;
      option.title = m.note;
      return option;
    }));
    if (current) select.value = current;
  }
  renderChatHeader();
}

function openChat(chat, messages) {
  if (!chat) return;
  const log = $("chat-log");
  const switched = chat.id !== Chat.activeId;
  Chat.active = chat;
  Chat.activeId = chat.id;
  Chat.messages = [];
  log.replaceChildren();
  for (const stream of Chat.streams.values()) stream.row = null;
  renderChatHeader();
  for (const message of messages || []) addMessage(message, false);
  if (!Chat.messages.length) renderWelcome();
  for (const [id, stream] of Chat.streams) if (stream.chatId === chat.id) mountStream(id, stream);
  for (const approval of Chat.approvals.values()) if (approval.chat_id === chat.id) placeApproval(approval);
  log.scrollTop = log.scrollHeight;
  renderChatList();
  renderSwitcher();
  if (switched) closeVisualPanel();
}

function renderChatHeader() {
  const chat = Chat.active;
  if (!chat) return;
  $("chat-title").textContent = chat.title;
  document.title = chat.title && chat.title !== "New chat" ? `${chat.title} · J.A.R.V.I.S` : "J.A.R.V.I.S";
  if (Chat.models.length) $("model-select").value = chat.model;
  $("model-select").title = Chat.models.find((m) => m.id === chat.model)?.note || "";
  $("think-toggle").checked = !!chat.think;
}

function renderWelcome() {
  const hour = new Date().getHours();
  const part = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  const box = el("div", "welcome");
  box.id = "welcome";
  box.append(el("div", "welcome-title", `Good ${part}, sir.`), el("p", "welcome-sub", "Say “Hey Jarvis”, or type below. I can code, run things on your PC (with your approval), search the web, look at your screen, and draw diagrams and charts."));
  const ideas = el("div", "welcome-ideas");
  for (const idea of ["Explain how recursion works, with a diagram", "Help me debug the C code in my CSC 230 folder", "Chart the next week's weather for Raleigh", "Take a screenshot of my browser and summarize it"]) {
    ideas.append(button(idea, null, () => {
      $("chat-text").value = idea;
      autosize();
      $("chat-text").focus();
    }, "idea"));
  }
  box.append(ideas);
  $("chat-log").append(box);
}

// ---------- messages ----------

function nearBottom(log) {
  return log.scrollHeight - log.scrollTop - log.clientHeight < 160;
}

function receiveMessage(message) {
  if (message.role === "jarvis") {
    const stream = Chat.streams.get(message.id);
    if (stream) {
      clearTimeout(stream.timer);
      stream.row?.remove();
      Chat.streams.delete(message.id);
    }
  }
  if (message.chat_id !== Chat.activeId) return;
  addMessage(message, true);
}

function addMessage(message, live) {
  const log = $("chat-log");
  const stick = !live || nearBottom(log) || message.role === "user";
  Chat.messages.push(message);
  $("welcome")?.remove();
  const row = message.role === "user" ? userRow(message) : jarvisRow(message);
  const streaming = log.querySelector(".msg.streaming");
  if (streaming && message.role === "user") log.insertBefore(row, streaming);
  else log.append(row);
  refreshRetryButtons();
  if (stick) log.scrollTop = log.scrollHeight;
}

function userRow(m) {
  const row = el("div", "msg from-user");
  row.dataset.id = m.id;
  const bubble = el("div", "bubble");
  if (m.text) bubble.append(el("p", "msg-text", m.text));
  if (m.attachments?.length) {
    const box = el("div", "sent-files");
    for (const file of m.attachments) {
      if (file.kind === "image" && /^data:image\/jpeg;base64,/.test(file.image || "")) {
        const img = new Image();
        img.src = file.image;
        img.alt = file.name;
        img.title = file.name;
        box.append(img);
      } else {
        const chip = el("span", "file-chip");
        chip.append(icon(file.kind === "pdf" ? "i-file" : "i-code"), el("span", "", file.name));
        box.append(chip);
      }
    }
    bubble.append(box);
  }
  const meta = el("div", "msg-meta");
  meta.append(el("span", "", timeOf(m.time)));
  if (m.source === "voice") meta.append(el("span", "tag", "voice"));
  bubble.append(meta);
  row.append(bubble, messageActions([
    ["Copy", "i-copy", (e) => copyText(m.text, e.currentTarget)],
    ["Edit", "i-pen", () => editMessage(row, m)],
  ]));
  return row;
}

function jarvisRow(m) {
  const row = el("div", "msg from-jarvis");
  row.dataset.id = m.id;
  const bubble = el("div", "bubble");
  if (m.thinking) bubble.append(thinkingBox(m.thinking, false));
  bubble.append(Markdown.render(m.text));   // spoken replies are written out in full here too
  appendExtras(bubble, m);

  const meta = el("div", "msg-meta");
  meta.append(el("span", "", timeOf(m.time)));
  if (m.model && m.source !== "voice") meta.append(el("span", "tag model", modelLabel(m.model)));
  if (m.source === "voice") {
    const tag = el("span", "tag", m.spoken && m.spoken !== m.text ? "read aloud: short" : "spoken");
    if (m.spoken) tag.title = `Out loud: “${m.spoken}”`;
    meta.append(tag);
  }
  for (const tool of m.tools || []) meta.append(el("span", "tag tool", toolName(tool)));
  if (m.stopped) meta.append(el("span", "tag warn", "stopped"));
  if (m.error) {
    const tag = el("span", "tag error", "error");
    tag.title = m.error;
    meta.append(tag);
  }
  bubble.append(meta);
  if (m.error) bubble.append(el("p", "error-detail", m.error));
  row.append(bubble, messageActions([
    ["Copy", "i-copy", (e) => copyText(m.text, e.currentTarget)],
    ["Retry", "i-refresh", () => send({ type: "retry", chat_id: m.chat_id }), "retry"],
  ]));
  return row;
}

function appendExtras(bubble, m) {
  for (const visual of m.visuals || []) bubble.append(Visuals.card(visual, openVisual));

  const shots = (m.screenshots || []).filter((shot) => /^data:image\/jpeg;base64,/.test(shot.image || ""));
  if (shots.length) {
    const box = el("div", "shots");
    for (const shot of shots) {
      const b = el("button", "shot");
      b.type = "button";
      const img = new Image();
      img.src = shot.image;
      img.alt = `Screenshot of ${shot.label}`;
      b.append(img, el("span", "shot-label", shot.label));
      b.addEventListener("click", () => openLightbox(shot));
      box.append(b);
    }
    bubble.append(box);
  }

  for (const file of m.files || []) bubble.append(fileCard(file, openFile));

  const links = (m.links || []).map((link) => ({ ...link, parsed: safeUrl(link.url) })).filter((l) => l.parsed);
  if (links.length) {
    const box = el("div", "links");
    for (const link of links) {
      const host = link.parsed.hostname.replace(/^www\./, "");
      const a = el("a", "link-chip");
      a.href = link.parsed.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.title = link.parsed.href;
      a.append(icon("i-link"), el("span", "link-title", link.title && link.title !== link.url ? link.title : host), el("span", "link-host", host));
      box.append(a);
    }
    bubble.append(box);
  }
}

function thinkingBox(text, open) {
  const details = el("details", "thinking");
  details.open = open;
  const summary = el("summary");
  summary.append(icon("i-brain"), el("span", "", "Thought process"));
  details.append(summary, el("div", "thinking-text", text));
  return details;
}

function messageActions(items) {
  const bar = el("div", "msg-actions");
  for (const [label, iconName, onClick, extra] of items) {
    const b = button(label, iconName, onClick, `mini-pill ${extra || ""}`);
    b.title = label;
    bar.append(b);
  }
  return bar;
}

function refreshRetryButtons() {
  const rows = [...$("chat-log").querySelectorAll(".msg.from-jarvis:not(.streaming)")];
  rows.forEach((row, i) => row.querySelector(".retry")?.toggleAttribute("hidden", i !== rows.length - 1 || Chat.generating));
}

function editMessage(row, m) {
  if (Chat.generating || row.querySelector(".edit-box")) return;
  const bubble = row.querySelector(".bubble");
  const box = el("div", "edit-box");
  const area = el("textarea");
  area.value = m.text;
  const save = button("Send", "i-send", () => {
    if (area.value.trim()) send({ type: "edit", chat_id: m.chat_id, message_id: m.id, text: area.value.trim() });
  }, "pill-btn primary");
  const cancel = button("Cancel", null, () => {
    box.remove();
    bubble.hidden = false;
  });
  const note = el("p", "edit-note", "Sending replaces this message and everything after it.");
  const buttons = el("div", "edit-buttons");
  buttons.append(cancel, save);
  box.append(area, note, buttons);
  bubble.hidden = true;
  row.insertBefore(box, bubble);
  area.focus();
  area.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      save.click();
    } else if (e.key === "Escape") cancel.click();
  });
}

// ---------- replies as they're written ----------

function startReply(event) {
  const stream = { chatId: event.chat_id, text: "", thinking: "", activity: "", row: null, timer: null };
  Chat.streams.set(event.id, stream);
  if (event.chat_id === Chat.activeId) mountStream(event.id, stream);
}

function mountStream(id, stream) {
  const log = $("chat-log");
  $("welcome")?.remove();
  const row = el("div", "msg from-jarvis streaming");
  row.dataset.id = id;
  const bubble = el("div", "bubble");
  const thinking = thinkingBox("", true);
  thinking.hidden = true;
  const body = el("div", "live-body");
  const status = el("div", "live-status");
  const dots = el("span", "dots");
  dots.append(el("i"), el("i"), el("i"));
  status.append(dots, el("span", "live-activity", "Thinking…"));
  bubble.append(thinking, body, status);
  row.append(bubble);
  Object.assign(stream, { row, bubble, thinkingBox: thinking, body, status });
  log.append(row);
  paintStream(stream);
  log.scrollTop = log.scrollHeight;
}

function applyDelta(event) {
  const stream = Chat.streams.get(event.id);
  if (!stream) return;
  if (event.text) stream.text += event.text;
  if (event.thinking) stream.thinking += event.thinking;
  if (event.text) stream.activity = "";
  if (stream.row && !stream.timer) {
    stream.timer = setTimeout(() => {
      stream.timer = null;
      paintStream(stream);
    }, 90);   // re-render a few times a second, not on every word
  }
}

function paintStream(stream) {
  if (!stream.row) return;
  const log = $("chat-log");
  const stick = nearBottom(log);
  if (stream.thinking) {
    stream.thinkingBox.hidden = false;
    stream.thinkingBox.querySelector(".thinking-text").textContent = stream.thinking;
    if (stream.text) stream.thinkingBox.open = false;
  }
  const text = stream.text.trimStart();
  stream.body.replaceChildren(...(text ? [Markdown.render(text, { streaming: true })] : []));
  stream.status.querySelector(".live-activity").textContent = stream.activity || (text ? "" : stream.thinking ? "Thinking…" : "Working on it…");
  stream.status.classList.toggle("quiet", !!text && !stream.activity);
  if (stick) log.scrollTop = log.scrollHeight;
}

function showActivity(event) {
  const stream = Chat.streams.get(event.id);
  if (stream) {
    stream.activity = event.done ? "" : `${toolName(event.tool)}…`;
    paintStream(stream);
  }
  if (typeof renderActivity === "function" && !event.done) renderActivity(event.tool);
}

// ---------- approvals ----------

function showApproval(approval) {
  Chat.approvals.set(approval.id, approval);
  if (approval.chat_id === Chat.activeId) placeApproval(approval);
  else toast(`Jarvis needs your approval in another chat.`, "Open", () => send({ type: "chat_open", chat_id: approval.chat_id }));
  if (typeof notifyApproval === "function") notifyApproval(approval);
}

function placeApproval(approval) {
  const log = $("chat-log");
  if (log.querySelector(`.approval-card[data-id="${approval.id}"]`)) return;
  const card = approvalCard(approval, (id, decision) => send({ type: "approve", id, decision }));
  const stream = [...Chat.streams.values()].find((s) => s.chatId === approval.chat_id && s.row);
  if (stream) stream.bubble.insertBefore(card, stream.status);
  else log.append(card);
  card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function approvalDone(event) {
  Chat.approvals.delete(event.id);
  settleApproval($("chat-log").querySelector(`.approval-card[data-id="${event.id}"]`), event.decision, event.via);
}

// ---------- composer ----------

function autosize() {
  const area = $("chat-text");
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight, 220)}px`;
}

function setGenerating(generating) {
  Chat.generating = generating;
  const sendButton = $("send-btn");
  sendButton.classList.toggle("stop", generating);
  sendButton.setAttribute("aria-label", generating ? "Stop" : "Send");
  sendButton.title = generating ? "Stop (Esc)" : "Send (Enter)";
  setIcon(sendButton.querySelector("svg"), generating ? "i-stop" : "i-send");
  refreshRetryButtons();
}

function submitComposer() {
  if (Chat.generating) {
    send({ type: "stop" });
    return;
  }
  const input = $("chat-text");
  const text = input.value.trim();
  if (!text && !Chat.attachments.length) return;
  send({
    type: "send",
    chat_id: Chat.activeId,
    text,
    attachments: Chat.attachments.map(({ name, type, data }) => ({ name, type, data })),
  });
  input.value = "";
  autosize();
  for (const file of Chat.attachments) if (file.preview) URL.revokeObjectURL(file.preview);
  Chat.attachments = [];
  renderAttachments();
}

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function addFiles(files) {
  for (const file of [...files]) {
    if (Chat.attachments.length >= 10) {
      toast("Ten files at a time, sir.");
      break;
    }
    if (file.size > 25 * 1024 * 1024) {
      toast(`${file.name} is over 25 MB.`);
      continue;
    }
    try {
      const data = await readBase64(file);
      Chat.attachments.push({ name: file.name || "pasted.png", type: file.type, size: file.size, data,
        preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : null });
    } catch {
      toast(`Couldn't read ${file.name}.`);
    }
  }
  renderAttachments();
}

function renderAttachments() {
  const box = $("attachments");
  box.hidden = !Chat.attachments.length;
  box.replaceChildren(...Chat.attachments.map((file, index) => {
    const chip = el("div", "attach-chip");
    if (file.preview) {
      const img = new Image();
      img.src = file.preview;
      img.alt = "";
      chip.append(img);
    } else {
      chip.append(icon("i-file"));
    }
    chip.append(el("span", "attach-name", file.name), el("span", "attach-size", formatSize(file.size)));
    const remove = button(null, "i-close", () => {
      if (file.preview) URL.revokeObjectURL(file.preview);
      Chat.attachments.splice(index, 1);
      renderAttachments();
    }, "attach-remove");
    remove.title = `Remove ${file.name}`;
    chip.append(remove);
    return chip;
  }));
}

// ---------- sidebar and chat switcher ----------

function chatGroup(chat) {
  if (chat.pinned) return "Pinned";
  const day = 86400;
  const startOfToday = new Date().setHours(0, 0, 0, 0) / 1000;
  if (chat.updated >= startOfToday) return "Today";
  if (chat.updated >= startOfToday - day) return "Yesterday";
  if (chat.updated >= startOfToday - 7 * day) return "Previous 7 days";
  if (chat.updated >= startOfToday - 30 * day) return "Previous 30 days";
  return "Older";
}

function renderChatList() {
  const list = $("chat-list");
  if (!list) return;
  list.replaceChildren();
  const searching = Chat.searchResults !== null && Chat.searchQuery.trim();
  const chats = searching ? Chat.searchResults : Chat.chats;
  if (!chats.length) {
    list.append(el("p", "list-empty", searching ? "No chats match." : "No chats yet."));
    return;
  }
  let currentGroup = null;
  for (const chat of chats) {
    const group = searching ? "Results" : chatGroup(chat);
    if (group !== currentGroup) {
      list.append(el("div", "list-group", group));
      currentGroup = group;
    }
    list.append(chatItem(chat));
  }
}

function chatItem(chat) {
  const item = el("div", `chat-item${chat.id === Chat.activeId ? " active" : ""}`);
  const open = el("button", "chat-open");
  open.type = "button";
  open.append(el("span", "chat-item-title", chat.title));
  if (chat.snippet) open.append(el("span", "chat-item-snippet", chat.snippet.replace(/[«»]/g, "")));
  open.addEventListener("click", () => send({ type: "chat_open", chat_id: chat.id }));
  const actions = el("div", "chat-item-actions");
  const pin = button(null, "i-pin", () => send({ type: "chat_pin", chat_id: chat.id, pinned: !chat.pinned }), `item-btn${chat.pinned ? " on" : ""}`);
  pin.title = chat.pinned ? "Unpin" : "Pin";
  const rename = button(null, "i-pen", () => renameInline(item, chat), "item-btn");
  rename.title = "Rename";
  const remove = button(null, "i-trash", () => {
    if (remove.classList.contains("confirm")) send({ type: "chat_delete", chat_id: chat.id });
    else {
      remove.classList.add("confirm");
      remove.title = "Click again to delete";
      setTimeout(() => remove.classList.remove("confirm"), 3000);
    }
  }, "item-btn danger");
  remove.title = "Delete";
  actions.append(pin, rename, remove);
  item.append(open, actions);
  return item;
}

function renameInline(item, chat) {
  const input = el("input", "rename-input");
  input.value = chat.title;
  item.replaceChildren(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save && input.value.trim() && input.value.trim() !== chat.title) send({ type: "chat_rename", chat_id: chat.id, title: input.value.trim() });
    renderChatList();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

function renderSwitcher() {
  const list = $("switcher-list");
  if (!list) return;
  list.replaceChildren(...Chat.chats.slice(0, 8).map((chat) => {
    const b = el("button", `switcher-item${chat.id === Chat.activeId ? " active" : ""}`);
    b.type = "button";
    b.append(el("span", "", chat.title), el("span", "switcher-when", chatGroup(chat)));
    b.addEventListener("click", () => {
      $("chat-switcher").hidden = true;
      send({ type: "chat_open", chat_id: chat.id });
    });
    return b;
  }));
}

// ---------- visuals, files, screenshots ----------

function openVisual(visual) {
  if (document.body.dataset.view === "chats") {
    Chat.panelVisual = visual;
    $("visual-panel").hidden = false;
    $("panel-title").textContent = visual.title || Visuals.KIND_LABEL[visual.kind];
    showPanel("preview");
  } else {
    $("visual-dialog-title").textContent = visual.title || Visuals.KIND_LABEL[visual.kind];
    const body = $("visual-dialog-body");
    body.replaceChildren();
    $("visual-dialog").showModal();
    Visuals.draw(body, visual);
  }
}

function showPanel(mode) {
  const visual = Chat.panelVisual;
  if (!visual) return;
  const body = $("panel-body");
  body.replaceChildren();
  $("panel-source").classList.toggle("on", mode === "source");
  $("panel-preview").classList.toggle("on", mode === "preview");
  if (mode === "source") {
    const language = { mermaid: "mermaid", chart: "json", svg: "xml", html: "html" }[visual.kind] || "";
    body.append(codeBlock(visual.content, language));
  } else {
    const holder = el("div", "panel-visual");
    body.append(holder);
    Visuals.draw(holder, visual);
  }
}

function closeVisualPanel() {
  Chat.panelVisual = null;
  const panel = $("visual-panel");
  if (panel) {
    panel.hidden = true;
    $("panel-body").replaceChildren();
  }
}

function openFile(path, reveal) {
  send({ type: "open_file", path, reveal });
}

function openLightbox(shot) {
  $("lightbox-img").src = shot.image;
  $("lightbox-img").alt = `Screenshot of ${shot.label}`;
  $("lightbox-label").textContent = shot.label;
  $("lightbox").showModal();
}

// ---------- export, memory, toasts ----------

function exportChat() {
  if (!Chat.messages.length) return toast("Nothing to export yet.");
  const lines = [`# ${Chat.active?.title || "Conversation with Jarvis"}`, "", `Exported ${new Date().toLocaleString()}`, ""];
  for (const m of Chat.messages) {
    const who = m.role === "user" ? "You" : "Jarvis";
    lines.push(`## ${who}${m.source === "voice" ? " (voice)" : ""} · ${timeOf(m.time)}`, "", m.text || "", "");
    for (const file of m.attachments || []) lines.push(`*Attached: ${file.name}*`);
    for (const visual of m.visuals || []) {
      lines.push(`**${Visuals.KIND_LABEL[visual.kind] || "Visual"}: ${visual.title}**`, "", "```" + ({ chart: "json", svg: "xml" }[visual.kind] || visual.kind), visual.content, "```", "");
    }
    for (const file of m.files || []) lines.push(`*${ACTION_LABEL[file.action] || file.action}: ${file.path}*`);
    for (const shot of m.screenshots || []) lines.push(`*Screenshot: ${shot.label}*`);
    for (const link of m.links || []) lines.push(`- [${String(link.title || link.url).replace(/[[\]]/g, "")}](${link.url})`);
    lines.push("");
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const a = el("a");
  const d = new Date();
  const slug = (Chat.active?.title || "chat").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "chat";
  a.href = URL.createObjectURL(blob);
  a.download = `jarvis-${slug}-${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}.md`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function renderMemory() {
  const count = $("memory-count");
  if (count) count.textContent = Chat.memory.length;
  const list = $("memory-list");
  if (!list) return;
  if (!Chat.memory.length) {
    list.replaceChildren(el("li", "memory-empty", "Nothing yet. Tell Jarvis something worth remembering, or add it here."));
    return;
  }
  list.replaceChildren(...Chat.memory.map((fact, index) => {
    const item = el("li", "memory-item");
    const remove = button(null, "i-trash", () => send({ type: "memory_delete", index }), "item-btn danger");
    remove.title = "Forget this";
    item.append(el("span", "", fact), remove);
    return item;
  }));
}

let toastTimer = null;
function toast(text, actionLabel, action) {
  const box = $("toast");
  box.replaceChildren(el("span", "", text));
  if (actionLabel) box.append(button(actionLabel, null, () => {
    box.hidden = true;
    action();
  }, "mini-pill"));
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 5000);
}

// ---------- wiring ----------

function wireChat() {
  const input = $("chat-text");
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitComposer();
  });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!Chat.generating) submitComposer();
    }
  });
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  });
  $("attach-btn").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (event) => {
    addFiles(event.target.files);
    event.target.value = "";
  });

  const card = $("chat-card");
  let dragDepth = 0;
  card.addEventListener("dragenter", (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    e.preventDefault();
    dragDepth++;
    $("drop-overlay").hidden = false;
  });
  card.addEventListener("dragover", (e) => e.preventDefault());
  card.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) {
      dragDepth = 0;
      $("drop-overlay").hidden = true;
    }
  });
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("drop-overlay").hidden = true;
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
  });

  $("model-select").addEventListener("change", (e) => send({ type: "chat_settings", chat_id: Chat.activeId, model: e.target.value }));
  $("think-toggle").addEventListener("change", (e) => send({ type: "chat_settings", chat_id: Chat.activeId, think: e.target.checked }));
  $("new-chat-btn").addEventListener("click", () => send({ type: "chat_new" }));
  $("sidebar-new").addEventListener("click", () => send({ type: "chat_new" }));
  $("export-btn").addEventListener("click", exportChat);

  $("chat-title-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    const switcher = $("chat-switcher");
    switcher.hidden = !switcher.hidden;
  });
  $("switcher-new").addEventListener("click", () => {
    $("chat-switcher").hidden = true;
    send({ type: "chat_new" });
  });
  $("switcher-all").addEventListener("click", () => {
    $("chat-switcher").hidden = true;
    setView("chats");
  });
  document.addEventListener("click", (event) => {
    if (!$("chat-switcher").hidden && !event.target.closest("#chat-switcher")) $("chat-switcher").hidden = true;
  });

  let searchTimer = null;
  $("chat-search").addEventListener("input", (event) => {
    Chat.searchQuery = event.target.value;
    clearTimeout(searchTimer);
    if (!Chat.searchQuery.trim()) {
      Chat.searchResults = null;
      renderChatList();
      return;
    }
    searchTimer = setTimeout(() => send({ type: "chat_search", query: Chat.searchQuery }), 220);
  });

  $("memory-btn").addEventListener("click", () => $("memory-dialog").showModal());
  $("memory-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const fact = $("memory-input").value.trim();
    if (fact) send({ type: "memory_add", fact });
    $("memory-input").value = "";
  });

  $("panel-close").addEventListener("click", closeVisualPanel);
  $("panel-source").addEventListener("click", () => showPanel("source"));
  $("panel-preview").addEventListener("click", () => showPanel("preview"));
  $("lightbox").addEventListener("click", (event) => {
    if (event.target === $("lightbox")) $("lightbox").close();
  });
}
