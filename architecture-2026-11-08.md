# Architecture review, 2026-11-08

Reviewed revision: `2adc2f6`, the first revision where the whole service exists: T15–T24 are implemented, tested against fakes at 100% statement and branch coverage, mutation-tested and reviewed. Nothing has run against real YouTube yet (T25), and the `youtube-whisperer` orchestrator does not consume the API yet (DESIGN section 10).

This document holds proposals, not decisions. It is an ad-hoc document, not one of the project's governed documents, and per [AGENTS](AGENTS.md#document-boundaries) it authorizes nothing: each proposal takes effect only when the user records a decision in DESIGN and the work is planned as tasks in `tasks/open/`. Task [T29](tasks/open/T29.md) tracks its disposition.

## Question

The problem is small: one user, one consumer (the orchestrator), a few jobs a day, 10–20 files in staging, one YouTube connection. Which features play a minor role in the overall design but cost disproportionate complexity, and how should their trade-offs be judged?

## How to weigh the trade-offs

Separate three kinds of complexity.

- **Load-bearing.** It protects data, the user's account or correctness, and stays whatever the scale:
  - one YouTube thread (C2);
  - atomic `NEVER` publication through stage directories (5.4);
  - the staging marker before writes (C17);
  - idempotent submission (5.1);
  - failures visible until cleared (C13);
  - restart recovery (5.12).
- **Incidental.** General-purpose machinery whose value grows with scale and consumer count, neither of which this system has:
  - a globally ordered change feed with tombstone pruning, cursors and 410 resynchronization (5.10, C27, C38);
  - a status table that mirrors the staging filesystem (C8), which needs a reconciliation thread with stall handling (C25, C26);
  - cross-thread wake-ups for long polls.
- **Already paid.** The incidental machinery is built and well tested, so removing it now buys:
  - less to maintain and explain;
  - fewer failure modes;
  - above all, a smaller contract for the orchestrator, whose client code is not written yet.

  Every mechanism the collector exposes is one the orchestrator must implement correctly: persisting cursors, handling 410, resynchronizing from two snapshots, applying upserts. That consumer-side cost is the strongest argument, and it shrinks to nothing once the orchestrator is built against the current API.

Prefer, at this scale:

- per-entity state over global ordering;
- retention by time over retention by external state (the files in a share);
- polling the database over notification;
- one background actor over several.

## Size of the parts in question

Lines at `2adc2f6`, code / tests:

| Part | Code | Tests |
|---|---|---|
| `store.py` (whole module) | 1,077 | 970 |
| …of which the change feed, snapshots and job views (T23) | about 140 | about 150 |
| …of which removal, tombstones, pruning and key finalization | about 120 | about 200 |
| `reconcile.py` | 198 | 309 |
| `notify.py` | 105 | 215 |
| API code for the feed, the `GET /jobs` pass and health's staging age | about 60 | about 120 |

The application is about 3,200 lines of code; the parts above total about 620 of them, roughly a fifth, with a similar share of the tests.

## Proposals

### P1. Replace the global change feed with retained jobs and per-job long polls

- **Today:** a global revision counter orders every change to jobs, items and failures. Removals leave tombstones, which are pruned after 7 days with a durable pruned-through revision. Callers page `GET /jobs/changes` with cursors, get 410 below the pruned revision, and resynchronize from two snapshots carrying revisions (C38). Finished jobs disappear from `GET /jobs/{id}` once their files leave staging, so their final outcome survives only as a tombstone (C30). Idempotency keys store the job's final representation for replays after removal (C34).
- **Proposal:**
  - Finished jobs, with their items, outcomes and failures, stay readable through `GET /jobs/{id}` for a fixed retention, for example 7 days after they finish, and are then deleted by age.
  - Each job carries a version that any change to it or its items increments; `GET /jobs/{id}?since=<version>&wait=` waits on it.
  - An idempotency key maps to its job, which outlives it.
  - The orchestrator's whole protocol becomes: submit with a key; long-poll that job until it finishes; record its outcomes.
- **Removes:** global ordering, change pages, cursors, 410, two-snapshot resynchronization, tombstones as a separate concept, the pruned-through revision, stored final representations, and job views computed over items.
- **Keeps:** `GET /jobs` (recent and active jobs), `GET /failures` and the failure lifecycle for the user, and long polling (C5).
- **Loses:** one stream of every change. The orchestrator must track the jobs it submitted, which it already does to persist its idempotency keys, and it cannot discover jobs the user submitted by hand. If that matters, `GET /jobs?updated_since=<timestamp>` covers it without ordering guarantees.
- **Supersedes:** C27, C30, C34 and C38, and changes 5.10's contract.
- **Risk:** low; the store's per-record rows already hold the needed state. The work is mostly deletion plus a version column, with an API change before any consumer exists.

### P2. Re-read the database inside long polls instead of notifying

