# tools.py — the things Jarvis can DO: git, files, code, apps, notes, Spotify, visuals, screenshots, memory

import base64
import difflib
import fnmatch
import io
import os
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path
import functools
import json
import re
from rapidfuzz import fuzz
from collections import deque
import ctypes
import time
from dotenv import load_dotenv
import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyPKCE

from memory import forget, remember

load_dotenv(Path(__file__).with_name(".env"))  # so SPOTIFY_CLIENT_ID is there even when tools.py runs on its own


# ---------- Permission: files, git and code work in any folder, but Jarvis asks first ----------
# In the dashboard, APPROVER shows an approval card (and listens for a spoken yes or no). "Allow for this chat"
# covers that folder, or all commands, for the rest of the chat.
# In the terminal version there's no card: the tool tells Claude to ask, and a call with confirmed=true only
# counts after the user has actually replied (a new turn), so Claude can't ask and answer its own question.
# Either way a yes covers that repo or folder, and everything inside it, until Jarvis restarts.

APPROVER = None      # gui.py sets this: APPROVER(kind, title, detail, chat_id) -> "once", "chat" or "deny"
CURRENT_CHAT = None  # the chat the current reply belongs to (brain.py sets it through begin_reply)
WORKSPACE = Path(__file__).with_name("workspace")   # Jarvis's own folder for scripts, backups and new files

_approved = {}       # chat id (None in the terminal version) -> folders the user said yes to
_commands_ok = set() # chats where the user allowed every command
_asked = {}          # folder -> the turn Jarvis asked about it (terminal version)
_turn = 0            # how many times the user has spoken (brain.py calls new_turn)
CONFIRM_WINDOW = 2   # the yes has to come within this many replies of the question

# Never read or commit these, even with permission: they hold passwords and API keys
SECRET_NAMES = {".env", ".git-credentials", ".netrc", "_netrc", "id_rsa", "id_ecdsa", "id_ed25519"}
SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12")
NOT_SECRET = {".env.example", ".env.sample", ".env.template"}   # placeholder files meant to be shared


def new_turn():
    """brain.py calls this every time the user says something."""
    global _turn
    _turn += 1


def _is_secret(path) -> bool:
    """True for .env files, SSH keys and the like. Only looks at the file name."""
    name = Path(path).name.lower()
    if name in NOT_SECRET:
        return False
    return name in SECRET_NAMES or name.startswith(".env.") or name.endswith(SECRET_SUFFIXES)


def _project_folder(path: Path) -> Path:
    """What a yes applies to: the git repo that path is in, or else its own folder.
    Your home folder and the drive root never count as a repo."""
    folder = path if path.is_dir() else path.parent
    for candidate in (folder, *folder.parents):
        if candidate == Path.home() or candidate == Path(candidate.anchor):
            break
        if (candidate / ".git").exists():
            return candidate
    return folder


def begin_reply(chat_id):
    """brain.py calls this at the start of every reply: which chat it's for, and fresh lists for the chat window."""
    global CURRENT_CHAT, quiet_reply
    CURRENT_CHAT = chat_id
    quiet_reply = None
    for pending in (links_to_show, screenshots_to_show, visuals_to_show, files_to_show):
        pending.clear()


def _permission(path: Path, confirmed: bool, tool: str):
    """None if Jarvis may use path, otherwise a message for Claude (ask first, or the user said no)."""
    folder = _project_folder(path)
    if folder.is_relative_to(WORKSPACE.resolve()):
        return None   # Jarvis's own workspace never needs asking
    approved = _approved.setdefault(CURRENT_CHAT, set())
    if any(folder.is_relative_to(yes) for yes in approved):
        return None
    if APPROVER is not None:
        decision = APPROVER("folder", f"Let Jarvis read and change files in {folder}?",
                            f"Asked by {tool}. This covers everything inside that folder for the rest of this chat.",
                            CURRENT_CHAT)
        if decision == "deny":
            return f"The user said no to using {folder}. Don't look for a way around it; ask what they'd like instead."
        approved.add(folder)
        return None
    asked = _asked.get(folder)
    if confirmed and asked is not None and 0 < _turn - asked <= CONFIRM_WINDOW:
        approved.add(folder)
        del _asked[folder]
        return None
    _asked[folder] = _turn   # (re)start the question; a confirmed=true in the same turn doesn't count
    return (f"Permission needed. Ask the user in one short question whether you may use {folder}, "
            f"saying the folder name the way a person would. Only if they clearly say yes, call {tool} "
            f"again with confirmed set to true. A yes covers that whole folder until Jarvis restarts.")


def _run(cmd, cwd=None):
    """Run a command safely and return its text output."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    return (result.stdout + result.stderr).strip() or "(no output)"


# ---------- Git ----------

def _repo(repo: str, confirmed: bool, tool: str):
    """Rough name or exact path -> (repo folder, None), or (None, a message for Claude)."""
    path, message = _find(repo, "folder")   # _find is in the files & folders section below
    if message:
        return None, message
    message = _permission(path, confirmed, tool)
    if message:
        return None, message
    return _project_folder(path), None


def git_status(repo: str, confirmed: bool = False) -> str:
    folder, message = _repo(repo, confirmed, "git_status")
    if message:
        return message
    return _run(["git", "status", "-s", "-b"], cwd=folder)


def git_commit_all(repo: str, message: str, confirmed: bool = False) -> str:
    folder, problem = _repo(repo, confirmed, "git_commit_all")
    if problem:
        return problem
    _run(["git", "add", "-A"], cwd=folder)
    # Take secrets back out of the commit, in case this repo's .gitignore forgot them
    staged = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"], cwd=folder, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60).stdout.split("\0")
    secrets = [name for name in staged if name and _is_secret(name)]
    if secrets:
        _run(["git", "--literal-pathspecs", "reset", "-q", "--", *secrets], cwd=folder)
    result = _run(["git", "commit", "-m", message], cwd=folder)
    if secrets:
        result += ("\nKept out of the commit because they hold secrets: " + ", ".join(secrets)
                   + ". Adding them to the repo's .gitignore would stop them being picked up.")
    return result


def git_push(repo: str, confirmed: bool = False) -> str:
    folder, message = _repo(repo, confirmed, "git_push")
    if message:
        return message
    return _run(["git", "push"], cwd=folder)


def git_pull(repo: str, confirmed: bool = False) -> str:
    folder, message = _repo(repo, confirmed, "git_pull")
    if message:
        return message
    return _run(["git", "pull"], cwd=folder)


# ---------- Files ----------

READ_CHARS = 40_000   # most text one read_file call hands to Claude; start_line reads further
PDF_LIMIT = 30_000_000
PICTURE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def _existing_file(text: str):
    """Exact path, a path inside the workspace, or a rough name -> (path, None) or (None, message)."""
    raw = Path(text).expanduser()
    if not raw.is_absolute() and (WORKSPACE / raw).exists():
        return (WORKSPACE / raw).resolve(), None
    return _find(text, "file")


def read_file(filepath: str, confirmed: bool = False, start_line: int = 1, max_lines: int = 0):
    path, message = _existing_file(filepath)
    if message:
        return message
    if path.is_dir():
        return f"{path} is a folder, not a file. Use list_files or find_files to see what's inside."
    if _is_secret(path):
        return f"Refused: {path.name} holds secrets like passwords or API keys, so I never read it."
    message = _permission(path, confirmed, "read_file")
    if message:
        return message

    suffix = path.suffix.lower()
    if suffix in PICTURE_TYPES:   # Claude can look at pictures
        from PIL import Image
        with Image.open(path) as picture:
            picture.load()
            return _for_claude(picture, path.name, add_thumbnail=False)
    if suffix == ".pdf":
        data = path.read_bytes()
        if len(data) > PDF_LIMIT:
            return f"{path.name} is too big to read ({len(data) // 1_000_000} MB; the limit is 30 MB)."
        return [{"type": "text", "text": f"{path} ({len(data) // 1024} KB PDF)"},
                {"type": "document", "title": path.name,
                 "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode("ascii")}}]

    raw = path.read_bytes()
    if b"\x00" in raw[:8000]:
        return f"{path.name} is a binary file, so I can't read it as text."
    lines = raw.decode("utf-8", errors="replace").splitlines()
    start = max(1, int(start_line or 1))
    chunk = lines[start - 1:start - 1 + max_lines] if max_lines else lines[start - 1:]
    shown, used = [], 0
    for number, line in enumerate(chunk, start):
        entry = f"{number}\t{line}"
        if shown and used + len(entry) > READ_CHARS:
            break
        shown.append(entry)
        used += len(entry) + 1
    end = start + len(shown) - 1
    more = f" (more below: call again with start_line={end + 1})" if end < len(lines) else ""
    return f"{path}: lines {start}-{end} of {len(lines)}{more}. Line numbers are not part of the file.\n" + "\n".join(shown)


# ---------- Coding: commands, scripts and file edits ----------
# Commands and scripts always ask first (the dashboard shows the exact command). File changes use the
# folder permission above, back up the old version into the workspace, and show a diff in the chat.

MAX_OUTPUT = 12_000      # characters of command output handed back to Claude
MAX_WRITE = 2_000_000
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__"}
_running = None          # the process a command or script is waiting on, so Stop can end it
_stopped_by_user = False


def _work_folder(folder: str):
    """Where a command runs -> (folder, None) or (None, message). Empty means the workspace."""
    if not (folder or "").strip():
        WORKSPACE.mkdir(exist_ok=True)
        return WORKSPACE.resolve(), None
    path, message = _find(folder, "folder")
    if message:
        return None, message
    return (path if path.is_dir() else path.parent), None


def _approve_run(title: str, detail: str):
    """None if Jarvis may run it, otherwise a message for Claude."""
    if APPROVER is None:
        return "Running commands and scripts only works in the dashboard (python gui.py), where the user can approve them."
    if CURRENT_CHAT in _commands_ok:
        return None
    decision = APPROVER("command", title, detail, CURRENT_CHAT)
    if decision == "deny":
        return "The user denied this. Don't run it another way; ask what they'd like instead."
    if decision == "chat":
        _commands_ok.add(CURRENT_CHAT)
    return None


def _kill_tree(process):
    """End a process and everything it started (a script's own child processes too)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def stop_running():
    """gui.py's Stop button: end whatever command or script is running."""
    global _stopped_by_user
    process = _running
    if process is not None and process.poll() is None:
        _stopped_by_user = True
        _kill_tree(process)


def _run_process(args: list, cwd: Path, timeout, env=None) -> str:
    global _running, _stopped_by_user
    timeout = max(5, min(int(timeout or 120), 900))
    started = time.monotonic()
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=env, creationflags=flags,
                                   start_new_session=os.name != "nt")
    except OSError as e:
        return f"Couldn't start it: {e}"
    _running, _stopped_by_user = process, False
    ending = ""
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        output, _ = process.communicate()
        ending = f" (stopped: it hit the {timeout} s time limit)"
    finally:
        _running = None
    if _stopped_by_user:
        ending = " (stopped by the user)"
    text = output.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT // 2] + f"\n… ({len(text) - MAX_OUTPUT} characters cut) …\n" + text[-MAX_OUTPUT // 2:]
    return f"Exit code {process.returncode}{ending}, took {time.monotonic() - started:.1f} s.\n{text or '(no output)'}"


