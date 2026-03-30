"""
script2_evaluate.py  —  HinglishMath Parallel Evaluator  (FAST VERSION)
=========================================================================

WHAT THIS SCRIPT DOES
----------------------
Reads hinglishmath_1k.jsonl (output of script1), evaluates every problem
variant (EN / HI / HG_030 / HG_065) through Gemini, and saves structured
results to results_raw.jsonl for analysis by script3.

SPEED OPTIMISATIONS vs original version
-----------------------------------------
1. 6 parallel Gemini tabs (was 4)
2. BATCH mode: 4 problems sent in ONE prompt → 4x fewer API calls
3. Per-tab variant assignment: no within-tab contamination
4. Resume: already-evaluated pairs are skipped on restart

USAGE
-----
  python3 script2_evaluate.py \
    --input  hm_dataset/hinglishmath_1k.jsonl \
    --agents 6 \
    --batch-size 4 \
    --mode blind \
    --output-dir hm_results

  # Quick test on first 40 problems:
  python3 script2_evaluate.py --input hm_dataset/hinglishmath_1k.jsonl --limit 40

  # Resume after crash (auto-skips already done):
  python3 script2_evaluate.py --input hm_dataset/hinglishmath_1k.jsonl

COMMON MISTAKE — WRONG INPUT FILE
-----------------------------------
  WRONG:  --input hm_results/results_raw.jsonl   (that is the OUTPUT, not input)
  RIGHT:  --input hm_dataset/hinglishmath_1k.jsonl

OUTPUT (results_raw.jsonl) — one JSON per line
-----------------------------------------------
{
  "problem_id":         "HM-0001",
  "variant":            "HG_065",
  "mode":               "blind",
  "cm_degree":          0.65,
  "problem_text":       "...",
  "gold_answer":        "180",
  "predicted_answer":   "180",
  "is_correct":         true,
  "reasoning_language": "HINGLISH",
  "language_switches":  3,
  "off_target_lang":    false,
  "response_length":    923,
  "topic":              "Speed, Distance, Time",
  ...
}
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional
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
        TimeoutException, NoSuchElementException, StaleElementReferenceException,
    )
except ImportError:
    sys.exit("ERROR: pip install selenium webdriver-manager")

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WDM = True
except ImportError:
    HAS_WDM = False


GEMINI_URL       = "https://gemini.google.com/app"
RESPONSE_TIMEOUT = 300
STABLE_CHECKS    = 4
STABLE_INTERVAL  = 3.0
VARIANTS_ALL     = ["EN", "HI", "HG_030", "HG_065"]

INPUT_SELS = [
    "rich-textarea div[contenteditable='true']",
    "rich-textarea p",
    "div[contenteditable='true'][data-placeholder]",
    "div[contenteditable='true']",
    ".ql-editor", "textarea",
]
SEND_SELS = [
    "button[aria-label='Send message']", "button[jsname='Qx7uuf']",
    "button[data-testid='send-button']", "button[mattooltip='Send message']",
    "button[aria-label='Submit']", "button.send-button",
    "mat-icon[data-mat-icon-name='send']",
]
STOP_SELS = [
    "button[aria-label='Stop response']", "button[aria-label='Stop generating']",
    "button[aria-label='Stop']", "button[jsname='k9Ysde']",
    "button[data-testid='stop-button']", ".stop-button",
]
RESP_SELS = [
    "model-response .markdown", "model-response response-text", "model-response",
    "message-content", "[data-turn-role='model']", "[data-message-author-role='model']",
    ".response-content .markdown", ".response-content",
]

_print_lock = threading.Lock()

def log(msg: str, level: str = "INFO", tab: str = "") -> None:
    tag = f"[{tab}]" if tab else "      "
    ts  = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with _print_lock:
        print(f"[{ts}] {level:5s} {tag} {msg}", flush=True)

def utc_now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


# ── Language analysis ─────────────────────────────────────────────────────────
DEVANAGARI    = re.compile(r'[\u0900-\u097F]')
LATIN_WORD    = re.compile(r'[a-zA-Z]+')
CONF_WORDS    = ["i think","maybe","might be","possibly","perhaps",
                 "शायद","लगता है","हो सकता है","probably","not sure"]

def lang_dist(text: str) -> dict:
    words = text.split()
    if not words:
        return {"EN": 0.0, "HI": 0.0, "MATH": 0.0}
    n   = max(len(words), 1)
    en  = sum(1 for w in words if LATIN_WORD.fullmatch(w) and not w[0].isdigit())
    hi  = sum(1 for w in words if DEVANAGARI.search(w))
    num = sum(1 for w in words if re.match(r'^\d', w))
    return {"EN": round(en/n,3), "HI": round(hi/n,3), "MATH": round(num/n,3)}

def classify_lang(text: str) -> str:
    d = lang_dist(text)
    en, hi = d["EN"], d["HI"]
    if en > 0.6:                      return "EN"
    if hi > 0.6:                      return "HI"
    if en > 0.15 and hi > 0.15:       return "HINGLISH"
    if en > 0.15:                     return "EN_DOMINANT"
    if hi > 0.05:                     return "HI_WITH_EN"
    return "MIXED"

def count_switches(text: str) -> tuple:
    prev, switches = None, []
    for ch in text:
        if DEVANAGARI.match(ch):   script = "HI"
        elif ch.isalpha():         script = "EN"
        else:                      continue
        if prev and script != prev:
            switches.append(f"{prev}→{script}")
        prev = script
    deduped: list = []
    for s in switches:
        if not deduped or deduped[-1] != s:
            deduped.append(s)
    return len(deduped), deduped[:20]

def extract_answer(text: str) -> tuple:
    m = re.search(r'ANSWER\s*[:\-]\s*([^\n,;]+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip().split()[0].rstrip(".,"), "explicit_tag"
    m = re.search(r'(?:answer|ans|उत्तर|जवाब)\s*[=:is ]+\s*([\d\-./]+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "answer_phrase"
    nums = re.findall(r'(?<!\w)([\d]+(?:[,.][\d]+)?(?:/[\d]+)?)(?!\w)', text)
    if nums:
        return nums[-1].replace(",",""), "last_number"
    return "", "not_found"

def is_correct(pred: str, gold: str, tol: float = 0.01) -> bool:
    p = pred.strip().replace(",","")
    g = gold.strip().replace(",","")
    if p == g:
        return True
    def to_f(s):
        s = s.strip()
        if "/" in s:
            try:
                a, b = s.split("/",1)
                return float(a)/float(b)
            except Exception:
                return None
        try:
            return float(s)
        except Exception:
            return None
    pf, gf = to_f(p), to_f(g)
    if pf is not None and gf is not None and gf != 0:
        return abs(pf - gf)/abs(gf) <= tol
    return False


# ── Batch prompt ──────────────────────────────────────────────────────────────
def make_batch_prompt(batch: list[tuple], mode: str) -> str:
    """batch = list of (problem_id, variant, problem_text)"""
    if mode == "blind":
        instr = "Solve each math problem. Show all working. End each solution with ANSWER: <number>"
    elif mode == "instructed":
        instr = "Solve each math problem. Answer in the SAME language as the question. End with ANSWER: <number>"
    else:
        instr = "Solve each problem. Prefix each reasoning step [EN], [HI], or [MIX]. End with ANSWER: <number>"

    lines = [instr, ""]
    for i, (pid, v, txt) in enumerate(batch, 1):
        lines.append(f"--- PROBLEM {i} (ID:{pid} V:{v}) ---")
        lines.append(txt)
        lines.append("")

    lines += [
        "After solving ALL problems, output a JSON summary array at the very end:",
        '[{"id":"<id>","variant":"<v>","answer":"<numeric answer only>","reasoning_language":"EN|HI|HINGLISH|MIXED"}]',
        "Start the JSON with [ on its own line. This must be the last thing in your response.",
    ]
    return "\n".join(lines)


def parse_batch_response(raw: str, batch: list[tuple]) -> list[dict]:
    """Extract per-problem answers from a batch response."""
    results = []

    # Try JSON array at end
    depth, start, best = 0, -1, ""
    for i, ch in enumerate(raw):
        if ch == '[':
            if depth == 0: start = i
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start != -1:
                cand = raw[start:i+1]
                if len(cand) > len(best):
                    best = cand
                start = -1
    if best:
        try:
            parsed = json.loads(best)
            if isinstance(parsed, list):
                by_key = {(str(r.get("id","")), str(r.get("variant",""))): r
                          for r in parsed if isinstance(r, dict)}
                for pid, v, _ in batch:
                    r = by_key.get((pid, v)) or by_key.get((pid,"")) or {}
                    results.append({
                        "answer":             str(r.get("answer","")),
                        "reasoning_language": str(r.get("reasoning_language","")),
                        "method": "json_array",
                    })
                if len(results) == len(batch):
                    return results
        except Exception:
            pass

    # Fallback: split by problem section headers
    results = []
    sections = re.split(r"---\s*PROBLEM\s*\d+", raw, flags=re.IGNORECASE)
    prob_secs = sections[1:] if len(sections) > 1 else []
    for i, (pid, v, _) in enumerate(batch):
        sec  = prob_secs[i] if i < len(prob_secs) else raw
        ans, meth = extract_answer(sec)
        results.append({
            "answer":             ans,
            "reasoning_language": classify_lang(sec),
            "method":             f"section_{meth}",
        })

    # Final fallback: grab ANSWER: tags in order
    if not results or all(not r["answer"] for r in results):
        all_ans = re.findall(r'ANSWER\s*[:\-]\s*([^\n,;]{1,20})', raw, re.IGNORECASE)
        results = []
        for i, (pid, v, _) in enumerate(batch):
            ans = all_ans[i].strip().split()[0].rstrip(".,") if i < len(all_ans) else ""
            results.append({"answer": ans, "reasoning_language": classify_lang(raw), "method": "global"})

    return results[:len(batch)]


# ── GeminiTab (parallel-safe) ─────────────────────────────────────────────────
class GeminiTab:
    def __init__(self, driver, handle, name, lock):
        self.driver = driver
        self.handle = handle
        self.name   = name
        self.lock   = lock

    def _focus(self):
        if self.driver.current_window_handle != self.handle:
            self.driver.switch_to.window(self.handle)

    def _find_input(self, timeout=20):
        for sel in INPUT_SELS:
            try:
                return WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
            except TimeoutException:
                continue
        raise TimeoutException(f"{self.name}: input not found")

    def _type(self, el, text):
        self.driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('delete',false,null);", el,
        )
        time.sleep(0.15)
        for i in range(0, len(text), 300):
            chunk = text[i:i+300]
            safe  = chunk.replace("\\","\\\\").replace("`","\\`").replace("${","\\${")
            self.driver.execute_script(
                "document.execCommand('insertText',false,`"+safe+"`);", el)
            time.sleep(0.04)

    def _click_send(self) -> bool:
        for sel in SEND_SELS:
            els = self.driver.find_elements(By.CSS_SELECTOR, sel)
            if not els: continue
            el = els[0]
            if el.tag_name.lower() != "button":
                try: el = el.find_element(By.XPATH, "ancestor::button[1]")
                except NoSuchElementException: pass
            try:
                self.driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                try: el.click(); return True
                except Exception: pass
        return False

    def _streaming(self) -> bool:
        for sel in STOP_SELS:
            try:
                if self.driver.find_element(By.CSS_SELECTOR, sel).is_displayed():
                    return True
            except NoSuchElementException: pass
        return False

    def _last_text(self) -> str:
        for sel in RESP_SELS:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    t = els[-1].text.strip()
                    if t: return t
            except StaleElementReferenceException: continue
        return ""

    def _resp_count(self) -> int:
        for sel in RESP_SELS:
            c = len(self.driver.find_elements(By.CSS_SELECTOR, sel))
            if c: return c
        return 0

    def send(self, text: str) -> int:
        with self.lock:
            self._focus()
            before = self._resp_count()
            el     = self._find_input()
            self._type(el, text)
            time.sleep(0.6)
            if not self._click_send():
                el.send_keys(Keys.RETURN)
        log(f"Sent {len(text)} chars", "TX", self.name)
        return before

    def recv(self, before: int) -> str:
        t0 = time.time()
        while time.time()-t0 < 25:
            with self.lock:
                self._focus()
                if self._streaming() or self._resp_count() > before:
                    break
            time.sleep(0.6)

        deadline = time.time() + RESPONSE_TIMEOUT
        last_text, stable = "", 0
        while time.time() < deadline:
            time.sleep(STABLE_INTERVAL)   # ← outside lock: true parallel
            with self.lock:
                self._focus()
                live    = self._streaming()
                current = self._last_text()
            if live:
                stable=0; last_text=current; continue
            if current and current == last_text:
                stable += 1
                if stable >= STABLE_CHECKS:
                    log(f"Response ready ({len(current)} chars)", "OK", self.name)
                    return current
            else:
                stable=0; last_text=current
        log("Timeout", "WARN", self.name)
        return last_text or "[TIMED OUT]"

    def ask(self, text: str) -> str:
        return self.recv(self.send(text))


# ── ResultsWriter ──────────────────────────────────────────────────────────────
class ResultsWriter:
    def __init__(self, path: Path):
        self.path      = path
        self._lock     = threading.Lock()
        self.done_keys : set[str] = set()
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        r   = json.loads(line)
                        pid = r.get("problem_id","")
                        v   = r.get("variant","")
                        if pid and v:
                            self.done_keys.add(f"{pid}_{v}")
                    except Exception: pass
            if self.done_keys:
                log(f"Resume: {len(self.done_keys)} pairs already done")

    def write(self, record: dict):
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False)+"\n")

    def is_done(self, pid: str, v: str) -> bool:
        return f"{pid}_{v}" in self.done_keys

    def mark_done(self, pid: str, v: str):
        self.done_keys.add(f"{pid}_{v}")


# ── Build result record ────────────────────────────────────────────────────────
def build_result(problem: dict, variant: str, mode: str,
                 raw: str, ext: dict) -> dict:
    text    = problem["variants"].get(variant, {}).get("problem", "")
    gold    = problem.get("gold_answer", "")
    pred    = ext.get("answer", "")
    correct = is_correct(pred, gold)
    d       = lang_dist(raw)
    lang    = ext.get("reasoning_language") or classify_lang(raw)
    n_sw, dirs = count_switches(raw)
    off     = variant in ("HI","HG_065") and lang in ("EN","EN_DOMINANT")
    conf    = [w for w in CONF_WORDS if w in raw.lower()]
    return {
        "problem_id":         problem["id"],
        "variant":            variant,
        "mode":               mode,
        "cm_degree":          problem["variants"].get(variant, {}).get("cm_degree"),
        "problem_text":       text,
        "gold_answer":        gold,
        "gold_answer_num":    problem.get("gold_answer_num"),
        "predicted_answer":   pred,
        "is_correct":         correct,
        "answer_extraction":  ext.get("method","unknown"),
        "reasoning_language": lang,
        "lang_dist_en":       d["EN"],
        "lang_dist_hi":       d["HI"],
        "lang_dist_math":     d["MATH"],
        "language_switches":  n_sw,
        "switch_directions":  dirs,
        "off_target_lang":    off,
        "response_length":    len(raw),
        "confidence_markers": conf,
        "topic":              problem.get("topic"),
        "difficulty":         problem.get("difficulty"),
        "source_type":        problem.get("source_type"),
        "linguistic_trap":    problem.get("linguistic_trap"),
        "why_hard":           problem.get("why_hard"),
        "tags":               problem.get("tags",[]),
        "model_raw_response": raw,
        "evaluated_at":       utc_now(),
    }


# ── Per-tab worker ─────────────────────────────────────────────────────────────
def tab_worker(tab: GeminiTab, variant: str, problems: list[dict],
               writer: ResultsWriter, mode: str, batch_sz: int,
               counters: dict, c_lock: threading.Lock, total: int) -> int:
    pending = [p for p in problems
               if p["variants"].get(variant, {}).get("problem","").strip()
               and not writer.is_done(p["id"], variant)]

    if not pending:
        log(f"No pending problems for {variant}", "INFO", tab.name)
        return 0

    log(f"{len(pending)} pending for variant {variant}", "INFO", tab.name)
    contributed = 0

    for bs in range(0, len(pending), batch_sz):
        batch_probs = pending[bs:bs+batch_sz]
        batch_tuples = [
            (p["id"], variant, p["variants"][variant]["problem"])
            for p in batch_probs
        ]
        prompt = make_batch_prompt(batch_tuples, mode)
        log(f"Batch {bs//batch_sz+1} [{variant}]: {len(batch_tuples)} problems", "EVAL", tab.name)

        try:
            raw = tab.ask(prompt)
        except Exception as e:
            log(f"ask() failed: {e}", "ERR", tab.name)
            time.sleep(5)
            continue

        extracted = parse_batch_response(raw, batch_tuples)
        while len(extracted) < len(batch_probs):
            extracted.append({"answer":"","reasoning_language":"UNKNOWN","method":"missing"})

        for i, prob in enumerate(batch_probs):
            ext    = extracted[i]
            result = build_result(prob, variant, mode, raw, ext)
            writer.write(result)
            writer.mark_done(prob["id"], variant)
            contributed += 1

            with c_lock:
                counters["done"]    += 1
                counters["correct"] += int(result["is_correct"])
                done, corr = counters["done"], counters["correct"]

            mark = "✓" if result["is_correct"] else "✗"
            log(
                f"{mark} {prob['id']} [{variant}] "
                f"pred={result['predicted_answer']!r} gold={prob.get('gold_answer','?')!r} "
                f"lang={result['reasoning_language']} "
                f"[{done}/{total}] acc={100*corr/done:.1f}%",
                "OK" if result["is_correct"] else "FAIL",
                tab.name,
            )

        time.sleep(1.5)

    log(f"Done — {contributed} results for {variant}", "DONE", tab.name)
    return contributed


# ── Chrome helpers ─────────────────────────────────────────────────────────────
def attach_driver(port: int) -> webdriver.Chrome:
    log(f"Attaching to Chrome port {port}")
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    if HAS_WDM:
        try:
            return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
        except Exception as e:
            log(f"WDM failed: {e}", "WARN")
    return webdriver.Chrome(options=opts)

def open_tab(driver, url: str, label: str) -> str:
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


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="HinglishMath Evaluator — Fast Parallel")
    p.add_argument("--input",      required=True,
                   help="Path to hinglishmath_1k.jsonl (from script1). NOT results_raw.jsonl.")
    p.add_argument("--debug-port", type=int, default=9222)
    p.add_argument("--agents",     type=int, default=6,
                   help="Parallel Gemini tabs (default 6)")
    p.add_argument("--batch-size", type=int, default=4,
                   help="Problems per prompt (default 4, max 6)")
    p.add_argument("--mode",       choices=["blind","instructed","research"],
                   default="blind")
    p.add_argument("--output-dir", default="hm_results")
    p.add_argument("--limit",      type=int, default=0,
                   help="First N problems only (0=all). For testing.")
    p.add_argument("--variants",   nargs="+", default=VARIANTS_ALL,
                   choices=VARIANTS_ALL)
    args = p.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── Load + validate dataset ───────────────────────────────────────────────
    problems: list[dict] = []
    bad_lines = 0
    with Path(args.input).open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as e:
                log(f"Line {lineno}: bad JSON — {e}", "WARN")
                bad_lines += 1
                continue

            # ── GUARD: detect if user passed the wrong file ───────────────────
            if "variant" in item and "variants" not in item:
                print("\n" + "!"*60)
                print("  ERROR: Wrong input file!")
                print(f"  You passed: {args.input}")
                print("  This looks like results_raw.jsonl (evaluator output),")
                print("  NOT hinglishmath_1k.jsonl (dataset from script1).")
                print()
                print("  FIX: Pass the DATASET file:")
                print("    --input hm_dataset/hinglishmath_1k.jsonl")
                print("!"*60 + "\n")
                sys.exit(1)

            if "variants" not in item:
                log(f"Line {lineno}: missing 'variants' key. Keys: {list(item.keys())}", "WARN")
                bad_lines += 1
                continue

            if "id" not in item:
                log(f"Line {lineno}: missing 'id' key — skipping", "WARN")
                bad_lines += 1
                continue

            problems.append(item)

    if not problems:
        print("\nERROR: No valid problems loaded.")
        print(f"File: {args.input}")
        print("Make sure you are passing the dataset file from script1.")
        sys.exit(1)

    if bad_lines:
        log(f"{bad_lines} bad lines skipped", "WARN")

    if args.limit > 0:
        problems = problems[:args.limit]

    n_agents    = max(1, min(args.agents, 8))
    batch_sz    = max(1, min(args.batch_size, 6))
    n_variants  = len(args.variants)
    total_pairs = len(problems) * n_variants

    results_path = out / "results_raw.jsonl"
    writer       = ResultsWriter(results_path)
    remaining    = total_pairs - len(writer.done_keys)

    print("\n" + "═"*66)
    print(f"  HinglishMath Evaluator  |  {n_agents} tabs  |  {batch_sz} probs/batch")
    print(f"  Problems: {len(problems)}  |  Variants: {n_variants}  |  Total pairs: {total_pairs}")
    print(f"  Already done: {len(writer.done_keys)}  |  Remaining: {remaining}")
    print(f"  Mode: {args.mode}  |  Output: {results_path}")
    print("═"*66 + "\n")

    if remaining == 0:
        print("Nothing to evaluate — all pairs already done.")
        print(f"Run: python3 dataclaude/script3_analyze.py --input {results_path}")
        return

    counters = {"done": len(writer.done_keys), "correct": 0}
    c_lock   = threading.Lock()

    driver      = attach_driver(args.debug_port)
    driver_lock = threading.Lock()
    tabs: list[GeminiTab] = []
    for i in range(n_agents):
        lbl    = f"EVAL{i+1}"
        handle = open_tab(driver, GEMINI_URL, lbl)
        time.sleep(3.5)
        tabs.append(GeminiTab(driver, handle, lbl, driver_lock))

    # Assign variants round-robin across tabs
    # 6 tabs, 4 variants → EVAL1=EN, EVAL2=HI, EVAL3=HG030, EVAL4=HG065, EVAL5=EN, EVAL6=HI
    assignments = [(tabs[i], args.variants[i % n_variants]) for i in range(n_agents)]
    for tab, variant in assignments:
        log(f"Assigned variant: {variant}", "INFO", tab.name)

    log("All tabs ready. Firing ALL in parallel now!", "START")
    print()

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n_agents) as pool:
        futures = {
            pool.submit(
                tab_worker, tab, variant, problems, writer,
                args.mode, batch_sz, counters, c_lock, total_pairs,
            ): (tab.name, variant)
            for tab, variant in assignments
        }
        for future in as_completed(futures):
            name, variant = futures[future]
            try:
                c = future.result()
                log(f"Thread done — {c} results for {variant}", "DONE", name)
            except Exception as e:
                log(f"Thread crashed [{variant}]: {e}", "ERR", name)

    elapsed = time.time() - t0

    # Final summary
    by_variant: dict = defaultdict(lambda: {"total":0,"correct":0})
    with results_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                r = json.loads(line)
                v = r.get("variant","?")
                by_variant[v]["total"]   += 1
                by_variant[v]["correct"] += int(r.get("is_correct",False))
            except Exception: pass

    print("\n" + "═"*66)
    print(f"  DONE  |  {elapsed/60:.1f} min  |  {results_path.resolve()}")
    print()
    print(f"  {'Variant':<12} {'N':>6} {'Correct':>8} {'Accuracy':>10}")
    print(f"  {'─'*40}")
    for v in VARIANTS_ALL:
        d = by_variant.get(v)
        if d and d["total"]:
            print(f"  {v:<12} {d['total']:>6} {d['correct']:>8} {100*d['correct']/d['total']:>9.1f}%")
    print("═"*66)
    print(f"\n  Next step: python3 dataclaude/script3_analyze.py --input {results_path}\n")

    (out/"eval_summary.json").write_text(json.dumps({
        "total_evaluated": counters["done"],
        "elapsed_s":       round(elapsed,1),
        "mode":            args.mode,
        "by_variant":      {v:d for v,d in by_variant.items()},
        "done_at":         utc_now(),
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()