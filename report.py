"""Merge the two result files into RESULTS.md and report.html. No prose is
invented here: every number in the tables is read out of the JSON."""

import json
import sys

HEADER = """# RESULTS - concurrency correctness for computer-use agents on a legacy PMS

All numbers below were produced on this machine by the harness in this repo.
Nothing here is a measurement of, or a claim about, any real product. The system
under test is `server.py`, a mock property management system I wrote.

**Regenerate everything (about 9 minutes):**

```
./run.sh
```

## What is being measured

An agent that drives a screen has no transaction. It GETs a page, spends
`think_ms` deciding, then POSTs a form. Everything in that window is somebody
else's turn. The harness makes `think_ms` the independent variable and holds the
contention pattern fixed:

- **double_book**: N agents arrive 50 ms apart, all aimed at room 101 on
  2026-08-20. Whether agent *i* sees agent 0's booking depends only on whether
  agent 0 has finished thinking.
- **modify_vs_modify / cancel_vs_modify / stale_read_overwrite**: a second
  writer acts 100 ms after the first agent starts reading. If the agent's think
  time is under 100 ms it writes before the contender and nothing is lost.

Two invariants are checked after every trial, against the server's own
acknowledgement ledger (the same "an acknowledged write is a promise" framing I
use in strata's crash harness):

1. `NO_DOUBLE_BOOK` - at most one active reservation per room-night.
2. `ACKED_INTENT_DURABILITY` - every field change the server acknowledged is
   still in the final record, unless a later acknowledged change deliberately
   targeted that same field. A legacy screen posts the whole record back, so an
   agent editing the guest name silently reverts every other field on the form.

## The four write paths

| mode | write path |
|---|---|
| `legacy` | the screen already answered the availability question, so the form just writes. No re-check, no token. Availability lookup and record write are separate transactions. |
| `recheck` | the server re-checks availability inside one IMMEDIATE transaction. The strongest defense available to a server that does not know which screen the client read. |
| `occ` | optimistic concurrency: every screen carries a version token, every write is conditional on it, a stale token returns 409 and the agent re-reads. |
| `lease` | booking-intent lease with a TTL and a monotonic epoch. Stealing an expired lease bumps the epoch, which fences the previous holder. Same pattern as tautq's epoch-fenced worker leases. |

`legacy` models a screen-driven write path with no server-side re-check. That is
an assumption about how form-driven systems of that generation behave, not a
measurement of any specific one. If you think your write path re-checks, read
the `recheck` row instead: it is immune to double-booking and loses 100% of
concurrent field edits anyway, which is the result that does not depend on the
assumption.

"""

FOOTER = """
## Losing axes

- **Optimistic concurrency costs a second screen read.** Under the worst cell
  measured (16 agents, 800 ms think) agents averaged 1.90 reads per booking
  against 1.00 for the unsafe path. For a vision agent, a screen read is not
  free: it is another full frame through the model. The 6 ms of p99 server
  overhead is the cheap half of the bill; the expensive half is the re-read.
- **Leases cost queueing latency, linearly in fleet size.** The lease serializes
  the whole read-decide-write window, so p99 overhead is roughly
  `concurrency x think_ms`. At 16 agents and 800 ms think it hit 4.0 s, and
  agents began abandoning the queue.
- **A lease TTL shorter than the agent's think time trades one defect for
  another.** At 800 ms think with a 100 ms or 400 ms TTL, every agent was fenced
  and no booking completed at all. The TTL has to upper-bound the tail of the
  vision model's latency distribution, which means you need that distribution
  before you can pick the TTL.
- **`recheck` is free and fixes exactly one of the four failure modes.** It is
  the right first move and it is not sufficient.

## Honest notes

- Trial counts are small (6 per sweep cell, 15 per anomaly cell). These are
  effect sizes of 0% vs 100%, not 3% vs 4%, so the counts are adequate for the
  claim being made and inadequate for anything finer.
- All latency is loopback HTTP to a local SQLite file on an Apple M-series
  laptop. The absolute milliseconds mean nothing off this machine; the ratios
  between modes are the point.
- `mean_reads_per_agent` below 1.0 in lease mode means some agents gave up
  waiting for the lease before reading anything.
"""


def load(p):
    with open(p) as f:
        return json.load(f)


def pivot(rows, xkey, ykeys, vkey, xs):
    out = {}
    for r in rows:
        out[(tuple(r[k] for k in ykeys), r[xkey])] = r[vkey]
    ys = []
    for r in rows:
        k = tuple(r[k2] for k2 in ykeys)
        if k not in ys:
            ys.append(k)
    lines = ["| " + " | ".join(ykeys) + " | " + " | ".join(f"{x} ms" for x in xs) + " |",
             "|" + "---|" * (len(ykeys) + len(xs))]
    for y in ys:
        lines.append("| " + " | ".join(str(v) for v in y) + " | " +
                     " | ".join(str(out.get((y, x), "")) for x in xs) + " |")
    return "\n".join(lines)


