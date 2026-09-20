"""Uploads navalgaming-logger's output to your Naval Gaming account on the LOM site
(docs/plans/battle-upload.md, docs/plans/voyages.md, docs/plans/battle-diagram.md).

It runs on a thread inside navalgaming_logger.py, so a slow or dead network never
delays a battle read, and nothing here can raise into the logger's loop: a failure
means "try again next time".

A voyage -- one track file, port to port -- goes up as one package once the ship
docks: each battle fought during it, followed by that battle's motion,
broadsides and wind, then the track, then the voyage record. The logger wakes this thread
on arrival; otherwise a cycle runs every 5 minutes and once more when the logger
exits. A cycle sends at most one voyage, the oldest not yet sent. Nothing is sent
from a voyage still at sea.

All the data goes up: every track line and every motion line,
unthinned. The one field kept back is the battle's battle_scene, an address in
the game's memory.

Every cycle also opens with a health ping -- the logger's version and what it is
doing -- so the site can show on your dossier whether the logger is running, and
tell you when a new release is out (docs/plans/logger-health.md). It writes no
records, and a failed ping never holds up a voyage. Until the site first takes one --
a token not yet registered -- it is re-sent every 15 s for up to 30 minutes, so a
new install shows on the dossier moments after Register.

Every write on the site is idempotent, so a voyage cut short by a network failure
is simply sent again, whole, next cycle.

    py upload.py --backfill <the logger's folder>

sends every battle and every finished voyage under that folder, whether sent
before or not -- for data that went up before the site took all of it. It leaves
the logger's upload state alone.

Config: %APPDATA%\\navalgaming-logger\\upload.json

    {"url": "https://lom.navalgaming.com/logger_upload.php", "token": "...",
     "basic_auth": "user:..."}            <- only for a dev site behind HTTP Basic auth

A token is made here, on the machine that uses it, never by the site:

    py upload.py --new-token https://lom.navalgaming.com/logger_upload.php

writes a fresh token into upload.json and prints its SHA-256. Paste that on
the Logger card of your dossier, then restart the logger. Only the hash ever leaves this
machine. The config lives outside the logger's folder, so copying that
folder never carries the token. With no config, nothing uploads.

State: <root>\\upload-state.json -- the voyages sent, each with the site's id for it.
"""
import base64
import calendar
import hashlib
import json
import os
import re
import secrets
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# This logger's release. Sent in every health ping so the site can tell a member
# their logger is behind; keep it equal to LOGGER_VERSION in inc/logger_version.php.
# The date form compares correctly as a plain string, so nothing parses versions.
VERSION = "2026-09-20"
CYCLE_S = 300               # between cycles, when no arrival wakes one sooner
PING_S = 15                 # until the site first takes a ping, re-ping this often...
PING_WINDOW_S = 1800        # ...for at most this long after starting
# Lines per request. The site takes 256 KB a body: a full track point is ~130
# bytes, a broadside ~110, a wind sample ~50, a motion line ~40 (test_upload.py
# checks the worst case).
MAX_POINTS, MAX_BROADSIDES, MAX_MOTION, MAX_WIND = 1500, 1000, 4000, 4000
MAX_AGE_S = 29 * 86400      # the site refuses anything older than 30 days
TIMEOUT_S = 20
REJECTED = (400, 413, 415)  # the payload itself is wrong: retrying cannot help
REACH = 200.0               # world units, a port's join_radius: closer than this went nowhere

# The battle JSON's fields, all sent but battle_scene, an address in the game's memory.
BATTLE_FIELDS = ("started", "ended", "my_ship_id", "location", "reads", "last_read")
SHIP_FIELDS = ("id", "ship_id", "name", "faction", "is_merchant", "is_dead", "kills",
               "assists", "damage_dealt", "side_health", "max_side_health",
               "structure_health", "max_structure_health", "sail_health", "water_frac",
               "in_map", "local", "battery", "player_id")
TRACK_FIELDS = {"sea": ("lat", "lon", "heading", "x", "y", "server_ts"), "port": ("port",),
                "battle": ()}
MOTION_FIELDS = ("id", "server_ts", "x", "y", "heading")
BROADSIDE_FIELDS = ("server_ts", "id", "side", "decks", "guns", "damage", "target")
WIND_FIELDS = ("server_ts", "dir", "speed")


def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "navalgaming-logger", "upload.json")


def check_url(url):
    """https, or plain http to this machine only (the tests' stub server)."""
    u = urllib.parse.urlsplit(url)
    if not u.hostname or not (u.scheme == "https" or
                              (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))):
        raise ValueError("needs an https url")


