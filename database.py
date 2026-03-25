# ============================================================
# database.py — SQLite persistence layer
#
# Stores everything the agent system needs to survive restarts:
#   • leads          — all leads the system has seen
#   • sequence_steps — which nurture steps have been sent
#   • conversations  — full message history per lead
#   • system_logs    — activity log for the dashboard
# ============================================================

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime
from contextlib import contextmanager

log = logging.getLogger(__name__)

DB_PATH = "agentos.db"


# ════════════════════════════════════════════════════════════
# Connection
# ════════════════════════════════════════════════════════════

@contextmanager
def get_db():
    """Context manager — auto-commits and closes the connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# Schema
# ════════════════════════════════════════════════════════════

def init_db():
    """
    Creates all tables if they don't exist.
    Safe to call on every startup.
    """
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                id              TEXT PRIMARY KEY,   -- GHL contact ID
                name            TEXT,
                email           TEXT,
                phone           TEXT,
                lead_type       TEXT DEFAULT 'unknown',
                source          TEXT,
                status          TEXT DEFAULT 'new',
                urgency         TEXT DEFAULT 'medium',
                opener_fired    INTEGER DEFAULT 0,  -- 0/1 boolean
                nurturer_step   INTEGER DEFAULT 0,  -- current day reached
                nurturer_status TEXT DEFAULT 'pending',  -- pending/running/stopped/completed
                classification  TEXT,               -- JSON string
                created_at      TEXT,
                updated_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS sequence_steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id     TEXT NOT NULL,
                day         INTEGER NOT NULL,
                channel     TEXT NOT NULL,
                theme       TEXT,
                status      TEXT DEFAULT 'pending',  -- pending/sent/skipped/failed
                sent_at     TEXT,
                message     TEXT,                    -- what was actually sent
                result      TEXT,                    -- JSON response from GHL
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id     TEXT NOT NULL,
                role        TEXT NOT NULL,   -- 'user' or 'assistant'
                channel     TEXT,
                content     TEXT NOT NULL,
                created_at  TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            );

            CREATE TABLE IF NOT EXISTS system_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                level       TEXT NOT NULL,   -- INFO/SUCCESS/WARN/ERROR
                source      TEXT,            -- MANAGER/OPENER/NURTURER/SERVER
                message     TEXT NOT NULL,
                lead_id     TEXT,
                created_at  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_leads_status      ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_steps_lead        ON sequence_steps(lead_id);
            CREATE INDEX IF NOT EXISTS idx_convos_lead       ON conversations(lead_id);
            CREATE INDEX IF NOT EXISTS idx_logs_created      ON system_logs(created_at);
        """)
    log.info("Database initialised ✓")


# ════════════════════════════════════════════════════════════
# Leads
# ════════════════════════════════════════════════════════════

def upsert_lead(lead: dict, classification: dict = None, status: str = "new") -> bool:
    """
    Inserts a new lead or updates an existing one.
    Safe to call multiple times for the same lead.
    """
    now = _now()
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM leads WHERE id = ?", (lead["id"],)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE leads SET
                        name=?, email=?, phone=?, lead_type=?, source=?,
                        status=?, classification=?, updated_at=?
                    WHERE id=?
                """, (
                    lead.get("name"), lead.get("email"), lead.get("phone"),
                    lead.get("lead_type", "unknown"), lead.get("source", ""),
                    status,
                    json.dumps(classification) if classification else None,
                    now, lead["id"]
                ))
            else:
                conn.execute("""
                    INSERT INTO leads
                        (id, name, email, phone, lead_type, source, status,
                         classification, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    lead["id"], lead.get("name"), lead.get("email"),
                    lead.get("phone"), lead.get("lead_type", "unknown"),
                    lead.get("source", ""), status,
                    json.dumps(classification) if classification else None,
                    now, now
                ))
        return True
    except Exception as e:
        log.error(f"upsert_lead({lead.get('id')}) failed: {e}")
        return False


