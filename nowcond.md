# Current Condition (NowCond)

Last updated: 2026-03-18 (local session)

## What We Are Doing Right Now
- Running autonomous Gemini UI automation for Hinglish math generation/solution loops.
- Script in focus: `mainhh.py` (headless-attached workflow).
- Browser mode: attach to already-running Chrome DevTools endpoint (port 9222).
- Goal: multi-agent cycle execution (4 agents, 30 cycles target, 10 problems per agent per cycle).

## Active Runtime Architecture
- Core attach and tab automation logic comes from `main.py` primitives (`GeminiTab`, send/recv, stream stabilization).
- `mainhh.py` opens 4 Gemini tabs (`AGENT1..AGENT4`) and executes generation + solving loop.
- Prompt pattern in each loop:
  - Generate 10 Hinglish problems (+ gold answer)
  - Solve each using fixed Hinglish-teacher system prompt and JSON-only output requirement.

## Current Output State (Observed)
Output directory: `mainhh_output/`

Files present:
- `mainhh_output/history.json`
- `mainhh_output/problems.json`
- `mainhh_output/run_20260318_210151.log`
- `mainhh_output/run_20260318_214223.log`

Latest observed counters:
- `history.json` cycles saved: **0**
- `problems.json` items saved: **36**
- Last saved problem item:
  - cycle: **1**
  - agent: **AGENT4**
  - index: **6**
  - generated_at: **2026-03-18T17:15:52Z**

Note:
- Problem-level persistence is now incremental in `problems.json` (saved per problem).
- Cycle-level persistence in `history.json` may lag until cycle/agent blocks finalize.

## Why Throughput Feels Slow
- Workload size is large (4 agents x 30 cycles x 10 problems + generation calls).
- UI Selenium flow is sequential per request with stream-completion stabilization checks.
- Gemini responses can remain in streaming state for long windows.

## Parallelization Status
- `mainhh.py` now supports safer worker parallelization using:
  - `--output-dir`
  - `--agent-start-index`
- True parallel requires separate Chrome instances (separate debug ports and profiles) per worker process.

## Known Constraints / Risks
- UI mode remains slower and less deterministic than direct API mode.
- Long streaming states can delay checkpoint advancement.
- If multiple workers share one output file/profile, collisions can occur (mitigated by output-dir split).

## Quick Commands
Check current saved problem count:
```bash
python3 -c "import json, pathlib; p=pathlib.Path('mainhh_output/problems.json'); j=json.loads(p.read_text()); print(len(j.get('items',[])))"
```

Inspect latest run logs:
```bash
tail -n 80 mainhh_output/run_20260318_214223.log
```

List output artifacts:
```bash
ls -lah mainhh_output
```

- Commit note 01 on 2026-03-30: minor documentation touch.
- Commit note 02 on 2026-03-30: minor documentation touch.