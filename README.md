# AIDC Planning Suite

An English design studio for data-center planning. The model interprets the site brief; SciPy/HiGHS builds and audits the cost-baseline design. The v4.5 workflow then evaluates stress scenarios, faults, finite candidate trade-offs and a demand-change redesign.

## Deploy to Vercel

1. Import the repository root into Vercel, choose **Other**, and leave the Build Command unset.
2. In **Project Settings → Environment Variables**, add `OPENROUTER_API_KEY` with your OpenRouter key for the intended environment (for example Production). Enter the value only in Vercel's secure settings, never in GitHub or the page source.
3. Redeploy after changing environment variables. The panel displays **Server API configured** when the deployed function can read a server key. Visitors then need no API key.
4. Optionally configure `PLANNER_ACCESS_CODE` to restrict use of the shared server key. Visitors enter the project access code instead of an OpenRouter credential.

If no server key is configured, the advanced personal-key input remains available for compatibility. The server key takes precedence. The panel does not save either key or the project access code to browser storage or reports. `/api/config` returns configuration flags and models, never credential values.

For hosts with a private secret mount, `OPENROUTER_API_KEY_FILE` may point to a UTF-8 text file containing only the key. `OPENROUTER_API_KEY` takes precedence over the file. Do not commit this file, place it in public/static assets, or bundle it with the application. On Vercel, encrypted environment variables are the supported default; uploading a key file into this repository is not required.

`vercel.json` bundles the panel, `core/` and frozen aggregate data into the Python function. It routes `/`, `/index.html`, `/api/models`, `/api/config` and `/api/plan` to the handler. The maximum request duration is 300 seconds, subject to the Vercel plan.

## Design workflow

- Choose an interpretation model; DeepSeek V4.1 Flash is the default.
- Write an English site brief, load the example, or use guided inputs for compute, site, energy and continuity constraints.
- When inputs need clarification, complete missing values or edit the brief to resolve conflicts; then retry.
- Review equipment quantities, investment, operating metrics and findings. The baseline optimizes annual cost subject to the accepted constraints. Renewable-objective optimization is not silently claimed; explicit green-share constraints are supported.
- Inspect **v4 evaluation details**, then preview, open or download the standalone **HTML report**. Downloaded reports contain site information and design data; store or share them accordingly.
- Revise the brief and generate another proposal to compare the latest two runs in the current tab. The previous report remains available during a failed update. No server-side project history is stored.

## Report contents and v4 validation

The report contains the input basis and evidence, all modelled equipment capacities and their design roles, CAPEX and annual cost, operational metrics, an hourly dispatch table, assumptions, provenance and a complete structured record.

The API has always called `evaluate("v4.5", ...)`; the earlier compact table omitted most of its results. The studio now exposes all stages and distinguishes evaluation execution from feasibility, conservation audits and service outcomes:

| Stage | Report evidence |
| --- | --- |
| v4.1 | Baseline solver status, MIP gap, conservation/matrix/integer checks and independent audit |
| v4.2 | Seven observed/synthetic stress scenarios; fixed design vs per-case redesign; joint redesign; heat-wave cooling upgrade diagnostic |
| v4.3 | Eight fault cases with applicability, timing, unserved energy, UPS shortfall and audit outcome; battery precharge mitigation |
| v4.4 | Audited finite candidates, their equipment/cost/service metrics, preference weights and negotiation rounds |
| v4.5 | Demand-change trigger, old investment vs new investment, changed capacities and client decisions |

**Completed with findings** means the evaluation ran and found a limitation. An infeasible stress design is not a missing test. A fault conservation audit can pass even when energy is unserved. **Not applicable** and **Not run** are never presented as passed. If the v4 evaluation raises an error, the API does not issue a complete validation report.

Reports are generated from actual solver results without an additional LLM narrative call. HTML text is escaped and the standalone report includes no scripts or remote resources. Equipment quotes, electrical protection, cooling curves, local codes, site measurements and SLA approval remain necessary for engineering design. Plans are research scenarios, not site certification, a continuity guarantee or real-time control.

Frozen public aggregates in `reference/` are reused only for matching coordinates. Other locations collect bounded public data at run time. No raw trace files are included. COPT is optional and not installed.

## Run and test locally

Use Python 3.11 or later:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

For the CLI, securely export `OPENROUTER_API_KEY`, optionally set `OPENROUTER_MODEL`, and run `python demo.py`. `python demo.py --local` uses explicit offline parsing; location and aggregate cache flags can support a fully offline run. The existing CLI Markdown report is retained; the web API provides the HTML report.

For the local web handler, export the server key and run:

```bash
python -c 'from http.server import ThreadingHTTPServer; from api.index import handler; ThreadingHTTPServer(("127.0.0.1", 8000), handler).serve_forever()'
```

The tests use mocked key verification/LLM interpretation but run the real cached SciPy solver and all v4 stages. They also cover report escaping, secret precedence, key non-disclosure, missing-test outcomes and routing. Successful local tests do not establish real OpenRouter access for your deployment credentials.
