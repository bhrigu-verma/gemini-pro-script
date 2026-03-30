"""
pipeline.py — Master Hinglish Math Data Pipeline (Selenium UI mode)
====================================================================
Replaces mainhh.py with:
  - Multi-Chrome parallelism   (one Chrome process per account/profile)
  - Per-Chrome token-bucket    (respects Gemini UI limits per account)
  - Thread-per-tab workers     pulling from a shared work queue
  - Thread-safe JSONL writer   (one line per problem, crash-safe)
  - Automatic resume           (skips already-completed problem keys)
  - Model selector             (switch to Thinking / Pro from config)
  - Clean Ctrl-C shutdown

Quickstart
----------
1.  Launch one Chrome per Google account (separate ports + profiles):

      /path/to/Google\ Chrome \\
          --remote-debugging-port=9222 \\
          --user-data-dir=/tmp/chrome-p1 \\
          --headless=new &

      /path/to/Google\ Chrome \\
          --remote-debugging-port=9223 \\
          --user-data-dir=/tmp/chrome-p2 \\
          --headless=new &

2.  Edit CHROME_INSTANCES below.

3.  python3 pipeline.py

Throughput math
---------------
  N chromes * T tabs * (60 / interval_sec) = req/min

  Example  3 chromes, 4 tabs, rate 12/min per chrome:
           3 * 4 * (12/4) = 36 req/min = 2160/hr
  Each req = 1 full problem solved (generate + solve).
  Add more Chrome entries to scale linearly (no code changes needed).
"""

from __future__ import annotations

import argparse
import datetime
import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import main as core  # your existing GeminiTab / attach_driver / open_tab primitives


# ---------------------------------------------------------------------------
# CONFIG  — edit this block
# ---------------------------------------------------------------------------

CHROME_INSTANCES = [
    # (debug_port, label)  — one entry per running Chrome / Google account
    (9222, "chrome-A"),
    (9223, "chrome-B"),
    # (9224, "chrome-C"),   # uncomment to add more
]

TABS_PER_CHROME        = 3     # concurrent agent tabs per Chrome instance
CYCLES                 = 30    # total generation cycles
PROBLEMS_PER_CYCLE     = 10    # problems generated per agent per cycle

# Requests per minute budget for ALL tabs inside one Chrome instance combined.
# Gemini free UI: 10-15 is safe.  Paid / higher tier: try 30+.
RATE_LIMIT_PER_MIN     = 12.0

# Seconds to wait between opening successive tabs (avoids burst on launch).
TAB_START_STAGGER_SEC  = 4

OUTPUT_DIR             = Path("pipeline_output")

# Model name substring as it appears in the Gemini UI model dropdown.
# "2.0 Flash Thinking"  — fastest thinking, ideal for math step tracing
# "2.5 Pro"             — deepest reasoning, slower
# "2.0 Flash"           — no thinking trace, fastest
# None                  — leave whatever model is already selected
GEMINI_MODEL           = "2.0 Flash Thinking"

GEMINI_URL             = "https://gemini.google.com/app"

SYSTEM_PROMPT = """You are a math teacher who teaches in Hinglish
(code-mixed Hindi-English). Generate a step-by-step solution
to math problems where:
- Mathematical notation, numbers, formulas stay in English
- Problem comprehension steps are in Hindi (Devanagari script)
- Intermediate reasoning can mix both
- Final answer is numeric only

For each step, also output a language tag: EN, HI, MIXED, MATH

Output ONLY valid JSON, no other text."""


# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def extract_json_value(text: str) -> Optional[Any]:
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    for chunk in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
        if chunk.strip():
            candidates.append(chunk.strip())
    s = text.strip()
    for lc, rc in [("[", "]"), ("{", "}")]:
        li, ri = s.find(lc), s.rfind(rc)
        if li != -1 and ri > li:
            candidates.append(s[li : ri + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except Exception:
            pass
    return None


def problem_key(cycle: int, agent: str, index: int) -> str:
    return f"{cycle}:{agent}:{index}"


# ---------------------------------------------------------------------------
# TOKEN BUCKET  (per-Chrome rate limiter)
# ---------------------------------------------------------------------------

class TokenBucket:
    """Thread-safe token bucket.  One token = one Gemini UI request."""

    def __init__(self, rate_per_min: float) -> None:
        self._rate   = rate_per_min / 60.0
        self._tokens = rate_per_min          # start full
        self._max    = rate_per_min
        self._lock   = threading.Lock()
        self._last   = time.monotonic()

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            with self._lock:
                now     = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._max, self._tokens + elapsed * self._rate)
                self._last   = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_for = (1.0 - self._tokens) / self._rate
            time.sleep(min(wait_for, 1.0))


# ---------------------------------------------------------------------------
# THREAD-SAFE JSONL WRITER
# ---------------------------------------------------------------------------

class JSONLWriter:
    """
    Background writer thread.  Workers call .write(dict) and never block on
    disk I/O.  One JSON line appended per problem — crash-safe.
    """

    def __init__(self, path: Path) -> None:
        self._path   = path
        self._q: queue.Queue[Optional[dict]] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="jsonl-writer"
        )
        self._thread.start()

    def write(self, item: dict) -> None:
        self._q.put(item)

    def flush_and_stop(self, timeout: float = 15.0) -> None:
        self._q.put(None)            # sentinel
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            while True:
                item = self._q.get()
                if item is None:
                    fh.flush()
                    return
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                fh.flush()


# ---------------------------------------------------------------------------
# WORK ITEM
# ---------------------------------------------------------------------------

@dataclass
class WorkItem:
    cycle: int
    agent: str
    index: int          # cycle-level index (used for key uniqueness)
    key:   str = field(init=False)

    def __post_init__(self) -> None:
        self.key = problem_key(self.cycle, self.agent, self.index)


# ---------------------------------------------------------------------------
# TAB WORKER  (one thread per GeminiTab)
# ---------------------------------------------------------------------------

