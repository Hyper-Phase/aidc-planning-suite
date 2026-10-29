"""Human-readable step-five output for each v4 research version."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write(path, context, manifest, evaluation):
    path = Path(path)
    baseline = evaluation["baseline"]
    requested_objective = ("unspecified; using the documented cost baseline" if
                           context["goal"] == "cost_green_bargaining" else context["goal"])
    lines = [
        f"# {evaluation['version']} offline plan and scenario replay", "",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}", "",
        "## Input and scope", "",
        f"- Original request: {context['text']}",
        f"- Parsed planning objective: {requested_objective}.",
        f"- Located site: {context['research'].get('point')}",
        f"- Data access: {manifest['access']}; aggregate features: {manifest['aggregate_bytes']:,} bytes; no local raw traces.", "",
        "This is an offline research scenario based on public data and explicit assumptions. It is not site certification, a reliability guarantee, or real-time control.", "",
        "## Baseline", "",
        "- Baseline optimization objective: minimum annual cost.",
        f"- Annual cost: {baseline['annual_cost_usd']:,.0f} USD.",
        f"- Green electricity share: {baseline['green_fraction']:.2%}.",
        f"- Optimization and both audits passed: {baseline['verified']}.",
    ]
    if manifest.get("site_adjustment"):
        names = ", ".join(manifest["site_adjustment"]["adjustments"])
        lines.insert(10, f"- Site-value priority: user-provided {names} adjusted the hourly weather/solar proxy; see source_manifest for amounts and limitations.")
    if "worst_fault_unserved_kwh" in baseline:
        lines += [
            f"- Worst applicable fault unserved energy: {baseline['worst_fault_unserved_kwh']:.2f} kWh.",
            f"- Worst UPS bridge shortfall: {baseline['worst_ups_bridge_shortfall_kwh']:.2f} kWh.",
            f"- Worst service gap proxy: {baseline['worst_service_gap_proxy_kwh']:.2f} kWh. This is a per-event proxy, not observed loss or expected annual loss.",
        ]
    if "stress" in evaluation:
        lines += ["", "## Hourly stress scenarios: fixed and redesigned equipment", "",
                  "Each day is repeated 365 times only for conditional annualization. The rows do not assign event probabilities. A one-day redesign does not ensure feasibility across all days.", "",
                  "| Scenario | Data label | Fixed status | Fixed annualized cost USD | Redesign status | Redesign annualized cost USD | Cooling kW |",
                  "| --- | --- | --- | ---: | --- | ---: | ---: |"]
        for row in evaluation["stress"]:
            redesign = row["redesign"]
            lines.append("| " + " | ".join([
                row["name"], row["label"], row["status"],
                f"{row['annual_cost_usd']:,.0f}" if row["verified"] else "—",
                redesign["status"],
                f"{redesign['annual_cost_usd']:,.0f}" if redesign["verified"] else "—",
                f"{redesign['capacities']['cooling_kw']:.2f}" if redesign["verified"] else "—",
            ]) + " |")
        joint = evaluation.get("stress_joint_redesign")
        if joint:
            lines += ["", f"Shared design across seven stress days: {joint['status']}."
                      + (f" Cooling: {joint['capacities']['cooling_kw']:.2f} kW; equal-weight annualized cost: {joint['annual_cost_usd']:,.0f} USD." if joint["verified"] else ""),
                      "Equal weights are experimental design weights, not event probabilities."]
        heat = next((r for r in evaluation["stress"] if r["name"] == "heat_wave"), None)
        if heat and heat["status"] == "fixed_design_infeasible":
            upgrade = heat.get("cooling_only_upgrade", {})
            lines += ["", "### Heat-wave infeasibility", "",
                      f"Original cooling capacity: {evaluation['baseline_capacities']['cooling_kw']:.2f} kW. Fixed investment is infeasible; free redesign status: {heat['redesign']['status']}."]
            if upgrade.get("sufficient_cooling_kw") is not None:
                lines += [f"Changing only cooling to about {upgrade['sufficient_cooling_kw']:.2f} kW (+{upgrade['additional_cooling_kw']:.2f} kW) yields an audited feasible solution. Search resolution: {upgrade['resolution_kw']:.2f} kW; this is not an engineering safety margin."]
            lines += ["The threshold uses a simplified cyclic RC thermal model and a 35 C hard limit. Equipment curves, airflow, and design margin require independent review."]
    if "faults" in evaluation:
        lines += ["", "## Minute-level fault replay", "",
                  "Faults reuse the selected design and starting SOC. The model does not represent fault probabilities, protection actions, or transient stability.", "",
                  "| Fault | Start hour | Duration min | Status | Unserved kWh | UPS bridge shortfall kWh | Audit |",
                  "| --- | ---: | ---: | --- | ---: | ---: | --- |"]
        for row in evaluation["faults"]:
            event = row["event"]
            if row["status"] == "not_applicable":
                lines.append(f"| {event['name']} | {event['start_hour']} | {event['duration_min']} | Not applicable: {row['applicability_reason']} | — | — | — |")
            else:
                lines.append(f"| {event['name']} | {event['start_hour']} | {event['duration_min']} | Simulated | {row['unserved_kwh']:.2f} | {row['ups_bridge_shortfall_kwh']:.2f} | {row['audit_passed']} |")
        mitigation = evaluation.get("fault_mitigation")
        if mitigation:
            lines += ["", f"Precharging the existing battery changes grid-outage unserved energy from {mitigation['grid_outage_baseline_unserved_kwh']:.2f} to {mitigation['grid_outage_precharged_unserved_kwh']:.2f} kWh. Starting SOC is {mitigation['starting_soc_kwh']:.1f} kWh, already at the model limit of {mitigation['battery_max_soc_kwh']:.1f} kWh. Battery power is {mitigation['battery_power_kw']:.1f} kW versus peak net load {mitigation['peak_net_load_after_pv_kw']:.1f} kW; net energy need is {mitigation['net_load_after_pv_kwh']:.1f} kWh versus {mitigation['initial_usable_battery_kwh']:.1f} kWh initially usable. Fault-hardening investment is not optimized here."]
    if "bargaining" in evaluation:
        bargaining = evaluation["bargaining"]
        equal, preferred = bargaining["equal_preferences"], bargaining["reliability_first"]
        lines += ["", "## Four-party choice among finite candidates", "",
                  f"Candidates: {len(bargaining['candidates'])}; equal-preference choice: {equal['selected_candidate_id']} ({equal['status']}); reliability-first choice: {preferred['selected_candidate_id']} ({preferred['status']}).",
                  "Candidates are audited and scored on a common two-season baseline. The service-gap proxy is computed after fixed-design fault replay. This finite selection is not a full Pareto frontier or a game equilibrium."]
    if "replanning" in evaluation:
        changed = evaluation["replanning"]
        old, new = changed["old_design_on_new_input"], changed["new_design_on_new_input"]
        lines += ["", "## Offline replanning after an input change", "",
                  f"Trigger: {changed['trigger']}; UTC: {changed['triggered_utc']}.",
                  f"Old equipment on new input: {old['status']}; audited feasible: {old['verified']}.",
                  f"New equipment on new input: {new['status']}; audited feasible: {new['verified']}."]
        if old["verified"] and new["verified"]:
            lines += [f"- Annual cost: old {old['annual_cost_usd']:,.0f}; new {new['annual_cost_usd']:,.0f} USD.",
                      f"- Worst applicable fault unserved: old {old['worst_fault_unserved_kwh']:.2f}; new {new['worst_fault_unserved_kwh']:.2f} kWh.",
                      f"- Worst service-gap proxy: old {old['worst_service_gap_proxy_kwh']:.2f}; new {new['worst_service_gap_proxy_kwh']:.2f} kWh."]
        lines += ["Client decisions still required: " + "; ".join(changed["missing_decisions"]) + "."]
    lines += ["", "## Complete structured record", "", "```json",
              json.dumps({"input_facts": context["facts"], "field_evidence": context["evidence"],
                          "source_manifest": manifest, "evaluation": evaluation},
                         ensure_ascii=False, indent=2, default=float), "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{evaluation['version']}: USD {baseline['annual_cost_usd']:,.0f}/year; "
          f"green {baseline['green_fraction']:.2%}; report {path}")
    return 0
