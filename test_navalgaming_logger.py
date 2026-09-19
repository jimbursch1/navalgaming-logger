"""Synthetic test for navalgaming_logger.py: plant a scene_root -> battle_scene -> opaque
chain in fake memory over test_dumpbattle's real three-node tree, and drive the
Watcher through a battle without a game.

Usage:  py test_navalgaming_logger.py      (exit 0 = pass)
"""
import json
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "gamefiles")]
import navalgaming_logger as B
import dumpbattle as D
import test_dumpbattle as T

VPTR = 0x391810000 + B.VTABLE_RVA_FALLBACK + B.VPTR_SKIP
ROOT_OFF, BS_OFF = 0x20000, 0x30000
LOCAL = 8906
FAILS = []


class FakeProcess(T.FakeReader):
    def regions(self):
        yield self.base, len(self.buf)


def build():
    buf = T.build()
    struct.pack_into("<Q", buf, ROOT_OFF, VPTR)
    struct.pack_into("<Q", buf, ROOT_OFF + B.SR_BS, T.BASE_VA + BS_OFF)
    struct.pack_into("<Q", buf, BS_OFF + B.BS_MY_SHIP, LOCAL)
    struct.pack_into("<Q", buf, BS_OFF + B.BS_OPAQUE, T.BASE_VA + T.OPAQUE_OFF)
    buf[0x2F003:0x2F00B] = struct.pack("<Q", VPTR)     # misaligned: not an instance
    # At sea off Messina, exactly as read live on 2026-09-10: 16 samples, the
    # current one last.
    buf[ROOT_OFF + B.SR_PORT + B.PORT_ENGAGED] = 0
    ship = ROOT_OFF + B.SR_PLAYER
    put_vec(buf, ship + B.SHIP_POS, 0x38000, [(0.0, 0.0)] * 15 + [MESSINA_UNDOCK], "<ff")
    put_vec(buf, ship + B.SHIP_POS + B.IV_TIMES, 0x38100, [(1605.0 + i,) for i in range(16)], "<d")
    put_vec(buf, ship + B.SHIP_DIR, 0x38200, [(1.0, 0.0)] * 15 + [MESSINA_DIR], "<ff")
    # In battle, as read live on 2026-09-10 heading due west: 16 samples 0.02 s
    # apart. Ship 20001 (index 2) keeps empty buffers.
    struct.pack_into("<ff", buf, BS_OFF + B.BS_BATTLE_POS, *BATTLE_POS)
    for i, (pos, head) in enumerate(((LOCAL_POS, LOCAL_DIR), (AI_POS, AI_DIR))):
        plant_motion(buf, i, 0x39000 + i * 0x800, pos, head)
    put_vec(buf, BS_OFF + B.BS_WIND, 0x3E000, [(0.0, 0.0)] * 15 + [WIND], "<ff")
    put_vec(buf, BS_OFF + B.BS_WIND + B.IV_TIMES, 0x3E100,
            [(WIND_T - 0.2 * (15 - k),) for k in range(16)], "<d")
    return buf


MESSINA_UNDOCK = (-83144.6640625, -173242.875)
MESSINA_DIR = (0.83843994140625, 0.5449939966201782)
BATTLE_POS = (-82852.78, -172757.72)
LOCAL_POS, LOCAL_DIR = (727.77, 998.33, -0.60), (-0.0269, 0.9996)     # 8906, heading 268.5
AI_POS, AI_DIR = (752.37, 764.96, -0.77), (-0.0536, 0.9986)           # 15506
T0 = 13099.64
# The wind as read live on 2026-09-14, the HUD giving it from WNW: heading() 119.4.
WIND, WIND_T = (-9.8098, -17.4290), 939.03


def plant_motion(buf, i, data, pos, head, t0=T0):
    """Fill ship i's position/direction interpolated_values; the current sample last."""
    rec = T.NODES_OFF + i * T.NODE_STRIDE + D.NODE_VALUE
    put_vec(buf, rec + B.BSHIP_POS, data, [(0.0, 0.0, 0.0)] * 15 + [pos], "<fff")
    put_vec(buf, rec + B.BSHIP_POS + B.IV_TIMES, data + 0x200,
            [(t0 + 0.02 * k,) for k in range(16)], "<d")
    put_vec(buf, rec + B.BSHIP_DIR, data + 0x400, [(1.0, 0.0)] * 15 + [head], "<ff")


def put_vec(buf, at, data, items, fmt):
    """A std::vector at buf offset `at` whose elements live at offset `data`."""
    size = struct.calcsize(fmt)
    for i, it in enumerate(items):
        struct.pack_into(fmt, buf, data + i * size, *it)
    beg = T.BASE_VA + data
    struct.pack_into("<QQQ", buf, at, beg, beg + len(items) * size, beg + len(items) * size)


