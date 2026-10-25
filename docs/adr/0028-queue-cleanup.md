# ADR 0028: Periodic cleanup of finished queue rows

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 8; amended in PR 8 (a narrow definer, not the built-in task).

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

## Amendment (PR 8, 2026-10-25): a narrow definer, not the built-in task

- **The built-in `remove_old_jobs` is not granted to the worker.** Verified in the installed 3.10.0 source (`procrastinate/manager.py`, `delete_old_jobs`, and `sql/queries.sql`): the delete needs DELETE on the whole job table, SELECT on every job column and on the event times, and it runs a delete trigger (`procrastinate_unlink_periodic_defers_v1`) that updates `procrastinate_periodic_defers` as the caller. Granting worker_user those would give it rights over every queue row, which is more than the cleanup needs.
- **Built instead:** `grounded.cleanup_finished_jobs(p_retention_hours)` (migration `0015_queue_cleanup`), owned by a new NOLOGIN role `queue_cleanup_definer`. It deletes **succeeded** jobs whose last event is older than the window (1 to 8760 hours, checked in SQL). worker_user gets EXECUTE on it and nothing else in the queue.
- **The window is 168 hours (7 days)**, and the cleanup runs daily at 03:00 UTC (`jobs/queue_cleanup.py`).
- **Failed, cancelled and aborted jobs are kept**, as decided above. Queued and running jobs are never removed. A job with no event at all is kept, because its age is unknown (fail safe).
- **Periodic links.** The definer holds UPDATE on `procrastinate_periodic_defers.job_id`, which the library's delete trigger needs. The link is cleared and the row is kept.

## Amendment (PR 9, 2026-10-25): the cleanup runs once a day on the worker's timer

The daily 03:00 UTC Procrastinate periodic task is replaced by the worker's timer, which runs `grounded.cleanup_finished_jobs` once every 24 hours, starting at worker start. The window (168 hours), the status rule (succeeded only), and the grants are unchanged. The clock time is not kept: a timer counts from the start of the worker, not from 03:00 UTC.
