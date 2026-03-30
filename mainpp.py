"""
mainpp.py - Standalone Gemini Hinglish Math Dataset Generator
==============================================================

What this script does
---------------------
This script autonomously generates a JSON dataset of Hinglish math tasks using
Gemini web UI automation through Selenium.

For each cycle and each agent tab:
1) Generate N Hinglish math word problems with numeric gold answers
2) Solve each problem using a fixed Hinglish teaching system prompt
3) Persist each solved item immediately to problems.json (crash-safe)
4) Persist run progress to history.json and final summary.json

How this script is built
------------------------
This is a standalone implementation. It does NOT import runtime helpers from
main.py or any other project script.

Included inside this file:
- Chrome attach logic (DevTools debuggerAddress mode)
- New tab opening logic (new_window + fallback)
- GeminiTab abstraction (send, receive, probing, extraction)
- JSON extraction and repair prompting
- Autonomous orchestration loop
- Incremental dataset persistence

Dependencies
------------
Install once:
  pip install selenium webdriver-manager

Prerequisite: start Chrome with remote debugging
-----------------------------------------------
macOS example:
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-gemini-headless \
    --headless=new

Then run:
  python mainpp.py --debug-port 9222 --agents 4 --cycles 30 --problems-per-cycle 10

Output files (default folder: mainpp_output)
--------------------------------------------
- problems.json : incremental dataset items
- history.json  : full cycle/agent state
- summary.json  : run summary
- run_*.log     : runtime logs
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
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
        TimeoutException,
        NoSuchElementException,
        StaleElementReferenceException,
        WebDriverException,
    )
except ImportError:
    sys.exit("ERROR: pip install selenium webdriver-manager")

try:
    from webdriver_manager.chrome import ChromeDriverManager

    HAS_WDM = True
except ImportError:
    HAS_WDM = False


DEBUG_PORT = 9222
GEMINI_URL = "https://gemini.google.com/app"
DEFAULT_OUTPUT_DIR = Path("mainpp_output")

DEFAULT_AGENTS = 4
DEFAULT_CYCLES = 30
DEFAULT_PROBLEMS_PER_CYCLE = 10

RESPONSE_TIMEOUT = 180
STABLE_CHECKS = 4
STABLE_INTERVAL = 2.5

SYSTEM_PROMPT = """You are a math teacher who teaches in Hinglish
(code-mixed Hindi-English). Generate a step-by-step solution
to math problems where:
- Mathematical notation, numbers, formulas stay in English
- Problem comprehension steps are in Hindi (Devanagari script)
- Intermediate reasoning can mix both
- Final answer is numeric only

For each step, also output a language tag: EN, HI, MIXED, MATH