def put_str(buf, at, s, heap):
    """A std::string at `at`: inline up to 15 bytes, as libstdc++ does, else at `heap`."""
    nb = s.encode()
    where = at + 16 if len(nb) <= 15 else heap
    buf[where:where + len(nb)] = nb
    struct.pack_into("<QQ", buf, at, T.BASE_VA + where, len(nb))


# The Victory's model names, as read live: a deck's first gun has no suffix.
GUNS = [("lcannon_deck0", 0, 0, 12), ("lcannon_deck3.005", 0, 3, 42),
        ("rcannon_deck1.011", 1, 1, 12)]


def plant_guns(buf, rec, guns, data=0x3A000, heap=0x3B000):
    for i, (model, side, deck, pd) in enumerate(guns):
        struct.pack_into("<fiii", buf, data + i * B.GUN_SIZE, 54.5, deck, side, pd)
        put_str(buf, data + i * B.GUN_SIZE + B.GUN_MODEL, model, heap + i * 32)
    beg = T.BASE_VA + data
    end = beg + len(guns) * B.GUN_SIZE
    struct.pack_into("<QQQ", buf, rec + B.BSHIP_GUNS, beg, end, end)


def plant_fires(buf, rec, fires, nodes=0x3C000, heap=0x3D000):
    """cannon_fire as a real three-node red-black tree: node 1 the root."""
    va = [T.BASE_VA + nodes + i * 0x100 for i in range(3)]
    for i, (model, start) in enumerate(fires):
        o = nodes + i * 0x100
        put_str(buf, o + B.FIRE_KEY, model, heap + i * 32)
        struct.pack_into("<dd", buf, o + B.FIRE_START, start, start + 54.5)
        parent, left, right = (va[1], 0, 0) if i != 1 else (0, va[0], va[2])
        struct.pack_into("<QQQ", buf, o + 8, parent, left, right)
    struct.pack_into("<QQQQ", buf, rec + B.BSHIP_FIRE + 16, va[1], va[0], va[2], len(fires))


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILS.append(msg)


def reading(buf):
    return B.read_battle(FakeProcess(buf), [T.BASE_VA + ROOT_OFF], VPTR)


def fake_dll(symbols, sig=b"PE\0\0", pe_off=0x80, nsec=2, size_of_image=0x2000000,
             sec_vaddr=(0x1000, 0x1000000), strip=False):
    """A minimal PE with a COFF symbol table, for vtable_rva().

    `symbols` is a list of (name, section_number, value, naux). A name over 8 bytes goes in
    the string table and its record holds the offset, which is the only case that matters
    here -- _ZTV10scene_root is 16 characters. Auxiliary records are emitted as filler after
    their symbol, because skipping them wrongly is the classic way to misread this table.
    """
    strtab = bytearray(b"\0\0\0\0")              # the size field, filled in at the end
    recs = bytearray()
    for name, secno, value, naux in symbols:
        nb = name.encode()
        rec = bytearray(18)
        if len(nb) > 8:
            off = len(strtab)
            strtab += nb + b"\0"
            struct.pack_into("<II", rec, 0, 0, off)
        else:
            rec[0:len(nb)] = nb
        struct.pack_into("<IhHBB", rec, 8, value, secno, 0, 2, naux)
        recs += rec
        recs += bytes(18 * naux)                 # the auxiliary records themselves
    struct.pack_into("<I", strtab, 0, len(strtab))

    optsz = 0x70
    sect_at = pe_off + 4 + 20 + optsz
    symtab_at = sect_at + nsec * 40
    nsyms = 0 if strip else len(recs) // 18
    buf = bytearray(symtab_at)
    struct.pack_into("<I", buf, 0x3C, pe_off)
    buf[pe_off:pe_off + 4] = sig
    # COFF: Machine, NumberOfSections, TimeDateStamp, PointerToSymbolTable,
    # NumberOfSymbols, SizeOfOptionalHeader.
    struct.pack_into("<HHIIIH", buf, pe_off + 4, 0x8664, nsec, 1789062088,
                     0 if strip else symtab_at, nsyms, optsz)
    struct.pack_into("<H", buf, pe_off + 4 + 20, 0x20B)          # PE32+
    struct.pack_into("<I", buf, pe_off + 4 + 20 + 56, size_of_image)
    for i in range(nsec):
        struct.pack_into("<I", buf, sect_at + i * 40 + 12, sec_vaddr[i])
    return bytes(buf + recs + strtab)


