"""Target-based job link completion loop (seed + Gemini top-up).

This script keeps requesting additional job links until target count is reached,
or safety limits are hit. It does NOT modify main.py and reuses its runtime primitives.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import random
import time
from pathlib import Path
from typing import Any, Optional

import main as core

from config.constants import (
    BROWSER_MODE_ATTACH,
    BROWSER_MODE_HEADLESS,
    DEBUG_PORT,
    GEMINI_FALLBACK_MODELS,
    GEMINI_PRIMARY_MODEL,
    GEMINI_URL,
)
from config.defaults import (
    DEFAULT_BROWSER_MODE,
    DEFAULT_BATCH_SIZE,
    DEFAULT_CHROME_USER_DATA_DIR,
    DEFAULT_GEMINI_PRIMARY_MODEL,
    DEFAULT_TARGET_COUNT,
    DEFAULT_RUNTIME_MODE,
    DEFAULT_INTER_ROUND_SLEEP_MAX,
    DEFAULT_INTER_ROUND_SLEEP_MIN,
    DEFAULT_CONTEXT_RESET_INTERVAL,
    MAX_EMPTY_STREAK,
    MAX_EXCLUDED_URLS_IN_PROMPT,
    MAX_ITERATIONS,
    MAX_REPAIR_ATTEMPTS,
)
from lib.gemini_api_client import GeminiAPIClient
from lib.browser import BrowserSession
from lib.gemini_client import GeminiClient
from lib.history import load_history, save_history
from lib.output_writers import write_csv, write_json, write_markdown_report
from lib.url_utils import dedupe_key, is_valid_http_url
from scripts.batch_processor import CompletionController, controller_from_history

OUTPUT_DIR = Path("job_completion_output")
URL_REGEX = re.compile(r"https?://[^\s\]\)\>\"']+", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _extract_json_value(text: str) -> Optional[Any]:
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    candidates.extend([block.strip() for block in fenced if block.strip()])

    first_arr = stripped.find("[")
    last_arr = stripped.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        candidates.append(stripped[first_arr : last_arr + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue

    return None


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


class JobLinkCompletionLoop:
    def __init__(self, output_dir: Path = OUTPUT_DIR) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        core.OUTPUT_DIR = self.output_dir
        core._LOG_PATH = self.output_dir / (
            "run_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
        )

        self.session: Optional[BrowserSession] = None
        self.driver = None
        self.collector: Optional[Any] = None
        self.seen: set[tuple[str, str]] = set()
        self._ui_round_counter = 0

        self.state: dict[str, Any] = {
            "started_at": _utc_now(),
            "config": {},
            "seed_rows": [],
            "rounds": [],
            "records": [],
        }

    def _state_path(self) -> Path:
        return self.output_dir / "history.json"

    def _save_state(self) -> None:
        save_history(self._state_path(), self.state)

    def _load_resume(self) -> bool:
        raw = load_history(self._state_path())
        if not isinstance(raw, dict):
            return False

        self.state = raw
        for row in self.state.get("records", []):
            self.seen.add(dedupe_key(str(row.get("title", "")), str(row.get("url", ""))))
        return True

    def _require_collector(self) -> Any:
        if self.collector is None:
            raise RuntimeError("Collector tab is not initialized")
        return self.collector

    def setup(self, args: argparse.Namespace) -> None:
        core.hr("JOB LINK COMPLETION LOOP", c="=")
        runtime_mode = args.runtime_mode

        if runtime_mode == "api":
            api_key = args.api_key or os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                raise RuntimeError("API mode requires --api-key or GEMINI_API_KEY env var")

            fallback_chain = [m.strip() for m in args.fallback_models.split(",") if m.strip()]
            self.collector = GeminiAPIClient(
                api_key=api_key,
                role_name="COLLECTOR_API",
                primary_model=args.primary_model,
                fallback_models=fallback_chain,
            )
            core.log(
                f"Collector initialized in API mode with primary={args.primary_model}",
                "OK",
            )
            return

        print(
            "\n  Needs:\n"
            "    * Chrome running with --remote-debugging-port=9222\n"
            "    * Logged into gemini.google.com in that profile\n"
        )
        input("  Press ENTER to connect ... ")

        self.session = BrowserSession(
            DEBUG_PORT,
            browser_mode=args.browser_mode,
            headless=args.headless,
            user_data_dir=args.user_data_dir,
            user_agent=args.user_agent,
        )
        self.driver = self.session.attach()

        core.log("Opening COLLECTOR tab ...")
        collector_handle = self.session.open_tab("COLLECTOR", url=GEMINI_URL)
        collector_tab = core.GeminiTab(self.driver, collector_handle, "COLLECTOR")
        collector_tab.focus()
        self.session.ensure_logged_in("COLLECTOR")
        self.collector = GeminiClient(collector_tab)

    def _load_seed_file(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            core.log(f"Seed file not found: {path}", "WARN")
            return []

        rows: list[dict[str, Any]] = []
        suffix = path.suffix.lower()

        if suffix in {".txt", ".md"}:
            for line in path.read_text(encoding="utf-8").splitlines():
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                rows.append({"title": "", "url": value, "source_method": "seed"})
            return rows

        if suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, str):
                        rows.append({"title": "", "url": item, "source_method": "seed"})
                    elif isinstance(item, dict):
                        rows.append(
                            {
                                "title": str(item.get("title", "")).strip(),
                                "url": str(item.get("url", "")).strip(),
                                "source_method": "seed",
                                "company": str(item.get("company", "")).strip(),
                                "location": str(item.get("location", "")).strip(),
                            }
                        )
            return rows

        if suffix == ".csv":
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for item in reader:
                    rows.append(
                        {
                            "title": str(item.get("title", "")).strip(),
                            "url": str(item.get("url", "")).strip(),
                            "source_method": "seed",
                            "company": str(item.get("company", "")).strip(),
                            "location": str(item.get("location", "")).strip(),
                        }
                    )
            return rows

        core.log(f"Unsupported seed file format: {path.suffix}", "WARN")
        return []

    def _accept_rows(self, rows: list[dict[str, Any]], source_method: str) -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []

        for row in rows:
            title = str(row.get("title", "")).strip()
            url = str(row.get("url", "")).strip()
            if not url or not is_valid_http_url(url):
                continue

            key = dedupe_key(title, url)
            if key in self.seen:
                continue

            record = {
                "id": len(self.state["records"]) + len(accepted) + 1,
                "title": title,
                "url": url,
                "company": str(row.get("company", "")).strip(),
                "location": str(row.get("location", "")).strip(),
                "source_method": source_method,
                "status": "accepted",
                "validation_note": "valid_url_unique",
                "added_at": _utc_now(),
            }
            accepted.append(record)
            self.seen.add(key)

        self.state["records"].extend(accepted)
        return accepted

    def _excluded_urls(self) -> list[str]:
        return [
            str(r.get("url", ""))
            for r in self.state.get("records", [])[-MAX_EXCLUDED_URLS_IN_PROMPT:]
            if r.get("url")
        ]

    def _build_collect_prompt(
        self,
        role: str,
        location: str,
        query: str,
        required_count: int,
    ) -> str:
        excluded_lines = "\n".join(f"- {u}" for u in self._excluded_urls())
        return (
            "You are a job sourcing assistant. Return ONLY strict JSON array.\n"
            f"Role: {role}\n"
            f"Location: {location}\n"
            f"Query focus: {query}\n"
            f"Need exactly {required_count} unique job posting links.\n"
            "Avoid duplicates and avoid any URL in EXCLUDED_URLS.\n"
            "Each object fields: title, url, company, location.\n"
            "No markdown. No explanation. Only JSON array.\n"
            "EXCLUDED_URLS:\n"
            + (excluded_lines if excluded_lines else "- none")
        )

    @staticmethod
    def _inter_round_sleep() -> None:
        lower = int(os.environ.get("INTER_ROUND_SLEEP_MIN", str(DEFAULT_INTER_ROUND_SLEEP_MIN)))
        upper = int(os.environ.get("INTER_ROUND_SLEEP_MAX", str(DEFAULT_INTER_ROUND_SLEEP_MAX)))
        lower = max(0, lower)
        upper = max(lower, upper)
        if upper <= 0:
            return
        time.sleep(random.randint(lower, upper))

    def _maybe_refresh_ui_collector(self, args: argparse.Namespace) -> None:
        if args.runtime_mode != "ui" or self.session is None or self.driver is None:
            return
        interval = int(os.environ.get("CONTEXT_RESET_INTERVAL", str(DEFAULT_CONTEXT_RESET_INTERVAL)))
        if interval <= 0:
            return
        if self._ui_round_counter == 0 or self._ui_round_counter % interval != 0:
            return

        core.log(f"Refreshing collector tab at round {self._ui_round_counter} to avoid context bloat", "INFO")
        collector_handle = self.session.open_tab("COLLECTOR", url=GEMINI_URL, wait_seconds=2.0)
        collector_tab = core.GeminiTab(self.driver, collector_handle, "COLLECTOR")
        collector_tab.focus()
        self.session.ensure_logged_in("COLLECTOR")
        self.collector = GeminiClient(collector_tab)

    def _repair_array(self, raw_text: str) -> list[dict[str, Any]]:
        parsed = _extract_json_value(raw_text)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]

        attempts = 0
        while attempts < MAX_REPAIR_ATTEMPTS:
            attempts += 1
            prompt = (
                "Convert the following content into a strict JSON array with objects containing "
                "title, url, company, location. Return only JSON array.\n\n"
                "CONTENT:\n" + raw_text
            )
            raw_text = self._require_collector().ask(prompt)
            parsed = _extract_json_value(raw_text)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]

        # Fallback: pull bare URLs from text.
        return [{"title": "", "url": u, "company": "", "location": ""} for u in URL_REGEX.findall(raw_text)]

    def run(self, args: argparse.Namespace) -> None:
        resumed = self._load_resume() if args.resume else False
        if resumed:
            core.log(f"Resume loaded. Existing accepted records: {len(self.state.get('records', []))}", "OK")

        self.setup(args)

        role = args.role or input("Role [AI Engineer]: ").strip() or "AI Engineer"
        location = args.location or input("Location [Remote]: ").strip() or "Remote"
        target = args.target if args.target > 0 else DEFAULT_TARGET_COUNT
        batch_size = args.batch_size if args.batch_size > 0 else DEFAULT_BATCH_SIZE
        query = args.query or input("Search angle [latest AI/ML roles]: ").strip() or "latest AI/ML roles"

        self.state["config"] = {
            "role": role,
            "location": location,
            "target": target,
            "batch_size": batch_size,
            "query": query,
            "max_iterations": args.max_iterations,
            "max_empty_streak": args.max_empty_streak,
        }

        # Seed ingest first.
        if args.seed_file:
            seed_rows = self._load_seed_file(Path(args.seed_file))
            self.state["seed_rows"] = seed_rows
            accepted_seed = self._accept_rows(seed_rows, "seed")
            core.log(f"Accepted from seed: {len(accepted_seed)}", "OK")

        controller, persisted = controller_from_history(
            self._state_path(),
            target,
            args.max_iterations,
            args.max_empty_streak,
        )
        if isinstance(persisted, dict) and args.resume:
            controller.accepted_total = len(self.state.get("records", []))

        while controller.should_continue():
            self._ui_round_counter += 1
            self._maybe_refresh_ui_collector(args)
            remaining = controller.remaining
            request_n = min(batch_size, remaining)
            prompt = self._build_collect_prompt(role, location, query, request_n)

            raw = self._require_collector().ask(prompt)
            rows = self._repair_array(raw)
            accepted = self._accept_rows(rows, "generated")
            accepted_count = len(accepted)

            controller.register_round(accepted_count)
            round_data = {
                "round": controller.iteration,
                "requested": request_n,
                "raw_rows": len(rows),
                "accepted": accepted_count,
                "total": controller.accepted_total,
                "remaining": controller.remaining,
                "query": query,
                "timestamp": _utc_now(),
            }
            self.state["rounds"].append(round_data)
            self._save_state()

            core.log(
                f"Round {controller.iteration}: raw={len(rows)} accepted={accepted_count} "
                f"total={controller.accepted_total}/{target} remaining={controller.remaining}",
                "OK",
                "COLLECTOR",
            )

            if controller.should_continue() and not args.non_interactive:
                print("\nENTER = continue | stop = finalize | query:<text> = change angle\n")
                action = input("  > ").strip()
                if action.lower() in {"stop", "quit", "q", "done", "exit"}:
                    break
                if action.lower().startswith("query:"):
                    query = action[6:].strip() or query

            if controller.should_continue():
                self._inter_round_sleep()

        summary = {
            "role": role,
            "location": location,
            "target": target,
            "accepted": len(self.state.get("records", [])),
            "rounds": len(self.state.get("rounds", [])),
            "stop_reason": controller.stop_reason,
            "completed_at": _utc_now(),
        }
        self.state["summary"] = summary
        self._save_state()

        records = self.state.get("records", [])
        write_json(self.output_dir / "final_report.json", {"summary": summary, "records": records})
        write_csv(
            self.output_dir / "records.csv",
            records,
            headers=[
                "id",
                "title",
                "url",
                "company",
                "location",
                "source_method",
                "status",
                "validation_note",
                "added_at",
            ],
        )
        write_markdown_report(self.output_dir / "report.md", "Job Link Completion Report", summary, records)

        core.hr("DONE", c="=")
        print(f"  Accepted links: {summary['accepted']}/{target}")
        print(f"  Rounds: {summary['rounds']}")
        print(f"  Stop reason: {summary['stop_reason']}")
        print(f"  Artifacts: {self.output_dir.resolve()}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect job links until target count is reached.")
    parser.add_argument("--role", default="", help="Role to search for")
    parser.add_argument("--location", default="", help="Location filter")
    parser.add_argument("--query", default="", help="Search angle")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET_COUNT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--seed-file", default="", help="Optional seed file (txt/csv/json)")
    parser.add_argument("--resume", action="store_true", help="Resume from existing history in output dir")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    parser.add_argument("--max-empty-streak", type=int, default=MAX_EMPTY_STREAK)
    parser.add_argument("--non-interactive", action="store_true", help="Run without per-round prompts")
    parser.add_argument(
        "--runtime-mode",
        choices=["ui", "api"],
        default=DEFAULT_RUNTIME_MODE,
        help="Select Gemini transport mode.",
    )
    parser.add_argument(
        "--browser-mode",
        choices=[BROWSER_MODE_ATTACH, BROWSER_MODE_HEADLESS],
        default=DEFAULT_BROWSER_MODE,
        help="Browser session mode when runtime-mode=ui.",
    )
    parser.add_argument("--headless", action="store_true", help="Launch Chrome in headless mode for UI runtime.")
    parser.add_argument("--user-data-dir", default=DEFAULT_CHROME_USER_DATA_DIR)
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--api-key", default="", help="Gemini API key when runtime-mode=api")
    parser.add_argument("--primary-model", default=DEFAULT_GEMINI_PRIMARY_MODEL)
    parser.add_argument(
        "--fallback-models",
        default=",".join([GEMINI_PRIMARY_MODEL, *GEMINI_FALLBACK_MODELS][1:]),
        help="Comma-separated fallback model names.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    loop = JobLinkCompletionLoop()
    try:
        loop.run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        core.log(f"Unhandled: {exc}", "ERR")
        raise


if __name__ == "__main__":
    main()
