"""Concurrency-correctness harness for computer-use agents on a legacy PMS.

An agent GETs an HTML screen, spends think_ms deciding (that is the vision
model's window), then POSTs a form. Everything between the GET and the POST is
somebody else's turn. This harness drives N of those agents at one room-night
and checks two invariants against the server's own acknowledgement ledger.

INVARIANT 1  NO_DOUBLE_BOOK
    at most one active reservation per room-night.

INVARIANT 2  ACKED_INTENT_DURABILITY
    every field change the server acknowledged is present in the final record,
    unless a later acknowledged change deliberately targeted that same field.
    A legacy screen posts the WHOLE record back, so an agent that meant to edit
    only the guest name silently reverts every other field on the form.

Run:  ./run.sh   (or: python3 harness.py --stage sweep --out results.sweep.json)
"""

import argparse
import json
import random
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8799"
INPUT_RE = re.compile(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
STATUS_RE = re.compile(r"STATUS: (\w+)")


# ------------------------------------------------------------------ transport

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.read().decode()


def post(path, fields):
    data = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(BASE + path, data=data, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def read_room_screen(room="101", night="2026-08-20"):
    """What a vision agent gets: a rendered screen it has to scrape."""
    html = get(f"/screen/room?room={room}&night={night}")
    f = dict(INPUT_RE.findall(html))
    f["status"] = STATUS_RE.search(html).group(1)
    return f


def read_res_screen(rid):
    return dict(INPUT_RE.findall(get(f"/screen/res?id={rid}")))


def think(think_ms):
    """Vision model latency. Uniform jitter +/-25%, like a real decode."""
    t = think_ms / 1000.0 * random.uniform(0.75, 1.25)
    time.sleep(t)
    return t * 1000.0


# --------------------------------------------------------------------- agents

def acquire(resource, actor, ttl_ms, budget_ms=4000.0):
    """Wait for the booking-intent lease. Giving up is itself a measured cost."""
    deadline = time.perf_counter() + budget_ms / 1000.0
    while time.perf_counter() < deadline:
        code, body = post("/lease", {"resource": resource, "actor": actor,
                                     "ttl_ms": ttl_ms})
        if code == 200:
            return body["epoch"]
        time.sleep(random.uniform(0.005, 0.025))
    return None


def book_agent(actor, think_ms, mode, lease_ttl_ms, out, start_delay_ms=0.0,
               max_attempts=6):
    time.sleep(start_delay_ms / 1000.0)
    t0 = time.perf_counter()
    slept = 0.0
    epoch = None
    if mode == "lease":
        epoch = acquire("room:101:2026-08-20", actor, lease_ttl_ms)
        if epoch is None:
            out.append(dict(actor=actor, outcome="lease_denied", attempts=0,
                            total_ms=(time.perf_counter() - t0) * 1e3, slept_ms=slept))
            return
    outcome, attempts = "max_attempts", 0
    while attempts < max_attempts:
        attempts += 1
        screen = read_room_screen()
        slept += think(think_ms)
        if screen["status"] != "free":
            outcome = "declined_occupied"      # correct, informed refusal
            break
        code, body = post("/book", {"actor": actor, "guest": actor,
                                    "room": screen["room"], "night": screen["night"],
                                    "room_version": screen["room_version"],
                                    "nights": 1, "epoch": epoch or 0})
        if code == 200:
            outcome = "booked"
            break
        err = body.get("error")
        if err == "occupied":
            outcome = "declined_occupied"
            break
        if err in ("fenced", "lease_expired", "no_lease", "not_holder"):
            outcome = "fenced"
            break
        # stale_version: the screen moved under us. Re-read and re-decide.
    if mode == "lease" and epoch is not None:
        post("/release", {"resource": "room:101:2026-08-20", "actor": actor,
                          "epoch": epoch})
    out.append(dict(actor=actor, outcome=outcome, attempts=attempts,
                    total_ms=(time.perf_counter() - t0) * 1e3, slept_ms=slept))


def edit_agent(actor, rid, field, value, think_ms, mode, lease_ttl_ms, out,
               delay_ms=0.0, cancel=False, max_attempts=6):
    """Reads the reservation screen, thinks, posts the whole form back."""
    time.sleep(delay_ms / 1000.0)
    t0 = time.perf_counter()
    slept = 0.0
    epoch = None
    if mode == "lease":
        epoch = acquire(f"res:{rid}", actor, lease_ttl_ms)
        if epoch is None:
            out.append(dict(actor=actor, outcome="lease_denied", attempts=0, intent=None,
                            total_ms=(time.perf_counter() - t0) * 1e3, slept_ms=slept))
            return
    outcome, attempts, intent = "max_attempts", 0, None
    while attempts < max_attempts:
        attempts += 1
        s = read_res_screen(rid)
        slept += think(think_ms)
        if cancel:
            if s["status"] == "cancelled":
                outcome = "already_cancelled"
                break
            intent = ("status", "cancelled")
            code, body = post("/cancel", {"actor": actor, "res_id": rid,
                                          "res_version": s["res_version"],
                                          "epoch": epoch or 0})
        else:
            if s["status"] == "cancelled":
                outcome = "declined_cancelled"   # correct, informed refusal
                break
            form = {"guest": s["guest"], "nights": s["nights"], "status": s["status"]}
            form[field] = value                  # the one field it meant to change
            intent = (field, value)
            code, body = post("/modify", dict(actor=actor, res_id=rid,
                                              res_version=s["res_version"],
                                              epoch=epoch or 0, **form))
        if code == 200:
            outcome = "cancelled" if cancel else "modified"
            break
        err = body.get("error")
        if err in ("fenced", "lease_expired", "no_lease", "not_holder"):
            outcome, intent = "fenced", None
            break
        intent = None
    if mode == "lease" and epoch is not None:
        post("/release", {"resource": f"res:{rid}", "actor": actor, "epoch": epoch})
    out.append(dict(actor=actor, outcome=outcome, attempts=attempts, intent=intent,
                    total_ms=(time.perf_counter() - t0) * 1e3, slept_ms=slept))


def run_threads(targets):
    ts = [threading.Thread(target=f, args=a) for f, a in targets]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


# ---------------------------------------------------------------- invariants

def check(intents_by_actor, rid=None):
    """Returns (double_bookings, lost_intents) from the server's own ledger."""
    d = json.loads(get("/dump"))
    active = {}
    for r in d["reservations"]:
        if r["status"] == "active":
            active[(r["room"], r["night"])] = active.get((r["room"], r["night"]), 0) + 1
    double = sum(max(0, v - 1) for v in active.values())

    # replay acknowledged intents in ack order
    acked = [(a["seq"], intents_by_actor[a["actor"]])
             for a in d["acks"]
             if a["kind"] in ("modify", "cancel")
             and intents_by_actor.get(a["actor"])]
    final = {r["id"]: r for r in d["reservations"]}
    lost = 0
    if rid is not None and rid in final:
        for i, (_, (field, value)) in enumerate(acked):
            if any(f == field for (_, (f, _)) in acked[i + 1:]):
                continue                      # deliberately superseded
            if str(final[rid][field]) != str(value):
                lost += 1
    return double, lost


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))
    return xs[k]


