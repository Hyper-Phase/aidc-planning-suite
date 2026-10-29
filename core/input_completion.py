"""Shared field completion and physical preflight for online and explicit local entry."""
from __future__ import annotations
import math
import json
import re
import sys
from pathlib import Path
from location_search import lookup_location

FIELD_LABELS = {
    "gpu_count": "GPU count", "roof_m2": "usable roof area (m2)", "grid_kw": "grid import capacity (kW)",
    "backup_hours": "backup duration (hours)", "price_usd_kwh": "electricity price (USD/kWh)",
    "ambient_mean_c": "representative-day mean ambient temperature (C)", "ups_minutes": "UPS autonomy (minutes)",
    "training_share": "flexible training workload share", "growth": "workload growth multiplier",
    "pv_peak_cf": "PV peak capacity factor", "ambient_swing_c": "daily ambient temperature swing (C)",
    "burst_kw_gpu": "training power burst per GPU (kW)",
    "budget_usd": "CAPEX limit (USD)", "renewable_fraction": "minimum green electricity share",
    "max_delay_h": "maximum training delay (hours)", "premium_pct": "allowed cost premium (%)",
}



REQUIRED = {
    "gpu_count": "GPU count (multiple of 8)", "roof_m2": "usable roof area (m2)",
    "grid_kw": "grid import capacity (kW)", "backup_hours": "backup duration (hours)",
    "price_usd_kwh": "electricity price (USD/kWh)", "ambient_mean_c": "representative-day mean ambient temperature (C)",
    "ups_minutes": "required UPS autonomy (minutes; based on continuity requirements)",
    "training_share": "flexible training workload share (fraction or percent; inference share also accepted)",
    "pv_peak_cf": "site representative-day PV peak capacity factor (0 to 1; provide a weather or generation basis)",
}



INPUT_QUESTIONS = {
    "gpu_count": "GPU count (a positive multiple of 8)",
    "roof_m2": "usable roof area (m2)",
    "grid_kw": "grid import capacity (kW)",
    "backup_hours": "backup duration (hours)",
    "price_usd_kwh": "electricity price (USD/kWh)",
    "ambient_mean_c": "representative-day mean ambient temperature (C)",
    "ups_minutes": "required UPS autonomy (minutes, based on continuity needs)",
    "training_share": "flexible training workload share (fraction or percent; inference share also accepted)",
    "pv_peak_cf": "site representative-day PV peak capacity factor (0 to 1)",
}



OMITTABLE = {
    "growth": (1., "Future workload growth was not modeled; capacity may be understated."),
    "ambient_swing_c": (0., "Daily temperature variation was not modeled; peak cooling demand may be understated."),
    "burst_kw_gpu": (0., "Training power bursts were not modeled; instantaneous supply and backup needs may be understated."),
}



def valid_fact(field, value):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    if field == "gpu_count":
        return value >= 8 and value == int(value) and int(value) % 8 == 0
    if field == "ambient_mean_c":
        return -30 <= value <= 50
    if field == "training_share":
        return 0 < value <= 1
    if field == "pv_peak_cf":
        return 0 <= value <= 1
    if field == "growth":
        return value >= 1
    if field in ("ambient_swing_c", "burst_kw_gpu"):
        return value >= 0
    return value > 0


def clarify_constraint_issues(limits, issues, *, correction_parser=None, reply_interpreter=None):
    """Ask for one explicit replacement; unsupported metrics remain blocking."""
    prefixes = {
        "budget_usd": "CAPEX ceiling ",
        "renewable_fraction": "Renewable electricity must be at least ",
        "max_delay_h": "Training delay at most ",
        "premium_pct": "Cost premium within ",
    }
    for field in list(issues):
        if field not in prefixes:
            continue
        attempts = 0
        while field in issues:
            try:
                reply = input(f"{issues[field]} Provide one corrected {field} value with units, or /unknown: ").strip()
            except EOFError:
                reply = "/unknown"
            if not reply or reply.lower() in ("/unknown", "/done"):
                break
            attempts += 1
            if reply_interpreter is not None:
                candidate = reply_interpreter(field, reply)
                if candidate is not None:
                    limits[field] = candidate
                    issues.pop(field)
                    continue
            elif correction_parser is None:
                from llm_interpreter import parse_field_correction
                value = parse_field_correction(field, reply)
                if value is not None:
                    limits[field] = {"value": value, "quote": reply,
                                     "method": "field-specific numeric correction"}
                    issues.pop(field)
                    continue
            else:
                replacement, replacement_issues = correction_parser(prefixes[field] + reply)
                if field in replacement and field not in replacement_issues:
                    limits[field] = {**replacement[field], "quote": reply,
                                     "method": "explicit correction after clarification"}
                    issues.pop(field)
                    continue
            print(f"Could not verify one supported {field} value from that response.")
            if attempts >= 2:
                break
    return limits, issues


