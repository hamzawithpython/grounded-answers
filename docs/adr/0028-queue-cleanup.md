# ADR 0028: Periodic cleanup of finished queue rows

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 8.

**Date:** 2026-10-24

**Deciders:** Hamza Asif

## Context

ADR 0015 (decision 5) asks for periodic deletion of finished jobs. Procrastinate 3.10.0 ships a built-in task, `remove_old_jobs` (installed source, `procrastinate/builtin_tasks.py`). It deletes finished jobs older than `max_hours`, with their linked events. By default it removes only successful jobs. Failed, cancelled and aborted jobs are kept unless the flags say otherwise. The docs note that the deletion is irreversible.

## Decision

1. Use the built-in `remove_old_jobs` task, on a daily periodic schedule, with `max_hours = 168` (7 days) and `remove_failed = False`. Failed jobs stay for diagnosis, so they accumulate. This is a known limit, revisited when Step 8 shows how many there are.
2. Use the Procrastinate **queueing lock** on this task, so a run is skipped while one is already waiting (the docs' cron pattern).
3. Confirm, with a test, that `worker_user` has exactly the grants that `delete_old_jobs` needs on the queue tables, and nothing more. Migration `0011` or later adds any missing grant, and that change is reviewed as a grant change.

## Alternatives

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. Built-in `remove_old_jobs` (chosen)** | Maintained by the library. Matches the library's own schema. | Failed jobs need a separate policy later. | Low |
| B. Our own SQL delete on the queue tables | Full control. | We maintain queue internals. | Medium |
| C. Keep finished rows forever | No work. | The table grows without limit. | Not acceptable |

## Consequences

- **Positive:** the queue table stays bounded for successful jobs.
- **Negative:** failed jobs accumulate until a policy is added. Deleted rows cannot be recovered.

## Tests (PR 8)

- The task deletes an old successful job and keeps a recent one.
- A failed job is kept.
- `worker_user` can run the cleanup and cannot read or write anything else in the queue beyond the grants the test lists.

## Related

- ADR 0015 (decision 5), ADR 0016 (queue grants).
