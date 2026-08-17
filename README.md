# pms-race - a concurrency harness for computer-use agents on a legacy PMS

A computer-use agent driving a screen has no transaction. It reads availability,
spends a second deciding, then clicks. Between the read and the click, the world
can change: another agent, the front desk, an OTA, a phone call.

This is a mock property management system with a real SQLite backing store, plus
a harness that drives N concurrent agents through its booking screens with an
injected delay between the read and the write. It measures four failure modes,
then implements two mitigations and measures what they cost.

Nothing here touches or describes any real product. Everything is local.

---

## The short version

**What I noticed.** Lance runs computer-use agents against legacy hotel PMS software. An
agent driving a UI has no transaction: it reads a screen, thinks, then clicks. Between the
read and the click, the world can change. Nobody publishes what that costs at scale.

**The finding that matters.** The defect rate is proportional to how long the agent thinks,
and thinking is the entire job. At 16 concurrent agents competing for one room-night:

| agent think time | double-bookings per 100 agents |
|---|---:|
| 50 ms | 2.1 |
| 200 ms | 18.8 |
| 800 ms | **90.6** |

A fast script barely trips this. A vision model deliberating over a screenshot trips it
almost every time. The race window is not incidental to computer-use agents, it is
proportional to the reasoning that makes them useful.

**The trap.** The obvious fix works on the obvious bug and hides a worse one:

| approach | double-bookings | lost acknowledged edits | p99 overhead |
|---|---:|---:|---:|
| trust the screen | 229 | 65 of 135 | 21 ms |
| re-check server-side | **0** | **62 of 135** | 21 ms |
| version token from the screen | 0 | 0 | **25 ms** |
| booking-intent lease | 0 | 0 | **4,027 ms** |

**Re-checking availability on the server eliminates double-booking entirely and still loses
62 acknowledged edits out of 135 trials.** The system tells the agent the change was saved
and the change is not there. That failure is invisible to anyone testing for double-bookings,
which is the test everyone writes first.

**What actually works.** Reading a version token off the screen and rejecting a stale write
takes all four failure modes to zero for **4 ms of p99 overhead**. The lease also reaches
zero but costs **160x that**, and it fences every agent once its TTL drops below think time,
which is exactly when a vision model needs it most.

**What it is not.** A mock PMS I wrote, not Lance's. It measures the class of failure a
screen-driven agent is exposed to, and says nothing about how Lance handles it, because
nothing about that is public.

## Run it

```
./run.sh                     # about 9 minutes, writes ../RESULTS.md + report.html
```

or piecewise:

```
python3 server.py 8799 &
python3 harness.py --stage sweep     --trials 6          --out results.sweep.json
python3 harness.py --stage anomalies --anomaly-trials 15 --out results.anomalies.json
python3 report.py ../RESULTS.md
```

Stdlib Python 3 only. No dependencies, no network, no downloads. Verified on
Python 3.14.6, macOS arm64.

Look at a screen the way an agent does:

```
curl 'http://127.0.0.1:8799/screen/room?room=101&night=2026-08-20'
```

## Files

| file | what it is |
|---|---|
| `server.py` | the mock PMS. SQLite store, HTML form screens, four write paths (`legacy`, `recheck`, `occ`, `lease`), and an acknowledgement ledger. |
| `harness.py` | the agents and the scenarios. Agents scrape the HTML screen with a regex, sleep for `think_ms`, then POST the form back. |
| `report.py` | merges the two result files into `../RESULTS.md` and `report.html`. |
| `results.*.json` | raw output of the run that produced `../RESULTS.md`. |

## The four failure modes

| scenario | the race |
|---|---|
| `double_book` | N agents read "room 101 is free" inside each other's think windows and all of them book it. |
| `modify_vs_modify` | two agents edit different fields of one reservation. The legacy form posts the whole record, so the second write reverts the first field. |
| `cancel_vs_modify` | one agent cancels, another was mid-think on the modify screen and posts `status=active` back from its stale form. The cancellation the guest was promised is undone. |
| `stale_read_overwrite` | the front desk changes the record while the agent is thinking. The agent posts its stale form and reverts the human. |

## The two invariants

1. `NO_DOUBLE_BOOK` - at most one active reservation per room-night.
2. `ACKED_INTENT_DURABILITY` - every field change the server acknowledged is
   present in the final record, unless a later acknowledged change deliberately
   targeted that same field.

Both are checked against the server's own ack ledger after every trial, which is
the same contract I use in strata's crash harness: an acknowledged write is a
promise, and the test is whether the promise survived.

## The two mitigations

- **`occ`** - the screen carries a version token, every write is conditional on
  it, a stale token returns 409 and the agent re-reads and re-decides. This is
  the one to reach for first: it is a hidden field and a `WHERE version = ?`.
- **`lease`** - a booking-intent lease with a TTL and a monotonic epoch, held
  across the whole read-decide-write window. Stealing an expired lease bumps the
  epoch, which fences the old holder's write. Same shape as tautq's epoch-fenced
  worker leases.

Both take all four failure modes to zero. `RESULTS.md` publishes what each one
costs, including the cell where the lease design is worse than doing nothing.

## Modelling assumption, stated plainly

`legacy` mode assumes the write path does not re-check availability, on the
theory that the availability screen already answered that question. If that
assumption does not hold for a given system, `recheck` mode is the honest
comparison: it eliminates double-booking entirely and still loses concurrent
field edits at the same rate, because no amount of server-side re-checking tells
the server which version of the screen the client was looking at.
