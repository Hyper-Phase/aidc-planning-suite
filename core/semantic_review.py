"""LLM review of complete source-to-schema relations, not keyword matching."""
from __future__ import annotations

import json

from domain_schema import SCHEMA_TEXT
from utils import APIAgent, parse_json


def review(text, proposal, *, agent=None):
    """Return field verdicts and trace; malformed or missing verdicts fail closed."""
    system = (
        "You are an independent semantic reviewer of an English AIDC request. "
        "Decide whether the ORIGINAL TEXT entails each proposed complete schema meaning, "
        "including entity, metric, currency, comparison direction, time event, negation, "
        "and objective scope. A quote containing the number alone is insufficient. "
        "Do not infer missing details from typical engineering practice, field names, "
        "public estimates, or the candidate itself. Apply only the explicit scope conventions "
        "stated in the domain definitions below. A stated but unqualified cost priority is "
        "ambiguous; an entirely unstated objective is different. The model's task taxonomy "
        "cannot supply a missing target for a bare delay statement. "
        "Return JSON only: {\"facts\":[{\"field\":string,\"accept\":boolean,\"reason\":string}],"
        "\"limits\":[{\"field\":string,\"accept\":boolean,\"reason\":string}],"
        "\"objective\":{\"accept\":boolean,\"reason\":string}}. "
        "Give exactly one verdict for every proposed fact and limit. The objective verdict "
        "must be present; accept unspecified if the user states no explicit objective. "
        "Reject a candidate when meaning is ambiguous or unsupported. "
        "Do not generate replacement values.\n" + SCHEMA_TEXT
    )
    user = json.dumps({"original_text": text, "proposal": proposal}, ensure_ascii=False)
    response = (agent or APIAgent()).complete(system, user)
    try:
        result = parse_json(response["raw"])
        if not isinstance(result, dict):
            raise ValueError("review is not an object")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Malformed semantic review: {exc}") from exc
    verdicts = {}
    for section in ("facts", "limits"):
        rows = result.get(section)
        if not isinstance(rows, list):
            raise ValueError(f"Missing {section} review")
        names = list(proposal.get(section, {}))
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Invalid {section} review row")
            if row.get("field") not in names:
                # An unsolicited verdict cannot admit a field absent from the proposal.
                continue
            if type(row.get("accept")) is not bool:
                raise ValueError(f"Invalid {section} review row")
            key = (section, row["field"])
            if key in verdicts:
                raise ValueError(f"Duplicate {section} review row")
            verdicts[key] = {"accept": row["accept"], "reason": str(row.get("reason") or "No reason supplied")}
        if {field for part, field in verdicts if part == section} != set(names):
            raise ValueError(f"Incomplete {section} review")
    objective = result.get("objective")
    if not isinstance(objective, dict) or type(objective.get("accept")) is not bool:
        raise ValueError("Missing objective review")
    verdicts[("objective", "goal")] = {"accept": objective["accept"],
                                        "reason": str(objective.get("reason") or "No reason supplied")}
    return verdicts, {"model": response["model"], "raw": response["raw"],
                      "seconds": response.get("seconds"), "usage": response.get("usage")}
