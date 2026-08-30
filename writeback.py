from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .config import ProjectConfig
from .opcua_io import SiemensOpcUaClient


CONFIRMATION_TOKEN = "WRITE_OPCUA_INPUT"
FIELDS = ("Heating", "DHW", "P1", "P2")


def validate_result_for_legacy_write(config: ProjectConfig, result_path: Path) -> tuple[dict, dict[str, dict[str, float]]]:
    raw = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if raw.get("converged") is not True:
        raise ValueError("Fluidit result is not converged")

    min_dp = _finite(raw.get("min_dp_bar"), "min_dp_bar")
    max_deficit = _finite(raw.get("max_deficit_kw"), "max_deficit_kw")
    heat_loss = _finite(raw.get("heat_loss_kw"), "heat_loss_kw")
    required_dp = float(config.get("optimization.required_min_dp_bar", 3.5))
    allowed_deficit = float(config.get("optimization.maximum_deficit_kw", 0.1))
    if min_dp + 1e-6 < required_dp:
        raise ValueError(f"Minimum differential pressure {min_dp} bar is below {required_dp} bar")
    if max_deficit > allowed_deficit + 1e-6:
        raise ValueError(f"Maximum heat deficit {max_deficit} kW exceeds {allowed_deficit} kW")
    if heat_loss < 0.0:
        raise ValueError("Heat loss cannot be negative")

    source = raw.get("consumers")
    if not isinstance(source, dict):
        raise ValueError("Fluidit result has no consumers object")
    expected = [consumer.address for consumer in config.consumers]
    if set(source) != set(expected):
        raise ValueError("Consumer address set does not match config.toml")

    max_pressure = float(config.get("anomaly.max_pressure_bar", 16.0))
    max_demand = float(config.get("anomaly.max_demand_gcal_h", 2.0))
    consumers: dict[str, dict[str, float]] = {}
    for address in expected:
        values = source[address]
        if not isinstance(values, dict):
            raise ValueError(f"Consumer result is not an object: {address}")
        consumers[address] = {field: _finite(values.get(field), f"{address}.{field}") for field in FIELDS}
        if not 0.0 <= consumers[address]["Heating"] <= max_demand:
            raise ValueError(f"Heating result is outside limits: {address}")
        if not 0.0 <= consumers[address]["DHW"] <= max_demand:
            raise ValueError(f"DHW result is outside limits: {address}")
        p1 = consumers[address]["P1"]
        p2 = consumers[address]["P2"]
        if not 0.0 <= p2 < p1 <= max_pressure:
            raise ValueError(f"Pressure result is outside limits or P1 <= P2: {address}")
    return raw, consumers


async def write_result_once(config: ProjectConfig, result_path: Path, confirmation: str) -> dict:
    if confirmation != CONFIRMATION_TOKEN:
        raise PermissionError(f"Explicit confirmation required: {CONFIRMATION_TOKEN}")
    raw, consumers = validate_result_for_legacy_write(config, result_path)
    backup_dir = config.resolve("project.work_dir") / "plc_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"opcua_input_before_{timestamp}.json"

    async with SiemensOpcUaClient(config) as client:
        before = await client.read_legacy_results()
        backup_path.write_text(
            json.dumps({"created_at": timestamp, "values": before}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        after = await client.write_legacy_results(consumers)

    return {
        "status": "written_and_verified",
        "target_db": str(config.get("opcua.legacy_write_db", "OPCUA_Input")),
        "consumer_count": len(after),
        "field_count": len(after) * len(FIELDS),
        "fluidit_metrics": {
            "min_dp_bar": raw["min_dp_bar"],
            "max_deficit_kw": raw["max_deficit_kw"],
            "heat_loss_kw": raw["heat_loss_kw"],
        },
        "backup": str(backup_path),
        "readback": after,
    }


def _finite(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Missing or invalid numeric result: {label}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Non-finite numeric result: {label}")
    return result