def run_command(command: str, folder: str = "", timeout: int = 120) -> str:
    """Run a PowerShell command on the user's PC, after they approve it, and return the output."""
    command = (command or "").strip()
    if not command:
        return "No command given."
    cwd, message = _work_folder(folder)
    if message:
        return message
    shell = "PowerShell" if os.name == "nt" else "bash"
    blocked = _approve_run(f"Run this {shell} command in {cwd}?", command)
    if blocked:
        return blocked
    if os.name == "nt":
        args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "$ProgressPreference = 'SilentlyContinue'; [Console]::OutputEncoding = [Text.Encoding]::UTF8; " + command]
    else:
        args = ["bash", "-lc", command]
    return _run_process(args, cwd, timeout)


def _pictures_in(folder: Path) -> dict:
    try:
        return {p: p.stat().st_mtime for p in folder.iterdir() if p.suffix.lower() in (*PICTURE_TYPES, ".svg")}
    except OSError:
        return {}


def run_python(code: str, folder: str = "", timeout: int = 120) -> str:
    """Run a Python script with Jarvis's own Python, after the user approves it."""
    if not (code or "").strip():
        return "No code given."
    cwd, message = _work_folder(folder)
    if message:
        return message
    blocked = _approve_run(f"Run this Python script in {cwd}?", code)
    if blocked:
        return blocked
    scripts = WORKSPACE / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / f"script_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.py"
    script.write_text(code, encoding="utf-8")
    before = _pictures_in(cwd)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "MPLBACKEND": "Agg"}  # Agg: charts save, no pop-ups
    result = _run_process([sys.executable, str(script)], cwd, timeout, env)
    new_pictures = [p for p, modified in _pictures_in(cwd).items() if before.get(p) != modified][:6]
    for picture in new_pictures:
        _file_card(picture, "created")
    if new_pictures:
        result += "\nNew pictures (already shown in the chat): " + ", ".join(p.name for p in new_pictures)
    return f"Script saved as {script}\n{result}"


def _writable(path_text: str, confirmed: bool, tool: str):
    """A path Jarvis may write -> (path, None) or (None, message). Relative paths go in the workspace."""
    raw = Path((path_text or "").strip()).expanduser()
    if not str(raw) or str(raw) == ".":
        return None, "No file path given."
    path = (raw if raw.is_absolute() else WORKSPACE / raw).resolve()
    if _is_secret(path):
        return None, f"Refused: {path.name} holds secrets, so I never write it."
    if ".git" in path.parts:
        return None, "Refused: I don't edit files inside a .git folder."
    if path.is_dir():
        return None, f"{path} is a folder, not a file."
    message = _permission(path, confirmed, tool)
    return (None, message) if message else (path, None)


def _backup(path: Path) -> Path:
    backups = WORKSPACE / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    copy = backups / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}_{path.name}"
    shutil.copy2(path, copy)
    return copy


def _diff(before: str, after: str, name: str, limit: int = 150) -> str:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), f"{name} (before)", f"{name} (after)",
                                      lineterm="", n=2))
    if len(lines) > limit:
        lines = lines[:limit] + [f"… {len(lines) - limit} more lines of changes"]
    return "\n".join(lines)


def _decode(raw: bytes):
    """File bytes -> (text with \\n line endings, used CRLF, had a BOM), or None if it isn't UTF-8 text."""
    if b"\x00" in raw[:8000]:
        return None
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig" if bom else "utf-8")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n"), b"\r\n" in raw, bom


def _encode(text: str, crlf: bool, bom: bool) -> bytes:
    data = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
    return b"\xef\xbb\xbf" + data if bom else data


def write_file(path: str, content: str, confirmed: bool = False) -> str:
    """Create a file, or replace a whole file, with content."""
    target, message = _writable(path, confirmed, "write_file")
    if message:
        return message
    content = (content or "").replace("\r\n", "\n")
    if len(content) > MAX_WRITE:
        return "That's too big to write in one go (2 MB limit)."
    existed = target.exists()
    before, crlf, bom = ("", False, False)
    if existed:
        decoded = _decode(target.read_bytes())
        before, crlf, bom = decoded if decoded else ("", False, False)
    backup = _backup(target) if existed else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_encode(content, crlf, bom))   # keeps a Windows file's line endings
    lines = len(content.splitlines())
    _file_card(target, "updated" if existed else "created", diff=_diff(before, content, target.name) if existed else None)
    if existed:
        return f"Replaced {target} ({lines} lines). The old version is backed up at {backup}."
    return f"Created {target} ({lines} lines)."


def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False, confirmed: bool = False) -> str:
    """Replace exact text in an existing file."""
    target, message = _writable(path, confirmed, "edit_file")
    if message:
        return message
    if not target.is_file():
        return f"There's no file at {target}. Use write_file to create it."
    decoded = _decode(target.read_bytes())
    if decoded is None:
        return f"{target.name} isn't a UTF-8 text file, so I won't edit it."
    text, crlf, bom = decoded
    old, new = (old_text or "").replace("\r\n", "\n"), (new_text or "").replace("\r\n", "\n")
    if not old:
        return "old_text can't be empty. To add to a file, include the lines around where the new text goes."
    count = text.count(old)
    if count == 0:
        return "old_text wasn't found. Read the file again and copy the exact text, spaces and indentation included."
    if count > 1 and not replace_all:
        return f"old_text appears {count} times. Include more surrounding lines so it's unique, or set replace_all."
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    first_line = text[:text.index(old)].count("\n") + 1
    backup = _backup(target)
    target.write_bytes(_encode(updated, crlf, bom))
    _file_card(target, "edited", diff=_diff(text, updated, target.name))
    return (f"Edited {target}: replaced {count if replace_all else 1} occurrence{'s' if replace_all and count > 1 else ''}, "
            f"starting at line {first_line}. Backup: {backup}.")


def _walk(base: Path, max_files: int = 20_000):
    """Files under base, skipping .git, venv and friends."""
    seen = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            seen += 1
            if seen > max_files:
                return
            yield Path(root) / name


