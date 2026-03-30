# Gemini Automation Loops

Modular Gemini automation system with two execution modes:
- `ui`: Selenium + Chrome remote-debugging workflow.
- `api`: Official Gemini API workflow with fallback models and retry behavior.

## Scripts

### 1) Self-improvement loop (legacy)
- Entry: `main.py`
- Launcher: `./run.sh`

### 2) Research loop
- Entry: `gemini_research_loop.py`
- Wrapper: `scripts/idea_research.py`
- Launcher: `./run_research.sh`

### 3) Job-link completion loop
- Entry: `scripts/job_link_completion.py`
- Launcher: `./run_job_completion.sh`

### 4) HinglishMath dataset + eval pipeline
- Generate dataset: `python3 dataclaude/script1_generate_dataset.py ...`
- Run evaluator: `python3 dataclaude/script2_evaluate.py --input dataclaude/hm_dataset/hinglishmath_1k.jsonl ...`
- Analyze results: `python3 dataclaude/script3_analyze.py --input dataclaude/hm_results/results_raw.jsonl`

Milestone-2 analysis (stress-set + KPI gates):

```bash
python3 dataclaude/script3_analyze.py \
  --input dataclaude/hm_results/results_raw.jsonl \
  --output-dir dataclaude/hm_results \
  --stress-variants HG_065,HG_070 \
  --stress-cm-degree-min 0.65 \
  --stress-tag-keywords "trap,distractor,code-mix,code mix,ambiguous,negation,remainder,indian notation" \
  --gate-thresholds-json '{"overall_accuracy_min":0.55,"stress_set_accuracy_min":0.45,"safety_score_min":0.85,"harmful_response_rate_max":0.02,"off_target_lang_rate_max":0.20}'
```

Analysis outputs:
- `eval_summary.json` (machine-readable metrics including `stress_set` and `kpi_gates`)
- `eval_report.md` (human-readable report with variant degradation, reliability, safety, stress-set performance, and KPI gate verdict)

## Runtime modes

Both `gemini_research_loop.py` and `scripts/job_link_completion.py` support:
- `--runtime-mode ui`
- `--runtime-mode api`

When using API mode, provide key via:
- `--api-key <key>` or
- `GEMINI_API_KEY` environment variable.

## Terminal product (single package, no API)

This repository now includes an installable terminal product: `hmmterm`.

`hmmterm` is API-free by design and uses Gemini UI automation through a Chrome
remote-debugging session. It wraps generation, progress inspection, validation,
and export under a single CLI.

### Install

From the project root, inside your active virtual environment:

```bash
python3 -m pip install -e .
```

### Start Chrome for UI automation

`hmmterm` expects an already-running Chrome debug endpoint (default port: `9222`).
Example:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-gemini-hmmterm \
  --headless=new
```

### Recommended command flow

1. Environment checks:

```bash
hmmterm doctor --debug-port 9222 --output-dir mainpp_output
```

2. Start generation:

```bash
hmmterm run \
  --debug-port 9222 \
  --agents 4 \
  --cycles 30 \
  --problems-per-cycle 10 \
  --output-dir mainpp_output
```

3. Monitor progress:

```bash
hmmterm status --output-dir mainpp_output --watch --interval 2
```

4. Validate output quality:

```bash
hmmterm validate --output-dir mainpp_output --strict
```

5. Export JSONL dataset:

```bash
hmmterm export \
  --output-dir mainpp_output \
  --out-file dataclaude/hmm_dataset/hinglishmath_export.jsonl
```

### Interactive mode

For menu-driven terminal control:

```bash
hmmterm interactive --debug-port 9222 --output-dir mainpp_output
```

### Available `hmmterm` subcommands

- `run`: run a full generation job (`mainpp` runtime)
- `status`: show run progress (`--watch` supported)
- `validate`: run integrity and language-mix checks on `problems.json`
- `export`: write dataset rows to JSONL
- `doctor`: verify Chrome debug endpoint, storage path, and runtime import
- `interactive`: menu-driven terminal workflow

## Focus-stealing mitigation

For UI mode, use one of these:
- Headless Chrome: `GEMINI_HEADLESS=1`
- Virtual display (Linux): `GEMINI_XVFB=1` (starts `Xvfb :99` if available)

Launch scripts (`run.sh`, `run_research.sh`, `run_job_completion.sh`) include:
- `--disable-focus-on-load`
- anti-backgrounding flags

Example:

```bash
GEMINI_HEADLESS=1 GEMINI_XVFB=1 ./run_job_completion.sh --runtime-mode ui --non-interactive
```

For long unattended runs, API mode is recommended to avoid browser focus/session issues.

## CLI reference

### Research loop

```bash
python3 gemini_research_loop.py \
  --runtime-mode ui \
  --browser-mode attach \
  --primary-model gemini-3.1-pro \
  --fallback-models gemini-3-flash,gemini-3.1-flash-lite
