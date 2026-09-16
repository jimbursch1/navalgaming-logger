"""Recover a battle's roster and scoreboard from a full-memory dump of the client.

Battle data is server-supplied and never written to disk, so a dump taken while a
battle is live is the only route -- same situation as the port list, and this
script is the battle-shaped sibling of `dumpports.py`.

**Find `opaque`, then walk the map. Do not scan for battle_ship records.**

The first version scanned memory for battle_ship-shaped bytes using a dozen field
constraints. It found nothing in a real battle dump, because three of those
constraints were wrong, and each was an invention rather than something read out
of DWARF:

  * `id` is a large global ship id (8906, 15506 in the first capture), not a
    small battle slot. A `id < 4096` bound rejected every ship.
  * `player_id` is **0xFFFFFFFFFFFFFFFF** on a ship with no player behind it,
    not 0 and not a small account id.
  * `faction` is **-1** for AI. A `faction >= 0` bound rejected the AI side.

The structural route has no such assumptions and is what works. `opaque` is four
consecutive `std::map`s, and a libstdc++ `_Rb_tree` header validates itself:

    _Rb_tree, 48 bytes:
      +0  key_compare (+pad)   +8  header._M_color
      +16 header._M_parent (root)   +24 header._M_left (leftmost)
      +32 header._M_right           +40 _M_node_count
    empty: parent == 0 and left == right == addr+8
    live:  parent/left/right are heap pointers and count > 0

    opaque, 232 bytes:
      +0 map<int,cannon>              +48 map<int,battle_ship> ships
      +96 map<int,battle_projectile>  +144 map<int,battle_projectile>
      +192 side_ui_state(16)          +208 splash_tracker(24)

    node _Rb_tree_node<pair<const int, battle_ship>>:
      +0 color +8 parent +16 left +24 right +32 key(int) +40 battle_ship

Then the decisive filter, which needs no field heuristics whatsoever: the map is
keyed by ship id, so **every node's key must equal battle_ship.id**. In the first
real capture that took 5,499 four-map candidates down to exactly 1, with both
ships correct. A tree either walks to its stated node count and agrees with its
own keys, or it does not.

Usage:  py dumpbattle.py [dumpfile] [outfile]
"""
import bisect
import json
import os
import struct
import sys

DUMP = sys.argv[1] if len(sys.argv) > 1 else r"C:\temp\lomdump\lom.dmp"
OUT = sys.argv[2] if len(sys.argv) > 2 else r"C:\temp\lomdump\battle.json"

SHIP_SIZE = 1168
CHUNK = 1 << 26

# battle_ship field offsets, from DWARF (GAMEFILES.md 7.11).
O_PLAYER_ID, O_SHIP_ID, O_ID, O_FACTION = 8, 16, 20, 24
O_SIDE_HEALTH, O_STRUCT_HEALTH, O_SAIL_HEALTH = 692, 708, 712
O_MAX_SIDE, O_MAX_STRUCT = 716, 732
O_NAME = 792
O_RENDER_HANDLE = 0                      # render_ship_handle*
# std::vector members, as (offset, element size). A vector is three pointers --
# begin, end, capacity -- so it is either all-zero or strictly ordered, with the
# span an exact multiple of the element size. Random memory almost never
# satisfies that, which makes these the strongest single filter in the record.
VECTORS = ((512, 4),      # sail_health_on_mast: vector<float>
           (976, 16),     # items:               vector<server_item>
           (1120, 8),     # model_decals:        vector<godot::Node3D*>
           (1144, 112))   # pending_decals:      vector<decal>, 112 each
O_HIDE_SAILS = 688                       # hide_sails/hide_bow/hide_stern
O_INVENTORY_OK = 968
O_IS_MERCHANT, O_IS_DEAD = 1096, 1097
O_KILLS, O_ASSISTS, O_DAMAGE, O_WATER = 1100, 1104, 1108, 1112

# Globals with static addresses, for the diagnostic in report_globals().
# DLL ImageBase is 0x391810000, so these are addr-minus-base.
GLOBALS = {"pipe (server_pipe)": 0x1027780,
           "data (async_queue<variant<...,server_battle_message>>)": 0x1462940}
