"""
script1_generate_dataset.py  —  HinglishMath-1K Dataset Generator
==================================================================

PURPOSE
-------
Generates a research-grade dataset of 1,000 Hinglish math problems
structured as CONTROLLED PAIRS (English / Pure-Hindi / Light-Hinglish /
Heavy-Hinglish) to prove the HinglishMath hypothesis:

  "Code-mixed Hinglish inputs cause measurable accuracy degradation in
   state-of-the-art LLMs on multi-step mathematical reasoning tasks."

DATASET SCHEMA (one JSON object per problem)
--------------------------------------------
{
  "id":               "HM-0001",
  "topic":            "Work & Time",
  "difficulty":       "JEE-Advanced",
  "source_type":      "rotation_work | probability | algebra | ...",

  "variants": {
    "EN":      { "problem": "...", "cm_degree": 0.0 },
    "HI":      { "problem": "...", "cm_degree": 0.0 },  # pure Devanagari
    "HG_030":  { "problem": "...", "cm_degree": 0.3 },  # light Hinglish
    "HG_065":  { "problem": "...", "cm_degree": 0.65 }, # heavy Hinglish
    "HG_NAT":  { "problem": "...", "cm_degree": null }  # natural scraped style
  },

  "gold_answer":      "180",          # exact string to match
  "gold_answer_num":  180.0,          # float for fuzzy match
  "unit":             "km",
  "solution_steps":   [...],          # ground-truth step-by-step
  "linguistic_traps": [...],          # documented traps per variant
  "tags":             ["speed-distance", "indian-context"]
}

HOW IT WORKS
------------
- Opens 4 parallel Gemini tabs (or fewer if --agents < 4)
- Each tab generates BATCHES of 15 problems via a carefully engineered prompt
- Immediately persists each batch to disk (crash-safe)
- Deduplicates by gold_answer + topic fingerprint
- Produces final hinglishmath_1k.jsonl (one problem per line)

USAGE
-----
  # Start Chrome first:
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-hm \
    --headless=new

  python script1_generate_dataset.py \
    --debug-port 9222 \
    --agents 4 \
    --target 1000 \
    --output-dir hm_dataset

INSTALL
-------
  pip install selenium webdriver-manager

OUTPUT
------
  hm_dataset/
    batches/          raw batch JSON files (crash recovery)
    hinglishmath_1k.jsonl   final merged dataset
    generation_log.json     provenance + stats
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
import hashlib
from pathlib import Path
from typing import Any, Optional

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


# ─── Config ──────────────────────────────────────────────────────────────────
RESPONSE_TIMEOUT = 240
STABLE_CHECKS    = 4
STABLE_INTERVAL  = 3.0
GEMINI_URL       = "https://gemini.google.com/app"

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
    "model-response .markdown", "model-response response-text",
    "model-response", "message-content",
    "[data-turn-role='model']", "[data-message-author-role='model']",
    "message-content .markdown", ".response-content .markdown",
    ".response-content",
]


# ─── Topics pool (67 unique types for diversity) ──────────────────────────────
TOPIC_POOL = [
    # Arithmetic
    ("Work & Time",           "JEE-Main",     "rotation_work"),
    ("Work & Time",           "JEE-Advanced", "variable_efficiency"),
    ("Pipes & Cisterns",      "JEE-Main",     "phase_change_flow"),
    ("Speed, Distance, Time", "JEE-Main",     "relative_speed"),
    ("Speed, Distance, Time", "JEE-Advanced", "multi_leg_journey"),
    ("Mixture & Alligation",  "JEE-Main",     "two_vessels"),
    ("Mixture & Alligation",  "JEE-Advanced", "repeated_dilution"),
    ("Percentage",            "Grade-12",     "chain_percentage"),
    ("Profit & Loss",         "Grade-12",     "successive_discount"),
    ("Simple Interest",       "Grade-12",     "compare_si"),
    ("Compound Interest",     "JEE-Main",     "semi_annual"),
    ("Compound Interest",     "JEE-Advanced", "emi_reverse"),
    # Algebra
    ("Quadratic Equations",   "JEE-Main",     "vieta_formulas"),
    ("Quadratic Equations",   "JEE-Advanced", "roots_transformation"),
    ("Sequences & Series",    "JEE-Main",     "ap_gp_combined"),
    ("Sequences & Series",    "JEE-Advanced", "recursive_sequence"),
    ("Logarithms",            "JEE-Main",     "nested_log"),
    ("Logarithms",            "JEE-Advanced", "log_equation_system"),
    ("Complex Numbers",       "JEE-Main",     "modulus_argument"),
    ("Complex Numbers",       "JEE-Advanced", "de_moivre"),
    ("Polynomials",           "JEE-Advanced", "remainder_theorem"),
    # Combinatorics
    ("Permutations",          "JEE-Main",     "circular_arrangement"),
    ("Combinations",          "JEE-Main",     "selection_constraint"),
    ("Combinatorics",         "JEE-Advanced", "inclusion_exclusion"),
    ("Probability",           "JEE-Main",     "conditional_probability"),
    ("Probability",           "JEE-Advanced", "bayes_theorem"),
    ("Probability",           "JEE-Advanced", "multi_stage_tree"),
    # Geometry & Calculus
    ("Coordinate Geometry",   "JEE-Main",     "line_circle_intersection"),
    ("Coordinate Geometry",   "JEE-Advanced", "parabola_tangent"),
    ("Trigonometry",          "JEE-Main",     "height_distance"),
    ("Trigonometry",          "JEE-Advanced", "trig_identity_chain"),
    ("Calculus - Diff",       "JEE-Main",     "chain_rule"),
    ("Calculus - Diff",       "JEE-Advanced", "implicit_differentiation"),
    ("Calculus - Integ",      "JEE-Main",     "definite_integral"),
    ("Calculus - Integ",      "JEE-Advanced", "area_between_curves"),
    # Indian-context financial
    ("Partnership",           "Grade-12",     "variable_capital"),
    ("GST & Taxation",        "Grade-12",     "itc_chain"),
    ("Ratio & Proportion",    "Grade-12",     "lakh_crore_notation"),
    ("Time Value of Money",   "Grade-12",     "npv_comparison"),
    ("Statistics",            "Grade-12",     "mean_median_mode"),
    # Number Theory (Codeforces-style)
    ("Number Theory",         "Codeforces-B", "divisibility_count"),
    ("Number Theory",         "Codeforces-C", "prime_factorisation"),
    ("Modular Arithmetic",    "Codeforces-C", "mod_inverse"),
    ("Modular Arithmetic",    "Codeforces-D", "crt_system"),
    ("GCD & LCM",             "Codeforces-B", "lcm_word_problem"),
    ("Bit Manipulation",      "Codeforces-C", "xor_sum"),
    # Combinatorics (Codeforces-style)
    ("Counting Paths",        "Codeforces-C", "grid_dp"),
    ("Game Theory",           "Codeforces-D", "nim_variant"),
    ("Sequences",             "Codeforces-D", "dp_recurrence"),
    # Physics-styled (JEE)
    ("Kinematics",            "JEE-Advanced", "relative_projectile"),
    ("Electricity",           "JEE-Advanced", "kirchhoff_laws"),
]


def now_str() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def utc_now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

def log(msg: str, level: str = "INFO", tab: str = "") -> None:
    tag = f"[{tab}]" if tab else "      "
    print(f"[{now_str()}] {level:5s} {tag} {msg}", flush=True)


# ─── Core prompt engineering ───────────────────────────────────────────────────

GENERATION_SYSTEM = """You are a research mathematician designing a SCIENTIFIC BENCHMARK to study how code-mixing in Hinglish (Hindi-English mix) affects LLM mathematical reasoning. Your job is to generate mathematically HARD problems in a specific format.

