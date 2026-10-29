"""Vercel Python function for the v4.5 AIDC planning workflow."""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.dont_write_bytecode = True

MODELS = [
    {"id": "deepseek/deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash"},
    {"id": "openai/gpt-4o-mini", "label": "GPT-4o mini"},
    {"id": "anthropic/claude-3.5-haiku", "label": "Claude 3.5 Haiku"},
    {"id": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
]


def _plan(data):
    api_key = str(data.get("apiKey") or "").strip()
    model = str(data.get("model", ""))
    text = str(data.get("text") or "").strip()
    if not api_key or len(api_key) > 500:
        return 400, {"error": "Enter a valid OpenRouter API key."}
    if model not in {item["id"] for item in MODELS}:
        return 400, {"error": "Choose a supported model."}
    if not text or len(text) > 12_000:
        return 400, {"error": "Input text must be 1–12,000 characters."}

    from initial_input import complete_initial_information
    from solver import build_and_solve_model
    from v4_data import load, apply_site_measurements
    from v4_simulation import evaluate

    try:
        from utils import REQUEST_AGENT
        try:
            if not _verify_key(api_key):
                return 401, {"error": "OpenRouter rejected this API key."}
        except HTTPError as exc:
            if exc.code in (401, 403):
                return 401, {"error": "OpenRouter rejected this API key."}
            return 502, {"error": "OpenRouter key verification is temporarily unavailable."}
        except (URLError, TimeoutError):
            return 502, {"error": "Could not reach OpenRouter. Please try again."}
        token = REQUEST_AGENT.set(_RequestAgent(api_key, model))
        try:
            context = complete_initial_information({"text": text, "backend": "scipy", "interactive": False})
        finally:
            REQUEST_AGENT.reset(token)
        interpretation_error = (context.get("semantic_trace") or {}).get("extract_error")
        if interpretation_error:
            return 502, {"error": "OpenRouter could not interpret the site brief. Your input has not been checked yet.",
                         "details": [str(interpretation_error).replace(api_key, "[redacted]")[:700]], "model": model}
        if context["missing"] or context.get("clarification_issues") or not context["research"].get("point"):
            details = list(context["missing"])
            details.extend(context.get("clarification_issues", {}).values())
            if not context["research"].get("point"):
                details.append("A valid site location is required.")
            return 422, {"error": "Input needs clarification.", "details": details}

        cache_file = ROOT / "reference" / "experiment_features.json"
        cache = cache_file if _cache_matches(cache_file, context["research"]["point"]) else None
        features, manifest = load(context["research"]["point"], cache=cache)
        features, site_adjustment = apply_site_measurements(features, context)
        if site_adjustment:
            manifest["site_adjustment"] = site_adjustment
        context["features"] = features
        context["variant"] = "v3.6"
        model_result = build_and_solve_model(context)
        if not model_result or not model_result["cost"][0]["verified_feasible"]:
            return 422, {"error": "No audited feasible baseline for these conditions.",
                         "details": context.get("preflight_issues", [])}
        study = evaluate("v4.5", model_result["boundary"], context["facts"], features,
                         model_result["cost"], context["backend"])
        return 200, {"model": model, "rows": _table_rows(context, study, manifest),
                     "caveat": "Offline research scenario using public data and explicit assumptions. Not site certification, a reliability guarantee, or real-time control."}
    except Exception as exc:
        return 500, {"error": "Planning failed. Please check your site requirements and try again."}


class _RequestAgent:
    """Request-scoped OpenRouter caller; credentials never enter shared env state."""
    def __init__(self, api_key, model):
        self.api_key, self.model = api_key, model

    def complete(self, system, user):
        request = Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps({"model": self.model, "temperature": 0,
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST")
        try:
            with urlopen(request, timeout=120) as response:
                payload = json.load(response)
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(8192)).get("error", {})
                reason = error.get("message", "Request rejected") if isinstance(error, dict) else str(error)
            except (ValueError, UnicodeError):
                reason = "Request rejected"
            reason = str(reason).replace(self.api_key, "[redacted]")[:500]
            raise RuntimeError(f"OpenRouter HTTP {exc.code} for {self.model}: {reason}") from None
        except (URLError, TimeoutError):
            raise RuntimeError("OpenRouter inference could not be reached or timed out. Please try again.") from None
        if payload.get("error"):
            raise RuntimeError("OpenRouter returned an inference error. Check model availability and account permissions.")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("OpenRouter returned no usable model response.") from None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter returned an empty model response.")
        return {"raw": content, "model": self.model, "provider": "OpenRouter"}


def _verify_key(api_key):
    from urllib.request import Request, urlopen
    request = Request("https://openrouter.ai/api/v1/key",
                      headers={"Authorization": f"Bearer {api_key}"})
    with urlopen(request, timeout=10) as response:
        return response.status == 200


def _cache_matches(cache_file, point):
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        url = cached["weather"]["days"][0]["source_url"]
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(url).query)
        return (abs(float(query["latitude"][0]) - point["latitude"]) <= .05 and
                abs(float(query["longitude"][0]) - point["longitude"]) <= .05)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def _table_rows(context, study, manifest):
    base = study.get("baseline", {})
    capacities = study.get("baseline_capacities", {})
    rows = [
        {"category": "Summary", "item": "Site", "value": context.get("research", {}).get("point", {}).get("name", "—"), "unit": ""},
        {"category": "Summary", "item": "Objective", "value": context.get("goal", "Minimum annual cost"), "unit": ""},
        {"category": "Result", "item": "Annual cost", "value": round(base.get("annual_cost_usd", 0)), "unit": "USD/year"},
        {"category": "Result", "item": "Green electricity share", "value": round(base.get("green_fraction", 0) * 100, 2), "unit": "%"},
        {"category": "Audit", "item": "Optimization and audits", "value": "Passed" if base.get("verified") else "Not verified", "unit": ""},
    ]
    for key, label, unit in [("cooling_kw", "Cooling capacity", "kW"), ("battery_kwh", "Battery capacity", "kWh"),
                             ("pv_kw", "Solar capacity", "kW"), ("grid_kw", "Grid capacity", "kW")]:
        if key in capacities:
            rows.append({"category": "Design", "item": label, "value": round(capacities[key], 2), "unit": unit})
    for item in study.get("stress", []):
        rows.append({"category": "Scenario", "item": item.get("name", "Stress case"),
                     "value": item.get("redesign", {}).get("status", item.get("status", "—")), "unit": ""})
    for item in study.get("faults", []):
        rows.append({"category": "Fault replay", "item": item.get("event", {}).get("name", "Fault"),
                     "value": item.get("status", "—"), "unit": f"unserved {item.get('unserved_kwh', 0):.2f} kWh" if item.get("unserved_kwh") is not None else ""})
    rows += [{"category": "Data", "item": "Feature source", "value": manifest.get("access", "—"), "unit": ""},
             {"category": "Data", "item": "Aggregate data size", "value": manifest.get("aggregate_bytes", "—"), "unit": "bytes"}]
    return rows


class handler(BaseHTTPRequestHandler):
    def _endpoint(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/")
        if path == "/api/index":
            endpoint = parse_qs(url.query).get("endpoint", [""])[0]
            if endpoint == "panel":
                return "/"
            if endpoint in ("models", "plan"):
                return "/api/" + endpoint
        return path

    def do_GET(self):
        if self._endpoint() in ("", "/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self._endpoint() == "/api/models":
            self._respond(200, {"models": MODELS})
        else:
            self._respond(404, {"error": "Not found"})

    def do_POST(self):
        if self._endpoint() != "/api/plan":
            return self._respond(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 20_000:
                return self._respond(413, {"error": "Request must be under 20 KB."})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                return self._respond(400, {"error": "Invalid request."})
            status, payload = _plan(data)
            self._respond(status, payload)
        except (ValueError, json.JSONDecodeError) as exc:
            self._respond(400, {"error": str(exc)})
        except Exception as exc:
            self._respond(500, {"error": str(exc) or "Planning run failed."})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return
