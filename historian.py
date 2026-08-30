from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3

from .domain import AnomalyDecision, ConsumerReading, Recommendation, Snapshot


class LocalHistorian:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    measured_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    address TEXT NOT NULL,
                    heating REAL NOT NULL,
                    dhw REAL NOT NULL,
                    p1 REAL NOT NULL,
                    p2 REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_samples_time ON samples(measured_at);
                CREATE TABLE IF NOT EXISTS events (
                    created_at TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    created_at TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    safe INTEGER NOT NULL,
                    supply_temperature REAL,
                    differential_pressure REAL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def store_snapshot(self, snapshot: Snapshot):
        rows = [
            (
                snapshot.timestamp.isoformat(), snapshot.source, item.address,
                item.heating_gcal_h, item.dhw_gcal_h, item.p1_bar, item.p2_bar,
            )
            for item in snapshot.consumers.values()
        ]
        with self._connect() as db:
            db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def store_decision(self, decision: AnomalyDecision):
        payload = json.dumps([item.__dict__ for item in decision.anomalies], ensure_ascii=False)
        with self._connect() as db:
            db.execute(
                "INSERT INTO events VALUES (datetime('now'), ?, ?)",
                (decision.trigger, payload),
            )

    def store_recommendation(self, recommendation: Recommendation):
        candidate = recommendation.candidate
        with self._connect() as db:
            db.execute(
                "INSERT INTO recommendations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    recommendation.timestamp.isoformat(),
                    recommendation.trigger,
                    int(recommendation.safe),
                    candidate.supply_temperature_c if candidate else None,
                    candidate.differential_pressure_bar if candidate else None,
                    json.dumps(recommendation.to_dict(), ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO state(key, value) VALUES ('last_simulation_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (recommendation.timestamp.isoformat(),),
            )

    def last_simulation_at(self) -> datetime | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM state WHERE key='last_simulation_at'").fetchone()
        return datetime.fromisoformat(row[0]) if row else None


class SqlServerArchiveReader:
    """Optional read-only adapter for a normalized WinCC SQL view."""

    def __init__(self, connection_string_env: str, query: str):
        self.connection_string_env = connection_string_env
        self.query = query

    def read_since(self, since: datetime) -> list[tuple]:
        try:
            import pyodbc
        except ImportError as exc:
            raise RuntimeError("Install the optional SQL dependencies: pip install -e .[sql]") from exc
        connection_string = os.environ.get(self.connection_string_env, "")
        if not connection_string:
            raise RuntimeError(f"Environment variable {self.connection_string_env} is empty")
        with pyodbc.connect(connection_string, timeout=5) as db:
            cursor = db.cursor()
            cursor.execute(self.query, since)
            return list(cursor.fetchall())

    @staticmethod
    def to_snapshots(rows: list[tuple], required_addresses: list[str]) -> list[Snapshot]:
        grouped: dict[datetime, dict[str, ConsumerReading]] = {}
        for row in rows:
            measured_at, address, heating, dhw, p1, p2 = row[:6]
            address = str(address)
            grouped.setdefault(measured_at, {})[address] = ConsumerReading(
                address, float(heating), float(dhw), float(p1), float(p2)
            )
        snapshots = []
        for measured_at in sorted(grouped):
            readings = grouped[measured_at]
            if all(address in readings for address in required_addresses):
                snapshots.append(Snapshot(measured_at, readings, source="wincc_sql"))
        return snapshots
