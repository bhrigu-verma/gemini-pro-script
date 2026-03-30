from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional

from hmmterm.runtime import (
    RunConfig,
    doctor_report,
    run_generation,
    summarize_progress,
)
from hmmterm.ui import (
    clear_screen,
    print_banner,
    print_doctor,
    print_export_result,
    print_status,
    print_validation,
)
from hmmterm.validate import export_dataset_jsonl, validate_dataset


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return ivalue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hmmterm",
        description="Terminal toolkit for synthetic Hinglish data generation via Gemini UI automation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Start a generation run")
    run_p.add_argument("--debug-port", type=int, default=9222)
    run_p.add_argument("--agents", type=_positive_int, default=4)
    run_p.add_argument("--cycles", type=_positive_int, default=30)
    run_p.add_argument("--problems-per-cycle", type=_positive_int, default=10)
    run_p.add_argument("--output-dir", default="mainpp_output")
    run_p.add_argument("--agent-start-index", type=_positive_int, default=1)
    run_p.add_argument("--skip-doctor", action="store_true")

    status_p = sub.add_parser("status", help="Show run progress")
    status_p.add_argument("--output-dir", default="mainpp_output")
    status_p.add_argument("--watch", action="store_true")
    status_p.add_argument("--interval", type=float, default=3.0)

    val_p = sub.add_parser("validate", help="Validate dataset quality")
    val_p.add_argument("--output-dir", default="mainpp_output")
    val_p.add_argument("--strict", action="store_true", help="exit non-zero if validation has any issue")

    exp_p = sub.add_parser("export", help="Export problems.json items as JSONL")
    exp_p.add_argument("--output-dir", default="mainpp_output")
    exp_p.add_argument("--out-file", default="")

    doc_p = sub.add_parser("doctor", help="Run environment diagnostics")
    doc_p.add_argument("--debug-port", type=int, default=9222)
    doc_p.add_argument("--output-dir", default="mainpp_output")

    int_p = sub.add_parser("interactive", help="Interactive terminal control mode")
    int_p.add_argument("--debug-port", type=int, default=9222)
    int_p.add_argument("--output-dir", default="mainpp_output")

    return parser


def cmd_run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)

    if not args.skip_doctor:
        report = doctor_report(args.debug_port, output_dir)
        print_doctor(report)
        if not report.get("ok"):
            print("Doctor checks failed. Fix issues or use --skip-doctor.")
            return 1

    cfg = RunConfig(
        debug_port=args.debug_port,
        agents=args.agents,
        cycles=args.cycles,
        problems_per_cycle=args.problems_per_cycle,
        output_dir=output_dir,
        agent_start_index=args.agent_start_index,
    )

    run_generation(cfg)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)

    if args.watch:
        try:
            while True:
                clear_screen()
                print_banner()
                summary = summarize_progress(output_dir)
                print_status(summary)
                print("Press Ctrl+C to stop watching.")
                time.sleep(max(0.5, args.interval))
        except KeyboardInterrupt:
            return 0
    else:
        summary = summarize_progress(output_dir)
        print_status(summary)

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        report = validate_dataset(Path(args.output_dir))
    except FileNotFoundError as exc:
        print(f"Validation failed: {exc}")
        return 1
    except Exception as exc:
        print(f"Validation crashed: {exc}")
        return 1

    print_validation(report)
    if args.strict and not report.get("ok"):
        return 1
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    out_file = Path(args.out_file) if args.out_file else (output_dir / "dataset.jsonl")
    try:
        count = export_dataset_jsonl(output_dir, out_file)
    except FileNotFoundError as exc:
        print(f"Export failed: {exc}")
        return 1
    except Exception as exc:
        print(f"Export crashed: {exc}")
        return 1

    print_export_result(str(out_file.resolve()), count)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_report(args.debug_port, Path(args.output_dir))
    print_doctor(report)
    return 0 if report.get("ok") else 1


def _prompt_int(label: str, default: int) -> int:
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return default
    return max(1, int(raw))


def _prompt_str(label: str, default: str) -> str:
    raw = input(f"{label} [{default}]: ").strip()
    return raw or default


def cmd_interactive(args: argparse.Namespace) -> int:
    default_output = args.output_dir
    default_port = args.debug_port

    while True:
        clear_screen()
        print_banner()
        print("1) Run generation")
        print("2) Show status")
        print("3) Validate dataset")
        print("4) Export JSONL")
        print("5) Doctor checks")
        print("0) Exit")

        choice = input("\nSelect option: ").strip()

        if choice == "0":
            return 0

        if choice == "1":
            ns = argparse.Namespace(
                debug_port=_prompt_int("Debug port", default_port),
                agents=_prompt_int("Agents", 4),
                cycles=_prompt_int("Cycles", 30),
                problems_per_cycle=_prompt_int("Problems per cycle", 10),
                output_dir=_prompt_str("Output directory", default_output),
                agent_start_index=_prompt_int("Agent start index", 1),
                skip_doctor=False,
            )
            cmd_run(ns)
            input("\nRun finished. Press ENTER to continue...")
            continue

        if choice == "2":
            ns = argparse.Namespace(
                output_dir=_prompt_str("Output directory", default_output),
                watch=False,
                interval=2.0,
            )
            cmd_status(ns)
            input("\nPress ENTER to continue...")
            continue

        if choice == "3":
            ns = argparse.Namespace(
                output_dir=_prompt_str("Output directory", default_output),
                strict=False,
            )
            cmd_validate(ns)
            input("\nPress ENTER to continue...")
            continue

        if choice == "4":
            out_dir = _prompt_str("Output directory", default_output)
            out_file = _prompt_str("Output JSONL path", f"{out_dir}/dataset.jsonl")
            ns = argparse.Namespace(output_dir=out_dir, out_file=out_file)
            cmd_export(ns)
            input("\nPress ENTER to continue...")
            continue

        if choice == "5":
            ns = argparse.Namespace(
                debug_port=_prompt_int("Debug port", default_port),
                output_dir=_prompt_str("Output directory", default_output),
            )
            cmd_doctor(ns)
            input("\nPress ENTER to continue...")
            continue

        print("Invalid option.")
        time.sleep(1.0)


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "run":
        return cmd_run(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "interactive":
        return cmd_interactive(args)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
