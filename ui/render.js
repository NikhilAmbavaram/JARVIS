// render.js — turning Jarvis's replies into things worth reading: Markdown, code, math, diagrams, charts,
// drawings, demos, file cards and approval cards. Plain helpers; chat.js decides where they go.
"use strict";

const $ = (id) => document.getElementById(id);

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

const timeOf = (unixSeconds) =>
  new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function button(label, iconName, onClick, className = "pill-btn") {
  const b = el("button", className);
  b.type = "button";
  if (iconName) b.append(icon(iconName));
  if (label) b.append(el("span", "", label));
  b.addEventListener("click", onClick);
  return b;
}

async function copyText(text, trigger) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = el("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  const label = trigger?.querySelector("span");
  if (label) {
    const before = label.textContent;
    label.textContent = "Copied";
    setTimeout(() => (label.textContent = before), 1400);
  }
}

// ---------- Markdown, code and math ----------

const Markdown = (() => {
  const MATH_HINT = /[\\^_{}=<>]|\\[a-zA-Z]/;              // "$5 and $10" stays money; "$x^2$" becomes math
  const MATH_LOOKS = (tex) => MATH_HINT.test(tex) || /^[a-zA-Z]$/.test(tex) ||
    (!/\s/.test(tex) && /[a-zA-Z]/.test(tex));             // ...and so does "$O(n)$"
  const MARK = "⁣";                        // invisible separator around placeholders

  marked.use({ gfm: true, breaks: false });

  // Math is lifted out before Markdown runs (Markdown would eat its underscores and asterisks),
  // skipping code, where dollar signs are just dollar signs.
  function protectMath(text) {
    const math = [];
    const keep = (tex, display) => `${MARK}MATH${math.push({ tex, display }) - 1}${MARK}`;
    const parts = text.split(/(```[\s\S]*?(?:```|$)|`[^`\n]+`)/g);
    for (let i = 0; i < parts.length; i += 2) {
      parts[i] = parts[i]
        .replace(/\$\$([\s\S]+?)\$\$/g, (_, tex) => keep(tex, true))
        .replace(/\\\[([\s\S]+?)\\\]/g, (_, tex) => keep(tex, true))
        .replace(/\\\((.+?)\\\)/g, (_, tex) => keep(tex, false))
        .replace(/(^|[^\\$\w])\$(?![\s$])([^$\n]+?)(?<!\s)\$(?![\d\w])/g, (whole, before, tex) =>
          MATH_LOOKS(tex) ? before + keep(tex, false) : whole);
    }
    return [parts.join(""), math];
  }

  function render(text, { streaming = false } = {}) {
    const [prepared, math] = protectMath(String(text || ""));
    let html = DOMPurify.sanitize(marked.parse(prepared), { ADD_ATTR: ["target"], FORBID_TAGS: ["style", "form", "input"], FORBID_ATTR: ["style"] });
    html = html.replace(new RegExp(`${MARK}MATH(\\d+)${MARK}`, "g"), (_, index) => {
      const item = math[Number(index)];
      try {
        return katex.renderToString(item.tex, { displayMode: item.display, throwOnError: false });
      } catch {
        return escapeHtml(item.tex);
      }
    });
    const box = el("div", "md");
    box.innerHTML = html;
    enhance(box, streaming);
    return box;
  }

  function escapeHtml(text) {
    const div = el("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function enhance(box, streaming) {
    for (const a of box.querySelectorAll("a[href]")) {
      if (/^(https?:|mailto:)/i.test(a.getAttribute("href"))) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      } else {
        a.removeAttribute("href");
      }
    }
    for (const table of box.querySelectorAll("table")) {
      const wrap = el("div", "table-wrap");
      table.replaceWith(wrap);
      wrap.append(table);
    }
    for (const code of box.querySelectorAll("pre > code")) {
      const language = (code.className.match(/language-([\w+#-]+)/) || [])[1] || "";
      const source = code.textContent;
      if (language === "mermaid" && !streaming) {
        const holder = el("div", "inline-diagram");
        code.parentElement.replaceWith(holder);
        Visuals.draw(holder, { kind: "mermaid", content: source });
        continue;
      }
      code.parentElement.replaceWith(codeBlock(source, language, { highlight: !streaming }));
    }
  }

  return { render };
})();

function codeBlock(source, language, { highlight = true, maxHeight = null } = {}) {
  const block = el("div", "code-block");
  const head = el("div", "code-head");
  head.append(el("span", "code-lang", language || "text"), button("Copy", "i-copy", (e) => copyText(source, e.currentTarget), "mini-pill"));
  const pre = el("pre");
  const code = el("code");
  code.textContent = source;
  if (highlight && language && window.hljs?.getLanguage(language)) {
    code.className = `language-${language}`;
    try {
      hljs.highlightElement(code);
    } catch {
      /* plain text is fine */
    }
  }
  if (maxHeight) pre.style.maxHeight = maxHeight;
  pre.append(code);
  block.append(head, pre);
  return block;
}

// ---------- Visuals: Mermaid, Chart.js, SVG and sandboxed HTML ----------

const Visuals = (() => {
  // Categorical colors in a fixed order, checked for color-blind separation and contrast on this dark surface
  const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  const KIND_ICON = { mermaid: "i-diagram", chart: "i-chart", svg: "i-pen", html: "i-code" };
  const KIND_LABEL = { mermaid: "Diagram", chart: "Chart", svg: "Drawing", html: "Interactive" };
  let mermaidLoading = null;
  let diagramCount = 0;

  function loadMermaid() {
    if (!mermaidLoading) {
      mermaidLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/ui/vendor/mermaid.min.js";
        script.onload = () => {
          window.mermaid.initialize({
            startOnLoad: false,
            securityLevel: "strict",
            theme: "base",
            fontFamily: "Outfit, Segoe UI, sans-serif",
            themeVariables: {
              darkMode: true, background: "transparent", fontSize: "15px",
              primaryColor: "#2a1650", primaryTextColor: "#f3ecff", primaryBorderColor: "#a855f7",
              secondaryColor: "#1f1238", tertiaryColor: "#170d2b", lineColor: "#c084fc", textColor: "#e9dcff",
              mainBkg: "#2a1650", nodeBorder: "#a855f7", clusterBkg: "#170d2b", clusterBorder: "#6d28d9",
              edgeLabelBackground: "#1a0f30", noteBkgColor: "#3b1d6e", noteTextColor: "#f3ecff", noteBorderColor: "#a855f7",
              actorBkg: "#2a1650", actorBorder: "#a855f7", actorTextColor: "#f3ecff", actorLineColor: "#7c3aed",
              signalColor: "#c084fc", signalTextColor: "#e9dcff", labelBoxBkgColor: "#2a1650", labelTextColor: "#f3ecff",
              loopTextColor: "#e9dcff", activationBkgColor: "#3b1d6e", sequenceNumberColor: "#0f0920",
            },
          });
          resolve(window.mermaid);
        };
        script.onerror = () => reject(new Error("the diagram library didn't load"));
        document.head.append(script);
      });
    }
    return mermaidLoading;
  }

  async function mermaidInto(container, code) {
    container.classList.add("visual-loading");
    const id = `diagram-${Date.now()}-${diagramCount++}`;
    try {
      const mermaid = await loadMermaid();
      const { svg } = await mermaid.render(id, code);
      container.innerHTML = svg;   // strict mode: Mermaid sanitizes its own output
    } catch (error) {
      document.getElementById(`d${id}`)?.remove();   // Mermaid leaves its error drawing behind
      container.replaceChildren(el("p", "visual-error", `Couldn't draw this diagram: ${error.message || error}`), codeBlock(code, "mermaid"));
    } finally {
      container.classList.remove("visual-loading");
    }
  }

  function chartInto(container, specText) {
    let spec;
    try {
      spec = typeof specText === "string" ? JSON.parse(specText) : structuredClone(specText);
    } catch (error) {
      container.replaceChildren(el("p", "visual-error", `The chart data isn't valid JSON: ${error.message}`));
      return;
    }
    const type = spec.type || "bar";
    const round = ["pie", "doughnut", "polarArea"].includes(type);
    const datasets = spec.data?.datasets || [];
    datasets.forEach((set, i) => {
      const color = SERIES[i % SERIES.length];
      const kind = set.type || type;
      if (set.backgroundColor === undefined) set.backgroundColor = round ? SERIES : kind === "line" || kind === "radar" ? `${color}33` : color;
      if (set.borderColor === undefined) set.borderColor = round ? "#140c24" : color;
      if (set.borderWidth === undefined) set.borderWidth = round ? 2 : kind === "line" || kind === "radar" ? 2 : 0;
      if (kind === "bar" && set.borderRadius === undefined) set.borderRadius = 4;
      if ((kind === "line" || kind === "radar" || kind === "scatter") && set.pointRadius === undefined) set.pointRadius = 4;
    });
    spec.options = spec.options || {};
    spec.options.responsive = true;
    spec.options.maintainAspectRatio = false;
    spec.options.plugins = spec.options.plugins || {};
    spec.options.plugins.legend = { display: datasets.length > 1 || round, ...(spec.options.plugins.legend || {}) };
    if (!round) {
      spec.options.scales = spec.options.scales || {};
      for (const axis of ["x", "y"]) {
        const scale = (spec.options.scales[axis] = spec.options.scales[axis] || {});
        scale.grid = { color: "rgba(168, 85, 247, 0.12)", ...(scale.grid || {}) };
      }
    }
    Chart.defaults.color = "#cdbdf0";
    Chart.defaults.borderColor = "rgba(168, 85, 247, 0.18)";
    Chart.defaults.font.family = "Outfit, Segoe UI, sans-serif";
    const wrap = el("div", "chart-wrap");
    const canvas = el("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", spec.options.plugins.title?.text || "Chart");
    wrap.append(canvas);
    container.replaceChildren(wrap);
    try {
      new Chart(canvas, spec);
    } catch (error) {
      container.replaceChildren(el("p", "visual-error", `Couldn't draw this chart: ${error.message}`));
    }
  }

  function svgInto(container, source) {
    container.innerHTML = DOMPurify.sanitize(source, { USE_PROFILES: { svg: true, svgFilters: true } });
    const svg = container.querySelector("svg");
    if (!svg) {
      container.replaceChildren(el("p", "visual-error", "This drawing had nothing safe to show."));
      return;
    }
    if (svg.getAttribute("viewBox")) {
      svg.removeAttribute("width");
      svg.removeAttribute("height");
    }
  }

  // Interactive pages run in a sandbox: their own opaque origin, no access to this page or the internet.
  const POLICY = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:">`;
  const BASE_STYLE = "<style>html,body{margin:0;background:#0f0920;color:#f3ecff;font-family:'Segoe UI',system-ui,sans-serif}</style>";
  function htmlInto(container, source) {
    const frame = el("iframe", "visual-frame");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.title = "Interactive visual";
    const withPolicy = /<head[^>]*>/i.test(source)
      ? source.replace(/<head[^>]*>/i, (head) => head + POLICY + BASE_STYLE)
      : POLICY + BASE_STYLE + source;
    frame.srcdoc = withPolicy;
    container.replaceChildren(frame);
  }

  function draw(container, visual) {
    const content = String(visual.content || "");
    if (visual.kind === "mermaid") return mermaidInto(container, content);
    if (visual.kind === "chart") return chartInto(container, content);
    if (visual.kind === "svg") return svgInto(container, content);
    if (visual.kind === "html") return htmlInto(container, content);
    container.replaceChildren(el("p", "visual-error", "Unknown kind of visual."));
  }

  function card(visual, onOpen) {
    const box = el("figure", `visual-card kind-${visual.kind}`);
    const head = el("figcaption", "visual-head");
    head.append(icon(KIND_ICON[visual.kind] || "i-diagram"), el("span", "visual-title", visual.title || KIND_LABEL[visual.kind]),
      el("span", "visual-kind", KIND_LABEL[visual.kind] || visual.kind));
    const actions = el("span", "visual-actions");
    actions.append(button("Source", "i-copy", (e) => copyText(visual.content, e.currentTarget), "mini-pill"));
    if (onOpen) actions.append(button("Open", "i-expand", () => onOpen(visual), "mini-pill"));
    head.append(actions);
    const body = el("div", "visual-body");
    box.append(head, body);
    requestAnimationFrame(() => draw(body, visual));   // after it's in the page, so charts get a real size
    return box;
  }

  return { draw, card, KIND_LABEL };
})();

