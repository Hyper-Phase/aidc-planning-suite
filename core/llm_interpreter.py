"""LLM-first English interpretation with mechanical evidence and domain checks.

This module does not decide meaning from keyword lists. The model proposes the
meaning; code checks source spans, arithmetic, allowed units, ranges, and
conflicts before a proposal can enter the planner.
"""
from __future__ import annotations

import math
import re

from utils import APIAgent, parse_json
from domain_schema import SCHEMA_TEXT
from semantic_review import review

FACT_FIELDS = {"gpu_count", "roof_m2", "grid_kw", "backup_hours", "ups_minutes",
               "training_share", "growth", "pv_peak_cf", "price_usd_kwh",
               "ambient_mean_c", "ambient_swing_c", "burst_kw_gpu"}
LIMIT_OPERATORS = {"budget_usd": "max", "renewable_fraction": "min",
                   "max_delay_h": "max", "premium_pct": "max"}
GOALS = {"minimum_annual_cost", "maximize_green", "cost_then_green",
         "unspecified", "unsupported", "ambiguous"}
ISSUE_KINDS = {"missing_public_data", "missing_user_data", "conflict",
               "unsupported", "ambiguous", "invalid"}
WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
         "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20,
         "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}


def _numbers(quote):
    out = [float(raw.replace(",", "")) for raw in
           re.findall(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?", quote)]
    out += [float(WORDS[word]) for word in re.findall(r"\b[a-z]+\b", quote.lower())
            if word in WORDS]
    return out


def _supported_value(field, value, quote, derivation="direct"):
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return False
    nums = _numbers(quote)
    if not nums:
        # The LLM reviewer must establish the meaning of a nonnumeric zero claim.
        return field in {"burst_kw_gpu", "max_delay_h"} and float(value) == 0
    lower = quote.lower()
    candidates = list(nums)
    if "%" in quote or "percent" in lower:
        candidates += [x / 100 for x in nums]
        if field == "training_share" and derivation == "one_minus_fraction":
            candidates += [1 - x / 100 for x in nums]
    if "million" in lower:
        candidates += [x * 1e6 for x in nums]
    if "billion" in lower:
        candidates += [x * 1e9 for x in nums]
    if field == "grid_kw" and re.search(r"\bMW\b", quote):
        candidates += [x * 1000 for x in nums]
    if field in {"max_delay_h", "backup_hours"} and "minute" in lower:
        candidates += [x / 60 for x in nums]
    if field == "growth" and derivation == "one_plus_fraction" and ("%" in quote or "percent" in lower):
        candidates += [1 + x / 100 for x in nums]
    return any(abs(float(value) - x) <= max(1e-8, abs(x) * 1e-9) for x in candidates)


def _range_ok(field, value):
    if field == "gpu_count":
        return value >= 8 and value == int(value) and int(value) % 8 == 0
    if field == "training_share":
        return 0 < value <= 1
    if field in {"pv_peak_cf", "renewable_fraction"}:
        return 0 <= value <= 1
    if field == "ambient_mean_c":
        return -30 <= value <= 50
    if field == "growth":
        return value >= 1
    if field in {"ambient_swing_c", "burst_kw_gpu"}:
        return value >= 0
    if field == "max_delay_h":
        return value == int(value) and 0 <= value < 24
    if field == "premium_pct":
        return 0 <= value <= 100
    return value > 0


def parse_field_correction(field, reply):
    """Parse a field-specific answer using numbers and units only."""
    nums = _numbers(reply)
    if len(nums) != 1:
        return None
    value = nums[0]
    lower = reply.lower()
    if field == "budget_usd":
        if "USD" not in reply.upper() and "$" not in reply:
            return None
        if "million" in lower:
            value *= 1e6
        elif "billion" in lower:
            value *= 1e9
    elif field == "renewable_fraction" and ("%" in reply or "percent" in lower):
        value /= 100
    elif field == "max_delay_h" and "minute" in lower:
        value /= 60
    return value if field in LIMIT_OPERATORS and _range_ok(field, value) else None


def interpret_field_reply(field, reply):
    """Interpret one online clarification in its stated field context with the LLM."""
    from domain_schema import FACTS, LIMITS
    definition = FACTS.get(field) or LIMITS.get(field)
    if definition is None:
        return None
    system = (
        "Interpret the user's English reply to a question about one planning field. "
        "The field context identifies what was asked, but do not invent a value, currency, "
        "comparison direction, or scope that the reply and field question do not support. "
        "A bare number may answer the explicitly requested field using the unit in its definition. "
        "For a budget in USD, the reply itself must specify USD or $. "
        "Return JSON only with field, value, quote, derivation, operator, accept, reason. "
        "quote must be an exact substring of the reply; accept must be a boolean. "
        "For a limit, operator must match the definition.\n" + field + ": " + definition
    )
    response = APIAgent().complete(system, reply)
    try:
        obj = parse_json(response["raw"])
        if (not isinstance(obj, dict) or obj.get("field") != field or
                obj.get("accept") is not True or not _quote(reply, obj)):
            return None
        value = obj.get("value")
        if field in LIMITS and obj.get("operator") != LIMIT_OPERATORS[field]:
            return None
        if not _supported_value(field, value, obj["quote"], obj.get("derivation", "direct")):
            return None
        if not _range_ok(field, value):
            return None
        return {"value": float(value), "quote": obj["quote"],
                "method": "LLM interpretation of a field-specific reply"}
    except (ValueError, TypeError, KeyError):
        return None


def _quote(text, item):
    quote = item.get("quote")
    return quote if isinstance(quote, str) and quote and quote in text else None


def _accept_unique(rows, allowed, text, issues, *, operators=None):
    accepted = {}
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        field, value, quote = item.get("field"), item.get("value"), _quote(text, item)
        if field not in allowed:
            continue
        if quote is None:
            issues[field] = f"The proposed {field} field lacks an exact source quote."
            continue
        if operators and item.get("operator") != operators[field]:
            issues[field] = f"Unsupported direction for {field}; clarify the intended bound."
            continue
        if not _supported_value(field, value, quote, item.get("derivation", "direct")) or not _range_ok(field, value):
            issues[field] = f"The proposed {field} value lacks numeric source support or is outside the model range."
            continue
        accepted.setdefault(field, []).append({"value": float(value), "quote": quote,
                                                "method": "LLM interpretation with source and numeric checks"})
    resolved = {}
    for field, values in accepted.items():
        if len({round(item["value"], 8) for item in values}) > 1:
            issues[field] = f"Conflicting {field} values were stated; specify one value."
        elif field not in issues:
            resolved[field] = values[0]
    return resolved


def interpret(text):
    """Return one structured interpretation; errors become explicit input issues."""
    system = (
        "Interpret the English AIDC planning request, not a keyword template. Return JSON only with "
        '{"facts":[{"field":string,"value":number,"quote":string,'
        '"derivation":"direct|one_minus_fraction|one_plus_fraction|explicit_zero"}],'
        '"limits":[{"field":string,"operator":"min|max","value":number,"quote":string}],'
        '"objective":{"type":"minimum_annual_cost|maximize_green|cost_then_green|unspecified|unsupported|ambiguous",'
        '"quote":string},"site":{"name":string,"quote":string},'
        '"claims":[{"id":string,"quote":string,"meaning":string,"status":"explicit|derived|ambiguous|unsupported"}],'
        '"issues":[{"field":string,"kind":"missing_public_data|missing_user_data|conflict|unsupported|ambiguous|invalid",'
        '"required_by_user":boolean,"reason":string,"quote":string}]}. '
        "Every quote must be an exact substring of the user text. Return all explicitly stated values, "
        "including conflicting alternatives. Allowed fact fields: " + ", ".join(sorted(FACT_FIELDS)) + ". "
        "Allowed limits: budget_usd (maximum CAPEX USD), renewable_fraction (minimum delivered "
        "renewable electricity fraction), max_delay_h (maximum training start delay in hours), "
        "premium_pct (maximum annual-cost premium percent). Normalize units to the field names. "
        "In this model, workload has only training and inference categories: if the text explicitly "
        "states the inference share, infer training_share as one minus that share, cite the inference "
        "phrase, set derivation=one_minus_fraction, and do not also call training_share missing. "
        "An explicit statement that there is no training power burst means burst_kw_gpu=0; cite it. "
        "Do not turn a hard numerical bound into an optimization objective. Distinguish annual cost "
        "from upfront investment, emissions, and lifecycle cost. Unsupported or ambiguous requests "
        "belong in issues. If a named site has unknown weather or solar resource, label those issues "
        "missing_public_data for ambient_mean_c, ambient_swing_c, or pv_peak_cf; a later public-source "
        "lookup may fill them. Do not use missing_public_data for conflicts, invalid values, or project "
        "specific facts such as contract price. growth, ambient_swing_c, and burst_kw_gpu are optional "
        "model inputs with documented defaults. Do not create issues just because an optional value "
        "is absent. State atomic claims before mapping them to facts and limits. A candidate "
        "must preserve the complete meaning of its source claim. An unstated currency, comparison "
        "direction, constrained workload type, or time event cannot be inferred. A completion "
        "deadline is not a training start delay. An explicit prohibition on training delay may "
        "be normalized to zero with derivation=explicit_zero. Do not demand an objective merely "
        "because none was stated. For every missing-data issue, set required_by_user=true only if the user explicitly "
        "requires that particular value before planning; otherwise set false. If a fact or bound is "
        "unstated, omit it; never invent site data.\nDOMAIN DEFINITIONS:\n" + SCHEMA_TEXT
    )
    response = None
    try:
        response = APIAgent().complete(system, text)
        obj = parse_json(response["raw"])
        if not isinstance(obj, dict):
            raise ValueError("structured response is not an object")
    except Exception as exc:
        return {"facts": {}, "evidence": {}, "limits": {}, "goal": "unresolved_objective",
                "location": None, "issues": {"llm": f"LLM interpretation unavailable: {type(exc).__name__}"},
                "issue_kinds": {},
                "note": f"LLM-first interpretation failed: {type(exc).__name__}",
                "semantic_trace": {"extract_error": str(exc),
                                   "extraction_raw": response.get("raw") if response else None}}
    issues = {}
    issue_kinds = {}
    facts = _accept_unique(obj.get("facts"), FACT_FIELDS, text, issues)
    limits = _accept_unique(obj.get("limits"), LIMIT_OPERATORS, text, issues,
                            operators=LIMIT_OPERATORS)
    evidence = {field: {"quote": item["quote"], "method": item["method"]}
                for field, item in facts.items()}
    facts = {field: item["value"] for field, item in facts.items()}
    for item in obj.get("issues", []) if isinstance(obj.get("issues"), list) else []:
        if isinstance(item, dict) and _quote(text, item):
            field = str(item.get("field") or "request")
            kind = item.get("kind") if item.get("kind") in ISSUE_KINDS else "unknown"
            if (field in {"growth", "ambient_swing_c", "burst_kw_gpu"} and
                    kind in {"missing_public_data", "missing_user_data"} and
                    item.get("required_by_user") is False):
                continue
            if field in facts and kind in {"missing_public_data", "missing_user_data"}:
                continue
            if field not in issues:
                issues[field] = str(item.get("reason") or "Clarification required.")
                issue_kinds[field] = kind
    objective = obj.get("objective") if isinstance(obj.get("objective"), dict) else {}
    goal = objective.get("type") if objective.get("type") in GOALS else "ambiguous"
    if goal in {"unsupported", "ambiguous"}:
        issues["objective"] = "The requested objective is unsupported or ambiguous; clarify before planning."
        goal = "unresolved_objective"
    elif goal != "unspecified" and not _quote(text, objective):
        issues["objective"] = "The proposed objective lacks an exact source quote."
        goal = "unresolved_objective"
    elif goal == "unspecified":
        goal = "cost_green_bargaining"
    site = obj.get("site") if isinstance(obj.get("site"), dict) else {}
    location = (site.get("name") if _quote(text, site) and
                isinstance(site.get("name"), str) and
                site["name"].casefold() in site["quote"].casefold() else None)
    proposal = {"facts": {field: {"value": value, "quote": evidence[field]["quote"]}
                          for field, value in facts.items()},
                "limits": limits, "objective": {"type": objective.get("type"), "quote": objective.get("quote")},
                "claims": obj.get("claims", [])}
    try:
        verdicts, review_trace = review(text, proposal)
    except Exception as exc:
        issues["llm"] = f"Semantic review unavailable: {type(exc).__name__}: {exc}"
        return {"facts": {}, "evidence": {}, "limits": {}, "goal": "unresolved_objective",
                "location": location, "issues": issues, "issue_kinds": issue_kinds,
                "note": "LLM semantic review failed; nothing accepted",
                "semantic_trace": {"extraction_raw": response["raw"], "review_error": str(exc)}}
    for field in list(facts):
        verdict = verdicts[("facts", field)]
        if not verdict["accept"]:
            facts.pop(field)
            evidence.pop(field, None)
            issues[field] = f"Semantic review rejected {field}: {verdict['reason']}"
            issue_kinds[field] = "ambiguous"
    for field in list(limits):
        verdict = verdicts[("limits", field)]
        if not verdict["accept"]:
            limits.pop(field)
            issues[field] = f"Semantic review rejected {field}: {verdict['reason']}"
            issue_kinds[field] = "ambiguous"
    if not verdicts[("objective", "goal")]["accept"]:
        goal = "unresolved_objective"
        issues["objective"] = f"Semantic review rejected objective: {verdicts[('objective', 'goal')]['reason']}"
        issue_kinds["objective"] = "ambiguous"
    return {"facts": facts, "evidence": evidence, "limits": limits, "goal": goal,
            "location": location, "issues": issues, "issue_kinds": issue_kinds,
            "note": f"LLM-first interpretation and independent semantic review ({response['model']})",
            "semantic_trace": {"extraction_raw": response["raw"], "review_raw": review_trace["raw"],
                               "model": response["model"],
                               "extraction_seconds": response.get("seconds"),
                               "review_seconds": review_trace.get("seconds"),
                               "extraction_usage": response.get("usage"),
                               "review_usage": review_trace.get("usage")}}
