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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer TEXT NOT NULL,
            direction TEXT NOT NULL,       -- 'in' or 'out'
            body TEXT NOT NULL,
            timestamp REAL NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            message_type TEXT NOT NULL DEFAULT 'sms'   -- 'sms' or 'whatsapp'
        )
    """)
    # Add message_type column to existing DBs that pre-date this schema
    try:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN "
                     "message_type TEXT NOT NULL DEFAULT 'sms'")
        conn.commit()
    except Exception:
        pass  # Column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_peer "
                 "ON chat_messages(peer, timestamp)")
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


# ---- Chat messages (SIP MESSAGE) ----

def add_chat_message(peer, direction, body, timestamp, read=0, message_type="sms"):
    """Insert a chat message and return its ID."""
    conn = get_connection()
    try:
        cursor = conn.execute("""
            INSERT INTO chat_messages (peer, direction, body, timestamp, read, message_type)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (peer, direction, body, timestamp, read, message_type))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_chats(message_type=None):
    """Return per-peer summary for a given channel, newest first.

    Args:
        message_type: "sms", "whatsapp", or None (all channels)
    """
    conn = get_connection()
    try:
        if message_type:
            rows = conn.execute("""
                SELECT peer,
                       MAX(timestamp) AS last_ts,
                       SUM(CASE WHEN read = 0 AND direction = 'in' THEN 1 ELSE 0 END) AS unread
                FROM chat_messages
                WHERE message_type = ?
                GROUP BY peer
                ORDER BY last_ts DESC
            """, (message_type,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT peer,
                       MAX(timestamp) AS last_ts,
                       SUM(CASE WHEN read = 0 AND direction = 'in' THEN 1 ELSE 0 END) AS unread
                FROM chat_messages
                GROUP BY peer
                ORDER BY last_ts DESC
            """).fetchall()
        result = []
        for row in rows:
            q = ("SELECT body, direction FROM chat_messages "
                 "WHERE peer = ? " +
                 ("AND message_type = ? " if message_type else "") +
                 "ORDER BY timestamp DESC LIMIT 1")
            params = (row["peer"], message_type) if message_type else (row["peer"],)
            last = conn.execute(q, params).fetchone()
            result.append({
                "peer": row["peer"],
                "last_body": last["body"] if last else "",
                "last_direction": last["direction"] if last else "",
                "last_timestamp": row["last_ts"],
                "unread": row["unread"] or 0,
            })
        return result
    finally:
        conn.close()


def get_messages(peer, message_type="sms", limit=500):
    """Return all messages for a peer on a given channel, oldest first."""
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM chat_messages WHERE peer = ? AND message_type = ? "
            "ORDER BY timestamp ASC LIMIT ?",
            (peer, message_type, limit)
        ).fetchall()]
    finally:
        conn.close()


def mark_chat_read(peer, message_type="sms"):
    """Mark all inbound messages from peer on a given channel as read."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE chat_messages SET read = 1 "
            "WHERE peer = ? AND direction = 'in' AND message_type = ?",
            (peer, message_type)
        )
        conn.commit()
    finally:
        conn.close()