```

Key options:
- `--runtime-mode {ui,api}`
- `--api-key <key>`
- `--primary-model <model>`
- `--fallback-models <comma-separated>`
- `--browser-mode {attach,headless}` (ui mode)
- `--headless` (ui mode)
- `--user-data-dir <path>`
- `--user-agent <ua>`

### Job-link completion loop

```bash
python3 scripts/job_link_completion.py \
  --runtime-mode api \
  --target 100 \
  --batch-size 25 \
  --role "AI Engineer" \
  --location "Remote" \
  --query "latest AI/ML roles" \
  --primary-model gemini-3.1-pro \
  --fallback-models gemini-3-flash,gemini-3.1-flash-lite
```

Key options:
- `--runtime-mode {ui,api}`
- `--target <int>`
- `--batch-size <int>`
- `--seed-file <txt|csv|json>`
- `--resume`
- `--max-iterations <int>`
- `--max-empty-streak <int>`
- `--non-interactive`
- `--api-key <key>`
- `--primary-model <model>`
- `--fallback-models <comma-separated>`
- `--browser-mode {attach,headless}` (ui mode)
- `--headless` (ui mode)

### Dedicated long-run orchestrator

Use this wrapper to keep a workflow running unattended and emit heartbeat metrics:

```bash
python3 scripts/long_run.py \
  --workflow research \
  --runtime-mode api \
  --poll-seconds 20 \
  --max-minutes 0
```

Useful options:
- `--workflow {research,job,legacy}`
- `--runtime-mode {ui,api}`
- `--api-key <key>` (or `GEMINI_API_KEY`)
- `--poll-seconds <int>`
- `--max-minutes <int>`
- `--metrics-file <path>`
- `--extra-args ...` (forwarded to underlying script)

## Outputs

### Research loop
- `research_loop_output/research_history.json`
- `research_loop_output/final_report.json`
- `research_loop_output/run_*.log`

### Job completion loop
- `job_completion_output/history.json`
- `job_completion_output/final_report.json`
- `job_completion_output/records.csv`
- `job_completion_output/report.md`

### Long-run orchestrator
- `long_run_output/metrics.csv`

## Long-run tuning knobs

Environment variables used by the loops:
- `INTER_ROUND_SLEEP_MIN` (default: `45`)
- `INTER_ROUND_SLEEP_MAX` (default: `90`)
- `RATE_LIMIT_BACKOFF` (default: `60,120,300`)
- `CONTEXT_RESET_INTERVAL` (default: `15`)
- `SCREENSHOT_INTERVAL` (default: `10`)
- `SCREENSHOT_ON_ERROR` (default: `1`)
- `MEMORY_CLEAN_INTERVAL` (default: `20`)
- `AUTO_MAX_ROUNDS` (default: `0`, only used by `main.py`)

These controls help for 100+ iteration stability by reducing rate-limit bursts,
refreshing UI contexts, and limiting disk growth from screenshots.

## Tests

Run unit tests:

```bash
python3 -m pytest tests -v
```

Alternative (unittest-discovery-compatible tests only):

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

Covered areas:
- URL normalization and dedupe key behavior.
- Completion controller stop conditions.
- JSON parser/repair fallback logic.

- 2026-03-30 note 01: clarify setup for maintainability and team handoff.

- 2026-03-30 note 07: document naming for maintainability and team handoff.

- 2026-03-30 note 13: polish analysis notes for maintainability and team handoff.

- 2026-03-30 note 19: capture examples for maintainability and team handoff.

- 2026-03-30 note 25: tighten runbook for maintainability and team handoff.

- 2026-03-30 note 25: tighten runbook for maintainability and team handoff.

- 2026-03-30 note 31: clarify setup for maintainability and team handoff.

- 2026-03-30 note 37: document naming for maintainability and team handoff.

- 2026-03-30 note 43: polish analysis notes for maintainability and team handoff.

- 2026-03-30 note 49: capture examples for maintainability and team handoff.