def find_files(pattern: str, folder: str = "", confirmed: bool = False) -> str:
    base, message = _work_folder(folder)
    if message:
        return message
    message = _permission(base, confirmed, "find_files")
    if message:
        return message
    pattern = (pattern or "*").strip().replace("\\", "/")
    matches = []
    for path in _walk(base):
        relative = path.relative_to(base).as_posix()
        if fnmatch.fnmatch(path.name.lower(), pattern.lower()) or fnmatch.fnmatch(relative.lower(), pattern.lower()):
            matches.append(relative)
            if len(matches) >= 300:
                break
    if not matches:
        return f"No files matching '{pattern}' in {base}."
    return f"{len(matches)} file(s) matching '{pattern}' in {base}:\n" + "\n".join(matches)


def search_text(query: str, folder: str = "", file_pattern: str = "*", confirmed: bool = False) -> str:
    base, message = _work_folder(folder)
    if message:
        return message
    message = _permission(base, confirmed, "search_text")
    if message:
        return message
    try:
        finder = re.compile(query, re.IGNORECASE)
    except re.error:
        finder = re.compile(re.escape(query), re.IGNORECASE)
    results = []
    for path in _walk(base, max_files=8000):
        if not fnmatch.fnmatch(path.name.lower(), (file_pattern or "*").lower()) or _is_secret(path):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if finder.search(line):
                results.append(f"{path.relative_to(base).as_posix()}:{number}: {line.strip()[:200]}")
                if len(results) >= 150:
                    return f"First 150 matches for '{query}' in {base}:\n" + "\n".join(results)
    if not results:
        return f"No matches for '{query}' in {base}."
    return f"{len(results)} match(es) for '{query}' in {base}:\n" + "\n".join(results)


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
    # Files and folders show their full path (so "which saves folder?" is answerable); apps show their name
    listing = "\n".join(f"- {s[4] if isinstance(s[4], str) else s[3]}" for s in options)
    if len(options) == 1:
        return None, f"The only close match for '{query}' is:\n{listing}\nAsk the user if that's what they meant."
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

    # A file path isn't an app name (fuzzy-matching one gives nonsense), so send Claude to run_program
    if re.search(r"[\\/]", target):
        return f"'{name}' is a file path, not an installed app. To start a program from a path, use run_program."

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

