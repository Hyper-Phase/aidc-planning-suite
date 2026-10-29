"""Hourly source-backed and explicitly synthetic stress cases."""
from __future__ import annotations

from copy import deepcopy

from scenario_builder import build_scenarios
from solver import certified


def adapt_for_variant(scenario, variant):
    """v3.1 consumes total arrivals; later models consume separate job streams."""
    adapted = deepcopy(scenario)
    if variant == "v3.1":
        adapted.pop("training_arrivals", None)
        adapted.pop("inference_arrivals", None)
    return adapted


def stress_cases(boundary, facts, features):
    winter, summer = build_scenarios(boundary, facts, "v3.6", features)
    cases = []

    def add(name, source, changes, label, description):
        scenario = deepcopy(source)
        scenario["name"] = name
        scenario["weight"] = 365.0  # isolated year-equivalent sensitivity, not an annual frequency
        for field, multiplier, offset in changes:
            scenario[field] = [(value * multiplier + offset if field == "ambient"
                                else max(0., value * multiplier + offset))
                               for value in scenario[field]]
        if any(field in ("inference_arrivals", "training_arrivals") for field, _, _ in changes):
            scenario["arrivals"] = [a + b for a, b in zip(
                scenario["training_arrivals"], scenario["inference_arrivals"])]
        cases.append({"name": name, "scenario": scenario, "label": label,
                      "description": description, "source_date": source.get("name"),
                      "weather_url": source.get("weather_source_url"),
                      "weight_note": "365-day repetition for sensitivity only; no event probability"})

    add("winter_observed", winter, [], "observed weather + combined assumed load/price",
        "2025 winter date with independently sampled Azure daily load shape")
    add("summer_observed", summer, [], "observed weather + combined assumed load/price",
        "2025 summer date with independently sampled Azure daily load shape")
    add("inference_peak", summer, [("inference_arrivals", 1.4, 0.)],
        "synthetic stress", "summer weather plus 40% more inference GPU-hours")
    add("heat_wave", summer, [("ambient", 1., 5.)],
        "synthetic stress", "summer weather plus 5 C at every hour")
    add("cold_wave", winter, [("ambient", 1., -5.)],
        "synthetic stress", "winter weather minus 5 C at every hour")
    add("low_solar", summer, [("pv", .2, 0.)],
        "synthetic stress", "summer weather with PV proxy at 20%")
    add("price_surge", summer, [("price", 1.5, 0.)],
        "synthetic stress", "summer weather with electricity price 50% higher")
    return cases


def evaluate_fixed(boundary, features, fixed_capacities, cases, backend="scipy", variant="v3.6"):
    """Compare frozen investments and a new design on each identical stress day."""
    rows = []
    for case in cases:
        scenario = adapt_for_variant(case["scenario"], variant)
        result, hourly, jobs = certified(boundary, [scenario], (1, 0, 0, 0),
                                         fixed=fixed_capacities, backend=backend,
                                         variant=variant, features=features)
        row = {k: case[k] for k in ("name", "label", "description", "source_date",
                                      "weather_url", "weight_note")}
        row.update(status=("fixed_design_feasible" if result["verified_feasible"]
                           else "fixed_design_infeasible" if result["status"] == "infeasible"
                           else "solver_not_proven_or_audit_failed"),
                   solver_status=result["status"], verified=result["verified_feasible"],
                   mip_gap=result.get("mip_gap"), seconds=result["seconds"])
        redesign = certified(boundary, [scenario], (1, 0, 0, 0),
                             backend=backend, variant=variant, features=features)
        row["redesign"] = {"status": redesign[0]["status"],
                           "verified": redesign[0]["verified_feasible"],
                           "mip_gap": redesign[0].get("mip_gap")}
        if redesign[0]["verified_feasible"]:
            row["redesign"].update(capacities=redesign[0]["capacities"],
                                   annual_cost_usd=redesign[0]["annual_cost_usd"],
                                   green_fraction=redesign[0]["metrics"]["green_fraction_delivered"],
                                   delay_gpu_hour_hours=redesign[0]["metrics"]["delay"],
                                   minimum_grid_headroom_kw=redesign[0]["metrics"]["minimum_grid_headroom_kw"])
        if result["verified_feasible"]:
            metrics = result["metrics"]
            row.update(annual_cost_usd=result["annual_cost_usd"],
                       green_fraction=metrics["green_fraction_delivered"],
                       delay_gpu_hour_hours=metrics["delay"],
                       thermal_risk=metrics["risk"],
                       minimum_grid_headroom_kw=metrics["minimum_grid_headroom_kw"],
                       capacity_shortfall_kw=0.)
        else:
            # This is a lower-bound diagnostic, not a proof of the precise shortfall.
            infer_stream = ([(1 - boundary.training_share) * value for value in scenario["arrivals"]]
                            if variant == "v3.1" else scenario["inference_arrivals"])
            max_inference = max(infer_stream)
            available = max(0., (fixed_capacities["servers"] * 8 - 8 * boundary.redundancy)
                            * boundary.utilization)
            row["minimum_inference_gpu_hour_shortfall"] = max(0., max_inference - available)
            row["capacity_shortfall_kw"] = None
            if case["name"] == "heat_wave":
                row["cooling_only_upgrade"] = minimum_cooling_upgrade(
                    boundary, features, fixed_capacities, scenario, backend, variant)
        rows.append(row)
    return rows


