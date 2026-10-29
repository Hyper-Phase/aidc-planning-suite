"""One cumulative feature switch per ablation version."""

VERSIONS = {
    "v3.1": {"separate_jobs": False, "cross_day": False, "azure": False,
             "h100_power": False, "multi_day": False, "multi_objective": False},
    "v3.2": {"separate_jobs": True, "cross_day": True, "azure": False,
             "h100_power": False, "multi_day": False, "multi_objective": False},
    "v3.3": {"separate_jobs": True, "cross_day": True, "azure": True,
             "h100_power": False, "multi_day": False, "multi_objective": False},
    "v3.4": {"separate_jobs": True, "cross_day": True, "azure": True,
             "h100_power": True, "multi_day": False, "multi_objective": False},
    "v3.5": {"separate_jobs": True, "cross_day": True, "azure": True,
             "h100_power": True, "multi_day": True, "multi_objective": False},
    "v3.6": {"separate_jobs": True, "cross_day": True, "azure": True,
             "h100_power": True, "multi_day": True, "multi_objective": True},
}
