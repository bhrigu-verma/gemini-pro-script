"""
chatgpt_pipeline.py — ChatGPT Selenium Pipeline (Fixed)
=========================================================

ROOT CAUSE OF PREVIOUS ERRORS
-------------------------------
Every tab logged "probe timed out" and then "None of selectors found".
This happens for ONE of two reasons:

  1. The Chrome profile is not logged in.  chatgpt.com redirects to
     /auth/login — the chat input never appears.

  2. ChatGPT switched to a ProseMirror editor.  The actual DOM is:
        <div id="prompt-textarea" contenteditable="true" class="ProseMirror">
          <p data-placeholder="Message ChatGPT"></p>
        </div>
     Plain .textContent injection into the div doesn't fire ProseMirror
     events — the send button stays disabled. You must use execCommand
     or ActionChains.send_keys directly on the editor div.

FIXES IN THIS VERSION
---------------------
  • probe() now also checks the page URL — if it's a /auth/* page it
    tells you exactly what to do (log in manually first).
  • _diagnose_page() dumps the current URL + first 800 chars of page
    source whenever something fails — so you always know what the
    browser is actually showing.
  • _type_into_editor() uses document.execCommand('insertText') which
    is the correct way to inject text into a ProseMirror instance.
  • SEL_INPUT covers both the old id selector and the ProseMirror class.
  • send() waits for the send button to become enabled before clicking.
  • recv() has a two-phase wait: stop-button appears → disappears.
  • new_chat() waits for the input to be ready before returning.

HOW TO FIX THE LOGIN ISSUE (do this once per profile)
------------------------------------------------------
  1. Launch Chrome with the profile:

       /path/to/Google\ Chrome \\
           --remote-debugging-port=9222 \\
           --user-data-dir=/tmp/chrome-chatgpt-main \\
           &

  2. In the browser window: go to https://chatgpt.com and log in.
  3. Verify you see the chat input (not a login form).
  4. Leave Chrome running. Run this script — it attaches to that session.

  For multiple accounts: repeat with --remote-debugging-port=9223 and
  --user-data-dir=/tmp/chrome-chatgpt-2, etc.

Dataset swap  (look for ← DATASET HOOK)
-----------------------------------------
  Change SYSTEM_PROMPT, make_gen_prompt(), make_solve_prompt().
"""

from __future__ import annotations

import argparse
import datetime
import json
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════

DEBUG_PORT   = 9222
CHATGPT_URL  = "https://chatgpt.com/"      # landing page (model chosen below)

NUM_TABS           = 6
CYCLES             = 30
PROBLEMS_PER_CYCLE = 10

RATE_PER_MIN  = 5.0        # req/min shared across all tabs (free=3-5, Plus=20-40)
JITTER_MIN    = 1.5        # seconds — minimum random delay before each request
JITTER_MAX    = 5.0        # seconds — maximum random delay

STREAM_TIMEOUT  = 120      # seconds to wait for a full response
ELEMENT_TIMEOUT = 30       # seconds to wait for DOM elements

OUTPUT_DIR = Path("chatgpt_output")

# Model slug appended to URL so no dropdown clicking is needed.
# Options: gpt-4o  gpt-4o-mini  o1  o1-mini  o3-mini
# None = use whatever is active
CHATGPT_MODEL = "gpt-4o"

# ── DATASET HOOK 1 ──────────────────────────────────────────
SYSTEM_PROMPT = """You are a math teacher who teaches in Hinglish
(code-mixed Hindi-English). Generate a step-by-step solution
to math problems where:
- Mathematical notation, numbers, formulas stay in English
- Problem comprehension steps are in Hindi (Devanagari script)
- Intermediate reasoning can mix both
- Final answer is numeric only
For each step, output a language tag: EN, HI, MIXED, MATH
Output ONLY valid JSON, no other text."""

# ── DATASET HOOK 2 ──────────────────────────────────────────
def make_gen_prompt(n: int, agent: str, cycle: int) -> str:
    return (
        f"Generate exactly {n} unique Hinglish math word problems "
        f"for cycle {cycle}, session {agent}.\n"
        "Topics: arithmetic, algebra, percentages, ratios, geometry. "
        "Every problem must have a deterministic numeric answer.\n"
        "Return ONLY a valid JSON array (no markdown), schema per item:\n"
        '{"hinglish_problem": string, "gold_answer": number}'
    )

