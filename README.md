# AIDC Planning Suite

This repository contains the `demo.py` v4.5 research workflow and a Vercel-hosted control panel. The panel accepts an OpenRouter API key, selects a model, interprets an English site brief, runs the SciPy optimization and scenario workflow, and displays the proposal as a table.

## Deploy to Vercel

1. Push this `deploy/` directory as the root of a GitHub repository.
2. In Vercel, import that GitHub repository. The included `vercel.json` routes `/` to the control panel and `/api/*` to the Python function.
3. The panel takes an OpenRouter key per request. It is sent to the Python function over HTTPS, used only for that run, and is not stored as a Vercel environment variable. The key is verified through OpenRouter before any LLM request.

The selected model IDs are configured in `api/index.py` and can be edited there. The server runs the LLM-first v4.5 path; `demo.py` remains available for command-line use.

## Run locally

Use Python 3.11 or later and install the dependencies:

```powershell
python -m pip install -r requirements.txt
python demo.py
```

For the online workflow, set `OPENROUTER_API_KEY` and optionally `OPENROUTER_MODEL` before running the CLI. The web function passes its request-scoped key directly to the model client and does not use process-global credentials.

## Scope and data

The solver uses SciPy/HiGHS. COPT is optional and is not installed by this deployment. Frozen feature aggregates under `reference/` are reused only for matching coordinates; other locations fetch bounded public aggregate data when a run executes. No raw trace files are included. Plans are research scenarios, not site certification, a reliability guarantee, or real-time control.

## Project contents

- `demo.py`, `core/`: five-stage v4.5 CLI workflow and required modules.
- `api/index.py`: Vercel WSGI endpoint.
- `index.html`: responsive control panel.
- `reference/`: small frozen aggregate records needed by the core and coordinate-matched cache.