Output ONLY valid JSON, no other text."""


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


LOG_PATH: Optional[Path] = None


def now_clock() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def log(msg: str, level: str = "INFO", tab: str = "") -> None:
    tag = f"[{tab}] " if tab else ""
    line = f"[{now_clock()}] {level} {tag}{msg}"
    print(line, flush=True)
    if LOG_PATH:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def hr(title: str = "", width: int = 72, ch: str = "-") -> None:
    if title:
        padding = max(0, width - len(title) - 2)
        print("\n" + (ch * (padding // 2)) + f" {title} " + (ch * (padding - padding // 2)) + "\n")
    else:
        print(ch * width)


def attach_driver(port: int) -> webdriver.Chrome:
    log(f"Attaching to Chrome on localhost:{port}")
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")

    last_error: Optional[Exception] = None

    if HAS_WDM:
        try:
            drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
            log(f"Attached via webdriver-manager. Chrome {drv.capabilities.get('browserVersion', '?')}", "OK")
            return drv
        except Exception as exc:
            last_error = exc
            log(f"webdriver-manager attach failed: {exc}", "WARN")

    try:
        drv = webdriver.Chrome(options=opts)
        log("Attached via system chromedriver", "OK")
        return drv
    except Exception as exc:
        last_error = exc

    hr("ATTACH FAILED", ch="=")
    print(f"Last error: {last_error}")
    print("Start Chrome with --remote-debugging-port and retry.")
    sys.exit(1)


def open_tab(driver: webdriver.Chrome, url: str, label: str) -> str:
    before = set(driver.window_handles)

    try:
        driver.switch_to.new_window("tab")
        after = set(driver.window_handles)
        new_handles = after - before
        if new_handles:
            handle = list(new_handles)[0]
            driver.switch_to.window(handle)
            driver.get(url)
            log(f"[{label}] opened tab via new_window", "OK")
            return handle
    except Exception as exc:
        log(f"[{label}] new_window failed: {exc}", "WARN")

    try:
        driver.execute_cdp_cmd("Target.createTarget", {"url": url})
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(0.4)
            after = set(driver.window_handles)
            new_handles = after - before
            if new_handles:
                handle = list(new_handles)[0]
                driver.switch_to.window(handle)
                log(f"[{label}] opened tab via CDP", "OK")
                return handle
    except Exception as exc:
        log(f"[{label}] CDP open failed: {exc}", "WARN")

    raise RuntimeError(f"Failed to open a tab for {label}")


def probe_dom(driver: webdriver.Chrome, label: str) -> None:
    hr(f"DOM PROBE {label}", ch="=")
    for group, selectors in [
        ("INPUT", INPUT_SELS),
        ("SEND", SEND_SELS),
        ("STOP", STOP_SELS),
        ("RESP", RESP_SELS),
    ]:
        log(f"{group} selectors", "INFO", label)
        for selector in selectors:
            try:
                count = len(driver.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                count = 0
            mark = "FOUND" if count else "MISS"
            log(f"{mark} ({count}) {selector}", "DBG", label)


class GeminiTab:
    def __init__(self, driver: webdriver.Chrome, handle: str, name: str):
        self.driver = driver
        self.handle = handle
        self.name = name

    def focus(self) -> None:
        try:
            if self.driver.current_window_handle != self.handle:
                self.driver.switch_to.window(self.handle)
        except WebDriverException as exc:
            log(f"Lost session: {exc}", "ERR", self.name)
            raise

    def probe(self) -> None:
        self.focus()
        probe_dom(self.driver, self.name)

    def _find(self, selectors: list[str], timeout: int, what: str):
        for selector in selectors:
            try:
                element = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                log(f"Found {what} via {selector}", "DBG", self.name)
                return element
            except TimeoutException:
                continue
        raise TimeoutException(f"{self.name}: no selector matched for {what}")

    def _type_text(self, element, text: str) -> None:
        self.driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('delete',false,null);",
            element,
        )
        time.sleep(0.15)

        for i in range(0, len(text), 400):
            chunk = text[i : i + 400]
            safe = chunk.replace("\\", "\\\\").replace("`", "\\`")
            self.driver.execute_script(
                "document.execCommand('insertText',false,`" + safe + "`);",
                element,
            )
            time.sleep(0.04)

    def _click_send(self) -> bool:
        for selector in SEND_SELS:
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if not elements:
                continue
            element = elements[0]
            if element.tag_name.lower() != "button":
                try:
                    element = element.find_element(By.XPATH, "ancestor::button[1]")
                except NoSuchElementException:
                    pass
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                try:
                    element.click()
                    return True
                except Exception:
                    continue
        return False

    def _streaming(self) -> bool:
        for selector in STOP_SELS:
            try:
                if self.driver.find_element(By.CSS_SELECTOR, selector).is_displayed():
                    return True
            except NoSuchElementException:
                pass
        return False

    def _last_text(self) -> str:
        for selector in RESP_SELS:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    text = elements[-1].text.strip()
                    if text:
                        return text
            except StaleElementReferenceException:
                continue
        return ""

    def _resp_count(self) -> int:
        for selector in RESP_SELS:
            count = len(self.driver.find_elements(By.CSS_SELECTOR, selector))
            if count:
                return count
        return 0

    def send(self, text: str) -> None:
        self.focus()
        log(f"Sending {len(text)} chars", "TX", self.name)
        element = self._find(INPUT_SELS, timeout=15, what="input")
        self._type_text(element, text)
        time.sleep(0.6)

        if not self._click_send():
            log("Send button not found, trying Enter", "WARN", self.name)
            element.send_keys(Keys.RETURN)

    def recv(self) -> str:
        self.focus()
        log("Waiting for response", "RX", self.name)

        before_count = self._resp_count()

        started = False
        t0 = time.time()
        while time.time() - t0 < 15:
            if self._streaming() or self._resp_count() > before_count:
                started = True
                break
            time.sleep(0.5)
        if not started:
            log("Stream start not detected", "WARN", self.name)

        deadline = time.time() + RESPONSE_TIMEOUT
        last_text = ""
        stable = 0

        while time.time() < deadline:
            time.sleep(STABLE_INTERVAL)
            live = self._streaming()
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
                stable = 0
                last_text = current

        log("Response timeout, returning best effort", "WARN", self.name)
        return last_text or "[RESPONSE TIMED OUT]"


def extract_json_value(text: str) -> Optional[Any]:
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    fenced = re.findall(r"```(?:json)?\\s*([\\s\\S]*?)```", text, flags=re.IGNORECASE)
    candidates.extend([chunk.strip() for chunk in fenced if chunk.strip()])

    first_arr = stripped.find("[")
    last_arr = stripped.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        candidates.append(stripped[first_arr : last_arr + 1])

    first_obj = stripped.find("{")
    last_obj = stripped.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        candidates.append(stripped[first_obj : last_obj + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue

    return None


class MainPPRunner:
    def __init__(
        self,
        debug_port: int,
        agents: int,
        cycles: int,
        problems_per_cycle: int,
        output_dir: Path,
        agent_start_index: int,
    ) -> None:
        self.debug_port = debug_port
        self.agents = agents
        self.cycles = cycles
        self.problems_per_cycle = problems_per_cycle
        self.output_dir = output_dir
        self.agent_start_index = agent_start_index

        self.output_dir.mkdir(parents=True, exist_ok=True)

        global LOG_PATH
        LOG_PATH = self.output_dir / ("run_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")

        self.driver: Optional[webdriver.Chrome] = None
        self.tabs: list[GeminiTab] = []
        self.problems_path = self.output_dir / "problems.json"
        self.state: dict[str, Any] = {
            "started_at": utc_now(),
            "config": {
                "debug_port": debug_port,
                "agents": agents,
                "cycles": cycles,
                "problems_per_cycle": problems_per_cycle,
                "agent_start_index": agent_start_index,
            },
            "cycles": [],
        }

        if not self.problems_path.exists():
            self.problems_path.write_text(
                json.dumps(
                    {
                        "started_at": utc_now(),
                        "config": self.state["config"],
                        "items": [],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    def _save_state(self) -> None:
        (self.output_dir / "history.json").write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _append_problem_item(self, item: dict[str, Any]) -> None:
        try:
            current = json.loads(self.problems_path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                current = {"started_at": utc_now(), "config": self.state["config"], "items": []}
        except Exception:
            current = {"started_at": utc_now(), "config": self.state["config"], "items": []}

        rows = current.get("items")
        if not isinstance(rows, list):
            rows = []

        rows.append(item)
        current["items"] = rows
        current["updated_at"] = utc_now()

        self.problems_path.write_text(
            json.dumps(current, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def setup(self) -> None:
        hr("MAINPP AUTONOMOUS LOOP", ch="=")
        self.driver = attach_driver(self.debug_port)

        for idx in range(self.agent_start_index, self.agent_start_index + self.agents):
            label = f"AGENT{idx}"
            log(f"Opening {label} tab", "INFO")
            handle = open_tab(self.driver, GEMINI_URL, label)
            time.sleep(3)
            tab = GeminiTab(self.driver, handle, label)
            tab.probe()
            self.tabs.append(tab)

    def _repair_list_of_problems(self, tab: GeminiTab, raw: str) -> list[dict[str, Any]]:
        parsed = extract_json_value(raw)
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]

        repair_prompt = (
            "Convert the following content into strict JSON array only. "
            "Each item must have: hinglish_problem (string), gold_answer (number/string).\n\n"
            "CONTENT:\n" + raw
        )
        tab.send(repair_prompt)
        repaired = tab.recv()

        repaired_parsed = extract_json_value(repaired)
        if isinstance(repaired_parsed, list):
            return [row for row in repaired_parsed if isinstance(row, dict)]

        return []

    def _repair_solution_object(self, tab: GeminiTab, raw: str) -> dict[str, Any]:
        parsed = extract_json_value(raw)
        if isinstance(parsed, dict):
            return parsed

        repair_prompt = (
            "Convert the following response into ONE strict JSON object only.\n"
            "CONTENT:\n" + raw
        )
        tab.send(repair_prompt)
        repaired = tab.recv()

        repaired_parsed = extract_json_value(repaired)
        if isinstance(repaired_parsed, dict):
            return repaired_parsed

        return {
            "raw_response": raw,
            "parse_status": "failed",
        }

    def _problem_generation_prompt(self, cycle_idx: int, agent_name: str) -> str:
        return (
            "You are generating practice problems for Hinglish math tutoring.\n"
            f"Generate exactly {self.problems_per_cycle} unique math word problems in Hinglish for cycle {cycle_idx}, agent {agent_name}.\n"
            "Constraints:\n"
            "- Problem text can be code-mixed Hindi-English\n"
            "- Include topics from arithmetic/algebra/percentages/ratios/geometry\n"
            "- Every problem must have a deterministic numeric final answer\n"
            "Return ONLY valid JSON array (no markdown) with this schema per item:\n"
            "{\"hinglish_problem\": string, \"gold_answer\": number or numeric string}"
        )

    def run(self) -> None:
        self.setup()
        self._save_state()

        for cycle_idx in range(1, self.cycles + 1):
            hr(f"CYCLE {cycle_idx}", ch="-")
            cycle_record: dict[str, Any] = {
                "cycle": cycle_idx,
                "started_at": utc_now(),
                "agents": [],
            }

            for tab in self.tabs:
                agent_record: dict[str, Any] = {
                    "agent": tab.name,
                    "started_at": utc_now(),
                    "problems": [],
                }

                gen_prompt = self._problem_generation_prompt(cycle_idx, tab.name)
                tab.send(gen_prompt)
                raw_problem_set = tab.recv()
                problems = self._repair_list_of_problems(tab, raw_problem_set)
                problems = problems[: self.problems_per_cycle]

                log(
                    f"{tab.name} generated {len(problems)} candidate problems in cycle {cycle_idx}",
                    "OK",
                    tab.name,
                )

                for p_idx, item in enumerate(problems, start=1):
                    hinglish_problem = str(item.get("hinglish_problem", "")).strip()
                    gold_answer = item.get("gold_answer", "")
                    if not hinglish_problem:
                        continue

                    user_prompt = (
                        f"Problem: {hinglish_problem}\n"
                        f"Known correct answer: {gold_answer}\n"
                        "Generate step-by-step Hinglish solution leading to this answer."
                    )
                    full_prompt = SYSTEM_PROMPT + "\n\n" + user_prompt

                    tab.send(full_prompt)
                    raw_solution = tab.recv()
                    solution_obj = self._repair_solution_object(tab, raw_solution)

                    problem_item = {
                        "cycle": cycle_idx,
                        "agent": tab.name,
                        "index": p_idx,
                        "hinglish_problem": hinglish_problem,
                        "gold_answer": gold_answer,
                        "solution": solution_obj,
                        "generated_at": utc_now(),
                    }

                    agent_record["problems"].append(problem_item)
                    self._append_problem_item(problem_item)
                    self._save_state()

                agent_record["completed_at"] = utc_now()
                cycle_record["agents"].append(agent_record)
                self._save_state()

            cycle_record["completed_at"] = utc_now()
            self.state["cycles"].append(cycle_record)
            self._save_state()

        self.state["completed_at"] = utc_now()
        self._save_state()

        summary = {
            "cycles": len(self.state.get("cycles", [])),
            "agents": self.agents,
            "problems_per_cycle": self.problems_per_cycle,
            "total_problem_attempts": self.cycles * self.agents * self.problems_per_cycle,
            "completed_at": utc_now(),
        }
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        hr("DONE", ch="=")
        log(f"Artifacts written to: {self.output_dir.resolve()}", "OK")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone autonomous Hinglish math loop on Gemini"
    )
    parser.add_argument("--debug-port", type=int, default=DEBUG_PORT)
    parser.add_argument("--agents", type=int, default=DEFAULT_AGENTS)
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--problems-per-cycle", type=int, default=DEFAULT_PROBLEMS_PER_CYCLE)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--agent-start-index", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runner = MainPPRunner(
        debug_port=args.debug_port,
        agents=max(1, args.agents),
        cycles=max(1, args.cycles),
        problems_per_cycle=max(1, args.problems_per_cycle),
        output_dir=Path(args.output_dir),
        agent_start_index=max(1, args.agent_start_index),
    )
    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        log(f"Unhandled: {exc}", "ERR")
        raise


if __name__ == "__main__":
    main()
