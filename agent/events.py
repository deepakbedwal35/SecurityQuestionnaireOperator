"""
Append-only monotonic event log for Security Questionnaire Operator.
Events are stored in SQLite in table: (run_id, seq, ts, type, payload_json).
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunEvent(BaseModel):
    run_id: str
    seq: int
    ts: str = Field(default_factory=_utcnow_iso)
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class EventSink:
    """
    Central event emitter. Emits append-only monotonic events per run.
    Ensures safe concurrency and transactional serialization to SQLite.
    """
    def __init__(self, db_path: str = "data/runs.sqlite") -> None:
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
            """)
            conn.commit()

    def emit(self, run_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> RunEvent:
        """
        Atomically appends a new event for run_id with the next monotonic sequence number.
        """
        payload = payload or {}
        ts = _utcnow_iso()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?", (run_id,))
            next_seq = cursor.fetchone()[0]
            payload_str = json.dumps(payload, default=str)
            cursor.execute(
                "INSERT INTO events (run_id, seq, ts, type, payload_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, next_seq, ts, event_type, payload_str),
            )
            conn.commit()
            return RunEvent(
                run_id=run_id,
                seq=next_seq,
                ts=ts,
                type=event_type,
                payload=payload,
            )

    def get_events(self, run_id: str, after_seq: int = 0) -> List[RunEvent]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT run_id, seq, ts, type, payload_json FROM events WHERE run_id = ? AND seq > ? ORDER BY seq ASC",
                (run_id, after_seq),
            )
            rows = cursor.fetchall()
            return [
                RunEvent(
                    run_id=row[0],
                    seq=row[1],
                    ts=row[2],
                    type=row[3],
                    payload=json.loads(row[4]),
                )
                for row in rows
            ]

    def has_event(self, run_id: str, event_type: str, approval_id: Optional[str] = None) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if approval_id:
                cursor.execute(
                    "SELECT payload_json FROM events WHERE run_id = ? AND type = ?",
                    (run_id, event_type),
                )
                rows = cursor.fetchall()
                for r in rows:
                    try:
                        p = json.loads(r[0])
                        if p.get("id") == approval_id or p.get("approval_id") == approval_id:
                            return True
                    except Exception:
                        pass
                return False
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id = ? AND type = ?",
                    (run_id, event_type),
                )
                return cursor.fetchone()[0] > 0

    def get_latest_event(self, run_id: str, event_type: str) -> Optional[RunEvent]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT run_id, seq, ts, type, payload_json FROM events WHERE run_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
                (run_id, event_type),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return RunEvent(
                run_id=row[0],
                seq=row[1],
                ts=row[2],
                type=row[3],
                payload=json.loads(row[4]),
            )


_DEFAULT_EVENT_SINK: Optional[EventSink] = None


def get_event_sink(db_path: Optional[str] = None) -> EventSink:
    import os
    global _DEFAULT_EVENT_SINK
    target_path = db_path or os.environ.get("CHECKPOINT_DB", "data/runs.sqlite")
    if _DEFAULT_EVENT_SINK is None or _DEFAULT_EVENT_SINK.db_path != target_path:
        _DEFAULT_EVENT_SINK = EventSink(db_path=target_path)
    return _DEFAULT_EVENT_SINK


