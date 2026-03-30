# HMMTERM (Implementation Slice 1)

Single terminal command product for synthetic dataset generation using Gemini UI automation.

## Install (local dev)

```bash
/Users/bhriguverma/code/gemininpro/.venv-1/bin/python -m pip install -e .
```

## Commands

```bash
hmmterm doctor --debug-port 9222 --output-dir mainpp_output
hmmterm run --debug-port 9222 --agents 4 --cycles 30 --problems-per-cycle 10 --output-dir mainpp_output
hmmterm status --output-dir mainpp_output
hmmterm status --output-dir mainpp_output --watch --interval 2
hmmterm validate --output-dir mainpp_output --strict
hmmterm export --output-dir mainpp_output --out-file dataclaude/hmm_dataset/hinglishmath_export.jsonl
hmmterm interactive
```

## Notes

- `run` uses the standalone generation runtime from `mainpp.py`.
- `validate` checks core integrity and language-mix heuristics.
- `export` converts `problems.json` items to canonical JSONL.
- All flows are terminal-only and API-free.

- 2026-03-30 note 06: improve assumptions for maintainability and team handoff.

- 2026-03-30 note 12: refine workflow for maintainability and team handoff.

- 2026-03-30 note 18: annotate observations for maintainability and team handoff.

- 2026-03-30 note 24: expand report context for maintainability and team handoff.

- 2026-03-30 note 30: sync operational notes for maintainability and team handoff.
