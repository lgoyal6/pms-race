"""Mock legacy PMS: HTML form screens over a real SQLite store.

Four write-path modes, selected per trial via POST /reset:

  legacy   the screen answered the availability question, so the booking form
           just writes. No re-check, no version token. This is the "green
           screen" model: availability lookup and record write are separate
           transactions with an arbitrary human (or agent) gap between them.
  recheck  the server re-checks availability inside a single IMMEDIATE
           transaction before inserting. Best a server can do when it has no
           idea which screen the client was looking at.
  occ      optimistic concurrency. Every screen carries a version token; every
           write is conditional on it. Stale token -> 409.
  lease    booking-intent lease with a TTL and a monotonic epoch. A writer must
           present a lease that is held, unexpired, and epoch-current. Stealing
           an expired lease bumps the epoch, which fences the old holder.

SQLite is genuinely transactional. Every race below happens anyway, because the
transaction boundary is one HTTP request and the agent's decision spans two.
"""

import http.server
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pms.db")
STATE = {"mode": "legacy"}
_ack_lock = threading.Lock()


def db():
    c = sqlite3.connect(DB_PATH, timeout=20.0, isolation_level=None)
    c.execute("PRAGMA busy_timeout=20000")
    c.row_factory = sqlite3.Row
    return c


def init_db(mode):
    STATE["mode"] = mode
    c = db()
    c.executescript(
        """
        PRAGMA journal_mode=WAL;
        DROP TABLE IF EXISTS rooms;
        DROP TABLE IF EXISTS reservations;
        DROP TABLE IF EXISTS leases;
        DROP TABLE IF EXISTS acks;
        CREATE TABLE rooms (
            room TEXT, night TEXT, status TEXT, version INTEGER,
            PRIMARY KEY (room, night));
        -- deliberately NO unique index on (room, night) for active reservations:
        -- that constraint is the thing legacy property management systems do not have.
        CREATE TABLE reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, night TEXT,
            guest TEXT, nights INTEGER, status TEXT, version INTEGER);
        CREATE TABLE leases (
            resource TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, expires_at REAL);
        CREATE TABLE acks (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, kind TEXT, detail TEXT);
        """
    )
    c.execute("INSERT INTO rooms VALUES ('101','2026-08-20','free',1)")
    c.close()


def seed_reservation(guest="alice", nights=2):
    c = db()
    c.execute(
        "INSERT INTO reservations (room,night,guest,nights,status,version)"
        " VALUES ('101','2026-08-20',?,?,'active',1)",
        (guest, nights),
    )
    rid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    c.execute("UPDATE rooms SET status='occupied', version=version+1"
              " WHERE room='101' AND night='2026-08-20'")
    c.close()
    return rid


def ack(c, actor, kind, detail):
    """Ledger of what the server told a caller succeeded.

    Same contract as strata's crash harness: an acknowledged write is a promise.
    The invariant checker replays this ledger against the final database.
    """
    c.execute("INSERT INTO acks (actor,kind,detail) VALUES (?,?,?)",
              (actor, kind, json.dumps(detail)))


# --------------------------------------------------------------------------- leases

def lease_check(c, resource, holder, epoch):
    """Returns None if the caller may write, else a rejection reason."""
    row = c.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
    if row is None:
        return "no_lease"
    if row["holder"] != holder:
        return "not_holder"
    if row["epoch"] != epoch:
        return "fenced"          # somebody stole the lease and bumped the epoch
    if row["expires_at"] <= time.time():
        return "lease_expired"
    return None


def lease_acquire(resource, holder, ttl_ms):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
        now = time.time()
        if row is not None and row["expires_at"] > now:
            c.execute("COMMIT")
            return 409, {"error": "held", "holder": row["holder"]}
        epoch = (row["epoch"] + 1) if row is not None else 1
        c.execute("INSERT INTO leases VALUES (?,?,?,?)"
                  " ON CONFLICT(resource) DO UPDATE SET holder=?, epoch=?, expires_at=?",
                  (resource, holder, epoch, now + ttl_ms / 1000.0,
                   holder, epoch, now + ttl_ms / 1000.0))
        c.execute("COMMIT")
        return 200, {"epoch": epoch, "resource": resource}
    finally:
        c.close()


