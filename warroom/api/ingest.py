"""Page-10 data path: machines/PLC → (Modbus/OPC UA) → MQTT → data validation + time-series DB →
event detection.

What runs here:
- `TimeSeriesStore`: every reading, with its validation result, lands in a SQLite time-series table
  (`readings`) before detection sees it. Always on.
- `MqttBridge`: when WARROOM_INGEST=mqtt, each sim tick is published to a broker (Mosquitto in the
  compose files) as one message per machine on `warroom/telemetry/<line>/<machine_id>`, and the
  runtime takes the readings back from its own subscription. That is the hop a real gateway
  (Modbus/OPC UA → MQTT) feeds; the sim stands in for the gateway. If the broker does not answer in
  time, the tick falls back to the direct readings and the miss is counted (`IngestStats`), so the
  safety core never waits on the network.
- The safety core is fed BEFORE any of this can block: Runtime runs the red path on the readings it
  has in hand, then stores and detects.

Not here: a real Modbus/OPC UA client (no PLC on this machine; the sim is the gateway), and the
INT8 time-series model, which the fitted detector (warroom.detect) stands in for.
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from warroom import factory as F
from warroom.contracts import DataQuality, SensorReading

log = logging.getLogger("warroom.ingest")

TOPIC_PREFIX = "warroom/telemetry"


@dataclass
class IngestStats:
    mode: str = "direct"
    published: int = 0
    received: int = 0
    stored: int = 0
    mqtt_missed_ticks: int = 0
    bad_quality: int = 0
    last_error: str | None = None
    broker: str | None = None
    by_quality: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class TimeSeriesStore:
    """Append-only SQLite time-series table. `path=None` keeps it in memory (tests)."""

    def __init__(self, path: Path | None = None, retention_h: float = 48.0):
        self.path = path
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self.retention_h = retention_h
        self._writes = 0
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL") if path else None
            self._db.execute("""CREATE TABLE IF NOT EXISTS readings (
                ts TEXT NOT NULL, machine_id TEXT NOT NULL, seq INTEGER, temp_c REAL, pressure_kgf_cm2 REAL,
                water_flow_lpm REAL, steam_kgf_cm2 REAL, quality TEXT NOT NULL, via TEXT NOT NULL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS ix_readings_m_ts ON readings(machine_id, ts)")
            self._db.commit()

    def write(self, readings: list[SensorReading], via: str = "direct") -> int:
        rows = [(r.ts.isoformat(), r.machine_id, r.seq, r.temp_c, r.pressure_kgf_cm2, r.water_flow_lpm,
                 r.steam_kgf_cm2, r.quality.value, via) for r in readings]
        with self._lock:
            self._db.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._writes += 1
            if self._writes % 600 == 0 and readings:       # trim every ~10 sim minutes
                cutoff = readings[-1].ts.timestamp() - self.retention_h * 3600
                self._db.execute("DELETE FROM readings WHERE ts < ?",
                                 (datetime.fromtimestamp(cutoff, F.TZ).isoformat(),))
            self._db.commit()
        return len(rows)

    def query(self, machine_id: str, since: datetime | None = None, limit: int = 5000) -> list[SensorReading]:
        sql = "SELECT ts, machine_id, seq, temp_c, pressure_kgf_cm2, water_flow_lpm, steam_kgf_cm2, quality " \
              "FROM readings WHERE machine_id = ?"
        args: list = [machine_id]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since.isoformat())
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [SensorReading(ts=datetime.fromisoformat(r[0]), machine_id=r[1], seq=r[2] or 0, temp_c=r[3],
                              pressure_kgf_cm2=r[4], water_flow_lpm=r[5], steam_kgf_cm2=r[6],
                              quality=DataQuality(r[7])) for r in reversed(rows)]

    def count(self, via: str | None = None) -> int:
        with self._lock:
            if via:
                return self._db.execute("SELECT COUNT(*) FROM readings WHERE via = ?", (via,)).fetchone()[0]
            return self._db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def topic_for(machine_id: str) -> str:
    d = F.MACHINE_BY_ID.get(machine_id)
    return f"{TOPIC_PREFIX}/{d.line.value if d else 'X'}/{machine_id}"


