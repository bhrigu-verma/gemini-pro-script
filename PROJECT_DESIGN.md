# Gemini Pro Script - Project Design Document

## 1) Project status today

This project currently contains one production script: main.py.

What it does now:
- Attaches Selenium to an already-running Chrome instance using remote debugging.
- Opens two Gemini tabs:
  - Improver tab (writer)
  - Critic tab (reviewer)
- Runs an iterative Writer <-> Critic loop:
  - User gives initial prompt
  - Improver writes version 0
  - Critic critiques
  - Improver rewrites from critique
  - Repeat until user stops
- Saves round history and screenshots for traceability.

Main objective of current implementation:
- Automate quality improvement by repeatedly feeding critique into rewrite cycles.

---

## 2) Current files and purpose

- main.py
  - Core orchestration, browser/session handling, DOM interaction, loop logic, logging, and persistence.
- run.sh
  - Helper script to launch Chrome in remote-debug mode and run the Python script.
- gemini_loop_output/
  - Runtime outputs (logs, screenshots, history JSON, final text).
- chrome-profile/
  - Browser profile/state for login persistence.
- storage_state.json
  - Session-related state file.

---

## 3) Runtime architecture in main.py

### 3.1 Configuration layer
Key configuration values:
- DEBUG_PORT = 9222
- GEMINI_URL = https://gemini.google.com/app
- OUTPUT_DIR = gemini_loop_output
- RESPONSE_TIMEOUT = 180 seconds
- STABLE_CHECKS = 4
- STABLE_INTERVAL = 2.5 seconds

Prompt templates:
- CRITIC_PROMPT: strict critique format with 5 sections.
- IMPROVE_PROMPT_PREFIX / IMPROVE_PROMPT_SUFFIX: enforce full rewrite based on critique.

### 3.2 Logging layer
Functions:
- log(msg, level, tab)
- dbg(msg, tab)
- hr(title)

Behavior:
- Writes timestamped logs to terminal.
- Mirrors logs to run log file in output folder.

### 3.3 Browser/session layer
Functions:
- attach_driver(port)
  - Attaches to existing Chrome via debuggerAddress.
  - Uses webdriver-manager when available, else system chromedriver fallback.
- open_tab(driver, url, label)
  - Robust tab creation with fallback sequence:
    1. Selenium switch_to.new_window(tab)
    2. CDP Target.createTarget
    3. Ctrl+T keyboard simulation

Why this matters:
- Avoids popup-block issues from window.open on Gemini pages.

### 3.4 DOM selector strategy
Selector groups are centrally defined for resilience:
- INPUT_SELS
- SEND_SELS
- STOP_SELS
- RESP_SELS

Function:
- probe_dom(driver, label)
  - Diagnostics for selector hit rates.

Design principle:
- Use multi-selector fallback lists to survive frontend markup changes.

### 3.5 Gemini tab abstraction
Class: GeminiTab

Core responsibilities:
- focus(): switch to correct browser tab.
- _find(): find first valid selector from fallback list.
- _type_text(): insert prompt text into contenteditable safely using execCommand and chunking.
- _click_send(): send action with button + fallback click logic.
- _streaming(): detect response in progress using stop-button selectors.
- _last_text(): fetch latest response text with selector fallback + JS fallback.
- _resp_count(): count response blocks to detect changes.
- send(text): full send workflow.
- recv(): wait-for-completion workflow using stability checks.
- screenshot(path), dump_dom(tag): debugging and evidence capture.

### 3.6 Orchestration layer
Class: GeminiLoop

Flow:
1. setup()
   - Verify preconditions (Chrome started in debug mode, logged in)
   - Attach driver
   - Open Improver and Critic tabs
   - Run login checks
2. run()
   - Read initial user prompt
   - Round 0: Improver generates first output
   - Iterative rounds:
     - Critic critiques current version
     - Improver rewrites
     - Save outputs and screenshots
   - Stop on user command
   - Persist final version

Persistence:
- history.json stores per-round content and metadata.
- final_version.txt stores final output.

---

## 4) Response completion logic

The recv() completion strategy is designed to avoid partial captures:

Phase 1:
- Detect response start by checking streaming state or response count change.

Phase 2:
- Poll every STABLE_INTERVAL seconds.
- Require the same latest text snapshot STABLE_CHECKS times.
- If stable snapshots reached, response is considered done.
- If timeout reached, return best available text.

Why this matters:
- Reduces truncation risk from capturing while Gemini is still streaming.

---

## 5) Output and observability

Per execution:
- One run log file with timestamps and severity symbols.

Per round:
- Screenshot of Improver tab.
- Screenshot of Critic tab.
- history.json entry.

End of run:
- final_version.txt for final answer.

Operational benefit:
- Full audit trail for post-run analysis and debugging.

---

## 6) Known constraints and risk areas

Current known constraints:
- Selector fragility if Gemini UI changes significantly.
- Long runs can produce many screenshots/log files.
- Loop is user-driven and not yet target-driven for counts.
- Single-script monolith structure limits reuse.

Risk handling already present:
- Multi-fallback selectors.
- Browser and tab opening fallback methods.
- DOM dump and screenshots on crash paths.

---

## 7) Designed next architecture (agreed direction)

The next design direction is to split current logic into reusable modules and add specialized scripts.

Planned module layout:
- lib/browser.py
  - Driver attach, tab open, session lifecycle.
- lib/gemini_client.py
  - GeminiTab behavior (send/recv/probe) as reusable client.
