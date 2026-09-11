# tools.py — the things Jarvis can DO (Phase 4 tools + the Phase 5 remember tool)

import os
import subprocess
from pathlib import Path
import functools
import json
import re
from rapidfuzz import fuzz

from memory import remember

# SAFETY: Jarvis may only touch things inside these folders. Edit to taste.
ALLOWED_ROOTS = [
    Path.home() / "code",               # e.g. C:\Users\you\code
    Path.home() / "Documents" / "cs",
]


def _is_allowed(path: Path) -> bool:
    """True only if path is inside one of ALLOWED_ROOTS. is_relative_to compares whole
    folder names, so a 'code-old' folder can't sneak past a 'code' root."""
    path = path.resolve()
    return any(path.is_relative_to(root.resolve()) for root in ALLOWED_ROOTS)


def _run(cmd, cwd=None):
    """Run a command safely and return its text output."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    return (result.stdout + result.stderr).strip() or "(no output)"


# ---------- Git ----------

def git_status(repo: str) -> str:
    path = Path(repo).expanduser()
    if not _is_allowed(path):
        return f"Refused: {repo} is outside my allowed folders."
    return _run(["git", "status", "-s", "-b"], cwd=path)


def git_commit_all(repo: str, message: str) -> str:
    path = Path(repo).expanduser()
    if not _is_allowed(path):
        return f"Refused: {repo} is outside my allowed folders."
    _run(["git", "add", "-A"], cwd=path)
    return _run(["git", "commit", "-m", message], cwd=path)


def git_push(repo: str) -> str:
    path = Path(repo).expanduser()
    if not _is_allowed(path):
        return f"Refused: {repo} is outside my allowed folders."
    return _run(["git", "push"], cwd=path)


def git_pull(repo: str) -> str:
    path = Path(repo).expanduser()
    if not _is_allowed(path):
        return f"Refused: {repo} is outside my allowed folders."
    return _run(["git", "pull"], cwd=path)


# ---------- Files ----------

def list_files(folder: str) -> str:
    path = Path(folder).expanduser()
    if not _is_allowed(path):
        return f"Refused: {folder} is outside my allowed folders."
    return "\n".join(p.name for p in path.iterdir()) or "(empty)"


def read_file(filepath: str) -> str:
    path = Path(filepath).expanduser()
    if not _is_allowed(path):
        return f"Refused: {filepath} is outside my allowed folders."
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:4000]  # keep it short enough to speak/reason about


# ---------- Apps, files & folders (rough names are fine) ----------

MATCH_MIN = 60    # below this score: "not found"
MATCH_SURE = 85   # at or above this, and clearly ahead of the runner-up: just open it
MATCH_GAP = 8     # how far the best match must beat the next one to count as "clearly ahead"
MATCH_OK = 75     # ...or at least this, when it's the only plausible match

# Words people say that aren't part of the name
FILLER = {"the", "my", "a", "an", "app", "application", "program", "folder", "file",
          "please", "open", "up", "called", "named"}


def _clean(text: str) -> str:
    """'My_Algorithms-HW3' -> 'algorithms hw 3': lowercase, punctuation to spaces,
    letters split from numbers (so 'csc316' matches 'csc 316'), filler dropped."""
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text.lower())
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in FILLER)


def _pick(query: str, candidates: list):
    """candidates: (name, payload, recency). Returns (payload, None) when one match clearly
    wins, otherwise (None, a message telling Claude what happened)."""
    q = _clean(query)
    scored = sorted(
        ((fuzz.WRatio(q, _clean(name)) if q else 0, recency, -len(name), name, payload)
         for name, payload, recency in candidates),
        reverse=True,
    )
    if not scored or scored[0][0] < MATCH_MIN:
        return None, f"Nothing matching '{query}'."
    best, runner_up = scored[0], (scored[1] if len(scored) > 1 else None)
    if best[0] >= MATCH_SURE and (runner_up is None or best[0] - runner_up[0] >= MATCH_GAP):
        return best[4], None
    options = [s for s in scored[:5] if s[0] >= MATCH_MIN]
    if len(options) == 1 and best[0] >= MATCH_OK:  # only one plausible match: go with it
        return best[4], None
    listing = "\n".join(f"- {s[3]}" for s in options)
    return None, f"Several things could match '{query}'. Ask the user which one they meant:\n{listing}"


# --- Apps ---

APP_ALIASES = {
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "browser": "https://google.com",
    "spotify": "spotify:",
    "obsidian": "obsidian://open?vault=VAULT",   # opens your vault directly
}

START_MENUS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
]


@functools.lru_cache(maxsize=1)
def _installed_apps():
    """Every app in the Start Menu, Microsoft Store apps included, as (name, launch_id).
    Windows is asked once (about a second), then the list is reused until Jarvis restarts."""
    try:
        cmd = ["powershell", "-NoProfile", "-Command",
               "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json"]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
        data = json.loads(out or "[]")
        data = [data] if isinstance(data, dict) else data
        apps = [(a["Name"], a["AppID"]) for a in data if a.get("Name") and a.get("AppID")]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        apps = []
    if not apps:  # fallback: plain Start Menu shortcuts
        apps = [(p.stem, str(p)) for folder in START_MENUS if folder.is_dir()
                for p in folder.rglob("*.lnk")]
    unique = {}
    for name, launch_id in apps:
        if "uninstall" not in name.lower():
            unique.setdefault(name.lower(), (name, launch_id))
    return list(unique.values())


def open_app(name: str) -> str:
    """Open an installed app (Store apps included) or a link, from a rough or misheard name."""
    target = APP_ALIASES.get(_clean(name), name.strip())

    # Links and app protocols (https://..., spotify:, obsidian://...) open directly
    if "://" in target or target.endswith(":"):
        os.startfile(target)
        return f"Opened {name}."

    app, message = _pick(target, [(n, (n, i), 0) for n, i in _installed_apps()])
    if message:
        return message
    app_name, launch_id = app
    if launch_id.lower().endswith(".lnk"):
        os.startfile(launch_id)
    else:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{launch_id}"])
    return f"Opened {app_name}."


# --- Files & folders ---

# Where Jarvis looks for things to open. Only names ever go to Claude, never file contents,
# so this can be wider than ALLOWED_ROOTS (which read_file and the git tools use).
OPEN_ROOTS = ALLOWED_ROOTS + [
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
]
SKIP_NAMES = {"node_modules", "venv", "__pycache__", "appdata", "desktop.ini", "thumbs.db"}  # lowercase
MAX_DEPTH = 5       # how many folders deep to look inside each root
MAX_ITEMS = 20000   # stop after this many entries so the search stays fast

# Opening these would RUN them, so Jarvis highlights them in File Explorer instead
RUNS_WHEN_OPENED = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".js", ".msi",
                    ".scr", ".py", ".pyw", ".reg", ".lnk"}


def _scan(kind: str) -> list:
    """Collect (name, path, last_modified) for files and/or folders under OPEN_ROOTS."""
    found, seen = [], set()
    roots = [root for root in OPEN_ROOTS if root.is_dir()]
    if kind in ("folder", "any"):  # the roots themselves, so "open downloads" works
        for root in roots:
            if str(root).lower() not in seen:
                seen.add(str(root).lower())
                found.append((root.name, str(root), root.stat().st_mtime))
    stack = [(str(root), 0) for root in roots]
    while stack and len(found) < MAX_ITEMS:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue  # no permission, removed, etc.
        for entry in entries:
            if entry.name.startswith(".") or entry.name.lower() in SKIP_NAMES:
                continue
            if entry.path.lower() in seen:  # roots can overlap (Documents and Documents/cs)
                continue
            seen.add(entry.path.lower())
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                modified = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if is_dir and depth < MAX_DEPTH:
                stack.append((entry.path, depth + 1))
            if (is_dir and kind == "file") or (not is_dir and kind == "folder"):
                continue
            name = entry.name if is_dir else os.path.splitext(entry.name)[0]
            found.append((name, entry.path, modified))
    return found


def open_path(query: str, kind: str = "any") -> str:
    """Open a file or folder in its default app, from a rough name or an exact path."""
    kind = kind if kind in ("file", "folder", "any") else "any"
    exact = Path(query).expanduser()
    if exact.is_absolute() and exact.exists():
        target = exact
    else:
        found, message = _pick(query, _scan(kind))
        if message:
            return message
        target = Path(found)

    target = target.resolve()
    if not any(target.is_relative_to(root.resolve()) for root in OPEN_ROOTS):
        return f"Refused: {target} is outside the folders I'm allowed to open."

    if target.is_file() and target.suffix.lower() in RUNS_WHEN_OPENED:
        subprocess.Popen(["explorer.exe", "/select,", str(target)])  # highlight, don't run
        return f"{target.name} is a program or script, so I highlighted it in File Explorer instead of running it."

    os.startfile(target)
    return f"Opened {target}."


# ---------- The schema Claude reads ----------
# This tells Claude what tools exist, what they do, and their inputs.

TOOL_SCHEMA = [
    {
        "name": "git_status",
        "description": "Show the git status (branch + changed files) of a repo.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "Path to the repo folder"}},
            "required": ["repo"],
        },
    },
    {
        "name": "git_commit_all",
        "description": "Stage all changes and commit them with a message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "message": {"type": "string", "description": "Commit message"},
            },
            "required": ["repo", "message"],
        },
    },
    {
        "name": "git_push",
        "description": "Push committed changes to the remote.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "git_pull",
        "description": "Pull latest changes from the remote.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "list_files",
        "description": "List the files in a folder.",
        "input_schema": {
            "type": "object",
            "properties": {"folder": {"type": "string"}},
            "required": ["folder"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a text/code file (first 4000 chars).",
        "input_schema": {
            "type": "object",
            "properties": {"filepath": {"type": "string"}},
            "required": ["filepath"],
        },
    },
    {
        "name": "open_app",
        "description": (
            "Open an installed Windows app (desktop or Microsoft Store) from a rough or misheard "
            "name, e.g. 'discord', 'chrome', 'calculator', or a website by full URL starting with "
            "https://. If it replies with several possible matches, ask the user which one they "
            "meant, then call it again with that exact name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "App name (rough is fine) or a full URL"}},
            "required": ["name"],
        },
    },
    {
        "name": "open_path",
        "description": (
            "Open a file or folder on the user's PC in its default app, from a rough description "
            "of its name (e.g. 'algorithms homework', 'resume', 'cs101 folder', 'downloads') or an "
            "exact path. Set kind to 'folder' or 'file' when the user says which. If it replies "
            "with several possible matches, ask the user which one they meant, then call it again "
            "with that exact path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Rough name of the file or folder, or an exact path"},
                "kind": {"type": "string", "enum": ["any", "file", "folder"], "description": "What to look for"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a fact about the user to long-term memory so you still know it in future "
            "sessions. Use it whenever the user asks you to remember something, such as a "
            "repo location, a class schedule, or a preference. One short, self-contained fact per call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The fact, written so it makes sense on its own later, "
                                   "e.g. 'The algorithms repo is in Documents/cs/algo'",
                }
            },
            "required": ["fact"],
        },
    },
]

# Map tool names to the real functions
TOOL_FUNCTIONS = {
    "git_status": git_status,
    "git_commit_all": git_commit_all,
    "git_push": git_push,
    "git_pull": git_pull,
    "list_files": list_files,
    "read_file": read_file,
    "open_app": open_app,
    "remember": remember,
}

# ---------- Web (runs on Anthropic's servers — nothing to add to TOOL_FUNCTIONS) ----------
# web_search: Claude searches the web (about 1 cent per search, plus tokens for the results).
# web_fetch:  Claude reads a page it found (no extra fee, just tokens; capped at 5,000 here).
TOOL_SCHEMA += [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 5000},
]