def update_lead_status(lead_id: str, status: str, **kwargs) -> bool:
    """Updates a lead's status and any additional fields passed as kwargs."""
    allowed = {"urgency", "opener_fired", "nurturer_step", "nurturer_status", "classification"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    fields["status"]     = status
    fields["updated_at"] = _now()

    set_clause = ", ".join(f"{k}=?" for k in fields)
    values     = list(fields.values()) + [lead_id]

    try:
        with get_db() as conn:
            conn.execute(f"UPDATE leads SET {set_clause} WHERE id=?", values)
        return True
    except Exception as e:
        log.error(f"update_lead_status({lead_id}) failed: {e}")
        return False


def get_lead(lead_id: str) -> dict | None:
    """Returns a single lead as a dict, or None if not found."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            if row:
                d = dict(row)
                if d.get("classification"):
                    d["classification"] = json.loads(d["classification"])
                return d
        return None
    except Exception as e:
        log.error(f"get_lead({lead_id}) failed: {e}")
        return None


def get_all_leads(status: str = None, limit: int = 200) -> list[dict]:
    """Returns all leads, optionally filtered by status."""
    try:
        with get_db() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM leads WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM leads ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get("classification"):
                    d["classification"] = json.loads(d["classification"])
                result.append(d)
            return result
    except Exception as e:
        log.error(f"get_all_leads() failed: {e}")
        return []


# ════════════════════════════════════════════════════════════
# Sequence Steps
# ════════════════════════════════════════════════════════════

def log_sequence_step(lead_id: str, day: int, channel: str,
                      theme: str, status: str, message: str = "",
                      result: dict = None) -> bool:
    """Records a nurture sequence step being sent (or skipped/failed)."""
    try:
        with get_db() as conn:
            # Update if already exists, otherwise insert
            existing = conn.execute(
                "SELECT id FROM sequence_steps WHERE lead_id=? AND day=?",
                (lead_id, day)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE sequence_steps
                    SET status=?, sent_at=?, message=?, result=?
                    WHERE lead_id=? AND day=?
                """, (status, _now(), message, json.dumps(result or {}), lead_id, day))
            else:
                conn.execute("""
                    INSERT INTO sequence_steps
                        (lead_id, day, channel, theme, status, sent_at, message, result)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (lead_id, day, channel, theme, status, _now(),
                      message, json.dumps(result or {})))
        return True
    except Exception as e:
        log.error(f"log_sequence_step({lead_id}, day={day}) failed: {e}")
        return False


def get_sequence_steps(lead_id: str) -> list[dict]:
    """Returns all sequence steps for a lead."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM sequence_steps WHERE lead_id=? ORDER BY day",
                (lead_id,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_sequence_steps({lead_id}) failed: {e}")
        return []


# ════════════════════════════════════════════════════════════
# Conversations
# ════════════════════════════════════════════════════════════

def save_message(lead_id: str, role: str, content: str, channel: str = "") -> bool:
    """Saves a single message to the conversation history."""
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO conversations (lead_id, role, channel, content, created_at)
                VALUES (?,?,?,?,?)
            """, (lead_id, role, channel, content, _now()))
        return True
    except Exception as e:
        log.error(f"save_message({lead_id}) failed: {e}")
        return False


def get_conversation(lead_id: str, limit: int = 50) -> list[dict]:
    """Returns the conversation history for a lead."""
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT role, channel, content, created_at
                FROM conversations
                WHERE lead_id=?
                ORDER BY created_at ASC
                LIMIT ?
            """, (lead_id, limit)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_conversation({lead_id}) failed: {e}")
        return []


# ════════════════════════════════════════════════════════════
# System Logs
# ════════════════════════════════════════════════════════════

def add_log(level: str, message: str, source: str = "SYSTEM", lead_id: str = None) -> bool:
    """Adds an entry to the system log (shown in dashboard Logs page)."""
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO system_logs (level, source, message, lead_id, created_at)
                VALUES (?,?,?,?,?)
            """, (level.upper(), source, message, lead_id, _now()))
        return True
    except Exception as e:
        log.error(f"add_log() failed: {e}")
        return False


def get_logs(limit: int = 200, lead_id: str = None) -> list[dict]:
    """Returns recent system logs, optionally filtered by lead."""
    try:
        with get_db() as conn:
            if lead_id:
                rows = conn.execute("""
                    SELECT * FROM system_logs
                    WHERE lead_id=?
                    ORDER BY created_at DESC LIMIT ?
                """, (lead_id, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM system_logs
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_logs() failed: {e}")
        return []


def get_stats() -> dict:
    """Returns aggregate stats for the dashboard stat tiles."""
    try:
        with get_db() as conn:
            total     = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            active    = conn.execute("SELECT COUNT(*) FROM leads WHERE status NOT IN ('handed_off','completed','stopped')").fetchone()[0]
            qualified = conn.execute("SELECT COUNT(*) FROM leads WHERE status='handed_off'").fetchone()[0]
            sms_sent  = conn.execute("SELECT COUNT(*) FROM sequence_steps WHERE channel='sms' AND status='sent'").fetchone()[0]
            email_sent= conn.execute("SELECT COUNT(*) FROM sequence_steps WHERE channel='email' AND status='sent'").fetchone()[0]
            replies   = conn.execute("SELECT COUNT(*) FROM conversations WHERE role='user'").fetchone()[0]
            return {
                "total_leads":   total,
                "active_leads":  active,
                "qualified":     qualified,
                "sms_sent":      sms_sent,
                "emails_sent":   email_sent,
                "total_replies": replies,
            }
    except Exception as e:
        log.error(f"get_stats() failed: {e}")
        return {}


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
