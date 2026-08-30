# -*- coding: utf-8 -*-
"""Fluidit Heat 2.8 internal Python 3 bridge.

Invoked by heat64.exe.  The preferred Fluidit 2.8 transport is the inherited
ALMATY_FLUIDIT_REQUEST_JSON environment variable.  Legacy comma-separated
--pyparam arguments remain supported.
"""

import json
import math
import os
import sys
import traceback

import java
from java.nio.file import Paths
from java.util.logging import Logger

# Fluidit Heat 2.8 embeds GraalPy for --python3.  Application classes live in
# NetBeans module JARs, so conventional Python package imports may not expose
# them even though the classes are present.  Load them through GraalPy's Java
# interoperability API instead.
Model = java.type("fi.fluidit.heat.model.Model")
Junction = java.type("fi.fluidit.heat.model.Junction")
Pipe = java.type("fi.fluidit.heat.model.Pipe")
System = java.type("java.lang.System")


LOG = Logger.getLogger("almaty-fluidit-bridge")
GCAL_H_TO_KW = 1163.0

CONSUMERS = {
    "183 Kazybek Bi St": {
        "junctions": ["Junction-11", "Junction-12"],
    },
    "30 Isaeva St": {
        "junctions": ["Junction-14"],
    },
    "129 Chokina St": {
        "junctions": ["Junction-13", "Junction-16"],
    },
    "194 Aiteke Bi St": {
        "junctions": ["Junction-19", "Junction-18"],
    },
    "28 Isaeva St": {
        "junctions": ["Junction-10"],
    },
}


def log(message):
    text = str(message)
    LOG.info(text)
    print(text)


def script_arguments():
    args = [str(value) for value in sys.argv]
    if args and args[0].lower().endswith(".py"):
        args = args[1:]
    if len(args) == 1 and "," in args[0]:
        args = args[0].split(",")
    elif args and "," in args[-1]:
        expanded = args[-1].split(",")
        if len(expanded) >= 3:
            args = expanded
    args = [value.strip().strip('"') for value in args if value.strip()]
    if len(args) == 3:
        return [os.path.abspath(value) for value in args]

    request_from_env = os.environ.get("ALMATY_FLUIDIT_REQUEST_JSON", "").strip().strip('"')
    if request_from_env:
        request_path = os.path.abspath(request_from_env)
        with open(request_path, "r", encoding="utf-8-sig") as stream:
            request = json.load(stream)
        model_path = str(request.get("model", "")).strip()
        result_path = str(request.get("result_json", "")).strip()
        if not model_path or not result_path:
            raise ValueError(
                "The Fluidit request must contain non-empty model and result_json fields: %s"
                % request_path
            )
        return [os.path.abspath(model_path), os.path.abspath(result_path), request_path]

    raise ValueError(
        "Expected ALMATY_FLUIDIT_REQUEST_JSON or --pyparam "
        "candidate.fheat,result.json,simulation_request.json; got %r" % args
    )


def number(value, label):
    if hasattr(value, "doubleValue"):
        result = float(value.doubleValue())
    else:
        result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite Fluidit result for %s: %r" % (label, value))
    return result


def component_result(component, result_name):
    if component is None:
        raise ValueError("Fluidit component was not found")
    return number(component.result(result_name), "%s.%s" % (component.name, result_name))


def find_component(scenario, name, component_type):
    component = scenario.findComponent(name, component_type)
    if component is None:
        raise ValueError("Component not found in active scenario: %s" % name)
    return component


def export_results(scenario, request):
    snapshot = request.get("snapshot", {}).get("consumers", {})
    exported = {}
    address_deficits = []
    differential_pressures = []

    for address, mapping in CONSUMERS.items():
        service_junctions = [
            find_component(scenario, junction_name, Junction)
            for junction_name in mapping["junctions"]
        ]
        junction = service_junctions[0]
        p1 = component_result(junction, "PRESSURE")
        p2 = component_result(junction, "RET_PRESSURE")
        address_dps = [
            component_result(service_junction, "PRESSURE_DIFFERENCE")
            for service_junction in service_junctions
        ]
        differential_pressures.extend(address_dps)

        delivered_kw = 0.0
        deficit_kw = 0.0
        for service_junction in service_junctions:
            delivered_kw += abs(component_result(service_junction, "DELIVERED_POWER"))
            deficit_kw += max(0.0, component_result(service_junction, "DEFICIT"))
        address_deficits.append(deficit_kw)

        measured = snapshot.get(address, {})
        heating_in = float(measured.get("heating_gcal_h", 0.0))
        dhw_in = float(measured.get("dhw_gcal_h", 0.0))
        input_total = heating_in + dhw_in
        delivered_gcal_h = delivered_kw / GCAL_H_TO_KW
        if input_total > 0.0:
            heating_out = delivered_gcal_h * heating_in / input_total
            dhw_out = delivered_gcal_h * dhw_in / input_total
        else:
            heating_out = 0.0
            dhw_out = 0.0

        exported[address] = {
            "Heating": heating_out,
            "DHW": dhw_out,
            "P1": p1,
            "P2": p2,
        }

    total_heat_loss_kw = 0.0
    for pipe in scenario.allComponentsOfType(Pipe):
        total_heat_loss_kw += abs(component_result(pipe, "TOTAL_HEAT_LOSS"))

    return {
        "schema_version": 1,
        "converged": True,
        "max_deficit_kw": max(address_deficits) if address_deficits else 0.0,
        "min_dp_bar": min(differential_pressures) if differential_pressures else None,
        "heat_loss_kw": total_heat_loss_kw,
        "consumers": exported,
        "notes": [
            "Generated by Fluidit Heat internal Python 3 bridge.",
            "Pump energy is intentionally omitted because no physical pump is modeled.",
        ],
    }


def write_json(path, payload):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)


def main():
    model_path, result_path, request_path = script_arguments()
    with open(request_path, "r", encoding="utf-8") as stream:
        request = json.load(stream)

    log("Loading Fluidit Heat model: %s" % model_path)
    model = Model.load(Paths.get(model_path))
    scenario = model.active
    log("Active scenario: %s" % scenario.name)
    scenario.simulate()
    scenario.loadResults()
    payload = export_results(scenario, request)
    model.save(Paths.get(model_path))
    write_json(result_path, payload)
    log("Fluidit result written: %s" % result_path)


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except BaseException as exc:
        exit_code = 1
        message = "%s: %s" % (exc.__class__.__name__, exc)
        LOG.severe(message)
        traceback.print_exc()
        try:
            args = script_arguments()
            write_json(args[1], {
                "schema_version": 1,
                "converged": False,
                "max_deficit_kw": None,
                "min_dp_bar": None,
                "heat_loss_kw": None,
                "consumers": {},
                "notes": [message],
            })
        except BaseException:
            pass
    finally:
        # The NetBeans Windows launcher otherwise leaves a headless Heat JVM
        # and its floating licence alive after the script has completed.
        System.exit(exit_code)
