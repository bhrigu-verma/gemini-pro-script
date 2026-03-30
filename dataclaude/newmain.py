"""
script1_generate_dataset.py  —  HinglishMath-1K Dataset Generator (PARALLEL)
=============================================================================

WHAT CHANGED FROM V1
--------------------
Previous version was SEQUENTIAL: GEN1 sends → waits 60s → saves → GEN2 sends...
This version is PARALLEL: GEN1, GEN2, GEN3, GEN4 all send simultaneously.
Each tab runs in its own thread. The driver_lock is held ONLY for fast DOM
operations (~2s). The 60-180s Gemini response wait happens OUTSIDE the lock.
Result: ~3.5x speedup. 4 tabs generating at the same time.

PARALLELISM DESIGN
------------------
  Thread-GEN1: [LOCK: send] → unlock → [wait 90s no lock] → [LOCK: read] → save
  Thread-GEN2: [LOCK: send] → unlock → [wait 90s no lock] → [LOCK: read] → save
  Thread-GEN3: [LOCK: send] → unlock → [wait 90s no lock] → [LOCK: read] → save
  Thread-GEN4: [LOCK: send] → unlock → [wait 90s no lock] → [LOCK: read] → save
               ↑ staggered by ~1s             ↑ all waiting in parallel

USAGE
-----
  # Start Chrome first (one-time):
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-hm \
    --headless=new

  # Quick test - 60 problems, 2 tabs:
  python3 script1_generate_dataset.py --agents 2 --target 60

  # Full run - 1000 problems, 4 tabs:
  python3 script1_generate_dataset.py --agents 4 --target 1000

  # Resume if interrupted (reads existing JSONL, skips duplicates):
  python3 script1_generate_dataset.py --agents 4 --target 1000

OUTPUT
------
  hm_dataset/
    hinglishmath_1k.jsonl      final dataset (1 JSON object per line)
    batches/                   raw batch files for crash recovery
    generation_log.json        run stats
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
        StaleElementReferenceException, WebDriverException,
    )
except ImportError:
    sys.exit("ERROR: pip install selenium webdriver-manager")

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WDM = True
except ImportError:
    HAS_WDM = False


# ─── Timing constants ─────────────────────────────────────────────────────────
RESPONSE_TIMEOUT = 240   # max seconds to wait per response
STABLE_CHECKS    = 4     # how many identical reads = done
STABLE_INTERVAL  = 3.0   # seconds between stability polls
GEMINI_URL       = "https://gemini.google.com/app"

INPUT_SELS = [
    "rich-textarea div[contenteditable='true']",
    "rich-textarea p",
    "div[contenteditable='true'][data-placeholder]",
    "div[contenteditable='true']",
    ".ql-editor",
    "textarea",
]
SEND_SELS = [
    "button[aria-label='Send message']",
    "button[jsname='Qx7uuf']",
    "button[data-testid='send-button']",
    "button[mattooltip='Send message']",
    "button[aria-label='Submit']",
    "button.send-button",
    "mat-icon[data-mat-icon-name='send']",
]
STOP_SELS = [
    "button[aria-label='Stop response']",
    "button[aria-label='Stop generating']",
    "button[aria-label='Stop']",
    "button[jsname='k9Ysde']",
    "button[data-testid='stop-button']",
    ".stop-button",
]
RESP_SELS = [
    "model-response .markdown",
    "model-response response-text",
    "model-response",
    "message-content",
    "[data-turn-role='model']",
    "[data-message-author-role='model']",
    "message-content .markdown",
    ".response-content .markdown",
    ".response-content",
]


# ─── Topic pool ───────────────────────────────────────────────────────────────
TOPIC_POOL = [
    ("Work & Time",           "JEE-Main",     "rotation_work"),
    ("Work & Time",           "JEE-Advanced", "variable_efficiency"),
    ("Pipes & Cisterns",      "JEE-Main",     "phase_change_flow"),
    ("Speed, Distance, Time", "JEE-Main",     "relative_speed"),
    ("Speed, Distance, Time", "JEE-Advanced", "multi_leg_journey"),
    ("Mixture & Alligation",  "JEE-Main",     "two_vessels"),
    ("Mixture & Alligation",  "JEE-Advanced", "repeated_dilution"),
    ("Percentage",            "Grade-12",     "chain_percentage"),
    ("Profit & Loss",         "Grade-12",     "successive_discount"),
    ("Compound Interest",     "JEE-Main",     "semi_annual"),
    ("Compound Interest",     "JEE-Advanced", "emi_reverse"),
    ("Quadratic Equations",   "JEE-Main",     "vieta_formulas"),
    ("Quadratic Equations",   "JEE-Advanced", "roots_transformation"),
    ("Sequences & Series",    "JEE-Main",     "ap_gp_combined"),
    ("Sequences & Series",    "JEE-Advanced", "recursive_sequence"),
    ("Logarithms",            "JEE-Main",     "nested_log"),
    ("Complex Numbers",       "JEE-Main",     "modulus_argument"),
    ("Complex Numbers",       "JEE-Advanced", "de_moivre"),
    ("Combinatorics",         "JEE-Advanced", "inclusion_exclusion"),
    ("Probability",           "JEE-Main",     "conditional_probability"),
    ("Probability",           "JEE-Advanced", "bayes_theorem"),
    ("Probability",           "JEE-Advanced", "multi_stage_tree"),
    ("Coordinate Geometry",   "JEE-Main",     "line_circle_intersection"),
    ("Coordinate Geometry",   "JEE-Advanced", "parabola_tangent"),
    ("Trigonometry",          "JEE-Advanced", "trig_identity_chain"),
    ("Calculus - Diff",       "JEE-Advanced", "implicit_differentiation"),
    ("Calculus - Integ",      "JEE-Advanced", "area_between_curves"),
    ("Partnership",           "Grade-12",     "variable_capital"),
    ("GST & Taxation",        "Grade-12",     "itc_chain"),
    ("Ratio & Proportion",    "Grade-12",     "lakh_crore_notation"),
    ("Time Value of Money",   "Grade-12",     "npv_comparison"),
    ("Number Theory",         "Codeforces-C", "prime_factorisation"),
    ("Modular Arithmetic",    "Codeforces-D", "crt_system"),
    ("GCD & LCM",             "Codeforces-B", "lcm_word_problem"),
    ("Counting Paths",        "Codeforces-C", "grid_dp"),
    ("Game Theory",           "Codeforces-D", "nim_variant"),
    ("Sequences",             "Codeforces-D", "dp_recurrence"),
    ("Kinematics",            "JEE-Advanced", "relative_projectile"),
    ("Statistics",            "Grade-12",     "mean_median_mode"),
    ("Polynomials",           "JEE-Advanced", "remainder_theorem"),
]

GENERATION_SYSTEM = """You are a research mathematician designing a SCIENTIFIC BENCHMARK to test whether code-mixed Hinglish (Hindi+English) inputs cause LLMs to give wrong answers on math problems they would solve correctly in English.