# Where Jarvis looks for things to open and list, and where rough names are searched. Only names
# ever go to Claude from here, so no permission is needed. (Git and read_file can reach any folder,
# by exact path, but ask first.)
OPEN_ROOTS = [
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

# run_program only starts these (no scripts or shortcuts), and never from these folders
RUN_OK = {".exe"}
NO_RUN_FROM = [Path.home() / "Downloads"]   # where untrusted downloads land: you double-click those


def _scan(kind: str) -> list:
    """Collect (name, path, last_modified) for files and/or folders under OPEN_ROOTS.
    Breadth-first: the top level of every root gets checked before going deeper anywhere,
    so a huge Downloads or Documents folder can't use up the budget before Desktop."""
    found, seen, queue = [], set(), deque()
    for root in OPEN_ROOTS:
        key = str(root).lower()
        if not root.is_dir() or key in seen:
            continue
        seen.add(key)
        queue.append((str(root), 0))
        if kind in ("folder", "any"):  # the roots themselves, so "open downloads" works
            found.append((root.name, str(root), root.stat().st_mtime))
    visited = 0
    while queue and visited < MAX_ITEMS:
        folder, depth = queue.popleft()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue  # no permission, removed, etc.
        for entry in entries:
            visited += 1
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
                queue.append((entry.path, depth + 1))
            if (is_dir and kind == "file") or (not is_dir and kind == "folder"):
                continue
            name = entry.name if is_dir else os.path.splitext(entry.name)[0]
            found.append((name, entry.path, modified))
    return found


def _can_open(path: Path) -> bool:
    """True if path is inside OPEN_ROOTS (the wider folders Jarvis can open and list)."""
    path = path.resolve()
    return any(path.is_relative_to(root.resolve()) for root in OPEN_ROOTS)


def _roots_text() -> str:
    """The real OPEN_ROOTS as text, so Jarvis reports them instead of guessing."""
    return ", ".join(str(root) for root in OPEN_ROOTS if root.is_dir())


def _find(query: str, kind: str = "any"):
    """Exact path or rough name -> (path, None), or (None, a message for Claude).
    In 'Desktop/Marvel Tokon' the last part is the name; anything before it says where to look."""
    exact = Path(query).expanduser()
    if exact.is_absolute() and exact.exists():
        return exact.resolve(), None
    parts = [p for p in re.split(r"[\\/]+", query) if p not in ("", "~", ".")]
    name, hints = (parts[-1], [_clean(h) for h in parts[:-1]]) if parts else (query, [])
    candidates = _scan(kind)
    if hints:  # compared cleaned, so "csc316/hw3" still finds "CSC 316\hw3"
        candidates = [c for c in candidates if all(h in _clean(c[1]) for h in hints)] or candidates
    found, message = _pick(name, candidates)
    if message:
        if message.startswith("Nothing"):
            message += f" Searched: {_roots_text()}. Tell the user it wasn't found there; don't guess why."
        return None, message
    return Path(found).resolve(), None


def open_path(query: str, kind: str = "any") -> str:
    """Open a file or folder in its default app, from a rough name or an exact path."""
    kind = kind if kind in ("file", "folder", "any") else "any"
    target, message = _find(query, kind)
    if message:
        return message
    if not _can_open(target):
        return f"Refused: {target} is outside the folders I can open. Allowed: {_roots_text()}."

    if target.is_file() and target.suffix.lower() in RUNS_WHEN_OPENED:
        subprocess.Popen(["explorer.exe", "/select,", str(target)])  # highlight, don't run
        if target.suffix.lower() in RUN_OK:
            return f"{target.name} is a program, so I highlighted it in File Explorer. If the user wants it started, use run_program."
        return f"{target.name} would run something when opened, so I highlighted it in File Explorer instead."

    os.startfile(target)
    return f"Opened {target}."


MAX_LISTED = 50  # most folder names (and file names) one listing hands to Claude


def list_files(folder: str) -> str:
    """What's inside a folder, from a rough name or an exact path. Only names go to Claude."""
    path, message = _find(folder, "folder")
    if message:
        return message
    if not _can_open(path):
        return f"Refused: {path} is outside the folders I can look in. Allowed: {_roots_text()}."
    if not path.is_dir():
        return f"{path} is a file, not a folder."
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError:
        return f"Windows wouldn't let me look inside {path}."

    folders, files = [], []
    for entry in entries:
        if entry.name.startswith(".") or entry.name.lower() in ("desktop.ini", "thumbs.db"):
            continue
        try:
            (folders if entry.is_dir() else files).append(entry.name)
        except OSError:
            continue

    if not folders and not files:
        return f"{path} is empty."
    lines = [str(path)]
    for label, names in (("Folders", folders), ("Files", files)):
        if names:
            lines.append(f"{label} ({len(names)}):")
            lines += [f"  {name}" for name in names[:MAX_LISTED]]
            if len(names) > MAX_LISTED:
                lines.append(f"  ...and {len(names) - MAX_LISTED} more")
    return "\n".join(lines)


# --- Running programs ---

def run_program(path: str) -> str:
    """Start a program (.exe) like double-clicking it. Needs the exact path, e.g. from list_files."""
    target = Path(path).expanduser()
    if not (target.is_absolute() and target.is_file()):
        return f"No program at '{path}'. Find its exact path with list_files first."
    target = target.resolve()
    if target.suffix.lower() not in RUN_OK:
        return f"Refused: {target.name} isn't a program (.exe). I don't run scripts, installers or shortcuts."
    if any(target.is_relative_to(root.resolve()) for root in NO_RUN_FROM):
        return f"Refused: {target.name} is in Downloads, and I don't run downloaded files. The user can double-click it."
    if not _can_open(target):
        return f"Refused: {target} is outside the folders I can use ({_roots_text()})."
    try:
        os.startfile(target, cwd=str(target.parent))  # its own folder as the working directory, like a double-click
    except OSError as e:
        return f"Windows couldn't start {target.name}: {e.strerror or e}"
    return f"Started {target.name}."


# ---------- Obsidian notes ----------

VAULT = Path.home() / "VAULT"   # your Obsidian vault


def _note_name(title: str) -> str:
    """'Ideas: Jarvis v2?' -> 'Ideas Jarvis v2'. Drops characters Windows file names or Obsidian links can't have."""
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", title)
    return re.sub(r"\s+", " ", name).strip(" .")[:100]


def _in_vault(path: Path) -> bool:
    """True for anything inside the vault, except Obsidian's own hidden folders (.obsidian, .trash)."""
    try:
        parts = path.resolve().relative_to(VAULT.resolve()).parts
    except ValueError:
        return False
    return not any(part.startswith(".") for part in parts)


def _vault_label(folder: Path) -> str:
    """How to say where a note is: 'Job/Jarvis', or 'the top of the vault'."""
    relative = folder.resolve().relative_to(VAULT.resolve()).as_posix()
    return "the top of the vault" if relative == "." else relative


def _vault_folder(folder: str):
    """Where a new note goes -> (folder path, None) or (None, a message for Claude).
    '' = top of the vault, 'jarvis' = the closest existing folder by name, 'Job/Ideas' = exactly that path.
    A folder that doesn't exist yet gets created when the note is written."""
    folder = folder.strip().strip("\\/")
    if not folder:
        return VAULT, None
    if not re.search(r"[\\/]", folder):
        existing = [p for p in VAULT.rglob("*") if p.is_dir() and _in_vault(p)]
        found, message = _pick(folder, [(p.name, str(p), p.stat().st_mtime) for p in existing])
        if found:
            return Path(found), None
        if message.startswith("Several"):
            return None, message
    parts = [_note_name(part) for part in re.split(r"[\\/]+", folder)]
    return VAULT.joinpath(*[part for part in parts if part]), None


def create_note(title: str, text: str, folder: str = "") -> str:
    """Write a brand-new note into the Obsidian vault. Never overwrites an existing note."""
    name = _note_name(title)
    if not name:
        return "The note needs a title."
    if not VAULT.is_dir():
        return f"I can't find the Obsidian vault at {VAULT}."
    where, message = _vault_folder(folder)
    if message:
        return message
    note = where / f"{name}.md"
    if not _in_vault(note):
        return "Refused: notes can only go inside the vault."
    where.mkdir(parents=True, exist_ok=True)
    try:
        with open(note, "x", encoding="utf-8", newline="\n") as f:  # "x" fails instead of overwriting
            f.write(text.strip() + "\n")
    except FileExistsError:
        return f"There's already a note called '{name}' in {_vault_label(where)}. Ask the user whether to add to it instead."
    return f"Created the note '{name}' in {_vault_label(where)}."


def add_to_note(title: str, text: str) -> str:
    """Add text to the end of an existing note, found by a rough title or its exact path."""
    if re.search(r"[\\/]", title):  # a path ('Job/Jarvis' or a full path) is used exactly, never guessed at
        note = Path(title).expanduser()
        note = note if note.is_absolute() else VAULT / note
        if note.suffix.lower() != ".md":
            note = note.with_name(note.name + ".md")  # not with_suffix: 'Plan v2.1' would lose its '.1'
        if not (note.is_file() and _in_vault(note)):
            return f"There's no note at '{title}' in the vault."
    else:
        notes = [p for p in VAULT.rglob("*.md") if _in_vault(p)]
        title = title.removesuffix(".md")
        found, message = _pick(title, [(p.stem, str(p), p.stat().st_mtime) for p in notes])
        if message:
            if message.startswith("Nothing"):
                message += " Offer to create a new note with that title instead."
            elif message.startswith("The only"):
                message += " If not, offer to create a new note with that title."
            return message
        note = Path(found)
    existing = note.read_text(encoding="utf-8", errors="replace")
    with open(note, "a", encoding="utf-8", newline="\n") as f:
        f.write(("\n" if existing and not existing.endswith("\n") else "") + text.strip() + "\n")
    return f"Added that to '{note.stem}' in {_vault_label(note.parent)}."


# ---------- Spotify ----------
# Picking songs and playlists and queueing use Spotify's Web API, which needs Spotify Premium, a free
# developer app (SPOTIFY_CLIENT_ID in .env) and a one-time login:  python -c "import tools; tools.spotify_login()"
# Until that's done, play / pause / next / previous still work by pressing the keyboard's media keys.

SPOTIFY_REDIRECT = "http://127.0.0.1:8888/callback"   # must match the Redirect URI in your Spotify app exactly
SPOTIFY_SCOPES = ("user-read-playback-state user-modify-playback-state "
                  "playlist-read-private playlist-read-collaborative user-library-read")
SPOTIFY_TOKEN = Path(os.environ.get("APPDATA", Path.home())) / "Jarvis" / "spotify_token.json"  # outside the repo on purpose
MEDIA_KEYS = {"play": 0xB3, "pause": 0xB3, "next": 0xB0, "previous": 0xB1}  # play and pause are the same toggle key
SPOTIFY_NOT_READY = ("Spotify isn't connected yet: it needs SPOTIFY_CLIENT_ID in .env and the one-time spotify_login(). "
                     "Tell the user. Pause, play, next and previous still work without it.")

_spotify_client = None


def _spotify_auth():
    return SpotifyPKCE(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        redirect_uri=SPOTIFY_REDIRECT,
        scope=SPOTIFY_SCOPES,
        cache_handler=CacheFileHandler(cache_path=str(SPOTIFY_TOKEN)),
    )


def spotify_login():
    """One-time setup (not a Jarvis tool). In the JARVIS folder with the venv active, run:
        python -c "import tools; tools.spotify_login()"
    Your browser opens, you click Agree, and the login is saved for Jarvis to reuse."""
    if not os.getenv("SPOTIFY_CLIENT_ID"):
        print("Add SPOTIFY_CLIENT_ID=... to .env first.")
        return
    SPOTIFY_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    SPOTIFY_TOKEN.unlink(missing_ok=True)  # an expired login would otherwise block the new one
    devices = spotipy.Spotify(auth_manager=_spotify_auth()).devices()["devices"]
    print("Spotify login saved. Devices I can see:", ", ".join(d["name"] for d in devices) or "none right now")


def _spotify():
    """The Spotify client, or None until SPOTIFY_CLIENT_ID is set and spotify_login() has been run.
    Checking for the saved login first means Jarvis never sits waiting on a browser window."""
    global _spotify_client
    if _spotify_client is None and os.getenv("SPOTIFY_CLIENT_ID") and SPOTIFY_TOKEN.exists():
        _spotify_client = spotipy.Spotify(auth_manager=_spotify_auth(), requests_timeout=10, retries=0)
    return _spotify_client


def _press_media_key(key: int):
    """Tap a keyboard media key. Works on whatever app is playing, no Spotify login needed."""
    ctypes.windll.user32.keybd_event(key, 0, 1, 0)  # 1 = extended key, down
    ctypes.windll.user32.keybd_event(key, 0, 3, 0)  # 3 = extended key, up


def _spotify_error(e) -> str:
    """A Spotify API error as something Jarvis can say."""
    if isinstance(e, spotipy.SpotifyOauthError):
        return "The Spotify login has expired or was refused. The user needs to run spotify_login again."
    text = f"{e.reason} {e.msg}".lower()
    if "premium" in text:
        return "Spotify says that needs Spotify Premium."
    if "no_active_device" in text or e.http_status == 404:
        return "Spotify isn't active on any device right now. Start playing something first."
    if "restriction" in text:
        return "Spotify wouldn't do that, most likely because it's already in that state."
    if e.http_status == 429:
        return "Spotify is rate-limiting me right now. Try again in a bit."
    return f"Spotify error: {e.msg}"


def _spotify_device(sp):
    """The device to play on: whatever is already active, else this PC. Opens Spotify if it's closed."""
    for second in range(10):
        devices = sp.devices()["devices"]
        if devices:
            active = [d for d in devices if d["is_active"]]
            computers = [d for d in devices if d["type"] == "Computer"]
            return (active or computers or devices)[0]["id"]
        if second == 0:
            os.startfile("spotify:")  # Spotify is closed: open it and give it a few seconds to sign in
        time.sleep(1)
    return None


def _spoken(item: dict) -> str:
    """'Blinding Lights by The Weeknd' for songs and albums; just the name for artists and playlists."""
    artists = ", ".join(a["name"] for a in item.get("artists", []))
    return f"{item['name']} by {artists}" if artists else item["name"]


def _spotify_find(sp, query: str, kind: str):
    """Search Spotify -> (uri, spoken name, None) or (None, None, a message for Claude).
    For playlists, the user's own and followed playlists are checked before all of Spotify."""
    if kind == "playlist":
        mine = [p for p in sp.current_user_playlists(limit=50)["items"] if p]
        found, message = _pick(query, [(p["name"], (p["uri"], p["name"]), 0) for p in mine])
        if found:
            return found[0], f"your {found[1]} playlist", None
        if message.startswith("Several"):
            return None, None, message
    results = sp.search(q=query, type=kind, limit=5)
    items = [item for item in results[kind + "s"]["items"] if item]  # Spotify sometimes leaves empty slots
    if not items:
        return None, None, f"Nothing on Spotify matches '{query}'."
    return items[0]["uri"], _spoken(items[0]), None


def spotify_play(query: str, kind: str = "track") -> str:
    """Play a song, album, artist, playlist, or Liked Songs right now."""
    sp = _spotify()
    if sp is None:
        return SPOTIFY_NOT_READY
    try:
        if kind == "liked":
            saved = sp.current_user_saved_tracks(limit=50)["items"]
            uris = [t["track"]["uri"] for t in saved if t and t.get("track")]
            if not uris:
                return "Liked Songs is empty."
            name = "Liked Songs (the 50 most recent)"
        else:
            kind = kind if kind in ("track", "album", "artist", "playlist") else "track"
            uri, name, message = _spotify_find(sp, query, kind)
            if message:
                return message
            uris = [uri] if kind == "track" else None
        device = _spotify_device(sp)
        if device is None:
            return "Spotify didn't come online. Ask the user to open Spotify and try again."
        if uris:
            sp.start_playback(device_id=device, uris=uris)
        else:
            sp.start_playback(device_id=device, context_uri=uri)
        return f"Playing {name}."
    except (spotipy.SpotifyException, spotipy.SpotifyOauthError) as e:
        return _spotify_error(e)


def spotify_queue(query: str) -> str:
    """Add a song to the end of the Spotify queue."""
    sp = _spotify()
    if sp is None:
        return SPOTIFY_NOT_READY
    try:
        uri, name, message = _spotify_find(sp, query, "track")
        if message:
            return message
        sp.add_to_queue(uri)  # goes to whichever device is playing
        return f"Queued {name}."
    except (spotipy.SpotifyException, spotipy.SpotifyOauthError) as e:
        return _spotify_error(e)


def spotify_control(action: str) -> str:
    """Pause, resume, skip, go back, or check what's playing."""
    sp = _spotify()
    if sp is None:
        if action not in MEDIA_KEYS:
            return SPOTIFY_NOT_READY
        _press_media_key(MEDIA_KEYS[action])
        return (f"Pressed the {action} media key. Without the Spotify login I can't check the result, "
                "and play and pause are the same key.")
    try:
        if action == "pause":
            sp.pause_playback()
            return "Paused."
        if action == "play":
            sp.start_playback(device_id=_spotify_device(sp))
            return "Resumed."
        if action == "next":
            sp.next_track()
            return "Skipped to the next song."
        if action == "previous":
            sp.previous_track()
            return "Went back a song."
        playback = sp.current_playback()
        if not playback or not playback.get("item"):
            return "Nothing is playing on Spotify."
        state = "Playing" if playback["is_playing"] else "Paused on"
        return f"{state} {_spoken(playback['item'])}."
    except (spotipy.SpotifyException, spotipy.SpotifyOauthError) as e:
        return _spotify_error(e)


def spotify_now_playing() -> dict:
    """What's playing, as data for the GUI's Now Playing card. Not a Claude tool."""
    sp = _spotify()
    if sp is None:
        return {"connected": False}
    try:
        playback = sp.current_playback()
    except (spotipy.SpotifyException, spotipy.SpotifyOauthError) as e:
        return {"connected": True, "error": _spotify_error(e)}
    item = (playback or {}).get("item")
    if not item:
        return {"connected": True, "playing": False, "title": None}
    album = item.get("album") or {}
    images = album.get("images") or (item.get("images") or [])   # songs have album art; podcast episodes have their own
    art = images[1]["url"] if len(images) > 1 else (images[0]["url"] if images else None)  # the 300 px one
    by = ", ".join(a["name"] for a in item.get("artists", [])) or (item.get("show") or {}).get("name", "")
    return {
        "connected": True,
        "playing": bool(playback.get("is_playing")),
        "title": item.get("name"),
        "artist": by,
        "art": art,
        "progress_ms": playback.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "device": (playback.get("device") or {}).get("name"),
        "url": (item.get("external_urls") or {}).get("spotify"),
    }


# ---------- The chat window: links and screenshots ----------
# gui.py sets CHAT_WINDOW = True. After every reply, brain.py collects these lists so the GUI can
# show links under the reply (instead of Jarvis reading addresses out) and screenshots as thumbnails.

CHAT_WINDOW = False
GUI_TITLE = "J.A.R.V.I.S"   # the dashboard's own window, never picked for a screenshot
CHAT_SEARCH = None          # gui.py sets this to the chat store's search, for search_chats
quiet_reply = None          # set by answer_in_chat: the one line to say while the written answer stays on screen
links_to_show = []          # {"title": ..., "url": ...}
screenshots_to_show = []    # {"label": ..., "image": "data:image/jpeg;base64,..."}, small copies
visuals_to_show = []        # {"id", "kind": mermaid|chart|svg|html, "title", "content"}
files_to_show = []          # {"path", "name", "folder", "action", "size", "image" or "preview", "diff"}
VISUAL_KINDS = ("mermaid", "chart", "svg", "html")
PREVIEW_TYPES = {".py", ".c", ".h", ".cpp", ".java", ".js", ".ts", ".html", ".css", ".json", ".md", ".txt", ".csv",
                 ".xml", ".yml", ".yaml", ".toml", ".ini", ".sql", ".sh", ".ps1", ".bat", ".rs", ".go", ".cs", ".kt", ".m"}


def show_links(links: list) -> str:
    """Put links in the chat window (or the terminal) instead of saying them."""
    good = []
    for link in links if isinstance(links, list) else []:
        url = str(link.get("url", "")).strip() if isinstance(link, dict) else ""
        if re.fullmatch(r"https?://\S+", url):
            good.append({"title": str(link.get("title") or url).strip()[:120], "url": url})
    if not good:
        return "No usable links: each one needs a url starting with https://."
    links_to_show.extend(good)
    where = "the chat window" if CHAT_WINDOW else "the terminal"
    return f"Put {len(good)} link{'s' if len(good) > 1 else ''} in {where}. Don't read the address out; just say where it is."


def answer_in_chat(say: str = "") -> str:
    """Keep this reply on screen and read only one short line out loud."""
    global quiet_reply
    if not CHAT_WINDOW:
        return "There's no chat window in the terminal version, so everything is spoken. Keep the answer short instead."
    quiet_reply = " ".join(str(say or "It's in the chat, sir.").split())[:200]
    return (f'Your written answer will be shown in the chat and not read aloud. Out loud you will say only: '
            f'"{quiet_reply}". Now write the full answer in Markdown, as long and as detailed as it needs to be.')


def create_visual(kind: str, title: str, content: str) -> str:
    """Show a diagram, chart, drawing or small interactive page in the chat window."""
    kind = (kind or "").strip().lower()
    if kind not in VISUAL_KINDS:
        return "kind must be one of: mermaid, chart, svg, html."
    content = (content or "").strip()
    if not content:
        return "The visual is empty."
    if len(content) > 400_000:
        return "That visual is too big (400 KB limit). Simplify it."
    if kind == "chart":
        try:
            spec = json.loads(content)
        except ValueError as e:
            return f"chart content must be a Chart.js config in JSON: {e}"
        if not isinstance(spec, dict) or "data" not in spec:
            return 'The chart JSON needs at least "type" and "data".'
    if kind == "svg" and "<svg" not in content.lower():
        return "svg content must contain an <svg> element."
    if not CHAT_WINDOW:
        return "Visuals need the dashboard (python gui.py). Describe it in words instead."
    title = (title or kind).strip()[:100]
    visuals_to_show.append({"id": uuid.uuid4().hex[:10], "kind": kind, "title": title, "content": content})
    return f"Showing '{title}' in the chat window."


def _file_card(path: Path, action: str, diff: str = None):
    """Describe a file for the chat window: a picture, a preview of the text, or just a card to open."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    card = {"path": str(path), "name": path.name, "folder": str(path.parent), "action": action, "size": size}
    suffix = path.suffix.lower()
    try:
        if suffix in PICTURE_TYPES:
            from PIL import Image
            with Image.open(path) as picture:
                small = _shrink(picture.convert("RGB"), 720)
                card["image"] = "data:image/jpeg;base64," + _jpeg_base64(small, 82)
        elif suffix == ".svg" and size < 300_000:
            card["image"] = "data:image/svg+xml;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        elif (suffix in PREVIEW_TYPES or not suffix) and size < 400_000:
            decoded = _decode(path.read_bytes())
            if decoded:
                lines = decoded[0].splitlines()
                card["preview"] = "\n".join(lines[:80]) + ("\n…" if len(lines) > 80 else "")
                card["language"] = suffix.lstrip(".")
    except Exception:
        pass   # a card without a preview is still useful
    if diff:
        card["diff"] = diff
    files_to_show.append(card)


def show_file(path: str, confirmed: bool = False) -> str:
    """Show a file from the user's PC in the chat window."""
    target, message = _existing_file(path)
    if message:
        return message
    if target.is_dir():
        return f"{target} is a folder. Use list_files or find_files."
    if _is_secret(target):
        return f"Refused: {target.name} holds secrets, so I never show it."
    message = _permission(target, confirmed, "show_file")
    if message:
        return message
    if not CHAT_WINDOW:
        return "Showing files needs the dashboard (python gui.py). Use open_path to open it instead."
    _file_card(target, "shown")
    return f"Showing {target.name} in the chat window."


def search_chats(query: str) -> str:
    """Search the user's past chats."""
    if CHAT_SEARCH is None:
        return "Searching past chats only works in the dashboard (python gui.py)."
    hits = CHAT_SEARCH(query, limit=8, exclude_chat=CURRENT_CHAT)
    if not hits:
        return f"No other chats mention '{query}'."
    lines = [f"- {time.strftime('%b %d, %Y', time.localtime(hit['time']))}, chat \"{hit['chat_title']}\", "
             f"{'user' if hit['role'] == 'user' else 'Jarvis'}: {hit['snippet']}" for hit in hits]
    return f"Matches for '{query}' in other chats:\n" + "\n".join(lines)


# ---------- Screenshots (Windows) ----------
# Captures one app window, found by a rough name, or the whole screen, so Claude can look at it.
# The picture goes to Anthropic like any message does, so this only runs when the user asks.

SHOT_MAX_SIDE = 1568          # Claude sees images best up to about 1568 px on the long side...
SHOT_MAX_PIXELS = 1_150_000   # ...and about 1.15 megapixels; bigger ones only add delay
THUMB_MAX_SIDE = 480          # the copy shown in the chat window
SHOT_FILLER = {"window", "screen", "screenshot", "tab"}   # extra words to ignore in app names
WHOLE_SCREEN = {"", "whole", "entire", "everything", "desktop", "monitor", "display"}

WINDOW_ALIASES = {            # what people say -> the app's process name (without .exe)
    "vs code": ["code"], "vscode": ["code"], "visual studio code": ["code"],
    "browser": ["chrome", "msedge", "firefox", "brave", "opera"], "edge": ["msedge"],
    "file explorer": ["explorer"], "explorer": ["explorer"], "files": ["explorer"],
    "terminal": ["windowsterminal", "powershell", "pwsh", "cmd"],
}


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class _Win32:
    """The Windows API calls screenshots need. Argument types are spelled out so 64-bit handles survive."""

    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi")
        self.EnumProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        signatures = {
            (self.user32, "EnumWindows"): ([self.EnumProc, w.LPARAM], w.BOOL),
            (self.user32, "IsWindowVisible"): ([w.HWND], w.BOOL),
            (self.user32, "IsIconic"): ([w.HWND], w.BOOL),
            (self.user32, "GetWindowTextLengthW"): ([w.HWND], ctypes.c_int),
            (self.user32, "GetWindowTextW"): ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user32, "GetWindowThreadProcessId"): ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.user32, "GetWindowLongPtrW"): ([w.HWND, ctypes.c_int], ctypes.c_ssize_t),
            (self.user32, "GetWindowRect"): ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.user32, "GetForegroundWindow"): ([], w.HWND),
            (self.user32, "ShowWindow"): ([w.HWND, ctypes.c_int], w.BOOL),
            (self.user32, "PrintWindow"): ([w.HWND, w.HDC, w.UINT], w.BOOL),
            (self.user32, "GetDC"): ([w.HWND], w.HDC),
            (self.user32, "ReleaseDC"): ([w.HWND, w.HDC], ctypes.c_int),
            (self.gdi32, "CreateCompatibleDC"): ([w.HDC], w.HDC),
            (self.gdi32, "CreateCompatibleBitmap"): ([w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
            (self.gdi32, "SelectObject"): ([w.HDC, w.HGDIOBJ], w.HGDIOBJ),
            (self.gdi32, "GetDIBits"): ([w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT],
                                        ctypes.c_int),
            (self.gdi32, "DeleteObject"): ([w.HGDIOBJ], w.BOOL),
            (self.gdi32, "DeleteDC"): ([w.HDC], w.BOOL),
            (self.dwmapi, "DwmGetWindowAttribute"): ([w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD], ctypes.c_long),
        }
        for (dll, name), (argtypes, restype) in signatures.items():
            function = getattr(dll, name)
            function.argtypes, function.restype = argtypes, restype
        try:  # Windows 10 1607 and newer: lets us measure windows in real pixels on scaled displays
            self.set_dpi = self.user32.SetThreadDpiAwarenessContext
            self.set_dpi.argtypes, self.set_dpi.restype = [ctypes.c_void_p], ctypes.c_void_p
        except AttributeError:
            self.set_dpi = None


@functools.lru_cache(maxsize=1)
def _win32() -> _Win32:
    return _Win32()


def _open_windows() -> list:
    """(title, process name, handle) for every normal app window, frontmost first."""
    win = _win32()
    try:
        import psutil
    except ImportError:
        psutil = None
    found = []

    def visit(hwnd, _):
        try:
            if not win.user32.IsWindowVisible(hwnd):
                return True
            length = win.user32.GetWindowTextLengthW(hwnd)
            if length == 0 or win.user32.GetWindowLongPtrW(hwnd, -20) & 0x80:   # GWL_EXSTYLE has WS_EX_TOOLWINDOW
                return True
            cloaked = ctypes.c_int(0)   # suspended Store apps say they're visible but are hidden ("cloaked")
            win.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            win.user32.GetWindowTextW(hwnd, title, length + 1)
            if title.value in ("Program Manager", GUI_TITLE):
                return True
            pid = win.w.DWORD()
            win.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = ""
            if psutil:
                try:
                    process = Path(psutil.Process(pid.value).name()).stem
                except (psutil.Error, OSError):
                    pass
            found.append((title.value, process, hwnd))
        except Exception:
            pass   # one odd window shouldn't stop the search
        return True

    win.user32.EnumWindows(win.EnumProc(visit), 0)
    return found


def _pick_window(query: str, windows: list):
    """Rough app name -> (handle, title, None), or (None, None, a message for Claude)."""
    wanted = " ".join(word for word in _clean(query).split() if word not in SHOT_FILLER)
    if not windows:
        return None, None, "There are no open windows to take a screenshot of."
    names = WINDOW_ALIASES.get(wanted, [wanted.replace(" ", "")])
    for title, process, hwnd in windows:   # the app's own name ("discord", "chrome") -> its frontmost window
        if process and process.lower().replace(" ", "") in names:
            return hwnd, title, None
    found, message = _pick(wanted, [(f"{title} {process}", (hwnd, title), -i)
                                    for i, (title, process, hwnd) in enumerate(windows)])
    if message:
        if message.startswith("Nothing"):
            message += " Open windows: " + "; ".join(title for title, _, _ in windows[:12]) + "."
        return None, None, message
    return found[0], found[1], None


def _window_image(hwnd):
    """-> (picture of one window, its box on screen), or (None, None). The window draws itself into
    the picture (PrintWindow), so this works even when other windows are covering it."""
    from PIL import Image
    win, w = _win32(), _win32().w
    previous_dpi = win.set_dpi(ctypes.c_void_p(-4)) if win.set_dpi else None   # -4: per-monitor aware v2
    minimized = bool(win.user32.IsIconic(hwnd))
    if minimized:
        win.user32.ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE: restore it without taking the focus
        time.sleep(0.5)
    try:
        outer, frame = w.RECT(), w.RECT()
        win.user32.GetWindowRect(hwnd, ctypes.byref(outer))
        # The visible frame, without the invisible resize border Windows 10/11 adds around windows
        if win.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)) != 0:
            frame = outer
        width, height = outer.right - outer.left, outer.bottom - outer.top
        if width <= 0 or height <= 0:
            return None, None
        screen_dc = win.user32.GetDC(None)
        memory_dc = win.gdi32.CreateCompatibleDC(screen_dc)
        bitmap = win.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        pixels = ctypes.create_string_buffer(width * height * 4)
        try:
            old = win.gdi32.SelectObject(memory_dc, bitmap)
            win.user32.PrintWindow(hwnd, memory_dc, 2)   # PW_RENDERFULLCONTENT: also works for GPU-drawn apps
            win.gdi32.SelectObject(memory_dc, old)        # GetDIBits needs the bitmap deselected
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height   # negative height: top row first
            info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
            copied = win.gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0)
        finally:
            win.gdi32.DeleteObject(bitmap)
            win.gdi32.DeleteDC(memory_dc)
            win.user32.ReleaseDC(None, screen_dc)
        on_screen = (frame.left, frame.top, frame.right, frame.bottom)
        if not copied:
            return None, on_screen   # the caller can still copy it from the screen instead
        image = Image.frombuffer("RGB", (width, height), pixels, "raw", "BGRX", 0, 1).copy()
        box = (max(0, frame.left - outer.left), max(0, frame.top - outer.top),
               min(width, frame.right - outer.left), min(height, frame.bottom - outer.top))
        if box[2] > box[0] and box[3] > box[1]:
            image = image.crop(box)
        return image, on_screen
    finally:
        if minimized:
            win.user32.ShowWindow(hwnd, 7)   # SW_SHOWMINNOACTIVE: minimize it again
        if previous_dpi:
            win.set_dpi(previous_dpi)