- lib/logger.py
  - Structured logging utilities.
- lib/history.py
  - Checkpointing and round/task persistence.
- lib/output_writers.py
  - JSON, CSV, Markdown exports.
- config/constants.py
  - URLs, selectors, static strings.
- config/defaults.py
  - Timeouts, retry limits, loop limits.

Planned scripts:
- scripts/idea_research.py
  - Automated idea generation and refinement loops.
- scripts/job_link_completion.py
  - Target-based completion workflow for links (seed input + Gemini top-up until target).
- scripts/batch_processor.py
  - Generic count/quality completion engine.

---

## 8) Use cases designed so far

### Use case A: Idea research loop
Goal:
- Generate and refine research ideas in multiple rounds automatically.

Expected behavior:
- Run autonomously to target source count (default 400).
- Data phase runs up to 25 loops, requesting 50 sources per loop, stopping early when target is reached.
- Prompt strategy changes intelligently each loop based on acceptance/duplicate ratios.
- Research refinement phase runs 5 loops to improve and merge trends before ideation.
- Save structured idea outputs and rationale.
- Export in JSON, CSV, Markdown report.

### Use case B: Completion-to-target (example: 100 job links)
Goal:
- If AI returns partial output (example: 10 links), continue automatically until target count is reached or stop conditions trigger.

Expected behavior:
- Ingest seed links from file.
- Ask Gemini for missing count repeatedly.
- Deduplicate and validate links each iteration.
- Track progress and retries.
- Stop on:
  - target reached, or
  - max retries/iterations/stall threshold reached.

Exports:
- JSON, CSV, Markdown report with completion status.

---

## 9) Logic patterns we are using

Core patterns in this project:
- Fallback-first automation for browser and DOM selectors.
- Stable-snapshot response detection instead of single-capture reads.
- Tab-based role separation (writer and critic).
- Round-based state persistence for full reproducibility.
- Crash-forensics support (DOM dumps and screenshots).

Planned extension patterns:
- Count-based completion loops with strict stop conditions.
- Resume/checkpoint execution for long jobs.
- Unified output adapters for machine-readable and human-readable reports.

---

## 10) Quick operational checklist

Before running autonomous research loop:
1. Start Chrome with remote debugging port and dedicated user-data-dir.
2. Log into Gemini in that Chrome profile.
3. Run gemini_research_loop.py (or run_research.sh).

During run:
- Watch logs for selector and streaming behavior.
- Script runs autonomously through data and refinement phases.

After run:
- Review research_loop_output history and final report.

---

## 11) Summary

Current state:
- The self-improvement loop is implemented and functional in a single script with robust fallbacks and observability.
- The research loop now supports autonomous collection and analysis:
  - 50-source request batches
  - up to 25 autonomous data loops
  - automatic query/prompt adaptation
  - 5 research-refinement loops before final idea ranking

Designed direction:
- Move to a modular architecture and add specialized automation scripts for idea research and completion-to-target workflows such as collecting 100 job links automatically.

---

## 12) Implementation status (in progress)

This section tracks what has already been implemented without modifying main.py.

### 12.1 Constraint respected
- main.py remains unchanged.
- All new implementation was added in new files only.

### 12.2 New files implemented

Config:
- config/constants.py
- config/defaults.py

Shared libraries:
- lib/url_utils.py
- lib/output_writers.py
- lib/browser.py
- lib/gemini_client.py
- lib/logger.py
- lib/history.py

Scripts:
- scripts/batch_processor.py
- scripts/job_link_completion.py
- scripts/idea_research.py

Operational helpers:
- legacy_main_copy.py
- run_job_completion.sh

### 12.3 Functional capabilities now available

Job link completion script (scripts/job_link_completion.py):
- Target-based loop to collect links until target is reached.
- Hybrid input model:
  - Seed links from txt/csv/json.
  - Gemini-generated top-up rounds.
- Deduplication and URL validation before acceptance.
- Safety stop conditions:
  - max iterations
  - max empty streak
- Resume/checkpoint support from history.json.
- Multi-format outputs:
  - final_report.json
  - records.csv
  - report.md

Shared runtime support:
- BrowserSession wrapper for attach/open/login-check.
- GeminiClient wrapper for ask/send/receive interactions.
- CompletionController for reusable target-loop logic.

### 12.4 Validation completed
- Syntax compilation passed for all newly added modules and scripts.
- Launcher script run_job_completion.sh is executable.

### 12.5 Implementation completion updates
- Refactored gemini_research_loop.py to use shared modules directly:
  - BrowserSession for UI attach/headless startup
  - GeminiClient wrapper for tab interactions
  - shared config constants/defaults for runtime options
- Added unit tests:
  - tests/test_url_utils.py (dedupe + URL normalization/validation)
  - tests/test_batch_processor.py (stop condition behavior)
  - tests/test_parser_repair.py (parser/repair behavior)
- Added consolidated CLI documentation in README.md for:
  - research loop options and examples
  - job completion loop options and examples
  - runtime mode (ui/api) usage and outputs

- 2026-03-30 note 02: refine workflow for maintainability and team handoff.

- 2026-03-30 note 08: annotate observations for maintainability and team handoff.

- 2026-03-30 note 14: expand report context for maintainability and team handoff.

- 2026-03-30 note 20: sync operational notes for maintainability and team handoff.

- 2026-03-30 note 26: improve assumptions for maintainability and team handoff.

- 2026-03-30 note 32: refine workflow for maintainability and team handoff.
