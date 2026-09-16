"""navalgaming-logger: log your battles and your ship's track out of the running
Letters of Marque client -- no dump, no freeze.

It records four things, all about your own ship's game:
  - each battle's scoreboard, as the Tab screen shows it, one JSON per battle
  - each battle's maneuvering: every ship's position and heading once a second,
    one JSON-lines file beside the battle's JSON
  - each battle's broadsides: every ship's, with the damage each did, another
    JSON-lines file beside it
  - where your ship is (at sea, in port, in battle), one JSON-lines file per voyage
It opens the client read-only, and never reads the ships around you on the
overworld or the server connection's `auth_key`.

`tools/gamefiles/dumpbattle.py` gets the battle by suspending the game ~35 s, writing a 7.5 GB
full-memory dump and scanning it for ~6 min. The data itself is a few KB: the
`battle_ship` records in `opaque.ships`, which is what the Tab scoreboard
(`battle_scene::display_scoreboard` -- Name, Ship, Kills, Assists, Damage,
Status) draws from every frame. So read those few KB with ReadProcessMemory
while the game runs.

**No scan per battle.** `scene_root` lives for the whole game session and is a
godot-cpp class, so each instance begins with a C++ vptr into the gameplay DLL.
Find it once with an exact 8-byte search, then every read is a pointer chain:

    scene_root +3056 bs -> battle_scene +256 o -> opaque +48 ships (std::map)
                           battle_scene +40 my_ship_id

The tree walk and ship parsing are `dumpbattle.py`'s, unchanged; the key == id
check it relies on also catches a read torn by the game writing mid-read.

The same anchor reaches the overworld (GAMEFILES.md 7.12), so each tick also
logs where the local ship is -- at sea, in port, or in battle -- to a track
file. Only the local ship: `overworld.other` is never read.

Only parsed fields are written. The reader never touches `pipe`, so the session
`auth_key` (GAMEFILES.md 7.11) cannot land in an output file.

It runs from logon to logoff, started by the "NavalGaming Logger" scheduled task
(install.ps1): it waits for the client and for the world to load, logs, and
goes back to waiting when the client closes. Under pythonw, as the task runs it,
there is no console, so it writes its own log, LOG_PATH.

Usage:  py navalgaming_logger.py         log; one JSON per battle in OUT_DIR, and the
                                        track in TRACK_DIR, one JSON-lines file per voyage
        py navalgaming_logger.py --once  print the current battle and position
"""
import json
import math
import os
import struct
import sys
import threading
import time
import traceback

# dumpbattle.py sits next to this file when installed, and in ../gamefiles in the lom repo.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "gamefiles")]
import dumpbattle as D
import upload

EXE = "LettersOfMarque.exe"
ROOT = HERE                 # the logger keeps its records beside itself, wherever it was put
OUT_DIR = os.path.join(ROOT, "battles")
TRACK_DIR = os.path.join(ROOT, "track")
LOG_PATH = os.path.join(ROOT, "navalgaming-logger.log")
LOG_MAX = 5 << 20           # bytes; then the log starts afresh, the old one kept as .1
WAIT_S = 15.0               # between looks for the client while it is not running
SCAN_S = 60.0               # between scene_root scans (~5 s each) while the world loads
TICK_S = 3.0
BATTLE_TICK_S = 1.0         # while a battle is loaded: a replay needs finer steps than a scoreboard
TORN_LIMIT = 9              # consecutive bad reads before a battle counts as over (~9 s)
CHUNK = 1 << 26

# 2026-09-10 DLL. The vtable is from the COFF symbol table, the offsets from DWARF.
VTABLE_RVA = 0x132D200      # _ZTV10scene_root
VPTR_RVA = VTABLE_RVA + 16  # an instance's vptr skips offset-to-top and typeinfo
SR_BS = 3056                # scene_root.bs : battle_scene*
BS_MY_SHIP = 40             # battle_scene.my_ship_id : uint64_t
BS_OPAQUE = 256             # battle_scene.o : opaque*
BS_BATTLE_POS = 8           # battle_scene.server_battle_pos : vec2f, overworld coordinates
BS_WIND = 56                # battle_scene.wind_i : interpolated_value<vec2f>
BSHIP_POS, BSHIP_DIR = 80, 128  # battle_ship; interpolated_value<vec3f>, <vec2f>

# Gunnery, GAMEFILES.md 7.11 ("Gunnery -- verified live").
BSHIP_GUNS = 872            # battle_ship.cannons_static : vector<network_cannon_constant_state>
GUN_SIZE, GUN_DECK, GUN_MODEL = 88, 4, 40  # int deck, side, pd at +4; string model_name at +40
BSHIP_FIRE = 920            # battle_ship.cannon_fire : map<string, cannon_fire_data>
FIRE_KEY, FIRE_START = 32, 96  # map node: key string; value +64, whose evt.start is at +32
SIDES = ("port", "starboard", "bow", "stern")
RIPPLE_S = 1.0              # a gap longer than this on one side starts a new broadside
LEAD_S = 7.0                # ...except a lone gun, which folds into a run starting this soon after
DAMAGE_S = 4.0              # hits land up to ~3 s after firing at 778 units

