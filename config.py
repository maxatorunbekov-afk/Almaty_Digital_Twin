from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any


@dataclass(frozen=True)
class ConsumerConfig:
    address: str
    component_ids: tuple[str, ...]
    weights: tuple[float, ...]
    expected_heating_gcal_h: float
    expected_dhw_gcal_h: float


class ProjectConfig:
    def __init__(self, path: Path, raw: dict[str, Any]):
        self.path = path.resolve()
        self.raw = raw
        self.root = self.path.parent.parent.resolve()
        self.consumers = tuple(
            ConsumerConfig(
                address=item["address"],
                component_ids=tuple(item["component_ids"]),
                weights=tuple(float(x) for x in item["weights"]),
                expected_heating_gcal_h=float(item["expected_heating_gcal_h"]),
                expected_dhw_gcal_h=float(item["expected_dhw_gcal_h"]),
            )
            for item in raw["consumers"]
        )
        self.validate()

    def validate(self) -> None:
        addresses = [item.address for item in self.consumers]
        if len(addresses) != len(set(addresses)):
            raise ValueError("Consumer addresses must be unique")
        for item in self.consumers:
            if len(item.component_ids) != len(item.weights):
                raise ValueError(f"Component/weight mismatch for {item.address}")
            if not item.component_ids or abs(sum(item.weights) - 1.0) > 1e-6:
                raise ValueError(f"Weights must sum to 1 for {item.address}")
        if float(self.get("control.min_supply_temperature_c")) >= float(
            self.get("control.max_supply_temperature_c")
        ):
            raise ValueError("Invalid supply temperature limits")

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def resolve(self, dotted: str) -> Path:
        value = Path(str(self.get(dotted)))
        return value if value.is_absolute() else (self.root / value).resolve()

    @property
    def consumer_by_address(self) -> dict[str, ConsumerConfig]:
        return {item.address: item for item in self.consumers}


def load_config(path: str | Path) -> ProjectConfig:
    config_path = Path(path)
    with config_path.open("rb") as stream:
        return ProjectConfig(config_path, tomllib.load(stream))