class MqttBridge:
    """Publishes readings to the broker and collects them back from a subscription."""

    def __init__(self, url: str = "mqtt://127.0.0.1:1883", client_id: str = "warroom-runtime",
                 connect_timeout_s: float = 3.0):
        import paho.mqtt.client as mqtt

        u = urlparse(url if "://" in url else f"mqtt://{url}")
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 1883
        self.url = f"mqtt://{self.host}:{self.port}"
        self._q: queue.Queue[SensorReading] = queue.Queue()
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self.cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{client_id}-{os.getpid()}")
        self.cli.on_connect = self._on_connect
        self.cli.on_subscribe = lambda *a, **k: self._subscribed.set()
        self.cli.on_message = self._on_message
        self.cli.on_disconnect = lambda *a, **k: self._connected.clear()
        self.cli.connect(self.host, self.port, keepalive=30)
        self.cli.loop_start()
        if not (self._connected.wait(connect_timeout_s) and self._subscribed.wait(connect_timeout_s)):
            self.close()
            raise ConnectionError(f"MQTT broker {self.url} not reachable")

    def _on_connect(self, cli, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            self._connected.set()
            cli.subscribe(f"{TOPIC_PREFIX}/#", qos=0)

    def _on_message(self, cli, userdata, msg):
        try:
            self._q.put(SensorReading.model_validate_json(msg.payload))
        except Exception as e:  # noqa: BLE001 - malformed payload is bad data, never an exception
            log.warning("bad MQTT payload on %s: %s", msg.topic, e)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def publish(self, readings: list[SensorReading]) -> int:
        n = 0
        for r in readings:
            info = self.cli.publish(topic_for(r.machine_id), r.model_dump_json(), qos=0)
            n += info.rc == 0
        return n

    def collect(self, expected: list[SensorReading], timeout_s: float = 0.5) -> list[SensorReading] | None:
        """The readings for this tick as they came back from the broker, or None on timeout."""
        want = {(r.machine_id, r.seq, r.ts) for r in expected}
        got: dict[tuple, SensorReading] = {}
        deadline = time.monotonic() + timeout_s
        while len(got) < len(want):
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                r = self._q.get(timeout=left)
            except queue.Empty:
                return None
            k = (r.machine_id, r.seq, r.ts)
            if k in want:
                got[k] = r
        order = {(r.machine_id, r.seq, r.ts): i for i, r in enumerate(expected)}
        return sorted(got.values(), key=lambda r: order[(r.machine_id, r.seq, r.ts)])

    def close(self) -> None:
        try:
            self.cli.loop_stop()
            self.cli.disconnect()
        except Exception:  # noqa: BLE001
            pass


class IngestPipeline:
    """validation tag + time-series write (+ optional MQTT hop) for every tick's readings."""

    def __init__(self, store: TimeSeriesStore, bridge: MqttBridge | None = None, mqtt_timeout_s: float = 0.5):
        self.store = store
        self.bridge = bridge
        self.mqtt_timeout_s = mqtt_timeout_s
        self.stats = IngestStats(mode="mqtt" if bridge else "direct", broker=bridge.url if bridge else None)
        self._prev: dict[str, SensorReading] = {}

    @classmethod
    def from_env(cls, runtime_dir: Path) -> IngestPipeline:
        mode = os.environ.get("WARROOM_INGEST", "direct").lower()
        tsdb = os.environ.get("WARROOM_TSDB_PATH")
        store = TimeSeriesStore(Path(tsdb) if tsdb else runtime_dir / "timeseries.sqlite")
        bridge = None
        if mode == "mqtt":
            url = os.environ.get("WARROOM_MQTT_URL", "mqtt://127.0.0.1:1883")
            try:
                bridge = MqttBridge(url)
            except Exception as e:  # noqa: BLE001 - no broker: direct path, reported in stats
                p = cls(store)
                p.stats.last_error = f"mqtt unavailable ({url}): {e}"[:300]
                log.warning(p.stats.last_error)
                return p
        return cls(store, bridge)

    def transport(self, readings: list[SensorReading]) -> list[SensorReading]:
        """The MQTT hop. Returns the readings as received from the broker, or the direct ones when
        the broker is down or late (counted)."""
        if self.bridge is None or not readings:
            return readings
        try:
            self.stats.published += self.bridge.publish(readings)
            back = self.bridge.collect(readings, self.mqtt_timeout_s)
        except Exception as e:  # noqa: BLE001
            self.stats.last_error = f"{type(e).__name__}: {e}"[:300]
            back = None
        if back is None:
            self.stats.mqtt_missed_ticks += 1
            return readings
        self.stats.received += len(back)
        return back

    def record(self, readings: list[SensorReading]) -> list[SensorReading]:
        """Tag each reading with its validation result (range / jump) and store it."""
        from warroom.detect.validate import validate_reading

        out = []
        for r in readings:
            try:
                q = validate_reading(r, self._prev.get(r.machine_id))
            except Exception:  # noqa: BLE001
                q = r.quality
            if q is not DataQuality.OK:
                self.stats.bad_quality += 1
            self.stats.by_quality[q.value] = self.stats.by_quality.get(q.value, 0) + 1
            if q is not r.quality:
                r = r.model_copy(update={"quality": q})
            self._prev[r.machine_id] = r
            out.append(r)
        try:
            self.stats.stored += self.store.write(out, via=self.stats.mode if self.bridge else "direct")
        except Exception as e:  # noqa: BLE001 - a full disk must not stop the plant loop
            self.stats.last_error = f"tsdb: {e}"[:300]
        return out

    def reset(self) -> None:
        """New scenario: forget the previous reading per machine (the jump check restarts)."""
        self._prev.clear()

    def close(self) -> None:
        if self.bridge:
            self.bridge.close()
        self.store.close()