def dll_build(path):
    """Which game build this is: the gameplay DLL's PE TimeDateStamp, or None.

    The logger's DWARF struct offsets are read out of one file -- that DLL -- so its link
    time is exactly the right thing to call "the build". (The vtable anchor is no longer
    pinned: navalgaming_logger.vtable_rva reads it from whichever DLL is in front of it,
    so what this build number still gates is the offsets.) Keying on the DLL rather
    than on Steam's buildid means a content-only patch, which cannot break the logger,
    raises no alarm; and because a TimeDateStamp is a unix time it still sorts, so the site
    can tell "the game moved past us" from "this machine is behind".

    Four seeks, no hashing: e_lfanew at 0x3C, then the COFF header's TimeDateStamp 8 bytes
    into the PE signature. Opened without locking, so it reads fine while the game runs.

    Returns None on anything unexpected -- a missing file, a non-PE, a zeroed stamp. The
    site treats absent as "not saying", so being wrong here must never cost a battle.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            head = f.read(4)
            if len(head) != 4:
                return None
            pe = struct.unpack("<I", head)[0]
            f.seek(pe)
            rec = f.read(12)                     # signature, then Machine, nsec, stamp
            if len(rec) != 12 or rec[:4] != b"PE\0\0":
                return None
            stamp = struct.unpack_from("<I", rec, 8)[0]
            # 0 means a reproducible build that deliberately erased it; 2015 predates the
            # game. Either way it is not a build identity, so say nothing.
            return stamp if 1420070400 <= stamp <= 4102444800 else None
    except (OSError, struct.error):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would re-send the token header to wherever a redirect points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                             # the 3xx surfaces as an HTTPError: try later


OPENER = urllib.request.build_opener(NoRedirect)


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    check_url(cfg.get("url", ""))
    if not isinstance(cfg.get("token"), str) or not cfg["token"]:
        raise ValueError("needs a token")
    return cfg


def new_token(url, path=None):
    """Write a fresh token for url into the config and return its SHA-256 hex, which
    is all the site is ever given. devlom's basic_auth carries over from the old
    config when the host is the same; for a first dev setup, add it by hand."""
    check_url(url)
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    token = secrets.token_urlsafe(32)                 # 43 characters of base64url
    cfg = {"url": url, "token": token}
    if (isinstance(old, dict) and old.get("basic_auth") and
            urllib.parse.urlsplit(str(old.get("url", ""))).hostname == urllib.parse.urlsplit(url).hostname):
        cfg["basic_auth"] = old["basic_auth"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, path)
    return hashlib.sha256(token.encode()).hexdigest()


class Later(Exception):
    """The site cannot take it now. Stop this cycle and try again next one."""


def epoch(t):
    return calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%SZ"))


def voyage_name(t):
    """The track file of a voyage whose first line is stamped t, named like a
    battle file: 2026-09-11T12:34:56Z -> 20260911-123456.jsonl."""
    return t[:10].replace("-", "") + "-" + t[11:19].replace(":", "") + ".jsonl"


def is_voyage(name):
    """The day files from before voyages (YYYYMMDD.jsonl) were sent under the old
    scheme, and are not voyages."""
    return re.fullmatch(r"\d{8}-\d{6}\.jsonl", name) is not None


def ship_payload(s):
    """player_id is 2**64-1 when unset, which overflows the site's integers: null."""
    return dict({k: s.get(k) for k in SHIP_FIELDS},
                player_id=None if s.get("player_id_unset") else s.get("player_id"))


def battle_payload(b):
    return dict({k: b.get(k) for k in BATTLE_FIELDS},
                ships=[ship_payload(s) for s in b.get("ships", [])])


def track_point(p):
    """The site's form of one track line, or None if it is not one it takes."""
    keys = TRACK_FIELDS.get(p.get("state"))
    if keys is None or not all(k in p for k in keys):
        return None
    return dict({k: p[k] for k in keys}, t=p.get("t"), state=p["state"])


