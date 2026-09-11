# tools.py — the things Jarvis can DO (Phase 4 tools + the Phase 5 remember tool)

import os
import subprocess
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

from memory import remember

load_dotenv(Path(__file__).with_name(".env"))  # so SPOTIFY_CLIENT_ID is there even when tools.py runs on its own

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
    "open_path": open_path,
    "run_program": run_program,
    "create_note": create_note,
    "add_to_note": add_to_note,
    "spotify_play": spotify_play,
    "spotify_queue": spotify_queue,
    "spotify_control": spotify_control,
    "remember": remember,
}

# ---------- Web (runs on Anthropic's servers — nothing to add to TOOL_FUNCTIONS) ----------
# web_search: Claude searches the web (about 1 cent per search, plus tokens for the results).
# web_fetch:  Claude reads a page it found (no extra fee, just tokens; capped at 5,000 here).
TOOL_SCHEMA += [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 5000},
]