class TabWorker(threading.Thread):
    """
    Pulls WorkItems from the shared queue, generates problems for that cycle,
    solves each one, and writes results via JSONLWriter.
    """

    def __init__(
        self,
        tab: core.GeminiTab,
        bucket: TokenBucket,
        work_q: "queue.Queue[WorkItem]",
        writer: JSONLWriter,
        done_keys: set,
        done_lock: threading.Lock,
        problems_per_cycle: int,
        stats: dict,
        stats_lock: threading.Lock,
    ) -> None:
        super().__init__(daemon=True, name=f"worker-{tab.name}")
        self.tab               = tab
        self.bucket            = bucket
        self.work_q            = work_q
        self.writer            = writer
        self.done_keys         = done_keys
        self.done_lock         = done_lock
        self.problems_per_cycle = problems_per_cycle
        self.stats             = stats
        self.stats_lock        = stats_lock
        self._stop             = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # -- internal helpers ---------------------------------------------------

    def _req(self, prompt: str) -> str:
        """Acquire rate-limit token, then send+recv."""
        self.bucket.acquire()
        self.tab.send(prompt)
        return self.tab.recv()

    def _repair_problems(self, raw: str) -> list[dict]:
        parsed = extract_json_value(raw)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        fixed = self._req(
            "Convert the following into a strict JSON array only. "
            'Each item: {"hinglish_problem": string, "gold_answer": number}.\n\n'
            "CONTENT:\n" + raw
        )
        parsed2 = extract_json_value(fixed)
        if isinstance(parsed2, list):
            return [r for r in parsed2 if isinstance(r, dict)]
        return []

    def _repair_solution(self, raw: str) -> dict:
        parsed = extract_json_value(raw)
        if isinstance(parsed, dict):
            return parsed
        fixed = self._req(
            "Convert the following into ONE strict JSON object only.\nCONTENT:\n" + raw
        )
        parsed2 = extract_json_value(fixed)
        if isinstance(parsed2, dict):
            return parsed2
        return {"raw_response": raw[:500], "parse_status": "failed"}

    def _gen_prompt(self, cycle: int) -> str:
        return (
            f"Generate exactly {self.problems_per_cycle} unique Hinglish math word problems "
            f"for cycle {cycle}, agent {self.tab.name}.\n"
            "Constraints:\n"
            "- Text is code-mixed Hindi-English\n"
            "- Topics: arithmetic, algebra, percentages, ratios, geometry\n"
            "- Every problem has a deterministic numeric answer\n"
            "Return ONLY valid JSON array (no markdown), schema per item:\n"
            '{"hinglish_problem": string, "gold_answer": number}'
        )

    def _solve_prompt(self, problem: str, gold: Any) -> str:
        return (
            SYSTEM_PROMPT
            + f"\n\nProblem: {problem}\n"
            + f"Known correct answer: {gold}\n"
            + "Generate step-by-step Hinglish solution leading to this answer."
        )

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        core.log("Worker started", "OK", self.tab.name)

        while not self._stop.is_set():
            # grab next work item
            try:
                item: WorkItem = self.work_q.get(timeout=3)
            except queue.Empty:
                continue

            # resume: skip if already done
            with self.done_lock:
                if item.key in self.done_keys:
                    self.work_q.task_done()
                    core.log(f"Skip (done): {item.key}", "INFO", self.tab.name)
                    continue

            # --- generate problem batch ---
            try:
                raw_gen  = self._req(self._gen_prompt(item.cycle))
                problems = self._repair_problems(raw_gen)
                problems = problems[: self.problems_per_cycle]
            except Exception as exc:
                core.log(f"Gen error cycle={item.cycle}: {exc}", "ERR", self.tab.name)
                self.work_q.task_done()
                self.work_q.put(item)   # requeue for retry
                time.sleep(8)
                continue

            core.log(
                f"cycle={item.cycle} generated {len(problems)} problems",
                "OK", self.tab.name,
            )

            # --- solve each problem ---
            for p_idx, p in enumerate(problems, start=1):
                hinglish = str(p.get("hinglish_problem", "")).strip()
                gold     = p.get("gold_answer", "")
                if not hinglish:
                    continue

                try:
                    raw_sol  = self._req(self._solve_prompt(hinglish, gold))
                    solution = self._repair_solution(raw_sol)
                except Exception as exc:
                    core.log(f"Solve error p={p_idx}: {exc}", "ERR", self.tab.name)
                    solution = {"parse_status": "error", "error": str(exc)}

                record = {
                    "key":              item.key,
                    "cycle":            item.cycle,
                    "agent":            self.tab.name,
                    "index":            p_idx,
                    "hinglish_problem": hinglish,
                    "gold_answer":      gold,
                    "solution":         solution,
                    "generated_at":     utc_now(),
                }
                self.writer.write(record)

                with self.done_lock:
                    self.done_keys.add(item.key)

                with self.stats_lock:
                    self.stats["solved"] += 1

                core.log(
                    f"cycle={item.cycle} p={p_idx}/{len(problems)} ✓",
                    "OK", self.tab.name,
                )

            self.work_q.task_done()

        core.log("Worker stopped", "INFO", self.tab.name)


# ---------------------------------------------------------------------------
# MODEL SELECTOR  (best-effort, non-fatal if UI changes)
# ---------------------------------------------------------------------------