DLL_NAME = "libgdexample"


# --- minidump memory map -------------------------------------------------------
# memory_ranges() and VAReader are lifted verbatim from dumpports.py; they are
# generic minidump plumbing with nothing port-specific in them.

def memory_ranges(path):
    """Return sorted [(va, size, file_offset)] from a minidump's memory streams."""
    f = open(path, "rb")
    hdr = f.read(32)
    if hdr[:4] != b"MDMP":
        raise SystemExit(f"{path}: not a minidump")
    nstreams, dir_rva = struct.unpack_from("<II", hdr, 8)
    f.seek(dir_rva)
    directory = f.read(nstreams * 12)
    ranges = []
    for i in range(nstreams):
        stype, dsize, rva = struct.unpack_from("<III", directory, i * 12)
        if stype == 9:  # Memory64ListStream
            f.seek(rva)
            nr, base_rva = struct.unpack("<QQ", f.read(16))
            desc = f.read(nr * 16)
            off = base_rva
            for j in range(nr):
                va, sz = struct.unpack_from("<QQ", desc, j * 16)
                ranges.append((va, sz, off))
                off += sz
        elif stype == 5:  # MemoryListStream
            f.seek(rva)
            (nr,) = struct.unpack("<I", f.read(4))
            desc = f.read(nr * 16)
            for j in range(nr):
                va, sz, doff = struct.unpack_from("<QII", desc, j * 16)
                ranges.append((va, sz, doff))
    f.close()
    ranges.sort()
    return ranges


class VAReader:
    def __init__(self, path, ranges):
        self.f = open(path, "rb")
        self.ranges = ranges
        self.starts = [r[0] for r in ranges]

    def read(self, va, n):
        i = bisect.bisect_right(self.starts, va) - 1
        if i < 0:
            return None
        start, size, off = self.ranges[i]
        if va + n > start + size:
            return None
        self.f.seek(off + (va - start))
        return self.f.read(n)


def module_base(path, needle=DLL_NAME):
    """Base VA of the gameplay DLL, from ModuleListStream (stream type 4)."""
    f = open(path, "rb")
    hdr = f.read(32)
    nstreams, dir_rva = struct.unpack_from("<II", hdr, 8)
    f.seek(dir_rva)
    directory = f.read(nstreams * 12)
    for i in range(nstreams):
        stype, dsize, rva = struct.unpack_from("<III", directory, i * 12)
        if stype != 4:
            continue
        f.seek(rva)
        (nmod,) = struct.unpack("<I", f.read(4))
        blob = f.read(nmod * 108)
        for j in range(nmod):
            # MINIDUMP_MODULE is 108 bytes: BaseOfImage +0, SizeOfImage +8,
            # CheckSum +12, TimeDateStamp +16, ModuleNameRva +20, then
            # VS_FIXEDFILEINFO (52) and two location descriptors.
            base, = struct.unpack_from("<Q", blob, j * 108)
            name_rva, = struct.unpack_from("<I", blob, j * 108 + 20)
            f.seek(name_rva)
            (blen,) = struct.unpack("<I", f.read(4))
            nm = f.read(blen).decode("utf-16-le", "replace")
            if needle.lower() in nm.lower():
                f.close()
                return base, nm
    f.close()
    return None, None


# --- finding opaque, and walking its ships map ---------------------------------

PMIN, PMAX = 0x10000, 1 << 48
OPAQUE_SIZE = 232
MAP_OFFSETS = (0, 48, 96, 144)
SHIPS_MAP = 48
NODE_KEY, NODE_VALUE = 32, 40


