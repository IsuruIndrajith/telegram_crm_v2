"""
database.py — SQLite database layer for Nexora Pips Telegram CRM v2
Tables: admin_users, clients, broadcasts, message_logs
Clients and broadcasts are scoped per admin_id.
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
                    admin_id    INTEGER NOT NULL DEFAULT 0,
                    telegram_id TEXT,
                    username    TEXT,
                    name        TEXT,
                    notes       TEXT,
                    status      TEXT    DEFAULT 'active',
                    added_at    TEXT    DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS broadcasts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id     INTEGER NOT NULL DEFAULT 0,
                    message      TEXT    NOT NULL,
                    media_path   TEXT,
                    total        INTEGER DEFAULT 0,
                    sent         INTEGER DEFAULT 0,
                    failed       INTEGER DEFAULT 0,
                    status       TEXT    DEFAULT 'in_progress',
                    sent_by      TEXT,
                    min_delay    REAL    DEFAULT 3.0,
                    max_delay    REAL    DEFAULT 7.0,
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
            # Migrate existing DBs that lack columns
            for migration in [
                "ALTER TABLE clients ADD COLUMN admin_id INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE clients ADD COLUMN access_hash TEXT DEFAULT NULL",
                "ALTER TABLE broadcasts ADD COLUMN admin_id INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE broadcasts ADD COLUMN min_delay REAL DEFAULT 3.0",
                "ALTER TABLE broadcasts ADD COLUMN max_delay REAL DEFAULT 7.0",
                "ALTER TABLE broadcasts ADD COLUMN abort_reason TEXT DEFAULT NULL",
                "ALTER TABLE broadcasts ADD COLUMN batch_size INTEGER DEFAULT 50",
                "ALTER TABLE broadcasts ADD COLUMN batch_pause INTEGER DEFAULT 300",
                "ALTER TABLE broadcasts ADD COLUMN phase TEXT DEFAULT 'sending'",
                "ALTER TABLE broadcasts ADD COLUMN current_batch INTEGER DEFAULT 1",
                "ALTER TABLE broadcasts ADD COLUMN total_batches INTEGER DEFAULT 1",
                "ALTER TABLE broadcasts ADD COLUMN resume_at TEXT DEFAULT NULL",
            ]:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists

            # Remove the old global UNIQUE constraint on clients.telegram_id.
            # The original schema had `telegram_id TEXT UNIQUE` which blocks
            # two different admins from sharing the same Telegram ID.
            # We rebuild the table without that constraint if it still exists.
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='clients'"
            ).fetchone()
            if row and "telegram_id TEXT    UNIQUE" in row[0]:
                conn.executescript("""
                    PRAGMA foreign_keys = OFF;

                    CREATE TABLE clients_new (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        admin_id    INTEGER NOT NULL DEFAULT 0,
                        telegram_id TEXT,
                        username    TEXT,
                        name        TEXT,
                        notes       TEXT,
                        status      TEXT DEFAULT 'active',
                        added_at    TEXT DEFAULT (datetime('now'))
                    );

                    INSERT INTO clients_new
                        SELECT id, admin_id, telegram_id, username, name, notes, status, added_at
                        FROM clients;

                    DROP TABLE clients;
                    ALTER TABLE clients_new RENAME TO clients;

                    PRAGMA foreign_keys = ON;
                """)

    # ──────────────────────── Admin Users ──────────────────────────

    def create_admin(self, username: str, password: str, role: str = "admin") -> bool:
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

    def add_client(self, telegram_id: str, username: str, name: str,
                   notes: str = "", admin_id: int = 0, access_hash: str = None) -> bool:
        tid = telegram_id or None
        try:
            with self._connect() as conn:
                # Prevent duplicate telegram_id within the same admin's scope
                if tid:
                    exists = conn.execute(
                        "SELECT id FROM clients WHERE admin_id = ? AND telegram_id = ?",
                        (admin_id, tid)
                    ).fetchone()
                    if exists:
                        # Update access_hash if we now have one and didn't before
                        if access_hash:
                            conn.execute(
                                "UPDATE clients SET access_hash = ? WHERE admin_id = ? AND telegram_id = ? AND access_hash IS NULL",
                                (str(access_hash), admin_id, tid)
                            )
                        return False
                conn.execute(
                    "INSERT INTO clients (admin_id, telegram_id, username, name, notes, access_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    (admin_id, tid, username or None, name or None, notes or None, str(access_hash) if access_hash else None)
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def delete_client(self, client_id: int, admin_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM clients WHERE id = ? AND admin_id = ?", (client_id, admin_id))

    def delete_clients_bulk(self, client_ids: list, admin_id: int) -> int:
        if not client_ids:
            return 0
        placeholders = ",".join("?" * len(client_ids))
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM clients WHERE admin_id = ? AND id IN ({placeholders})",
                [admin_id] + list(client_ids)
            )
        return cur.rowcount

    def toggle_client_status(self, client_id: int, admin_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE clients SET status = CASE WHEN status='active' THEN 'inactive' ELSE 'active' END "
                "WHERE id = ? AND admin_id = ?",
                (client_id, admin_id)
            )

    def get_all_clients(self, admin_id: int) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clients WHERE admin_id = ? ORDER BY added_at DESC", (admin_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_clients(self, admin_id: int) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clients WHERE admin_id = ? AND status = 'active' ORDER BY name",
                (admin_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_client_count(self, admin_id: int) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM clients WHERE admin_id = ? AND status = 'active'",
                (admin_id,)
            ).fetchone()[0]

    def search_clients(self, query: str, admin_id: int) -> list:
        q = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clients WHERE admin_id = ? AND "
                "(name LIKE ? OR username LIKE ? OR telegram_id LIKE ?) ORDER BY name",
                (admin_id, q, q, q)
            ).fetchall()
        return [dict(r) for r in rows]

    # ────────────────────────── Broadcasts ─────────────────────────

    def create_broadcast(self, message: str, total: int, admin_id: int,
                         media_path: str = None, sent_by: str = None,
                         min_delay: float = 3.0, max_delay: float = 7.0,
                         batch_size: int = 50, batch_pause: int = 300) -> int:
        import math
        total_batches = max(1, math.ceil(total / batch_size)) if batch_size > 0 else 1
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO broadcasts "
                "(admin_id, message, media_path, total, sent_by, min_delay, max_delay, "
                " batch_size, batch_pause, total_batches, current_batch, phase) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'sending')",
                (admin_id, message, media_path, total, sent_by, min_delay, max_delay,
                 batch_size, batch_pause, total_batches)
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

    def update_broadcast_phase(self, broadcast_id: int, phase: str,
                               current_batch: int = None, resume_at: str = None):
        fields = ["phase = ?"]
        values = [phase]
        if current_batch is not None:
            fields.append("current_batch = ?")
            values.append(current_batch)
        if resume_at is not None:
            fields.append("resume_at = ?")
            values.append(resume_at)
        else:
            fields.append("resume_at = NULL")
        values.append(broadcast_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE broadcasts SET {', '.join(fields)} WHERE id = ?",
                values
            )

    def abort_broadcast(self, broadcast_id: int, reason: str = ""):
        with self._connect() as conn:
            conn.execute(
                "UPDATE broadcasts SET status = 'aborted', finished_at = datetime('now'), abort_reason = ? WHERE id = ?",
                (reason, broadcast_id)
            )

    def get_broadcast(self, broadcast_id: int, admin_id: int = None) -> dict:
        with self._connect() as conn:
            if admin_id is not None:
                row = conn.execute(
                    "SELECT * FROM broadcasts WHERE id = ? AND admin_id = ?",
                    (broadcast_id, admin_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)
                ).fetchone()
        return dict(row) if row else {}

    def get_all_broadcasts(self, admin_id: int) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM broadcasts WHERE admin_id = ? ORDER BY started_at DESC",
                (admin_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_broadcasts(self, admin_id: int, limit: int = 5) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM broadcasts WHERE admin_id = ? ORDER BY started_at DESC LIMIT ?",
                (admin_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ───────────────────────── Message Logs ────────────────────────

    def log_message(self, broadcast_id: int, client_id: int, status: str, error: str = None):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO message_logs (broadcast_id, client_id, status, error_msg) VALUES (?, ?, ?, ?)",
                (broadcast_id, client_id, status, error)
            )

    def get_broadcast_logs_after(self, broadcast_id: int, offset: int) -> list:
        """Return log rows after the given offset — used by SSE to push only new rows."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ml.status, ml.error_msg, ml.sent_at,
                          c.name, c.username, c.telegram_id
                   FROM message_logs ml
                   JOIN clients c ON c.id = ml.client_id
                   WHERE ml.broadcast_id = ?
                   ORDER BY ml.sent_at
                   LIMIT -1 OFFSET ?""",
                (broadcast_id, offset)
            ).fetchall()
        return [dict(r) for r in rows]

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

    def get_stats(self, admin_id: int) -> dict:
        with self._connect() as conn:
            total      = conn.execute("SELECT COUNT(*) FROM clients WHERE admin_id = ?", (admin_id,)).fetchone()[0]
            active     = conn.execute("SELECT COUNT(*) FROM clients WHERE admin_id = ? AND status='active'", (admin_id,)).fetchone()[0]
            broadcasts = conn.execute("SELECT COUNT(*) FROM broadcasts WHERE admin_id = ?", (admin_id,)).fetchone()[0]
            msgs_sent  = conn.execute("SELECT COALESCE(SUM(sent),0) FROM broadcasts WHERE admin_id = ?", (admin_id,)).fetchone()[0]
            admins     = conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
        return {
            "total_clients": total,
            "active_clients": active,
            "inactive_clients": total - active,
            "total_broadcasts": broadcasts,
            "total_messages_sent": msgs_sent,
            "total_admins": admins,
        }
