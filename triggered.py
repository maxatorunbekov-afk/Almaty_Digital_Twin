from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import time

from .anomaly import HybridAnomalyDetector
from .config import ProjectConfig
from .domain import Candidate, SimulationResult, Snapshot
from .fluidit.model import FHeatModel, ScenarioSpec
from .fluidit.runner import FluiditRunner, build_runner
from .opcua_io import SiemensOpcUaClient


LOGGER = logging.getLogger(__name__)

STATE_IDLE = 0
STATE_READING_PLC = 10
STATE_PREPARING_MODEL = 20
STATE_SIMULATING = 30
STATE_WRITING_PLC = 40
STATE_COMPLETED = 100
STATE_ERROR = 200

ERROR_NONE = 0
ERROR_BAD_INPUT = 201
ERROR_FLUIDIT = 202
ERROR_PLC_WRITE = 203
ERROR_INTERNAL = 299


class TriggeredSimulationEngine:
    """Run one real Fluidit scenario from one live PLC snapshot."""

    def __init__(self, config: ProjectConfig, runner: FluiditRunner | None = None):
        self.config = config
        self.runner = runner or build_runner(config)

    def run(self, snapshot: Snapshot) -> tuple[SimulationResult, Path]:
        detector = HybridAnomalyDetector(self.config)
        decision = detector.evaluate(snapshot)
        if decision.has_data_fault:
            messages = "; ".join(item.message for item in decision.anomalies if item.category == "data_quality")
            raise ValueError("PLC input data failed validation: " + messages)

        candidate = Candidate(
            float(self.config.get("scada_trigger.supply_temperature_c", 95.0)),
            float(self.config.get("scada_trigger.differential_pressure_bar", 4.0)),
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        output_dir = self.config.resolve("project.work_dir") / "scada_runs" / timestamp
        output_dir.mkdir(parents=True, exist_ok=False)
        parent_uuid = str(self.config.get("fluidit.parent_scenario_uuid"))
        spec = ScenarioSpec(
            name=f"SCADA Request {timestamp}",
            parent_uuid=parent_uuid,
            snapshot=snapshot,
            load_mode="combined",
            supply_temperature_c=candidate.supply_temperature_c,
            differential_pressure_bar=candidate.differential_pressure_bar,
            ambient_temperature_c=float(self.config.get("environment.ambient_temperature_c", -20.1)),
            ground_temperature_c=float(self.config.get("environment.ground_temperature_c", 10.0)),
            return_temperature_c=float(self.config.get("environment.return_temperature_c", 47.0)),
            description="Generated from live OPCUA_Output by a SCADA model request.",
        )
        candidate_model = FHeatModel(self.config.resolve("project.model"), self.config).derive(
            output_dir / "candidate.fheat", [spec], active_uuid=spec.scenario_uuid
        )
        result = self.runner.run(candidate_model, spec.scenario_uuid, candidate, snapshot, output_dir)
        validate_real_fluidit_result(self.config, result)
        return result, output_dir


def validate_real_fluidit_result(config: ProjectConfig, result: SimulationResult) -> None:
    if result.source == "mock_not_fluidit":
        raise ValueError("Mock results are never allowed for SCADA-triggered PLC writeback")
    if not result.converged or not result.complete_metrics:
        raise ValueError("Fluidit result did not converge or is incomplete")
    metrics = {
        "min_dp_bar": result.min_dp_bar,
        "max_deficit_kw": result.max_deficit_kw,
        "heat_loss_kw": result.heat_loss_kw,
    }
    if any(value is None or not math.isfinite(float(value)) for value in metrics.values()):
        raise ValueError("Fluidit returned missing or non-finite engineering metrics")
    required_dp = float(config.get("optimization.required_min_dp_bar", 3.5))
    allowed_deficit = float(config.get("optimization.maximum_deficit_kw", 0.1))
    if float(result.min_dp_bar) + 1e-6 < required_dp:
        raise ValueError(f"Fluidit minimum DP {result.min_dp_bar} bar is below {required_dp} bar")
    if float(result.max_deficit_kw) > allowed_deficit + 1e-6:
        raise ValueError(f"Fluidit heat deficit {result.max_deficit_kw} kW exceeds {allowed_deficit} kW")
    if float(result.heat_loss_kw) < 0.0:
        raise ValueError("Fluidit heat loss cannot be negative")
    expected = {consumer.address for consumer in config.consumers}
    if set(result.consumers) != expected:
        raise ValueError("Fluidit consumer set does not match config.toml")
    for address, values in result.consumers.items():
        for field in ("Heating", "DHW", "P1", "P2"):
            value = values.get(field)
            if value is None or not math.isfinite(float(value)):
                raise ValueError(f"Invalid Fluidit result: {address}.{field}")
        if not (0.0 <= float(values["P2"]) < float(values["P1"]) <= 16.0):
            raise ValueError(f"Invalid pressure result for {address}")
        max_demand = float(config.get("anomaly.max_demand_gcal_h", 2.0))
        if not (0.0 <= float(values["Heating"]) <= max_demand):
            raise ValueError(f"Invalid heating result for {address}")
        if not (0.0 <= float(values["DHW"]) <= max_demand):
            raise ValueError(f"Invalid DHW result for {address}")


async def execute_model_now(config: ProjectConfig, engine: TriggeredSimulationEngine | None = None) -> dict:
    engine = engine or TriggeredSimulationEngine(config)
    started = time.monotonic()
    async with SiemensOpcUaClient(config) as client:
        snapshot = await client.read_snapshot()
    result, output_dir = await asyncio.to_thread(engine.run, snapshot)
    async with SiemensOpcUaClient(config) as client:
        readback = await client.write_legacy_results(result.consumers)
    report = _cycle_report(result, snapshot, output_dir, time.monotonic() - started, readback)
    _save_cycle_report(output_dir, report)
    return report


async def serve_scada_requests(config: ProjectConfig) -> None:
    engine = TriggeredSimulationEngine(config)
    poll_seconds = max(0.5, float(config.get("scada_trigger.poll_seconds", 1.0)))
    LOGGER.info("SCADA model-request service started; polling every %.1f s", poll_seconds)
    while True:
        try:
            async with SiemensOpcUaClient(config) as client:
                requested = await client.read_scada_model_request()
            if not requested:
                await asyncio.sleep(poll_seconds)
                continue
            await _handle_scada_request(config, engine)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("SCADA trigger polling failed")
            await asyncio.sleep(max(2.0, poll_seconds))


async def _handle_scada_request(config: ProjectConfig, engine: TriggeredSimulationEngine) -> None:
    started = time.monotonic()
    output_dir: Path | None = None
    phase = "reading"
    try:
        async with SiemensOpcUaClient(config) as client:
            await client.write_scada_trigger_state(
                request=False, busy=True, done=False, error=False,
                state=STATE_READING_PLC, error_code=ERROR_NONE,
            )
            snapshot = await client.read_snapshot()

        async with SiemensOpcUaClient(config) as client:
            await client.write_scada_trigger_state(state=STATE_PREPARING_MODEL)
        LOGGER.info("SCADA request accepted; live PLC snapshot contains %.3f kW", snapshot.total_power_kw)

        async with SiemensOpcUaClient(config) as client:
            await client.write_scada_trigger_state(state=STATE_SIMULATING)
        phase = "fluidit"
        result, output_dir = await asyncio.to_thread(engine.run, snapshot)

        phase = "writing"
        async with SiemensOpcUaClient(config) as client:
            await client.write_scada_trigger_state(state=STATE_WRITING_PLC)
            readback = await client.write_legacy_results(result.consumers)
            duration = time.monotonic() - started
            await client.write_scada_trigger_state(
                busy=False, done=True, error=False, state=STATE_COMPLETED,
                error_code=ERROR_NONE,
                last_run_unix_s=int(datetime.now(timezone.utc).timestamp()),
                last_duration_s=duration,
                last_min_dp_bar=float(result.min_dp_bar),
                last_max_deficit_kw=float(result.max_deficit_kw),
                last_heat_loss_kw=float(result.heat_loss_kw),
            )
        report = _cycle_report(result, snapshot, output_dir, duration, readback)
        _save_cycle_report(output_dir, report)
        LOGGER.info("SCADA model request completed and %d fields verified", len(readback) * 4)
    except ValueError as exc:
        code = ERROR_BAD_INPUT if phase == "reading" else ERROR_FLUIDIT
        await _set_scada_error(config, code, started)
        LOGGER.exception("SCADA model request rejected: %s", exc)
    except Exception as exc:
        if phase == "fluidit":
            code = ERROR_FLUIDIT
        elif phase == "writing":
            code = ERROR_PLC_WRITE
        else:
            code = ERROR_INTERNAL
        await _set_scada_error(config, code, started)
        LOGGER.exception("SCADA model request failed: %s", exc)


async def _set_scada_error(config: ProjectConfig, error_code: int, started: float) -> None:
    try:
        async with SiemensOpcUaClient(config) as client:
            await client.write_scada_trigger_state(
                busy=False, done=False, error=True, state=STATE_ERROR,
                error_code=error_code,
                last_run_unix_s=int(datetime.now(timezone.utc).timestamp()),
                last_duration_s=time.monotonic() - started,
            )
    except Exception:
        LOGGER.exception("Could not publish SCADA error state to DT_Model_Control")


def _cycle_report(
    result: SimulationResult,
    snapshot: Snapshot,
    output_dir: Path,
    duration_s: float,
    readback: dict[str, dict[str, float]],
) -> dict:
    return {
        "status": "real_fluidit_result_written_and_verified",
        "input_source": snapshot.source,
        "input_timestamp": snapshot.timestamp.isoformat(),
        "input_total_power_kw": snapshot.total_power_kw,
        "candidate": {
            "supply_temperature_c": result.candidate.supply_temperature_c,
            "differential_pressure_bar": result.candidate.differential_pressure_bar,
        },
        "fluidit_source": result.source,
        "scenario_uuid": result.scenario_uuid,
        "min_dp_bar": result.min_dp_bar,
        "max_deficit_kw": result.max_deficit_kw,
        "heat_loss_kw": result.heat_loss_kw,
        "duration_s": duration_s,
        "output_dir": str(output_dir),
        "plc_field_count": len(readback) * 4,
        "readback": readback,
    }


def _save_cycle_report(output_dir: Path, report: dict) -> None:
    path = output_dir / "cycle_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
