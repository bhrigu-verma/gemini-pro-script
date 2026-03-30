from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_ITEM_FIELDS = [
    "cycle",
    "agent",
    "index",
    "hinglish_problem",
    "gold_answer",
    "solution",
    "generated_at",
]

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
LATIN_RE = re.compile(r"[A-Za-z]")


def _load_problems_file(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "problems.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing problems file: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("problems.json must contain a JSON object")
    return data


def _fingerprint_item(item: dict[str, Any]) -> str:
    base = "|".join(
        [
            str(item.get("agent", "")).strip().lower(),
            str(item.get("cycle", "")).strip(),
            str(item.get("index", "")).strip(),
        ]
    )
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def validate_dataset(output_dir: Path) -> dict[str, Any]:
    payload = _load_problems_file(output_dir)
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("problems.json.items must be a list")

    missing_required_fields = 0
    empty_problem = 0
    empty_gold_answer = 0
    parse_failures = 0

    duplicate_keys = 0
    seen: set[str] = set()

    has_both_scripts = 0
    latin_only = 0
    devanagari_only = 0
    neither_script = 0

    errors: list[str] = []

    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            missing_required_fields += 1
            errors.append(f"item {idx}: not an object")
            continue

        missing = [field for field in REQUIRED_ITEM_FIELDS if field not in item]
        if missing:
            missing_required_fields += 1
            errors.append(f"item {idx}: missing fields {missing}")

        problem = str(item.get("hinglish_problem", "")).strip()
        answer = str(item.get("gold_answer", "")).strip()

        if not problem:
            empty_problem += 1
        if not answer:
            empty_gold_answer += 1

        solution = item.get("solution", {})
        if isinstance(solution, dict) and solution.get("parse_status") == "failed":
            parse_failures += 1

        fp = _fingerprint_item(item)
        if fp in seen:
            duplicate_keys += 1
        else:
            seen.add(fp)

        has_hi = bool(DEVANAGARI_RE.search(problem))
        has_en = bool(LATIN_RE.search(problem))
        if has_hi and has_en:
            has_both_scripts += 1
        elif has_en:
            latin_only += 1
        elif has_hi:
            devanagari_only += 1
        else:
            neither_script += 1

    total_items = len(items)
    ok = (
        total_items > 0
        and missing_required_fields == 0
        and empty_problem == 0
        and empty_gold_answer == 0
        and duplicate_keys == 0
        and parse_failures == 0
    )

    return {
        "ok": ok,
        "total_items": total_items,
        "missing_required_fields": missing_required_fields,
        "empty_problem": empty_problem,
        "empty_gold_answer": empty_gold_answer,
        "parse_failures": parse_failures,
        "duplicate_keys": duplicate_keys,
        "language_mix": {
            "hi_en_mixed": has_both_scripts,
            "latin_only": latin_only,
            "devanagari_only": devanagari_only,
            "neither_script": neither_script,
        },
        "errors": errors,
    }


def export_dataset_jsonl(output_dir: Path, output_file: Path) -> int:
    payload = _load_problems_file(output_dir)
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("problems.json.items must be a list")

    output_file.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with output_file.open("w", encoding="utf-8") as handle:
        for item in items:
            if not isinstance(item, dict):
                continue
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            count += 1

    return count
