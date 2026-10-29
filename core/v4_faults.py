"""Deterministic minute-resolution fault replay of an audited hourly plan.

This is a planning stress calculation, not an electrical transient simulator.
"""
from __future__ import annotations

def event_catalog():
    common = {"recovery": "component returns to nominal capacity after duration",
              "probability": None, "origin": "explicit research stress assumption; no site failure history"}
    return [
        {**common, "name": "grid_outage", "kind": "grid", "start_hour": 18,
         "duration_min": 240, "factor": 0.},
        {**common, "name": "pv_string_loss", "kind": "pv", "start_hour": 12,
         "duration_min": 240, "factor": .5},
        {**common, "name": "inverter_derate", "kind": "inverter", "start_hour": 12,
         "duration_min": 240, "factor": .5},
        {**common, "name": "battery_failure", "kind": "battery", "start_hour": 18,
         "duration_min": 240, "factor": 0.},
        {**common, "name": "ups_failure_with_grid_trip", "kind": "ups", "start_hour": 18,
         "duration_min": 1, "factor": 0., "coincident_grid_trip": True,
         "fault_scope": "entire UPS fleet unavailable; common-cause stress"},
        {**common, "name": "diesel_failure_with_grid_trip", "kind": "diesel", "start_hour": 18,
         "duration_min": 240, "factor": 0., "coincident_grid_trip": True},
        {**common, "name": "one_transformer_failure", "kind": "transformer", "start_hour": 18,
         "duration_min": 240, "factor": 0.},
        {**common, "name": "synchronous_gpu_power_p95", "kind": "power_spike", "start_hour": 18,
         "duration_min": 1, "factor": 1.},
    ]