# The overworld and the port, also DWARF (GAMEFILES.md 7.12).
SR_PLAYER = 2232 + 136      # scene_root.overworld.player : overworld_ship
SR_PORT = 1152 + 336        # scene_root.port_manage.last_update : optional<server_port_update>
PORT_ENGAGED, PORT_ID = 80, 8
SHIP_POS, SHIP_DIR = 16, 64  # overworld_ship; both interpolated_value<vec2f>
IV_TIMES = 24               # interpolated_value: vector<T> +0, vector<double> timestamps +24

# World -> lat/long: GAMEFILES.md 11.5, the "projection" in data/ports_geo.json.
GEO_K, GEO_X0, GEO_Y0 = 392793.1357530084, -367537.1703285571, -66630.4331419439

OK, NONE, TORN, GONE = "ok", "none", "torn", "gone"


def u64(b):
    return struct.unpack("<Q", b)[0]


# --- the live process ----------------------------------------------------------

class ProcessReader:
    """ReadProcessMemory behind dumpbattle.VAReader's read(va, n) interface."""

    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes as W
        self.ct = ctypes
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = W.HANDLE
        k.OpenProcess.argtypes = (W.DWORD, W.BOOL, W.DWORD)
        k.ReadProcessMemory.argtypes = (W.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
        k.VirtualQueryEx.restype = ctypes.c_size_t
        k.VirtualQueryEx.argtypes = (W.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t)

        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
                        ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD),
                        ("RegionSize", ctypes.c_size_t), ("State", W.DWORD),
                        ("Protect", W.DWORD), ("Type", W.DWORD)]
        self.MBI = MBI
        # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION: read-only, and less than
        # MiniDumpWriteDump already asks for.
        self.h = k.OpenProcess(0x0010 | 0x0400, False, pid)
        if not self.h:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid})")

    def close(self):
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None

    def read(self, va, n):
        buf = self.ct.create_string_buffer(n)
        got = self.ct.c_size_t()
        if not self.k.ReadProcessMemory(self.h, self.ct.c_void_p(va), buf, n,
                                        self.ct.byref(got)) or got.value != n:
            return None
        return buf.raw

    def regions(self):
        """Committed private read-write memory -- the heap, where scene_root lives."""
        mbi, va = self.MBI(), 0
        while self.k.VirtualQueryEx(self.h, self.ct.c_void_p(va), self.ct.byref(mbi),
                                    self.ct.sizeof(mbi)):
            if (mbi.State == 0x1000 and mbi.Type == 0x20000      # MEM_COMMIT, MEM_PRIVATE
                    and mbi.Protect == 0x04):                     # PAGE_READWRITE, no guard
                yield mbi.BaseAddress, mbi.RegionSize
            va = mbi.BaseAddress + mbi.RegionSize


def find_process(exe=EXE, module=D.DLL_NAME):
    """(pid, gameplay DLL base) via a Toolhelp snapshot, or (None, None)."""
    import ctypes
    from ctypes import wintypes as W
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = W.HANDLE

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD),
                    ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                    ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD),
                    ("szExeFile", W.WCHAR * 260)]

    class ME(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("th32ModuleID", W.DWORD), ("th32ProcessID", W.DWORD),
                    ("GlblcntUsage", W.DWORD), ("ProccntUsage", W.DWORD),
                    ("modBaseAddr", ctypes.c_uint64), ("modBaseSize", W.DWORD),
                    ("hModule", W.HMODULE), ("szModule", W.WCHAR * 256),
                    ("szExePath", W.WCHAR * 260)]

    k.CreateToolhelp32Snapshot.argtypes = (W.DWORD, W.DWORD)
    k.CloseHandle.argtypes = (W.HANDLE,)
    for fn in (k.Process32FirstW, k.Process32NextW, k.Module32FirstW, k.Module32NextW):
        fn.argtypes = (W.HANDLE, ctypes.c_void_p)
        fn.restype = W.BOOL

    def entries(flags, pid, cls, first, nxt):
        snap = k.CreateToolhelp32Snapshot(flags, pid)
        if snap in (None, W.HANDLE(-1).value):
            return
        e = cls()
        e.dwSize = ctypes.sizeof(cls)
        ok = first(snap, ctypes.byref(e))
        while ok:
            yield e
            ok = nxt(snap, ctypes.byref(e))
        k.CloseHandle(snap)

    pid = next((e.th32ProcessID for e in entries(0x2, 0, PE, k.Process32FirstW,
                                                  k.Process32NextW)
                if e.szExeFile.lower() == exe.lower()), None)
    if pid is None:
        return None, None
    base = next((e.modBaseAddr for e in entries(0x8 | 0x10, pid, ME, k.Module32FirstW,
                                                 k.Module32NextW)
                 if module.lower() in e.szModule.lower()), None)
    return pid, base


# --- finding scene_root, and reading the battle under it ------------------------

