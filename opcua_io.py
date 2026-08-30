from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
import math
import os
from typing import Any

from .config import ProjectConfig
from .domain import Candidate, ConsumerReading, Snapshot


class SiemensOpcUaClient(AbstractAsyncContextManager):
    def __init__(self, config: ProjectConfig):
        self.config = config
        self.client: Any = None
        self.ua: Any = None

    async def __aenter__(self):
        try:
            from asyncua import Client, ua
        except ImportError as exc:
            raise RuntimeError("Install dependencies with scripts/install_windows.ps1") from exc
        self.ua = ua
        timeout = float(self.config.get("opcua.timeout_seconds", 5))
        self.client = Client(url=str(self.config.get("opcua.endpoint")), timeout=timeout)
        username = str(self.config.get("opcua.username", ""))
        password_env = str(self.config.get("opcua.password_env", ""))
        password = os.environ.get(password_env, "") if password_env else ""
        if username:
            self.client.set_user(username)
            self.client.set_password(password)
        security = str(self.config.get("opcua.security_string", ""))
        if security:
            await self.client.set_security_string(security)
        await self.client.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.client is not None:
            await self.client.disconnect()
        self.client = None

    def consumer_node_id(self, db_name: str, address: str, field: str) -> str:
        ns = int(self.config.get("opcua.namespace_index", 3))
        return f'ns={ns};s="{db_name}"."{address}"."{field}"'

    async def read_snapshot(self) -> Snapshot:
        db_name = str(self.config.get("opcua.read_db", "OPCUA_Output"))
        readings: dict[str, ConsumerReading] = {}
        for consumer in self.config.consumers:
            values: dict[str, float] = {}
            for field in ("Heating", "DHW", "P1", "P2"):
                node = self.client.get_node(self.consumer_node_id(db_name, consumer.address, field))
                values[field] = float(await node.read_value())
            readings[consumer.address] = ConsumerReading(
                consumer.address,
                values["Heating"], values["DHW"], values["P1"], values["P2"],
            )
        return Snapshot(datetime.now(timezone.utc), readings, source="siemens_opcua")

    async def probe(self) -> list[dict[str, Any]]:
        db_name = str(self.config.get("opcua.read_db", "OPCUA_Output"))
        rows = []
        for consumer in self.config.consumers:
            for field in ("Heating", "DHW", "P1", "P2"):
                node_id = self.consumer_node_id(db_name, consumer.address, field)
                try:
                    value = await self.client.get_node(node_id).read_value()
                    rows.append({"node_id": node_id, "ok": True, "value": value})
                except Exception as exc:  # diagnostic command must report every tag
                    rows.append({"node_id": node_id, "ok": False, "error": str(exc)})
        return rows

    async def read_operator_approval(self) -> bool:
        node_id = str(self.config.get("control.nodes.operator_approved"))
        return bool(await self.client.get_node(node_id).read_value())

    async def read_scada_model_request(self) -> bool:
        node_id = self._scada_trigger_node_id("request")
        return bool(await self.client.get_node(node_id).read_value())

    async def write_scada_trigger_state(
        self,
        *,
        request: bool | None = None,
        busy: bool | None = None,
        done: bool | None = None,
        error: bool | None = None,
        state: int | None = None,
        error_code: int | None = None,
        last_run_unix_s: int | None = None,
        last_duration_s: float | None = None,
        last_min_dp_bar: float | None = None,
        last_max_deficit_kw: float | None = None,
        last_heat_loss_kw: float | None = None,
    ) -> None:
        values = (
            ("request", request, self.ua.VariantType.Boolean),
            ("busy", busy, self.ua.VariantType.Boolean),
            ("done", done, self.ua.VariantType.Boolean),
            ("error", error, self.ua.VariantType.Boolean),
            ("state", state, self.ua.VariantType.UInt16),
            ("error_code", error_code, self.ua.VariantType.UInt16),
            ("last_run_unix_s", last_run_unix_s, self.ua.VariantType.Int64),
            ("last_duration_s", last_duration_s, self.ua.VariantType.Float),
            ("last_min_dp_bar", last_min_dp_bar, self.ua.VariantType.Float),
            ("last_max_deficit_kw", last_max_deficit_kw, self.ua.VariantType.Float),
            ("last_heat_loss_kw", last_heat_loss_kw, self.ua.VariantType.Float),
        )
        for key, value, variant_type in values:
            if value is None:
                continue
            node = self.client.get_node(self._scada_trigger_node_id(key))
            await self._write_value_only(node, value, variant_type)

    async def write_supervisory_setpoint(self, candidate: Candidate) -> int:
        nodes = self.config.get("control.nodes", {})
        if not bool(await self.client.get_node(nodes["enable"]).read_value()):
            raise PermissionError("DT_Command.Enable is FALSE in the PLC")
        if bool(self.config.get("control.require_operator_approval", True)):
            if not bool(await self.client.get_node(nodes["operator_approved"]).read_value()):
                raise PermissionError("DT_Command.OperatorApproved is FALSE")
        request_node = self.client.get_node(nodes["request_id"])
        current_request = int(await request_node.read_value())
        request_id = (current_request + 1) & 0xFFFFFFFF
        await self.client.get_node(nodes["valid"]).write_value(False, self.ua.VariantType.Boolean)
        await self.client.get_node(nodes["supply_temperature_c"]).write_value(
            float(candidate.supply_temperature_c), self.ua.VariantType.Float
        )
        await self.client.get_node(nodes["differential_pressure_bar"]).write_value(
            float(candidate.differential_pressure_bar), self.ua.VariantType.Float
        )
        unix_seconds = int(datetime.now(timezone.utc).timestamp())
        await self.client.get_node(nodes["unix_time_s"]).write_value(unix_seconds, self.ua.VariantType.Int64)
        await request_node.write_value(request_id, self.ua.VariantType.UInt32)
        await self.client.get_node(nodes["valid"]).write_value(True, self.ua.VariantType.Boolean)
        return request_id

    async def write_legacy_results(self, consumers: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        """Compatibility write to the user's existing OPCUA_Input DB.

        These values are model outputs, not actuator commands.
        """
        db_name = str(self.config.get("opcua.legacy_write_db", "OPCUA_Input"))
        fields = ("Heating", "DHW", "P1", "P2")
        missing = [
            f"{consumer.address}.{field}"
            for consumer in self.config.consumers
            for field in fields
            if consumer.address not in consumers or field not in consumers[consumer.address]
        ]
        if missing:
            raise ValueError("Incomplete legacy result set; no OPC UA values were written: " + ", ".join(missing))
        prepared = {
            consumer.address: {
                field: float(consumers[consumer.address][field])
                for field in fields
            }
            for consumer in self.config.consumers
        }
        non_finite = [
            f"{address}.{field}"
            for address, values in prepared.items()
            for field, value in values.items()
            if not math.isfinite(value)
        ]
        if non_finite:
            raise ValueError("Non-finite legacy results; no OPC UA values were written: " + ", ".join(non_finite))

        before = await self.read_legacy_results()
        # Siemens S7 OPC UA accepts a client write to the Value attribute, but
        # rejects attempts to write the SourceTimestamp/StatusCode carried by
        # asyncua's write_value convenience method.  Prove value-only writing
        # with an unchanged value before sending any Fluidit result.
        first_consumer = self.config.consumers[0]
        first_address = first_consumer.address
        first_field = fields[0]
        probe_node = self.client.get_node(
            self.consumer_node_id(db_name, first_address, first_field)
        )
        try:
            await self._write_float_value_only(probe_node, before[first_address][first_field])
            probe_readback = float(await probe_node.read_value())
            if not math.isclose(
                probe_readback,
                before[first_address][first_field],
                rel_tol=1e-5,
                abs_tol=1e-5,
            ):
                raise IOError(
                    "OPC UA value-only preflight readback failed: "
                    f"expected={before[first_address][first_field]!r}, "
                    f"read={probe_readback!r}"
                )
        except BaseException as probe_error:
            raise RuntimeError(
                "OPCUA_Input value-only preflight failed; no Fluidit results were written: "
                f"{probe_error}"
            ) from probe_error

        try:
            await self._write_legacy_values(db_name, prepared)
            after = await self.read_legacy_results()
            mismatches = [
                f"{address}.{field}: expected={prepared[address][field]!r}, read={after[address][field]!r}"
                for address in prepared
                for field in fields
                if not math.isclose(prepared[address][field], after[address][field], rel_tol=1e-5, abs_tol=1e-5)
            ]
            if mismatches:
                raise IOError("OPC UA readback verification failed: " + "; ".join(mismatches))
            return after
        except BaseException as write_error:
            try:
                await self._write_legacy_values(db_name, before)
            except BaseException as rollback_error:
                raise RuntimeError(
                    "OPCUA_Input write failed and rollback also failed: %s" % rollback_error
                ) from write_error
            raise

    async def read_legacy_results(self) -> dict[str, dict[str, float]]:
        db_name = str(self.config.get("opcua.legacy_write_db", "OPCUA_Input"))
        values: dict[str, dict[str, float]] = {}
        for consumer in self.config.consumers:
            values[consumer.address] = {}
            for field in ("Heating", "DHW", "P1", "P2"):
                node = self.client.get_node(self.consumer_node_id(db_name, consumer.address, field))
                values[consumer.address][field] = float(await node.read_value())
        return values

    async def _write_legacy_values(self, db_name: str, consumers: dict[str, dict[str, float]]) -> None:
        for consumer in self.config.consumers:
            address = consumer.address
            for field in ("Heating", "DHW", "P1", "P2"):
                node = self.client.get_node(self.consumer_node_id(db_name, address, field))
                await self._write_float_value_only(node, consumers[address][field])

    async def _write_float_value_only(self, node: Any, value: float) -> None:
        """Write only the OPC UA Value field, without status or timestamps.

        asyncua's ``node.write_value(value, variant_type)`` adds a source
        timestamp.  Siemens S7 servers reject that DataValue combination with
        BadWriteNotSupported even when the PLC tag itself is writable.
        """
        await self._write_value_only(node, float(value), self.ua.VariantType.Float)

    async def _write_value_only(self, node: Any, value: Any, variant_type: Any) -> None:
        data_value = self.ua.DataValue(self.ua.Variant(value, variant_type))
        await node.write_attribute(self.ua.AttributeIds.Value, data_value)

    def _scada_trigger_node_id(self, key: str) -> str:
        node_id = self.config.get(f"scada_trigger.nodes.{key}")
        if not node_id:
            raise KeyError(f"Missing SCADA trigger node in config.toml: {key}")
        return str(node_id)
