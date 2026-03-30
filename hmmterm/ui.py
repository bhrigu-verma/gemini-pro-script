from __future__ import annotations

import json
from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    HAS_RICH = True
except Exception:  # pragma: no cover - fallback mode
    HAS_RICH = False
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]


_CONSOLE = Console() if HAS_RICH else None


def clear_screen() -> None:
    print("\033c", end="")


def print_banner() -> None:
    title = "HMMTERM - Terminal Synthetic Data Toolkit"
    subtitle = "Gemini UI automation only (no API mode)"

    if HAS_RICH:
        _CONSOLE.print(Panel.fit(f"[bold cyan]{title}[/bold cyan]\n{subtitle}"))
    else:
        print("=" * 68)
        print(title)
        print(subtitle)
        print("=" * 68)


def print_json(data: dict[str, Any]) -> None:
    if HAS_RICH:
        _CONSOLE.print_json(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def print_status(summary: dict[str, Any]) -> None:
    if HAS_RICH:
        table = Table(title="Run Status")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")
        fields = [
            ("Output", summary.get("output_dir")),
            ("Agents", summary.get("agents")),
            ("Cycles", summary.get("cycles")),
            ("Problems/Cycle", summary.get("problems_per_cycle")),
            ("Expected Total", summary.get("expected_total")),
            ("Completed Items", summary.get("completed_items")),
            ("Completed Cycles", summary.get("completed_cycles")),
            ("Last Cycle", summary.get("last_cycle")),
            ("Completion %", f"{summary.get('completion_pct', 0)}%"),
            ("Started At", summary.get("started_at")),
            ("Updated At", summary.get("updated_at")),
            ("Run Completed", summary.get("run_completed")),
        ]
        for k, v in fields:
            table.add_row(str(k), str(v))
        _CONSOLE.print(table)
    else:
        print_json(summary)


def print_validation(report: dict[str, Any]) -> None:
    if HAS_RICH:
        table = Table(title="Dataset Validation")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")
        for key in [
            "ok",
            "total_items",
            "missing_required_fields",
            "empty_problem",
            "empty_gold_answer",
            "parse_failures",
            "duplicate_keys",
        ]:
            table.add_row(key, str(report.get(key)))
        _CONSOLE.print(table)
        _CONSOLE.print("Language mix:", style="bold")
        _CONSOLE.print(report.get("language_mix", {}))
        errors = report.get("errors", [])
        if errors:
            _CONSOLE.print(f"Errors ({len(errors)}):", style="bold red")
            for err in errors[:15]:
                _CONSOLE.print(f"- {err}")
    else:
        print_json(report)


def print_doctor(report: dict[str, Any]) -> None:
    if HAS_RICH:
        table = Table(title="Doctor Report")
        table.add_column("Check", style="cyan")
        table.add_column("Status", style="white")
        table.add_column("Details", style="white")

        chrome = report.get("chrome", {})
        storage = report.get("storage", {})
        runtime = report.get("runtime", {})

        table.add_row("Chrome endpoint", "OK" if chrome.get("ok") else "FAIL", str(chrome))
        table.add_row("Output storage", "OK" if storage.get("ok") else "FAIL", str(storage))
        table.add_row("Runtime module", "OK" if runtime.get("ok") else "FAIL", str(runtime))
        table.add_row("Overall", "OK" if report.get("ok") else "FAIL", "")
        _CONSOLE.print(table)
    else:
        print_json(report)


def print_export_result(output_file: str, count: int) -> None:
    if HAS_RICH:
        _CONSOLE.print(f"[green]Exported {count} records -> {output_file}[/green]")
    else:
        print(f"Exported {count} records -> {output_file}")
