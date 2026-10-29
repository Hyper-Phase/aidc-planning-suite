"""The five stable entry stages shared by v4.1 through v4.5."""
from __future__ import annotations

import sys
from pathlib import Path

from user_input import read_user_input
from initial_input import complete_initial_information
from solver import build_and_solve_model
from v4_data import load, apply_site_measurements
from v4_simulation import evaluate
from v4_report import write

VERSIONS = {f"v4.{n}" for n in range(1, 6)}


def run(version):
    if version not in VERSIONS:
        raise ValueError(version)
    output = Path(__file__).resolve().parents[1] / "plan_output.md"
    try:
        request = read_user_input()                     # 1. user input
    except RuntimeError as exc:
        output.write_text("# Conditions insufficient for v4 planning\n\nInput service: " +
                          str(exc) + ".\n", encoding="utf-8")
        print(f"Planning input unavailable: {exc}. Report: {output}")
        return 2
    if "--local" in sys.argv:
        from local_input import complete_local_information
        context = complete_local_information(request)
    else:
        context = complete_initial_information(request)  # 2. information check
    if context["missing"] or context.get("clarification_issues") or not context["research"].get("point"):
        fields = list(context["missing"])
        fields.extend(context.get("clarification_issues", {}).values())
        if not context["research"].get("point"):
            fields.append("geocodable site location")
        output.write_text("# Conditions insufficient for v4 planning\n\nMissing, conflicting, or unsupported: " + ", ".join(fields) + ".\n",
                          encoding="utf-8")
        print(f"Missing planning inputs: {fields}. Report: {output}")
        return 2
    cache = None
    if "--feature-cache" in sys.argv:
        cache = Path(sys.argv[sys.argv.index("--feature-cache") + 1])
    features, manifest = load(context["research"]["point"], cache=cache,
                              refresh="--refresh" in sys.argv)
    if "--local" not in sys.argv:
        features, site_adjustment = apply_site_measurements(features, context)
        if site_adjustment:
            manifest["site_adjustment"] = site_adjustment
            print("User-provided site weather/solar values calibrated the hourly proxy:",
                  ", ".join(site_adjustment["adjustments"]))
    context["features"] = features
    context["variant"] = "v3.6"                  # v4 uses the audited v3 capacity model
    try:
        model = build_and_solve_model(context)        # 3. optimization
    except ValueError as exc:
        output.write_text("# Conditions insufficient for v4 planning\n\nInput validation: " +
                          str(exc) + ".\n", encoding="utf-8")
        print(f"Input validation failed before an audited baseline. Report: {output}")
        return 2
    if not model or not model["cost"][0]["verified_feasible"]:
        output.write_text("# Conditions insufficient for an audited v4 plan\n\n"
                          "No audited feasible baseline under the supplied conditions. "
                          "Solver status: " + str(context.get("solver_status")) + ". "
                          "Review the site boundary and planning limits before rerunning. "
                          + " ".join(context.get("preflight_issues", [])) + "\n",
                          encoding="utf-8")
        print(f"No audited baseline. Report: {output}")
        return 2
    study = evaluate(version, model["boundary"], context["facts"], features,
                     model["cost"], context["backend"]) # 4. simulation + bargaining
    study["version"] = version
    return write(output, context, manifest, study)     # 5. output
