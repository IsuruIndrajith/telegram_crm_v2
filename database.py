"""
database.py — SQLite database layer for Nexora Pips Telegram CRM v2
Tables: admin_users, clients, broadcasts, message_logs
"""

import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash


class Database:
    def __init__(self, db_path: str = "crm.db"):
        self.db_path = db_path
        self._init_tables()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ─────────────────────────── Schema ────────────────────────────

    def _init_tables(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT    NOT NULL UNIQUE,
                    password_hash TEXT    NOT NULL,
                    role          TEXT    NOT NULL DEFAULT 'admin',
                    created_at    TEXT    DEFAULT (datetime('now')),
                    last_login    TEXT
                );

                CREATE TABLE IF NOT EXISTS clients (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id TEXT    UNIQUE,
                    username    TEXT,
                    name        TEXT,
                    notes       TEXT,
                    status      TEXT    DEFAULT 'active',
                    added_at    TEXT    DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS broadcasts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    message      TEXT    NOT NULL,
                    media_path   TEXT,
                    total        INTEGER DEFAULT 0,
                    sent         INTEGER DEFAULT 0,
                    failed       INTEGER DEFAULT 0,
                    status       TEXT    DEFAULT 'in_progress',
                    sent_by      TEXT,
                    started_at   TEXT    DEFAULT (datetime('now')),
                    finished_at  TEXT
                );

                CREATE TABLE IF NOT EXISTS message_logs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    broadcast_id INTEGER NOT NULL,
                    client_id    INTEGER NOT NULL,
                    status       TEXT    NOT NULL,
                    error_msg    TEXT,
                    sent_at      TEXT    DEFAULT (datetime('now')),
                    FOREIGN KEY (broadcast_id) REFERENCES broadcasts(id),
                    FOREIGN KEY (client_id)    REFERENCES clients(id)
                );
            """)

    # ──────────────────────── Admin Users ──────────────────────────

    def create_admin(self, username: str, password: str, role: str = "admin") -> bool:
        """Create a new admin user. Returns False if username taken."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, ?)",
                    (username.strip(), generate_password_hash(password), role)
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_admin_by_id(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_admin_by_username(self, username: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE username = ?", (username.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def verify_admin(self, username: str, password: str) -> dict | None:
        """Return admin dict if credentials valid, else None."""
        admin = self.get_admin_by_username(username)
        if admin and check_password_hash(admin["password_hash"], password):
            self._update_last_login(admin["id"])
            return admin
        return None

    def _update_last_login(self, user_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE admin_users SET last_login = datetime('now') WHERE id = ?",
                (user_id,)
            )

    def get_all_admins(self) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, username, role, created_at, last_login FROM admin_users ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_admin(self, user_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))

    def change_password(self, user_id: int, new_password: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE admin_users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user_id)
            )

    def admin_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]

    # ─────────────────────────── Clients ───────────────────────────

    def add_client(self, telegram_id: str, username: str, name: str, notes: str = "") -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO clients (telegram_id, username, name, notes) VALUES (?, ?, ?, ?)",
                    (telegram_id or None, username or None, name or None, notes or None)
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_client(self, client_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))

    def toggle_client_status(self, client_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE clients SET status = CASE WHEN status='active' THEN 'inactive' ELSE 'active' END WHERE id = ?",
                (client_id,)
            )

    def get_all_clients(self) -> list:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM clients ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get_active_clients(self) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clients WHERE status = 'active' ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_client_count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM clients WHERE status = 'active'"
            ).fetchone()[0]

    def search_clients(self, query: str) -> list:
        q = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clients WHERE name LIKE ? OR username LIKE ? OR telegram_id LIKE ? ORDER BY name",
                (q, q, q)
            ).fetchall()
        return [dict(r) for r in rows]

    # ────────────────────────── Broadcasts ─────────────────────────

    def create_broadcast(self, message: str, total: int, media_path: str = None, sent_by: str = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO broadcasts (message, media_path, total, sent_by) VALUES (?, ?, ?, ?)",
                (message, media_path, total, sent_by)
            )
            return cur.lastrowid

    def update_broadcast_progress(self, broadcast_id: int, sent: int, failed: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE broadcasts SET sent = ?, failed = ? WHERE id = ?",
                (sent, failed, broadcast_id)
            )

    def complete_broadcast(self, broadcast_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE broadcasts SET status = 'completed', finished_at = datetime('now') WHERE id = ?",
                (broadcast_id,)
            )

    def get_broadcast(self, broadcast_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
        return dict(row) if row else {}

    def get_all_broadcasts(self) -> list:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM broadcasts ORDER BY started_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get_recent_broadcasts(self, limit: int = 5) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM broadcasts ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ───────────────────────── Message Logs ────────────────────────

    def log_message(self, broadcast_id: int, client_id: int, status: str, error: str = None):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO message_logs (broadcast_id, client_id, status, error_msg) VALUES (?, ?, ?, ?)",
                (broadcast_id, client_id, status, error)
            )

    def get_broadcast_details(self, broadcast_id: int) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ml.status, ml.error_msg, ml.sent_at,
                          c.name, c.username, c.telegram_id
                   FROM message_logs ml
                   JOIN clients c ON c.id = ml.client_id
                   WHERE ml.broadcast_id = ?
                   ORDER BY ml.sent_at""",
                (broadcast_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ───────────────────────────── Stats ───────────────────────────

    def get_stats(self) -> dict:
        with self._connect() as conn:
            total      = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            active     = conn.execute("SELECT COUNT(*) FROM clients WHERE status='active'").fetchone()[0]
            broadcasts = conn.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]
            msgs_sent  = conn.execute("SELECT COALESCE(SUM(sent),0) FROM broadcasts").fetchone()[0]
            admins     = conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
        return {
            "total_clients": total,
            "active_clients": active,
            "inactive_clients": total - active,
            "total_broadcasts": broadcasts,
            "total_messages_sent": msgs_sent,
            "total_admins": admins,
        }