def find_roots(rd, vptr):
    """Every 8-aligned heap address holding scene_root's vptr, plus bytes scanned."""
    needle = struct.pack("<Q", vptr)
    hits, scanned = [], 0
    for base, size in rd.regions():
        for off in range(0, size, CHUNK):       # CHUNK is 8-aligned: nothing straddles
            buf = rd.read(base + off, min(CHUNK, size - off))
            if buf is None:
                continue                         # decommitted since the query; fine
            scanned += len(buf)
            i = buf.find(needle)
            while i != -1:
                if (base + off + i) % 8 == 0:
                    hits.append(base + off + i)
                i = buf.find(needle, i + 1)
    return hits, scanned


def read_battle(rd, roots, vptr, known=()):
    """(state, snapshot). GONE when no root still carries the vptr; NONE when no
    battle is loaded; TORN when a battle is loaded but the read did not validate.

    Batteries are read only for ships not in `known`: they never change."""
    alive, torn = False, False
    for sr in roots:
        b = rd.read(sr, 8)
        if not b or u64(b) != vptr:
            continue
        alive = True
        b = rd.read(sr + SR_BS, 8)
        bs = u64(b) if b else 0
        if not bs:
            continue
        mine, op = rd.read(bs + BS_MY_SHIP, 8), rd.read(bs + BS_OPAQUE, 8)
        cnt = rd.read(u64(op) + D.SHIPS_MAP + 40, 8) if op else None
        if not (mine and cnt and 0 < u64(cnt) < 4096):
            torn = True
            continue
        opaque, cnt = u64(op), u64(cnt)
        nodes = D.walk_tree(rd, opaque + D.SHIPS_MAP, cnt)
        if len(nodes) != cnt or not D.keys_agree(rd, nodes):
            torn = True
            continue
        ships, motion, fires, batteries = [], [], {}, {}
        for nd in nodes:
            va = nd + D.NODE_VALUE
            rec = rd.read(va, D.SHIP_SIZE)
            if not rec:
                break
            s = D.parse_ship(rec, va, rd)
            del s["va"]
            ships.append(s)
            m = read_motion(rd, va)
            if m:
                motion.append({"id": s["id"], **m})
            fires[s["id"]] = read_fires(rd, va)
            if s["id"] not in known:
                guns = read_battery(rd, va)
                if guns:                         # empty may just be not loaded yet: retry
                    batteries[s["id"]] = guns
        if len(ships) != cnt:
            torn = True
            continue
        return OK, {"battle_scene": bs, "my_ship_id": u64(mine), "ships": ships,
                    "motion": motion, "fires": fires, "batteries": batteries,
                    "location": battle_location(rd, bs), "wind": read_wind(rd, bs)}
    if not alive:
        return GONE, None
    return (TORN if torn else NONE), None


# --- one battle at a time -------------------------------------------------------

SCORE_FIELDS = ("kills", "assists", "damage_dealt", "is_dead", "structure_health",
                "side_health", "sail_health", "water_frac")


def side_name(side):
    return SIDES[side] if 0 <= side < len(SIDES) else str(side)


def battery_rows(guns):
    """A battery as the battle JSON carries it: gun count per side, deck and pd."""
    rows = {}
    for g in guns:
        k = (g["side"], g["deck"], g["pd"])
        rows[k] = rows.get(k, 0) + 1
    return [{"side": side_name(s), "deck": d, "pd": pd, "guns": n}
            for (s, d, pd), n in sorted(rows.items())]