def jsonl(path):
    """A JSON-lines file's complete lines that are objects; [] if there is no file.
    The last line may be half-written by a logger still running."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    out = []
    for raw in data[:data.rfind(b"\n") + 1].splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict):
            out.append(line)
    return out


def closed(lines):
    """A voyage that has reached port, having left it and gone somewhere."""
    return bool(lines) and lines[-1].get("state") == "port" and \
        any(l.get("state") != "port" for l in lines) and not went_nowhere(lines)


def went_nowhere(lines):
    """No battle, and never REACH from where the ship first put to sea: an undock and
    redock. The client can flicker to sea for a single tick while undocking, which
    made prod's voyage 2 (2026-09-11), Messina to Messina in 6 s. A sea line without
    x/y may have gone anywhere, so it counts as having gone somewhere."""
    away = [l for l in lines if l.get("state") != "port"]
    if not away or any(l.get("state") != "sea" or "x" not in l or "y" not in l for l in away):
        return False
    x0, y0 = away[0]["x"], away[0]["y"]
    return all((l["x"] - x0) ** 2 + (l["y"] - y0) ** 2 < REACH ** 2 for l in away)


class Uploader:
    def __init__(self, root, config, log=print, now=time.time):
        self.battles_dir = os.path.join(root, "battles")
        self.track_dir = os.path.join(root, "track")
        self.state_path = os.path.join(root, "upload-state.json")
        self.config, self.log, self.now = config, log, now
        self.state = self.load_state()
        # What the logger is doing, for the health ping: the main thread sets it
        # through Runner.set_state. "waiting" until it has attached to anything.
        self.state_name = "waiting"
        self.told = None                # the newest release already named in the log
        self.greeted = False            # whether the site has taken a ping this run
        # The gameplay DLL, remembered across restarts. Steam patches while the game is
        # shut, and the path is only learnable from a running process -- so without this,
        # a build change would not be reported until the member next played. With it,
        # a machine left on notices within one cycle.
        self.game_dll = self.state.get("game_dll")
        self.told_build = None          # the newest game build already named in the log

    def load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        s = s if isinstance(s, dict) else {}
        voyages = s.get("voyages")
        dll = s.get("game_dll")
        # A state file written before 2026-09-19 has no game_dll, which is simply unknown.
        return {"voyages": voyages if isinstance(voyages, dict) else {},
                "game_dll": dll if isinstance(dll, str) and dll else None}

    def save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def set_game(self, path):
        """Remember the gameplay DLL, so the build is readable with the game closed."""
        if path and path != self.game_dll:
            self.game_dll = self.state["game_dll"] = path
            self.save_state()

    def hello(self):
        """Tell the site the logger is alive, and what it is doing -- one small POST at
        the top of every cycle, so a member can see on their dossier whether it runs
        (docs/plans/logger-health.md). The reply names the current release, and the game
        build the release's offsets were derived from
        (docs/plans/game-build-detection.md)."""
        ping = {"kind": "hello", "version": VERSION, "state": self.state_name}
        # Omitted, not null, when unknown: a logger that has never seen the game run
        # cannot say, and the site must not read that as a mismatch.
        build = dll_build(self.game_dll) if self.game_dll else None
        if build is not None:
            ping["build"] = build
        reply = self.post(ping)
        if not self.greeted:
            self.greeted = True
            self.log("upload: the site accepted this machine's token")
        current = (reply or {}).get("current_version")
        # Once per release, not once per cycle: this runs every 5 minutes.
        if isinstance(current, str) and current > VERSION and current != self.told:
            self.told = current
            self.log(f"upload: version {VERSION} -- version {current} is available. "
                     "Download the new release.")
        # The same guard for the game build. A member who never opens their dossier still
        # finds out here why their battles stopped -- and that it is not theirs to fix.
        ours = (reply or {}).get("current_build")
        if (build is not None and isinstance(ours, int) and build > ours
                and build != self.told_build):
            self.told_build = build
            self.log(f"upload: the game has updated (build {build}); this release reads "
                     f"build {ours}, and the offsets it uses to find the game are per "
                     "build. A new logger release is needed -- "
                     "github.com/jimbursch1/navalgaming-logger/releases/latest")

    def cycle(self):
        """Send the oldest voyage not yet sent, if it has reached port. Never raises."""
        try:
            self.hello()
        except Exception:
            pass                        # a lost ping must never hold up a voyage
        try:
            self.voyages()
        except Later as e:
            self.log(f"upload: {e} -- trying again in {CYCLE_S // 60} min")
        except Exception as e:
            self.log(f"upload: {type(e).__name__}: {e}")

    def post(self, payload):
        """The site's reply on success, None if it rejected the payload; raises Later
        for anything retrying might fix."""
        headers = {"Content-Type": "application/json", "User-Agent": "navalgaming-logger",
                   "X-NavalGaming-Logger-Token": self.config["token"]}
        if self.config.get("basic_auth"):
            headers["Authorization"] = "Basic " + base64.b64encode(
                self.config["basic_auth"].encode()).decode()
        body = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(self.config["url"], data=body, headers=headers, method="POST")
        try:
            with OPENER.open(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error", "")
            except Exception:
                detail = ""
            if e.code in REJECTED:
                self.log(f"upload: rejected, HTTP {e.code}: {detail}")
                return None
            raise Later(f"HTTP {e.code} {detail}".strip())
        except Exception as e:                  # refused, timed out, reset, a bad reply
            raise Later(f"{type(e).__name__}: {e}")

    def voyages(self):
        try:
            names = sorted(n for n in os.listdir(self.track_dir) if is_voyage(n))
        except FileNotFoundError:
            return
        for name in names:
            if name in self.state["voyages"]:
                continue
            lines = self.read_lines(os.path.join(self.track_dir, name))
            if not closed(lines):
                continue                        # still at sea, never left port, or went nowhere
            self.send(name, lines)
            return

    def read_lines(self, path):
        """A track file's complete, well-formed lines."""
        lines = []
        for line in jsonl(path):
            try:
                epoch(line["t"])
                lines.append(line)
            except (ValueError, TypeError, KeyError):
                continue
        return lines

    def battles(self):
        """Every battle file, as (name, battle)."""
        try:
            names = sorted(n for n in os.listdir(self.battles_dir) if n.endswith(".json"))
        except FileNotFoundError:
            return []
        found = []
        for name in names:
            try:
                with open(os.path.join(self.battles_dir, name), encoding="utf-8") as f:
                    b = json.load(f)
            except (OSError, ValueError):
                continue                        # not ours
            if isinstance(b, dict) and isinstance(b.get("started"), str):
                found.append((name, b))
        return found

    def battles_in(self, departed, arrived):
        """The battles started between a voyage's first and last lines. The ship is in
        port, so each is over, whether or not the logger lived to set `ended`."""
        return [(n, b) for n, b in self.battles() if departed <= b["started"] <= arrived]

    def send(self, name, lines):
        """One voyage: its battles, its track, then the voyage record. Only once the
        record is taken is the voyage marked sent."""
        for bname, b in self.battles_in(lines[0]["t"], lines[-1]["t"]):
            self.send_battle(bname, b)
        reply = self.send_voyage(name, lines)
        # A rejected record is marked sent anyway, so one bad file cannot block the queue.
        self.state["voyages"][name] = (reply or {}).get("voyage_id")
        self.save_state()

    def send_battle(self, name, b):
        """A battle, then its motion, broadsides and wind, which the site files under
        the battle's start. A battle the site rejected has nothing to file them under.
        A battle fought before the logger read the wind has no wind file, and sends none."""
        reply = self.post({"kind": "battle", "battle": battle_payload(b)})
        if reply is None:
            return
        stem = os.path.join(self.battles_dir, name[:-len(".json")])
        motion = [[m[k] for k in MOTION_FIELDS] for m in jsonl(stem + ".motion.jsonl")
                  if all(k in m for k in MOTION_FIELDS)]
        broadsides = [{k: s.get(k) for k in BROADSIDE_FIELDS}
                      for s in jsonl(stem + ".broadsides.jsonl") if "server_ts" in s and "id" in s]
        wind = [{k: w.get(k) for k in WIND_FIELDS}
                for w in jsonl(stem + ".wind.jsonl") if "server_ts" in w and "dir" in w]
        for kind, rows, n in (("motion", motion, MAX_MOTION),
                              ("broadsides", broadsides, MAX_BROADSIDES),
                              ("wind", wind, MAX_WIND)):
            for i in range(0, len(rows), n):
                self.post({"kind": kind, "started": b["started"], kind: rows[i:i + n]})
        self.log(f"upload: battle {name} -> #{reply.get('battle_id')}, "
                 f"{len(motion)} motion lines, {len(broadsides)} broadsides, {len(wind)} wind samples")

    def send_voyage(self, name, lines):
        """A voyage's track, every line of it, then its record. Returns the site's
        reply to the record, or None if it was rejected."""
        points = [p for p in map(track_point, lines)
                  if p and self.now() - epoch(p["t"]) < MAX_AGE_S]
        stored = 0
        for i in range(0, len(points), MAX_POINTS):
            reply = self.post({"kind": "track", "points": points[i:i + MAX_POINTS]})
            stored += (reply or {}).get("stored", 0)
        first = lines[0]
        reply = self.post({"kind": "voyage", "voyage": {
            "departed": lines[0]["t"], "arrived": lines[-1]["t"],
            "from_port": first.get("port") if first.get("state") == "port" else None,
            "to_port": lines[-1].get("port")}})
        self.log(f"upload: voyage {name}: {len(points)} points sent, {stored} rows stored"
                 + (f" -> #{reply.get('voyage_id')}" if reply else ""))
        return reply

    def backfill(self):
        """Every battle, then every finished voyage, whether sent before or not. Raises
        Later if the site cannot take it now; the upload state is not touched."""
        for name, b in self.battles():
            self.send_battle(name, b)
        try:
            names = sorted(n for n in os.listdir(self.track_dir) if is_voyage(n))
        except FileNotFoundError:
            names = []
        for name in names:
            lines = self.read_lines(os.path.join(self.track_dir, name))
            if closed(lines):
                self.send_voyage(name, lines)