def summarise(rows, trials, double, lost, **meta):
    over = [r["total_ms"] - r["slept_ms"] for r in rows]
    outcomes = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    return dict(meta, trials=trials, agents=len(rows),
                double_bookings=double, lost_intents=lost,
                defects=double + lost,
                double_per_100_agents=round(100.0 * double / max(1, len(rows)), 1),
                lost_per_100_trials=round(100.0 * lost / max(1, trials), 1),
                overhead_p50_ms=round(pct(over, 50), 1),
                overhead_p95_ms=round(pct(over, 95), 1),
                overhead_p99_ms=round(pct(over, 99), 1),
                mean_attempts=round(statistics.fmean(r["attempts"] for r in rows), 2),
                outcomes=outcomes)


# ----------------------------------------------------------------- scenarios

def reset(mode, seed_res=False):
    _, b = post("/reset", {"mode": mode, "seed_res": "1" if seed_res else "0"})
    return b.get("res_id")


def scenario_double_book(mode, n, think_ms, trials, lease_ttl_ms, stagger_ms=50.0):
    """n agents arrive stagger_ms apart and all target the same room-night.

    Whether agent i sees agent 0's booking depends only on whether agent 0 has
    finished thinking. think_ms is the independent variable; stagger_ms is the
    inter-arrival time that a fleet size implies."""
    rows, double, lost = [], 0, 0
    for t in range(trials):
        reset(mode)
        out = []
        run_threads([(book_agent, (f"a{t}_{i}", think_ms, mode, lease_ttl_ms, out,
                                   i * stagger_ms)) for i in range(n)])
        d, l = check({})
        double += d
        lost += l
        rows += out
    return summarise(rows, trials, double, lost, scenario="double_book", mode=mode,
                     concurrency=n, think_ms=think_ms, lease_ttl_ms=lease_ttl_ms,
                     stagger_ms=stagger_ms)


def _pair_scenario(name, mode, think_ms, trials, lease_ttl_ms, build, contend_ms=100.0):
    rows, double, lost = [], 0, 0
    for t in range(trials):
        rid = reset(mode, seed_res=True)
        out = []
        run_threads(build(t, rid, mode, think_ms, lease_ttl_ms, out, contend_ms))
        by_actor = {r["actor"]: r["intent"] for r in out if r.get("intent")}
        d, l = check(by_actor, rid)
        double += d
        lost += l
        rows += out
    return summarise(rows, trials, double, lost, scenario=name, mode=mode,
                     concurrency=2, think_ms=think_ms, lease_ttl_ms=lease_ttl_ms,
                     contend_ms=contend_ms)