def test_vtable_rva():
    """The scene_root anchor, read out of the DLL instead of pinned. Never raises: a
    failure means the caller uses VTABLE_RVA_FALLBACK, which is the old behaviour."""
    d = tempfile.mkdtemp()

    def write(name, blob):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(blob)
        return p

    # Section 2 is at 0x1000000, so value 0x32D200 lands on the real 2026-09-10 RVA.
    good = [("__imp_foo", 1, 0x10, 0), (B.SCENE_ROOT_SYMBOL, 2, 0x32D200, 0)]
    check(B.vtable_rva(write("a.dll", fake_dll(good))) == 0x132D200,
          "vtable_rva: section VirtualAddress + symbol value")

    # An auxiliary record before the symbol: a plain 18-byte stride would read it as a
    # record and miss what follows.
    aux = [("withauxx_long_name", 1, 0x10, 2), (B.SCENE_ROOT_SYMBOL, 2, 0x32D200, 0)]
    check(B.vtable_rva(write("b.dll", fake_dll(aux))) == 0x132D200,
          "vtable_rva: auxiliary records are skipped, not read as symbols")

    # A longer name ending in ours must not be mistaken for it.
    decoy = [("_Z_prefix" + B.SCENE_ROOT_SYMBOL, 1, 0x10, 0)]
    check(B.vtable_rva(write("c.dll", fake_dll(decoy))) is None,
          "vtable_rva: a name merely ending with the symbol is not a match")
    check(B.vtable_rva(write("d.dll", fake_dll(decoy + good))) == 0x132D200,
          "vtable_rva: ...and the real one is still found alongside it")

    # An RVA outside SizeOfImage is not believable; better the fallback than a bad scan.
    check(B.vtable_rva(write("e.dll", fake_dll(good, size_of_image=0x1000))) is None,
          "vtable_rva: an RVA past SizeOfImage is refused")

    for name, blob in (("stripped", fake_dll(good, strip=True)),
                       ("not a PE", fake_dll(good, sig=b"XX\0\0")),
                       ("symbol absent", fake_dll([("__imp_foo", 1, 0x10, 0)])),
                       ("truncated", fake_dll(good)[:0x40]),
                       ("empty", b"")):
        check(B.vtable_rva(write("f.dll", blob)) is None, f"vtable_rva: None for {name}")
    check(B.vtable_rva(os.path.join(d, "absent.dll")) is None, "vtable_rva: None for no file")
    check(B.vtable_rva(d) is None, "vtable_rva: None for a directory")


