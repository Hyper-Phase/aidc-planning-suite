"""English request constraints with explicit evidence and conflict checks."""
from __future__ import annotations

import math
import os
import re
import sys

from utils import APIAgent, parse_json

WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
         "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20, "twenty-three": 23,
         "fifty": 50}
NUMBER = r"(?:\d+(?:,\d{3})*(?:\.\d+)?|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"
AMOUNT = rf"(?:USD|\$)\s*({NUMBER})\s*(million|billion)?"
DIRECTION = r"(?:at least|minimum|no less than|not below|cannot dip below|at most|no more than|not exceed|cannot go beyond|up to|maximum|ceiling|cap|must be)"
NEGATED = re.compile(r"\b(?:is|are|was|were|does|do|will)\s+not\b|\bmust\s+not\b(?!\s+exceed)|\bnot\s+required\b|\bno\s+longer\b", re.I)


def _number(raw):
    raw = raw.strip().lower()
    return float(WORDS[raw] if raw in WORDS else raw.replace(",", ""))


def _clauses(text):
    pattern = r"[;\n]|(?<=[.!?])\s+|,\s+while\s+|,\s+(?=(?:and\s+)?(?:renewable|training|inference|cost|annual|CAPEX|budget|investment|grid|GPU|reliability|carbon))"
    return [part.strip() for part in re.split(pattern, text, flags=re.I) if part.strip()]


def _candidate(field, value, quote):
    if not math.isfinite(value):
        return False
    if field == "budget_usd":
        return value > 0 and bool(re.search(r"\b(?:capex|investment|budget)\b", quote, re.I))
    if field == "renewable_fraction":
        return 0 <= value <= 1 and bool(re.search(r"\b(?:renewable|green electricity)\b", quote, re.I))
    if field == "max_delay_h":
        return value == int(value) and 0 <= value < 24 and bool(re.search(r"\b(?:training|queueing|tasks?)\b", quote, re.I))
    if field == "premium_pct":
        return 0 <= value <= 100 and bool(re.search(r"\b(?:cost|premium)\b", quote, re.I))
    return False


def _add(candidates, issues, field, value, quote):
    if not _candidate(field, value, quote):
        issues[field] = f"Invalid or unsupported {field} value; clarify before planning."
        return
    candidates.setdefault(field, []).append({"value": value, "quote": quote})