def lease_release(resource, holder, epoch):
    c = db()
    # expire in place; the epoch keeps climbing so a released holder can never
    # come back and write.
    c.execute("UPDATE leases SET expires_at=0 WHERE resource=? AND holder=? AND epoch=?",
              (resource, holder, epoch))
    c.close()
    return 200, {"ok": True}


# --------------------------------------------------------------------------- writes

def do_book(f):
    mode, actor = STATE["mode"], f.get("actor", "?")
    room, night = f.get("room", "101"), f.get("night", "2026-08-20")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if mode == "lease":
            bad = lease_check(c, f"room:{room}:{night}", actor, int(f.get("epoch", 0)))
            if bad:
                c.execute("COMMIT")
                return 409, {"error": bad}
        if mode == "occ":
            n = c.execute("UPDATE rooms SET status='occupied', version=version+1"
                          " WHERE room=? AND night=? AND version=?",
                          (room, night, int(f.get("room_version", -1)))).rowcount
            if n == 0:
                c.execute("COMMIT")
                return 409, {"error": "stale_version"}
        elif mode == "recheck":
            row = c.execute("SELECT status FROM rooms WHERE room=? AND night=?",
                            (room, night)).fetchone()
            if row["status"] != "free":
                c.execute("COMMIT")
                return 409, {"error": "occupied"}
            c.execute("UPDATE rooms SET status='occupied', version=version+1"
                      " WHERE room=? AND night=?", (room, night))
        else:  # legacy, lease
            c.execute("UPDATE rooms SET status='occupied', version=version+1"
                      " WHERE room=? AND night=?", (room, night))
        c.execute("INSERT INTO reservations (room,night,guest,nights,status,version)"
                  " VALUES (?,?,?,?, 'active', 1)",
                  (room, night, f.get("guest", actor), int(f.get("nights", 1))))
        rid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        ack(c, actor, "book", {"res_id": rid, "room": room, "night": night})
        c.execute("COMMIT")
        return 200, {"res_id": rid}
    finally:
        c.close()


def do_modify(f):
    """The legacy form posts the WHOLE record back, which is the lost-update vector."""
    mode, actor = STATE["mode"], f.get("actor", "?")
    rid = int(f["res_id"])
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if mode == "lease":
            bad = lease_check(c, f"res:{rid}", actor, int(f.get("epoch", 0)))
            if bad:
                c.execute("COMMIT")
                return 409, {"error": bad}
        if mode == "occ":
            n = c.execute(
                "UPDATE reservations SET guest=?, nights=?, status=?, version=version+1"
                " WHERE id=? AND version=?",
                (f["guest"], int(f["nights"]), f["status"], rid,
                 int(f.get("res_version", -1)))).rowcount
            if n == 0:
                c.execute("COMMIT")
                return 409, {"error": "stale_version"}
        else:
            # legacy and recheck are identical here: re-checking availability
            # fixes double-booking and nothing else.
            c.execute("UPDATE reservations SET guest=?, nights=?, status=?,"
                      " version=version+1 WHERE id=?",
                      (f["guest"], int(f["nights"]), f["status"], rid))
        row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
        c.execute("UPDATE rooms SET status=?, version=version+1 WHERE room=? AND night=?",
                  ("occupied" if row["status"] == "active" else "free",
                   row["room"], row["night"]))
        ack(c, actor, "modify",
            {"res_id": rid, "guest": f["guest"], "nights": int(f["nights"]),
             "status": f["status"]})
        c.execute("COMMIT")
        return 200, {"res_id": rid}
    finally:
        c.close()


