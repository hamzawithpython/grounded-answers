"""
The names of the background tasks (ADR 0015, ADR 0019 decision D6).

A task name is a contract between the web app (which enqueues) and the worker (which runs). It is
defined here once. The worker's implementation of a task comes later (ingestion is Step 6); until
then, a job for that name waits in the queue and is not run.
"""

from typing import Final

INGEST_DOCUMENT: Final = "grounded.ingest_document"
