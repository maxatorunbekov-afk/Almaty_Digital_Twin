from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

from .config import load_config
from .fluidit.model import FHeatModel
from .opcua_io import SiemensOpcUaClient
from .orchestrator import DigitalTwinOrchestrator
from .scenarios import build_research_model
from .triggered import execute_model_now, serve_scada_requests
from .writeback import write_result_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Almaty heating digital-twin bridge")
    parser.add_argument("--config", default="config/config.toml", help="Path to TOML configuration")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config")
    inspect = commands.add_parser("inspect-model")
    inspect.add_argument("--model", default=None)
    build = commands.add_parser("build-scenarios")
    build.add_argument("--input", default=None)
    build.add_argument("--output", default=None)
    once = commands.add_parser("once")
    once.add_argument("--offline", action="store_true", help="Use the research-table sample instead of OPC UA")
    service = commands.add_parser("service")
    service.add_argument("--offline", action="store_true")
    commands.add_parser("model-now", help="Read live PLC, run one real Fluidit scenario and write OPCUA_Input")
    commands.add_parser("scada-service", help="Wait for DT_Model_Control.Request from SCADA")
    commands.add_parser("probe-opcua")
    write_once = commands.add_parser("write-result-once")
    write_once.add_argument("--result", default="runtime/fluidit_bridge_test/result.json")
    write_once.add_argument("--confirm", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
        if args.command == "validate-config":
            print(json.dumps({"ok": True, "config": str(config.path), "consumers": len(config.consumers)}, ensure_ascii=False, indent=2))
        elif args.command == "inspect-model":
            model = Path(args.model).resolve() if args.model else config.resolve("project.model")
            print(json.dumps(FHeatModel(model, config).inspect(), ensure_ascii=False, indent=2))
        elif args.command == "build-scenarios":
            source = Path(args.input).resolve() if args.input else config.resolve("project.original_model")
            output = Path(args.output).resolve() if args.output else config.resolve("project.model")
            print(build_research_model(config, source, output))
        elif args.command == "once":
            recommendation = asyncio.run(DigitalTwinOrchestrator(config, offline=args.offline).run_once())
            print(json.dumps(recommendation.to_dict() if recommendation else {"status": "no_simulation"}, ensure_ascii=False, indent=2))
        elif args.command == "service":
            asyncio.run(DigitalTwinOrchestrator(config, offline=args.offline).serve())
        elif args.command == "model-now":
            print(json.dumps(asyncio.run(execute_model_now(config)), ensure_ascii=False, indent=2))
        elif args.command == "scada-service":
            asyncio.run(serve_scada_requests(config))
        elif args.command == "probe-opcua":
            asyncio.run(_probe_opcua(config))
        elif args.command == "write-result-once":
            result_path = Path(args.result)
            if not result_path.is_absolute():
                result_path = config.root / result_path
            report = asyncio.run(write_result_once(config, result_path.resolve(), args.confirm))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.exception("Command failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


async def _probe_opcua(config):
    async with SiemensOpcUaClient(config) as client:
        rows = await client.probe()
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    failed = sum(1 for row in rows if not row["ok"])
    if failed:
        raise RuntimeError(f"{failed} OPC UA nodes could not be read")


if __name__ == "__main__":
    raise SystemExit(main())