def _is_blank(image) -> bool:
    """Some apps come out solid black from PrintWindow."""
    histogram = image.convert("L").histogram()
    return sum(histogram[:4]) >= 0.98 * sum(histogram)


def _grab_from_screen(hwnd, box):
    """Fallback: bring the window to the front and copy that part of the screen."""
    from PIL import ImageGrab
    win = _win32()
    if win.user32.GetForegroundWindow() != hwnd:
        win.user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE then SW_RESTORE: the dependable way to bring a window forward
        win.user32.ShowWindow(hwnd, 9)
        time.sleep(0.6)
    return ImageGrab.grab(bbox=box, all_screens=True)


def _shrink(image, max_side: int, max_pixels: int = 0):
    """A copy no bigger than max_side on its long side (and max_pixels in total, if given)."""
    from PIL import Image
    scale = min(1.0, max_side / max(image.size))
    if max_pixels:
        scale = min(scale, (max_pixels / (image.width * image.height)) ** 0.5)
    if scale >= 1:
        return image
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _jpeg_base64(image, quality: int) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _for_claude(image, label: str, add_thumbnail: bool = True) -> list:
    """A picture as content blocks Claude can look at, plus (for screenshots) a small copy for the chat window."""
    big = _shrink(image, SHOT_MAX_SIDE, SHOT_MAX_PIXELS)
    shown = ""
    if add_thumbnail:
        small = _shrink(image, THUMB_MAX_SIDE)
        screenshots_to_show.append({"label": label, "image": "data:image/jpeg;base64," + _jpeg_base64(small, 80)})
        shown = " It's also shown in the user's chat window." if CHAT_WINDOW else ""
    return [
        {"type": "text", "text": f"{'Screenshot of ' if add_thumbnail else 'Picture: '}{label} ({big.width}x{big.height}).{shown}"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_base64(big, 85)}},
    ]


