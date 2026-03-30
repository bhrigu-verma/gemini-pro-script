"""
Gemini-driven research loop for collecting high-quality sources in batches
and synthesizing trends + H100-heavy product ideas.

Workflow:
1) Attach to existing Chrome debug session.
2) Open two Gemini tabs: COLLECTOR and ANALYST.
3) Collect 20-30 sources per round until target is reached (or user stops).
4) Deduplicate, score, and checkpoint after every round.
5) Ask Gemini to extract trends and propose H100-native product ideas.
"""

import datetime
import json
import os
import re
import time
import argparse
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

import main as core
from config.constants import (
    BROWSER_MODE_ATTACH,
    BROWSER_MODE_HEADLESS,
    DEBUG_PORT,
    DEFAULT_SPOOFED_USER_AGENT,
    GEMINI_FALLBACK_MODELS,
    GEMINI_PRIMARY_MODEL,
    GEMINI_URL,
)
from config.defaults import (
    DEFAULT_BROWSER_MODE,
    DEFAULT_CHROME_USER_DATA_DIR,
    DEFAULT_RUNTIME_MODE,
    DEFAULT_CONTEXT_RESET_INTERVAL,
    DEFAULT_INTER_ROUND_SLEEP_MAX,
    DEFAULT_INTER_ROUND_SLEEP_MIN,
)
from lib.browser import BrowserSession
from lib.gemini_api_client import GeminiAPIClient
from lib.gemini_client import GeminiClient
from lib.history import load_history, save_history
from lib.output_writers import write_json


OUTPUT_DIR = Path("research_loop_output")
DEFAULT_BATCH_SIZE = 50
DEFAULT_TARGET_SOURCES = 400
DATA_PHASE_MAX_LOOPS = 25
RESEARCH_REFINEMENT_LOOPS = 5
MAX_EXCLUDED_URLS_IN_PROMPT = 80
DEFAULT_PRIMARY_MODEL = GEMINI_PRIMARY_MODEL
DEFAULT_FALLBACK_MODELS = list(GEMINI_FALLBACK_MODELS)

QUERY_STRATEGY_CATALOG = [
    "recent agentic systems benchmarks and leaderboard papers",
    "tool-use planning and autonomous workflow orchestration papers",
    "long-horizon agents memory, retrieval, and context engineering",
    "agent reliability, self-correction, verification, and eval systems",
    "enterprise agent deployment, observability, governance, and safety",
    "synthetic data and precomputation pipelines for agent training",
    "multi-agent collaboration protocols and planning architectures",
    "code agents, tool routing, and execution policy research",
    "high-compute infrastructure for agents: training, serving, and caching",
    "industry reports on AI agent adoption and production outcomes",
]


def _utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _normalize_url(url: str) -> str:
    value = (url or "").strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = value.rstrip("/")
    return value.lower()


def _extract_json_value(text: str) -> Optional[Any]:
    if not text:
        return None

    candidates: List[str] = []

    stripped = text.strip()
    candidates.append(stripped)

    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    candidates.extend([block.strip() for block in fenced if block.strip()])

    first_arr = stripped.find("[")
    last_arr = stripped.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        candidates.append(stripped[first_arr:last_arr + 1])

    first_obj = stripped.find("{")
    last_obj = stripped.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        candidates.append(stripped[first_obj:last_obj + 1])

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


def _quality_score(source: Dict[str, Any], focus: str) -> int:
    score = 0
    year = _safe_int(source.get("year"), 0)
    source_type = str(source.get("source_type", "")).lower()
    title = str(source.get("title", "")).lower()
    why = str(source.get("why_relevant", "")).lower()
    focus_l = focus.lower()

    if year >= 2025:
        score += 3
    elif year >= 2023:
        score += 2
    elif year >= 2021:
        score += 1

    if any(token in source_type for token in ["conference", "journal", "arxiv", "paper", "preprint"]):
        score += 3
    elif any(token in source_type for token in ["blog", "report", "industry", "whitepaper"]):
        score += 2

    if any(token in title or token in why for token in ["agent", "tool use", "autonomous", "planner", "reasoning", "workflow"]):
        score += 2

    if any(token in title or token in why for token in ["training", "precompute", "synthetic", "distillation", "h100", "compute"]):
        score += 2

    if any(token in title or token in why for token in focus_l.split()[:6]):
        score += 1

    return min(score, 10)


