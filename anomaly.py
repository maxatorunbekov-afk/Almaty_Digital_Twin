from __future__ import annotations

from collections import deque
import math

from .config import ProjectConfig
from .domain import Anomaly, AnomalyDecision, Snapshot


class HybridAnomalyDetector:
    """Data-quality gate plus process anomaly detector.

    The rules prevent bad sensors from triggering control. When enough history
    exists, an optional Isolation Forest adds a multivariate data-driven score.
    """

    def __init__(self, config: ProjectConfig):
        self.config = config
        self.last_signatures: dict[str, tuple[float, ...]] = {}
        self.frozen_counts: dict[str, int] = {}
        self.history: deque[list[float]] = deque(maxlen=5000)

    def evaluate(self, snapshot: Snapshot) -> AnomalyDecision:
        anomalies: list[Anomaly] = []
        minimum_pressure = float(self.config.get("anomaly.min_pressure_bar", 0.0))
        maximum_pressure = float(self.config.get("anomaly.max_pressure_bar", 16.0))
        maximum_demand = float(self.config.get("anomaly.max_demand_gcal_h", 2.0))
        required_dp = float(self.config.get("anomaly.min_consumer_dp_bar", 3.5))
        relative_warning = float(self.config.get("anomaly.relative_demand_warning", 0.40))
        frozen_limit = int(self.config.get("anomaly.frozen_cycles", 12))
        configured = self.config.consumer_by_address

        vector: list[float] = []
        for address, reading in snapshot.consumers.items():
            values = (
                reading.heating_gcal_h,
                reading.dhw_gcal_h,
                reading.p1_bar,
                reading.p2_bar,
            )
            vector.extend(values)
            for field, value in zip(("Heating", "DHW", "P1", "P2"), values):
                if not math.isfinite(value):
                    anomalies.append(Anomaly("data_quality", "critical", "NON_FINITE", f"{field} is not finite", address, value))
            if not 0.0 <= reading.heating_gcal_h <= maximum_demand:
                anomalies.append(Anomaly("data_quality", "critical", "HEATING_RANGE", "Heating is outside sensor limits", address, reading.heating_gcal_h))
            if not 0.0 <= reading.dhw_gcal_h <= maximum_demand:
                anomalies.append(Anomaly("data_quality", "critical", "DHW_RANGE", "DHW is outside sensor limits", address, reading.dhw_gcal_h))
            if not minimum_pressure <= reading.p1_bar <= maximum_pressure:
                anomalies.append(Anomaly("data_quality", "critical", "P1_RANGE", "P1 is outside sensor limits", address, reading.p1_bar))
            if not minimum_pressure <= reading.p2_bar <= maximum_pressure:
                anomalies.append(Anomaly("data_quality", "critical", "P2_RANGE", "P2 is outside sensor limits", address, reading.p2_bar))
            if reading.p1_bar < reading.p2_bar:
                anomalies.append(Anomaly("data_quality", "critical", "PRESSURE_ORDER", "P1 is lower than P2", address, reading.dp_bar))

            signature = tuple(round(item, 7) for item in values)
            if self.last_signatures.get(address) == signature:
                self.frozen_counts[address] = self.frozen_counts.get(address, 0) + 1
            else:
                self.frozen_counts[address] = 0
            self.last_signatures[address] = signature
            if self.frozen_counts[address] >= frozen_limit:
                anomalies.append(Anomaly("data_quality", "warning", "FROZEN", "All four values are unchanged", address))

            if reading.dp_bar < required_dp:
                anomalies.append(Anomaly("process", "warning", "LOW_DP", "Consumer differential pressure is low", address, reading.dp_bar))
            expected = configured.get(address)
            if expected:
                for field, measured, reference in (
                    ("Heating", reading.heating_gcal_h, expected.expected_heating_gcal_h),
                    ("DHW", reading.dhw_gcal_h, expected.expected_dhw_gcal_h),
                ):
                    if reference > 0 and abs(measured - reference) / reference > relative_warning:
                        anomalies.append(Anomaly("process", "warning", "DEMAND_DEVIATION", f"{field} differs from research baseline", address, measured))

        if vector and all(math.isfinite(item) for item in vector):
            ml_score = self._isolation_forest_score(vector)
            if ml_score is not None and ml_score < -0.15:
                anomalies.append(Anomaly("process", "warning", "ML_OUTLIER", "Isolation Forest detected a multivariate outlier", value=ml_score))
            self.history.append(vector)

        if any(item.category == "data_quality" for item in anomalies):
            trigger = "data_fault"
        elif any(item.category == "process" for item in anomalies):
            trigger = "process_anomaly"
        else:
            trigger = "normal"
        return AnomalyDecision(trigger=trigger, anomalies=tuple(anomalies))

    def prime(self, snapshots: list[Snapshot]) -> int:
        """Load historical SQL samples without generating live alarms."""
        count = 0
        order = [item.address for item in self.config.consumers]
        for snapshot in snapshots:
            if any(address not in snapshot.consumers for address in order):
                continue
            vector = []
            for address in order:
                item = snapshot.consumers[address]
                vector.extend((item.heating_gcal_h, item.dhw_gcal_h, item.p1_bar, item.p2_bar))
            if all(math.isfinite(value) for value in vector):
                self.history.append(vector)
                count += 1
        return count

    def _isolation_forest_score(self, vector: list[float]) -> float | None:
        if len(self.history) < 50:
            return None
        try:
            from sklearn.ensemble import IsolationForest
        except ImportError:
            return None
        model = IsolationForest(n_estimators=100, contamination="auto", random_state=42)
        model.fit(list(self.history))
        return float(model.decision_function([vector])[0])
