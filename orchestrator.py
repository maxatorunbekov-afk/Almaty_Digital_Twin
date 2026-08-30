from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging

from .anomaly import HybridAnomalyDetector
from .config import ProjectConfig
from .domain import Recommendation, Snapshot, utc_now
from .fluidit.runner import build_runner
from .historian import LocalHistorian, SqlServerArchiveReader
from .opcua_io import SiemensOpcUaClient
from .optimizer import ScenarioOptimizer
from .static_data import research_snapshot


LOGGER = logging.getLogger(__name__)


class DigitalTwinOrchestrator:
    def __init__(self, config: ProjectConfig, offline: bool = False):
        self.config = config
        self.offline = offline
        self.detector = HybridAnomalyDetector(config)
        self.historian = LocalHistorian(config.resolve("project.database"))
        self.optimizer = ScenarioOptimizer(config, build_runner(config))
        self._sql_history_loaded = False

    async def run_once(self) -> Recommendation | None:
        await asyncio.to_thread(self._load_sql_history_once)
        if self.offline:
            snapshot = research_snapshot("offline_research_table")
            return await self._process(snapshot, opcua=None)
        async with SiemensOpcUaClient(self.config) as opcua:
            snapshot = await opcua.read_snapshot()
            return await self._process(snapshot, opcua)

    def _load_sql_history_once(self) -> None:
        if self._sql_history_loaded or not bool(self.config.get("sql.enabled", False)):
            return
        reader = SqlServerArchiveReader(
            str(self.config.get("sql.connection_string_env")),
            str(self.config.get("sql.history_query")),
        )
        since = datetime.now(timezone.utc) - timedelta(hours=float(self.config.get("sql.history_hours", 168)))
        rows = reader.read_since(since)
        snapshots = reader.to_snapshots(rows, [item.address for item in self.config.consumers])
        loaded = self.detector.prime(snapshots)
        self._sql_history_loaded = True
        LOGGER.info("Loaded %d complete historical snapshots from SQL", loaded)

    async def _process(self, snapshot: Snapshot, opcua: SiemensOpcUaClient | None):
        self.historian.store_snapshot(snapshot)
        decision = self.detector.evaluate(snapshot)
        self.historian.store_decision(decision)
        LOGGER.info("Cycle trigger=%s anomalies=%d", decision.trigger, len(decision.anomalies))
        if decision.has_data_fault:
            LOGGER.error("Simulation blocked by data-quality fault")
            return None
        trigger = decision.trigger
        if trigger == "normal" and not self._periodic_due():
            return None
        if trigger == "normal":
            trigger = "periodic"
        recommendation = self.optimizer.optimize(snapshot, trigger)
        self.historian.store_recommendation(recommendation)
        self._write_recommendation_file(recommendation)
        if opcua is not None:
            await self._optional_writeback(opcua, recommendation)
        return recommendation

    def _periodic_due(self) -> bool:
        last = self.historian.last_simulation_at()
        if last is None:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= float(self.config.get("project.periodic_simulation_seconds", 900))

    def _write_recommendation_file(self, recommendation: Recommendation):
        path = self.config.resolve("project.recommendation_file")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(recommendation.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    async def _optional_writeback(self, opcua: SiemensOpcUaClient, recommendation: Recommendation):
        if not bool(self.config.get("control.enable_writeback", False)):
            LOGGER.info("PLC writeback disabled; recommendation saved only")
            return
        if not recommendation.safe or recommendation.candidate is None:
            LOGGER.warning("PLC writeback blocked: recommendation is not fully validated")
            return
        mode = str(self.config.get("control.writeback_mode", "supervisory"))
        if mode == "supervisory":
            request_id = await opcua.write_supervisory_setpoint(recommendation.candidate)
            LOGGER.warning("Validated supervisory setpoint written, request_id=%s", request_id)
        elif mode == "legacy_results":
            selected = next(
                item for item in recommendation.simulations if item.candidate == recommendation.candidate
            )
            await opcua.write_legacy_results(selected.consumers)
            LOGGER.warning("Model result fields written to legacy OPCUA_Input DB")
        else:
            raise ValueError(f"Unknown writeback mode: {mode}")

    async def serve(self):
        cycle = max(1.0, float(self.config.get("project.cycle_seconds", 10)))
        while True:
            started = utc_now()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Digital-twin cycle failed")
            elapsed = (utc_now() - started).total_seconds()
            await asyncio.sleep(max(0.1, cycle - elapsed))