RESEARCH GOAL: We want problems where top LLMs (GPT-4o, Gemini) might give WRONG answers when the problem is in Hinglish but CORRECT answers when in English — due to language-encoding failures, not math difficulty alone.

HARDNESS CRITERIA (problems must satisfy ALL):
1. Multi-step reasoning (minimum 4 algebraic steps)
2. At least one linguistic trap specific to Hinglish/Hindi
3. Numeric final answer that can be verified exactly
4. NOT solvable by simple pattern matching or memorisation
5. Would score on Codeforces Div2-C or JEE-Advanced level

LINGUISTIC TRAPS TO INCLUDE (use different traps for each problem):
- Indian number notation: "3 lakh 75 hazaar" = 3,75,000
- "Aur" ambiguity: "10 aur log" = 10 MORE people (additive, not separate)
- "Na...na" double negation: "na 2 se na 3 se divisible" = neither, not or
- Passive voice in Hinglish: "band kar diya gaya" — by whom? when?
- "Zyada" direction: "A se 20% zyada" could be A+20% or confused as A-20%
- "Pehle...phir" ordering: sequence of operations
- "Baaki" = remainder after subtraction (often ignored)
- "Withdraw kar liya" timing: when exactly in a compound interest calculation
- Implicit unit conversion: "6 mahine" embedded in annual rate problem
- "Sirf" = only/exactly (changes inclusion-exclusion logic)
"""


def make_batch_prompt(topic: str, difficulty: str, source_type: str, batch_idx: int) -> str:
    return f"""
{GENERATION_SYSTEM}

