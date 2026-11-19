#!/usr/bin/env python3
r"""watch_signed.py - background watcher: a signed sponsor deal rebuilds the kits.

It runs alongside the game (started by KitGenerator.lua when Sider loads) and
polls

    <sider>\content\KitGenerator\kit_request.txt

which the Lua module appends to the moment you sign an offer:

    offer_id|club_id|season|brand|tier|timestamp

For every request it has not seen before it regenerates that club's
home/away/third kits with the signed sponsor (new design + sponsor logo) and
installs them into

    <sider>\content\kits\_KitGenerator\u<team>p<slot>.ftex

which KitGenerator.lua serves to Sider (only while a Master League career is
loaded), so the new kits show at the next match.  Processed offer ids are
remembered in watcher_state.txt, so a request is only ever built once.

Only one watcher may run at a time: it holds a loopback TCP port and a second
copy exits immediately.

Usage
-----
    pythonw watch_signed.py                       # normal, background
    python watch_signed.py --once                 # process pending, then exit
    python watch_signed.py --sider "C:\...\SiderAddons"
"""
import argparse
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

DEFAULT_GAME_ROOT = Path(r"C:\Program Files (x86)\SP Football Life 2027")
LOCK_PORT = 51517
POLL_SECONDS = 2.0

# private store the generated .ftex set is installed into, relative to the sider
# folder (served to the game only in Master League, via the livecpk hooks)
INSTALL_SUBDIR = Path("content/kits/_KitGenerator")


# --------------------------------------------------------------------------- #
#  small helpers
# --------------------------------------------------------------------------- #
def _log(msg, logfile=None):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:                    # a console without the glyphs
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _acquire_lock():
    """Return a listening socket that marks this process as the only watcher."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


def _read_state(state_file):
    if not state_file.is_file():
        return set()
    try:
        text = state_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def _read_pid(pid_file):
    """Return the game's pid from pid_file, or None if it is missing/unreadable."""
    try:
        return int(pid_file.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return None


def _game_alive(pid):
    """True/False if we can tell whether `pid` is running, else None (unknown).

    False is only returned when Windows explicitly says there is no such
    process; anything ambiguous (access denied, API failure) returns None so we
    keep watching rather than shut down by mistake.
    """
    try:
        import ctypes
        ERROR_INVALID_PARAMETER = 87
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
                return False                     # no process with that id
            return None                          # access denied etc: don't guess
        try:
            code = ctypes.c_ulong()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return None
        finally:
            k.CloseHandle(h)
    except Exception:                            # noqa: BLE001
        return None


def _mark_done(state_file, offer_id):
    try:
        with open(state_file, "a", encoding="utf-8") as f:
            f.write(offer_id + "\n")
    except OSError:
        pass


def _parse_request(line):
    """offer_id|club_id|season|brand|tier|timestamp -> dict (or None)."""
    line = line.strip().lstrip("\ufeff")           # tolerate a Notepad BOM
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4:
        return None
    try:
        club_id = int(parts[1])
        season = int(parts[2])
    except (ValueError, IndexError):
        return None
    return {"offer": parts[0], "club_id": club_id, "season": season,
            "brand": parts[3] or None}


# --------------------------------------------------------------------------- #
#  the actual rebuild
# --------------------------------------------------------------------------- #
def rebuild(club_id, season, brand, sider, game_root, logfile):
    """Regenerate one club's kit set with `brand` and install it. Returns the
    list of (slot, ftex, design) build_set produced."""
    import build_kit

    club = build_kit._load_club(club_id, sider)
    logos = Path(sider) / "content" / "KitGenerator" / "logos"
    outdir = Path(sider) / "content" / "KitGenerator" / "staging" / f"season_{season}"

    results = build_kit.build_set(
        club, season, outdir,
        install=True, game_root=game_root, quiet=True,
        sponsor=brand, logo_dir=logos, sider=sider,
    )
    for slot, _atlas, ftex, design in results:
        _log("  built p%d -> %s (pattern=%s sponsor=%s)"
             % (slot, Path(ftex).name, design.get("pattern"), design.get("_brand")),
             logfile)
    return results


def process_pending(req_file, state_file, sider, game_root, logfile):
    """Build every request in req_file that we have not built yet. Returns count."""
    if not req_file.is_file():
        return 0
    seen = _read_state(state_file)
    try:
        lines = req_file.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return 0

    done = 0
    for line in lines:
        req = _parse_request(line)
        if not req or req["offer"] in seen:
            continue
        _log("signed: offer=%s club=%d season=%d sponsor=%s"
             % (req["offer"], req["club_id"], req["season"], req["brand"]), logfile)
        try:
            rebuild(req["club_id"], req["season"], req["brand"], sider, game_root, logfile)
        except Exception as exc:                      # noqa: BLE001 - keep watching
            _log("  ERROR building %s: %s: %s"
                 % (req["offer"], type(exc).__name__, exc), logfile)
            continue
        _mark_done(state_file, req["offer"])
        done += 1
    if done:
        _log("installed %d new kit set(s) - they appear at the next match" % done, logfile)
    return done


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sider", default=str(DEFAULT_GAME_ROOT / "SiderAddons"),
                    help="the game's SiderAddons folder (holds content/ and livecpk/)")
    ap.add_argument("--game-root", default=str(DEFAULT_GAME_ROOT),
                    help="the game install root (for the livecpk install path)")
    ap.add_argument("--poll", type=float, default=POLL_SECONDS,
                    help="seconds between checks (default 2)")
    ap.add_argument("--once", action="store_true",
                    help="process any pending requests, then exit")
    ap.add_argument("--keep-running", action="store_true",
                    help="do not exit when the game process closes (for manual runs)")
    args = ap.parse_args(argv)

    sider = Path(args.sider)
    game_root = Path(args.game_root)
    content = sider / "content" / "KitGenerator"
    req_file = content / "kit_request.txt"
    state_file = content / "watcher_state.txt"
    pid_file = content / "game.pid"
    logfile = content / "watcher.log"

    lock = _acquire_lock()
    if lock is None and not args.once:
        _log("another watcher is already running (port %d) - exiting" % LOCK_PORT,
             logfile)
        return 0

    watch_pid = not (args.once or args.keep_running)
    _log("watcher started (sider=%s, once=%s, watch_pid=%s)"
         % (sider, args.once, watch_pid), logfile)
    dead = 0
    try:
        while True:
            try:
                process_pending(req_file, state_file, sider, game_root, logfile)
            except Exception as exc:                  # noqa: BLE001 - never die
                _log("poll error: %s: %s" % (type(exc).__name__, exc), logfile)
            if args.once:
                break
            if watch_pid:
                pid = _read_pid(pid_file)
                if pid is not None:
                    alive = _game_alive(pid)
                    if alive is False:
                        dead += 1
                        if dead >= 3:                 # ~6 s grace
                            _log("game process %d has closed - watcher exiting" % pid,
                                 logfile)
                            break
                    elif alive is True:
                        dead = 0
            time.sleep(max(0.25, args.poll))
    except KeyboardInterrupt:
        pass
    finally:
        if lock is not None:
            lock.close()
    _log("watcher stopped", logfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