class Broadsides:
    """Turns each tick's cannon_fire maps into one JSON line per broadside.

    A gun fired when its key's start changes. A broadside is one ship's guns on
    one side, fired as one order: they ripple out a few tenths of a second apart,
    so a gap over RIPPLE_S starts a new one -- except that most broadsides lead
    with a lone gun 1.6-6.3 s early, which folds into the run after it. Grouping
    is on each firing's server time, never on the tick it was read on.

    A line is written once DAMAGE_S has passed since the last gun. `damage` is
    the firing ship's damage_dealt rise over that window; a rise is claimed by
    one broadside only, so overlapping windows cannot double-count. `target` is
    the one other ship whose sides, structure or sails fell, or null if none or
    several did. At contact range a collision raises both ships' damage_dealt at
    once, and one inside a window is counted with the broadside (GAMEFILES.md 7.11).
    """

    def __init__(self, path):
        self.path = path
        self.guns = {}        # ship id -> {model: (side, deck)}
        self.seen = {}        # ship id -> {model: start} as last read
        self.pending = {}     # (ship id, side) -> [(start, deck)], not yet grouped for good
        self.ready = []       # grouped, waiting for their damage window
        self.score = {}       # ship id -> [(server_ts, damage_dealt, (sides, structure, sails))]
        self.claimed = {}     # ship id -> server time its damage is claimed up to

    def step(self, snap, now):
        for sid, guns in snap.get("batteries", {}).items():
            self.guns[sid] = {g["model"]: (g["side"], g["deck"]) for g in guns}
        for s in snap["ships"]:
            hist = self.score.setdefault(s["id"], [])
            hist.append((now, s["damage_dealt"],
                         (*s["side_health"], s["structure_health"], s["sail_health"])))
            del hist[:-300]
        for sid, fires in snap.get("fires", {}).items():
            if fires is None or sid not in self.guns:
                continue                          # torn, or the battery not read yet
            old, self.seen[sid] = self.seen.get(sid), fires
            if old is None:
                continue                          # first sight: earlier firings are history
            for model, start in fires.items():
                if old.get(model) != start and model in self.guns[sid]:
                    side, deck = self.guns[sid][model]
                    self.pending.setdefault((sid, side), []).append((start, deck))
        self.group(now)
        self.flush(now)

    def group(self, now):
        """Move every run that can no longer grow from pending to ready. One tick
        of slack: a gun that fired just before `now` may not have been read yet."""
        for (sid, side), fired in list(self.pending.items()):
            fired.sort()
            runs = [[fired[0]]]
            for f in fired[1:]:
                if f[0] - runs[-1][-1][0] > RIPPLE_S:
                    runs.append([])
                runs[-1].append(f)
            # Decided before folding: two lead guns fold into a run of two, which
            # must still wait for the broadside they lead (seen live, 2447.80 and
            # 2449.55 then 50 guns at 2454.14).
            tail = runs[-1]
            open_ = (now - tail[-1][0] <= RIPPLE_S + BATTLE_TICK_S
                     or len(tail) == 1 and now - tail[0][0] <= LEAD_S + BATTLE_TICK_S)
            for i in range(len(runs) - 2, -1, -1):     # back to front, so leads chain
                if len(runs[i]) == 1 and runs[i + 1][0][0] - runs[i][0][0] <= LEAD_S:
                    runs[i:i + 2] = [runs[i] + runs[i + 1]]
            last = runs[-1]
            done = runs[:-1] if open_ else runs
            self.ready += [(sid, side, run) for run in done]
            if open_:
                self.pending[(sid, side)] = last
            else:
                del self.pending[(sid, side)]

    def flush(self, now):
        due = sorted((r for r in self.ready if now - r[2][-1][0] >= DAMAGE_S),
                     key=lambda r: r[2][-1][0])
        self.ready = [r for r in self.ready if r not in due]
        for sid, side, run in due:
            self.write(sid, side, run)

    def finish(self):
        """Battle over: whatever is left is final, with the damage seen so far."""
        self.group(math.inf)
        self.flush(math.inf)

    def at(self, sid, t):
        """(damage_dealt, (4 x side_health, structure_health, sail_health)) as of
        server time t: the last reading at or before it."""
        hist = self.score.get(sid) or [(t, 0.0, ())]
        return next((h[1:] for h in reversed(hist) if h[0] <= t), hist[0][1:])

    def fell(self, sid, since, end):
        """Any side, the structure or the sails lower at `end`. A stripped side
        passes hits on to the structure, chain shot hits only the sails, and a
        repair on one side must not hide a hit on another (all seen live)."""
        return any(b < a for a, b in zip(self.at(sid, since)[1], self.at(sid, end)[1]))

    def write(self, sid, side, run):
        start, end = run[0][0], run[-1][0] + DAMAGE_S
        since = max(start, self.claimed.get(sid, start))
        damage = round(max(0.0, self.at(sid, end)[0] - self.at(sid, since)[0]), 3)
        self.claimed[sid] = max(end, self.claimed.get(sid, end))
        hit = [o for o in self.score if o != sid and self.fell(o, since, end)]
        decks = {}
        for _, d in run:
            decks[str(d)] = decks.get(str(d), 0) + 1
        line = {"server_ts": round(start, 2), "id": sid, "side": side_name(side),
                "decks": dict(sorted(decks.items())), "guns": len(run), "damage": damage,
                "target": hit[0] if damage > 0 and len(hit) == 1 else None}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