NOW GENERATE: Exactly 15 math problems on the topic below.

TOPIC: {topic}
DIFFICULTY: {difficulty}
PROBLEM TYPE: {source_type}
BATCH ID: {batch_idx}  (use this to ensure uniqueness — vary the numbers!)

OUTPUT FORMAT — Return ONLY a valid JSON array, no markdown fences, no explanation:

[
  {{
    "problem_en": "Full problem in clear, standard English. Hard. Multi-step.",
    "problem_hi": "Same problem in pure Hindi (Devanagari script only, no Roman letters except mathematical symbols like km, %, ₹, x, y).",
    "problem_hg_030": "Same problem in light Hinglish (30% English words mixed in naturally at syntactic switch points). Keep math terms in English.",
    "problem_hg_065": "Same problem in heavy Hinglish (65% code-mixed). Include at least ONE of the linguistic traps listed above. The trap must be subtle, not obvious.",
    "gold_answer": "exact numeric answer as a string (e.g. '180' or '37/70' or '123')",
    "gold_answer_num": 180.0,
    "unit": "km or hours or rupees or dimensionless etc",
    "topic": "{topic}",
    "difficulty": "{difficulty}",
    "source_type": "{source_type}",
    "solution_steps_en": [
      "Step 1: ...",
      "Step 2: ...",
      "Step 3: ...",
      "Step 4: ... (final answer)"
    ],
    "linguistic_trap_in_hg065": "describe exactly what the trap is and how it could mislead an LLM",
    "why_hard": "1-sentence explanation of what makes this hard for an LLM specifically",
    "tags": ["tag1", "tag2"]
  }}
]