def _parse_clause(clause, candidates, issues):
    lower = clause.lower()
    if re.search(r"\b(?:low.carbon|carbon emissions?|lifecycle cost|initial investment)\b", lower) and re.search(r"\b(?:keep|must|minimi[sz]e|cut|reduce|target)\b", lower):
        issues["unsupported_objective"] = "Requested carbon, lifecycle-cost, or initial-investment objective is not implemented."
    if re.search(r"\bcost is the priority\b", lower) and not re.search(r"\bannual cost is the priority\b", lower):
        issues["cost_scope"] = "Specify whether annual operating cost, investment, or lifecycle cost is the priority."
    if re.search(r"\b(?:reliability|availability|carbon emissions?)\b", lower) and re.search(r"\b(?:must|at least|at most|no more than|cut|reduce|target)\b|[<>]=?", lower):
        field = "reliability" if re.search(r"\b(?:reliability|availability)\b", lower) else "carbon_emissions"
        issues[field] = f"Requested {field} metric is not implemented by this planning model."
    if re.search(r"\b(?:capex|investment|budget)\b", lower):
        amounts = list(re.finditer(AMOUNT, clause, re.I))
        if amounts and NEGATED.search(clause):
            if len(amounts) > 1:
                issues["budget_usd"] = "Negated and alternative CAPEX amounts appear together; specify one ceiling."
            else:
                issues["budget_usd"] = "A negated budget amount does not specify the replacement ceiling."
            return
        if len(amounts) > 1:
            issues["budget_usd"] = "Multiple CAPEX amounts appear in one clause; specify one ceiling."
        for match in amounts:
            value = _number(match.group(1)) * {None: 1, "million": 1e6, "billion": 1e9}[match.group(2).lower() if match.group(2) else None]
            before_amount = clause[:match.start()].lower()
            if re.search(r"\b(?:at least|minimum|no less than)\b", before_amount):
                issues["budget_usd"] = "A CAPEX minimum cannot be compiled as a ceiling."
            elif not re.search(r"\b(?:capex|budget|investment)\b", before_amount):
                continue
            elif not re.search(r"\b(?:ceiling|cap|limit|budget|must not exceed|cannot go beyond|at most|no more than|maximum)\b", before_amount):
                issues["budget_usd"] = "Budget amount or ceiling direction is unclear."
            else:
                _add(candidates, issues, "budget_usd", value, clause)
        if not amounts and re.search(r"\d|\bmillion\b|\bbillion\b", lower):
            issues["budget_usd"] = "Budget amount or currency is unclear."
    if re.search(r"\b(?:renewable|green electricity)\b", lower):
        hard = bool(re.search(r"\b(?:must|at least|minimum|no less than|not below|require)\b|>=", lower))
        if hard and NEGATED.search(clause):
            issues["renewable_fraction"] = "A negated renewable requirement needs an explicit replacement or confirmation."
        if hard and not NEGATED.search(clause):
            found = re.search(rf"({NUMBER})\s*(?:%|percent)", clause, re.I)
            if found:
                _add(candidates, issues, "renewable_fraction", _number(found.group(1)) / 100, clause)
            else:
                issues["renewable_fraction"] = "Renewable requirement needs a numeric percentage."
    if re.search(r"\b(?:training|queueing delay|task delay|flexible tasks)\b", lower) and re.search(r"\b(?:delay(?:ed|ing)?|queueing|defer|postpone|completed)\b", lower):
        if "completed" in lower:
            issues["max_delay_h"] = "Completion deadline is not the model's start-delay limit."
        elif re.search(r"\b(?:cannot|can not)\s+be\s+delayed\b", lower):
            _add(candidates, issues, "max_delay_h", 0, clause)
        elif re.search(r"\b(?:at most|no more than|not exceed|up to|maximum|limit|within)\b|<=", lower):
            found = re.search(rf"({NUMBER})\s*(hours?|minutes?)\b", clause, re.I)
            if found:
                value = _number(found.group(1)) / (60 if found.group(2).lower().startswith("minute") else 1)
                _add(candidates, issues, "max_delay_h", value, clause)
            else:
                issues["max_delay_h"] = "Training delay limit needs a supported numeric duration."
    if re.search(r"\b(?:cost|premium)\b", lower) and re.search(r"\b(?:premium|within)\b", lower):
        found = re.search(r"(\d+(?:\.\d+)?)\s*%", clause)
        if found:
            _add(candidates, issues, "premium_pct", float(found.group(1)), clause)
    if re.search(r"\bdelay\b", lower) and re.search(rf"{NUMBER}\s*(?:hours?|minutes?)", lower) and not re.search(r"\b(?:training|inference|queueing|flexible tasks?)\b", lower):
        issues["max_delay_h"] = "Delay requirement does not identify the task type."
    if re.search(r"\bbudget\b", lower) and re.search(r"\b(?:not yet|undecided|pending)\b", lower):
        issues["budget_usd"] = "The budget is undecided; confirm whether planning may proceed without a CAPEX ceiling."