// ---------- Files Jarvis wrote, edited or showed ----------

const ACTION_LABEL = { created: "Created", updated: "Replaced", edited: "Edited", shown: "Shown" };

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function fileCard(file, openFile) {
  const card = el("div", "file-card");
  const head = el("div", "file-head");
  const kindIcon = file.image ? "i-image" : file.preview !== undefined ? "i-code" : "i-file";
  const names = el("div", "file-names");
  const title = el("div", "file-name");
  title.append(el("span", "", file.name), el("span", `file-action action-${file.action}`, ACTION_LABEL[file.action] || file.action));
  names.append(title, el("div", "file-folder", `${file.folder}${file.size !== undefined ? ` · ${formatSize(file.size)}` : ""}`));
  const actions = el("div", "file-buttons");
  actions.append(button("Open", "i-external", () => openFile(file.path, false), "mini-pill"),
    button("Folder", "i-folder", () => openFile(file.path, true), "mini-pill"));
  head.append(icon(kindIcon), names, actions);
  card.append(head);

  if (file.image && /^data:image\/(jpeg|png|svg\+xml);base64,/.test(file.image)) {
    const img = new Image();
    img.src = file.image;
    img.alt = file.name;
    img.className = "file-image";
    card.append(img);
  }
  if (file.diff) {
    const details = el("details", "file-diff");
    details.open = true;
    details.append(el("summary", "", "Changes"));
    const pre = el("pre");
    for (const line of file.diff.split("\n")) {
      const kind = line.startsWith("+++") || line.startsWith("---") ? "meta" : line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "same";
      pre.append(el("span", `diff-${kind}`, line + "\n"));
    }
    details.append(pre);
    card.append(details);
  } else if (file.preview !== undefined) {
    const details = el("details", "file-preview");
    details.append(el("summary", "", "Preview"));
    details.append(codeBlock(file.preview, file.language || "", { maxHeight: "320px" }));
    card.append(details);
  }
  return card;
}