HARDNESS REQUIREMENTS — every problem MUST satisfy ALL:
1. Minimum 4 algebraic steps — no trivial single-step problems
2. Requires genuine multi-step reasoning, cannot be solved by formula lookup
3. JEE-Advanced / JEE-Main / Codeforces Div2-C/D equivalent difficulty
4. Has a unique deterministic numeric answer

LINGUISTIC TRAPS — embed ONE of these in the HG_065 variant:
• "3 lakh 75 hazaar" notation → LLM may parse as 3 OR 75000, not 375000
• "10 aur log aaye" → means 10 MORE people joined (additive) — LLM reads as just 10
• "na X se na Y se divisible" → neither X nor Y — LLM interprets as OR
• "6 mahine baad" in annual rate problem → LLM uses annual rate instead of semi-annual
• "A se 20% zyada" → which base? LLM sometimes reverses the direction
• "pehle karo phir" → sequence matters; LLM may swap the operations
• "baaki" → explicit remainder; LLM ignores the subtraction from previous step
• "dono ko" → means BOTH together; LLM may do only one
• "withdraw kar liya N mahine baad" → timing in compound interest; LLM applies wrong
"""


def make_batch_prompt(topic: str, diff: str, src: str, batch_id: int, agent: str) -> str:
    return (
        f"{GENERATION_SYSTEM}\n\n"
        f"GENERATE exactly 15 problems.\n"
        f"TOPIC: {topic} | DIFFICULTY: {diff} | TYPE: {src}\n"
        f"BATCH ID: {batch_id}-{agent}  ← vary all numbers uniquely per batch\n\n"
        "Return ONLY a raw JSON array. No markdown. No ``` fences. No text outside the array.\n"
        "Start with [ and end with ].\n\n"
        "Each item schema (all fields required):\n"
        '{\n'
        '  "problem_en":    "Problem in clear English. Hard. 4+ steps.",\n'
        '  "problem_hi":    "Same in pure Devanagari Hindi only.",\n'
        '  "problem_hg_030":"Same in light Hinglish 30% English naturally mixed.",\n'
        '  "problem_hg_065":"Same in heavy Hinglish 65% mixed. MUST use one linguistic trap.",\n'
        '  "gold_answer":   "exact answer string e.g. 180 or 37/70",\n'
        '  "gold_answer_num": 180.0,\n'
        '  "unit":          "km or hours or rupees or dimensionless",\n'
        '  "topic":         "' + topic + '",\n'
        '  "difficulty":    "' + diff + '",\n'
        '  "source_type":   "' + src + '",\n'
        '  "solution_steps_en": ["Step 1: ...", "Step 2: ...", "Final: answer = ..."],\n'
        '  "linguistic_trap_in_hg065": "Which trap and exactly how it misleads an LLM.",\n'
        '  "why_hard": "One sentence: what makes this specifically hard for an LLM.",\n'
        '  "tags": ["tag1", "tag2"]\n'
        '}\n\n'
        "RULES: Verify your own arithmetic. All 15 scenarios must be DIFFERENT. "
        "JSON must parse with json.loads() with zero modifications."
    )


# ─── Thread-safe logging ──────────────────────────────────────────────────────
_print_lock = threading.Lock()

def log(msg: str, level: str = "INFO", tab: str = "") -> None:
    tag  = f"[{tab}]" if tab else "      "
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {level:5s} {tag} {msg}"
    with _print_lock:
        print(line, flush=True)

def utc_now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


# ─── JSON extraction ──────────────────────────────────────────────────────────
def extract_json_array(text: str) -> list[dict]:
    if not text:
        return []
    candidates = [text.strip()]
    for fenced in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
        if fenced.strip():
            candidates.append(fenced.strip())
    depth, start, best = 0, -1, ""
    for i, ch in enumerate(text):
        if ch == '[':
            if depth == 0:
                start = i
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start != -1:
                cand = text[start:i+1]
                if len(cand) > len(best):
                    best = cand
                start = -1
    if best:
        candidates.append(best)
    for cand in candidates:
        try:
            p = json.loads(cand)
            if isinstance(p, list):
                return [r for r in p if isinstance(r, dict)]
        except Exception:
            pass
    return []


def fingerprint(item: dict) -> str:
    key = (
        item.get("topic", "") +
        item.get("gold_answer", "") +
        item.get("source_type", "") +
        (item.get("problem_en") or "")[:50]
    )
    return hashlib.md5(key.encode()).hexdigest()[:14]


# ─── Thread-safe shared state ─────────────────────────────────────────────────
class SharedState:
    def __init__(self, out: Path, target: int):
        self.out        = out
        self.batch_dir  = out / "batches"
        self.jsonl_path = out / "hinglishmath_1k.jsonl"
        self.target     = target
        self._count     = 0
        self._fps       : set[str] = set()
        self._cnt_lock  = threading.Lock()
        self._wrt_lock  = threading.Lock()
        self._bat_lock  = threading.Lock()
        self._bat_n     = 0

        self.out.mkdir(parents=True, exist_ok=True)
        self.batch_dir.mkdir(parents=True, exist_ok=True)

        # Resume support
        if self.jsonl_path.exists():
            with self.jsonl_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        self._count += 1
                        self._fps.add(item.get("_fp", ""))
                    except Exception:
                        pass
            if self._count:
                log(f"Resuming — {self._count} items already on disk.")

    @property
    def count(self) -> int:
        return self._count

    @property
    def done(self) -> bool:
        return self._count >= self.target

    def next_batch_id(self) -> int:
        with self._bat_lock:
            self._bat_n += 1
            return self._bat_n

    def try_add(self, raw: dict) -> Optional[dict]:
        """Add item if not duplicate and target not reached. Thread-safe."""
        with self._cnt_lock:
            if self._count >= self.target:
                return None
            fp = fingerprint(raw)
            if fp in self._fps:
                return None
            self._fps.add(fp)
            idx          = self._count + 1
            self._count += 1

        enriched = self._enrich(raw, idx, fp)
        with self._wrt_lock:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(enriched, ensure_ascii=False) + "\n")
        return enriched

    def save_batch_file(self, items: list[dict], batch_id: int, agent: str) -> None:
        p = self.batch_dir / f"batch_{batch_id:04d}_{agent}.json"
        with self._wrt_lock:
            p.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _enrich(raw: dict, idx: int, fp: str) -> dict:
        return {
            "id":   f"HM-{idx:04d}",
            "_fp":  fp,
            "variants": {
                "EN":     {"problem": raw.get("problem_en",     ""), "cm_degree": 0.0},
                "HI":     {"problem": raw.get("problem_hi",     ""), "cm_degree": 0.0},
                "HG_030": {"problem": raw.get("problem_hg_030", ""), "cm_degree": 0.3},
                "HG_065": {"problem": raw.get("problem_hg_065", ""), "cm_degree": 0.65},
            },
            "gold_answer":       str(raw.get("gold_answer", "")),
            "gold_answer_num":   float(raw.get("gold_answer_num", 0) or 0),
            "unit":              raw.get("unit", ""),
            "topic":             raw.get("topic", ""),
            "difficulty":        raw.get("difficulty", ""),
            "source_type":       raw.get("source_type", ""),
            "solution_steps_en": raw.get("solution_steps_en", []),
            "linguistic_trap":   raw.get("linguistic_trap_in_hg065", ""),
            "why_hard":          raw.get("why_hard", ""),
            "tags":              raw.get("tags", []),
            "generated_at":      utc_now(),
        }


# ─── Parallel GeminiTab  ──────────────────────────────────────────────────────
class GeminiTab:
    """
    Wraps one browser tab. Designed for concurrent use with a shared driver.

    KEY PRINCIPLE:
      - driver_lock is acquired for DOM operations (fast, ~1-3s each)
      - driver_lock is RELEASED while sleeping / waiting for Gemini response
      - This means all tabs truly wait for their responses in parallel
    """

    def __init__(self, driver: webdriver.Chrome, handle: str,
                 name: str, driver_lock: threading.Lock):
        self.driver      = driver
        self.handle      = handle
        self.name        = name
        self.lock        = driver_lock

    # ── Lock-guarded DOM helpers ──────────────────────────────────────────────

    def _focus(self) -> None:
        if self.driver.current_window_handle != self.handle:
            self.driver.switch_to.window(self.handle)

    def _input_el(self, timeout: int = 15):
        for sel in INPUT_SELS:
            try:
                return WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
            except TimeoutException:
                continue
        raise TimeoutException(f"{self.name}: no input element found")

    def _type(self, el, text: str) -> None:
        self.driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('delete',false,null);", el,
        )
        time.sleep(0.15)
        for i in range(0, len(text), 300):
            chunk = text[i:i+300]
            safe  = chunk.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
            self.driver.execute_script(
                "document.execCommand('insertText',false,`" + safe + "`);", el
            )
            time.sleep(0.04)

    def _send_click(self) -> bool:
        for sel in SEND_SELS:
            els = self.driver.find_elements(By.CSS_SELECTOR, sel)
            if not els:
                continue
            el = els[0]
            if el.tag_name.lower() != "button":
                try:
                    el = el.find_element(By.XPATH, "ancestor::button[1]")
                except NoSuchElementException:
                    pass
            try:
                self.driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                try:
                    el.click()
                    return True
                except Exception:
                    pass
        return False

    def _streaming(self) -> bool:
        for sel in STOP_SELS:
            try:
                if self.driver.find_element(By.CSS_SELECTOR, sel).is_displayed():
                    return True
            except NoSuchElementException:
                pass
        return False

    def _last_text(self) -> str:
        for sel in RESP_SELS:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    t = els[-1].text.strip()
                    if t:
                        return t
            except StaleElementReferenceException:
                continue
        return ""

    def _resp_count(self) -> int:
        for sel in RESP_SELS:
            c = len(self.driver.find_elements(By.CSS_SELECTOR, sel))
            if c:
                return c
        return 0

    # ── Public API ────────────────────────────────────────────────────────────

    def send(self, text: str) -> int:
        """
        Type prompt and click send. Returns resp_count BEFORE sending.
        Holds lock for the full send (~2s), then releases it.
        """
        with self.lock:
            self._focus()
            before = self._resp_count()
            el     = self._input_el(timeout=20)
            self._type(el, text)
            time.sleep(0.6)
            if not self._send_click():
                log("Send button not found — trying Enter", "WARN", self.name)
                el.send_keys(Keys.RETURN)
        log(f"Sent ({len(text)} chars). Waiting for response…", "TX", self.name)
        return before

    def recv(self, before_count: int) -> str:
        """
        Wait for Gemini to finish generating.
        Lock is acquired only for brief polls (~0.1s) — released between polls.
        All tabs can run this concurrently.
        """
        # Wait for stream to START
        t0 = time.time()
        while time.time() - t0 < 25:
            with self.lock:
                self._focus()
                started = self._streaming() or self._resp_count() > before_count
            if started:
                break
            time.sleep(0.5)   # ← outside lock

        # Wait for stream to FINISH
        deadline  = time.time() + RESPONSE_TIMEOUT
        last_text = ""
        stable    = 0

        while time.time() < deadline:
            time.sleep(STABLE_INTERVAL)  # ← outside lock: true parallel wait

            with self.lock:
                self._focus()
                live    = self._streaming()
                current = self._last_text()

            if live:
                stable    = 0
                last_text = current
                continue

            if current and current == last_text:
                stable += 1
                if stable >= STABLE_CHECKS:
                    log(f"Response ready: {len(current)} chars", "OK", self.name)
                    return current
            else:
                stable    = 0
                last_text = current

        log("Timeout — returning best effort", "WARN", self.name)
        return last_text or "[TIMED OUT]"

    def ask(self, text: str) -> str:
        """Full send + receive cycle."""
        before = self.send(text)
        return self.recv(before)

    def ask_json(self, prompt: str) -> list[dict]:
        """Ask, parse JSON, repair once if needed."""
        raw   = self.ask(prompt)
        items = extract_json_array(raw)
        if not items:
            log("JSON parse failed — requesting repair", "WARN", self.name)
            repair = (
                "Your previous response was not valid JSON. "
                "Return ONLY the JSON array. Start with [ end with ]. "
                "No markdown. No ``` fences. No text before or after."
            )
            raw2  = self.ask(repair)
            items = extract_json_array(raw2)
            if items:
                log(f"Repair succeeded: {len(items)} items", "OK", self.name)
            else:
                log("Repair failed — skipping batch", "ERR", self.name)
        return items


# ─── Per-tab worker thread ────────────────────────────────────────────────────
def tab_worker(tab: GeminiTab, state: SharedState,
               start_idx: int) -> int:
    """Runs in its own thread. Loops until target reached."""
    contributed = 0
    topic_idx   = start_idx

    while not state.done:
        topic, diff, src = TOPIC_POOL[topic_idx % len(TOPIC_POOL)]
        topic_idx += 1

        batch_id = state.next_batch_id()
        log(f"Batch {batch_id} | {topic} ({diff})", "BATCH", tab.name)

        try:
            prompt = make_batch_prompt(topic, diff, src, batch_id, tab.name)
            items  = tab.ask_json(prompt)
        except Exception as e:
            log(f"Batch {batch_id} exception: {e}", "ERR", tab.name)
            time.sleep(5)
            continue

        state.save_batch_file(items, batch_id, tab.name)

        added = 0
        for raw in items:
            if state.done:
                break
            if state.try_add(raw):
                added      += 1
                contributed += 1

        log(
            f"Batch {batch_id} done: +{added} | "
            f"Total {state.count}/{state.target} ({100*state.count/state.target:.0f}%)",
            "OK", tab.name,
        )
        time.sleep(2)  # gentle rate-limit pause

    log(f"Worker done. Contributed {contributed} items total.", "DONE", tab.name)
    return contributed


# ─── Chrome attach / tab open ─────────────────────────────────────────────────
def attach_driver(port: int) -> webdriver.Chrome:
    log(f"Attaching to Chrome on port {port}")
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    if HAS_WDM:
        try:
            drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
            log("Attached via webdriver-manager", "OK")
            return drv
        except Exception as e:
            log(f"WDM failed: {e}", "WARN")
    drv = webdriver.Chrome(options=opts)
    log("Attached via system chromedriver", "OK")
    return drv

def open_tab(driver: webdriver.Chrome, url: str, label: str) -> str:
    before = set(driver.window_handles)
    driver.switch_to.new_window("tab")
    after = set(driver.window_handles)
    new_h = list(after - before)
    if not new_h:
        raise RuntimeError(f"Cannot open tab {label}")
    driver.switch_to.window(new_h[0])
    driver.get(url)
    log("Tab ready", "OK", label)
    return new_h[0]


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="HinglishMath-1K Generator — Parallel")
    p.add_argument("--debug-port", type=int, default=9222)
    p.add_argument("--agents",     type=int, default=4)
    p.add_argument("--target",     type=int, default=1000)
    p.add_argument("--output-dir", default="hmm_dataset")
    args = p.parse_args()

    n    = max(1, args.agents)
    out  = Path(args.output_dir)

    print("\n" + "═"*62)
    print(f"  HinglishMath Generator  |  PARALLEL  |  {n} tabs")
    print(f"  Target: {args.target} problems  →  {out}")
    print("═"*62 + "\n")

    state       = SharedState(out, args.target)
    driver_lock = threading.Lock()   # shared across all tabs
    driver      = attach_driver(args.debug_port)

    # Open all tabs first, then start workers
    tabs: list[GeminiTab] = []
    for i in range(n):
        lbl    = f"GEN{i+1}"
        handle = open_tab(driver, GEMINI_URL, lbl)
        time.sleep(3.5)   # let page load before opening next
        tabs.append(GeminiTab(driver, handle, lbl, driver_lock))

    log(f"All {n} tabs ready. Launching parallel workers NOW…", "START")
    print()

    t0 = time.time()

    # Each tab gets a different starting topic so they don't all generate
    # the same topic simultaneously
    step = len(TOPIC_POOL) // n
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {
            pool.submit(tab_worker, tab, state, i * step): tab.name
            for i, tab in enumerate(tabs)
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                c = future.result()
                log(f"Thread done — contributed {c} items", "DONE", name)
            except Exception as e:
                log(f"Thread crashed: {e}", "ERR", name)

    elapsed = time.time() - t0
    print("\n" + "═"*62)
    print(f"  COMPLETE  |  {state.count} problems in {elapsed/60:.1f} min")
    print(f"  File: {(out / 'hinglishmath_1k.jsonl').resolve()}")
    print("═"*62 + "\n")

    (out / "generation_log.json").write_text(json.dumps({
        "total":     state.count,
        "target":    args.target,
        "agents":    n,
        "elapsed_s": round(elapsed, 1),
        "file":      str(out / "hinglishmath_1k.jsonl"),
        "done_at":   utc_now(),
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()