def scan_opaque(buf, base_va):
    """Offsets in buf that look like `opaque` with a non-empty ships map."""
    import numpy as np          # here, not at the top: navalgaming_logger.py imports
                                # this module for the tree walk and never scans
    usable = len(buf) - OPAQUE_SIZE
    if usable <= 0:
        return []
    n = usable // 8
    if n <= 0:
        return []
    a64 = np.frombuffer(buf, dtype="<u8", count=(len(buf) // 8))
    idx = np.arange(n, dtype=np.int64)
    b = idx * 8
    m = np.ones(n, dtype=bool)
    ships_count = None
    for moff in MAP_OFFSETS:
        par = a64[idx + (moff + 16) // 8]
        lef = a64[idx + (moff + 24) // 8]
        rig = a64[idx + (moff + 32) // 8]
        cnt = a64[idx + (moff + 40) // 8]
        selfhdr = base_va + b + moff + 8
        empty = (par == 0) & (lef == selfhdr) & (rig == selfhdr) & (cnt == 0)
        live = ((cnt > 0) & (cnt < 4096)
                & (par >= PMIN) & (par < PMAX)
                & (lef >= PMIN) & (lef < PMAX)
                & (rig >= PMIN) & (rig < PMAX))
        m &= empty | live
        if moff == SHIPS_MAP:
            ships_count = cnt
    m &= ships_count > 0
    return [(int(o) * 8, int(ships_count[o])) for o in np.nonzero(m)[0]]


def walk_tree(rd, map_va, count):
    """In-order walk of a _Rb_tree. Returns node addresses, [] if malformed."""
    hdr = rd.read(map_va + 16, 8)
    if not hdr:
        return []
    root = struct.unpack("<Q", hdr)[0]
    out, stack, cur, guard = [], [], root, 0
    limit = count * 4 + 64                 # cycles in corrupt memory must end
    while (stack or cur) and guard < limit:
        guard += 1
        if cur:
            b = rd.read(cur, 32)
            if not b:
                return []
            stack.append(cur)
            cur = struct.unpack_from("<Q", b, 16)[0]
            if cur and not (PMIN <= cur < PMAX):
                cur = 0
        else:
            node = stack.pop()
            out.append(node)
            b = rd.read(node, 32)
            if not b:
                return []
            cur = struct.unpack_from("<Q", b, 24)[0]
            if cur and not (PMIN <= cur < PMAX):
                cur = 0
    return out


def keys_agree(rd, nodes):
    """Every node's map key must equal the battle_ship.id stored in it."""
    for nd in nodes:
        kb = rd.read(nd + NODE_KEY, 4)
        ib = rd.read(nd + NODE_VALUE + O_ID, 4)
        if not kb or not ib:
            return False
        if struct.unpack("<i", kb)[0] != struct.unpack("<i", ib)[0]:
            return False
    return True


def read_name(rec, va, rd):
    ptr, ln = struct.unpack_from("<QQ", rec, O_NAME)
    if ln == 0:
        return ""                      # unnamed ship: legal, not a rejection
    if ln > 4096:
        return None
    if ptr == va + O_NAME + 16:
        raw = rec[O_NAME + 16:O_NAME + 16 + ln]
    else:
        raw = rd.read(ptr, ln)
    if not raw or len(raw) != ln:
        return None
    try:
        # UTF-8, not ASCII. An ASCII-only filter silently dropped a real port
        # ("Ténès") in the port run; ship and player names are no safer.
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def parse_ship(rec, va, rd):
    """Unpack a battle_ship. The tree walk already established this is one, so
    this does not filter -- it reports what is there."""
    name = read_name(rec, va, rd)
    player_id, = struct.unpack_from("<Q", rec, O_PLAYER_ID)
    ship_id, sid, faction = struct.unpack_from("<iii", rec, O_SHIP_ID)
    side = list(struct.unpack_from("<4f", rec, O_SIDE_HEALTH))
    struct_h, sail_h = struct.unpack_from("<ff", rec, O_STRUCT_HEALTH)
    max_side = list(struct.unpack_from("<4f", rec, O_MAX_SIDE))
    max_struct, = struct.unpack_from("<f", rec, O_MAX_STRUCT)
    kills, assists = struct.unpack_from("<ii", rec, O_KILLS)
    dmg, water = struct.unpack_from("<ff", rec, O_DAMAGE)
    return {"id": sid, "player_id": player_id, "ship_id": ship_id,
            "name": name, "faction": faction,
            # 0xFFFFFFFFFFFFFFFF is "unset". Observed on BOTH ships in the
            # first capture, the local player's included, so it does not mark a
            # ship as AI -- identify the local ship with battle_scene.my_ship_id
            # instead. faction == -1 is the AI marker.
            "player_id_unset": player_id == 0xFFFFFFFFFFFFFFFF,
            "is_merchant": bool(rec[O_IS_MERCHANT]), "is_dead": bool(rec[O_IS_DEAD]),
            "kills": kills, "assists": assists, "damage_dealt": round(dmg, 3),
            "side_health": [round(x, 2) for x in side],
            "max_side_health": [round(x, 2) for x in max_side],
            "structure_health": round(struct_h, 2),
            "max_structure_health": round(max_struct, 2),
            "sail_health": round(sail_h, 4), "water_frac": round(water, 4),
            "va": va}


def report_globals(path):
    base, nm = module_base(path)
    if base is None:
        print("! gameplay DLL not found in ModuleListStream")
        return
    print(f"module: {nm}\n  base 0x{base:x}")
    for label, rva in GLOBALS.items():
        print(f"  0x{base + rva:x}  {label}")


def main():
    # Windows consoles default to cp1252 and a captain's name is arbitrary UTF-8.
    # Printing one crashed an early run *after* the scan and before the JSON was
    # written, losing the whole run to a display detail.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if not os.path.exists(DUMP):
        raise SystemExit(f"{DUMP}: no such file")
    print(f"dump: {DUMP}  ({os.path.getsize(DUMP) / 2**30:.1f} GB)")
    report_globals(DUMP)

    ranges = memory_ranges(DUMP)
    total = sum(r[1] for r in ranges)
    print(f"{len(ranges)} ranges, {total / 2**30:.1f} GB mapped")
    rd = VAReader(DUMP, ranges)

    candidates, scanned, last_gb = [], 0, [-1]
    for va, size, off in ranges:
        pos = 0
        while pos < size:
            take = min(CHUNK, size - pos)
            grab = min(take + OPAQUE_SIZE, size - pos)
            rd.f.seek(off + pos)
            buf = rd.f.read(grab)
            if len(buf) < OPAQUE_SIZE:
                break
            for o, cnt in scan_opaque(buf, va + pos):
                if o < take:
                    candidates.append((va + pos + o, cnt))
            pos += take
            scanned += take
            if scanned >> 30 != last_gb[0]:
                last_gb[0] = scanned >> 30
                print(f"  ... {scanned / 2**30:.0f} GB, {len(candidates)} candidates")

    print(f"\n{len(candidates)} `opaque` candidates with a non-empty ships map")

    # The filter that matters: the tree must walk to its stated size, and every
    # node's key must equal the battle_ship.id inside it.
    battles = []
    for addr, cnt in candidates:
        nodes = walk_tree(rd, addr + SHIPS_MAP, cnt)
        if len(nodes) != cnt or not nodes:
            continue
        if not keys_agree(rd, nodes):
            continue
        ships = []
        for nd in nodes:
            rec = rd.read(nd + NODE_VALUE, SHIP_SIZE)
            if not rec or len(rec) != SHIP_SIZE:
                ships = []
                break
            ships.append(parse_ship(rec, nd + NODE_VALUE, rd))
        if ships:
            battles.append({"opaque_va": addr, "ships": ships})

    print(f"{len(battles)} with self-consistent keys\n")

    # Write first, print second: the output is the point.
    json.dump(battles, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    for bt in battles:
        print(f"battle @0x{bt['opaque_va']:x}  {len(bt['ships'])} ships")
        for s in sorted(bt["ships"], key=lambda x: x["id"]):
            who = "pid unset" if s["player_id_unset"] else f"0x{s['player_id']:x}"
            tags = "".join([" DEAD" if s["is_dead"] else "",
                            " merchant" if s["is_merchant"] else ""])
            print(f"  id={s['id']:<7} hull={s['ship_id']:<4} fac={s['faction']:<3} "
                  f"{'AI' if s['faction'] == -1 else 'player':<7}{who:<12} "
                  f"{(s['name'] or '<unnamed>')[:24]:<24} "
                  f"k={s['kills']} a={s['assists']} dmg={s['damage_dealt']:.1f}")
            print(f"      hull {s['structure_health']:.1f}/{s['max_structure_health']:.1f}"
                  f"   sides {s['side_health']} / {s['max_side_health']}"
                  f"   sail {s['sail_health']}{tags}")

    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
