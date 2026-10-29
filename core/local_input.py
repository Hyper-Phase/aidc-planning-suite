"""Legacy English rule parser, used only by explicit --local runs and historical probes."""
from __future__ import annotations
import math
import json
import os
import re
import sys
from utils import APIAgent, parse_json
from constraint_input import analyze_constraints
from input_completion import REQUIRED, OMITTABLE, complete_request

def facts_from(text):
    return extract_inputs(text)


def limits_from(text):
    """Return safe explicit bounds; use analyze_constraints for blocking issues."""
    return analyze_constraints(text)[0]



def goal_from(text):
    text = re.sub(r"\b(?:do\s+not|don't|should\s+not|must\s+not|avoid)\s+"
                  r"(?:maximi[sz]e|prioriti[sz]e|favor|favour)\b[^.;,]*", "", text, flags=re.I)
    cost = bool(re.search(r"cost\s*first|lowest\s*(?:price|cost)\s*first|prioriti[sz]e\s+(?:the\s+)?(?:lowest\s*)?(?:annual\s*)?cost|annual\s+cost\s+is\s+the\s+priority", text, re.I))
    green = bool(re.search(r"maximi[sz]e\s+(?:the\s+)?(?:green|renewable)|minimi[sz]e\s+nonrenewable\s+electricity", text, re.I))
    if cost and green:
        return "cost_then_green"
    if green:
        return "maximize_green"
    if cost:
        return "minimum_annual_cost"
    return "cost_green_bargaining"


PREFERENCE_CUE = re.compile(
    r"\b(?:prefer|favor|favour|choose|select|selected|priority|prioritize|prioritise|"
    r"goal|objective|focus|optimize|optimise|maximize|maximise|minimize|minimise|"
    r"highest|lowest|largest|smallest|best|primary|desirable|push|"
    r"as\s+much\s+as\s+possible|as\s+little\s+as\s+possible|"
    r"as\s+high\s+as|as\s+low\s+as)\b", re.I
)


def verify_llm_goal(text, quote, proposed):
    """Classify a proposed goal independently before accepting or ignoring it."""
    if proposed == "cost":
        # The solver minimizes annual cost, not upfront construction cost. A
        # model's semantic agreement cannot establish the accounting period.
        if re.search(r"\b(?:upfront|up-front|initial|capital|capex|construction|investment|one-time)\b", quote, re.I):
            return "unresolved"
        if not re.search(r"\b(?:annual|annually|yearly|year|per\s+year|each\s+year|operating|recurring)\b", quote, re.I):
            return "unresolved"
    system = (
        "Independently check a proposed AIDC optimization objective against the user text. "
        "A hard numerical constraint, a topic mention, and a negated request are not objectives. "
        "Treat annual spending, renewable electricity share, and an explicit ordering of both "
        "as different objectives. Check the full text for contradictions. Return JSON only: "
        '{"classification":"objective|hard_constraint|ambiguous|topic_only",'
        '"explicit":boolean,"conflict":boolean,'
        '"priority":"cost_then_green|green|cost|unspecified"}. '
        "Use hard_constraint for a threshold such as 'at least 50%' without an optimization request. "
        "Set explicit=true only if the quoted words themselves direct the proposed objective."
    )
    try:
        reply = APIAgent().complete(system, json.dumps({"text": text, "quote": quote,
                                                         "proposed_priority": proposed}))
        obj = parse_json(reply["raw"])
        if not isinstance(obj, dict) or obj.get("conflict") is not False:
            return "unresolved"
        if obj.get("classification") == "hard_constraint":
            return "hard_constraint"
        if (obj.get("classification") == "objective" and obj.get("explicit") is True and
                obj.get("priority") == proposed):
            return "confirmed"
        return "unresolved"
    except Exception:
        return "unresolved"