def complete_request(request, interpreted, *, local=False, fact_parser=None,
                     constraint_parser=None, conflict_parser=None, reply_interpreter=None):
    """Complete interpreted fields and apply shared physical/solver checks."""
    text = request["text"]
    facts, evidence = interpreted["facts"], interpreted["evidence"]
    goal, parser_note = interpreted["goal"], interpreted["note"]
    semantic_trace = interpreted.get("semantic_trace")
    location = interpreted["location"]
    interpreted_limits, interpreted_issues = interpreted["limits"], interpreted["issues"]
    issue_kinds = interpreted.get("issue_kinds", {})
    for item in evidence.values():
        item["status"] = "user_provided"
    research = {"location_query": location, "fields": {}, "errors": [], "tariff_candidate": None}
    if location:
        if "--point-cache" in sys.argv:
            cache_path = Path(sys.argv[sys.argv.index("--point-cache") + 1])
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached["location_query"].casefold() == location.casefold():
                research["point"] = cached["point"]
                research["errors"].append("Using explicitly selected cached coordinates; live location sources not rechecked")
            else:
                research["point"] = None
                research["errors"].append("Cached coordinates do not match the requested location")
        else:
            research = lookup_location(location, search_tariff=("price_usd_kwh" not in facts and
                                                            not local))
        for field, record in research["fields"].items():
            if field in facts:
                difference = abs(facts[field] - record["value"])
                threshold = {"ambient_mean_c": 3, "ambient_swing_c": 3, "pv_peak_cf": .15}[field]
                if difference > threshold:
                    research["errors"].append(f"{field}: user value differs from public estimate by {difference:.3g}; review before treating them as comparable.")
                continue
            facts[field] = record["value"]
            evidence[field] = {"quote": f"Public estimate for {research['point']['name']}",
                               "method": record["method"], "status": record["status"],
                               "source_url": record["source_url"], "source_type": record["source_type"],
                               "retrieved_at": record["retrieved_at"],
                               "valid_period": record["valid_period"], "location": record["location"],
                               "unit": record["unit"], "uncertainty": record["uncertainty"]}
            if (field in {"ambient_mean_c", "ambient_swing_c", "pv_peak_cf"} and
                    issue_kinds.get(field) == "missing_public_data"):
                interpreted_issues.pop(field, None)
        if research["fields"]:
            print("Public location estimates found:", ", ".join(research["fields"]))
        for issue in research["errors"]:
            print("Source note:", issue)
    for field, question in INPUT_QUESTIONS.items():
        attempts = 0
        while field not in facts or not valid_fact(field, facts[field]):
            if field in facts:
                print(f"The value for {question} is outside the supported range.")
                facts.pop(field)
                evidence.pop(field, None)
            try:
                tariff = research.get("tariff_candidate") if field == "price_usd_kwh" else None
                if tariff and tariff.get("value_usd_kwh"):
                    print(f"Public tariff lead: {tariff['source_url']}; class: {tariff['tariff_class']}. "
                          "Applicability and the project contract are unverified; no price was auto-filled.")
                reply = input(f"Please provide {question}. Enter /unknown if unavailable: ").strip()
            except EOFError:
                reply = "/unknown"
            if not reply or reply.lower() in ("/done", "/unknown"):
                return {**request, "facts": facts, "evidence": evidence, "research": research,
                        "missing": [name for name in REQUIRED if name not in facts],
                        "goal": goal, "parser_note": parser_note,
                        "limits": interpreted_limits, "clarification_issues": interpreted_issues,
                        "semantic_trace": semantic_trace}
            attempts += 1
            if fact_parser is None:
                parsed, parsed_evidence = {}, {}
            else:
                try:
                    parsed, parsed_evidence = fact_parser(reply)
                except ValueError:
                    parsed, parsed_evidence = {}, {}
            if field in parsed:
                facts[field], evidence[field] = parsed[field], parsed_evidence[field]
                evidence[field]["status"] = "user_provided"
                continue
            if reply_interpreter is not None:
                candidate = reply_interpreter(field, reply)
                if candidate is not None:
                    facts[field] = candidate["value"]
                    evidence[field] = {"quote": candidate["quote"], "method": candidate["method"],
                                       "status": "user_provided"}
                else:
                    print("Could not confirm that the reply supplies the requested field.")
                    if attempts >= 2:
                        return {**request, "facts": facts, "evidence": evidence, "research": research,
                                "missing": [name for name in REQUIRED if name not in facts],
                                "goal": goal, "parser_note": parser_note,
                                "limits": interpreted_limits, "clarification_issues": interpreted_issues,
                                "semantic_trace": semantic_trace}
                continue
            match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", reply)
            if match:
                if not local and field in ("training_share", "pv_peak_cf") and not re.fullmatch(
                    r"\s*\d+(?:\.\d+)?\s*%?\s*", reply):
                    print("Provide this share directly as a number or percent.")
                    continue
                value = float(match.group().replace(",", ""))
                if field in ("training_share", "pv_peak_cf") and "%" in reply:
                    value /= 100
                facts[field] = value
                evidence[field] = {"quote": reply, "method": "provided in response to a field-specific question", "status": "user_provided"}
            else:
                print("No numeric value was recognized. Please provide the value.")
    ignored = []
    for field, (value, explanation) in OMITTABLE.items():
        if field not in facts:
            facts[field] = value
            ignored.append({"factor": FIELD_LABELS[field], "simplified_value": value, "impact": explanation})
    if constraint_parser is None:
        limits, clarification_issues = interpreted_limits, interpreted_issues
    else:
        limits, clarification_issues = constraint_parser(text, use_llm=False)
        clarification_issues.update(conflict_parser(text))
    limits, clarification_issues = clarify_constraint_issues(
        limits, clarification_issues, correction_parser=constraint_parser,
        reply_interpreter=reply_interpreter)
    if goal == "unresolved_objective":
        clarification_issues["objective"] = (
            "The planning objective was mentioned but could not be confirmed consistently; "
            "state one explicit annual-cost or renewable objective."
        )
    elif goal in ("maximize_green", "cost_then_green"):
        clarification_issues["objective"] = (
            "The requested renewable objective is not applied by the current cost-baseline "
            "workflow; use an explicit renewable minimum with an annual-cost objective, "
            "or add a separately audited renewable-objective solve."
        )
    if "budget_usd" in limits:
        # Necessary CAPEX floor from fixed servers and minimum redundant units.
        minimum_capex = (facts["gpu_count"] / 8) * 250_000 + 2 * 90_000 + 2 * 120_000
        if limits["budget_usd"]["value"] < minimum_capex:
            clarification_issues["budget_usd"] = (
                f"CAPEX ceiling is below the necessary equipment lower bound "
                f"of USD {minimum_capex:,.0f}; revise the ceiling or workload."
            )
            limits.pop("budget_usd")
    assumptions = []
    if "budget_usd" not in limits:
        assumptions.append("No CAPEX ceiling was supplied; the model uses a nonbinding numerical upper bound, not a client budget.")
    if "renewable_fraction" not in limits:
        assumptions.append("No minimum green electricity share was supplied; its achieved value is determined by optimization.")
    if "max_delay_h" not in limits:
        assumptions.append("No maximum training delay was supplied; scheduling may use the 24-hour representative day.")
    return {**request, "facts": facts, "evidence": evidence, "research": research, "goal": goal,
            "parser_note": parser_note, "limits": limits, "ignored": ignored,
            "assumptions": assumptions, "missing": [],
            "clarification_issues": clarification_issues, "semantic_trace": semantic_trace}