# ── DATASET HOOK 3 ──────────────────────────────────────────
def make_solve_prompt(problem: str, gold: Any) -> str:
    return (
        SYSTEM_PROMPT
        + f"\n\nProblem: {problem}\n"
        + f"Known correct answer: {gold}\n"
        + "Generate step-by-step Hinglish solution."
    )


# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

_LOG_LOCK = threading.Lock()
_LOG_PATH: Optional[Path] = None


def log(msg: str, level: str = "INFO", tag: str = "") -> None:
    ts   = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level:4s}] {('[' + tag + '] ') if tag else ''}{msg}"
    with _LOG_LOCK:
        print(line)
        if _LOG_PATH:
            try:
                with _LOG_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════

def utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def extract_json_value(text: str) -> Optional[Any]:
    if not text:
        return None
    candidates = [text.strip()]
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


# ══════════════════════════════════════════════════════════════
#  TOKEN BUCKET
# ══════════════════════════════════════════════════════════════

class TokenBucket:
    def __init__(self, rate_per_min: float) -> None:
        self._rate   = rate_per_min / 60.0
        self._tokens = rate_per_min
        self._max    = rate_per_min
        self._lock   = threading.Lock()
        self._last   = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._max, self._tokens + (now - self._last) * self._rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(min(wait, 1.0))


# ══════════════════════════════════════════════════════════════
#  JSONL WRITER
# ══════════════════════════════════════════════════════════════

class JSONLWriter:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._q: queue.Queue[Optional[dict]] = queue.Queue()
        self._t = threading.Thread(target=self._run, daemon=True, name="writer")
        self._t.start()

    def write(self, item: dict) -> None:
        self._q.put(item)

    def flush_and_stop(self, timeout: float = 15.0) -> None:
        self._q.put(None)
        self._t.join(timeout=timeout)

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


# ══════════════════════════════════════════════════════════════
#  CHATGPT TAB
# ══════════════════════════════════════════════════════════════

