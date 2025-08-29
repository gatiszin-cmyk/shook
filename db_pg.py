import os
import psycopg2
from urllib.parse import urlparse

_conn = None

def _connect():
    global _conn
    if _conn is not None:
        return _conn
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    p = urlparse(url)
    conn = psycopg2.connect(
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        host=p.hostname,
        port=p.port,
        sslmode="require"  # Railway typically supports SSL
    )
    conn.autocommit = True
    _conn = conn
    return _conn

def init_db():
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            section TEXT NOT NULL,
            admin_msg_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        # Optional index to speed up lookups:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_admin_msg_id ON tickets(admin_msg_id);")

def save_ticket(user_id: int, section: str, admin_msg_id: int) -> int:
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tickets (user_id, section, admin_msg_id) VALUES (%s, %s, %s) RETURNING ticket_id;",
            (user_id, section, admin_msg_id)
        )
        ticket_id = cur.fetchone()
        return ticket_id

def get_ticket_by_admin_msg_id(admin_msg_id: int):
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_id, user_id, section FROM tickets WHERE admin_msg_id=%s;",
            (admin_msg_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"ticket_id": row, "user_id": row[8], "section": row[9]}