def try_select_model(driver: Any, handle: str, model_name: Optional[str]) -> None:
    if not model_name:
        return
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        driver.switch_to.window(handle)
        wait = WebDriverWait(driver, 10)

        selectors = [
            "//button[contains(@aria-label,'model')]",
            "//button[contains(@aria-label,'Model')]",
            "//*[contains(@class,'model-selector')]",
            "//*[contains(@data-test-id,'model')]",
        ]
        btn = None
        for sel in selectors:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, sel)))
                break
            except Exception:
                pass

        if btn is None:
            core.log("Model selector not found — skipping", "WARN")
            return

        btn.click()
        time.sleep(0.8)

        opts = driver.find_elements(By.XPATH, f"//*[contains(text(),'{model_name}')]")
        for opt in opts:
            if opt.is_displayed():
                opt.click()
                core.log(f"Model set to: {model_name}", "OK")
                time.sleep(1.0)
                return

        core.log(f"Model option '{model_name}' not found in dropdown", "WARN")
    except Exception as exc:
        core.log(f"Model select skipped: {exc}", "WARN")


# ---------------------------------------------------------------------------
# CHROME WORKER POOL
# ---------------------------------------------------------------------------

class ChromeWorkerPool:
    """
    Manages one Chrome instance: attaches the Selenium driver, opens T tabs,
    starts one TabWorker thread per tab.
    """

    def __init__(
        self,
        port: int,
        label: str,
        tabs_per_chrome: int,
        rate_per_min: float,
        work_q: queue.Queue,
        writer: JSONLWriter,
        done_keys: set,
        done_lock: threading.Lock,
        problems_per_cycle: int,
        stats: dict,
        stats_lock: threading.Lock,
    ) -> None:
        self.port            = port
        self.label           = label
        self.tabs_per_chrome = tabs_per_chrome
        self.bucket          = TokenBucket(rate_per_min)
        self.work_q          = work_q
        self.writer          = writer
        self.done_keys       = done_keys
        self.done_lock       = done_lock
        self.problems_per_cycle = problems_per_cycle
        self.stats           = stats
        self.stats_lock      = stats_lock

        self.driver  = None
        self.workers: list[TabWorker] = []

    def start(self) -> None:
        core.log(f"Attaching to Chrome :{self.port}", "INFO", self.label)
        self.driver = core.attach_driver(self.port)

        for i in range(self.tabs_per_chrome):
            agent_name = f"{self.label}-TAB{i + 1}"
            handle     = core.open_tab(self.driver, GEMINI_URL, agent_name)
            time.sleep(TAB_START_STAGGER_SEC)

            if i == 0:
                try_select_model(self.driver, handle, GEMINI_MODEL)

            tab = core.GeminiTab(self.driver, handle, agent_name)
            tab.probe()

            worker = TabWorker(
                tab=tab,
                bucket=self.bucket,
                work_q=self.work_q,
                writer=self.writer,
                done_keys=self.done_keys,
                done_lock=self.done_lock,
                problems_per_cycle=self.problems_per_cycle,
                stats=self.stats,
                stats_lock=self.stats_lock,
            )
            self.workers.append(worker)
            worker.start()
            core.log(f"Tab worker started", "OK", agent_name)

    def stop(self) -> None:
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.join(timeout=12)


# ---------------------------------------------------------------------------
# ORCHESTRATOR
# ---------------------------------------------------------------------------