def main():
    buf = build()
    test_vtable_rva()

    roots, scanned = B.find_roots(FakeProcess(buf), VPTR)
    check(roots == [T.BASE_VA + ROOT_OFF], f"find_roots -> exactly the aligned instance {roots}")
    check(scanned == len(buf), "find_roots scanned every byte of the region")

    state, snap = reading(buf)
    check(state == B.OK, f"read_battle on a live battle -> {state}")
    ships = {s["id"]: s for s in (snap or {}).get("ships", [])}
    check(set(ships) == {sp["sid"] for sp in T.SHIPS}, "all three ships read")
    check(snap and snap["my_ship_id"] == LOCAL, "my_ship_id read from battle_scene+40")
    check(ships.get(20001, {}).get("kills") == 2 and ships[20001]["is_dead"],
          "scoreboard fields come through parse_ship")
    check(all("va" not in s for s in ships.values()), "no raw addresses in a snapshot")

    motion = {m["id"]: m for m in (snap or {}).get("motion", [])}
    check(set(motion) == {LOCAL, 15506}, f"motion for the two ships with samples, none for "
          f"the empty buffers {sorted(motion)}")
    check(motion.get(LOCAL) == {"id": LOCAL, "server_ts": 13099.94, "x": 727.8, "y": 998.3,
                                "heading": 268.5},
          f"the last sample, heading 268.5 as read live, z dropped {motion.get(LOCAL)}")
    loc = (snap or {}).get("location") or {}
    check(38.2 < loc.get("lat", 0) < 38.4 and 15.4 < loc.get("lon", 0) < 15.7,
          f"battle located off Messina {loc}")
    wind = (snap or {}).get("wind")
    check(wind == {"server_ts": WIND_T, "dir": 299.4, "speed": 20.0},
          f"the wind's last sample, turned round to the quarter it blows from {wind}")

    b = bytearray(buf)
    rec = T.NODES_OFF + 1 * T.NODE_STRIDE + D.NODE_VALUE
    beg = T.BASE_VA + 0x39800
    struct.pack_into("<QQQ", b, rec + B.BSHIP_DIR, beg + 64, beg, beg + 128)
    state, torn = reading(b)
    check(state == B.OK and [m["id"] for m in torn["motion"]] == [LOCAL],
          "a torn direction vector drops that ship's motion, not the scoreboard")

    b = bytearray(buf)
    beg = T.BASE_VA + 0x3E000
    struct.pack_into("<QQQ", b, BS_OFF + B.BS_WIND, beg + 64, beg, beg + 128)
    state, torn = reading(b)
    check(state == B.OK and torn["wind"] is None and len(torn["motion"]) == 2,
          "a torn wind buffer drops the wind, not the read or its motion")

    b = bytearray(buf)
    struct.pack_into("<Q", b, ROOT_OFF + B.SR_BS, 0)
    check(reading(b)[0] == B.NONE, "bs == 0 -> NONE (in port)")

    b = bytearray(buf)
    struct.pack_into("<i", b, T.NODES_OFF + D.NODE_KEY, 999999)
    check(reading(b)[0] == B.TORN, "key/id mismatch -> TORN, not a bad snapshot")

    b = bytearray(buf)
    struct.pack_into("<Q", b, ROOT_OFF, 0)
    check(reading(b)[0] == B.GONE, "vptr gone -> GONE")

    # The Watcher, one battle end to end.
    with tempfile.TemporaryDirectory() as tmp:
        w = B.Watcher(tmp, log=lambda *a: None)
        load = lambda: json.load(open(w.cur["file"] if w.cur else last, encoding="utf-8"))

        w.step(B.OK, snap, "2026-09-10T12:00:00Z")
        last = w.cur["file"]
        out = load()
        check(os.path.basename(last) == "20260910-120000.json", f"file named by start {last}")
        check(len(out["ships"]) == 3 and out["ended"] is None, "written on the first read")
        check([s["id"] for s in out["ships"] if s["local"]] == [LOCAL], "local ship marked")
        check(out["location"] == snap["location"], "the battle's location is in its JSON")

        moves = lambda: [json.loads(l) for l in open(last[:-5] + ".motion.jsonl", encoding="utf-8")]
        check(sorted(m["id"] for m in moves()) == [LOCAL, 15506], "a motion line per ship")
        w.step(B.OK, snap, "2026-09-10T12:00:01Z")
        check(len(moves()) == 2, "nothing new from the server -> no new lines")
        winds = lambda: [json.loads(l) for l in open(last[:-5] + ".wind.jsonl", encoding="utf-8")]
        check(winds() == [snap["wind"]], "one wind line for two reads of the same sample")

        for _ in range(B.TORN_LIMIT - 1):
            w.step(B.TORN, None, "2026-09-10T12:00:03Z")
        check(w.cur is not None, f"{B.TORN_LIMIT - 1} torn reads do not end the battle")

        later = json.loads(json.dumps(snap))
        later["ships"] = [s for s in later["ships"] if s["id"] != 15506]
        later["motion"] = [dict(m, server_ts=m["server_ts"] + 1, x=m["x"] + 4)
                           for m in later["motion"] if m["id"] != 15506]
        later["wind"] = dict(snap["wind"], server_ts=WIND_T + 1, dir=301.0)
        for s in later["ships"]:
            if s["id"] == LOCAL:
                s["kills"] = 1
        w.step(B.OK, later, "2026-09-10T12:00:06Z")
        out = {s["id"]: s for s in load()["ships"]}
        check(out[LOCAL]["kills"] == 1, "a kill is written when it happens")
        check(15506 in out and out[15506]["in_map"] is False,
              "a ship that leaves the map is kept, marked in_map=false")
        mine = [m["server_ts"] for m in moves() if m["id"] == LOCAL]
        check(len(moves()) == 3 and mine == sorted(mine) and len(mine) == 2,
              "new samples appended in order; a ship off the map gets no more lines")
        check([x["dir"] for x in winds()] == [299.4, 301.0], "a new wind sample is appended")
        w.step(B.OK, dict(later, wind=None), "2026-09-10T12:00:07Z")
        check(len(winds()) == 2 and w.cur is not None, "a read with no wind writes none, and goes on")

        w.step(B.NONE, None, "2026-09-10T12:05:00Z")
        check(w.cur is None and load()["ended"] == "2026-09-10T12:05:00Z",
              "bs -> 0 ends the battle and stamps ended")

        w.step(B.OK, snap, "2026-09-10T12:10:00Z")
        other = dict(snap, battle_scene=snap["battle_scene"] + 0x5000)
        w.step(B.OK, other, "2026-09-10T12:20:00Z")
        files = sorted(os.listdir(tmp))
        check(len([f for f in files if f.endswith(".json")]) == 3
              and len([f for f in files if f.endswith(".motion.jsonl")]) == 3,
              f"a new battle_scene starts a new file, and a new motion file {files}")

        for _ in range(B.TORN_LIMIT):
            w.step(B.TORN, None, "2026-09-10T12:21:00Z")
        check(w.cur is None, f"{B.TORN_LIMIT} torn reads in a row end the battle")

    # The track: battle, sea, port, and a torn read.
    track = lambda b: B.read_track(FakeProcess(b), [T.BASE_VA + ROOT_OFF], VPTR)
    check(track(buf) == {"state": "battle"}, "in battle -> state only, no stale position")

    sea = bytearray(buf)
    struct.pack_into("<Q", sea, ROOT_OFF + B.SR_BS, 0)
    fix = track(sea) or {}
    check(fix.get("state") == "sea" and (fix["x"], fix["y"]) == (-83144.7, -173242.9),
          f"at sea -> the last sample, not the first {fix.get('x')}, {fix.get('y')}")
    check(abs(fix.get("lat", 0) - 38.2721) < 1e-3 and abs(fix.get("lon", 0) - 15.5513) < 1e-3,
          f"lat/long through the 11.5 fit {fix.get('lat')}, {fix.get('lon')}")
    check(fix.get("heading") == 327.0 and fix.get("server_ts") == 1620.0,
          f"heading 327 as read live; server_ts is the last timestamp {fix.get('heading')}")

    port = bytearray(sea)
    port[ROOT_OFF + B.SR_PORT + B.PORT_ENGAGED] = 1
    struct.pack_into("<Q", port, ROOT_OFF + B.SR_PORT + B.PORT_ID, 0)
    check(track(port) == {"state": "port", "port": 0}, "docked -> the port id (0 = Messina)")

    torn = bytearray(sea)
    beg = T.BASE_VA + 0x38000
    struct.pack_into("<QQQ", torn, ROOT_OFF + B.SR_PLAYER + B.SHIP_POS, beg + 64, beg, beg + 128)
    check(track(torn) is None, "a vector caught mid-reallocation -> None, nothing logged")

    test_voyages(fix)
    test_supervisor(fix)

    test_gunnery_reads(buf)
    test_broadsides()

    print("RESULT:", "FAIL" if FAILS else "PASS")
    return 1 if FAILS else 0


