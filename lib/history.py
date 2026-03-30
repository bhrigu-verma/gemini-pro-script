"""Checkpoint/state persistence helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def _sqlite_path_from_json(path: Path) -> Path:
    return path.with_name("history.db")


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT NOT NULL,
            company TEXT,
            location TEXT,
            source_method TEXT,
            status TEXT,
            validation_note TEXT,
            added_at TEXT,
            UNIQUE(url)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_num INTEGER NOT NULL,
            requested INTEGER,
            raw_rows INTEGER,
            accepted INTEGER,
            total INTEGER,
            remaining INTEGER,
            query TEXT,
            timestamp TEXT,
            UNIQUE(round_num)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            started_at TEXT,
            config_json TEXT,
            summary_json TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def load_history(path: Path) -> dict[str, Any] | None:
    # Primary source remains JSON for compatibility.
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass

    # Fallback to SQLite reconstruction.
    sqlite_path = _sqlite_path_from_json(path)
    if not sqlite_path.exists():
        return None

    with sqlite3.connect(sqlite_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT payload_json FROM state_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        raw = json.loads(row[0])
        if isinstance(raw, dict):
            return raw
        return None


def save_history(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    sqlite_path = _sqlite_path_from_json(path)
    with sqlite3.connect(sqlite_path) as conn:
        _ensure_schema(conn)
        now = _now_iso()

        # Persist full snapshot per iteration for reliable crash recovery.
        conn.execute(
            "INSERT INTO state_snapshots(created_at, payload_json) VALUES (?, ?)",
            (now, json.dumps(payload, ensure_ascii=False)),
        )

        config = payload.get("config", {})
        summary = payload.get("summary", {})
        conn.execute(
            """
            INSERT INTO run_meta(id, started_at, config_json, summary_json, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              started_at=excluded.started_at,
              config_json=excluded.config_json,
              summary_json=excluded.summary_json,
              updated_at=excluded.updated_at
            """,
            (
                str(payload.get("started_at", "")),
                json.dumps(config, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                now,
            ),
        )

        for row in payload.get("records", []):
            conn.execute(
                """
                INSERT OR IGNORE INTO records(
                    title, url, company, location, source_method,
                    status, validation_note, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get("title", "")),
                    str(row.get("url", "")),
                    str(row.get("company", "")),
                    str(row.get("location", "")),
                    str(row.get("source_method", "")),
                    str(row.get("status", "")),
                    str(row.get("validation_note", "")),
                    str(row.get("added_at", "")),
                ),
            )

        for round_row in payload.get("rounds", []):
            round_num = int(round_row.get("round", 0) or 0)
            if round_num <= 0:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO rounds(
                    round_num, requested, raw_rows, accepted,
                    total, remaining, query, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    round_num,
                    int(round_row.get("requested", 0) or 0),
                    int(round_row.get("raw_rows", 0) or 0),
                    int(round_row.get("accepted", 0) or 0),
                    int(round_row.get("total", 0) or 0),
                    int(round_row.get("remaining", 0) or 0),
                    str(round_row.get("query", "")),
                    str(round_row.get("timestamp", "")),
                ),
            )