def semantic_with_api(text, facts, evidence, goal):
    """Accept only quoted numerical facts and a quoted priority from the model."""
    if "--local" in sys.argv or not os.environ.get("OPENROUTER_API_KEY", "").strip():
        return goal, "Local rule parser"
    system = (
        "Extract AIDC client facts and priority from the user's text. Return JSON only: "
        '{"facts":[{"field":str,"value":number,"quote":str}],'
        '"priority":"cost_then_green|green|cost|unspecified","priority_quote":str,'
        '"objective_present":boolean}. '
        "Allowed fact fields: gpu_count, roof_m2, grid_kw, backup_hours, price_usd_kwh, "
        "ambient_mean_c, ups_minutes, training_share, growth, pv_peak_cf, ambient_swing_c, burst_kw_gpu. "
        "Units are respectively count, m2, kW, hours, USD/kWh, Celsius, minutes, fraction, "
        "multiplier, fraction, Celsius, kW/GPU. Infer only explicit arithmetic complements such as "
        "training_share=1-inference_share. Every quote must be an exact substring of the input. "
        "Do not invent missing facts or turn optimization goals into fixed ratios."
    )
    try:
        reply = APIAgent().complete(system, text)
        obj = parse_json(reply["raw"])
        for item in obj.get("facts", []):
            field, quote, value = item["field"], item["quote"], item["value"]
            if field not in set(REQUIRED) | set(OMITTABLE) or field in facts:
                continue
            if not isinstance(quote, str) or quote not in text or type(value) not in (float, int):
                continue
            numbers = [float(x.replace(",", "")) for x in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", quote)]
            direct = any(abs(float(value) - x) < 1e-8 for x in numbers)
            complement = (field == "training_share" and len(numbers) == 1 and
                          abs(float(value) - (1 - numbers[0] / 100)) < 1e-8 and
                          re.search(r"inference", quote, re.I))
            if not math.isfinite(float(value)) or not (direct or complement):
                continue
            # A matching number alone does not prove it describes this field.
            # Require the finite parser to verify both the field and its value.
            quoted_values, _ = facts_from(quote)
            if field not in quoted_values or abs(float(value) - quoted_values[field]) > 1e-8:
                continue
            facts[field] = float(value)
            evidence[field] = {"quote": quote, "method": "LLM extraction; field and value checked by local rules"}
        preference = obj.get("priority")
        priority_quote = obj.get("priority_quote")
        target = {"cost_then_green": "cost_then_green", "green": "maximize_green",
                  "cost": "minimum_annual_cost"}.get(preference)
        note = f"OpenRouter semantic parser ({reply['model']}); only source-supported fields accepted"
        # Existing unsupported or contradictory requirements must be resolved
        # before a model's interpretation of a nearby objective is trusted.
        source_issues = analyze_constraints(text, use_llm=False)[1]
        if source_issues or conflicting_facts_from(text):
            return goal, note + "; objective interpretation deferred until input issues are resolved"
        if goal == "cost_green_bargaining" and not PREFERENCE_CUE.search(text):
            return goal, note + "; no explicit preference language to ground an objective"
        if target and (not isinstance(priority_quote, str) or not priority_quote or priority_quote not in text):
            return "unresolved_objective", note + "; objective quote missing"
        if target and goal != "cost_green_bargaining" and goal != target:
            return "unresolved_objective", note + "; conflicting objective readings"
        if target and goal == "cost_green_bargaining":
            verification = verify_llm_goal(text, priority_quote, preference)
            if verification == "confirmed":
                return target, note + "; objective confirmed by a second LLM call"
            if verification == "hard_constraint":
                return goal, note + "; numerical constraint rejected as an objective"
            return "unresolved_objective", note + "; objective could not be confirmed"
        if goal == "cost_green_bargaining" and obj.get("objective_present") is True:
            return "unresolved_objective", note + "; objective type is unclear"
        return goal, note
    except Exception as exc:
        return goal, f"Online semantic parsing failed; using local rules ({type(exc).__name__})"



def location_from(text):
    match = re.search(r"\b(?:Location|Site location)\s*:\s*([^.!?;\n]+)", text, re.I)
    if not match:
        match = re.search(r"\b(?:site in|located in)\s+([A-Za-z][A-Za-z .'-]+,\s*[A-Za-z .'-]+)(?=[.!?;])", text, re.I)
    return match.group(1).strip().rstrip(",") if match else None


NUMBER = r"(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)"



PATTERNS = {
    "gpu_count": [rf"\bGPU\s*count\s*(?:is|:|=)?\s*{NUMBER}", rf"{NUMBER}\s*GPUs?\b"],
    "roof_m2": [rf"\b(?:usable\s+)?roof\s+area\s*(?:is|:|=)?\s*{NUMBER}\s*m(?:2|²)\b",
                rf"{NUMBER}\s*m(?:2|²)\s+(?:usable\s+)?roof\b"],
    "grid_kw": [rf"\bgrid\s+(?:import\s+)?capacity\s*(?:is|:|=)?\s*{NUMBER}\s*kW\b",
                rf"{NUMBER}\s*kW\s+grid\s+(?:import\s+)?capacity\b"],
    "backup_hours": [rf"\bbackup\s+duration\s*(?:is|:|=)?\s*{NUMBER}\s*hours?\b",
                     rf"{NUMBER}\s*hours?\s+backup\b"],
    "ups_minutes": [rf"\bUPS\s+duration\s*(?:is|:|=)?\s*{NUMBER}\s*minutes?\b",
                    rf"{NUMBER}\s*minutes?\s+UPS\b"],
    "training_share": [rf"\b(?:flexible\s+)?training\s+(?:workload\s+)?share\s*(?:is|:|=)?\s*{NUMBER}\s*%?",
                       rf"{NUMBER}\s*%\s+(?:flexible\s+)?training\s+(?:workload|share)\b"],
    "growth": [rf"\b(?:workload\s+)?growth\s+multiplier\s*(?:is|:|=)?\s*{NUMBER}\b"],
    "pv_peak_cf": [rf"\bPV\s+peak\s+(?:capacity\s+)?factor\s*(?:is|:|=)?\s*{NUMBER}\b"],
    "price_usd_kwh": [rf"\b(?:flat\s+)?electricity\s+price\s*(?:is|:|=)?\s*(?:USD|\$)\s*{NUMBER}\s*/\s*kWh\b"],
    "ambient_mean_c": [rf"\b(?:average\s+)?ambient\s+temperature\s*(?:is|:|=)?\s*{NUMBER}\s*(?:C|°C)\b"],
    "ambient_swing_c": [rf"\bambient\s+(?:daily\s+)?swing\s*(?:is|:|=)?\s*{NUMBER}\s*(?:C|°C)\b"],
    "burst_kw_gpu": [rf"\btraining\s+burst\s*(?:is|:|=)?\s*{NUMBER}\s*kW\s+per\s+GPU\b"],
}



QUESTIONS = {
    "gpu_count": "GPU count (a positive multiple of 8)",
    "roof_m2": "usable roof area (m2)",
    "grid_kw": "grid import capacity (kW)",
    "backup_hours": "backup duration (hours)",
    "ups_minutes": "UPS autonomy (minutes)",
    "training_share": "flexible training workload share (fraction or percent)",
    "growth": "workload growth multiplier (at least 1)",
    "pv_peak_cf": "representative-day PV peak capacity factor (0 to 1)",
    "price_usd_kwh": "representative-day electricity price (USD/kWh)",
    "ambient_mean_c": "representative-day mean ambient temperature (C)",
    "ambient_swing_c": "representative-day ambient temperature swing (C)",
    "burst_kw_gpu": "training power burst per GPU (kW; enter 0 if confirmed absent)",
}



def _value(field: str, raw: str | float) -> float | int:
    value = float(str(raw).replace(",", ""))
    if field in {"gpu_count"}:
        if not value.is_integer() or int(value) < 8 or int(value) % 8:
            raise ValueError("GPU count must be a positive multiple of 8")
        return int(value)
    if field == "training_share" and value > 1:
        value /= 100
    if field == "training_share" and not 0 < value <= 1:
        raise ValueError("Training workload share must be greater than 0 and at most 1")
    if field == "pv_peak_cf" and not 0 <= value <= 1:
        raise ValueError("PV peak capacity factor must be between 0 and 1")
    if field == "growth" and value < 1:
        raise ValueError("Growth multiplier must be at least 1")
    if field in {"ambient_swing_c", "burst_kw_gpu"} and value < 0:
        raise ValueError(f"{QUESTIONS[field]} cannot be negative")
    if field not in {"training_share", "pv_peak_cf", "growth", "ambient_mean_c", "ambient_swing_c", "burst_kw_gpu"} and value <= 0:
        raise ValueError(f"{QUESTIONS[field]} must be greater than 0")
    return value



def extract_inputs(text: str) -> tuple[dict, dict]:
    """Return values and their literal evidence; infer only arithmetic complements."""
    values, evidence = {}, {}
    for field, patterns in PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                try:
                    values[field] = _value(field, match.group("value"))
                except ValueError:
                    continue
                evidence[field] = {"quote": match.group(), "method": "explicitly stated in the input"}
                break
    match = re.search(rf"\binference\s+(?:workload\s+)?share\s*(?:is|:|=)?\s*{NUMBER}\s*%", text, re.I)
    if match:
        try:
            inferred_share = _value("training_share", 100 - float(match.group("value")))
        except ValueError:
            values.pop("training_share", None)
            evidence.pop("training_share", None)
        else:
            if "training_share" in values and abs(values["training_share"] - inferred_share) > 1e-9:
                values.pop("training_share")
                evidence.pop("training_share")
            elif "training_share" not in values:
                values["training_share"] = inferred_share
                evidence["training_share"] = {"quote": match.group(), "method": "derived as one minus the stated inference workload share"}
    if "growth" not in values:
        match = re.search(rf"\bworkload\s+(?:grows?|increases?)\s+by\s+{NUMBER}\s*%", text, re.I)
        if match:
            values["growth"] = _value("growth", 1 + float(match.group("value")) / 100)
            evidence["growth"] = {"quote": match.group(), "method": "converted from the stated growth percentage"}
    if "burst_kw_gpu" not in values:
        match = re.search(r"\bno\s+training\s+burst\b", text, re.I)
        if match:
            values["burst_kw_gpu"] = 0.0
            evidence["burst_kw_gpu"] = {"quote": match.group(), "method": "explicit statement of no training burst, represented as zero"}
    return values, evidence


def conflicting_facts_from(text):
    """Block multiple incompatible values for the same site or workload fact."""
    issues = {}
    for field, patterns in PATTERNS.items():
        values = []
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                try:
                    values.append(float(_value(field, match.group("value"))))
                except ValueError:
                    continue
        if len({round(value, 8) for value in values}) > 1:
            issues[field] = f"Conflicting {field} values were stated; specify one value."
    extra_grid = re.findall(r"\bgrid\s+interconnection\s+capacity\s+to\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(MW|kW)\b", text, re.I)
    if extra_grid:
        stated = [float(raw.replace(",", "")) * (1000 if unit.lower() == "mw" else 1)
                  for raw, unit in extra_grid]
        parsed, _ = extract_inputs(text)
        if "grid_kw" in parsed:
            stated.append(float(parsed["grid_kw"]))
        if len({round(value, 8) for value in stated}) > 1:
            issues["grid_kw"] = "Conflicting grid_kw values were stated; specify one site capacity."
    return issues



def complete_local_information(request):
    """Reproduce the historical offline text entry without the online interpreter."""
    text = request["text"]
    facts, evidence = facts_from(text)
    interpreted = {"facts": facts, "evidence": evidence,
                   "goal": goal_from(text), "note": "Local rule parser",
                   "location": location_from(text), "limits": {}, "issues": {}}
    return complete_request(request, interpreted, local=True,
                            fact_parser=facts_from,
                            constraint_parser=analyze_constraints,
                            conflict_parser=conflicting_facts_from)
