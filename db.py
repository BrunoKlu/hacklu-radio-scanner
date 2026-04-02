#!/usr/bin/env python3
"""
SQLite storage for Radio Scanner.
3 tables: decoded_messages, power_readings, spectrum_snapshots.
Thread-safe — uses one connection per thread via check_same_thread=False + lock.
"""
import sqlite3
import json
import time
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "scanner.db"

_lock = threading.Lock()
_conn = None


def get_conn():
    """Get or create the shared DB connection."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read/write
        _conn.execute("PRAGMA synchronous=NORMAL")  # balance speed vs safety
        _init_tables(_conn)
    return _conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decoded_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            ts_iso TEXT NOT NULL,
            model TEXT,
            freq TEXT,
            label TEXT,
            rssi REAL,
            data TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS power_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            label TEXT NOT NULL,
            freq_mhz REAL NOT NULL,
            mean_db REAL NOT NULL,
            peak_db REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS spectrum_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            data TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_decoded_ts ON decoded_messages(ts);
        CREATE INDEX IF NOT EXISTS idx_decoded_model ON decoded_messages(model);
        CREATE INDEX IF NOT EXISTS idx_power_ts ON power_readings(ts);
        CREATE INDEX IF NOT EXISTS idx_spectrum_ts ON spectrum_snapshots(ts);
    """)
    conn.commit()


def store_decoded(msg):
    """Store a decoded rtl_433 message."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO decoded_messages (ts, ts_iso, model, freq, label, rssi, data) VALUES (?,?,?,?,?,?,?)",
            (
                time.time(),
                msg.get("_ts", ""),
                msg.get("model", ""),
                msg.get("_freq", ""),
                msg.get("_label", ""),
                msg.get("rssi", msg.get("snr")),
                json.dumps(msg),
            )
        )
        conn.commit()


def store_power(label, freq_mhz, mean_db, peak_db):
    """Store a power reading."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO power_readings (ts, label, freq_mhz, mean_db, peak_db) VALUES (?,?,?,?,?)",
            (time.time(), label, freq_mhz, mean_db, peak_db)
        )
        conn.commit()


def store_spectrum(spectrum_list):
    """Store a spectrum snapshot (called every ~5s, not every sweep)."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO spectrum_snapshots (ts, data) VALUES (?,?)",
            (time.time(), json.dumps(spectrum_list))
        )
        conn.commit()


def get_recent_decoded(limit=50):
    """Get the N most recent decoded messages."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT data FROM decoded_messages ORDER BY ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [json.loads(r["data"]) for r in reversed(rows)]


def get_recent_power(label=None, limit=100):
    """Get recent power readings."""
    with _lock:
        conn = get_conn()
        if label:
            rows = conn.execute(
                "SELECT * FROM power_readings WHERE label=? ORDER BY ts DESC LIMIT ?",
                (label, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM power_readings ORDER BY ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_decoded_stats():
    """Get summary stats of decoded messages."""
    with _lock:
        conn = get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM decoded_messages").fetchone()["c"]
        models = conn.execute(
            "SELECT model, COUNT(*) as c FROM decoded_messages GROUP BY model ORDER BY c DESC"
        ).fetchall()
        unique_devices = conn.execute(
            "SELECT COUNT(DISTINCT model || ':' || json_extract(data, '$.id')) as c FROM decoded_messages"
        ).fetchone()["c"]
    return {
        "total": total,
        "unique_devices": unique_devices,
        "models": {r["model"]: r["c"] for r in models},
    }
