"""Test for upload.py against a stub HTTP server: no site, no game, no network.

Usage:  py test_upload.py      (exit 0 = pass)
"""
import calendar
import http.server
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE]
import upload as U

FAILS = []
NOW = calendar.timegm((2026, 9, 10, 23, 0, 0))


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class Stub(http.server.ThreadingHTTPServer):
    """Records each request; answers from a queue of status codes, 200 when empty.

    Health pings are kept apart, in .hellos, and answered from .hello_code without
    touching .codes: every other test is about what the site says to a data upload,
    and a ping at the top of each cycle must not shift that queue by one."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests, self.codes, self.hellos = [], [], []
        self.hello_code = 200
        self.current_version = U.VERSION       # what the site calls the newest release
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/logger_upload.php"

    def kinds(self):
        return [r["body"]["kind"] for r in self.requests]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Header names are case-insensitive, and urllib sends them capitalized.
        headers = {k.lower(): v for k, v in self.headers.items()}
        if body.get("kind") == "hello":
            self.server.hellos.append({"headers": headers, "body": body})
            return self.reply(self.server.hello_code,
                              {"ok": True, "current_version": self.server.current_version})
        self.server.requests.append({"headers": headers, "body": body})
        code = self.server.codes.pop(0) if self.server.codes else 200
        reply = {"ok": True, "battle_id": len(self.server.requests)} if code == 200 else \
            {"ok": False, "error": "stub says no"}
        if code == 200 and body["kind"] == "track":
            reply = {"ok": True, "stored": len(body["points"])}
        if code == 200 and body["kind"] == "voyage":
            reply = {"ok": True, "voyage_id": len(self.server.requests)}
        self.reply(code, reply)

    def reply(self, code, body):
        out = json.dumps(body).encode()
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def ship(sid, local=False):
    return {"id": sid, "player_id": 18446744073709551615, "ship_id": 3 if local else 2,
            "name": "Marlinspike" if local else "AI 0", "faction": 1 if local else -1,
            "player_id_unset": True, "is_merchant": False, "is_dead": not local,
            "kills": 1 if local else 0, "assists": 0, "damage_dealt": 199.542,
            "side_health": [217.09, 270.0, 216.0, 54.0], "max_side_health": [270.0] * 2 + [216.0, 54.0],
            "structure_health": 360.0, "max_structure_health": 360.0, "sail_health": 1.0,
            "water_frac": 0.0, "in_map": True, "local": local,
            "battery": [{"side": "port", "deck": 0, "pd": 12, "guns": 6}]}


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def write_battle(root, started, ended=True):
    """A battle file, named from its start like the logger's."""
    path = os.path.join(root, "battles", U.voyage_name(iso(started))[:-1])   # .jsonl -> .json
    b = {"battle_scene": 2682492158032, "started": iso(started),
         "ended": iso(started + 548) if ended else None, "reads": 545,
         "location": {"lat": 38.29875, "lon": 15.49286}, "my_ship_id": 8906,
         "ships": [ship(15677), ship(8906, local=True)]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(b, f)
    return path


def sea(t, lat, lon):
    return {"t": iso(t), "state": "sea",
            "lat": lat, "lon": lon, "heading": 110.8, "x": -83321.0, "y": -173246.5,
            "server_ts": 1009.77}


def port(t, pid):
    return {"t": iso(t), "state": "port", "port": pid}


def voyage(t0, arrive=True, start=0, to=5):
    """In port `start` at t0, then a line every 3 s at sea for 2 min, ~1.5 m/s, a
    battle at +125 s, sea again, and in port `to` at +300 s. start=None: the logger
    started at sea."""
    lines = [port(t0, start)] if start is not None else []
    lines += [sea(t0 + 3 * i, 38.25 + 0.00004 * i, 15.55) for i in range(1, 41)]
    lines += [{"t": iso(t0 + 125), "state": "battle"}, sea(t0 + 200, 38.26, 15.56)]
    return lines + ([port(t0 + 300, to)] if arrive else [])


def write_track(root, lines, name=None, partial=b""):
    path = os.path.join(root, "track", name or U.voyage_name(lines[0]["t"]))
    with open(path, "ab") as f:
        for line in lines:
            f.write((json.dumps(line) + "\n").encode())
        f.write(partial)
    return path


def fresh(stub, **cfg):
    root = tempfile.mkdtemp()
    for d in ("battles", "track"):
        os.makedirs(os.path.join(root, d))
    log = []
    up = U.Uploader(root, dict({"url": stub.url, "token": "t" * 43}, **cfg), log.append,
                    now=lambda: NOW)
    return root, up, log


def test_voyage(stub):
    root, up, log = fresh(stub, basic_auth="devlom:pw")
    t0 = NOW - 3600
    lines = voyage(t0)
    path = write_track(root, lines[:-1])                         # at sea
    write_battle(root, t0 + 125)                                 # fought on this voyage
    write_battle(root, t0 + 130, ended=False)                    # the logger died mid-battle
    write_battle(root, t0 - 600)                                 # before it sailed
    write_battle(root, t0 + 900)                                 # after it docked
    with open(os.path.join(root, "battles", "x.motion.jsonl"), "w") as f:
        f.write('{"started": "%s"}\n' % iso(t0 + 125))
    write_track(root, [sea(t0 + 1, 38.0, 15.0), port(t0 + 2, 1)], name="20260910.jsonl")
    stub.requests.clear()
    up.cycle()
    check(stub.requests == [], "a voyage still at sea sends nothing; an old day file is not a voyage")

    write_track(root, lines[-1:], name=os.path.basename(path))  # docks
    up.cycle()
    check(stub.kinds() == ["battle", "battle", "track", "voyage"],
          f"on arrival: its battles, then its track, then the voyage record {stub.kinds()}")
    r = stub.requests[0]
    check(r["headers"].get("x-navalgaming-logger-token") == "t" * 43, "token header sent")
    check(r["headers"].get("authorization", "").startswith("Basic "), "dev basic auth sent")
    check(r["headers"].get("content-type") == "application/json", "content type json")
    battles = [q["body"]["battle"] for q in stub.requests[:2]]
    check([b["started"] for b in battles] == [iso(t0 + 125), iso(t0 + 130)] and
          battles[1]["ended"] is None,
          "only the battles inside the voyage, an unfinished one included")
    b = battles[0]
    check("battle_scene" not in b, "the memory address stays local")
    check(b["reads"] == 545 and "last_read" in b, "the read count and last read go up")
    check(set(b["ships"][0]) == set(U.SHIP_FIELDS), "every ship field sent")
    check(b["ships"][0]["battery"] == ship(1)["battery"], "the battery goes up")
    check(all(s["player_id"] is None for s in b["ships"]),
          "an unset player_id (2**64-1) goes up as null")

    pts = stub.requests[2]["body"]["points"]
    check([p["t"] for p in pts if p["state"] == "sea"] ==
          [iso(t0 + 3 * i) for i in range(1, 41)] + [iso(t0 + 200)],
          "every sea point sent, unthinned")
    check(pts[0] == port(t0, 0) and pts[-1] == port(t0 + 300, 5),
          "the port left and the port reached are both track points")
    check(pts[1] == lines[1], "a sea point goes up whole: x, y and server_ts included")
    check(stub.requests[3]["body"]["voyage"] ==
          {"departed": iso(t0), "arrived": iso(t0 + 300), "from_port": 0, "to_port": 5},
          "the voyage record: its first and last lines")
    check(up.state["voyages"] == {os.path.basename(path): 4}, "marked sent, with the site's id")

    stub.requests.clear()
    up.cycle()
    check(stub.requests == [], "second cycle sends nothing")
    up2 = U.Uploader(root, up.config, log.append, now=lambda: NOW)
    up2.cycle()
    check(stub.requests == [], "state survives a restart")


def test_queue(stub):
    root, up, log = fresh(stub)
    write_track(root, voyage(NOW - 7200, start=None, to=3))     # the logger started at sea
    write_track(root, voyage(NOW - 3600, start=3))
    write_track(root, [port(NOW - 1800, 3)])                     # killed before it sailed
    stub.requests.clear()
    up.cycle()
    check(stub.kinds() == ["track", "voyage"], "one voyage a cycle")
    v = stub.requests[1]["body"]["voyage"]
    check(v["departed"] == iso(NOW - 7200 + 3) and v["from_port"] is None and v["to_port"] == 3,
          f"the oldest first; started at sea, its origin is null {v}")
    stub.requests.clear()
    up.cycle()
    check(stub.kinds() == ["track", "voyage"] and
          stub.requests[1]["body"]["voyage"]["from_port"] == 3, "the next one next cycle")
    stub.requests.clear()
    up.cycle()
    check(stub.requests == [], "a file that never left port is not a voyage")


def test_retry_and_reject(stub):
    root, up, log = fresh(stub)
    write_track(root, voyage(NOW - 3600))
    write_battle(root, NOW - 3600 + 125)
    stub.requests.clear()
    stub.codes = [200, 503]
    up.cycle()
    check(stub.kinds() == ["battle", "track"], "503 on the track: the cycle stops there")
    check(up.state["voyages"] == {}, "503: the voyage is not marked sent")
    check(any("trying again" in m for m in log), "503: logged as try-again")
    stub.codes = [429]
    stub.requests.clear()
    up.cycle()
    check(up.state["voyages"] == {}, "429: nothing marked sent")
    stub.requests.clear()
    stub.codes = [302]
    up.cycle()
    check(up.state["voyages"] == {} and any("HTTP 302" in m for m in log),
          "302: not followed -- the token is never re-sent elsewhere; try again")
    stub.requests.clear()
    up.cycle()
    check(stub.kinds() == ["battle", "track", "voyage"] and len(up.state["voyages"]) == 1,
          "then the whole voyage is sent again, and marked")

    root, up, log = fresh(stub)
    write_track(root, voyage(NOW - 3600))
    write_battle(root, NOW - 3600 + 125)
    stub.codes = [400, 200, 400]
    stub.requests.clear()
    up.cycle()
    up.cycle()
    check(stub.kinds() == ["battle", "track", "voyage"],
          "400: a rejected battle doesn't stop its voyage; a rejected voyage is not retried")
    check(list(up.state["voyages"].values()) == [None], "a rejected voyage is marked sent anyway")
    check(any("rejected, HTTP 400: stub says no" in m for m in log), "400: the site's reason logged")


def test_never_raises(stub):
    root, up, log = fresh(stub)
    up.config["url"] = "http://127.0.0.1:1/logger_upload.php"    # refused
    write_track(root, voyage(NOW - 3600))
    up.cycle()
    check(any("trying again" in m for m in log), "connection refused: try again, no raise")
    with open(up.state_path, "w") as f:
        f.write("[not a state")
    up2 = U.Uploader(root, dict(up.config, url=stub.url), log.append, now=lambda: NOW)
    check(up2.state == {"voyages": {}}, "a corrupt state file starts fresh")
    with open(os.path.join(root, "battles", "junk.json"), "w") as f:
        f.write("{not json")
    with open(os.path.join(root, "track", U.voyage_name(iso(NOW - 3600))), "ab") as f:
        f.write(b'["not a line"]\n{"t": "garbage"}\n')
    write_track(root, [port(NOW - 3000, 9)], name=U.voyage_name(iso(NOW - 3600)))
    stub.requests.clear()
    up2.cycle()
    check(stub.kinds() == ["track", "voyage"] and stub.requests[1]["body"]["voyage"]["to_port"] == 9,
          "a junk battle file and junk lines are skipped, the rest go")

    up2.voyages = lambda: 1 / 0
    up2.cycle()
    check(any("ZeroDivisionError" in m for m in log), "an unexpected error is logged, not raised")


def write_battle_data(battle_path, n_motion, n_broadsides, n_wind=0):
    """The battle's .motion.jsonl and .broadsides.jsonl, each ending in a torn line, and
    with n_wind a .wind.jsonl too."""
    stem = battle_path[:-len(".json")]
    if n_wind:
        with open(stem + ".wind.jsonl", "w", encoding="utf-8") as f:
            for i in range(n_wind):
                f.write(json.dumps({"server_ts": 939.03 + i, "dir": 299.4, "speed": 20.0}) + "\n")
            f.write('{"server_ts": 9')
    with open(stem + ".motion.jsonl", "w", encoding="utf-8") as f:
        for i in range(n_motion):
            f.write(json.dumps({"id": (8906, 15677)[i % 2], "server_ts": 974.87 + i // 2,
                                "x": -162.4, "y": -79.5, "heading": 295.2}) + "\n")
        f.write('{"id": 8906, "server_ts": 99')
    with open(stem + ".broadsides.jsonl", "w", encoding="utf-8") as f:
        for i in range(n_broadsides):
            f.write(json.dumps({"server_ts": 1041.29 + i, "id": 8906, "side": "port",
                                "decks": {"0": 6, "1": 13}, "guns": 19, "damage": 3.745,
                                "target": 15677}) + "\n")
        f.write('{"server_ts"')


def test_battle_data(stub):
    root, up, log = fresh(stub)
    t0 = NOW - 3600
    write_track(root, voyage(t0))
    write_battle_data(write_battle(root, t0 + 125), U.MAX_MOTION + 3, U.MAX_BROADSIDES + 1, U.MAX_WIND + 2)
    stub.requests.clear()
    up.cycle()
    check(stub.kinds() == ["battle", "motion", "motion", "broadsides", "broadsides", "wind", "wind",
                           "track", "voyage"],
          f"a battle, then its motion, broadsides and wind in chunks, then the track {stub.kinds()}")
    bodies = [r["body"] for r in stub.requests]
    check(all(q["started"] == iso(t0 + 125) for q in bodies[1:7]),
          "motion, broadsides and wind name their battle by its start")
    check([len(q["wind"]) for q in bodies[5:7]] == [U.MAX_WIND, 2] and
          bodies[5]["wind"][0] == {"server_ts": 939.03, "dir": 299.4, "speed": 20.0},
          "every wind sample, whole; the torn last line left out")
    check([len(q["motion"]) for q in bodies[1:3]] == [U.MAX_MOTION, 3],
          "every motion line, unthinned; the torn last line left out")
    check(bodies[1]["motion"][0] == [8906, 974.87, -162.4, -79.5, 295.2],
          "a motion line is [id, server_ts, x, y, heading]")
    check([len(q["broadsides"]) for q in bodies[3:5]] == [U.MAX_BROADSIDES, 1],
          "every broadside; the torn last line left out")
    check(bodies[3]["broadsides"][0] == {"server_ts": 1041.29, "id": 8906, "side": "port",
                                         "decks": {"0": 6, "1": 13}, "guns": 19,
                                         "damage": 3.745, "target": 15677},
          "a broadside goes up whole, decks included")

    root, up, log = fresh(stub)
    write_track(root, voyage(t0))
    write_battle_data(write_battle(root, t0 + 125), 10, 2)
    stub.codes = [400]
    stub.requests.clear()
    up.cycle()
    check(stub.kinds() == ["battle", "track", "voyage"],
          "a rejected battle: no motion or broadsides, which would have no battle to join")


def test_chunk_sizes():
    """The largest request each kind can make, with every number at its widest,
    fits the site's 256 KB body."""
    worst = {
        "track": ("points", U.MAX_POINTS, {
            "t": "2026-09-11T16:41:01Z", "state": "sea", "lat": -38.123456, "lon": -15.123456,
            "heading": 359.9, "x": -123456.7, "y": -123456.7, "server_ts": 1234567.89}),
        "motion": ("motion", U.MAX_MOTION, [9999999, 1234567.89, -123456.7, -123456.7, 359.9]),
        "broadsides": ("broadsides", U.MAX_BROADSIDES, {
            "server_ts": 1234567.89, "id": 9999999, "side": "starboard",
            "decks": {"0": 16, "1": 16, "2": 16, "3": 16}, "guns": 64, "damage": 1234.567,
            "target": 9999999}),
        "wind": ("wind", U.MAX_WIND, {"server_ts": 1234567.89, "dir": 359.9, "speed": 1234.56}),
    }
    for kind, (key, n, line) in worst.items():
        body = {"kind": kind, "started": "2026-09-11T16:41:01Z", key: [line] * n}
        size = len(json.dumps(body, separators=(",", ":")))
        check(size < 262144, f"a full {kind} request is {size // 1024} KB, under 256 KB")


def test_backfill(stub):
    root, up, log = fresh(stub)
    t0 = NOW - 7200
    write_track(root, voyage(t0))
    write_track(root, voyage(t0 + 3600, arrive=False))              # still at sea
    write_battle_data(write_battle(root, t0 + 125), 4, 1)          # in the first voyage
    write_battle(root, t0 - 600)                                   # in no voyage
    up.cycle()
    sent = dict(up.state["voyages"])
    stub.requests.clear()
    up.backfill()
    check(stub.kinds() == ["battle", "battle", "motion", "broadsides", "track", "voyage"],
          f"backfill: every battle with its data, then every finished voyage; a battle with no "
          f"wind file sends no wind request {stub.kinds()}")
    check(up.state["voyages"] == sent, "backfill leaves the upload state alone")


def until(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_runner_and_config(stub):
    root, up, log = fresh(stub)
    cycles, cycle = [], up.cycle
    up.cycle = lambda: (cycles.append(1), cycle())
    path = write_track(root, voyage(NOW - 3600, arrive=False))
    r = U.Runner(up)
    r.thread.start()
    stub.requests.clear()
    check(until(lambda: cycles) and stub.requests == [], "the runner's first cycle: still at sea")
    write_track(root, [port(NOW - 3000, 5)], name=os.path.basename(path))
    r.nudge()                                                    # the ship docks
    check(until(lambda: "voyage" in stub.kinds()), "nudge() runs a cycle at once, not in 5 min")
    write_track(root, voyage(NOW - 2400))
    r.nudge()
    check(until(lambda: stub.kinds().count("voyage") == 2), "a second nudge works too")
    n = len(cycles)
    time.sleep(0.3)
    check(len(cycles) == n, f"and the runner then waits, rather than spinning {len(cycles) - n}")
    write_track(root, voyage(NOW - 1800))                        # docks just before exit
    r.stop(timeout=10)
    check(not r.thread.is_alive() and stub.kinds().count("voyage") == 3, "stop() runs one last cycle")

    d = tempfile.mkdtemp()
    check(U.start(root, os.path.join(d, "none.json"), log.append) is None, "no config: off")
    p = os.path.join(d, "upload.json")
    with open(p, "w") as f:
        json.dump({"url": "http://lom.navalgaming.com/logger_upload.php", "token": "x"}, f)
    check(U.start(root, p, log.append) is None, "plain http refused")


def test_new_token():
    import hashlib
    d = tempfile.mkdtemp()
    p = os.path.join(d, "navalgaming-logger", "upload.json")
    digest = U.new_token("https://lom.navalgaming.com/logger_upload.php", p)
    cfg = U.load_config(p)
    check(len(cfg["token"]) == 43 and set(cfg["token"]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"),
        "new token: 43 characters of base64url, the site's format")
    check(digest == hashlib.sha256(cfg["token"].encode()).hexdigest() and len(digest) == 64,
          "new token: returns the SHA-256 of what it saved")
    check("basic_auth" not in cfg, "new token: no basic_auth out of nowhere")
    first = cfg["token"]
    U.new_token("https://lom.navalgaming.com/logger_upload.php", p)
    check(U.load_config(p)["token"] != first, "new token: a fresh one each time")

    with open(p, "w") as f:
        json.dump({"url": "https://devlom.navalgaming.com/logger_upload.php", "token": "x",
                   "basic_auth": "devlom:pw"}, f)
    U.new_token("https://devlom.navalgaming.com/logger_upload.php", p)
    check(U.load_config(p).get("basic_auth") == "devlom:pw", "new token: dev basic_auth carried over, same host")
    U.new_token("https://lom.navalgaming.com/logger_upload.php", p)
    check("basic_auth" not in U.load_config(p), "new token: basic_auth dropped for another host")
    for bad in ("http://lom.navalgaming.com/logger_upload.php", "http://localhost.evil.com/x",
                "http://127.0.0.1.evil.com/x", "https:///x"):
        try:
            U.new_token(bad, p)
            check(False, f"new token: {bad} refused")
        except ValueError:
            check(U.load_config(p)["url"].startswith("https://lom."), f"new token: {bad} refused, config untouched")


def test_nowhere(stub):
    """An undock and redock is not a voyage: prod's voyage 2 was one tick at sea."""
    root, up, log = fresh(stub)
    t0 = NOW - 7200
    flicker = [port(t0, 0), sea(t0 + 3, 38.272, 15.551), port(t0 + 6, 0)]
    near = [port(t0 + 600, 0), sea(t0 + 603, 38.272, 15.551),
            dict(sea(t0 + 606, 38.273, 15.551), x=-83321.0 + 150), port(t0 + 900, 0)]
    far = [port(t0 + 1800, 0), sea(t0 + 1803, 38.272, 15.551),
           dict(sea(t0 + 1806, 38.28, 15.551), x=-83321.0 + 250), port(t0 + 2400, 0)]
    blind = sea(t0 + 3603, 38.272, 15.551)
    del blind["x"], blind["y"]
    unknown = [port(t0 + 3600, 0), blind, port(t0 + 3606, 0)]
    for lines in (flicker, near, far, unknown):
        write_track(root, lines)
    check(U.went_nowhere(flicker) and U.went_nowhere(near) and not U.closed(flicker),
          "one tick at sea, or never 200 units out: went nowhere, so not a finished voyage")
    check(not any(U.went_nowhere(v) for v in (voyage(t0), far, unknown)),
          "a battle, 250 units out, or a sea line with no x/y: went somewhere")
    stub.requests.clear()
    for _ in range(4):
        up.cycle()
    departed = lambda: [r["body"]["voyage"]["departed"] for r in stub.requests
                        if r["body"]["kind"] == "voyage"]
    check(departed() == [iso(t0 + 1800), iso(t0 + 3600)],
          f"cycles skip the voyages that went nowhere and send the rest {departed()}")
    stub.requests.clear()
    up.backfill()
    check(departed() == [iso(t0 + 1800), iso(t0 + 3600)], f"so does backfill {departed()}")


def test_hello(stub):
    """The health signal (docs/plans/logger-health.md): every cycle opens with one,
    it carries what the logger is doing, and it can never cost a voyage."""
    root, up, log = fresh(stub)
    stub.hellos.clear()
    up.cycle()
    check([h["body"] for h in stub.hellos] ==
          [{"kind": "hello", "version": U.VERSION, "state": "waiting"}],
          f"a cycle opens with one ping, and says what the logger is doing {stub.hellos}")
    check(stub.hellos[0]["headers"].get("x-navalgaming-logger-token") == "t" * 43,
          "the ping carries the token like any other request")

    up.state_name = "attached"
    up.cycle()
    check([h["body"]["state"] for h in stub.hellos] == ["waiting", "attached"],
          "one ping a cycle, reporting the state the main thread set")
    check(not [l for l in log if "is available" in l],
          "a logger on the current release is told nothing")

    # The site names a newer release. A 5-minute cycle must not say so 288 times a day.
    stub.current_version = "2099-01-01"
    up.cycle()
    up.cycle()
    told = [l for l in log if "is available" in l]
    check(len(told) == 1 and "2099-01-01" in told[0],
          f"an out-of-date logger is told once per release, not once per cycle {told}")
    stub.current_version = "2099-02-02"
    up.cycle()
    check(len([l for l in log if "is available" in l]) == 2, "a further release is named again")

    # A logger running ahead of the site is a development one, not a stale one.
    root, up, log = fresh(stub)
    stub.current_version = "2000-01-01"
    up.cycle()
    check(not [l for l in log if "is available" in l], "a logger ahead of the site is left alone")

    # The ping is opportunistic: losing it must not cost the voyage behind it.
    root, up, log = fresh(stub)
    stub.hello_code, stub.current_version = 503, U.VERSION
    write_track(root, voyage(NOW - 3600, start=3))
    stub.requests.clear()
    stub.hellos.clear()
    up.cycle()
    check(stub.kinds() == ["track", "voyage"],
          f"a ping the site refused still lets the voyage go {stub.kinds()}")
    check(len(stub.hellos) == 1, "and the ping was tried")


def main():
    test_new_token()
    test_chunk_sizes()
    stub = Stub()
    for t in (test_voyage, test_battle_data, test_backfill, test_queue, test_nowhere, test_retry_and_reject,
              test_never_raises, test_runner_and_config, test_hello):
        stub.codes = []
        stub.hellos, stub.hello_code, stub.current_version = [], 200, U.VERSION
        t(stub)
    stub.shutdown()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
