"""SQLite database for call history persistence."""

import sqlite3
import os
from datetime import datetime
from config import DB_FILE


def get_connection():
    """Get a database connection, creating the DB if needed."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,       -- 'inbound' or 'outbound'
            protocol TEXT NOT NULL,        -- 'SIP' or 'IAX'
            remote_number TEXT NOT NULL,
            remote_name TEXT DEFAULT '',
            status TEXT NOT NULL,          -- 'answered', 'missed', 'rejected', 'failed'
            started_at TEXT NOT NULL,
            answered_at TEXT,
            ended_at TEXT,
            duration_seconds INTEGER DEFAULT 0
        )
    """)
    conn.commit()


def add_call_record(direction, protocol, remote_number, remote_name, status,
                    started_at, answered_at=None, ended_at=None, duration_seconds=0):
    """Insert a call record and return its ID."""
    conn = get_connection()
    try:
        cursor = conn.execute("""
            INSERT INTO call_history
                (direction, protocol, remote_number, remote_name, status,
                 started_at, answered_at, ended_at, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (direction, protocol, remote_number, remote_name, status,
              started_at, answered_at, ended_at, duration_seconds))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_call_history(limit=100, direction_filter=None):
    """Retrieve call history records, newest first."""
    conn = get_connection()
    try:
        query = "SELECT * FROM call_history"
        params = []
        if direction_filter:
            query += " WHERE direction = ?"
            params.append(direction_filter)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def delete_call_record(record_id):
    """Delete a single call record."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM call_history WHERE id = ?", (record_id,))
        conn.commit()
    finally:
        conn.close()


def clear_call_history():
    """Delete all call history."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM call_history")
        conn.commit()
    finally:
        conn.close()
