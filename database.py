"""
database.py — SQLite шар для ESP Line Logger
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "esp_data.db"


class Database:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._lock   = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS line_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id   TEXT    NOT NULL,
                    event_type  TEXT    NOT NULL,
                    esp_ts      INTEGER,
                    received_at TEXT    NOT NULL,
                    cycle       INTEGER,
                    duration    REAL,
                    buffered    INTEGER DEFAULT 0,
                    uptime      INTEGER,
                    rssi        INTEGER,
                    buf_size    INTEGER,
                    version     TEXT,
                    speed       REAL,
                    raw_payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_esp_ts  ON line_events (esp_ts);
                CREATE INDEX IF NOT EXISTS idx_device  ON line_events (device_id);
                CREATE INDEX IF NOT EXISTS idx_type    ON line_events (event_type);

                -- Причини простоїв (інтерактивний таймлайн)
                CREATE TABLE IF NOT EXISTS downtime_periods (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id  TEXT NOT NULL,
                    start_ts   INTEGER NOT NULL,
                    stop_ts    INTEGER NOT NULL,
                    reason     TEXT,
                    comment    TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (device_id, start_ts, stop_ts)
                );
                CREATE INDEX IF NOT EXISTS idx_down_device ON downtime_periods(device_id);
                CREATE INDEX IF NOT EXISTS idx_down_start  ON downtime_periods(start_ts);
            """)

            # Сумісність: для старих БД додаємо колонку speed, якщо її ще нема.
            try:
                cols = {r["name"] for r in c.execute("PRAGMA table_info(line_events)").fetchall()}
                if "speed" not in cols:
                    c.execute("ALTER TABLE line_events ADD COLUMN speed REAL")
            except Exception:
                # Якщо щось піде не так — нехай логіка працює як раніше без speed.
                pass
        print(f"[DB] {self.db_path}")

    # ── Write ──────────────────────────────────────────────────────────
    def insert_event(self, data: dict) -> int:
        received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock, self._conn() as c:
            cur = c.execute("""
                INSERT INTO line_events
                    (device_id, event_type, esp_ts, received_at,
                     cycle, duration, buffered,
                     uptime, rssi, buf_size, version, speed, raw_payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                data.get("device_id"),
                data.get("event_type"),
                data.get("ts"),
                received_at,
                data.get("cycle"),
                data.get("dur"),
                1 if data.get("buffered") else 0,
                data.get("uptime"),
                data.get("rssi"),
                data.get("buf"),
                data.get("version"),
                data.get("speed"),
                data.get("raw"),
            ))
            return cur.lastrowid

    # ── Read ───────────────────────────────────────────────────────────
    def fetch_line_events(self, limit: int = 200,
                          device_id: str = None,
                          event_type: str = None) -> list[dict]:
        wheres, params = [], []
        if device_id:
            wheres.append("device_id = ?"); params.append(device_id)
        if event_type:
            wheres.append("event_type = ?"); params.append(event_type)
        where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        with self._lock, self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM line_events {where} ORDER BY esp_ts DESC LIMIT ?",
                params
            ).fetchall()
        return [dict(r) for r in rows]

    def fetch_cycles(
        self,
        device_id: str = None,
        limit: int = 200,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> list[dict]:
        """
        Повертає завершені цикли (пари line_start + line_stop) в межах часового вікна.

        Правило включення:
        - цикл потрапляє у відповідь тільки якщо start_ts і stop_ts обидва у [from_ts, to_ts]
        """
        where_clauses: list[str] = []
        params: list = []

        if device_id:
            where_clauses.append("device_id = ?")
            params.append(device_id)

        if from_ts is not None and to_ts is not None:
            where_clauses.append("esp_ts BETWEEN ? AND ?")
            params.extend([from_ts, to_ts])
        elif from_ts is not None:
            where_clauses.append("esp_ts >= ?")
            params.append(from_ts)
        elif to_ts is not None:
            where_clauses.append("esp_ts <= ?")
            params.append(to_ts)

        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        with self._lock, self._conn() as c:
            starts = [dict(r) for r in c.execute(
                f"""
                SELECT device_id, cycle, esp_ts AS start_ts, buffered
                FROM line_events
                WHERE event_type='line_start'
                {(" AND " + where[6:]) if where else ""}
                ORDER BY esp_ts ASC
                """.strip()
                , params
            ).fetchall()]

            stops = [dict(r) for r in c.execute(
                f"""
                SELECT device_id, cycle, esp_ts AS stop_ts, duration, buffered
                FROM line_events
                WHERE event_type='line_stop'
                {(" AND " + where[6:]) if where else ""}
                ORDER BY esp_ts ASC
                """.strip()
                , params
            ).fetchall()]

        # Pair by cycle number (present in both start/stop events from ESP firmware)
        start_map: dict[tuple[str, int], dict] = {}
        for s in starts:
            key = (s["device_id"], s["cycle"])
            start_map[key] = s

        complete: list[dict] = []
        for st in stops:
            key = (st["device_id"], st["cycle"])
            if key not in start_map:
                continue
            start = start_map[key]

            start_ts = start.get("start_ts")
            stop_ts = st.get("stop_ts")
            if not start_ts or not stop_ts:
                continue

            dur = st.get("duration")
            if dur is None:
                dur = (stop_ts - start_ts) if stop_ts and start_ts else None

            complete.append({
                "device_id": st["device_id"],
                "cycle": st["cycle"],
                "start_ts": start_ts,
                "stop_ts": stop_ts,
                "duration": dur,
                # "buffered" from stop; start also had it, so OR them for safety
                "buffered": bool(st.get("buffered")) or bool(start.get("buffered")),
            })

        # Limit to most recent completed cycles by stop_ts,
        # but keep chronological order for charts.
        complete.sort(key=lambda x: x["stop_ts"] or 0, reverse=True)
        complete = complete[: max(0, int(limit))]
        complete.sort(key=lambda x: x["start_ts"] or 0)

        return complete

    def fetch_stats(self, device_id: str = None) -> dict:
        """Загальна статистика для дашборду."""
        params: list = []
        where = ""
        if device_id:
            where = "WHERE device_id = ?"
            params.append(device_id)

        with self._lock, self._conn() as c:
            total   = c.execute(
                f"SELECT COUNT(*) FROM line_events {where}", params).fetchone()[0]
            starts  = c.execute(
                f"SELECT COUNT(*) FROM line_events {where}"
                + (" AND " if where else " WHERE ") + "event_type='line_start'",
                params).fetchone()[0]
            avg_dur = c.execute(
                f"SELECT AVG(duration) FROM line_events {where}"
                + (" AND " if where else " WHERE ") + "event_type='line_stop' AND duration IS NOT NULL",
                params).fetchone()[0]
            last_ts = c.execute(
                f"SELECT MAX(esp_ts) FROM line_events {where}", params).fetchone()[0]

        return {
            "total_events":  total,
            "total_cycles":  starts,
            "avg_duration":  round(avg_dur, 1) if avg_dur else None,
            "last_event_ts": last_ts,
        }

    def fetch_devices(self) -> list[str]:
        with self._lock, self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT device_id FROM line_events ORDER BY device_id"
            ).fetchall()]

    def count(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM line_events").fetchone()[0]

    # ── Timeline + downtime ─────────────────────────────────────────────
    def set_downtime(self, device_id: str, start_ts: int, stop_ts: int,
                     reason: str | None, comment: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock, self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO downtime_periods
                    (device_id, start_ts, stop_ts, reason, comment, created_at, updated_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?)
            """, (device_id, start_ts, stop_ts, reason, comment, now, now))

    def fetch_cycles_overlap(
        self,
        device_id: str,
        from_ts: int,
        to_ts: int,
        limit: int = 2000,
    ) -> list[dict]:
        """
        Повертає завершені цикли, які перетинають [from_ts, to_ts]
        (тобто start_ts <= to_ts і stop_ts >= from_ts).

        Під час парування ігноруємо поле `cycle` і будуємо стан-машину:
        береться останній незакритий `line_start` і париться з першим `line_stop`
        (щоб уникнути помилок, коли ESP/буфер дає дублікати або цикл "з'їжджає"
        після ребутів).
        
        Швидкість розраховується як середнє з усіх heartbeat з полем speed
        між start та stop.
        """
        if to_ts < from_ts:
            from_ts, to_ts = to_ts, from_ts

        with self._lock, self._conn() as c:
            starts = [dict(r) for r in c.execute(
                """
                SELECT cycle, esp_ts AS start_ts, buffered, speed
                FROM line_events
                WHERE event_type='line_start'
                  AND device_id=?
                  AND esp_ts <= ?
                ORDER BY esp_ts DESC
                LIMIT ?
                """,
                (device_id, to_ts, limit * 5),
            ).fetchall()]

            stops = [dict(r) for r in c.execute(
                """
                SELECT cycle, esp_ts AS stop_ts, duration, buffered
                FROM line_events
                WHERE event_type='line_stop'
                  AND device_id=?
                  AND esp_ts >= ?
                ORDER BY esp_ts ASC
                LIMIT ?
                """,
                (device_id, from_ts, limit * 5),
            ).fetchall()]

        events: list[dict] = []
        for s in starts:
            if s.get("start_ts") is None:
                continue
            events.append({"type": "line_start", **s})
        for st in stops:
            if st.get("stop_ts") is None:
                continue
            events.append({"type": "line_stop", **st})

        events.sort(key=lambda e: e.get("start_ts", e.get("stop_ts", 0)) or 0)

        pending = None  # last open line_start (without stop yet)
        complete: list[dict] = []
        for e in events:
            if e["type"] == "line_start":
                if pending is None:
                    pending = e
            elif e["type"] == "line_stop":
                if pending is None:
                    continue
                start_ts = pending.get("start_ts")
                stop_ts = e.get("stop_ts")
                if start_ts is None or stop_ts is None:
                    pending = None
                    continue
                if start_ts <= to_ts and stop_ts >= from_ts:
                    dur = e.get("duration")
                    if dur is None:
                        dur = (stop_ts - start_ts) if stop_ts and start_ts else None
                    
                    # Calculate average speed from heartbeats in this cycle
                    avg_speed = None
                    with self._lock, self._conn() as c2:
                        avg_result = c2.execute("""
                            SELECT AVG(speed) 
                            FROM line_events 
                            WHERE event_type='heartbeat' 
                              AND device_id=? 
                              AND esp_ts BETWEEN ? AND ?
                              AND speed IS NOT NULL
                        """, (device_id, start_ts, stop_ts)).fetchone()
                        if avg_result and avg_result[0] is not None:
                            avg_speed = round(avg_result[0], 2)
                    
                    # Fallback to speed from line_start if no heartbeats with speed
                    if avg_speed is None:
                        avg_speed = pending.get("speed")
                    
                    complete.append({
                        "device_id": device_id,
                        "cycle": e.get("cycle"),
                        "start_ts": start_ts,
                        "stop_ts": stop_ts,
                        "duration": dur,
                        "buffered": bool(e.get("buffered")) or bool(pending.get("buffered")),
                        "speed": avg_speed,
                    })
                pending = None

        complete.sort(key=lambda x: x["start_ts"] or 0)
        return complete[: max(0, int(limit))]

    def fetch_timeline(self, device_id: str, from_ts: int, to_ts: int) -> list[dict]:
        """
        Повертає послідовність інтервалів:
        - type='run'  : start -> stop
        - type='down' : stop -> next start (і крайові частини)
        """
        if to_ts < from_ts:
            from_ts, to_ts = to_ts, from_ts

        cycles = self.fetch_cycles_overlap(
            device_id=device_id,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=2000,
        )

        intervals: list[dict] = []
        cursor = from_ts

        for c in cycles:
            c_start = c.get("start_ts")
            c_stop = c.get("stop_ts")
            if c_start is None or c_stop is None:
                continue

            if c_start > cursor and cursor < to_ts:
                down_start = cursor
                down_stop = min(c_start, to_ts)
                if down_stop > down_start:
                    intervals.append({
                        "device_id": device_id,
                        "type": "down",
                        "start_ts": down_start,
                        "stop_ts": down_stop,
                        "reason": None,
                        "comment": None,
                    })

            run_start = max(c_start, from_ts)
            run_stop = min(c_stop, to_ts)
            if run_stop > run_start:
                intervals.append({
                    "device_id": device_id,
                    "type": "run",
                    "cycle": c.get("cycle"),
                    "start_ts": run_start,
                    "stop_ts": run_stop,
                    "speed": c.get("speed"),
                })

            cursor = min(c_stop, to_ts)
            if cursor >= to_ts:
                break

        # Крайній простій після останнього циклу в межах вікна
        if cursor < to_ts:
            intervals.append({
                "device_id": device_id,
                "type": "down",
                "start_ts": cursor,
                "stop_ts": to_ts,
                "reason": None,
                "comment": None,
            })

        # Підтягнемо з БД вже збережені причини для downtime сегментів
        down_pairs = [(iv["start_ts"], iv["stop_ts"]) for iv in intervals if iv["type"] == "down"]
        if down_pairs:
            with self._lock, self._conn() as c:
                rows = [dict(r) for r in c.execute(
                    """
                    SELECT start_ts, stop_ts, reason, comment
                    FROM downtime_periods
                    WHERE device_id=? AND start_ts>=? AND stop_ts<=?
                    """,
                    (device_id, from_ts, to_ts),
                ).fetchall()]
            meta = {(r["start_ts"], r["stop_ts"]): r for r in rows}
            for iv in intervals:
                if iv["type"] != "down":
                    continue
                key = (iv["start_ts"], iv["stop_ts"])
                if key in meta:
                    iv["reason"] = meta[key].get("reason")
                    iv["comment"] = meta[key].get("comment")

        return intervals

    def fetch_speed_data(self, device_id: str, from_ts: int, to_ts: int) -> list[dict]:
        """
        Fetch speed data points for chart.
        Returns speed_update events and cycle average speeds.
        """
        with self._lock, self._conn() as c:
            # Get speed_update events (real-time speed during operation)
            speed_updates = [dict(r) for r in c.execute("""
                SELECT esp_ts AS ts, speed, 'speed_update' AS source, cycle
                FROM line_events
                WHERE event_type = 'speed_update'
                  AND device_id = ?
                  AND esp_ts BETWEEN ? AND ?
                  AND speed IS NOT NULL
                ORDER BY esp_ts ASC
            """, (device_id, from_ts, to_ts)).fetchall()]
            
            # Get line_start speeds (initial speed at cycle start)
            starts = [dict(r) for r in c.execute("""
                SELECT esp_ts AS ts, speed, 'start' AS source, cycle
                FROM line_events
                WHERE event_type = 'line_start'
                  AND device_id = ?
                  AND esp_ts BETWEEN ? AND ?
                  AND speed IS NOT NULL
                ORDER BY esp_ts ASC
            """, (device_id, from_ts, to_ts)).fetchall()]
        
        # Calculate average speed per cycle from speed_updates
        cycles = self.fetch_cycles_overlap(device_id, from_ts, to_ts, limit=2000)
        cycle_avgs = []
        for c in cycles:
            start_ts = c.get("start_ts")
            stop_ts = c.get("stop_ts")
            if start_ts is None or stop_ts is None:
                continue
                
            # Get average from speed_updates for this cycle
            with self._lock, self._conn() as c2:
                avg_result = c2.execute("""
                    SELECT AVG(speed)
                    FROM line_events
                    WHERE event_type = 'speed_update'
                      AND device_id = ?
                      AND esp_ts BETWEEN ? AND ?
                      AND speed IS NOT NULL
                """, (device_id, start_ts, stop_ts)).fetchone()
                
                avg_speed = None
                if avg_result and avg_result[0] is not None:
                    avg_speed = round(avg_result[0], 2)
                
                # Fallback to speed from line_start if no speed_updates
                if avg_speed is None:
                    avg_speed = c.get("speed")
                
                if avg_speed is not None:
                    cycle_avgs.append({
                        "ts": (start_ts + stop_ts) // 2,
                        "speed": avg_speed,
                        "source": "cycle_avg",
                        "cycle": c.get("cycle")
                    })
        
        # Combine all speed data
        all_speeds = speed_updates + starts + cycle_avgs
        all_speeds.sort(key=lambda x: x["ts"] or 0)
        
        return all_speeds