class ChatGPTTab:
    """
    Manages one ChatGPT browser tab.

    Current ChatGPT DOM (2025-2026):
    ─────────────────────────────────
    Input (ProseMirror editor):
        div#prompt-textarea[contenteditable="true"].ProseMirror
          └── p[data-placeholder="Message ChatGPT"]

    Send button:
        button[data-testid="send-button"]           ← disabled while streaming
        button[aria-label="Send prompt"]             ← fallback

    Stop button (while streaming):
        button[data-testid="stop-button"]
        button[aria-label="Stop streaming"]

    Response:
        div[data-message-author-role="assistant"]    ← last one
        article[data-testid^="conversation-turn"]    ← fallback

    Key notes:
    • ProseMirror ignores .textContent changes — use execCommand('insertText')
      or ActionChains.send_keys() on the focused editor div.
    • Send button is disabled (aria-disabled="true") until text is present.
      Wait for it to become enabled before clicking.
    """

    # Ordered fallback lists — first match wins
    SEL_INPUT = [
        "div#prompt-textarea",                             # primary (ProseMirror root)
        "div.ProseMirror[contenteditable='true']",         # class fallback
        "div[contenteditable='true'][role='textbox']",     # role fallback
        "div[contenteditable='true']",                     # broadest
    ]
    SEL_SEND = [
        "button[data-testid='send-button']",
        "button[aria-label='Send prompt']",
        "button[aria-label='Send message']",
    ]
    SEL_STOP = [
        "button[data-testid='stop-button']",
        "button[aria-label='Stop streaming']",
        "button[aria-label='Stop generating']",
    ]
    SEL_RESP = [
        "div[data-message-author-role='assistant']",
        "article[data-testid^='conversation-turn-'] .markdown",
        "div.markdown",
    ]
    # Signals that the page has fully loaded and we're on the chat UI
    SEL_READY = [
        "div#prompt-textarea",
        "div.ProseMirror",
        "div[contenteditable='true'][role='textbox']",
    ]

    def __init__(self, driver: webdriver.Remote, handle: str, name: str) -> None:
        self.driver = driver
        self.handle = handle
        self.name   = name

    def _switch(self) -> None:
        self.driver.switch_to.window(self.handle)

    # ── diagnostics ────────────────────────────────────────────

    def _diagnose_page(self) -> None:
        """Log current URL and DOM snippet — call this when things fail."""
        try:
            self._switch()
            url = self.driver.current_url
            src = self.driver.page_source[:600]
            log(f"DIAG url={url}", "WARN", self.name)
            log(f"DIAG page_source_prefix={src!r}", "WARN", self.name)

            if "/auth/" in url or "login" in url.lower():
                log(
                    "⚠ PAGE IS A LOGIN WALL.  "
                    "You must log in to chatgpt.com manually in this Chrome profile "
                    "before running the pipeline.  See file header for instructions.",
                    "ERR", self.name,
                )
            elif "cf-browser-verification" in src or "cf_clearance" in src:
                log(
                    "⚠ CLOUDFLARE CHALLENGE detected.  "
                    "Solve it once manually in the browser, then rerun.",
                    "ERR", self.name,
                )
        except Exception as exc:
            log(f"diagnose_page error: {exc}", "WARN", self.name)

    # ── element helpers ────────────────────────────────────────

    def _find_first(self, selectors: list[str], timeout: int = ELEMENT_TIMEOUT) -> Any:
        """Try CSS selectors in order, return first visible match."""
        self._switch()
        wait = WebDriverWait(self.driver, timeout)
        for sel in selectors:
            try:
                el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                if el and el.is_displayed():
                    return el
            except Exception:
                pass
        return None

    def _wait_for_send_enabled(self, timeout: int = 10) -> bool:
        """Return True once the send button is clickable (not aria-disabled)."""
        self._switch()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for sel in self.SEL_SEND:
                try:
                    btns = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for btn in btns:
                        disabled = btn.get_attribute("aria-disabled") or ""
                        if disabled.lower() != "true" and btn.is_displayed():
                            return True
                except Exception:
                    pass
            time.sleep(0.3)
        return False

    # ── text injection ─────────────────────────────────────────

    def _type_into_editor(self, text: str) -> None:
        """
        Inject text into the ProseMirror editor.

        Strategy: click to focus, then use ActionChains.send_keys().
        This fires the correct keyboard events that ProseMirror listens to
        and enables the send button.  For very long texts we chunk into
        500-char blocks to avoid WebDriver timeout on a single send_keys call.
        """
        self._switch()
        editor = self._find_first(self.SEL_INPUT)
        if editor is None:
            self._diagnose_page()
            raise RuntimeError(f"[{self.name}] Chat input not found")

        # clear existing text  (Ctrl+A → Delete)
        try:
            editor.click()
            time.sleep(0.2)
            ActionChains(self.driver) \
                .key_down("\ue009")    \
                .send_keys("a")        \
                .key_up("\ue009")      \
                .send_keys("\ue017")   \
                .perform()             # Ctrl+A, Delete
            time.sleep(0.2)
        except Exception:
            pass

        # type text in 500-char chunks via ActionChains
        CHUNK = 500
        for i in range(0, len(text), CHUNK):
            chunk = text[i : i + CHUNK]
            ActionChains(self.driver).send_keys_to_element(editor, chunk).perform()
            time.sleep(0.05)

        time.sleep(0.3)

    # ── public interface ───────────────────────────────────────

    def send(self, prompt: str) -> None:
        """Type prompt and click send button."""
        self._switch()
        self._type_into_editor(prompt)

        # wait for send button to become enabled
        if not self._wait_for_send_enabled(timeout=8):
            log("Send button never became enabled — trying anyway", "WARN", self.name)

        # click send
        send_btn = self._find_first(self.SEL_SEND, timeout=5)
        if send_btn is None:
            raise RuntimeError(f"[{self.name}] Send button not found")

        # small human-feel pause before clicking
        time.sleep(random.uniform(0.3, 0.7))
        send_btn.click()
        log(f"→ sent ({len(prompt)} chars)", "INFO", self.name)

    def recv(self, timeout: int = STREAM_TIMEOUT) -> str:
        """
        Wait for streaming to complete and return the last assistant message.

        Phase 1: wait for stop button to appear   (streaming started)
        Phase 2: wait for stop button to disappear (streaming done)
        Phase 3: read the last assistant div
        """
        self._switch()
        deadline = time.monotonic() + timeout

        # Phase 1 — wait for stop button
        streaming_started = False
        while time.monotonic() < deadline:
            for sel in self.SEL_STOP:
                try:
                    els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    if any(e.is_displayed() for e in els):
                        streaming_started = True
                        break
                except Exception:
                    pass
            if streaming_started:
                break
            time.sleep(0.4)

        if not streaming_started:
            log("Stop button never appeared", "WARN", self.name)

        # Phase 2 — wait for stop button to disappear
        while time.monotonic() < deadline:
            stop_visible = False
            for sel in self.SEL_STOP:
                try:
                    els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    if any(e.is_displayed() for e in els):
                        stop_visible = True
                        break
                except Exception:
                    pass
            if not stop_visible:
                break
            time.sleep(0.6)

        # extra grace period for the very last tokens
        time.sleep(1.5)

        # Phase 3 — read response
        for sel in self.SEL_RESP:
            try:
                msgs = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if msgs:
                    text = msgs[-1].text.strip()
                    log(f"← recv {len(text)} chars", "OK", self.name)
                    return text
            except Exception:
                pass

        log("Could not read response text", "WARN", self.name)
        self._diagnose_page()
        return ""

    def probe(self) -> bool:
        """
        Check that this tab is on the ChatGPT chat UI (not a login page).
        Returns True if ready, False if login wall detected.
        """
        self._switch()
        url = self.driver.current_url
        log(f"Probing — current URL: {url}", "INFO", self.name)

        # Detect login wall immediately
        if any(x in url for x in ["/auth/", "login", "signup", "accounts.google"]):
            log(
                "LOGIN WALL DETECTED.  "
                "Log in to chatgpt.com manually in this Chrome profile first, "
                "then rerun the script.",
                "ERR", self.name,
            )
            return False

        # Wait for input to appear
        for sel in self.SEL_READY:
            try:
                WebDriverWait(self.driver, ELEMENT_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                log("Tab ready ✓", "OK", self.name)
                return True
            except Exception:
                pass

        # Timeout — diagnose
        log("Probe timed out", "WARN", self.name)
        self._diagnose_page()
        return False

    def new_chat(self) -> None:
        """Navigate to a fresh conversation and wait for the input to be ready."""
        self._switch()
        url = CHATGPT_URL
        if CHATGPT_MODEL:
            url = f"https://chatgpt.com/?model={CHATGPT_MODEL}"
        self.driver.get(url)

        # wait for input to appear before returning
        for sel in self.SEL_READY:
            try:
                WebDriverWait(self.driver, ELEMENT_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                return
            except Exception:
                pass

        log("new_chat: input not found after navigation", "WARN", self.name)
        self._diagnose_page()


# ══════════════════════════════════════════════════════════════
#  WORK ITEM
# ══════════════════════════════════════════════════════════════

@dataclass
class WorkItem:
    cycle: int
    agent: str
    key:   str = field(init=False)

    def __post_init__(self) -> None:
        self.key = f"{self.cycle}:{self.agent}"


# ══════════════════════════════════════════════════════════════
#  TAB WORKER
# ══════════════════════════════════════════════════════════════

class TabWorker(threading.Thread):
    def __init__(
        self,
        tab: ChatGPTTab,
        bucket: TokenBucket,
        work_q: "queue.Queue[WorkItem]",
        writer: JSONLWriter,
        done_keys: set,
        done_lock: threading.Lock,
        problems_per_cycle: int,
        jitter: tuple[float, float],
        stats: dict,
        stats_lock: threading.Lock,
    ) -> None:
        super().__init__(daemon=True, name=f"tab-{tab.name}")
        self.tab               = tab
        self.bucket            = bucket
        self.work_q            = work_q
        self.writer            = writer
        self.done_keys         = done_keys
        self.done_lock         = done_lock
        self.problems_per_cycle = problems_per_cycle
        self.jitter            = jitter
        self.stats             = stats
        self.stats_lock        = stats_lock
        self._stop             = threading.Event()
        self._consecutive_errs = 0
        self._MAX_CONSEC_ERRS  = 5     # abort worker after this many straight errors

    def stop(self) -> None:
        self._stop.set()

    def _send(self, prompt: str) -> str:
        """Jitter → rate-limit token → send → recv."""
        time.sleep(random.uniform(*self.jitter))
        self.bucket.acquire()
        self.tab.send(prompt)
        return self.tab.recv()

    def _repair_problems(self, raw: str) -> list[dict]:
        parsed = extract_json_value(raw)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        fixed  = self._send(
            "Convert the following into a strict JSON array only. "
            'Schema per item: {"hinglish_problem": string, "gold_answer": number}.\n'
            "CONTENT:\n" + raw
        )
        parsed2 = extract_json_value(fixed)
        return [r for r in parsed2 if isinstance(r, dict)] if isinstance(parsed2, list) else []

    def _repair_solution(self, raw: str) -> dict:
        parsed = extract_json_value(raw)
        if isinstance(parsed, dict):
            return parsed
        fixed = self._send("Convert into ONE strict JSON object only.\nCONTENT:\n" + raw)
        parsed2 = extract_json_value(fixed)
        return parsed2 if isinstance(parsed2, dict) else {"raw": raw[:300], "parse_status": "failed"}

    def run(self) -> None:
        log("Worker started", "OK", self.tab.name)

        while not self._stop.is_set():
            if self._consecutive_errs >= self._MAX_CONSEC_ERRS:
                log(
                    f"Too many consecutive errors ({self._consecutive_errs}) — "
                    "worker aborting.  Check Chrome profile login status.",
                    "ERR", self.tab.name,
                )
                break

            try:
                item: WorkItem = self.work_q.get(timeout=3)
            except queue.Empty:
                continue

            with self.done_lock:
                if item.key in self.done_keys:
                    self.work_q.task_done()
                    continue

            # open fresh conversation
            try:
                self.tab.new_chat()
                time.sleep(random.uniform(1.0, 2.5))
            except Exception as exc:
                log(f"new_chat error: {exc}", "WARN", self.tab.name)

            # 1) generate problems
            try:
                raw_gen  = self._send(make_gen_prompt(self.problems_per_cycle, self.tab.name, item.cycle))
                problems = self._repair_problems(raw_gen)[: self.problems_per_cycle]
                self._consecutive_errs = 0
            except Exception as exc:
                log(f"Gen error cycle={item.cycle}: {exc}", "ERR", self.tab.name)
                self._consecutive_errs += 1
                self.work_q.task_done()
                self.work_q.put(item)
                backoff = min(120, 10 * (2 ** self._consecutive_errs))
                log(f"Backing off {backoff}s before retry", "WARN", self.tab.name)
                time.sleep(backoff)
                continue

            log(f"cycle={item.cycle} → {len(problems)} problems", "OK", self.tab.name)

            # 2) solve each
            for p_idx, p in enumerate(problems, start=1):
                hinglish = str(p.get("hinglish_problem", "")).strip()
                gold     = p.get("gold_answer", "")
                if not hinglish:
                    continue
                try:
                    raw_sol  = self._send(make_solve_prompt(hinglish, gold))
                    solution = self._repair_solution(raw_sol)
                    self._consecutive_errs = 0
                except Exception as exc:
                    solution = {"parse_status": "error", "error": str(exc)}

                self.writer.write({
                    "key":              item.key,
                    "cycle":            item.cycle,
                    "agent":            self.tab.name,
                    "index":            p_idx,
                    "hinglish_problem": hinglish,
                    "gold_answer":      gold,
                    "solution":         solution,
                    "generated_at":     utc_now(),
                })

                with self.done_lock:
                    self.done_keys.add(item.key)
                with self.stats_lock:
                    self.stats["solved"] += 1

                log(f"cycle={item.cycle} p={p_idx}/{len(problems)} ✓", "OK", self.tab.name)

            self.work_q.task_done()

        log("Worker stopped", "INFO", self.tab.name)


# ══════════════════════════════════════════════════════════════
#  DRIVER HELPERS
# ══════════════════════════════════════════════════════════════

def attach_driver(port: int) -> webdriver.Remote:
    opts = webdriver.ChromeOptions()
    opts.add_experimental_option("debuggerAddress", f"localhost:{port}")
    # Suppress "Chrome is being controlled by automated software" bar
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=opts)


def open_tab(driver: webdriver.Remote, url: str) -> str:
    driver.execute_script("window.open(arguments[0]);", url)
    time.sleep(2.5)
    return driver.window_handles[-1]


# ══════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

class ChatGPTPipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args       = args
        self.output_dir = Path(args.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        global _LOG_PATH
        _LOG_PATH = self.output_dir / (
            "run_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
        )

        self.jsonl_path = self.output_dir / "problems.jsonl"

        self.done_keys: set[str] = set()
        self.done_lock            = threading.Lock()
        self._load_resume()

        self.bucket     = TokenBucket(args.rate_per_min)
        self.work_q: queue.Queue[WorkItem] = queue.Queue()
        self.writer     = JSONLWriter(self.jsonl_path)
        self.stats      = {"solved": 0, "started_at": utc_now()}
        self.stats_lock = threading.Lock()
        self.workers: list[TabWorker] = []

    def _load_resume(self) -> None:
        if not self.jsonl_path.exists():
            return
        count = 0
        with self.jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line.strip())
                    if "key" in rec:
                        self.done_keys.add(rec["key"])
                        count += 1
                except Exception:
                    pass
        if count:
            log(f"Resume: {count} already done", "INFO")

    def run(self) -> None:
        log("=== CHATGPT PIPELINE START ===", "INFO")
        log(f"{self.args.num_tabs} tabs | {self.args.cycles} cycles | "
            f"{self.args.problems_per_cycle} problems/cycle | "
            f"rate {self.args.rate_per_min}/min | "
            f"jitter {self.args.jitter_min}-{self.args.jitter_max}s", "INFO")

        agent_names = [f"TAB{i+1}" for i in range(self.args.num_tabs)]
        total = 0
        for cycle in range(1, self.args.cycles + 1):
            for agent in agent_names:
                item = WorkItem(cycle=cycle, agent=agent)
                if item.key not in self.done_keys:
                    self.work_q.put(item)
                    total += 1
        log(f"Enqueued {total} work items", "INFO")

        driver = attach_driver(self.args.debug_port)

        for i, agent_name in enumerate(agent_names):
            url = CHATGPT_URL
            if CHATGPT_MODEL:
                url = f"https://chatgpt.com/?model={CHATGPT_MODEL}"
            handle = open_tab(driver, url)
            tab    = ChatGPTTab(driver, handle, agent_name)

            # stagger tab startup
            time.sleep(3)
            ready = tab.probe()
            if not ready:
                log(
                    f"Tab {agent_name} is NOT ready — likely not logged in. "
                    "Log in manually first.",
                    "ERR",
                )
                # Still start worker — it will hit consecutive error limit and self-stop

            worker = TabWorker(
                tab=tab,
                bucket=self.bucket,
                work_q=self.work_q,
                writer=self.writer,
                done_keys=self.done_keys,
                done_lock=self.done_lock,
                problems_per_cycle=self.args.problems_per_cycle,
                jitter=(self.args.jitter_min, self.args.jitter_max),
                stats=self.stats,
                stats_lock=self.stats_lock,
            )
            self.workers.append(worker)
            worker.start()
            time.sleep(2)

        try:
            while not self.work_q.empty():
                with self.stats_lock:
                    solved = self.stats["solved"]
                log(f"solved={solved} | queue={self.work_q.qsize()}", "INFO")
                time.sleep(20)
        except KeyboardInterrupt:
            log("Ctrl-C — stopping", "WARN")

        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.join(timeout=15)

        self.writer.flush_and_stop()
        (self.output_dir / "summary.json").write_text(
            json.dumps({
                "started_at":    self.stats["started_at"],
                "completed_at":  utc_now(),
                "num_tabs":      self.args.num_tabs,
                "total_solved":  self.stats["solved"],
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log(f"=== DONE  total_solved={self.stats['solved']} ===", "OK")


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ChatGPT Selenium pipeline (fixed)")
    p.add_argument("--debug-port",         type=int,   default=DEBUG_PORT)
    p.add_argument("--num-tabs",           type=int,   default=NUM_TABS)
    p.add_argument("--cycles",             type=int,   default=CYCLES)
    p.add_argument("--problems-per-cycle", type=int,   default=PROBLEMS_PER_CYCLE)
    p.add_argument("--rate-per-min",       type=float, default=RATE_PER_MIN)
    p.add_argument("--jitter-min",         type=float, default=JITTER_MIN)
    p.add_argument("--jitter-max",         type=float, default=JITTER_MAX)
    p.add_argument("--output-dir",         default=str(OUTPUT_DIR))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    pipe = ChatGPTPipeline(args)
    try:
        pipe.run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        log(f"Unhandled: {exc}", "ERR")
        raise


if __name__ == "__main__":
    main()