def test_voyages(fix):
    """The track is one file per voyage, port to port (docs/plans/voyages.md)."""
    ts = lambda t: f"2026-09-10T{t}Z"
    port0, port5 = {"state": "port", "port": 0}, {"state": "port", "port": 5}
    moved = dict(fix, x=fix["x"] + 50, lat=fix["lat"] + 0.001)
    read = lambda p: [json.loads(l) for l in open(p, encoding="utf-8")]

    with tempfile.TemporaryDirectory() as tmp:
        tr = B.Tracker(tmp)
        got = [tr.step(f, ts(t)) for f, t in (
            (port0, "11:59:00"), (port0, "11:59:57"), (fix, "12:00:00"), (fix, "12:00:03"),
            (None, "12:00:06"), (moved, "12:00:09"), ({"state": "battle"}, "12:00:12"),
            ({"state": "battle"}, "12:05:00"), (port5, "12:09:00"))]
        first = os.path.join(tmp, "20260910-115957.jsonl")
        check(os.listdir(tmp) == ["20260910-115957.jsonl"],
              f"a voyage file named from its departure, the last tick in port {os.listdir(tmp)}")
        lines = read(first)
        check([l["state"] for l in lines] == ["port", "sea", "sea", "battle", "port"],
              f"a line per change of state or position only {[l['t'][11:19] for l in lines]}")
        check(lines[0] == {"t": ts("11:59:57"), **port0}, "the first line is the port it left")
        check(lines[1]["t"] == ts("12:00:00") and "port" not in lines[1],
              "each line stamped with the poll time")
        check(lines[-1] == {"t": ts("12:09:00"), **port5} and got == [False] * 8 + [True],
              f"the arrival closes the voyage in the same file, and only it returns True {got}")

        tr.step(port5, ts("12:10:00"))
        tr.step(port0, ts("12:20:00"))                   # sunk and respawned: port to port
        check(len(os.listdir(tmp)) == 1, "time in port, and port to port with no sea, write nothing")

        tr = B.Tracker(tmp)
        check(tr.path is None, "a restart after a closed voyage opens nothing")
        tr.step(port0, ts("12:30:00"))
        tr.step(fix, ts("12:31:00"))
        second = os.path.join(tmp, "20260910-123000.jsonl")
        check(os.path.exists(second) and read(second)[0] == {"t": ts("12:30:00"), **port0},
              "the next departure opens the next voyage")

        with open(second, "a", encoding="utf-8") as f:
            f.write('{"t": "2026-09-10T12:3')             # killed mid-write
        tr = B.Tracker(tmp)
        got = [tr.step(f, ts(t)) for f, t in ((fix, "12:40:00"), (moved, "12:41:00"),
                                              (port5, "12:50:00"))]
        lines = read(second)
        check(len(os.listdir(tmp)) == 2 and [l["t"][11:19] for l in lines] ==
              ["12:30:00", "12:31:00", "12:41:00", "12:50:00"] and got == [False, False, True],
              "a restart mid-voyage trims the torn line and appends to the same voyage, "
              f"without repeating the position it stopped at {[l['t'][11:19] for l in lines]}")

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "20260910.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": ts("09:00:00"), **fix}) + "\n")
        tr = B.Tracker(tmp)
        check(tr.path is None, "an old day file is not a voyage to resume")
        tr.step({"state": "battle"}, ts("13:00:00"))
        tr.step(fix, ts("13:05:00"))
        arrived = tr.step(port0, ts("13:20:00"))
        lines = read(os.path.join(tmp, "20260910-130000.jsonl"))
        check([l["state"] for l in lines] == ["battle", "sea", "port"] and arrived,
              "started in battle: a partial voyage, from the first reading to the port")

        for junk in (b"[]\n", b'{"t": "x"}\n', b"\n", b""):
            with open(os.path.join(tmp, "20260910-140000.jsonl"), "wb") as f:
                f.write(junk)
            try:
                ok = B.Tracker(tmp).path is None
            except Exception as e:
                ok = f"{type(e).__name__}: {e}"
            check(ok is True, f"a newest file of {junk!r} is left alone, not a crash at start {ok}")