// ---------- Approval cards ----------

function approvalCard(approval, decide) {
  const card = el("div", `approval-card kind-${approval.kind}`);
  card.dataset.id = approval.id;
  const head = el("div", "approval-head");
  head.append(icon(approval.kind === "command" ? "i-terminal" : "i-folder"), el("span", "approval-title", approval.title));
  card.append(head);
  if (approval.kind === "command") {
    const language = /python/i.test(approval.title) ? "python" : /bash/i.test(approval.title) ? "bash" : "powershell";
    card.append(codeBlock(approval.detail, language, { maxHeight: "260px" }));
  } else if (approval.detail) {
    card.append(el("p", "approval-detail", approval.detail));
  }
  const buttons = el("div", "approval-buttons");
  if (approval.kind === "command") {
    buttons.append(button("Run once", "i-play", () => decide(approval.id, "once"), "pill-btn primary"),
      button("Allow all commands in this chat", "i-check", () => decide(approval.id, "chat")));
  } else {
    buttons.append(button("Allow for this chat", "i-check", () => decide(approval.id, "chat"), "pill-btn primary"));
  }
  buttons.append(button("Deny", "i-close", () => decide(approval.id, "deny"), "pill-btn danger"));
  card.append(buttons, el("p", "approval-hint", "You can also say “yes” or “no” out loud during a voice request."));
  return card;
}

function settleApproval(card, decision, via) {
  if (!card) return;
  card.classList.add("settled", `decided-${decision}`);
  const text = decision === "deny" ? "Denied" : decision === "chat" ? "Allowed for this chat" : "Allowed once";
  const note = el("p", "approval-result", `${text}${via ? ` (${via === "voice" ? "by voice" : "by click"})` : ""}`);
  card.querySelector(".approval-buttons")?.replaceWith(note);
  card.querySelector(".approval-hint")?.remove();
}