def do_cancel(f):
    mode, actor = STATE["mode"], f.get("actor", "?")
    rid = int(f["res_id"])
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if mode == "lease":
            bad = lease_check(c, f"res:{rid}", actor, int(f.get("epoch", 0)))
            if bad:
                c.execute("COMMIT")
                return 409, {"error": bad}
        if mode == "occ":
            n = c.execute("UPDATE reservations SET status='cancelled', version=version+1"
                          " WHERE id=? AND version=?",
                          (rid, int(f.get("res_version", -1)))).rowcount
            if n == 0:
                c.execute("COMMIT")
                return 409, {"error": "stale_version"}
        else:
            c.execute("UPDATE reservations SET status='cancelled', version=version+1"
                      " WHERE id=?", (rid,))
        row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
        c.execute("UPDATE rooms SET status='free', version=version+1"
                  " WHERE room=? AND night=?", (row["room"], row["night"]))
        ack(c, actor, "cancel", {"res_id": rid})
        c.execute("COMMIT")
        return 200, {"res_id": rid}
    finally:
        c.close()


# --------------------------------------------------------------------------- screens

ROOM_SCREEN = """<html><body>
<h1>AVAILABILITY - ROOM {room} / {night}</h1>
<p>STATUS: {status}</p>
<form method="post" action="/book">
<input type="hidden" name="room" value="{room}">
<input type="hidden" name="night" value="{night}">
<input type="hidden" name="room_version" value="{version}">
<input type="text" name="guest" value="">
<input type="submit" value="BOOK">
</form></body></html>"""

RES_SCREEN = """<html><body>
<h1>RESERVATION {id}</h1>
<form method="post" action="/modify">
<input type="hidden" name="res_id" value="{id}">
<input type="hidden" name="res_version" value="{version}">
<input type="text" name="guest" value="{guest}">
<input type="text" name="nights" value="{nights}">
<input type="text" name="status" value="{status}">
<input type="submit" value="SAVE">
</form></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        c = db()
        try:
            if u.path == "/screen/room":
                r = c.execute("SELECT * FROM rooms WHERE room=? AND night=?",
                              (q.get("room", "101"), q.get("night", "2026-08-20"))).fetchone()
                return self._send(200, ROOM_SCREEN.format(**dict(r)), "text/html")
            if u.path == "/screen/res":
                r = c.execute("SELECT * FROM reservations WHERE id=?",
                              (int(q["id"]),)).fetchone()
                return self._send(200, RES_SCREEN.format(**dict(r)), "text/html")
            if u.path == "/dump":
                return self._send(200, json.dumps({
                    "mode": STATE["mode"],
                    "rooms": [dict(r) for r in c.execute("SELECT * FROM rooms")],
                    "reservations": [dict(r) for r in c.execute("SELECT * FROM reservations")],
                    "acks": [dict(r) for r in c.execute("SELECT * FROM acks ORDER BY seq")],
                }))
        finally:
            c.close()
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        f = {k: v[0] for k, v in
             urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        try:
            if self.path == "/reset":
                init_db(f.get("mode", "legacy"))
                rid = seed_reservation() if f.get("seed_res") == "1" else None
                return self._send(200, json.dumps({"ok": True, "res_id": rid}))
            if self.path == "/lease":
                code, body = lease_acquire(f["resource"], f["actor"], float(f["ttl_ms"]))
            elif self.path == "/release":
                code, body = lease_release(f["resource"], f["actor"], int(f["epoch"]))
            elif self.path == "/book":
                code, body = do_book(f)
            elif self.path == "/modify":
                code, body = do_modify(f)
            elif self.path == "/cancel":
                code, body = do_cancel(f)
            else:
                code, body = 404, {}
            self._send(code, json.dumps(body))
        except Exception as e:                       # surfaced, never swallowed
            self._send(500, json.dumps({"error": repr(e)}))


def serve(port):
    init_db("legacy")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8799)
