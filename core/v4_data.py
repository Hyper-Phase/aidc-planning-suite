"""Bounded remote feature intake with a small, auditable cache."""
from __future__ import annotations

import json
import copy
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from remote_features import collect


def apply_site_measurements(features, context):
    """Calibrate hourly proxy shapes to accepted site values without editing the cache.

    The two representative days remain public proxy shapes. User values set
    their aggregate temperature level, swing amplitude, and PV energy scale.
    """
    facts = context["facts"]
    evidence = context["evidence"]
    public = context.get("research", {}).get("fields", {})
    days = features["weather"]["days"]
    weights = features["weather"]["weights"]
    weighted_mean = sum(weight * sum(day["temperature_c"]) / 24
                        for day, weight in zip(days, weights)) / sum(weights)
    weighted_swing = sum(weight * (max(day["temperature_c"]) - min(day["temperature_c"])) / 2
                         for day, weight in zip(days, weights)) / sum(weights)
    sine_hours = sum(max(0.0, math.sin(math.pi * (hour - 6) / 12)) for hour in range(24))
    pv_equivalent = sum(weight * sum(min(1.0, max(0.0, .86 * radiation / 1000))
                                     for radiation in day["irradiance_w_m2"])
                        for day, weight in zip(days, weights)) / sum(weights) / sine_hours
    reference = {
        "ambient_mean_c": weighted_mean,
        "ambient_swing_c": weighted_swing,
        "pv_peak_cf": pv_equivalent,
    }
    adjustments = {}
    for field in ("ambient_mean_c", "ambient_swing_c", "pv_peak_cf"):
        if evidence.get(field, {}).get("status") != "user_provided" or field not in facts:
            continue
        baseline = reference[field]
        if baseline is None or (field != "ambient_mean_c" and baseline <= 0):
            continue
        requested = float(facts[field])
        if math.isclose(requested, baseline, rel_tol=0, abs_tol=1e-9):
            continue
        adjustments[field] = {"user_value": requested, "hourly_proxy_reference": baseline,
                              "public_estimate": public.get(field, {}).get("value"),
                              "operation": "shift" if field == "ambient_mean_c" else "scale",
                              "amount": requested - baseline if field == "ambient_mean_c" else requested / baseline}
    if not adjustments:
        return features, {}
    adjusted = copy.deepcopy(features)
    for day in adjusted["weather"]["days"]:
        temperatures = day["temperature_c"]
        if "ambient_swing_c" in adjustments:
            mean = sum(temperatures) / len(temperatures)
            factor = adjustments["ambient_swing_c"]["amount"]
            temperatures = [mean + (value - mean) * factor for value in temperatures]
        if "ambient_mean_c" in adjustments:
            delta = adjustments["ambient_mean_c"]["amount"]
            temperatures = [value + delta for value in temperatures]
        day["temperature_c"] = temperatures
        if "pv_peak_cf" in adjustments:
            factor = adjustments["pv_peak_cf"]["amount"]
            day["irradiance_w_m2"] = [value * factor for value in day["irradiance_w_m2"]]
    return adjusted, {"method": "User site values shift/scale frozen two-day hourly proxy shapes; PV factors are capped to 1 in scenarios. This is not a measured hourly trace.",
                      "adjustments": adjustments}

ALLOWED_HOSTS = {"github.com", "raw.githubusercontent.com", "archive-api.open-meteo.com"}


def _check_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Unexpected source URL: {url}")


def validate(features):
    azure, power, weather = (features[key] for key in ("azure", "power", "weather"))
    if len(azure["hourly_shape"]) != 24 or len(azure["hourly_requests"]) != 24:
        raise ValueError("Azure summary must cover 24 hours")
    if min(azure["hourly_shape"]) < 0 or min(azure["hourly_requests"]) <= 0:
        raise ValueError("Invalid Azure hourly aggregates")
    if abs(sum(azure["hourly_shape"]) - 24) > 1e-5:
        raise ValueError("Azure shape must have mean 1")
    if not (0 < power["active_gpu_kw_mean"] <= power["active_gpu_kw_p95"] < 2):
        raise ValueError("Invalid measured GPU power summary")
    if len(power.get("commit", "")) != 40:
        raise ValueError("H100 data must be pinned to a full commit SHA")
    if len(weather["days"]) != 2 or sum(weather["weights"]) != 365:
        raise ValueError("Expected two weighted weather days")
    for day in weather["days"]:
        if len(day["temperature_c"]) != 24 or len(day["irradiance_w_m2"]) != 24:
            raise ValueError("Weather day must have 24 hourly values")
        _check_url(day["source_url"])
    for item in (azure, power):
        _check_url(item["source_url"])
    return features