- **Today:** the worker and the reconciler call a `Notifier`, which wakes coroutines on the event loop through `call_soon_threadsafe`. Waiters take a ticket before reading, to avoid lost wake-ups. The T24 draft hung once because the notifier's deadline was given a frozen test clock while the wait ran on asyncio's clock.
- **Proposal:** a long poll re-reads its job, or the feed if P1 is rejected, every 0.5 s until something changed or the capped wait expires. One indexed read every 0.5 s per waiting request is negligible at this scale.
- **Removes:** `notify.py`, the notify callbacks threaded through the worker and the reconciler, the ticket-before-read ordering, and the class of lost-wake-up and clock-mixing bugs.
- **Costs:** up to 0.5 s more latency per change, and a little database load while callers wait.
- **Supersedes:** nothing. DESIGN 5.10's "an in-memory notification only wakes waiting requests" becomes an implementation choice rather than a requirement, and C5 still holds.
- **Risk:** very low and purely internal; it can be done independently of P1.

### P3. Decide whether the status table must mirror the staging filesystem (C8)

- **Today:** a record lives while any file it produced remains in staging. Enforcing that needs:
  - a reconciliation thread with periodic and requested passes;
  - the marker checked before and after each probe, because an unmounted share looks empty and would delete every record;
  - files that cannot be checked kept, not removed;
  - C25's guarantee that the API never touches the share, with a bounded wait in `GET /jobs` and a cached staging check with an age for health;
  - C26's removal rule.

  This is the largest source of failure-mode reasoning in the design.
- **Option A, drop the mirroring:**
  - Records are retained by time, as in P1, whatever happens to the files.
  - The worker keeps the marker check before writing (C17), which is load-bearing.
  - The orchestrator learns that a file moved when its transcription request fails, and the user sees the share directly.
  - **Removes:** `reconcile.py`, C25's stall machinery on the API path, C26 and the cached staging health. Health then reports the worker's last marker check.
  - **Supersedes:** C8, C25 and C26.
  - **Loses:** "what is still in staging" as an API view.
- **Option B, keep the mirroring but fold reconciliation into the worker thread:**
  - The worker already writes to the share, so a stall already blocks it, which DESIGN accepts (5.13).
  - A pass runs between units, and on request when the worker is idle.
  - **Removes:** a thread, a connection and the ticket machinery.
  - **Costs:** during a long download, records of moved files linger until the download ends, which is harmless.
  - **Supersedes:** only the part of 5.8 that calls for a separate thread.
- **Risk:** Option A is a product change and needs the user's judgment about the orchestrator's needs; Option B is an internal refactor.

### P4. Reconciler as a coroutine or a subprocess (answered, not recommended)

Considered and rejected.

- **Coroutine:** an asyncio coroutine does not escape the blocking filesystem calls of a stalled share. They would still have to run on a thread, and the single-flight guarantee would still be needed. What would get simpler is the waiting side: one pass as a shared `asyncio.Task` that `GET /jobs` awaits with a timeout would replace the condition variable and tickets.
- **Subprocess:** worse. A process blocked in uninterruptible sleep on a hung mount cannot be killed until the I/O returns, so isolation buys no cancellation. It would add inter-process communication, a second SQLite writer process, and lose in-process notification.

P3's options are the real levers.

## What not to change

- One YouTube thread with round-robin scheduling (C2, C9).
- Atomic, `NEVER`-policy publication.
- Existing-artifact lookup by ID, with caption pairing (C23, C33).
- The staging marker before writes (C17).
- Idempotency keys (5.1).
- Failure records with supersession (C13), and caption outcomes by job kind (5.7).
- SQLite on a named volume (C10).
- One connection per thread (C36).

Each is small relative to the risk it covers. The assumption that metadata-phase failures belong to the job's primary artifact ([T19](tasks/closed/T19.md)) is worth revisiting only if failures prove confusing in practice.

## Recommended sequence

1. **P1, before the orchestrator is written.** It is the largest simplification on both sides of the API, and it is cheapest now.
2. **P2, at any time.** It is internal, independent and low risk.
3. **P3.** Decide Option A or B once P1 has settled how long records live. If records are retained by time anyway, Option A becomes more natural.

Together, P1 to P3 (Option A) would remove `notify.py` and `reconcile.py`, about 260 lines of store code and most of the 410 and resynchronization contract: roughly a fifth of the application code, and more of its failure modes. The orchestrator's protocol would reduce to "submit, poll, read".

## Questions for the user

1. Does the orchestrator, or anything else, need a single stream of every change, including jobs submitted by hand? If not, P1 holds.
2. Is "what is still in staging" a view anyone uses through the API, or is looking at the share enough? This decides P3 between Option A and Option B.
3. Is up to half a second of extra long-poll latency acceptable? This decides P2.
4. Should the current API ship to the orchestrator first and be simplified later? This is cheaper now than after a consumer depends on it.