def scenario_modify_modify(mode, think_ms, trials, lease_ttl_ms):
    def build(t, rid, mode, think_ms, ttl, out, contend):
        return [(edit_agent, (f"g{t}", rid, "guest", "bob", think_ms, mode, ttl, out)),
                (edit_agent, (f"n{t}", rid, "nights", "5", think_ms, mode, ttl, out,
                              contend))]
    return _pair_scenario("modify_vs_modify", mode, think_ms, trials, lease_ttl_ms, build)


def scenario_cancel_modify(mode, think_ms, trials, lease_ttl_ms):
    def build(t, rid, mode, think_ms, ttl, out, contend):
        return [(edit_agent, (f"c{t}", rid, "status", "cancelled", think_ms, mode,
                              ttl, out, 0.0, True)),
                (edit_agent, (f"m{t}", rid, "guest", "bob", think_ms, mode, ttl, out,
                              contend))]
    return _pair_scenario("cancel_vs_modify", mode, think_ms, trials, lease_ttl_ms, build)


def scenario_stale_read(mode, think_ms, trials, lease_ttl_ms):
    """Agent reads the screen; the front desk edits a different field mid-think;
    the agent posts its stale form and reverts the front desk."""
    def build(t, rid, mode, think_ms, ttl, out, contend):
        return [(edit_agent, (f"agent{t}", rid, "guest", "bob", think_ms, mode,
                              ttl, out)),
                (edit_agent, (f"desk{t}", rid, "nights", "9", 1.0, mode, ttl, out,
                              contend))]
    return _pair_scenario("stale_read_overwrite", mode, think_ms, trials,
                          lease_ttl_ms, build)


# ---------------------------------------------------------------------- main

MODES = ["legacy", "recheck", "occ", "lease"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["sweep", "anomalies"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--anomaly-trials", type=int, default=12)
    ap.add_argument("--concurrency", default="2,4,8,16")
    ap.add_argument("--think", default="50,200,800")
    ap.add_argument("--anomaly-think", default="25,100,400")
    ap.add_argument("--stagger", type=float, default=50.0)
    ap.add_argument("--contend", type=float, default=100.0)
    ap.add_argument("--lease-ttl", type=float, default=5000.0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)
    res = {"stage": a.stage, "config": vars(a),
           "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    if a.stage == "sweep":
        res["sweep"], res["lease_ttl"] = [], []
        for mode in MODES:
            for n in [int(x) for x in a.concurrency.split(",")]:
                for th in [int(x) for x in a.think.split(",")]:
                    r = scenario_double_book(mode, n, th, a.trials, a.lease_ttl,
                                             a.stagger)
                    res["sweep"].append(r)
                    print(f"[double_book] {mode:8s} n={n:2d} think={th:4d}ms "
                          f"stagger={a.stagger:.0f}ms  "
                          f"double={r['double_bookings']:3d} "
                          f"({r['double_per_100_agents']:5.1f}/100 agents)  "
                          f"p99_overhead={r['overhead_p99_ms']:8.1f}ms  "
                          f"attempts={r['mean_attempts']}", flush=True)
        # the losing axis for leases: the TTL must upper-bound think time.
        for ttl in (100.0, 400.0, 1600.0, 5000.0):
            r = scenario_double_book("lease", 4, 800, max(4, a.trials // 2), ttl,
                                     a.stagger)
            res["lease_ttl"].append(r)
            o = r["outcomes"]
            print(f"[lease_ttl] ttl={ttl:6.0f}ms think=800ms  "
                  f"double={r['double_bookings']} fenced={o.get('fenced',0)} "
                  f"denied={o.get('lease_denied',0)} booked={o.get('booked',0)}",
                  flush=True)
    else:
        res["anomalies"] = []
        for fn in (scenario_modify_modify, scenario_cancel_modify, scenario_stale_read):
            for mode in MODES:
                for th in [int(x) for x in a.anomaly_think.split(",")]:
                    r = fn(mode, th, a.anomaly_trials, a.lease_ttl)
                    res["anomalies"].append(r)
                    print(f"[{r['scenario']:22s}] {mode:8s} think={th:4d}ms  "
                          f"lost_intents={r['lost_intents']:3d}/{a.anomaly_trials} "
                          f"({r['lost_per_100_trials']:5.1f}%)  "
                          f"p99_overhead={r['overhead_p99_ms']:7.1f}ms", flush=True)

    res["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(a.out, "w") as f:
        json.dump(res, f, indent=1)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
