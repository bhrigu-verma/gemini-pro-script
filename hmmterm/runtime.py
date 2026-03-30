from __future__ import annotations

import importlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunConfig:
    debug_port: int = 9222
    agents: int = 4
    cycles: int = 30
    problems_per_cycle: int = 10
    output_dir: Path = Path("mainpp_output")
    agent_start_index: int = 1


def check_chrome_endpoint(debug_port: int) -> dict[str, Any]:
    url = f"http://127.0.0.1:{debug_port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "ok": True,
            "endpoint": url,
            "browser": payload.get("Browser", "unknown"),
            "protocol": payload.get("Protocol-Version", "unknown"),
            "web_socket": payload.get("webSocketDebuggerUrl", ""),
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "endpoint": url,
            "error": f"{type(exc).__name__}: {exc}",
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "endpoint": url,
            "error": f"{type(exc).__name__}: {exc}",
        }


def check_output_dir(output_dir: Path) -> dict[str, Any]:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".hmmterm_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"ok": True, "path": str(output_dir.resolve())}
    except Exception as exc:
        return {
            "ok": False,
            "path": str(output_dir),
            "error": f"{type(exc).__name__}: {exc}",
        }


def doctor_report(debug_port: int, output_dir: Path) -> dict[str, Any]:
    chrome = check_chrome_endpoint(debug_port)
    storage = check_output_dir(output_dir)

    try:
        importlib.import_module("mainpp")
        runtime_ok = True
        runtime_error = ""
    except Exception as exc:
        runtime_ok = False
        runtime_error = f"{type(exc).__name__}: {exc}"

    return {
        "chrome": chrome,
        "storage": storage,
        "runtime": {
            "ok": runtime_ok,
            "module": "mainpp",
            "error": runtime_error,
        },
        "ok": chrome.get("ok") and storage.get("ok") and runtime_ok,
    }


def run_generation(config: RunConfig) -> None:
    module = importlib.import_module("mainpp")
    runner_cls = getattr(module, "MainPPRunner", None)
    if runner_cls is None:
        raise RuntimeError("mainpp.MainPPRunner not found")

    runner = runner_cls(
        debug_port=config.debug_port,
        agents=max(1, config.agents),
        cycles=max(1, config.cycles),
        problems_per_cycle=max(1, config.problems_per_cycle),
        output_dir=config.output_dir,
        agent_start_index=max(1, config.agent_start_index),
    )
    runner.run()


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_history(output_dir: Path) -> dict[str, Any]:
    data = _load_json(output_dir / "history.json", {})
    return data if isinstance(data, dict) else {}


def load_problems(output_dir: Path) -> dict[str, Any]:
    data = _load_json(output_dir / "problems.json", {"items": []})
    return data if isinstance(data, dict) else {"items": []}


def summarize_progress(output_dir: Path) -> dict[str, Any]:
    history = load_history(output_dir)
    problems = load_problems(output_dir)

    cfg = history.get("config")
    if not isinstance(cfg, dict):
        cfg = problems.get("config") if isinstance(problems.get("config"), dict) else {}

    agents = int(cfg.get("agents", 0) or 0)
    cycles = int(cfg.get("cycles", 0) or 0)
    problems_per_cycle = int(cfg.get("problems_per_cycle", 0) or 0)

    expected_total = 0
    if agents > 0 and cycles > 0 and problems_per_cycle > 0:
        expected_total = agents * cycles * problems_per_cycle

    cycle_records = history.get("cycles")
    if not isinstance(cycle_records, list):
        cycle_records = []

    items = problems.get("items")
    if not isinstance(items, list):
        items = []

    last_cycle = None
    if cycle_records:
        last_cycle = cycle_records[-1].get("cycle")

    completion_pct = 0.0
    if expected_total > 0:
        completion_pct = min(100.0, (len(items) / expected_total) * 100.0)

    return {
        "output_dir": str(output_dir.resolve()),
        "agents": agents,
        "cycles": cycles,
        "problems_per_cycle": problems_per_cycle,
        "expected_total": expected_total,
        "completed_items": len(items),
        "completed_cycles": len(cycle_records),
        "last_cycle": last_cycle,
        "started_at": history.get("started_at") or problems.get("started_at"),
        "updated_at": problems.get("updated_at") or history.get("completed_at"),
        "completion_pct": round(completion_pct, 2),
        "run_completed": bool(history.get("completed_at")),
    }
