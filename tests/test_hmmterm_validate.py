import json
from pathlib import Path

from hmmterm.validate import export_dataset_jsonl, validate_dataset


def _write_problems(path: Path, items: list[dict]) -> None:
    payload = {
        "started_at": "2026-03-28T00:00:00Z",
        "config": {
            "agents": 2,
            "cycles": 2,
            "problems_per_cycle": 2,
        },
        "items": items,
        "updated_at": "2026-03-28T01:00:00Z",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_validate_dataset_detects_issues(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    items = [
        {
            "cycle": 1,
            "agent": "AGENT1",
            "index": 1,
            "hinglish_problem": "राम bought 5 apples aur 3 oranges. total kitne fruits?",
            "gold_answer": "8",
            "solution": {"steps": []},
            "generated_at": "2026-03-28T00:00:00Z",
        },
        {
            "cycle": 1,
            "agent": "AGENT1",
            "index": 1,
            "hinglish_problem": "",  # empty problem should be flagged
            "gold_answer": "",  # empty answer should be flagged
            "solution": {"parse_status": "failed"},
            "generated_at": "2026-03-28T00:01:00Z",
        },
    ]
    _write_problems(output_dir / "problems.json", items)

    report = validate_dataset(output_dir)

    assert report["total_items"] == 2
    assert report["duplicate_keys"] == 1
    assert report["empty_problem"] == 1
    assert report["empty_gold_answer"] == 1
    assert report["parse_failures"] == 1
    assert report["ok"] is False


def test_export_dataset_jsonl_writes_rows(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    items = [
        {
            "cycle": 1,
            "agent": "AGENT1",
            "index": 1,
            "hinglish_problem": "A train travels 60 km in 1 ghanta.",
            "gold_answer": "60",
            "solution": {"steps": ["..."]},
            "generated_at": "2026-03-28T00:00:00Z",
        }
    ]
    _write_problems(output_dir / "problems.json", items)

    out_file = output_dir / "dataset.jsonl"
    count = export_dataset_jsonl(output_dir, out_file)

    assert count == 1
    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["hinglish_problem"].startswith("A train")