class PipelineOrchestrator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args       = args
        self.output_dir = Path(args.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.jsonl_path = self.output_dir / "problems.jsonl"
        self.log_path   = self.output_dir / (
            "run_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
        )

        core.OUTPUT_DIR = self.output_dir
        core._LOG_PATH  = self.log_path

        # resume state
        self.done_keys: set[str] = set()
        self.done_lock            = threading.Lock()
        self._load_done_keys()

        self.work_q    : queue.Queue[WorkItem] = queue.Queue()
        self.writer     = JSONLWriter(self.jsonl_path)
        self.stats      = {"solved": 0, "started_at": utc_now()}
        self.stats_lock = threading.Lock()
        self.pools: list[ChromeWorkerPool] = []

    def _load_done_keys(self) -> None:
        if not self.jsonl_path.exists():
            return
        count = 0
        with self.jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if "key" in rec:
                        self.done_keys.add(rec["key"])
                        count += 1
                except Exception:
                    pass
        if count:
            core.log(f"Resume: {count} items already done", "INFO")

    def _build_agent_names(self) -> list[str]:
        names = []
        for _port, label in CHROME_INSTANCES:
            for i in range(self.args.tabs_per_chrome):
                names.append(f"{label}-TAB{i + 1}")
        return names

    def _enqueue_work(self) -> None:
        agent_names = self._build_agent_names()
        total = 0
        for cycle in range(1, self.args.cycles + 1):
            for agent in agent_names:
                item = WorkItem(cycle=cycle, agent=agent, index=cycle)
                if item.key not in self.done_keys:
                    self.work_q.put(item)
                    total += 1
        core.log(f"Enqueued {total} work items", "INFO")

    def run(self) -> None:
        core.hr("PIPELINE START", c="=")
        total_agents = len(CHROME_INSTANCES) * self.args.tabs_per_chrome
        core.log(
            f"{len(CHROME_INSTANCES)} chromes x {self.args.tabs_per_chrome} tabs "
            f"= {total_agents} agents | "
            f"{self.args.cycles} cycles | "
            f"{self.args.problems_per_cycle} problems/cycle | "
            f"rate {self.args.rate_per_min}/min per chrome",
            "INFO",
        )

        self._enqueue_work()

        for port, label in CHROME_INSTANCES:
            pool = ChromeWorkerPool(
                port=port,
                label=label,
                tabs_per_chrome=self.args.tabs_per_chrome,
                rate_per_min=self.args.rate_per_min,
                work_q=self.work_q,
                writer=self.writer,
                done_keys=self.done_keys,
                done_lock=self.done_lock,
                problems_per_cycle=self.args.problems_per_cycle,
                stats=self.stats,
                stats_lock=self.stats_lock,
            )
            pool.start()
            self.pools.append(pool)

        try:
            while not self.work_q.empty():
                with self.stats_lock:
                    solved = self.stats["solved"]
                core.log(
                    f"Progress — solved: {solved} | queue: {self.work_q.qsize()} remaining",
                    "INFO",
                )
                time.sleep(20)
        except KeyboardInterrupt:
            core.log("Ctrl-C received — shutting down", "WARN")

        for pool in self.pools:
            pool.stop()

        self.writer.flush_and_stop()
        self._write_summary()

        core.hr("PIPELINE DONE", c="=")
        core.log(f"Total solved: {self.stats['solved']}", "OK")
        core.log(f"Output: {self.output_dir.resolve()}", "OK")

    def _write_summary(self) -> None:
        summary = {
            "started_at":        self.stats["started_at"],
            "completed_at":      utc_now(),
            "chromes":           len(CHROME_INSTANCES),
            "tabs_per_chrome":   self.args.tabs_per_chrome,
            "total_agents":      len(CHROME_INSTANCES) * self.args.tabs_per_chrome,
            "cycles":            self.args.cycles,
            "problems_per_cycle": self.args.problems_per_cycle,
            "rate_per_min_per_chrome": self.args.rate_per_min,
            "total_solved":      self.stats["solved"],
        }
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hinglish Math Pipeline — multi-Chrome Selenium")
    p.add_argument("--tabs-per-chrome",    type=int,   default=TABS_PER_CHROME)
    p.add_argument("--cycles",             type=int,   default=CYCLES)
    p.add_argument("--problems-per-cycle", type=int,   default=PROBLEMS_PER_CYCLE)
    p.add_argument("--rate-per-min",       type=float, default=RATE_LIMIT_PER_MIN)
    p.add_argument("--output-dir",         default=str(OUTPUT_DIR))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    orch = PipelineOrchestrator(args)
    try:
        orch.run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        core.log(f"Unhandled: {exc}", "ERR")
        raise


if __name__ == "__main__":
    main()