class Runner:
    """Runs cycles on a daemon thread; stop() runs one last cycle, then returns."""

    def __init__(self, uploader):
        self.up, self.wake, self.stopping = uploader, threading.Event(), False
        self.thread = threading.Thread(target=self.run, name="upload", daemon=True)
        self.started = time.monotonic()

    def run(self):
        while not self.stopping:
            self.up.cycle()
            if self.pause():
                self.wake.clear()               # or every wait after the first nudge returns at once
        self.up.cycle()                         # a voyage that closed just before exit

    def pause(self):
        """Wait out CYCLE_S between cycles; True if woken early.

        A new install makes its token before the player has registered it, so its first
        pings are refused. Until the site takes one, and for at most PING_WINDOW_S after
        starting, re-ping every PING_S instead, and run a cycle as soon as one is taken:
        the dossier then shows the logger seconds after Register, not minutes. The site
        writes nothing for an unknown token, so these pings cost it one indexed read."""
        end = time.monotonic() + CYCLE_S
        while not self.up.greeted and time.monotonic() - self.started < PING_WINDOW_S:
            if self.wake.wait(PING_S):
                return True
            try:
                self.up.hello()
                return True
            except Exception:
                pass
        return self.wake.wait(max(0.0, end - time.monotonic()))

    def set_state(self, name):
        """What the next health ping reports: attached, loading or waiting."""
        self.up.state_name = name

    def set_game(self, path):
        """Where the gameplay DLL is, so the ping can say which build this machine has."""
        self.up.set_game(path)

    def nudge(self):
        """Run a cycle now instead of waiting out the rest of CYCLE_S."""
        self.wake.set()

    def stop(self, timeout=120):
        self.stopping = True
        self.wake.set()
        self.thread.join(timeout)