def test_supervisor(fix):
    """The logger runs from logon, so it waits for the client and outlives it."""
    log, naps, closed, states, games = [], [], [], [], []
    base = 0x7FF000000000
    DLL = r"C:\Games\steamapps\common\LettersOfMarquePlaytest\libgdexample.dll"
    # find_process returns (pid, base, the DLL's path); the path is only known once the
    # module is loaded, so the early looks have none.
    procs = iter([(None, None, None), (None, None, None), (4242, None, None),
                  (4242, base, DLL), (4242, base, DLL)])
    scans = iter([[], [0x1000]])

    class Reader:
        def __init__(self, pid):
            self.pid = pid

        def close(self):
            closed.append(self.pid)

    saved = B.find_process, B.ProcessReader, B.find_roots
    B.find_process = lambda: next(procs)
    B.ProcessReader = Reader
    B.find_roots = lambda rd, vptr: (next(scans), 1 << 30)
    try:
        rd, roots, vptr = B.attach(log.append, naps.append, states.append, games.append)
    finally:
        B.find_process, B.ProcessReader, B.find_roots = saved
    check(roots == [0x1000] and vptr == base + B.VTABLE_RVA_FALLBACK + B.VPTR_SKIP
          and rd.pid == 4242,
          "attach: returns once the client is up and scene_root is found")
    check(naps == [B.WAIT_S] * 3 + [B.SCAN_S],
          f"attach: looks for the client every {B.WAIT_S:.0f} s (its DLL not loaded yet counts "
          f"as not up), and scans again {B.SCAN_S:.0f} s after finding no scene_root {naps}")
    check(sum(f"waiting for {B.EXE}" in l for l in log) == 1 and sum("no scene_root yet" in l for l in log) == 1,
          f"attach: each wait said once, not every look {len(log)} lines")
    check(closed == [4242], "attach: a scan that found nothing closes its process handle")
    # The health ping's state (docs/plans/logger-health.md). "loading" is reported every
    # scan, not once, because how long it has held is what tells a client parked at the
    # menu from offsets gone stale after a game update.
    check(states == ["waiting"] * 3 + ["loading"],
          f"attach: reports waiting until the client is up, then loading until the world is {states}")
    # The game build (docs/plans/game-build-detection.md). The path is reported as soon as
    # the module is there -- before the scan -- because a client that never loads a world
    # is exactly when knowing its build matters: that is what stale offsets look like.
    check(games == [DLL, DLL],
          f"attach: the gameplay DLL's path is reported once the module is loaded {games}")

    port0, port5 = {"state": "port", "port": 0}, {"state": "port", "port": 5}
    ticks = iter([(B.NONE, port0), (B.OK, fix), (B.NONE, port5), (B.GONE, None)])
    now = {}

    class Up:
        nudges = 0

        def nudge(self):
            Up.nudges += 1

    saved = B.read_battle, B.read_track
    B.read_battle = lambda rd, roots, vptr, known: (now.update(t=next(ticks)) or now["t"][0], None)
    B.read_track = lambda rd, roots, vptr: now["t"][1]

    class W:                                     # the Watcher is tested above
        batteries, states = {}, []

        def step(self, state, snap, t):
            W.states.append(state)

    naps = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            B.watch(None, [0x1000], 1, W(), B.Tracker(tmp), Up(), naps.append)
        finally:
            B.read_battle, B.read_track = saved
    check(W.states == [B.NONE, B.OK, B.NONE, B.GONE] and Up.nudges == 1,
          f"watch: runs until scene_root goes, and nudges the upload on arrival {W.states}")
    check(naps == [B.TICK_S, B.BATTLE_TICK_S, B.TICK_S],
          f"watch: {B.TICK_S:.0f} s ticks, {B.BATTLE_TICK_S:.0f} s in battle {naps}")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "navalgaming-logger.log")
        out = B.RotatingLog(path, limit=100)
        for i in range(5):
            print(f"line {i} " + "x" * 40, file=out)
        out.f.close()
        old, cur = (open(p, encoding="utf-8").read() for p in (path + ".1", path))
        check(os.path.getsize(path + ".1") > 100 and cur.startswith("line 3") and "line 4" in cur
              and "line 0" in old, "the log starts afresh past its limit, keeping the last one as .1")