def _llm_candidates(text, candidates, issues):
    if "--local" in sys.argv or not os.environ.get("OPENROUTER_API_KEY", "").strip():
        return
    system = (
        "Identify only explicit planning limits in English text. Return JSON with "
        '"constraints":[{"field":"budget_usd|renewable_fraction|max_delay_h|premium_pct",'
        '"value":number,"quote":string}], where quote is an exact source substring. '
        "Use USD, fraction, hours, and percent units respectively. Ignore negated limits, "
        "goals, unsupported metrics, and ambiguous or conflicting values. Do not invent numbers."
    )
    try:
        response = parse_json(APIAgent().complete(system, text)["raw"])
    except Exception:
        return
    if not isinstance(response, dict):
        return
    for item in response.get("constraints", []):
        try:
            field, quote, value = item["field"], item["quote"], item["value"]
            if field in candidates or field in issues or not isinstance(quote, str) or quote not in text or type(value) not in (float, int):
                continue
            if NEGATED.search(quote) or not re.search(DIRECTION, quote, re.I):
                continue
            if len(re.findall(AMOUNT, quote, re.I)) > 1:
                continue
            if field == "budget_usd":
                amount = re.search(AMOUNT, quote, re.I)
                if not amount:
                    continue
                expected = _number(amount.group(1)) * {None: 1, "million": 1e6, "billion": 1e9}[amount.group(2).lower() if amount.group(2) else None]
            else:
                found = re.search(rf"({NUMBER})\s*(%|percent|hours?|minutes?)", quote, re.I)
                if not found:
                    continue
                expected = _number(found.group(1))
                unit = found.group(2).lower()
                if field == "renewable_fraction":
                    expected /= 100 if unit in ("%", "percent") else 1
                elif field == "max_delay_h" and unit.startswith("minute"):
                    expected /= 60
            if abs(float(value) - expected) > 1e-8:
                continue
            _add(candidates, issues, field, float(value), quote)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue


def analyze_constraints(text, *, use_llm=False):
    """Return accepted limits and blocking issues; never silently choose conflicts."""
    candidates, issues = {}, {}
    clauses = _clauses(text)
    for clause in clauses:
        _parse_clause(clause, candidates, issues)
    budget_mentions = []
    for clause in clauses:
        if not re.search(r"\b(?:budget|capex|investment)\b", clause, re.I) or NEGATED.search(clause):
            continue
        for match in re.finditer(AMOUNT, clause, re.I):
            budget_mentions.append(_number(match.group(1)) *
                                   {None: 1, "million": 1e6, "billion": 1e9}
                                   [match.group(2).lower() if match.group(2) else None])
    if len({round(value, 6) for value in budget_mentions}) > 1:
        issues["budget_usd"] = "Conflicting CAPEX amounts were stated; specify one ceiling."
    unresolved_cue = any(
        re.search(pattern, text, re.I) and field not in candidates and field not in issues
        for field, pattern in (
            ("budget_usd", r"\b(?:budget|capex|investment)\b"),
            ("renewable_fraction", r"\b(?:renewable|green electricity)\b"),
            ("max_delay_h", r"\b(?:training|queueing)\b.{0,60}\b(?:delay|defer|postpone)\b"),
            ("premium_pct", r"\b(?:cost premium|premium cost)\b"),
        )
    )
    if use_llm and unresolved_cue:
        _llm_candidates(text, candidates, issues)
    for clause in clauses:
        if NEGATED.search(clause):
            continue
        if re.search(r"\b(?:budget|capex|investment)\b", clause, re.I) and re.search(r"\d", clause):
            covered = any(row["quote"] in clause for row in candidates.get("budget_usd", []))
            if not covered:
                issues["budget_usd"] = "Budget amount or ceiling direction is unclear; specify one USD ceiling."
        if re.search(r"\b(?:renewable|green electricity)\b", clause, re.I) and re.search(r"\d+\s*(?:%|percent)", clause, re.I):
            covered = any(row["quote"] in clause for row in candidates.get("renewable_fraction", []))
            if not covered and "renewable_fraction" not in issues:
                issues["renewable_fraction"] = "Renewable percentage direction is unclear; specify a minimum or a preference."
        if re.search(r"\b(?:training|queueing)\b", clause, re.I) and re.search(r"\bdelay(?:ed|ing)?\b", clause, re.I) and re.search(r"\d", clause):
            covered = any(row["quote"] in clause for row in candidates.get("max_delay_h", []))
            if not covered and "max_delay_h" not in issues:
                issues["max_delay_h"] = "Training delay direction is unclear; specify a maximum."
    limits = {}
    for field, rows in candidates.items():
        distinct = {round(row["value"], 9) for row in rows}
        if len(distinct) > 1:
            issues[field] = f"Conflicting {field} values: {', '.join(str(row['value']) for row in rows)}."
        if field not in issues:
            limits[field] = rows[0]
    return limits, issues