def screenshot(app: str = ""):
    """Screenshot an app window (rough name) or the whole screen, so Claude can see it."""
    if sys.platform != "win32":
        return "Screenshots only work on Windows."
    try:
        from PIL import ImageGrab
    except ImportError:
        return "Screenshots need Pillow. The user can install it with: pip install pillow"
    words = [word for word in _clean(app or "").split() if word not in SHOT_FILLER]
    if all(word in WHOLE_SCREEN for word in words):
        return _for_claude(ImageGrab.grab(), "the whole screen")   # the main monitor
    hwnd, title, message = _pick_window(app, _open_windows())
    if message:
        return message
    image, box = _window_image(hwnd)
    if (image is None or _is_blank(image)) and box:
        image = _grab_from_screen(hwnd, box)
    if image is None:
        return f"I couldn't capture '{title}'. It may be hidden in the system tray."
    return _for_claude(image, f"the {title} window")


# ---------- The schema Claude reads ----------
# This tells Claude what tools exist, what they do, and their inputs.

# Shared by the tools that touch folders
ASK_FIRST = (" Works in any folder. The first time a folder is used, the user is asked (in the terminal version it replies "
             "that permission is needed: ask the user, and only call again with confirmed true if they say yes).")
CONFIRMED = {
    "type": "boolean",
    "description": "Terminal version only: set to true when calling again after the user said yes to this folder.",
}
FOLDER = {"type": "string", "description": "A rough folder name or an exact path. Empty means Jarvis's workspace."}
REPO = ("The repo folder: a rough name of a folder on the Desktop or in Documents or Downloads "
        "(e.g. 'csc216'), or an exact path like ~/PycharmProjects/pythonProject")

