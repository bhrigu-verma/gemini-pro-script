"""Structured run logger that also writes to file."""

from __future__ import annotations

import datetime
from pathlib import Path


class RunLogger:
    def __init__(self, output_dir: Path, prefix: str = "run") -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = output_dir / f"{prefix}_{ts}.log"

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def write(self, msg: str, level: str = "INFO", tab: str = "") -> None:
        sym = {
            "INFO": "·",
            "OK": "✓",
            "WARN": "⚠",
            "ERR": "✗",
            "DBG": "›",
            "TX": "→",
            "RX": "←",
        }.get(level, "·")
        tag = f"[{tab}] " if tab else ""
        line = f"  [{self._now()}] {sym} {tag}{msg}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def dbg(self, msg: str, tab: str = "") -> None:
        self.write(msg, "DBG", tab)

    def hr(self, title: str = "", w: int = 72, c: str = "─") -> None:
        if title:
            pad = max(0, w - len(title) - 2)
            line = f"\n  {c*(pad//2)} {title} {c*(pad - pad//2)}\n"
        else:
            line = f"  {c*w}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
