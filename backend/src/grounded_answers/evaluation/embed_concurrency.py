"""
Measure query embedding in the web process at 1, 2 and 4 at a time (ADR 0033, decision 7; docs/EVAL.md).

One measurement is one fresh process, so the memory figures are clean: it loads the real pinned model through the same
`QueryEmbedder` the web app uses, embeds the evaluation questions with `clients` callers at once against a limit of
`limit` slots, and reports latency per call (the wait for a slot included), throughput, how long the event loop was
blocked at most, and the working set before the load, after it, and at its peak.

    uv run python -m grounded_answers.evaluation.embed_concurrency MODEL_DIR            # the whole matrix
    uv run python -m grounded_answers.evaluation.embed_concurrency MODEL_DIR --one 2 4  # limit 2, 4 callers (a child)

The matrix runs each case in a child process, one after another. Question texts are read from the demo file and are
never printed: the report has numbers only. Peak memory is the operating system's own counter for the process
(Windows: PeakWorkingSetSize; elsewhere: the maximum resident set size).
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final

from grounded_answers.embedding.model import load_embedder
from grounded_answers.embedding.query import READY, QueryEmbedder
from grounded_answers.evaluation.questions import load_questions
from grounded_answers.runtime import selector_loop_factory

QUESTIONS_FILE: Final = (
    Path(__file__).resolve().parents[4] / "demo-data" / "indus-freight" / "eval-questions.json"
)
TOTAL_QUERIES: Final = 200
WARM_UP: Final = 3
MIB: Final = 1024 * 1024

# (limit, callers): the pure scaling cases, then four callers against a smaller limit (queueing).
MATRIX: Final = ((1, 1), (2, 2), (4, 4), (1, 4), (2, 4))


def memory_mib() -> dict[str, float]:
    """Working set now and its peak for this process, in MiB, from the operating system's counters."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = (
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        )
        ok = psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not ok:
            raise OSError("GetProcessMemoryInfo failed")
        return {
            "working_set": counters.WorkingSetSize / MIB,
            "peak_working_set": counters.PeakWorkingSetSize / MIB,
        }
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mib = peak / MIB if sys.platform == "darwin" else peak / 1024
    current = 0.0
    statm = Path("/proc/self/statm")
    if statm.exists():
        import os

        current = int(statm.read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / MIB
    return {"working_set": current or peak_mib, "peak_working_set": peak_mib}


def free_memory_mib() -> float | None:
    """Free physical memory of the machine now, in MiB (Windows only; None elsewhere)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Status(ctypes.Structure):
        _fields_ = (
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    status = Status()
    status.dwLength = ctypes.sizeof(Status)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return float(status.ullAvailPhys) / MIB


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


async def _lag_monitor(stop: asyncio.Event, worst: list[float]) -> None:
    """The longest time a 5 ms sleep was late: how long the event loop was blocked."""
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(0.005)
        worst[0] = max(worst[0], (time.perf_counter() - started - 0.005) * 1000)


async def measure_one(model_dir: Path, limit: int, clients: int) -> dict[str, Any]:
    questions = [q.text for q in load_questions(QUESTIONS_FILE)]
    before = memory_mib()
    free_before = free_memory_mib()

    loaded = time.perf_counter()
    embedder = QueryEmbedder(lambda: load_embedder(model_dir, process="web"), concurrency=limit)
    embedder.start()
    while embedder.state != READY:
        if embedder.state == "failed":
            raise SystemExit("the model did not load (see the log line above)")
        await asyncio.sleep(0.05)
    load_seconds = time.perf_counter() - loaded
    after_load = memory_mib()

    for i in range(WARM_UP):
        await embedder.embed(questions[i % len(questions)])

    rounds = max(1, TOTAL_QUERIES // clients)
    latencies: list[float] = []
    stop = asyncio.Event()
    worst_lag = [0.0]
    monitor = asyncio.create_task(_lag_monitor(stop, worst_lag))

    async def client(index: int) -> None:
        for step in range(rounds):
            text = questions[(index * 7 + step) % len(questions)]
            started = time.perf_counter()
            await embedder.embed(text)
            latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(client(i) for i in range(clients)))
    elapsed = time.perf_counter() - started
    stop.set()
    await monitor
    after_run = memory_mib()
    free_after = free_memory_mib()
    await embedder.aclose()

    return {
        "limit": limit,
        "clients": clients,
        "queries": len(latencies),
        "load_seconds": round(load_seconds, 1),
        "latency_ms_p50": round(statistics.median(latencies), 1),
        "latency_ms_p95": round(percentile(latencies, 95), 1),
        "latency_ms_max": round(max(latencies), 1),
        "throughput_per_second": round(len(latencies) / elapsed, 1),
        "event_loop_lag_ms_max": round(worst_lag[0], 1),
        "working_set_mib_before_load": round(before["working_set"]),
        "working_set_mib_after_load": round(after_load["working_set"]),
        "peak_working_set_mib_after_load": round(after_load["peak_working_set"]),
        "working_set_mib_after_run": round(after_run["working_set"]),
        "peak_working_set_mib_final": round(after_run["peak_working_set"]),
        "free_physical_mib_before": None if free_before is None else round(free_before),
        "free_physical_mib_after": None if free_after is None else round(free_after),
    }


def run_matrix(model_dir: Path) -> list[dict[str, Any]]:
    """Each case in its own child process, one at a time, so memory figures never include an earlier case."""
    results: list[dict[str, Any]] = []
    for limit, clients in MATRIX:
        completed = subprocess.run(  # noqa: S603 - our own module, fixed arguments
            [
                sys.executable,
                "-m",
                "grounded_answers.evaluation.embed_concurrency",
                str(model_dir),
                "--one",
                str(limit),
                str(clients),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(
                f"case limit={limit} clients={clients} failed:\n{completed.stderr[-2000:]}"
            )
        results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
    return results


def print_table(results: list[dict[str, Any]]) -> None:
    print(
        "| Limit | Callers | p50 ms | p95 ms | max ms | per second | loop lag ms | MiB after load | MiB peak |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['limit']} | {r['clients']} | {r['latency_ms_p50']} | {r['latency_ms_p95']} | "
            f"{r['latency_ms_max']} | {r['throughput_per_second']} | {r['event_loop_lag_ms_max']} | "
            f"{r['working_set_mib_after_run']} | {r['peak_working_set_mib_final']} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--one", nargs=2, type=int, metavar=("LIMIT", "CALLERS"))
    parser.add_argument("--report", type=Path, help="write the matrix as JSON (numbers only)")
    args = parser.parse_args()
    if args.one:
        limit, clients = args.one
        with asyncio.Runner(loop_factory=selector_loop_factory) as runner:
            print(json.dumps(runner.run(measure_one(args.model_dir, limit, clients))))
        return
    results = run_matrix(args.model_dir)
    print_table(results)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
