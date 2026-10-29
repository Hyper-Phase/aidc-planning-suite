"""Bounded remote aggregation of research traces; no raw dataset is saved locally."""
from __future__ import annotations

import csv
import io
import json
import math
import statistics
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AZURE_URL = ("https://github.com/Azure/AzurePublicDataset/releases/download/"
             "dataset-llm-2024/AzureLLMInferenceTrace_code_1week.csv")
H100_REPO = "Ahmed-Elsayed95/High-resolution-AI-Data-Center-Training-Workloads-Dataset"
H100_PATH = ("Node_Dataset/Text%20Generation%20LLMs/H100/Batch_size/"
             "LLM_H100_8GPU_Node_Lamma8B_SeqLength2048_Deepspeedds_z3_BatchSize16.csv")
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"


def _open(url, *, byte_limit=None):
    headers = {"User-Agent": "GuidedStudyAIDC/3.0 academic research"}
    if byte_limit:
        headers["Range"] = f"bytes=0-{byte_limit - 1}"
    return urlopen(Request(url, headers=headers), timeout=40)


def _json(url):
    with _open(url) as response:
        body = response.read(3_000_001)
    if len(body) > 3_000_000:
        raise ValueError("Feature metadata exceeds 3 MB")
    return json.loads(body)


def azure_day_profile(url=AZURE_URL, max_bytes=100_000_000):
    """Stream one full UTC day of requests; retain 24 aggregate cells only."""
    hourly_tokens = [0.] * 24
    hourly_requests = [0] * 24
    observed_date = None
    with _open(url, byte_limit=max_bytes) as response:
        if response.status != 206:
            raise ValueError("Azure release did not honor bounded HTTP Range")
        stream = io.TextIOWrapper(response, encoding="utf-8", newline="")
        reader = csv.DictReader(stream)
        for row in reader:
            timestamp = row.get("TIMESTAMP")
            if not timestamp or len(timestamp) < 13:
                continue
            date = timestamp[:10]
            if observed_date is None:
                observed_date = date
            if date != observed_date:
                break
            try:
                hour = int(timestamp[11:13])
                tokens = int(row["ContextTokens"]) + int(row["GeneratedTokens"])
            except (TypeError, ValueError):
                continue
            hourly_requests[hour] += 1
            hourly_tokens[hour] += tokens
    if min(hourly_requests) == 0:
        raise ValueError("Bounded Azure sample did not cover every hour of one UTC day")
    mean = statistics.mean(hourly_tokens)
    return {"hourly_shape": [v / mean for v in hourly_tokens],
            "hourly_requests": hourly_requests, "total_requests": sum(hourly_requests),
            "total_tokens": int(sum(hourly_tokens)), "sample_date_utc": observed_date,
            "source_url": url, "method": "stream first complete UTC day; normalize token sum to mean 1",
            "mapping_status": "temporal shape only; Token-to-GPU-hour conversion uncalibrated",
            "retrieved_at": datetime.now(timezone.utc).isoformat()}


def h100_power_sample(max_bytes=20_000_000):
    """Aggregate measured 8-GPU H100 training rows; keep no high-frequency samples."""
    commit = _json(f"https://api.github.com/repos/{H100_REPO}/commits/main")["sha"]
    url = f"https://raw.githubusercontent.com/{H100_REPO}/{commit}/{H100_PATH}"
    active, all_power = [], []
    with _open(url, byte_limit=max_bytes) as response:
        if response.status != 206:
            raise ValueError("H100 raw file did not honor bounded HTTP Range")
        reader = csv.DictReader(io.TextIOWrapper(response, encoding="utf-8", newline=""))
        for row in reader:
            try:
                power = [float(row[f"gpu{i}_power_W"]) for i in range(8)]
                util = [float(row[f"gpu{i}_utilization_percent"]) for i in range(8)]
            except (ValueError, TypeError):
                continue
            if not all(math.isfinite(value) and 0 <= value <= 1200 for value in power):
                continue
            avg = statistics.mean(power) / 1000
            all_power.append(avg)
            if statistics.mean(util) >= 70:
                active.append(avg)
    if len(active) < 100:
        raise ValueError("Insufficient active H100 measurements")
    active.sort()
    return {"active_gpu_kw_mean": statistics.mean(active),
            "active_gpu_kw_p95": active[int(.95 * (len(active) - 1))],
            "all_gpu_kw_mean": statistics.mean(all_power), "active_rows": len(active),
            "source_url": url, "commit": commit, "hardware": "8× H100 node",
            "task": "Llama 8B training, sequence 2048, DeepSpeed ZeRO-3, batch 16",
            "measurement_level": "per-GPU power; CPU/node auxiliary power not calibrated",
            "retrieved_at": datetime.now(timezone.utc).isoformat()}


def two_day_weather(latitude, longitude, year=2025):
    """Retrieve two small, actual hourly weather slices as labeled scenarios."""
    days = [("winter", f"{year}-01-15"), ("summer", f"{year}-07-15")]
    scenarios = []
    for name, day in days:
        url = WEATHER_URL + "?" + urlencode({"latitude": latitude, "longitude": longitude,
                                              "start_date": day, "end_date": day,
                                              "hourly": "temperature_2m,shortwave_radiation",
                                              "timezone": "auto", "models": "era5"})
        hourly = _json(url)["hourly"]
        temperature = [float(value) for value in hourly["temperature_2m"]]
        irradiance = [float(value) for value in hourly["shortwave_radiation"]]
        if len(temperature) != 24 or len(irradiance) != 24:
            raise ValueError(f"{day} did not return 24 hourly values")
        scenarios.append({"name": name, "date": day, "temperature_c": temperature,
                          "irradiance_w_m2": irradiance, "source_url": url})
    return {"days": scenarios, "weights": [182.5, 182.5],
            "weight_status": "illustrative 50/50, not a fitted annual probability distribution",
            "pv_mapping": "irradiance/1000 × 0.86 proxy; not measured PV output",
            "retrieved_at": datetime.now(timezone.utc).isoformat()}


def collect(version, *, point=None):
    features = {}
    if version >= "v3.3":
        features["azure"] = azure_day_profile()
    if version >= "v3.4":
        features["power"] = h100_power_sample()
    if version >= "v3.5":
        if not point:
            raise ValueError("v3.5+ requires a geocoded location for hourly weather")
        features["weather"] = two_day_weather(point["latitude"], point["longitude"])
    return features
