from __future__ import annotations

from pathlib import Path

from .config import ProjectConfig
from .fluidit.model import FHeatModel, ScenarioSpec
from .static_data import research_snapshot


BASE_SCENARIO_UUID = "841d06cb-bc21-43f4-8dd1-41dbb427f149"
DATA_PARENT_UUID = "d3c6d76e-868c-5cf0-978b-025a19b8c925"


def build_research_model(config: ProjectConfig, source: Path, output: Path) -> Path:
    snapshot = research_snapshot()
    common = {
        "ambient_temperature_c": -20.1,
        "ground_temperature_c": 10.0,
        "return_temperature_c": 47.0,
    }
    specs = [
        ScenarioSpec(
            name="00 Network Reference Conditions",
            parent_uuid=BASE_SCENARIO_UUID,
            supply_temperature_c=95.0,
            differential_pressure_bar=4.0,
            description="Outdoor air -20.1 C, ground +10 C, supply +95 C, return +47 C.",
            scenario_uuid=DATA_PARENT_UUID,
            **common,
        ),
        ScenarioSpec(
            name="01 Space Heating",
            parent_uuid=DATA_PARENT_UUID,
            snapshot=snapshot,
            load_mode="heating",
            description="Space-heating load only: 1867.449 kW.",
            scenario_uuid="1df9699e-3e32-5d4f-a564-535586e1b84e",
            **common,
        ),
        ScenarioSpec(
            name="02 Domestic Hot Water",
            parent_uuid=DATA_PARENT_UUID,
            snapshot=snapshot,
            load_mode="dhw",
            description="Domestic-hot-water load only: 502.329 kW.",
            scenario_uuid="ea826b38-9e88-51e6-819f-7347705a4a06",
            **common,
        ),
        ScenarioSpec(
            name="03 Heating and DHW",
            parent_uuid=DATA_PARENT_UUID,
            snapshot=snapshot,
            load_mode="combined",
            description="Combined design load: 2369.778 kW.",
            scenario_uuid="e3afdb98-292b-5d72-99d4-01af420f0543",
            **common,
        ),
        ScenarioSpec(
            name="04 Low Supply Temperature 90 C",
            parent_uuid=DATA_PARENT_UUID,
            snapshot=snapshot,
            load_mode="combined",
            supply_temperature_c=90.0,
            differential_pressure_bar=4.0,
            description="What-if case: lower supply temperature at unchanged demand.",
            scenario_uuid="13f400cb-b002-5b80-b604-3ab0f4d4c132",
            **common,
        ),
        ScenarioSpec(
            name="05 Low Differential Pressure 3 bar",
            parent_uuid=DATA_PARENT_UUID,
            snapshot=snapshot,
            load_mode="combined",
            supply_temperature_c=95.0,
            differential_pressure_bar=3.0,
            description="Diagnostic case with reduced available differential pressure.",
            scenario_uuid="7a237e34-38dd-5b4b-b6e6-6fd74b0f6dd6",
            **common,
        ),
        ScenarioSpec(
            name="06 High Demand 120 percent",
            parent_uuid=DATA_PARENT_UUID,
            snapshot=snapshot,
            load_mode="combined",
            load_factor=1.2,
            supply_temperature_c=95.0,
            differential_pressure_bar=4.0,
            description="Diagnostic case with a 20 percent demand increase.",
            scenario_uuid="612a79ae-270e-5652-9d68-f4ece7a9bb27",
            **common,
        ),
    ]
    return FHeatModel(source, config).derive(output, specs, active_uuid=specs[3].scenario_uuid)
