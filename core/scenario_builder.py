"""Construct comparable 24-hour scenarios from stated facts and remote features."""
from __future__ import annotations

import math
from utils import make_scenarios
from variants import VERSIONS


def build_scenarios(boundary, facts, version, features):
    settings = VERSIONS[version]
    base = make_scenarios(boundary, facts)[0]
    if not settings["separate_jobs"]:
        return [base]

    p = boundary.training_share
    train_shape = [.64 + .08 * math.cos(2 * math.pi * (h - 12) / 24) for h in range(24)]
    infer_shape = [.38 + .28 * (h >= 18 or h < 6) for h in range(24)]
    total = sum(base['arrivals'])
    train = [total * p * value / sum(train_shape) for value in train_shape]
    infer = [total * (1 - p) * value / sum(infer_shape) for value in infer_shape]
    if settings["azure"]:
        shape = features["azure"]["hourly_shape"]
        total = sum(infer)
        infer = [total * value / sum(shape) for value in shape]
    base.update(training_arrivals=train, inference_arrivals=infer,
                arrivals=[a + b for a, b in zip(train, infer)])
    if not settings["multi_day"]:
        return [base]

    scenarios = []
    for day, weight in zip(features["weather"]["days"], features["weather"]["weights"]):
        scenarios.append({**base, "name": day["name"], "weight": weight,
                          "ambient": day["temperature_c"],
                          "pv": [min(1., max(0., .86 * radiation / 1000))
                                 for radiation in day["irradiance_w_m2"]],
                          "weather_source_url": day["source_url"]})
    return scenarios