def test_gunnery_reads(buf):
    b = bytearray(buf)
    rec = T.NODES_OFF + D.NODE_VALUE                     # ship 0, the local 8906
    plant_guns(b, rec, GUNS)
    fired = [(g[0], 1261.83 + i) for i, g in enumerate(GUNS)]
    plant_fires(b, rec, fired)
    ai = T.NODES_OFF + T.NODE_STRIDE + D.NODE_VALUE      # ship 1: nothing fired yet
    hdr = T.BASE_VA + ai + B.BSHIP_FIRE + 8
    struct.pack_into("<QQQQ", b, ai + B.BSHIP_FIRE + 16, 0, hdr, hdr, 0)
    state, snap = reading(b)
    check(state == B.OK and snap["batteries"].get(LOCAL) ==
          [{"model": m, "side": s, "deck": d, "pd": p} for m, s, d, p in GUNS],
          "battery read: side, deck, pd, and model names inline and on the heap")
    check(snap["fires"].get(LOCAL) == dict(fired), "cannon_fire read: each gun's last start")
    check(snap["fires"].get(15506) == {}, "an empty cannon_fire map -> {}, not torn")
    _, again = B.read_battle(FakeProcess(b), [T.BASE_VA + ROOT_OFF], VPTR, {LOCAL})
    check(LOCAL not in again["batteries"] and again["fires"].get(LOCAL) == dict(fired),
          "a known battery is not re-read; its fires still are")

    torn = bytearray(b)
    struct.pack_into("<Q", torn, rec + B.BSHIP_FIRE + 40, 4)          # says 4, holds 3
    beg = T.BASE_VA + 0x3A000
    struct.pack_into("<QQQ", torn, rec + B.BSHIP_GUNS, beg + B.GUN_SIZE, beg, beg)
    state, t = reading(torn)
    check(state == B.OK and t["fires"][LOCAL] is None and LOCAL not in t["batteries"]
          and len(t["ships"]) == 3, "a torn map or vector -> nothing for that ship, "
          "the scoreboard unharmed")

    with tempfile.TemporaryDirectory() as tmp:
        w = B.Watcher(tmp, log=lambda *a: None)
        w.step(B.OK, snap, "2026-09-10T12:00:00Z")
        out = {s["id"]: s for s in json.load(open(w.cur["file"], encoding="utf-8"))["ships"]}
        check(out[LOCAL].get("battery") == [
            {"side": "port", "deck": 0, "pd": 12, "guns": 1},
            {"side": "port", "deck": 3, "pd": 42, "guns": 1},
            {"side": "starboard", "deck": 1, "pd": 12, "guns": 1}],
            f"the battle JSON carries each ship's battery {out[LOCAL].get('battery')}")
        check(set(w.batteries) == {LOCAL}, "the Watcher keeps batteries as `known`; an "
              "empty one is not kept, so it is read again next tick")


