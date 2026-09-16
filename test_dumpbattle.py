"""Synthetic test for dumpbattle.py: build an `opaque` with a real red-black
tree of battle_ships in a fake address space, and assert the walk recovers them.

Runs without a dump, which is the point: a wrong offset or a wrong assumption
otherwise costs a live battle to discover, and the first real capture was lost
to exactly that -- three invented field bounds (`id < 4096`, `player_id > 0`,
`faction >= 0`) each rejected every real ship. The values here are taken from
that capture, so those specific mistakes cannot come back silently.

Usage:  py test_dumpbattle.py      (exit 0 = pass)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dumpbattle as D

BASE_VA = 0x7FF000000000
UNSET = 0xFFFFFFFFFFFFFFFF


class FakeReader:
    """VAReader over one flat buffer mapped at BASE_VA."""
    def __init__(self, buf, base=BASE_VA):
        self.buf, self.base = buf, base

    def read(self, va, n):
        o = va - self.base
        if o < 0 or o + n > len(self.buf):
            return None
        return bytes(self.buf[o:o + n])


def put_ship(buf, off, *, sid, ship_id, player_id, faction, name, kills,
             assists, dmg, dead, merch, struct_h, max_h, side, max_side,
             sail_h=1.0, water=0.0):
    r = bytearray(D.SHIP_SIZE)
    struct.pack_into("<Q", r, D.O_PLAYER_ID, player_id)
    struct.pack_into("<iii", r, D.O_SHIP_ID, ship_id, sid, faction)
    struct.pack_into("<4f", r, D.O_SIDE_HEALTH, *side)
    struct.pack_into("<ff", r, D.O_STRUCT_HEALTH, struct_h, sail_h)
    struct.pack_into("<4f", r, D.O_MAX_SIDE, *max_side)
    struct.pack_into("<f", r, D.O_MAX_STRUCT, max_h)
    r[D.O_IS_MERCHANT] = 1 if merch else 0
    r[D.O_IS_DEAD] = 1 if dead else 0
    struct.pack_into("<ii", r, D.O_KILLS, kills, assists)
    struct.pack_into("<ff", r, D.O_DAMAGE, dmg, water)
    nb = name.encode()
    if len(nb) <= 15:            # SSO, len 0 included: still a self-pointer
        struct.pack_into("<QQ", r, D.O_NAME, BASE_VA + off + D.O_NAME + 16, len(nb))
        r[D.O_NAME + 16:D.O_NAME + 16 + len(nb)] = nb
    else:
        struct.pack_into("<QQ", r, D.O_NAME, BASE_VA + off + D.O_NAME + 16, len(nb))
        r[D.O_NAME + 16:D.O_NAME + 32] = nb[:16]       # long name, still inline-ish
    buf[off:off + D.SHIP_SIZE] = r


# Values from the first successful capture, 2026-09-09. Note every one of these
# breaks a bound the first version of this scanner imposed.
SHIPS = [
    dict(sid=8906, ship_id=3, player_id=UNSET, faction=1, name="Marlinspike",
         kills=0, assists=0, dmg=81.7, dead=False, merch=False,
         struct_h=360.0, max_h=360.0,
         side=(270.0, 230.22, 216.0, 54.0), max_side=(270.0, 270.0, 216.0, 54.0)),
    dict(sid=15506, ship_id=2, player_id=UNSET, faction=-1, name="AI 0",
         kills=0, assists=0, dmg=39.8, dead=False, merch=False,
         struct_h=64.4, max_h=76.4, sail_h=0.9801,
         side=(0.0, 54.56, 45.86, 7.07), max_side=(57.33, 57.33, 45.86, 11.47)),
    dict(sid=20001, ship_id=9, player_id=0x1A2B3C4D5E, faction=2, name="",
         kills=2, assists=1, dmg=1500.0, dead=True, merch=True,
         struct_h=0.0, max_h=500.0,
         side=(0.0, 0.0, 0.0, 0.0), max_side=(100.0, 100.0, 100.0, 100.0)),
]

NODE_STRIDE = 0x1000
OPAQUE_OFF = 0x200
NODES_OFF = 0x8000


def build():
    """One opaque at OPAQUE_OFF whose ships map holds len(SHIPS) tree nodes."""
    buf = bytearray(os.urandom(0x40000))
    node_va = []
    for i, sp in enumerate(SHIPS):
        off = NODES_OFF + i * NODE_STRIDE
        node_va.append(BASE_VA + off)
        put_ship(buf, off + D.NODE_VALUE, **sp)
        struct.pack_into("<i", buf, off + D.NODE_KEY, sp["sid"])

    # A three-node tree: node[0] is the root, node[1] left, node[2] right.
    def link(i, parent, left, right):
        off = NODES_OFF + i * NODE_STRIDE
        struct.pack_into("<Q", buf, off + 8, parent)
        struct.pack_into("<Q", buf, off + 16, left)
        struct.pack_into("<Q", buf, off + 24, right)

    link(0, 0, node_va[1], node_va[2])
    link(1, node_va[0], 0, 0)
    link(2, node_va[0], 0, 0)

    hdr = BASE_VA + OPAQUE_OFF
    for moff in D.MAP_OFFSETS:
        a = OPAQUE_OFF + moff
        if moff == D.SHIPS_MAP:
            struct.pack_into("<Q", buf, a + 16, node_va[0])         # root
            struct.pack_into("<Q", buf, a + 24, node_va[1])         # leftmost
            struct.pack_into("<Q", buf, a + 32, node_va[2])         # rightmost
            struct.pack_into("<Q", buf, a + 40, len(SHIPS))
        else:                                                        # empty map
            struct.pack_into("<Q", buf, a + 16, 0)
            struct.pack_into("<Q", buf, a + 24, hdr + moff + 8)
            struct.pack_into("<Q", buf, a + 32, hdr + moff + 8)
            struct.pack_into("<Q", buf, a + 40, 0)
    return buf


def main():
    buf = build()
    rd = FakeReader(buf)
    ok = True

    hits = D.scan_opaque(bytes(buf), BASE_VA)
    if OPAQUE_OFF not in [o for o, _ in hits]:
        print(f"  FAIL  opaque at 0x{OPAQUE_OFF:x} not detected (hits={hits[:5]})")
        return 1
    cnt = dict(hits)[OPAQUE_OFF]
    print(f"  ok    opaque detected, ships map count={cnt}")

    nodes = D.walk_tree(rd, BASE_VA + OPAQUE_OFF + D.SHIPS_MAP, cnt)
    if len(nodes) != len(SHIPS):
        print(f"  FAIL  walked {len(nodes)} nodes, expected {len(SHIPS)}")
        return 1
    print(f"  ok    tree walked to {len(nodes)} nodes")

    if not D.keys_agree(rd, nodes):
        print("  FAIL  keys_agree rejected a valid tree")
        return 1
    print("  ok    every key == battle_ship.id")

    by_id = {}
    for nd in nodes:
        rec = rd.read(nd + D.NODE_VALUE, D.SHIP_SIZE)
        s = D.parse_ship(rec, nd + D.NODE_VALUE, rd)
        by_id[s["id"]] = s

    for sp in SHIPS:
        got = by_id.get(sp["sid"])
        if got is None:
            print(f"  FAIL  ship id={sp['sid']} missing"); ok = False; continue
        for k, want in (("ship_id", sp["ship_id"]), ("faction", sp["faction"]),
                        ("name", sp["name"]), ("player_id", sp["player_id"]),
                        ("kills", sp["kills"]), ("assists", sp["assists"]),
                        ("is_dead", sp["dead"]), ("is_merchant", sp["merch"])):
            if got[k] != want:
                print(f"  FAIL  id={sp['sid']} {k}: got {got[k]!r} want {want!r}")
                ok = False
        if abs(got["damage_dealt"] - sp["dmg"]) > 0.01:
            print(f"  FAIL  id={sp['sid']} damage {got['damage_dealt']} vs {sp['dmg']}")
            ok = False
        print(f"  ok    id={got['id']:<6} fac={got['faction']:<3} "
              f"{(got['name'] or '<unnamed>'):<14} dmg={got['damage_dealt']}")

    # keys_agree must actually reject a mismatch, or it filters nothing.
    bad = bytearray(buf)
    struct.pack_into("<i", bad, NODES_OFF + D.NODE_KEY, 999999)
    if D.keys_agree(FakeReader(bad), nodes):
        print("  FAIL  keys_agree accepted a key/id mismatch"); ok = False
    else:
        print("  ok    keys_agree rejects a key/id mismatch")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
