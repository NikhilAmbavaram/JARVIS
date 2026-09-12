# launch.pyw — what the Desktop shortcut runs: the dashboard, with no console window.
#
# Windows runs .pyw files with pythonw.exe, which has no console at all, so everything Jarvis would
# have printed goes to jarvis.log instead. If the shortcut seems to do nothing, read that file.
#
# Double-clicking this file works too. Running it while Jarvis is already up just brings the
# dashboard window back rather than starting a second copy (two copies would fight over the mic).

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
LOG = HERE / "jarvis.log"
DEFAULT_PORT = 8765

sys.path.insert(0, str(HERE))   # so the imports work no matter which folder it was started from


def already_running(port: int) -> bool:
    """True if Jarvis himself is on that port. Something else listening there doesn't count:
    gui.main() would just pick the next free port."""
    with socket.socket() as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", port)) != 0:
            return False
    try:
        from urllib.request import urlopen
        with urlopen(f"http://127.0.0.1:{port}/", timeout=1.5) as page:
            return b"J.A.R.V.I.S" in page.read(4000)
    except OSError:
        return False


def main():
    import gui
    if already_running(DEFAULT_PORT):
        print(f"Jarvis is already running; opening the dashboard at port {DEFAULT_PORT}.")
        gui.open_window(SimpleNamespace(started=True), f"http://127.0.0.1:{DEFAULT_PORT}/", browser_tab=False)
        return
    gui.main()


if __name__ == "__main__":
    sys.stdout = sys.stderr = LOG.open("w", encoding="utf-8", errors="replace", buffering=1)
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()   # the log is the only place an error can show up
        raise
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