def joint_redesign(boundary, features, cases, backend="scipy", variant="v3.6"):
    """One shared investment across all stress days; equal weights are illustrative."""
    scenarios = [adapt_for_variant(case["scenario"], variant) for case in cases]
    for scenario in scenarios:
        scenario["weight"] = 365 / len(scenarios)
    result, hourly, jobs = certified(boundary, scenarios, (1, 0, 0, 0),
                                     backend=backend, variant=variant, features=features)
    answer = {"status": result["status"], "verified": result["verified_feasible"],
              "mip_gap": result.get("mip_gap"),
              "scenario_names": [scenario["name"] for scenario in scenarios],
              "scenario_weight_days": 365 / len(scenarios),
              "weight_note": "equal stress weights for design experiment, not event probabilities"}
    if result["verified_feasible"]:
        answer.update(capacities=result["capacities"],
                      annual_cost_usd=result["annual_cost_usd"],
                      green_fraction=result["metrics"]["green_fraction_delivered"],
                      delay_gpu_hour_hours=result["metrics"]["delay"])
    return answer, (result, hourly, jobs)


def minimum_cooling_upgrade(boundary, features, fixed_capacities, scenario,
                            backend="scipy", variant="v3.6"):
    """Hold every other investment fixed; bisect a feasible cooling nameplate."""
    lower = fixed_capacities["cooling_kw"]
    upper = max(lower + 25., lower * 1.5)

    def trial(cooling_kw):
        cap = dict(fixed_capacities)
        cap["cooling_kw"] = cooling_kw
        return certified(boundary, [scenario], (1, 0, 0, 0), fixed=cap,
                         backend=backend, variant=variant, features=features)[0]

    solved = trial(upper)
    while not solved["verified_feasible"] and upper < 5000:
        lower, upper = upper, min(5000., upper * 1.5)
        solved = trial(upper)
    if not solved["verified_feasible"]:
        return {"status": "not restored by cooling up to 5000 kW",
                "solver_status": solved["status"]}
    for _ in range(9):
        mid = (lower + upper) / 2
        attempt = trial(mid)
        if attempt["verified_feasible"]:
            upper, solved = mid, attempt
        elif attempt["status"] == "infeasible":
            lower = mid
        else:
            return {"status": "solver not proven during threshold search",
                    "solver_status": attempt["status"]}
    return {"status": "audited feasible with only cooling capacity changed",
            "original_cooling_kw": fixed_capacities["cooling_kw"],
            "sufficient_cooling_kw": upper,
            "additional_cooling_kw": upper - fixed_capacities["cooling_kw"],
            "resolution_kw": upper - lower,
            "isolated_scenario_annual_cost_usd": solved["annual_cost_usd"],
            "note": "sufficient near-threshold nameplate under this MILP; not an engineered design margin"}