def test_broadsides():
    """A battle on the server clock, a tick a second, through Broadsides."""
    V, AI, X = LOCAL, 15506, 20001
    m = lambda p, d, i=0: f"{p}cannon_deck{d}" + (f".{i:03d}" if i else "")
    guns = lambda p, side, d, n, pd: [{"model": m(p, d, i), "side": side, "deck": d, "pd": pd}
                                      for i in range(n)]
    bat = {V: guns("l", 0, 0, 2, 12) + guns("l", 0, 3, 4, 42)
           + guns("r", 1, 1, 3, 12) + guns("r", 1, 2, 3, 24),
           AI: guns("l", 0, 0, 3, 6) + guns("r", 1, 0, 2, 6) + guns("b", 2, 0, 1, 6), X: []}
    fired = [
        # Two lead guns 1.8 s apart, then the ripple 4.5 s after the second.
        (V, m("l", 3), 98.2), (V, m("l", 0), 100.0),
        (V, m("l", 3, 1), 104.5), (V, m("l", 0, 1), 104.8), (V, m("l", 3, 2), 105.1),
        (V, m("l", 3, 3), 105.4),
        # One deck; then another 8.8 s later. No tick sees either until 131.
        (V, m("r", 1), 120.2), (V, m("r", 1, 1), 120.4), (V, m("r", 1, 2), 120.8),
        (V, m("r", 2), 129.6), (V, m("r", 2, 1), 129.9), (V, m("r", 2, 2), 130.2),
        # Port and starboard together.
        (AI, m("l", 0), 150.0), (AI, m("r", 0), 150.1), (AI, m("l", 0, 1), 150.3),
        (AI, m("r", 0, 1), 150.4), (AI, m("l", 0, 2), 150.6),
        (AI, m("b", 0), 170.0),                          # a lone gun, nothing after it
        (V, m("r", 1), 184.0), (V, m("r", 1, 1), 184.3),  # still in its window at the end
    ]
    history = {V: {m("r", 2, 2): 12.0}}                  # fired before the logger started
    # (server ts, firer, damage, target, what it hit: side_health 0-3, 4 structure,
    # 5 sails). All seen live on 2026-09-10: the first broadside hits only the
    # structure, as on a stripped side, while a repair raises another side; the
    # last is chain shot, which hits only the sails.
    hits = [(107.0, V, 1.1, AI, 4), (108.2, V, 2.5, AI, 4),
            (132.5, V, 1.0, AI, 1), (132.5, V, 1.0, X, 0),
            (152.0, AI, 1.0, V, 1), (153.0, AI, 1.0, V, 1),
            (184.9, V, 0.5, AI, 5)]
    repairs = [(107.5, AI, 2, 5.0)]

    def snap_at(now, first):
        fires = {sid: {**history.get(sid, {}),
                       **{k: t for s, k, t in fired if s == sid and t <= now}}
                 for sid in (V, AI, X)}
        if now == 121:
            fires[V] = None                              # a torn map
        health = lambda sid: [100 - sum(d for t, _, d, o, c in hits if o == sid and c == i and t <= now)
                              + sum(r for t, o, c, r in repairs if o == sid and c == i and t <= now)
                              for i in range(6)]
        ships = [{"id": sid,
                  "damage_dealt": round(sum(h[2] for h in hits if h[1] == sid and h[0] <= now), 3),
                  "side_health": health(sid)[:4], "structure_health": health(sid)[4],
                  "sail_health": health(sid)[5]} for sid in (V, AI, X)]
        return {"ships": ships, "fires": fires, "batteries": bat if first else {}}

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "b.broadsides.jsonl")
        g = B.Broadsides(path)
        ticks = [float(t) for t in range(90, 186) if not 122 <= t <= 130]
        for i, t in enumerate(ticks):
            g.step(snap_at(t, i == 0), t)
        lines = lambda: [json.loads(l) for l in open(path, encoding="utf-8")]
        before = lines()
        g.finish()
        after = lines()

    got = {(l["id"], l["side"], l["server_ts"]): l for l in after}
    line = lambda sid, side, ts, decks, guns, dmg, tgt: {
        "server_ts": ts, "id": sid, "side": side, "decks": decks, "guns": guns,
        "damage": dmg, "target": tgt}
    check(got.get((V, "port", 98.2)) == line(V, "port", 98.2, {"0": 2, "3": 4}, 6, 3.6, AI),
          f"two lead guns fold into the ripple they lead: one line over eight ticks, "
          f"damage across the window, the target found through a hit on its structure "
          f"and a repair {got.get((V, 'port', 98.2))}")
    check(got.get((V, "starboard", 120.2)) == line(V, "starboard", 120.2, {"1": 3}, 3, 0.0, None),
          "one deck through a torn read -> one line, one deck, a miss")
    check(got.get((V, "starboard", 129.6)) == line(V, "starboard", 129.6, {"2": 3}, 3, 2.0, None),
          "the next deck 8.8 s later, seen on the same tick -> its own line; two ships hit "
          "-> no target")
    ai = [l for l in after if l["id"] == AI and l["side"] in ("port", "starboard")]
    check(sorted((l["side"], l["guns"]) for l in ai) == [("port", 3), ("starboard", 2)],
          f"port and starboard fired together stay two lines {ai}")
    check(sum(l["damage"] for l in ai) == 2.0 and all(l["damage"] >= 0 for l in ai),
          f"overlapping windows never count a hit twice {[l['damage'] for l in ai]}")
    check(got.get((AI, "bow", 170.0), {}).get("guns") == 1,
          "a lone gun with nothing after it is a line of its own")
    check(not any(l["server_ts"] == 12.0 for l in after),
          "a firing from before the first read is not logged")
    check((V, "starboard", 184.0) not in {(l["id"], l["side"], l["server_ts"]) for l in before}
          and got.get((V, "starboard", 184.0)) ==
          line(V, "starboard", 184.0, {"1": 2}, 2, 0.5, AI),
          "the battle's end writes a broadside still inside its damage window; chain "
          f"shot's target is found through its sails {got.get((V, 'starboard', 184.0))}")
    check(len(after) == 7, f"seven broadsides, no more {len(after)}")


if __name__ == "__main__":
    sys.exit(main())
