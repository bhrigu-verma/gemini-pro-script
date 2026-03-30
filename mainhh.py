"""
Autonomous 4-agent Hinglish math loop on Gemini (UI attach mode).

- Attaches to an already-running Chrome DevTools endpoint (default: 9222)
- Opens 4 Gemini tabs (agents)
- Runs autonomous cycles (default: 30)
- For each agent, each cycle:
  1) Generate 10 Hinglish math problems with numeric gold answers
  2) Solve each problem using the provided Hinglish teaching system prompt
- Persists results continuously to JSON for crash-safe resume/review

Prerequisite:
  Start Chrome headless (or any attachable Chrome) separately, e.g.

  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-gemini-headless \
    --headless=new > /tmp/chrome-headless.log 2>&1 &
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import main as core


DEBUG_PORT = 9222
GEMINI_URL = "https://gemini.google.com/app"
DEFAULT_OUTPUT_DIR = Path("mainhh_output")

DEFAULT_AGENTS = 4
DEFAULT_CYCLES = 30
DEFAULT_PROBLEMS_PER_CYCLE = 10

SYSTEM_PROMPT = """You are a math teacher who teaches in Hinglish
(code-mixed Hindi-English). Generate a step-by-step solution
to math problems where:
- Mathematical notation, numbers, formulas stay in English
- Problem comprehension steps are in Hindi (Devanagari script)
- Intermediate reasoning can mix both
- Final answer is numeric only

For each step, also output a language tag: EN, HI, MIXED, MATH

Output ONLY valid JSON, no other text."""


def utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def extract_json_value(text: str) -> Optional[Any]:
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
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


class MainHHRunner:
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
        core.OUTPUT_DIR = self.output_dir
        core._LOG_PATH = self.output_dir / (
            "run_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
        )

        self.driver = None
        self.tabs: list[core.GeminiTab] = []
        self.problems_path = self.output_dir / "problems.json"
        self.state: dict[str, Any] = {
            "started_at": utc_now(),
            "config": {
                "debug_port": debug_port,
                "agents": agents,
                "cycles": cycles,
                "problems_per_cycle": problems_per_cycle,
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
        current: dict[str, Any]
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
        core.hr("MAINHH AUTONOMOUS LOOP", c="=")
        core.log(f"Attaching to Chrome on debug port {self.debug_port}", "INFO")
        self.driver = core.attach_driver(self.debug_port)

        for idx in range(self.agent_start_index, self.agent_start_index + self.agents):
            label = f"AGENT{idx}"
            core.log(f"Opening {label} tab", "INFO")
            handle = core.open_tab(self.driver, GEMINI_URL, label)
            time.sleep(3)
            tab = core.GeminiTab(self.driver, handle, label)
            tab.probe()
            self.tabs.append(tab)

    def _repair_list_of_problems(self, tab: core.GeminiTab, raw: str) -> list[dict[str, Any]]:
        parsed = extract_json_value(raw)
        if isinstance(parsed, list):
            rows = [r for r in parsed if isinstance(r, dict)]
            return rows

        repair_prompt = (
            "Convert the following content into strict JSON array only. "
            "Each item must have: hinglish_problem (string), gold_answer (number/string).\n\n"
            "CONTENT:\n" + raw
        )
        tab.send(repair_prompt)
        repaired = tab.recv()
        repaired_parsed = extract_json_value(repaired)
        if isinstance(repaired_parsed, list):
            return [r for r in repaired_parsed if isinstance(r, dict)]
        return []

    def _repair_solution_object(self, tab: core.GeminiTab, raw: str) -> dict[str, Any]:
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
            core.hr(f"CYCLE {cycle_idx}", c="-")
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

                # 1) Generate problems
                gen_prompt = self._problem_generation_prompt(cycle_idx, tab.name)
                tab.send(gen_prompt)
                raw_problem_set = tab.recv()
                problems = self._repair_list_of_problems(tab, raw_problem_set)
                problems = problems[: self.problems_per_cycle]

                core.log(
                    f"{tab.name} generated {len(problems)} candidate problems in cycle {cycle_idx}",
                    "OK",
                    tab.name,
                )

                # 2) Solve each problem with requested system/user style
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

        core.hr("DONE", c="=")
        core.log(f"Artifacts written to: {self.output_dir.resolve()}", "OK")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomous 4-agent Hinglish math loop on Gemini")
    parser.add_argument("--debug-port", type=int, default=DEBUG_PORT)
    parser.add_argument("--agents", type=int, default=DEFAULT_AGENTS)
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--problems-per-cycle", type=int, default=DEFAULT_PROBLEMS_PER_CYCLE)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--agent-start-index", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runner = MainHHRunner(
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
        core.log(f"Unhandled: {exc}", "ERR")
        raise


if __name__ == "__main__":
    main()
