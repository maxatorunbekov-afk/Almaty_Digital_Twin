from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ConsumerReading:
    address: str
    heating_gcal_h: float
    dhw_gcal_h: float
    p1_bar: float
    p2_bar: float

    @property
    def dp_bar(self) -> float:
        return self.p1_bar - self.p2_bar

    @property
    def total_power_kw(self) -> float:
        return (self.heating_gcal_h + self.dhw_gcal_h) * 1163.0

    def as_legacy_fields(self) -> dict[str, float]:
        return {
            "Heating": self.heating_gcal_h,
            "DHW": self.dhw_gcal_h,
            "P1": self.p1_bar,
            "P2": self.p2_bar,
        }


@dataclass(frozen=True)
class Snapshot:
    timestamp: datetime
    consumers: dict[str, ConsumerReading]
    source: str = "opcua"

    @property
    def total_power_kw(self) -> float:
        return sum(item.total_power_kw for item in self.consumers.values())

    @property
    def minimum_dp_bar(self) -> float:
        return min((item.dp_bar for item in self.consumers.values()), default=float("nan"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "total_power_kw": self.total_power_kw,
            "minimum_dp_bar": self.minimum_dp_bar,
            "consumers": {key: asdict(value) for key, value in self.consumers.items()},
        }


@dataclass(frozen=True)
class Anomaly:
    category: str
    severity: str
    code: str
    message: str
    address: str | None = None
    value: float | None = None


@dataclass(frozen=True)
class AnomalyDecision:
    trigger: str
    anomalies: tuple[Anomaly, ...] = ()

    @property
    def has_data_fault(self) -> bool:
        return any(item.category == "data_quality" for item in self.anomalies)

    @property
    def has_process_anomaly(self) -> bool:
        return any(item.category == "process" for item in self.anomalies)


@dataclass(frozen=True)
class Candidate:
    supply_temperature_c: float
    differential_pressure_bar: float


@dataclass
class SimulationResult:
    candidate: Candidate
    converged: bool
    max_deficit_kw: float | None = None
    min_dp_bar: float | None = None
    pump_energy_kwh: float | None = None
    heat_loss_kw: float | None = None
    consumers: dict[str, dict[str, float]] = field(default_factory=dict)
    scenario_uuid: str | None = None
    model_path: str | None = None
    source: str = "unknown"
    complete_metrics: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidate"] = asdict(self.candidate)
        return data


@dataclass
class Recommendation:
    timestamp: datetime
    trigger: str
    candidate: Candidate | None
    objective: float | None
    safe: bool
    complete_fluidit_validation: bool
    reason: str
    simulations: list[SimulationResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "trigger": self.trigger,
            "candidate": asdict(self.candidate) if self.candidate else None,
            "objective": self.objective,
            "safe": self.safe,
            "complete_fluidit_validation": self.complete_fluidit_validation,
            "reason": self.reason,
            "simulations": [item.to_dict() for item in self.simulations],
        }