def start(root, path=None, log=print):
    """Start uploading from root, or return None if there is no usable config."""
    path = path or config_path()
    try:
        cfg = load_config(path)
    except FileNotFoundError:
        log(f"upload: off -- no config at {path}")
        return None
    except Exception as e:
        log(f"upload: off -- {path}: {e}")
        return None
    runner = Runner(Uploader(root, cfg, log))
    runner.thread.start()
    log(f"upload: each voyage on arrival in port, to {cfg['url']}")
    return runner


def main(argv):
    if len(argv) != 2 or argv[0] not in ("--new-token", "--backfill", "--build"):
        raise SystemExit("usage: py upload.py --new-token https://<site>/logger_upload.php\n"
                         "       py upload.py --backfill <the logger's folder>\n"
                         "       py upload.py --build <the gameplay DLL>")
    if argv[0] == "--build":
        # For the maintainer: this is the number that goes in LOGGER_GAME_BUILD
        # (inc/logger_version.php) when a build is declared compatible.
        b = dll_build(argv[1])
        if b is None:
            raise SystemExit(f"{argv[1]}: no usable PE TimeDateStamp")
        print(f"{b}  ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(b))} UTC)")
        return
    if argv[0] == "--backfill":
        try:
            up = Uploader(argv[1], load_config(config_path()))
            up.backfill()
        except (OSError, ValueError, Later) as e:
            raise SystemExit(f"backfill stopped: {e}")
        return
    url = argv[1]
    try:
        digest = new_token(url)
    except ValueError as e:
        raise SystemExit(f"{url}: {e}")
    site = urllib.parse.urlsplit(url)
    copied = copy_to_clipboard(digest)
    print(f"New token saved in {config_path()}; it stays on this machine.\n"
          f"Register it on your dossier's Logger card, at {site.scheme}://{site.netloc}/, with this SHA-256"
          f"{' (already copied: just paste it)' if copied else ''}:\n\n"
          f"    {digest}\n\n"
          f"A logger already running keeps its old token until restarted (start.ps1).")


def copy_to_clipboard(text):
    """Put text on the Windows clipboard, so the hash can be pasted into the site.
    Only the hash: the token itself never goes near the clipboard."""
    if os.name != "nt":
        return False
    import subprocess
    try:
        return subprocess.run(["clip"], input=text.encode(), timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


if __name__ == "__main__":
    main(sys.argv[1:])