class GeminiResearchLoop:
    def __init__(
        self,
        runtime_mode: str = DEFAULT_RUNTIME_MODE,
        api_key: str = "",
        browser_mode: str = DEFAULT_BROWSER_MODE,
        headless: bool = False,
        user_data_dir: str = DEFAULT_CHROME_USER_DATA_DIR,
        user_agent: str = DEFAULT_SPOOFED_USER_AGENT,
    ) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        core.OUTPUT_DIR = OUTPUT_DIR
        core._LOG_PATH = OUTPUT_DIR / ("run_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")

        self.driver = None
        self.session: Optional[BrowserSession] = None
        self.collector: Optional[Any] = None
        self.analyst: Optional[Any] = None
        self.runtime_mode = runtime_mode
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.browser_mode = browser_mode
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.user_agent = user_agent

        self.state: Dict[str, Any] = {
            "started_at": _utc_now(),
            "config": {},
            "batches": [],
            "refinement_loops": [],
            "sources": [],
            "trends": [],
            "ideas": [],
        }
        self.seen_urls = set()
        self.seen_titles = set()
        self._ui_round_counter = 0

    @staticmethod
    def _ensure_state_schema(raw: Dict[str, Any]) -> Dict[str, Any]:
        # Backward-compatible migration for older checkpoint files.
        raw.setdefault("started_at", _utc_now())
        raw.setdefault("config", {})
        raw.setdefault("batches", [])
        raw.setdefault("refinement_loops", [])
        raw.setdefault("sources", [])
        raw.setdefault("trends", [])
        raw.setdefault("ideas", [])
        return raw

    def _state_path(self) -> Path:
        return OUTPUT_DIR / "research_history.json"

    def _save_state(self) -> None:
        save_history(self._state_path(), self.state)

    def _load_resume(self) -> bool:
        raw = load_history(self._state_path())
        if not isinstance(raw, dict):
            return False

        self.state = self._ensure_state_schema(raw)
        for source in self.state.get("sources", []):
            self.seen_urls.add(_normalize_url(str(source.get("url", ""))))
            self.seen_titles.add(str(source.get("title", "")).strip().lower())
        return True

    def setup(self, primary_model: str, fallback_models: list[str]) -> None:
        if self.runtime_mode == "api":
            if not self.api_key:
                raise RuntimeError("API mode requires GEMINI_API_KEY env var or --api-key")
            self.collector = GeminiAPIClient(
                api_key=self.api_key,
                role_name="COLLECTOR_API",
                primary_model=primary_model,
                fallback_models=fallback_models,
            )
            self.analyst = GeminiAPIClient(
                api_key=self.api_key,
                role_name="ANALYST_API",
                primary_model=primary_model,
                fallback_models=fallback_models,
            )
            core.log(
                f"Research loop started in API mode with primary={primary_model}",
                "OK",
            )
            return

        core.hr("GEMINI RESEARCH LOOP", c="=")
        print(
            "\n  Needs:\n"
            "    * Chrome running with --remote-debugging-port=9222\n"
            "    * Logged into gemini.google.com in that profile\n"
        )
        input("  Press ENTER to connect ... ")

        self.session = BrowserSession(
            DEBUG_PORT,
            browser_mode=self.browser_mode,
            headless=self.headless,
            user_data_dir=self.user_data_dir,
            user_agent=self.user_agent,
        )
        self.driver = self.session.attach()

        core.log("Opening COLLECTOR tab ...")
        collector_handle = self.session.open_tab("COLLECTOR", url=GEMINI_URL)
        collector_tab = core.GeminiTab(self.driver, collector_handle, "COLLECTOR")
        self.collector = GeminiClient(collector_tab)
        self.collector.probe()

        core.log("Opening ANALYST tab ...")
        analyst_handle = self.session.open_tab("ANALYST", url=GEMINI_URL)
        analyst_tab = core.GeminiTab(self.driver, analyst_handle, "ANALYST")
        self.analyst = GeminiClient(analyst_tab)
        self.analyst.probe()

        for tab in (collector_tab, analyst_tab):
            tab.focus()
            url = self.driver.current_url
            if "accounts.google.com" in url or "signin" in url.lower():
                core.hr("LOGIN REQUIRED", c="!")
                print(f"  [{tab.name}] is on login page. Login and press ENTER.")
                input()

    def _build_collect_prompt(
        self,
        focus: str,
        batch_size: int,
        round_no: int,
        query_hint: str,
        planner_note: str,
    ) -> str:
        recent_urls = [
            str(s.get("url", ""))
            for s in self.state.get("sources", [])[-MAX_EXCLUDED_URLS_IN_PROMPT:]
            if s.get("url")
        ]
        excluded = "\n".join(f"- {u}" for u in recent_urls)

        return (
            "You are a research collector. Search the web and research sources and return ONLY JSON.\n"
            "Focus domain: " + focus + "\n"
            "Round: " + str(round_no) + "\n"
            "Query angle for this round: " + query_hint + "\n"
            "Planner note: " + planner_note + "\n"
            "Need exactly " + str(batch_size) + " high-quality unique sources, mostly recent (2023+ preferred).\n"
            "Include both: (a) research papers/preprints, (b) strong industry reports/blogs.\n"
            "Avoid duplicates and avoid any URLs listed in EXCLUDE_URLS.\n"
            "Prioritize diversity across sub-topics; do not return near-duplicate titles.\n"
            "For each source return fields: title, url, year, source_type, why_relevant.\n"
            "Return strict JSON array only. No markdown, no explanation.\n"
            "EXCLUDE_URLS:\n" + (excluded if excluded else "- none")
        )

    def _repair_to_json_array(self, raw_text: str) -> List[Dict[str, Any]]:
        parsed = _extract_json_value(raw_text)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]

        repair_prompt = (
            "Convert the following content into a strict JSON array.\n"
            "Each element must be an object with: title, url, year, source_type, why_relevant.\n"
            "Return only JSON array, no markdown.\n\n"
            "CONTENT:\n" + raw_text
        )
        self.collector.send(repair_prompt)
        repaired = self.collector.recv()
        repaired_obj = _extract_json_value(repaired)
        if isinstance(repaired_obj, list):
            return [item for item in repaired_obj if isinstance(item, dict)]

        return []

    def _dedupe_and_enrich(self, rows: List[Dict[str, Any]], focus: str) -> List[Dict[str, Any]]:
        accepted = []
        for row in rows:
            title = str(row.get("title", "")).strip()
            url = str(row.get("url", "")).strip()
            if not title or not url:
                continue

            norm_url = _normalize_url(url)
            norm_title = title.lower()
            if norm_url in self.seen_urls or norm_title in self.seen_titles:
                continue

            source = {
                "id": len(self.state["sources"]) + len(accepted) + 1,
                "title": title,
                "url": url,
                "year": _safe_int(row.get("year"), 0),
                "source_type": str(row.get("source_type", "unknown")).strip() or "unknown",
                "why_relevant": str(row.get("why_relevant", "")).strip(),
                "quality_score": 0,
                "added_at": _utc_now(),
            }
            source["quality_score"] = _quality_score(source, focus)

            accepted.append(source)
            self.seen_urls.add(norm_url)
            self.seen_titles.add(norm_title)

        return accepted

    def _batch_collect(
        self,
        focus: str,
        batch_size: int,
        round_no: int,
        query_hint: str,
        planner_note: str,
    ) -> int:
        prompt = self._build_collect_prompt(focus, batch_size, round_no, query_hint, planner_note)
        self.collector.send(prompt)
        raw = self.collector.recv()

        rows = self._repair_to_json_array(raw)
        accepted = self._dedupe_and_enrich(rows, focus)
        duplicates = max(0, len(rows) - len(accepted))

        self.state["sources"].extend(accepted)
        batch_info = {
            "round": round_no,
            "query_hint": query_hint,
            "planner_note": planner_note,
            "requested": batch_size,
            "returned_raw": len(rows),
            "accepted_new": len(accepted),
            "duplicates_or_rejected": duplicates,
            "total_sources": len(self.state["sources"]),
            "timestamp": _utc_now(),
        }
        self.state["batches"].append(batch_info)
        self._save_state()

        core.log(
            f"Round {round_no}: raw={len(rows)} accepted={len(accepted)} total={len(self.state['sources'])}",
            "OK",
            "COLLECTOR",
        )

        return len(accepted)

    @staticmethod
    def _inter_round_sleep() -> None:
        lower = int(os.environ.get("INTER_ROUND_SLEEP_MIN", str(DEFAULT_INTER_ROUND_SLEEP_MIN)))
        upper = int(os.environ.get("INTER_ROUND_SLEEP_MAX", str(DEFAULT_INTER_ROUND_SLEEP_MAX)))
        lower = max(0, lower)
        upper = max(lower, upper)
        if upper <= 0:
            return
        time.sleep(random.randint(lower, upper))

    def _maybe_refresh_ui_tabs(self) -> None:
        if self.runtime_mode != "ui" or self.session is None or self.driver is None:
            return
        interval = int(os.environ.get("CONTEXT_RESET_INTERVAL", str(DEFAULT_CONTEXT_RESET_INTERVAL)))
        if interval <= 0:
            return
        if self._ui_round_counter == 0 or self._ui_round_counter % interval != 0:
            return

        core.log(f"Refreshing research tabs at round {self._ui_round_counter} to avoid context bloat", "INFO")

        collector_handle = self.session.open_tab("COLLECTOR", url=GEMINI_URL, wait_seconds=2.0)
        collector_tab = core.GeminiTab(self.driver, collector_handle, "COLLECTOR")
        self.collector = GeminiClient(collector_tab)

        analyst_handle = self.session.open_tab("ANALYST", url=GEMINI_URL, wait_seconds=2.0)
        analyst_tab = core.GeminiTab(self.driver, analyst_handle, "ANALYST")
        self.analyst = GeminiClient(analyst_tab)

    def _select_query_plan(self, round_no: int) -> Dict[str, str]:
        previous = self.state.get("batches", [])
        base_idx = (round_no - 1) % len(QUERY_STRATEGY_CATALOG)
        query_hint = QUERY_STRATEGY_CATALOG[base_idx]
        planner_note = "Rotate topic coverage to maximize novelty and source diversity."

        if previous:
            last = previous[-1]
            accepted = _safe_int(last.get("accepted_new"), 0)
            returned = _safe_int(last.get("returned_raw"), 1)
            dupes = _safe_int(last.get("duplicates_or_rejected"), 0)

            acceptance_ratio = accepted / max(returned, 1)
            duplicate_ratio = dupes / max(returned, 1)

            if acceptance_ratio < 0.35:
                planner_note = (
                    "Last batch had low acceptance. Shift to adjacent sub-domain and force stricter novelty."
                )
                query_hint = f"{query_hint}; avoid previously covered venues and titles"
            elif duplicate_ratio > 0.45:
                planner_note = (
                    "High duplicate pressure detected. Expand to different venues and newer time windows."
                )
                query_hint = f"{query_hint}; emphasize newly published 2025-2026 sources"
            elif accepted >= 35:
                planner_note = "Strong yield. Continue deepening this direction for one more round."

        return {"query_hint": query_hint, "planner_note": planner_note}

    def _source_digest(self, max_items: int = 260) -> str:
        ranked = sorted(self.state.get("sources", []), key=lambda s: s.get("quality_score", 0), reverse=True)
        sample = ranked[:max_items]
        lines = []
        for s in sample:
            lines.append(
                f"- [{s.get('id')}] {s.get('title')} | year={s.get('year')} | "
                f"type={s.get('source_type')} | q={s.get('quality_score')} | {s.get('url')}"
            )
        return "\n".join(lines)

    def _extract_trends(self) -> List[Dict[str, Any]]:
        digest = self._source_digest()
        prompt = (
            "Analyze the following source digest and produce emerging trends for Agentic AI + Tool Use.\n"
            "Return strict JSON array with objects: trend, confidence_0_to_10, support_count, key_sources, why_now, h100_fit_0_to_10.\n"
            "Only include trends with support_count >= 3.\n\n"
            "SOURCE_DIGEST:\n" + digest
        )
        self.analyst.send(prompt)
        raw = self.analyst.recv()
        parsed = _extract_json_value(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []

    def _refine_trends(self, current_trends: List[Dict[str, Any]], loop_no: int) -> List[Dict[str, Any]]:
        digest = self._source_digest(max_items=300)
        current_json = json.dumps(current_trends, ensure_ascii=False)
        prompt = (
            f"Refine the trend model. This is refinement loop {loop_no}/{RESEARCH_REFINEMENT_LOOPS}.\n"
            "Given current trends and source digest, improve trend precision and remove weak trends.\n"
            "Rules:\n"
            "1) Keep only trends supported by >=3 sources.\n"
            "2) Merge overlapping trends.\n"
            "3) Add missing high-signal trends from digest.\n"
            "Return strict JSON array with: trend, confidence_0_to_10, support_count, key_sources, why_now, h100_fit_0_to_10.\n\n"
            "CURRENT_TRENDS_JSON:\n"
            + current_json
            + "\n\nSOURCE_DIGEST:\n"
            + digest
        )

        self.analyst.send(prompt)
        raw = self.analyst.recv()
        parsed = _extract_json_value(raw)
        if isinstance(parsed, list):
            refined = [item for item in parsed if isinstance(item, dict)]
            self.state.setdefault("refinement_loops", [])
            self.state["refinement_loops"].append(
                {
                    "loop": loop_no,
                    "trend_count": len(refined),
                    "timestamp": _utc_now(),
                }
            )
            self._save_state()
            return refined

        return current_trends

    def _generate_ideas(self, trends: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        trend_json = json.dumps(trends, ensure_ascii=False)
        prompt = (
            "Using the trends below, propose H100-native product ideas with heavy precomputation advantage.\n"
            "Return strict JSON array with fields:\n"
            "name, target_user, problem, product_summary, precompute_pipeline, why_h100_is_required, "
            "time_to_mvp_weeks, moat, monetization, build_priority_0_to_10.\n"
            "Prioritize ideas that are difficult without sustained high compute.\n\n"
            "TRENDS_JSON:\n" + trend_json
        )
        self.analyst.send(prompt)
        raw = self.analyst.recv()
        parsed = _extract_json_value(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []

    def run(self, primary_model: str, fallback_models: list[str]) -> None:
        resumed = self._load_resume()
        if resumed:
            core.log(f"Resume loaded. Existing sources: {len(self.state.get('sources', []))}", "OK")

        self.setup(primary_model=primary_model, fallback_models=fallback_models)

        core.hr("AUTONOMOUS RESEARCH CONFIG", c="=")
        focus = "Agentic AI + Tool Use"
        batch_size = DEFAULT_BATCH_SIZE
        target = DEFAULT_TARGET_SOURCES

        self.state["config"] = {
            "focus": focus,
            "batch_size": batch_size,
            "target_sources": target,
            "mode": "gemini_repeated_search",
            "data_phase_max_loops": DATA_PHASE_MAX_LOOPS,
            "research_refinement_loops": RESEARCH_REFINEMENT_LOOPS,
            "autonomous": True,
        }
        self._save_state()

        core.log(
            (
                f"Autonomous mode ON | target={target} | requested_per_loop={batch_size} | "
                f"data_loops<= {DATA_PHASE_MAX_LOOPS} | refinement_loops={RESEARCH_REFINEMENT_LOOPS}"
            ),
            "OK",
        )

        round_no = len(self.state.get("batches", [])) + 1
        data_loops = 0

        while len(self.state["sources"]) < target and data_loops < DATA_PHASE_MAX_LOOPS:
            self._ui_round_counter += 1
            self._maybe_refresh_ui_tabs()
            core.hr(f"COLLECTION ROUND {round_no}", c="-")
            plan = self._select_query_plan(round_no)
            accepted = self._batch_collect(
                focus=focus,
                batch_size=batch_size,
                round_no=round_no,
                query_hint=plan["query_hint"],
                planner_note=plan["planner_note"],
            )

            total = len(self.state["sources"])
            core.log(
                f"Autonomous progress: {total}/{target} sources | accepted_this_round={accepted}",
                "INFO",
            )

            data_loops += 1
            round_no += 1
            if len(self.state["sources"]) < target and data_loops < DATA_PHASE_MAX_LOOPS:
                self._inter_round_sleep()

        if len(self.state["sources"]) < target:
            core.log(
                (
                    f"Stopped data phase at {len(self.state['sources'])}/{target} after "
                    f"{data_loops} loops (max {DATA_PHASE_MAX_LOOPS})."
                ),
                "WARN",
            )

        core.hr("TREND EXTRACTION", c="=")
        trends = self._extract_trends()
        core.log(f"Initial trends extracted: {len(trends)}", "OK", "ANALYST")

        core.hr("RESEARCH REFINEMENT LOOPS", c="=")
        for loop_no in range(1, RESEARCH_REFINEMENT_LOOPS + 1):
            trends = self._refine_trends(trends, loop_no)
            core.log(f"Refinement {loop_no}/{RESEARCH_REFINEMENT_LOOPS}: trends={len(trends)}", "OK", "ANALYST")

        self.state["trends"] = trends
        self._save_state()
        core.log(f"Final trends: {len(trends)}", "OK", "ANALYST")

        core.hr("H100 IDEA GENERATION", c="=")
        ideas = self._generate_ideas(trends)
        ranked_ideas = sorted(ideas, key=lambda x: _safe_int(x.get("build_priority_0_to_10"), 0), reverse=True)
        self.state["ideas"] = ranked_ideas
        self.state["completed_at"] = _utc_now()
        self._save_state()

        final_report = {
            "summary": {
                "focus": focus,
                "total_sources": len(self.state.get("sources", [])),
                "trend_count": len(trends),
                "idea_count": len(ranked_ideas),
                "generated_at": _utc_now(),
            },
            "top_ideas": ranked_ideas[:10],
        }
        write_json(OUTPUT_DIR / "final_report.json", final_report)

        core.hr("DONE", c="=")
        print(f"  Sources collected: {len(self.state.get('sources', []))}")
        print(f"  Trends: {len(trends)}")
        print(f"  Ideas: {len(ranked_ideas)}")
        print(f"  Artifacts: {OUTPUT_DIR.resolve()}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini research loop")
    parser.add_argument("--runtime-mode", choices=["ui", "api"], default=DEFAULT_RUNTIME_MODE)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--primary-model", default=DEFAULT_PRIMARY_MODEL)
    parser.add_argument(
        "--fallback-models",
        default=",".join(DEFAULT_FALLBACK_MODELS),
        help="Comma-separated model fallback chain",
    )
    parser.add_argument(
        "--browser-mode",
        choices=[BROWSER_MODE_ATTACH, BROWSER_MODE_HEADLESS],
        default=DEFAULT_BROWSER_MODE,
        help="Browser mode used when runtime-mode=ui",
    )
    parser.add_argument("--headless", action="store_true", help="Launch Chrome in headless mode for UI runtime")
    parser.add_argument("--user-data-dir", default=DEFAULT_CHROME_USER_DATA_DIR)
    parser.add_argument("--user-agent", default=DEFAULT_SPOOFED_USER_AGENT)
    args = parser.parse_args()

    fallback_models = [m.strip() for m in args.fallback_models.split(",") if m.strip()]

    loop = GeminiResearchLoop(
        runtime_mode=args.runtime_mode,
        api_key=args.api_key,
        browser_mode=args.browser_mode,
        headless=args.headless,
        user_data_dir=args.user_data_dir,
        user_agent=args.user_agent,
    )
    try:
        loop.run(primary_model=args.primary_model, fallback_models=fallback_models)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        core.log(f"Unhandled: {exc}", "ERR")
        raise


if __name__ == "__main__":
    main()