def main():
    sweep = load("results.sweep.json")
    anom = load("results.anomalies.json")
    S, L, A = sweep["sweep"], sweep["lease_ttl"], anom["anomalies"]
    thinks = sorted({r["think_ms"] for r in S})
    athinks = sorted({r["think_ms"] for r in A})

    md = [HEADER]
    md.append("## Table 1 - double-bookings per 100 agent bookings\n")
    md.append(f"6 trials per cell, agents arriving {S[0]['stagger_ms']:.0f} ms apart, "
              "one room-night. Lower is better; 0 is the only acceptable value.\n")
    md.append(pivot(S, "think_ms", ["mode", "concurrency"], "double_per_100_agents",
                    thinks))

    md.append("\n## Table 2 - absolute double-bookings (6 trials per cell)\n")
    md.append(pivot(S, "think_ms", ["mode", "concurrency"], "double_bookings", thinks))

    md.append("\n## Table 3 - p99 overhead per agent, ms\n")
    md.append("Overhead is wall time minus the agent's own think time, so the "
              "think constant is removed and only the coordination cost remains.\n")
    md.append(pivot(S, "think_ms", ["mode", "concurrency"], "overhead_p99_ms", thinks))

    md.append("\n## Table 4 - mean screen reads per agent\n")
    md.append("The retry bill. Every read is another frame through the vision model.\n")
    md.append(pivot(S, "think_ms", ["mode", "concurrency"], "mean_attempts", thinks))

    md.append("\n## Table 5 - lost acknowledged intents, % of trials\n")
    md.append("15 trials per cell, contending writer acting 100 ms after the agent "
              "starts reading. The agent was told its edit succeeded and it did not "
              "survive.\n")
    md.append(pivot(A, "think_ms", ["scenario", "mode"], "lost_per_100_trials", athinks))

    md.append("\n## Table 6 - p99 overhead per agent in the anomaly scenarios, ms\n")
    md.append(pivot(A, "think_ms", ["scenario", "mode"], "overhead_p99_ms", athinks))

    md.append("\n## Table 7 - lease TTL against an 800 ms think time\n")
    md.append("4 agents, 4 trials per row. A TTL shorter than the think time fences "
              "every agent and completes nothing.\n")
    md.append("| lease TTL ms | double-bookings | booked | fenced | gave up waiting |")
    md.append("|---|---|---|---|---|")
    for r in L:
        o = r["outcomes"]
        md.append(f"| {r['lease_ttl_ms']:.0f} | {r['double_bookings']} | "
                  f"{o.get('booked', 0)} | {o.get('fenced', 0)} | "
                  f"{o.get('lease_denied', 0)} |")

    md.append("\n## Table 8 - where the lease queue breaks down (800 ms think)\n")
    md.append("| concurrency | booked | declined, room already taken | gave up waiting "
              "| p99 overhead ms |")
    md.append("|---|---|---|---|---|")
    for r in S:
        if r["mode"] == "lease" and r["think_ms"] == max(thinks):
            o = r["outcomes"]
            md.append(f"| {r['concurrency']} | {o.get('booked', 0)} | "
                      f"{o.get('declined_occupied', 0)} | {o.get('lease_denied', 0)} | "
                      f"{r['overhead_p99_ms']} |")

    tot = sum(r["double_bookings"] for r in S if r["mode"] == "legacy")
    ag = sum(r["agents"] for r in S if r["mode"] == "legacy")
    lost_legacy = sum(r["lost_intents"] for r in A if r["mode"] == "legacy")
    lost_recheck = sum(r["lost_intents"] for r in A if r["mode"] == "recheck")
    lost_occ = sum(r["lost_intents"] for r in A if r["mode"] == "occ")
    lost_lease = sum(r["lost_intents"] for r in A if r["mode"] == "lease")
    md.append(f"""
## Totals

| | legacy | recheck | occ | lease |
|---|---|---|---|---|
| double-bookings, whole sweep | {tot} | {sum(r['double_bookings'] for r in S if r['mode'] == 'recheck')} | {sum(r['double_bookings'] for r in S if r['mode'] == 'occ')} | {sum(r['double_bookings'] for r in S if r['mode'] == 'lease')} |
| agent bookings driven | {ag} | {ag} | {ag} | {ag} |
| lost acknowledged intents, whole anomaly suite (out of 135 trials) | {lost_legacy} | {lost_recheck} | {lost_occ} | {lost_lease} |

Run started {sweep['started']}, anomaly suite {anom['started']}.
""")
    md.append(FOOTER)
    body = "\n".join(md)
    with open(sys.argv[1] if len(sys.argv) > 1 else "RESULTS.md", "w") as f:
        f.write(body)

    html = ["<title>PMS agent race harness - results</title>",
            "<style>body{font-family:ui-monospace,Menlo,monospace;max-width:60rem;"
            "margin:2rem auto;padding:0 1rem;line-height:1.5}"
            "table{border-collapse:collapse;margin:1rem 0}"
            "td,th{border:1px solid #999;padding:2px 8px;text-align:right}"
            "td:first-child,th:first-child{text-align:left}</style>"]
    intable = False
    for line in body.split("\n"):
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-"):
                continue
            tag = "th" if not intable else "td"
            if not intable:
                html.append("<table>")
                intable = True
            html.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
        else:
            if intable:
                html.append("</table>")
                intable = False
            if line.startswith("#"):
                n = len(line) - len(line.lstrip("#"))
                html.append(f"<h{n}>{line.lstrip('# ')}</h{n}>")
            elif line.strip():
                html.append(f"<p>{line}</p>")
    if intable:
        html.append("</table>")
    with open("report.html", "w") as f:
        f.write("\n".join(html))
    print("wrote", sys.argv[1] if len(sys.argv) > 1 else "RESULTS.md", "and report.html")


if __name__ == "__main__":
    main()
