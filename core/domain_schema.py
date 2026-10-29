"""Domain meanings for the online interpreter; no input phrases or examples."""

FACTS = {
    "gpu_count": "Installed GPU count, not an energy or power quantity.",
    "roof_m2": "Usable site roof area in square metres; the planner uses this quantity as the roof bound for photovoltaic installation. The user need only state usable roof area.",
    "grid_kw": "Maximum grid import capacity at the site, in kW; not electricity consumption.",
    "backup_hours": "Required duration of backup supply during a grid outage, in hours.",
    "ups_minutes": "Required UPS autonomy during a power interruption, in minutes.",
    "training_share": "Fraction of arriving workload that is training. The model has only training and immediate inference workload categories. Training is the only potentially flexible workload category. Training may be constrained to start without delay; this does not change its workload category. This fraction also participates in the model's daily arrival-shape mixture.",
    "growth": "Multiplier on workload demand relative to the current workload.",
    "pv_peak_cf": "Site photovoltaic peak capacity factor, a fraction; the planner uses it as a representative-day proxy. It is not annual renewable electricity share. The user need not specify the proxy time basis.",
    "price_usd_kwh": "Project electricity purchase price in USD per kWh; public tariff leads are not a project contract.",
    "ambient_mean_c": "Site mean ambient temperature in degrees Celsius; the planner uses it as a representative-day proxy. The user need not specify the proxy time basis.",
    "ambient_swing_c": "Magnitude of the site representative-day ambient temperature variation in degrees Celsius.",
    "burst_kw_gpu": "Additional instantaneous training power burst per GPU in kW.",
}

LIMITS = {
    "budget_usd": "Maximum upfront capital expenditure in USD. A project budget cap in a site-design request may denote this capital ceiling when its currency is explicit and no recurring or operating period is stated. An annual or operating budget is a different quantity; an unstated currency cannot be assumed.",
    "renewable_fraction": "Minimum fraction of delivered electricity from renewable sources. A share without a minimum requirement is not this bound.",
    "max_delay_h": "Maximum allowed time between a training job's arrival and its start, in whole hours. Training is the model's only potentially flexible task category. A delay bound may map here when the user identifies the constrained process as training or flexible workload, with no contrary task class. A delay statement that does not identify its constrained process remains ambiguous; the model taxonomy alone cannot supply that missing link. A completion deadline is a different requirement. Inference is processed on arrival and has no delay decision; a no-delay requirement for inference is already satisfied by the model.",
    "premium_pct": "Maximum permitted percentage premium on annual cost compared with the annual-cost baseline.",
}

GOALS = {
    "minimum_annual_cost": "Minimize the planner's total modeled annual cost, including operating and annualized investment costs. A general cost preference with an explicitly annual period may map to this standard objective unless the text narrows the cost scope. An explicit cost preference with no period is ambiguous; an entirely unstated objective uses the documented default instead.",
    "maximize_green": "Maximize renewable electricity share; recognized but not implemented by the current cost-baseline solver.",
    "cost_then_green": "Lexicographically minimize annual cost and then maximize renewable electricity share; recognized but not implemented by the current solver.",
    "unspecified": "No explicit optimization objective; the documented annual-cost baseline applies.",
    "unsupported": "An explicit objective that the current solver cannot represent.",
    "ambiguous": "The stated objective cannot be resolved without clarification.",
}

SCHEMA_TEXT = "\n".join(
    ["FACTS:"] + [f"{key}: {value}" for key, value in FACTS.items()]
    + ["LIMITS:"] + [f"{key}: {value}" for key, value in LIMITS.items()]
    + ["GOALS:"] + [f"{key}: {value}" for key, value in GOALS.items()]
)