CRITICAL RULES:
- Every gold_answer must be VERIFIABLE — check your own arithmetic
- ALL 15 problems must be on DIFFERENT specific scenarios (different numbers, different setups)
- The HG_065 variant MUST contain a real linguistic trap (not fake)
- Do NOT include explanatory text outside the JSON array
- Do NOT use markdown code fences
- The JSON must parse with json.loads() directly
"""


# ─── Selenium helpers (same pattern as mainpp.py) ─────────────────────────────

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
    try:
        driver.switch_to.new_window("tab")
        after = set(driver.window_handles)
        new_h = list(after - before)
        if new_h:
            driver.switch_to.window(new_h[0])
            driver.get(url)
            log(f"Opened tab {label}", "OK")
            return new_h[0]
    except Exception as e:
        log(f"new_window failed: {e}", "WARN")
    raise RuntimeError(f"Cannot open tab for {label}")


class GeminiTab:
    def __init__(self, driver: webdriver.Chrome, handle: str, name: str):
        self.driver = driver
        self.handle = handle
        self.name = name

    def focus(self) -> None:
        if self.driver.current_window_handle != self.handle:
            self.driver.switch_to.window(self.handle)

    def _find(self, selectors, timeout=15, what="element"):
        for sel in selectors:
            try:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                return el
            except TimeoutException:
                continue
        raise TimeoutException(f"{self.name}: no match for {what}")

    def _type_text(self, el, text: str) -> None:
        self.driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('delete',false,null);", el,
        )
        time.sleep(0.2)
        for i in range(0, len(text), 300):
            chunk = text[i:i+300]
            safe  = chunk.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
            self.driver.execute_script(
                "document.execCommand('insertText',false,`" + safe + "`);", el,
            )
            time.sleep(0.05)

    def _click_send(self) -> bool:
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
                    continue
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

    def send(self, text: str) -> None:
        self.focus()
        log(f"Sending {len(text)} chars", "TX", self.name)
        el = self._find(INPUT_SELS, timeout=20, what="input")
        self._type_text(el, text)
        time.sleep(0.8)
        if not self._click_send():
            log("Send button not found — trying Enter", "WARN", self.name)
            el.send_keys(Keys.RETURN)

    def recv(self) -> str:
        self.focus()
        log("Waiting for response …", "RX", self.name)
        before = self._resp_count()
        t0 = time.time()
        while time.time() - t0 < 20:
            if self._streaming() or self._resp_count() > before:
                break
            time.sleep(0.5)

        deadline  = time.time() + RESPONSE_TIMEOUT
        last_text = ""
        stable    = 0
        while time.time() < deadline:
            time.sleep(STABLE_INTERVAL)
            live    = self._streaming()
            current = self._last_text()
            if live:
                stable = 0
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


# ─── JSON extraction ───────────────────────────────────────────────────────────

def extract_json_array(text: str) -> list[dict]:
    """Try multiple strategies to extract a JSON array from model output."""
    if not text:
        return []
    stripped = text.strip()
    candidates = [stripped]

    # Remove markdown fences
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidates.extend(c.strip() for c in fenced if c.strip())

    # Extract largest [...] block
    depth, start, best = 0, -1, ""
    for i, ch in enumerate(stripped):
        if ch == '[':
            if depth == 0:
                start = i
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start != -1:
                candidate = stripped[start:i+1]
                if len(candidate) > len(best):
                    best = candidate
                start = -1
    if best:
        candidates.append(best)

    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict)]
        except Exception:
            continue

    return []


def fingerprint(item: dict) -> str:
    key = f"{item.get('topic','')}{item.get('gold_answer','')}{item.get('source_type','')}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ─── Main runner ──────────────────────────────────────────────────────────────

class DatasetGenerator:
    def __init__(self, args):
        self.args  = args
        self.out   = Path(args.output_dir)
        self.batch = self.out / "batches"
        self.out.mkdir(parents=True, exist_ok=True)
        self.batch.mkdir(parents=True, exist_ok=True)
        self.driver: Optional[webdriver.Chrome] = None
        self.tabs:   list[GeminiTab]            = []
        self.all_items: list[dict]              = []
        self.seen_fps: set[str]                 = set()
        self.total_generated: int               = 0

    def setup(self) -> None:
        self.driver = attach_driver(self.args.debug_port)
        for i in range(self.args.agents):
            lbl    = f"GEN{i+1}"
            handle = open_tab(self.driver, GEMINI_URL, lbl)
            time.sleep(4)
            self.tabs.append(GeminiTab(self.driver, handle, lbl))

    def _save_batch(self, items: list[dict], batch_id: str) -> None:
        p = self.batch / f"batch_{batch_id}.json"
        p.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    def _flush_jsonl(self) -> None:
        out_path = self.out / "hinglishmath_1k.jsonl"
        with out_path.open("w", encoding="utf-8") as f:
            for item in self.all_items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        log(f"Flushed {len(self.all_items)} items → {out_path}", "SAVE")

    def _enrich_item(self, raw: dict, idx: int) -> dict:
        """Add research-required fields to raw item."""
        return {
            "id":          f"HM-{idx:04d}",
            "variants": {
                "EN":     {"problem": raw.get("problem_en",     ""), "cm_degree": 0.0},
                "HI":     {"problem": raw.get("problem_hi",     ""), "cm_degree": 0.0},
                "HG_030": {"problem": raw.get("problem_hg_030", ""), "cm_degree": 0.3},
                "HG_065": {"problem": raw.get("problem_hg_065", ""), "cm_degree": 0.65},
            },
            "gold_answer":       str(raw.get("gold_answer",     "")),
            "gold_answer_num":   float(raw.get("gold_answer_num", 0)),
            "unit":              raw.get("unit",             ""),
            "topic":             raw.get("topic",            ""),
            "difficulty":        raw.get("difficulty",       ""),
            "source_type":       raw.get("source_type",      ""),
            "solution_steps_en": raw.get("solution_steps_en", []),
            "linguistic_trap":   raw.get("linguistic_trap_in_hg065", ""),
            "why_hard":          raw.get("why_hard",         ""),
            "tags":              raw.get("tags",              []),
            "generated_at":      utc_now(),
        }

    def run(self) -> None:
        self.setup()
        target   = self.args.target
        n_agents = len(self.tabs)
        batch_no = 0

        # Round-robin through topics
        topic_idx = 0

        while self.total_generated < target:
            tab = self.tabs[batch_no % n_agents]
            topic, diff, src = TOPIC_POOL[topic_idx % len(TOPIC_POOL)]
            topic_idx += 1
            batch_no  += 1

            log(f"Batch {batch_no} | {topic} | {diff}", "BATCH", tab.name)
            prompt = make_batch_prompt(topic, diff, src, batch_no)
            tab.send(prompt)
            raw_text = tab.recv()

            items = extract_json_array(raw_text)
            if not items:
                log(f"Batch {batch_no}: JSON parse failed — retrying with repair", "WARN", tab.name)
                repair = (
                    "The previous response was not valid JSON. "
                    "Return ONLY the JSON array, no text before or after, "
                    "no markdown fences. Start with [ and end with ]."
                )
                tab.send(repair)
                raw_text = tab.recv()
                items = extract_json_array(raw_text)

            added = 0
            for raw_item in items:
                if self.total_generated >= target:
                    break
                fp = fingerprint(raw_item)
                if fp in self.seen_fps:
                    continue
                self.seen_fps.add(fp)
                idx      = self.total_generated + 1
                enriched = self._enrich_item(raw_item, idx)
                self.all_items.append(enriched)
                self.total_generated += 1
                added += 1

            self._save_batch(items, f"{batch_no:04d}_{tab.name}")
            self._flush_jsonl()
            log(f"Batch {batch_no}: +{added} items | Total: {self.total_generated}/{target}", "OK", tab.name)

            # Small delay between batches to avoid rate limits
            time.sleep(2)

        # Final summary
        gen_log = {
            "total_generated": self.total_generated,
            "batches":         batch_no,
            "topics_used":     topic_idx,
            "completed_at":    utc_now(),
            "output_file":     str(self.out / "hinglishmath_1k.jsonl"),
        }
        (self.out / "generation_log.json").write_text(
            json.dumps(gen_log, indent=2), encoding="utf-8"
        )
        print(f"\n✓ Done! {self.total_generated} problems → {self.out}/hinglishmath_1k.jsonl")


def main():
    p = argparse.ArgumentParser(description="HinglishMath-1K Dataset Generator")
    p.add_argument("--debug-port", type=int, default=9222)
    p.add_argument("--agents",     type=int, default=4, help="Parallel Gemini tabs")
    p.add_argument("--target",     type=int, default=1000, help="Total problems to generate")
    p.add_argument("--output-dir", default="hm_dataset")
    args = p.parse_args()
    gen  = DatasetGenerator(args)
    try:
        gen.run()
    except KeyboardInterrupt:
        print(f"\nInterrupted. Saved {gen.total_generated} problems so far.")
        gen._flush_jsonl()

if __name__ == "__main__":
    main()