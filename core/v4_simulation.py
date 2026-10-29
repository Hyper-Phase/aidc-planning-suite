"""v4 offline scene replay, component faults, finite bargaining, and replanning."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

from scenario_builder import build_scenarios
from solver import certified
from v4_faults import event_catalog, replay, replay_catalog
from v4_scenarios import evaluate_fixed, joint_redesign, stress_cases


def candidate_metrics(pack, power):
    result = pack[0]
    events = replay_catalog(pack, power)
    applicable = [event for event in events if event["status"] == "simulated"]
    if not all(event["audit_passed"] for event in applicable):
        raise ValueError("A fault replay failed its conservation audit")
    return {"annual_cost_usd": result["annual_cost_usd"],
            "brown_kwh": result["metrics"]["brown"],
            "delay_gpu_hour_hours": result["metrics"]["delay"],
            "worst_fault_unserved_kwh": max(e["unserved_kwh"] for e in applicable),
            "worst_ups_bridge_shortfall_kwh": max(e["ups_bridge_shortfall_kwh"]
                                                   for e in applicable),
            "worst_service_gap_proxy_kwh": max(e["unserved_kwh"] + e["ups_bridge_shortfall_kwh"]
                                                for e in applicable),
            "green_fraction": result["metrics"]["green_fraction_delivered"],
            "mip_gap": result["mip_gap"], "verified": result["verified_feasible"]}


def negotiate(candidate_rows, preferences=None, rounds=3):
    """Finite four-agent protocol; no Nash equilibrium claim."""
    names = ("annual_cost_usd", "brown_kwh", "delay_gpu_hour_hours",
             "worst_service_gap_proxy_kwh")
    preferences = preferences or {name: 1. for name in names}
    ideals = {name: min(row[name] for row in candidate_rows) for name in names}
    spans = {name: max(max(row[name] for row in candidate_rows) - ideals[name], 1e-9)
             for name in names}
    regret = [{name: (row[name] - ideals[name]) / spans[name] for name in names}
              for row in candidate_rows]
    transcript = []
    selected = None
    for turn in range(rounds):
        threshold = [.4, .72, 1.05][min(turn, 2)]
        offers = {name: min(range(len(regret)), key=lambda i: regret[i][name]) for name in names}
        accepted = [i for i, values in enumerate(regret)
                    if all(values[name] <= threshold + 1e-9 for name in names)]
        if accepted:
            selected = max(accepted, key=lambda i: sum(
                preferences[name] * math.log(max(1e-8, 1.1 - regret[i][name])) for name in names))
        transcript.append({"round": turn + 1, "agent_threshold": threshold,
                           "offers": offers, "accepted_candidate_ids": accepted,
                           "selected_candidate_id": selected})
    return {"status": "agreement" if selected is not None else "no_agreement",
            "selected_candidate_id": selected, "ideal": ideals, "span": spans,
            "preferences": preferences, "rounds": transcript,
            "scope": "finite audited-candidate agreement, not a noncooperative equilibrium"}


def bargain(boundary, scenarios, features, baseline, backend="scipy",
            joint_capacities=None, variant="v3.6"):
    packs = [baseline]
    labels = ["cost"]
    for label, weight in (("brown", (0, 1, 0, 0)), ("delay", (0, 0, 1, 0)),
                          ("thermal_risk", (0, 0, 0, 1))):
        pack = certified(boundary, scenarios, weight, backend=backend,
                         variant=variant, features=features)
        if pack[0]["verified_feasible"]:
            labels.append(label); packs.append(pack)
    if joint_capacities is not None:
        pack = certified(boundary, scenarios, (1, 0, 0, 0), fixed=joint_capacities,
                         backend=backend, variant=variant, features=features)
        if pack[0]["verified_feasible"]:
            labels.append("stress_joint_design_on_base_scenarios"); packs.append(pack)
    rows = [{"id": i, "solver_objective": label,
             "capacities": pack[0]["capacities"], "capex_usd": pack[0].get("capex_usd"),
             **candidate_metrics(pack, features["power"])}
            for i, (label, pack) in enumerate(zip(labels, packs))]
    equal = negotiate(rows)
    reliability_first = negotiate(rows, {"annual_cost_usd": .5, "brown_kwh": 1.,
                                         "delay_gpu_hour_hours": 1.,
                                         "worst_service_gap_proxy_kwh": 3.})
    return {"candidates": rows, "equal_preferences": equal,
            "reliability_first": reliability_first,
            "candidate_packs": packs}


def replan(boundary, facts, features, old_pack, backend="scipy", variant="v3.6"):
    """Demand-change trigger, old-design recourse, and fresh investment optimization."""
    revised = replace(boundary, max_gpus=max(boundary.max_gpus, boundary.min_gpus + 64),
                      growth=boundary.growth * 1.2)
    scenarios = build_scenarios(revised, facts, variant, features)
    old_cap = old_pack[0]["capacities"]
    old_recourse = certified(revised, scenarios, (1, 0, 0, 0), fixed=old_cap,
                             backend=backend, variant=variant, features=features)
    new = certified(revised, scenarios, (1, 0, 0, 0), backend=backend,
                    variant=variant, features=features)
    def summarize(pack):
        r = pack[0]
        out = {"status": r["status"], "verified": r["verified_feasible"],
               "solver_message": r.get("solver_message"), "mip_gap": r.get("mip_gap")}
        if r["verified_feasible"]:
            fault_rows = [item for item in replay_catalog(pack, features["power"])
                          if item["status"] == "simulated"]
            out.update(annual_cost_usd=r["annual_cost_usd"],
                       green_fraction=r["metrics"]["green_fraction_delivered"],
                       delay_gpu_hour_hours=r["metrics"]["delay"],
                       capacities=r["capacities"],
                       worst_fault_unserved_kwh=max(item["unserved_kwh"] for item in fault_rows),
                       worst_ups_bridge_shortfall_kwh=max(
                           item["ups_bridge_shortfall_kwh"] for item in fault_rows),
                       worst_service_gap_proxy_kwh=max(
                           item["unserved_kwh"] + item["ups_bridge_shortfall_kwh"]
                           for item in fault_rows))
        return out
    return {"trigger": "research scenario: workload growth multiplier increases by 20%",
            "triggered_utc": datetime.now(timezone.utc).isoformat(),
            "old_design_on_new_input": summarize(old_recourse),
            "new_design_on_new_input": summarize(new),
            "old_design_before_change": summarize(old_pack),
            "scope": "offline full reoptimization; old investment frozen for comparison",
            "missing_decisions": ["client confirmation of actual demand and SLA",
                                  "approved grid connection", "equipment quotes and failure records"]}


def evaluate(version, boundary, facts, features, base_pack, backend="scipy",
             model_variant="v3.6"):
    base = base_pack[0]
    baseline = {"annual_cost_usd": base["annual_cost_usd"],
                "brown_kwh": base["metrics"]["brown"],
                "delay_gpu_hour_hours": base["metrics"]["delay"],
                "green_fraction": base["metrics"]["green_fraction_delivered"],
                "mip_gap": base["mip_gap"], "verified": base["verified_feasible"]}
    if version >= "v4.3":
        baseline = candidate_metrics(base_pack, features["power"])
    result = {"version": version, "model_variant": model_variant, "baseline": baseline,
              "baseline_capacities": base["capacities"]}
    if version >= "v4.2":
        cases = stress_cases(boundary, facts, features)
        result["stress"] = evaluate_fixed(boundary, features, base_pack[0]["capacities"],
                                          cases, backend, model_variant)
        result["stress_joint_redesign"], joint_pack = joint_redesign(
            boundary, features, cases, backend, model_variant)
    if version >= "v4.3":
        result["faults"] = replay_catalog(base_pack, features["power"])
        outage = next(event for event in event_catalog() if event["name"] == "grid_outage")
        base_outage = next(item for item in result["faults"]
                           if item["event"]["name"] == "grid_outage")
        max_soc = .9 * base["capacities"]["battery_kwh"]
        precharged = replay(base_pack, outage, features["power"], initial_soc_kwh=max_soc)
        result["fault_mitigation"] = {
            "option": "precharge installed battery to model maximum before outage",
            "capacity_changed": False, "forecast_required": True,
            "grid_outage_baseline_unserved_kwh": base_outage["unserved_kwh"],
            "grid_outage_precharged_unserved_kwh": precharged["unserved_kwh"],
            "starting_soc_kwh": base_outage["initial_soc_kwh"],
            "battery_max_soc_kwh": max_soc,
            "battery_power_kw": base_outage["battery_power_kw"],
            "peak_net_load_after_pv_kw": base_outage["peak_net_load_after_pv_kw"],
            "net_load_after_pv_kwh": base_outage["net_load_after_pv_kwh"],
            "initial_usable_battery_kwh": base_outage["initial_usable_battery_kwh"],
            "precharged_replay_audit_passed": precharged["audit_passed"],
            "scope": "counterfactual initial SOC; installed battery power and usable energy remain fixed"}
    if version >= "v4.4":
        negotiated = bargain(boundary, build_scenarios(boundary, facts, model_variant, features),
                             features, base_pack, backend,
                             result["stress_joint_redesign"].get("capacities"), model_variant)
        result["bargaining"] = {k: v for k, v in negotiated.items() if k != "candidate_packs"}
    if version >= "v4.5":
        result["replanning"] = replan(boundary, facts, features, base_pack,
                                     backend, model_variant)
    return result
