"""Chat and order log on disk (CJ, 2026-10-22: 「從現在開始這些對話紀錄跟訂單紀錄都要記錄下來」).

Every Agent Nemo chat turn (the supervisor's words and the reply) and every order event (a new or rush order arriving,
an order completed, a late delivery, a shipment leaving) is appended to SQLite at data/runtime/history.sqlite
(gitignored). Append-only: there is no delete, so a server restart, a scenario load, 重來 and the day-loop wrap all keep
it. A reset only starts a new chat session id (the epoch below); the old session's rows stay.

The audit log carries the same chat and order rows, but its chain is archived (`audit.jsonl.broken-N`) and restarted
when it breaks; this log does not depend on it. Read by GET /api/history/chat and /api/history/orders, and by
query_plant (chat_history, orders_arrived) for the overview chat."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from warroom import factory as F

MAX_TEXT = 4000


def _day(ts: datetime) -> str:
    return ts.astimezone(F.TZ).date().isoformat()


class HistoryLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.epoch = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")   # a new session on restart / reset
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS chat_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, "
                       "logged_at TEXT, session_id TEXT, scope TEXT, actor TEXT, role TEXT, text TEXT, turn_id TEXT, "
                       "event_id TEXT, by TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS chat_day ON chat_log(day, id)")
            db.execute("CREATE INDEX IF NOT EXISTS chat_session ON chat_log(session_id, id)")
            db.execute("CREATE INDEX IF NOT EXISTS chat_event ON chat_log(event_id, id)")
            db.execute("CREATE TABLE IF NOT EXISTS order_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, "
                       "logged_at TEXT, kind TEXT, order_id TEXT, customer TEXT, spec TEXT, qty INTEGER, due TEXT, "
                       "priority INTEGER, on_time INTEGER, text TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS order_day ON order_log(day, id)")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def new_session(self) -> None:
        """A scenario load / reset / day wrap: later turns get a new session id; nothing is deleted."""
        self.epoch = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f")

    def session_id(self, conversation: str) -> str:
        return f"{conversation}@{self.epoch}"

    # ---------------------------------------------------------------- writes (append only)
    def add_chat(self, ts: datetime, conversation: str, scope: str, actor: str, text: str, reply: str,
                 turn_id: str | None = None, event_id: str | None = None, by: str | None = None) -> None:
        now, sid = datetime.now().astimezone().isoformat(), self.session_id(conversation)
        rows = [(ts.isoformat(), _day(ts), now, sid, scope, actor, role, body[:MAX_TEXT], turn_id, event_id, by)
                for role, body in (("user", text), ("assistant", reply))]
        with self._lock, self._db() as db:
            db.executemany("INSERT INTO chat_log (ts, day, logged_at, session_id, scope, actor, role, text, turn_id, "
                           "event_id, by) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    def add_order(self, ts: datetime, kind: str, order_id: str | None, text: str, order: Any = None,
                  qty: int | None = None, on_time: bool | None = None) -> None:
        spec = getattr(getattr(order, "size", None), "value", getattr(order, "size", None))
        due = getattr(order, "due", None)
        row = (ts.isoformat(), _day(ts), datetime.now().astimezone().isoformat(), kind, order_id,
               getattr(order, "customer", None), str(spec) if spec is not None else None,
               getattr(order, "qty", None) if qty is None else qty, due.isoformat() if due is not None else None,
               getattr(order, "priority", None), None if on_time is None else int(on_time), text[:MAX_TEXT])
        with self._lock, self._db() as db:
            db.execute("INSERT INTO order_log (ts, day, logged_at, kind, order_id, customer, spec, qty, due, priority, "
                       "on_time, text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)

    # ---------------------------------------------------------------- reads
    def chats(self, day: str | None = None, session: str | None = None, scope: str | None = None,
              limit: int = 200, event_id: str | None = None) -> list[dict[str, Any]]:
        """Chat rows, oldest first (the last `limit` that match). `event_id`: one task's session (issue #189)."""
        where, args = [], []
        for col, v in (("day", day), ("session_id", session), ("scope", scope), ("event_id", event_id)):
            if v:
                where.append(f"{col}=?")
                args.append(v)
        q = (f"SELECT * FROM chat_log {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY id DESC LIMIT ?")
        with self._lock, self._db() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(q, (*args, max(1, limit)))]
        return rows[::-1]

    def sessions(self, day: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """One line per chat session, newest first: when, scope, turns, the first thing the supervisor said."""
        q = ("SELECT session_id, scope, actor, MIN(ts), MAX(ts), SUM(role='user'), "
             "(SELECT text FROM chat_log c2 WHERE c2.session_id=c.session_id AND role='user' ORDER BY id LIMIT 1) "
             f"FROM chat_log c {'WHERE day=?' if day else ''} GROUP BY session_id ORDER BY MAX(id) DESC LIMIT ?")
        with self._lock, self._db() as db:
            rows = db.execute(q, ((day,) if day else ()) + (max(1, limit),)).fetchall()
        return [{"session_id": r[0], "scope": r[1], "actor": r[2], "first": r[3], "last": r[4], "turns": r[5],
                 "opened_with": r[6]} for r in rows]

    def orders(self, day: str | None = None, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Order rows, oldest first (the last `limit` that match)."""
        where, args = [], []
        for col, v in (("day", day), ("kind", kind)):
            if v:
                where.append(f"{col}=?")
                args.append(v)
        q = f"SELECT * FROM order_log {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY id DESC LIMIT ?"
        with self._lock, self._db() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(q, (*args, max(1, limit)))]
        return rows[::-1]