def replay(pack, event, power_features, scenario=None, initial_soc_kwh=None):
    result, hourly, _jobs = pack
    if not result.get("verified_feasible"):
        raise ValueError("Fault replay requires an audited candidate")
    if scenario is None:
        names = set(hourly.scenario.unique())
        scenario = "summer" if "summer" in names else str(hourly.scenario.iloc[0])
    rows = {int(row.hour): row for row in hourly[hourly.scenario == scenario].itertuples()}
    if len(rows) != 24:
        raise ValueError(f"No 24-hour {scenario} scenario in candidate")
    b, cap = result["boundary"], result["capacities"]
    if event["kind"] == "diesel" and cap["diesel_units"] < .5:
        return {"event": event, "scenario": scenario, "status": "not_applicable",
                "applicability_reason": "no diesel generator installed in this candidate",
                "unserved_kwh": None, "ups_bridge_shortfall_kwh": None,
                "audit_passed": None}
    first = rows[event["start_hour"]]
    soc = (float(initial_soc_kwh) if initial_soc_kwh is not None
           else float(first.ep_before + first.eg_before))
    if not 0 <= soc <= .9 * cap["battery_kwh"] + 1e-6:
        raise ValueError("Initial fault SOC is outside installed battery limits")
    start_soc = soc
    floor = .1 * cap["battery_kwh"]
    fuel = cap["fuel_litres"]
    ups_energy = cap["ups_battery_kwh"] * .8 * .97
    initial_usable_battery_kwh = max(0., start_soc - floor) * .95
    unserved_kwh = bridge_shortfall = diesel_kwh = 0.
    net_load_after_pv_kwh = peak_net_load_after_pv_kw = 0.
    outage_minutes = 0
    min_margin = float("inf")
    max_balance_error = 0.
    for minute in range(event["duration_min"]):
        row = rows[(event["start_hour"] + minute // 60) % 24]
        load = float(row.load)
        if event["kind"] == "power_spike":
            delta = (power_features["active_gpu_kw_p95"] -
                     power_features["active_gpu_kw_mean"])
            load += max(0., delta) * row.training_proc
        pv = float(row.pv_available)
        if event["kind"] == "pv":
            pv *= event["factor"]
        if event["kind"] == "inverter":
            pv *= event["factor"]
        net_after_pv = max(0., load - pv)
        net_load_after_pv_kwh += net_after_pv / 60
        peak_net_load_after_pv_kw = max(peak_net_load_after_pv_kw, net_after_pv)
        grid_limit = float(b["grid_kw"])
        if event["kind"] == "transformer":
            units = max(0., cap["transformer_units"] - 1)
            grid_limit = min(grid_limit, units * 1000 * .8 * .95)
        else:
            grid_limit = min(grid_limit, cap["transformer_units"] * 1000 * .8 * .95)
        grid_lost = event["kind"] == "grid" or event.get("coincident_grid_trip", False)
        if grid_lost:
            grid_limit = 0.
        battery_factor = event["factor"] if event["kind"] in ("battery", "inverter") else 1.
        battery_power = max(0., cap["battery_kw"] * battery_factor)
        battery_energy = max(0., soc - floor) * .95
        diesel_factor = event["factor"] if event["kind"] == "diesel" else 1.
        diesel_power = (cap["diesel_units"] * 1000 * diesel_factor
                        if grid_lost and minute >= 1 else 0.)
        diesel_power = min(diesel_power, fuel / .27 * 60)
        total_possible = pv + grid_limit + min(battery_power, battery_energy * 60) + diesel_power
        min_margin = min(min_margin, total_possible - load)
        remaining = load
        pv_used = min(remaining, pv); remaining -= pv_used
        grid_used = min(remaining, grid_limit); remaining -= grid_used
        diesel_used = min(remaining, diesel_power); remaining -= diesel_used
        battery_used = min(remaining, battery_power, battery_energy * 60); remaining -= battery_used
        served = pv_used + grid_used + diesel_used + battery_used
        max_balance_error = max(max_balance_error, abs(load - served - remaining))
        unserved_kwh += remaining / 60
        diesel_kwh += diesel_used / 60
        fuel -= diesel_used / 60 * .27
        soc -= battery_used / 60 / .95
        if remaining > 1e-6:
            outage_minutes += 1
        if grid_lost and minute == 0:
            critical_kw = load * b["critical_fraction"]
            ups_factor = event["factor"] if event["kind"] == "ups" else 1.
            ups_power = cap["ups_units"] * 500 * ups_factor
            bridge_shortfall = max(0., critical_kw - min(ups_power, ups_energy * 60)) / 60
            ups_energy -= min(critical_kw, ups_power, ups_energy * 60) / 60
    passed = bool(max_balance_error < 1e-6 and soc >= floor - 1e-6 and fuel >= -1e-6)
    return {"event": event, "scenario": scenario, "status": "simulated",
            "initial_soc_kwh": start_soc,
            "initial_soc_mode": "assumed precharged" if initial_soc_kwh is not None else "baseline schedule",
            "battery_max_soc_kwh": .9 * cap["battery_kwh"],
            "initial_usable_battery_kwh": initial_usable_battery_kwh,
            "battery_power_kw": cap["battery_kw"],
            "net_load_after_pv_kwh": net_load_after_pv_kwh,
            "peak_net_load_after_pv_kw": peak_net_load_after_pv_kw,
            "unserved_kwh": unserved_kwh,
            "unserved_minutes": outage_minutes, "ups_bridge_shortfall_kwh": bridge_shortfall,
            "minimum_power_margin_kw": min_margin,
            "battery_soc_end_kwh": soc, "battery_floor_kwh": floor,
            "diesel_kwh": diesel_kwh, "fuel_end_litres": fuel,
            "audit_balance_error_kw": max_balance_error, "audit_passed": passed,
            "scope": "minute bins with frozen hourly load/PV; no transient stability, control latency, or fault probability"}


def replay_catalog(pack, power_features, events=None):
    return [replay(pack, event, power_features) for event in (events or event_catalog())]