class Watcher:
    """Turns a stream of read_battle() results into one JSON file per battle.

    The file is rewritten whenever the scoreboard changes, so a crash or Ctrl-C
    loses nothing. Every ship ever seen stays in the file: if a sunk ship drops
    out of the map, its last reading is kept and marked in_map=false.

    Beside it, <stamp>.motion.jsonl gets a line per ship per read: the latest
    position and heading, skipped when the server has sent nothing new;
    <stamp>.broadsides.jsonl a line per broadside (Broadsides); and
    <stamp>.wind.jsonl a line per new wind sample, the battle's and not a ship's.
    """

    def __init__(self, out_dir, log=print):
        self.out_dir, self.log = out_dir, log
        self.cur, self.torn = None, 0
        self.motion_ts = {}                      # ship id -> last server_ts written
        self.wind_ts = None                      # the last wind server_ts written
        self.batteries = {}                      # ship id -> guns, this battle; read_battle's `known`
        self.gunnery = None

    def step(self, state, snap, now):
        if state == OK:
            self.torn = 0
            ids = {s["id"] for s in snap["ships"]}
            if self.cur and (snap["battle_scene"] != self.cur["battle_scene"]
                             or not ids & set(self.cur["ships"])):
                self.finish(now)
            if self.cur is None:
                self.start(snap, now)
            self.merge(snap, now)
            return
        if state == TORN:
            self.torn += 1
            if self.torn < TORN_LIMIT:
                return
        self.finish(now)                         # NONE, GONE, or torn too long

    def start(self, snap, now):
        stamp = now.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
        self.cur = {"file": os.path.join(self.out_dir, f"{stamp}.json"),
                    "battle_scene": snap["battle_scene"], "started": now,
                    "ended": None, "reads": 0, "location": None, "ships": {}}
        self.motion_ts, self.batteries, self.wind_ts = {}, {}, None
        self.gunnery = Broadsides(self.cur["file"][:-len(".json")] + ".broadsides.jsonl")
        self.log(f"battle started  -> {self.cur['file']}")

    def merge(self, snap, now):
        cur = self.cur
        cur["reads"] += 1
        cur["last_read"] = now
        cur["my_ship_id"] = snap["my_ship_id"]
        cur["location"] = cur["location"] or snap.get("location")
        self.write_motion(snap.get("motion", ()))
        self.write_wind(snap.get("wind"))
        new ={k: v for k, v in snap.get("batteries", {}).items() if k not in self.batteries}
        self.batteries.update(new)
        changed = bool(new)
        now_ts = max((m["server_ts"] for m in snap.get("motion", ())), default=None)
        if now_ts is not None:                   # else no server clock: a start persists,
            self.gunnery.step(snap, now_ts)      # so the next tick still sees the firing
        seen = set()
        for s in snap["ships"]:
            seen.add(s["id"])
            old = cur["ships"].get(s["id"])
            s = dict(s, in_map=True, local=s["id"] == snap["my_ship_id"])
            if old is None or any(old[f] != s[f] for f in SCORE_FIELDS) or not old["in_map"]:
                changed = True
            cur["ships"][s["id"]] = s
        for sid, old in cur["ships"].items():
            if sid not in seen and old["in_map"]:
                old["in_map"] = False
                changed = True
        if changed:
            self.write()
            self.log(scoreboard(cur))

    def finish(self, now):
        if self.cur is None:
            return
        self.gunnery.finish()
        self.cur["ended"] = now
        self.write()
        self.log(f"battle over     -> {self.cur['file']}  ({self.cur['reads']} reads)")
        self.cur, self.torn = None, 0

    def write(self):
        cur = self.cur
        out = {k: v for k, v in cur.items() if k not in ("file", "ships")}
        out["ships"] = sorted(({**s, "battery": battery_rows(self.batteries[s["id"]])}
                               if s["id"] in self.batteries else s for s in cur["ships"].values()),
                              key=lambda s: (s["faction"], s["id"]))
        os.makedirs(self.out_dir, exist_ok=True)
        tmp = cur["file"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)
        os.replace(tmp, cur["file"])

    def write_motion(self, motion):
        new = [m for m in motion if self.motion_ts.get(m["id"]) != m["server_ts"]]
        if not new:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        with open(self.cur["file"][:-len(".json")] + ".motion.jsonl", "a", encoding="utf-8") as f:
            for m in new:
                self.motion_ts[m["id"]] = m["server_ts"]
                f.write(json.dumps(m) + "\n")

    def write_wind(self, wind):
        if not wind or wind["server_ts"] == self.wind_ts:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        with open(self.cur["file"][:-len(".json")] + ".wind.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(wind) + "\n")
        self.wind_ts = wind["server_ts"]


# --- the local ship's track ------------------------------------------------------

def latest(rd, va, size):
    """An interpolated_value's current value -- its last sample -- or None if the
    vector is empty or was caught mid-reallocation."""
    b = rd.read(va, 24)
    if not b:
        return None
    beg, end, cap = struct.unpack("<QQQ", b)
    if not (beg <= end <= cap) or (end - beg) % size or not 0 < (end - beg) // size <= 1024:
        return None
    return rd.read(end - size, size)


def world_to_latlon(x, y):
    """Transposed spherical Mercator: x tracks latitude, y tracks longitude."""
    lat = 2 * math.atan(math.exp((x - GEO_X0) / GEO_K)) - math.pi / 2
    return math.degrees(lat), math.degrees((GEO_Y0 - y) / GEO_K)


def heading(dx, dy):
    """Compass degrees from a direction vector. +x is north and -y is east, on the
    overworld as in the port fit, and in battle as checked against the compass."""
    return round(math.degrees(math.atan2(-dy, dx)) % 360, 1)


def read_motion(rd, va):
    """A battle_ship's latest position and heading, or None if a buffer is empty
    or torn. Each buffer holds only ~0.3 s (GAMEFILES.md 7.11), so the latest
    sample is all a read can usefully take. z is swell, and is dropped."""
    pos, head = latest(rd, va + BSHIP_POS, 12), latest(rd, va + BSHIP_DIR, 8)
    ts = latest(rd, va + BSHIP_POS + IV_TIMES, 8)
    if not (pos and head and ts):
        return None
    x, y, _ = struct.unpack("<fff", pos)
    dx, dy = struct.unpack("<ff", head)
    t = struct.unpack("<d", ts)[0]
    if not all(math.isfinite(v) for v in (x, y, dx, dy, t)):
        return None
    return {"server_ts": round(t, 2), "x": round(x, 1), "y": round(y, 1),
            "heading": heading(dx, dy)}


def read_wind(rd, bs):
    """The battle's latest wind, or None if a buffer is empty or torn -- which does
    not make the read torn. wind_i points downwind and its length is the speed
    (GAMEFILES.md 7.11, read live 2026-09-14), so dir is turned round to the quarter
    the wind blows from, as a sailor names it."""
    w, ts = latest(rd, bs + BS_WIND, 8), latest(rd, bs + BS_WIND + IV_TIMES, 8)
    if not (w and ts):
        return None
    dx, dy = struct.unpack("<ff", w)
    t = struct.unpack("<d", ts)[0]
    if not all(math.isfinite(v) for v in (dx, dy, t)):
        return None
    return {"server_ts": round(t, 2), "dir": round((heading(dx, dy) + 180) % 360, 1),
            "speed": round(math.hypot(dx, dy), 2)}


def read_str(rd, va):
    """A libstdc++ std::string: inline when it points at its own buffer (+16)."""
    b = rd.read(va, 32)
    if not b:
        return None
    ptr, ln = struct.unpack_from("<QQ", b)
    if ln > 256:
        return None
    raw = b[16:16 + ln] if ptr == va + 16 else rd.read(ptr, ln) if ln else b""
    return raw.decode("utf-8", "replace") if raw is not None else None


def read_battery(rd, va):
    """A battle_ship's guns, [{"model", "side", "deck", "pd"}], or None if torn.
    Indexes run parallel to cannons_dynamic; model_name is cannon_fire's key."""
    b = rd.read(va + BSHIP_GUNS, 24)
    if not b:
        return None
    beg, end, cap = struct.unpack("<QQQ", b)
    if not (beg <= end <= cap) or (end - beg) % GUN_SIZE or (end - beg) // GUN_SIZE > 1024:
        return None
    n = (end - beg) // GUN_SIZE
    raw = rd.read(beg, n * GUN_SIZE) if n else b""
    if raw is None:
        return None
    guns = []
    for i in range(n):
        deck, side, pd = struct.unpack_from("<iii", raw, i * GUN_SIZE + GUN_DECK)
        model = read_str(rd, beg + i * GUN_SIZE + GUN_MODEL)
        if not model or not (0 <= side < 8 and 0 <= deck < 16):
            return None
        guns.append({"model": model, "side": side, "deck": deck, "pd": pd})
    return guns


def read_fires(rd, va):
    """cannon_fire as {gun model: server time of its last firing}, or None if torn.
    A key's start changes only when that gun fires -- not when the ammunition
    changes, which moves reloaded_frac (GAMEFILES.md 7.11)."""
    b = rd.read(va + BSHIP_FIRE + 40, 8)
    cnt = u64(b) if b else -1
    if not 0 <= cnt <= 4096:
        return None
    nodes = D.walk_tree(rd, va + BSHIP_FIRE, cnt) if cnt else []
    if len(nodes) != cnt:
        return None
    out = {}
    for nd in nodes:
        key, t = read_str(rd, nd + FIRE_KEY), rd.read(nd + FIRE_START, 8)
        if not key or not t:
            return None
        start = struct.unpack("<d", t)[0]
        if not math.isfinite(start):
            return None
        out[key] = start
    return out


def battle_location(rd, bs):
    """Where the battle is on the chart. server_battle_pos is in overworld
    coordinates, so the track's lat/long fit applies."""
    b = rd.read(bs + BS_BATTLE_POS, 8)
    if not b:
        return None
    x, y = struct.unpack("<ff", b)
    if not (abs(x) < 1e7 and abs(y) < 1e7):     # also rejects NaN
        return None
    lat, lon = world_to_latlon(x, y)
    return {"lat": round(lat, 5), "lon": round(lon, 5)}


def read_track(rd, roots, vptr):
    """Where the local ship is, or None if it could not be read cleanly.

    In battle the overworld record is frozen at the moment the battle began, so a
    battle is logged as a state with no position."""
    sr = next((r for r in roots if (rd.read(r, 8) or b"") == struct.pack("<Q", vptr)), None)
    if sr is None:
        return None
    b = rd.read(sr + SR_BS, 8)
    if not b:
        return None
    if u64(b):
        return {"state": "battle"}
    port = rd.read(sr + SR_PORT, PORT_ENGAGED + 1)
    if not port:
        return None
    if port[PORT_ENGAGED]:
        return {"state": "port", "port": u64(port[PORT_ID:PORT_ID + 8])}
    ship = sr + SR_PLAYER
    pos, head = latest(rd, ship + SHIP_POS, 8), latest(rd, ship + SHIP_DIR, 8)
    ts = latest(rd, ship + SHIP_POS + IV_TIMES, 8)
    if not (pos and head and ts):
        return None                              # loading, or a torn read
    x, y = struct.unpack("<ff", pos)
    dx, dy = struct.unpack("<ff", head)
    if not all(math.isfinite(v) for v in (x, y, dx, dy)):
        return None
    lat, lon = world_to_latlon(x, y)
    return {"state": "sea", "lat": round(lat, 5), "lon": round(lon, 5),
            "heading": heading(dx, dy),
            "x": round(x, 1), "y": round(y, 1),
            "server_ts": round(struct.unpack("<d", ts)[0], 2)}


def track_key(fix):
    return (fix["state"], fix.get("port"), fix.get("x"), fix.get("y"))


class Tracker:
    """Appends the local ship's track to one JSON-lines file per voyage, port to port
    (docs/plans/voyages.md). A line is written only when the state or position
    changes, so time at anchor or in battle costs one line.

    A voyage file is named from its first line's time. That line is the port the
    ship left, stamped with the last tick it was seen there; the last line is the
    port it reached. Until then the voyage is open. Time in port between voyages
    writes nothing. A logger started at sea opens a partial voyage, with no port
    line. A voyage left open -- a logout at sea, a closed client, a killed logger
    -- is resumed by the next run, which appends to it."""

    def __init__(self, out_dir):
        self.out_dir, self.last, self.path, self.port = out_dir, None, None, None
        self.resume()

    def resume(self):
        """Take up the newest voyage if it is still open. A file it cannot read or
        make sense of is left alone, and the next departure starts a new voyage:
        this runs before the watch loop, so it must not raise."""
        try:
            names = sorted(n for n in os.listdir(self.out_dir) if upload.is_voyage(n))
            if not names:
                return
            path = os.path.join(self.out_dir, names[-1])
            with open(path, "rb+") as f:
                data = f.read()
                end = data.rfind(b"\n") + 1
                if end < len(data):
                    f.truncate(end)              # a line torn by a killed logger
            last = json.loads(data[:end].splitlines()[-1])
            if isinstance(last, dict) and last.get("state") not in ("port", None):
                self.path, self.last = path, track_key(last)
        except (OSError, IndexError, ValueError, KeyError):
            return

    def write(self, line):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def step(self, fix, now):
        """True when the ship reaches port and closes a voyage."""
        if fix is None:
            return False
        key, line = track_key(fix), {"t": now, **fix}
        if fix["state"] == "port":
            arrived = self.path is not None
            if arrived:
                self.write(line)
                self.path = None
            self.port, self.last = line, key     # the latest tick here: the departure time
            return arrived
        if key == self.last:
            return False
        if self.path is None:
            os.makedirs(self.out_dir, exist_ok=True)
            self.path = os.path.join(self.out_dir,
                                     upload.voyage_name((self.port or line)["t"]))
            if self.port:
                self.write(self.port)
            self.port = None
        self.write(line)
        self.last = key
        return False


def scoreboard(battle):
    """The Tab screen's columns, so a reading can be checked against it by eye."""
    ships = battle["ships"].values() if isinstance(battle["ships"], dict) else battle["ships"]
    rows = [f"  {'':2}{'Name':<22}{'Ship':>5}{'Fac':>5}{'Kills':>6}{'Assists':>8}"
            f"{'Damage':>9}  Status"]
    for s in sorted(ships, key=lambda s: (s["faction"], s["id"])):
        mark = "* " if s.get("local") else "  "
        status = "Sunk" if s["is_dead"] else "Alive"
        if s.get("in_map") is False:
            status += " (left map)"
        rows.append(f"  {mark}{(s['name'] or '<unnamed>')[:21]:<22}{s['ship_id']:>5}"
                    f"{s['faction']:>5}{s['kills']:>6}{s['assists']:>8}"
                    f"{s['damage_dealt']:>9.1f}  {status}")
    return "\n".join(rows)


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RotatingLog:
    """Where print goes under pythonw, which has no console: the log file, begun
    afresh once it passes `limit`, the old one kept as .1. The logger runs from
    logon to logoff, which can be weeks, and the upload thread prints too."""

    def __init__(self, path, limit=LOG_MAX):
        self.path, self.limit, self.lock = path, limit, threading.Lock()
        self.f = open(path, "a", encoding="utf-8", errors="replace")
        self.line_start = True

    def write(self, s):
        with self.lock:
            # Only between lines: print() writes the text and its newline separately.
            if self.line_start and self.f.tell() > self.limit:
                self.f.close()
                os.replace(self.path, self.path + ".1")
                self.f = open(self.path, "a", encoding="utf-8", errors="replace")
            n = self.f.write(s)
            self.f.flush()
            self.line_start = s.endswith("\n")
            return n

    def flush(self):
        with self.lock:
            self.f.flush()


def attach(log=print, sleep=time.sleep, on_state=None):
    """Wait for the client and its scene_root, then (reader, roots, vptr). The
    root appears once the world has loaded, so a scan finding none -- the client
    starting up, or in its menus -- is tried again SCAN_S later.

    on_state, when given, is told "waiting" (no client) or "loading" (a client, no
    scene_root) for the health ping. A client that stays "loading" for hours is how
    a stale VTABLE_RVA looks from the site."""
    said = None
    while True:
        pid, base = find_process()
        if pid is None or base is None:
            if on_state:
                on_state("waiting")
            if said != "client":
                log(f"{utcnow()} waiting for {EXE}")
                said = "client"
            sleep(WAIT_S)
            continue
        vptr = base + VPTR_RVA
        try:
            rd = ProcessReader(pid)
        except OSError:
            sleep(WAIT_S)                        # it exited between the two calls
            continue
        t0 = time.time()
        roots, scanned = find_roots(rd, vptr)
        if roots:
            log(f"{utcnow()} pid {pid}  {D.DLL_NAME} @ 0x{base:x}  scanned "
                f"{scanned / 2**30:.2f} GB in {time.time() - t0:.1f} s: {len(roots)} "
                "scene_root candidate(s) " + " ".join(f"0x{r:x}" for r in roots))
            return rd, roots, vptr
        rd.close()
        if on_state:
            on_state("loading")
        if said != pid:
            log(f"{utcnow()} {EXE} running (pid {pid}) but no scene_root yet: waiting for "
                "the world to load. If it never comes, VTABLE_RVA is stale -- the client "
                "updated, and a new logger release is needed "
                "(github.com/jimbursch1/navalgaming-logger)")
            said = pid
        sleep(SCAN_S)


def watch(rd, roots, vptr, w, track, up, sleep=time.sleep):
    """Log until scene_root goes: the client closed, or left the world."""
    while True:
        now = utcnow()
        state, snap = read_battle(rd, roots, vptr, w.batteries)
        w.step(state, snap, now)
        if track.step(read_track(rd, roots, vptr), now) and up:
            up.nudge()                           # in port: upload the voyage now
        if state == GONE:
            return
        sleep(BATTLE_TICK_S if state in (OK, TORN) else TICK_S)


def main():
    if sys.stdout is None:                       # pythonw, as the scheduled task runs it
        sys.stdout = sys.stderr = RotatingLog(LOG_PATH)
    for stream in (sys.stdout, sys.stderr):      # captains' names are arbitrary UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--once" in sys.argv[1:]:
        return once()

    w, track = Watcher(OUT_DIR), Tracker(TRACK_DIR)
    up = upload.start(ROOT)                      # its own thread; None when not configured
    print(f"{utcnow()} navalgaming-logger {upload.VERSION}: every {TICK_S:.0f} s, "
          f"{BATTLE_TICK_S:.0f} s in battle; track -> {TRACK_DIR}")
    try:
        while True:
            try:
                rd, roots, vptr = attach(on_state=up.set_state if up else None)
                if up:
                    up.set_state("attached")
                try:
                    watch(rd, roots, vptr, w, track, up)
                finally:
                    rd.close()
                    if up:
                        up.set_state("waiting")
                print(f"{utcnow()} scene_root gone (the client closed, or left the world)")
            except Exception:                    # never let one bad session end the logger
                print(f"{utcnow()} {traceback.format_exc()}")
                time.sleep(WAIT_S)
    except KeyboardInterrupt:
        w.finish(utcnow())
    finally:
        if up:
            up.stop()                            # one last upload, then exit


def once():
    """--once: the current battle and position, printed, for checking by eye."""
    pid, base = find_process()
    if pid is None:
        raise SystemExit(f"{EXE} is not running")
    if base is None:
        raise SystemExit(f"{D.DLL_NAME} not loaded in pid {pid}")
    vptr = base + VPTR_RVA
    print(f"pid {pid}  {D.DLL_NAME} @ 0x{base:x}  scene_root vptr 0x{vptr:x}")

    rd = ProcessReader(pid)
    t0 = time.time()
    roots, scanned = find_roots(rd, vptr)
    print(f"scanned {scanned / 2**30:.2f} GB in {time.time() - t0:.1f} s: "
          f"{len(roots)} scene_root candidate(s) "
          + " ".join(f"0x{r:x}" for r in roots))
    if not roots:
        raise SystemExit("scene_root not found -- VTABLE_RVA is stale if the client "
                         "updated, and a new logger release is needed "
                         "(github.com/jimbursch1/navalgaming-logger)")

    print("track:", json.dumps(read_track(rd, roots, vptr)))
    state, snap = read_battle(rd, roots, vptr)
    if state != OK:
        print({NONE: "no battle", TORN: "battle loaded, read did not validate "
               "(retry)", GONE: "scene_root gone"}[state])
        return
    for s in snap["ships"]:
        s["local"] = s["id"] == snap["my_ship_id"]
    print(f"battle_scene 0x{snap['battle_scene']:x}  my_ship_id {snap['my_ship_id']}  "
          f"location {json.dumps(snap['location'])}")
    bs = snap["battle_scene"]
    w, wt = latest(rd, bs + BS_WIND, 8), latest(rd, bs + BS_WIND + IV_TIMES, 8)
    if w and wt:
        dx, dy = struct.unpack("<ff", w)
        print(f"wind: raw ({dx:.4f}, {dy:.4f})  magnitude {math.hypot(dx, dy):.4f}  "
              f"heading {heading(dx, dy)}  server_ts {struct.unpack('<d', wt)[0]:.2f}")
    else:
        print("wind: empty or torn")
    print(scoreboard(snap))
    for m in snap["motion"]:
        print("motion:", json.dumps(m))
    for s in snap["ships"]:
        guns, fires = snap["batteries"].get(s["id"]), snap["fires"].get(s["id"])
        print(f"guns {s['id']}: {len(fires) if fires is not None else 'torn'} fired;",
              json.dumps(battery_rows(guns)) if guns else "no battery read")


if __name__ == "__main__":
    main()