TOOL_SCHEMA = [
    {
        "name": "git_status",
        "description": "Show the git status (branch + changed files) of a repo." + ASK_FIRST,
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": REPO}, "confirmed": CONFIRMED},
            "required": ["repo"],
        },
    },
    {
        "name": "git_commit_all",
        "description": "Stage all changes and commit them with a message. Never commits .env or key files." + ASK_FIRST,
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": REPO},
                "message": {"type": "string", "description": "Commit message"},
                "confirmed": CONFIRMED,
            },
            "required": ["repo", "message"],
        },
    },
    {
        "name": "git_push",
        "description": "Push committed changes to the remote." + ASK_FIRST,
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": REPO}, "confirmed": CONFIRMED},
            "required": ["repo"],
        },
    },
    {
        "name": "git_pull",
        "description": "Pull latest changes from the remote." + ASK_FIRST,
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": REPO}, "confirmed": CONFIRMED},
            "required": ["repo"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List the folders and files inside a folder on the user's PC (Desktop, Documents, "
            "Downloads, or a project folder), from a rough name (e.g. 'marvel', 'Desktop/NCSU') "
            "or an exact path like ~/Desktop. If it replies with several possible matches, ask "
            "the user which one they meant, then call it again with that exact path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"folder": {"type": "string", "description": "Rough folder name, or an exact path"}},
            "required": ["folder"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file. Text and code come back with line numbers (about 40,000 characters per call; use start_line "
            "to read further), pictures come back as images you can see, and PDFs as documents. Never opens .env or "
            "key files." + ASK_FIRST
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "Exact path (e.g. ~/PycharmProjects/pythonProject/main.py), a path inside Jarvis's "
                                   "workspace, or a rough file name from the Desktop, Documents or Downloads",
                },
                "start_line": {"type": "integer", "description": "First line to read (default 1)"},
                "max_lines": {"type": "integer", "description": "Most lines to read (default: as many as fit)"},
                "confirmed": CONFIRMED,
            },
            "required": ["filepath"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a file, or replace a whole file, with the given text: new code, scripts, notes, data. A relative "
            "path goes in Jarvis's workspace; an exact path like C:/Users/Nikhil/Desktop/development/proj/Main.java "
            "goes exactly there. Existing files are backed up first and keep their line endings. For small changes "
            "to an existing file, use edit_file instead." + ASK_FIRST
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Where to write the file"},
                "content": {"type": "string", "description": "The complete file contents"},
                "confirmed": CONFIRMED,
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Change part of an existing text file by replacing old_text with new_text. old_text must match the file "
            "exactly, indentation included (read the file first, and leave out read_file's line numbers). It must "
            "appear exactly once unless replace_all is true. The file is backed up first, and the change shows in "
            "the chat as a diff." + ASK_FIRST
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Exact path of the file, or a path inside the workspace"},
                "old_text": {"type": "string", "description": "The exact text to replace"},
                "new_text": {"type": "string", "description": "What to put in its place"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
                "confirmed": CONFIRMED,
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "find_files",
        "description": "Find files by name pattern, like '*.py' or 'Main*.java', in a folder and its subfolders (skips .git, venv and node_modules)." + ASK_FIRST,
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "A file name pattern with * and ? wildcards"},
                "folder": FOLDER,
                "confirmed": CONFIRMED,
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "search_text",
        "description": "Search inside files for text or a regular expression, like grep. Returns path:line: text for each match." + ASK_FIRST,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text or a regular expression (case-insensitive)"},
                "folder": FOLDER,
                "file_pattern": {"type": "string", "description": "Only search files whose names match, e.g. '*.c' (default all)"},
                "confirmed": CONFIRMED,
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a PowerShell command on the user's Windows PC and get its output (exit code, then stdout and stderr "
            "together). Use it for real work: compiling, running programs and tests, git, pip installs, checking the "
            "system. The user sees the exact command and approves it first, so write it plainly, one task per call. "
            "Commands can't answer prompts, so pass flags like -y. Dashboard only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The PowerShell command"},
                "folder": {"type": "string", "description": "Where to run it: a rough folder name or an exact path (empty = workspace)"},
                "timeout": {"type": "integer", "description": "Seconds before it's stopped (default 120, max 900)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run a Python script on the user's PC with Jarvis's own Python (numpy, requests and friends available), "
            "after the user approves it, and get its output. Good for calculations, data work, quick experiments, and "
            "making charts or files. Save pictures into the working folder (e.g. plt.savefig('chart.png')): new "
            "pictures appear in the chat automatically. Dashboard only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The complete Python script"},
                "folder": {"type": "string", "description": "Working folder: a rough name or exact path (empty = workspace)"},
                "timeout": {"type": "integer", "description": "Seconds before it's stopped (default 120, max 900)"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "create_visual",
        "description": (
            "Show a visual in the user's chat window when it explains something better than words: a diagram, chart, "
            "drawing or small interactive demo. The user can open it large. kind:\n"
            "- mermaid: Mermaid code (flowchart, sequenceDiagram, classDiagram, stateDiagram-v2, erDiagram, gantt, mindmap, ...)\n"
            "- chart: a Chart.js config as JSON, e.g. {\"type\":\"bar\",\"data\":{\"labels\":[\"A\",\"B\"],"
            "\"datasets\":[{\"label\":\"Score\",\"data\":[3,5]}]}}\n"
            "- svg: a complete <svg> drawing (use a viewBox)\n"
            "- html: a self-contained HTML page with inline CSS and JavaScript (no internet) for interactive demos or animations\n"
            "Prefer mermaid or chart when they fit. For spoken requests, say the visual is in the chat. Dashboard only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(VISUAL_KINDS)},
                "title": {"type": "string", "description": "A short title shown above the visual"},
                "content": {"type": "string", "description": "The Mermaid code, chart JSON, SVG or HTML"},
            },
            "required": ["kind", "title", "content"],
        },
    },
    {
        "name": "show_file",
        "description": (
            "Show a file from the user's PC in the chat window: pictures appear inline, text and code as a preview, "
            "anything else as a card the user can open. Dashboard only." + ASK_FIRST
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Exact path, a path inside the workspace, or a rough file name"},
                "confirmed": CONFIRMED,
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_chats",
        "description": (
            "Search the user's other chats with Jarvis for a topic, name or detail. Use it when the user refers to an "
            "earlier conversation ('what did we decide about...', 'like last time') or when older context would "
            "clearly help. Returns matching snippets with chat titles and dates. Dashboard only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Words to look for"}},
            "required": ["query"],
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
        "name": "run_program",
        "description": (
            "Start a program (.exe) on the user's PC, like double-clicking it. Only use it when the "
            "user asks to run, launch, start or play something. Needs the exact path to the .exe; "
            "if you don't have it, find it first with list_files. If a folder has several .exe files "
            "and it isn't clear which one starts the program, ask the user. Won't run scripts, "
            "shortcuts, or anything in Downloads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Exact path to the .exe"}},
            "required": ["path"],
        },
    },
    {
        "name": "create_note",
        "description": (
            "Create a new note in the user's Obsidian vault when they ask you to write, take or save a "
            "note. The note text is saved as Markdown, so headings, bullet lists and [[links]] are fine "
            "there (the no-markdown rule is only for what you say out loud). folder is optional: a rough "
            "name of an existing vault folder like 'jarvis' or 'concepts', a new folder name, or empty "
            "for the top of the vault. It never overwrites a note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Note title, which becomes the file name"},
                "text": {"type": "string", "description": "The note's contents, in Markdown"},
                "folder": {"type": "string", "description": "Optional vault folder (rough name is fine)"},
            },
            "required": ["title", "text"],
        },
    },
    {
        "name": "add_to_note",
        "description": (
            "Add text to the end of an existing note in the user's Obsidian vault, found by a rough "
            "title like 'shopping list' or 'jarvis ideas'. Write the text as Markdown, e.g. '- eggs' "
            "for a list item. If it replies with several possible matches, ask the user which one they "
            "meant, then call it again with that exact path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Rough title of the note, or its exact path"},
                "text": {"type": "string", "description": "What to add, in Markdown"},
            },
            "required": ["title", "text"],
        },
    },
    {
        "name": "spotify_play",
        "description": (
            "Play something on Spotify right now, replacing what's playing: a song (track), album, "
            "artist, playlist (the user's own playlists are checked first), or their Liked Songs "
            "(kind 'liked'). For songs, put the artist in the query when the user says it, e.g. "
            "'Blinding Lights The Weeknd'. To add a song after the current one instead, use spotify_queue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for (anything for kind 'liked')"},
                "kind": {"type": "string", "enum": ["track", "album", "artist", "playlist", "liked"]},
            },
            "required": ["query", "kind"],
        },
    },
    {
        "name": "spotify_queue",
        "description": (
            "Add a song to the end of the Spotify queue. Songs only: Spotify can't queue whole albums "
            "or playlists. Put the artist in the query when the user says it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The song, ideally with the artist"}},
            "required": ["query"],
        },
    },
    {
        "name": "spotify_control",
        "description": "Control Spotify: pause, play (resume), next, previous, or now_playing to find out what's on.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["play", "pause", "next", "previous", "now_playing"]},
            },
            "required": ["action"],
        },
    },
    {
        "name": "answer_in_chat",
        "description": (
            "Keep this reply in the chat window, to be read with the eyes instead of read aloud. Call it BEFORE "
            "you start writing, whenever the answer is something to look at rather than listen to: the solution to "
            "a problem, an explanation of any length, code, maths, steps, a table or a list. After calling it, "
            "write the full answer in Markdown, as long and as detailed as it needs to be - it is shown on screen, "
            "and the only thing spoken is the one short line you pass in 'say'. Quick facts, confirmations and "
            "conversation don't need this; just say those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "say": {
                    "type": "string",
                    "description": "The one short line to read out, e.g. 'The solution is in the chat, sir.'",
                }
            },
            "required": ["say"],
        },
    },
    {
        "name": "show_links",
        "description": (
            "Put links in the user's chat window instead of saying them. Use it whenever a link would help: "
            "a page they asked for, documentation, a download page, a video. Never read web addresses aloud; "
            "after calling this, just say the link is in the chat. Sources you cite from web search appear "
            "in the chat automatically, so don't repeat those here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Short name for the link"},
                            "url": {"type": "string", "description": "Full address starting with https://"},
                        },
                        "required": ["title", "url"],
                    },
                },
            },
            "required": ["links"],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Take a screenshot so you can see what's on the user's PC and answer questions about it. Give a "
            "rough app name (e.g. 'discord', 'chrome', 'vs code', 'spotify') to capture that app's window, or "
            "leave app empty for the whole screen. Only use it when the user asks you to look at their screen "
            "or an app. If it replies that several windows could match, ask which one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Rough app or window name; empty for the whole screen"},
            },
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a fact or a standing instruction to long-term memory, so you still know it in future "
            "sessions and in every chat. Use it whenever the user asks you to remember something, such as a "
            "repo location, a class schedule, or how they want you to answer. One short, self-contained "
            "line per call, written as an instruction if that is what it is. If it replaces something you "
            "already remember, call forget on the old wording first - never save a second version of the "
            "same rule. Remembering an instruction does not carry it out: follow it in this reply too."
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
    {
        "name": "forget",
        "description": "Remove a fact from long-term memory when the user asks you to forget it or says it's no longer true.",
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "The fact's wording, or a distinctive part of it"}},
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
    "open_path": open_path,
    "run_program": run_program,
    "create_note": create_note,
    "add_to_note": add_to_note,
    "spotify_play": spotify_play,
    "spotify_queue": spotify_queue,
    "spotify_control": spotify_control,
    "show_links": show_links,
    "answer_in_chat": answer_in_chat,
    "screenshot": screenshot,
    "write_file": write_file,
    "edit_file": edit_file,
    "find_files": find_files,
    "search_text": search_text,
    "run_command": run_command,
    "run_python": run_python,
    "create_visual": create_visual,
    "show_file": show_file,
    "search_chats": search_chats,
    "remember": remember,
    "forget": forget,
}
DASHBOARD_ONLY = {"run_command", "run_python", "create_visual", "show_file", "search_chats", "answer_in_chat"}

# ---------- Web (runs on Anthropic's servers — nothing to add to TOOL_FUNCTIONS) ----------
# web_search: Claude searches the web (about 1 cent per search, plus tokens for the results).
# web_fetch:  Claude reads a page it found (no extra fee, just tokens; capped at 5,000 here).
TOOL_SCHEMA += [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 3, "max_content_tokens": 8000},
]


def schema() -> list:
    """The tools to offer Claude: everything in the dashboard; the terminal version skips the dashboard-only ones."""
    return TOOL_SCHEMA if CHAT_WINDOW else [tool for tool in TOOL_SCHEMA if tool.get("name") not in DASHBOARD_ONLY]