"""
ai_db.py — SQLite database for AI assistant state.

PURPOSE:
    Stores the AI assistant's configuration (enabled state, user/AI names),
    scheduled reminders, and server-side chat history backup.

TABLES:
    ai_config   — Single-row config: enabled, user_name, ai_name
    reminders   — Scheduled Telegram messages (time, message, sent flag)
    chat_history — Server-side backup of conversation (role, content)

USAGE:
    db = AIDB("/data/ai.db")
    db.save_config(enabled=True, user_name="Marco", ai_name="Sentinel")
    db.add_reminder("Check cameras", "2026-02-21T22:00:00")
"""

import os
import time
import sqlite3
import logging
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger("dashboard.ai_db")


@dataclass
class AIConfig:
    """AI assistant configuration."""
    enabled: bool = False
    user_name: str = ""
    ai_name: str = "Atlas"
    created_at: float = 0.0


@dataclass
class Reminder:
    """A scheduled Telegram reminder."""
    id: int = 0
    message: str = ""
    trigger_time: float = 0.0
    sent: bool = False
    created_at: float = 0.0


class AIDB:
    """SQLite database for AI assistant state."""

    def __init__(self, db_path: str = "/data/ai.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS ai_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    user_name TEXT NOT NULL DEFAULT '',
                    ai_name TEXT NOT NULL DEFAULT 'Atlas',
                    created_at REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    trigger_time REAL NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0.0,
                    media_type TEXT NOT NULL DEFAULT 'text'
                );

                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL DEFAULT 0.0
                );
            """)
            conn.commit()

            # Migrations — add columns that may not exist in older DBs
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN media_type TEXT NOT NULL DEFAULT 'text'")
                conn.commit()
            except Exception:
                pass  # Column already exists
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def get_config(self) -> dict:
        """Get AI config (returns defaults if not yet set)."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
            if row:
                return {
                    "enabled": bool(row["enabled"]),
                    "user_name": row["user_name"],
                    "ai_name": row["ai_name"],
                    "created_at": row["created_at"],
                }
            return {"enabled": False, "user_name": "", "ai_name": "Atlas", "created_at": 0}
        finally:
            conn.close()

    def save_config(self, enabled: bool, user_name: str = "",
                    ai_name: str = "Atlas") -> dict:
        """Save or update AI configuration."""
        conn = self._get_conn()
        try:
            now = time.time()
            conn.execute("""
                INSERT INTO ai_config (id, enabled, user_name, ai_name, created_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    user_name = excluded.user_name,
                    ai_name = excluded.ai_name
            """, (int(enabled), user_name, ai_name, now))
            conn.commit()
            return self.get_config()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------
    def add_reminder(self, message: str, trigger_time: float, media_type: str = "text") -> int:
        """Schedule a new reminder. Returns the reminder ID."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO reminders (message, trigger_time, created_at, media_type) VALUES (?, ?, ?, ?)",
                (message, trigger_time, time.time(), media_type),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_due_reminders(self) -> list[dict]:
        """Get reminders that are due but not yet sent."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE sent = 0 AND trigger_time <= ?",
                (time.time(),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_reminder_sent(self, reminder_id: int):
        """Mark a reminder as sent."""
        conn = self._get_conn()
        try:
            conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
            conn.commit()
        finally:
            conn.close()

    def get_reminders(self, include_sent: bool = False) -> list[dict]:
        """Get all reminders, optionally including already-sent ones."""
        conn = self._get_conn()
        try:
            if include_sent:
                rows = conn.execute(
                    "SELECT * FROM reminders ORDER BY trigger_time DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE sent = 0 ORDER BY trigger_time ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def delete_reminder(self, reminder_id: int):
        """Delete a reminder."""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Chat History (server-side backup)
    # ------------------------------------------------------------------
    def save_message(self, role: str, content: str):
        """Save a chat message to server-side history."""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO chat_history (role, content, timestamp) VALUES (?, ?, ?)",
                (role, content, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_recent_history(self, limit: int = 50) -> list[dict]:
        """Get recent chat messages for context."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT role, content, timestamp FROM chat_history "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        finally:
            conn.close()

    def clear_history(self):
        """Clear all chat history."""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM chat_history")
            conn.commit()
        finally:
            conn.close()
