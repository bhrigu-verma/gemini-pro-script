#!/usr/bin/env python3
"""Long-run orchestrator for unattended Gemini loops.

Features:
- Starts one workflow process (research/job/legacy)
- Polls progress checkpoints and emits heartbeat metrics CSV
- Supports max runtime cutoff for controlled soak tests
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "long_run_output"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def collect_stats(workflow: str) -> dict[str, Any]:
    if workflow == "research":
        data = read_json(ROOT / "research_loop_output" / "research_history.json") or {}
        sources = data.get("sources", [])
        batches = data.get("batches", [])
        trends = data.get("trends", [])
        ideas = data.get("ideas", [])
        target = safe_int((data.get("config") or {}).get("target_sources"), 0)
        return {
            "round": len(batches),
            "accepted": len(sources),
            "target": target,
            "remaining": max(0, target - len(sources)) if target else 0,
            "trends": len(trends),
            "ideas": len(ideas),
        }

    if workflow == "job":
        data = read_json(ROOT / "job_completion_output" / "history.json") or {}
        rounds = data.get("rounds", [])
        records = data.get("records", [])
        target = safe_int((data.get("config") or {}).get("target"), 0)
        return {
            "round": len(rounds),
            "accepted": len(records),
            "target": target,
            "remaining": max(0, target - len(records)) if target else 0,
            "trends": 0,
            "ideas": 0,
        }

    # legacy main.py
    data_path = ROOT / "gemini_loop_output" / "history.json"
    rounds = []
    if data_path.exists():
        try:
            loaded = json.loads(data_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                rounds = loaded
        except Exception:
            pass
    return {
        "round": len(rounds),
        "accepted": len(rounds),
        "target": 0,
        "remaining": 0,
        "trends": 0,
        "ideas": 0,
    }


def build_command(args: argparse.Namespace) -> list[str]:
    python = sys.executable or "python3"

    if args.workflow == "research":
        cmd = [python, str(ROOT / "gemini_research_loop.py"), "--runtime-mode", args.runtime_mode]
    elif args.workflow == "job":
        cmd = [
            python,
            str(ROOT / "scripts" / "job_link_completion.py"),
            "--runtime-mode",
            args.runtime_mode,
            "--non-interactive",
        ]
    else:
        cmd = [python, str(ROOT / "main.py")]

    if args.extra_args:
        cmd.extend(args.extra_args)

    return cmd


def ensure_csv_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "timestamp_utc",
                "workflow",
                "runtime_mode",
                "pid",
                "round",
                "accepted",
                "target",
                "remaining",
                "trends",
                "ideas",
                "model_used",
                "status",
            ]
        )


def append_metric(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                row["timestamp_utc"],
                row["workflow"],
                row["runtime_mode"],
                row["pid"],
                row["round"],
                row["accepted"],
                row["target"],
                row["remaining"],
                row["trends"],
                row["ideas"],
                row["model_used"],
                row["status"],
            ]
        )


def terminate_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run and monitor long Gemini loops")
    parser.add_argument("--workflow", choices=["research", "job", "legacy"], default="research")
    parser.add_argument("--runtime-mode", choices=["ui", "api"], default="api")
    parser.add_argument("--api-key", default="", help="Optional API key; also reads GEMINI_API_KEY")
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--max-minutes", type=int, default=0, help="0 disables timeout")
    parser.add_argument(
        "--metrics-file",
        default=str(DEFAULT_OUT_DIR / "metrics.csv"),
        help="Heartbeat CSV output path",
    )
    parser.add_argument(
        "--extra-args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to underlying workflow script",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    env = os.environ.copy()
    api_key = args.api_key or env.get("GEMINI_API_KEY", "")
    if api_key:
        env["GEMINI_API_KEY"] = api_key

    cmd = build_command(args)
    metrics_path = Path(args.metrics_file)
    ensure_csv_header(metrics_path)

    print(f"[long-run] starting: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)

    start = time.time()
    timed_out = False

    try:
        while proc.poll() is None:
            stats = collect_stats(args.workflow)
            append_metric(
                metrics_path,
                {
                    "timestamp_utc": utc_now(),
                    "workflow": args.workflow,
                    "runtime_mode": args.runtime_mode,
                    "pid": proc.pid,
                    "round": stats["round"],
                    "accepted": stats["accepted"],
                    "target": stats["target"],
                    "remaining": stats["remaining"],
                    "trends": stats["trends"],
                    "ideas": stats["ideas"],
                    "model_used": "",
                    "status": "running",
                },
            )

            if args.max_minutes > 0 and (time.time() - start) > (args.max_minutes * 60):
                timed_out = True
                print("[long-run] max runtime reached; terminating workflow")
                terminate_process(proc)
                break

            time.sleep(max(5, args.poll_seconds))

        rc = proc.poll()
        if rc is None:
            rc = 0

        stats = collect_stats(args.workflow)
        append_metric(
            metrics_path,
            {
                "timestamp_utc": utc_now(),
                "workflow": args.workflow,
                "runtime_mode": args.runtime_mode,
                "pid": proc.pid,
                "round": stats["round"],
                "accepted": stats["accepted"],
                "target": stats["target"],
                "remaining": stats["remaining"],
                "trends": stats["trends"],
                "ideas": stats["ideas"],
                "model_used": "",
                "status": "timeout" if timed_out else f"exit_{rc}",
            },
        )

        print(f"[long-run] done: exit={rc}, metrics={metrics_path}")
        return rc
    except KeyboardInterrupt:
        print("[long-run] interrupted, terminating child process")
        terminate_process(proc)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