def _head(url):
    request = Request(url, headers={"User-Agent": "GuidedStudyAIDC/4.0"}, method="HEAD")
    with urlopen(request, timeout=25) as response:
        return {"http_status": response.status, "content_type": response.headers.get("Content-Type"),
                "content_length": response.headers.get("Content-Length")}


def _github_license(repository):
    url = f"https://api.github.com/repos/{repository}/license"
    with urlopen(Request(url, headers={"User-Agent": "GuidedStudyAIDC/4.0",
                                       "Accept": "application/vnd.github+json"}), timeout=25) as response:
        body = response.read(200_001)
    if len(body) > 200_000:
        raise ValueError("License metadata too large")
    data = json.loads(body)
    return {"spdx_id": data.get("license", {}).get("spdx_id"), "metadata_url": url}


def load(point, *, cache=None, refresh=False):
    """Read source files remotely only on refresh; cache stores aggregates, not raw data."""
    if cache and Path(cache).exists() and not refresh:
        features = json.loads(Path(cache).read_text(encoding="utf-8"))
        access = "validated cached aggregates; live URLs not rechecked in this run"
    else:
        features = collect("v3.6", point=point)
        access = "bounded remote streaming and two small API responses"
        for day in features["weather"]["days"]:
            with urlopen(Request(day["source_url"],
                                 headers={"User-Agent": "GuidedStudyAIDC/4.0"}), timeout=25) as response:
                metadata = json.loads(response.read(200_001))
            day["timezone"] = metadata.get("timezone")
            day["utc_offset_seconds"] = metadata.get("utc_offset_seconds")
        if cache:
            Path(cache).write_text(json.dumps(features, ensure_ascii=False, indent=2), encoding="utf-8")
    validate(features)
    weather_query = parse_qs(urlparse(features["weather"]["days"][0]["source_url"]).query)
    source_lat = float(weather_query["latitude"][0])
    source_lon = float(weather_query["longitude"][0])
    if abs(source_lat - point["latitude"]) > .05 or abs(source_lon - point["longitude"]) > .05:
        raise ValueError("Cached weather coordinates do not match the requested site")
    manifest = {"access": access, "validated_utc": datetime.now(timezone.utc).isoformat(),
                "aggregate_bytes": len(json.dumps(features).encode("utf-8")),
                "raw_files_persisted": False,
                "sources": [
                    {"kind": "Azure inference", "url": features["azure"]["source_url"],
                     "date": features["azure"]["sample_date_utc"], "unit": "hourly relative token count",
                     "scope": "observed UTC request trace; no GPU-hour calibration",
                     "license": "not programmatically confirmed; check source terms before redistribution"},
                    {"kind": "H100 training", "url": features["power"]["source_url"],
                     "commit": features["power"]["commit"], "unit": "kW per active GPU",
                     "scope": "measured training sample, not node or site power",
                     "license": "not programmatically confirmed; check repository terms"},
                    *[{"kind": "ERA5 weather", "url": day["source_url"], "date": day["date"],
                       "unit": "C and W/m2 hourly", "timezone": day.get("timezone", "unknown in cache"),
                       "utc_offset_seconds": day.get("utc_offset_seconds"),
                       "scope": "reanalysis, site proxy; not temporally aligned with Azure UTC trace",
                       "license": "check Open-Meteo usage terms for intended use"}
                      for day in features["weather"]["days"]]]}
    if refresh:
        for item in manifest["sources"]:
            try:
                item["remote_check"] = _head(item["url"])
            except Exception as exc:
                item["remote_check"] = {"status": "HEAD unavailable after successful data read",
                                        "reason": type(exc).__name__}
        for item, repository in zip(manifest["sources"][:2],
                                    ("Azure/AzurePublicDataset",
                                     "Ahmed-Elsayed95/High-resolution-AI-Data-Center-Training-Workloads-Dataset")):
            try:
                item["license_check"] = _github_license(repository)
                spdx = item["license_check"].get("spdx_id")
                if spdx and spdx != "NOASSERTION":
                    item["license"] = f"repository metadata: {spdx}; verify individual dataset terms"
            except Exception as exc:
                item["license_check"] = {"status": "unresolved", "reason": type(exc).__name__}
    return features, manifest
