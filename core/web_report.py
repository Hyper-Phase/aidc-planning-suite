"""Self-contained, escaped HTML design reports from audited solver outputs."""
from datetime import datetime, timezone
from html import escape
import json

CAPACITIES = {
    'servers': ('GPU servers', 'units', 'Eight GPUs per server; fixed requested compute fleet.'),
    'pv_kw': ('Solar PV', 'kWp', 'Installed solar nameplate within the usable roof constraint.'),
    'battery_kw': ('Battery inverter', 'kW', 'Maximum battery charge/discharge power.'),
    'battery_kwh': ('Battery storage', 'kWh', 'Installed energy capacity; usable energy is limited by the SOC model.'),
    'cooling_kw': ('Cooling plant', 'kW', 'Cooling nameplate under the simplified thermal model.'),
    'transformer_units': ('Transformers', 'units', 'Grid connection equipment under the model redundancy assumptions.'),
    'ups_units': ('UPS modules', 'units', 'UPS equipment sized for the requested continuity boundary.'),
    'ups_battery_kwh': ('UPS battery', 'kWh', 'Energy reserved for the specified UPS autonomy.'),
    'diesel_units': ('Diesel generators', 'units', 'Zero means no generator is installed in this candidate.'),
    'fuel_litres': ('Fuel storage', 'litres', 'Modelled fuel inventory; local installation requirements need review.'),
}

def text(value):
    if value is None:
        return '—'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, float):
        return f'{value:,.3f}'.rstrip('0').rstrip('.')
    return str(value)


def esc(value):
    return escape(text(value), quote=True)


def label(value):
    return str(value).replace('_', ' ').capitalize()


def table(headers, rows):
    rows = list(rows)
    if not rows:
        return '<p class="muted">Not run / no records available.</p>'
    return '<div class="scroll"><table><thead><tr>' + ''.join('<th>'+esc(h)+'</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+esc(c)+'</td>' for c in row)+'</tr>' for row in rows) + '</tbody></table></div>'


def validation(study, solver):
    stress, faults = study.get('stress', []), study.get('faults', [])
    applicable = [r for r in faults if r.get('status') == 'simulated']
    return [
        {'version': 'v4.1', 'name': 'Baseline optimization and two audits', 'status': 'passed' if solver.get('verified_feasible') else 'failed', 'executed': bool(solver), 'details': f"Solver: {solver.get('status', 'not run')}; conservation audit: {solver.get('audit', {}).get('passed')}; independent audit: {solver.get('independent_audit', {}).get('passed')}"},
        {'version': 'v4.2', 'name': 'Hourly stress and shared redesign', 'status': ('completed_with_findings' if any(not r.get('verified') for r in stress) or not study.get('stress_joint_redesign', {}).get('verified') else 'passed') if stress else 'not_run', 'executed': bool(stress), 'details': f"{len(stress)} scenarios; {sum(bool(r.get('verified')) for r in stress)} fixed designs feasible; shared redesign: {study.get('stress_joint_redesign', {}).get('status', 'not run')}"},
        {'version': 'v4.3', 'name': 'Minute-level fault replay', 'status': ('failed' if any(not r.get('audit_passed') for r in applicable) else 'completed_with_findings' if any((r.get('unserved_kwh') or 0) > 1e-6 or (r.get('ups_bridge_shortfall_kwh') or 0) > 1e-6 for r in applicable) else 'passed') if faults else 'not_run', 'executed': bool(faults), 'details': f"{len(applicable)} simulated; {sum(r.get('status') == 'not_applicable' for r in faults)} not applicable; {sum(bool(r.get('audit_passed')) for r in applicable)} conservation audits passed"},
        {'version': 'v4.4', 'name': 'Candidate trade-offs and negotiation', 'status': ('passed' if study['bargaining'].get('equal_preferences', {}).get('status') == 'agreement' else 'completed_with_findings') if study.get('bargaining') else 'not_run', 'executed': bool(study.get('bargaining')), 'details': f"{len(study.get('bargaining', {}).get('candidates', []))} audited candidates; finite comparison, not a Pareto frontier"},
        {'version': 'v4.5', 'name': 'Demand-change replanning', 'status': ('passed' if study['replanning'].get('new_design_on_new_input', {}).get('verified') else 'completed_with_findings') if study.get('replanning') else 'not_run', 'executed': bool(study.get('replanning')), 'details': study.get('replanning', {}).get('trigger', 'Not run')},
    ]


def build_report(context, manifest, study, pack, model):
    solver, hourly, jobs = pack
    baseline = study.get('baseline', {})
    capacities = study.get('baseline_capacities', {})
    checks = validation(study, solver)
    generated = datetime.now(timezone.utc).isoformat()
    point = context.get('research', {}).get('point') or {}
    recommendations = []
    for row in study.get('stress', []):
        if not row.get('verified'):
            recommendations.append(f"{label(row['name'])}: the installed baseline is {row.get('status')}. A scenario-specific redesign is {row.get('redesign', {}).get('status', 'not run')}.")
        upgrade = row.get('cooling_only_upgrade', {})
        if upgrade.get('sufficient_cooling_kw') is not None:
            recommendations.append(f"Heat-wave option: increase cooling from {text(upgrade.get('original_cooling_kw'))} to {text(upgrade['sufficient_cooling_kw'])} kW. This is a model threshold, not an engineering design margin.")
    if (baseline.get('worst_fault_unserved_kwh') or 0) > 0:
        recommendations.append(f"Fault hardening is required before claiming continuity: worst simulated unserved energy is {text(baseline['worst_fault_unserved_kwh'])} kWh. The baseline minimizes annual cost and does not guarantee fault-free service.")
    recommendations.extend(study.get('replanning', {}).get('missing_decisions', []))
    record = {
        'version': study.get('version'), 'generated_utc': generated, 'model': model,
        'site': point, 'brief': context.get('text', ''), 'objective': context.get('goal'),
        'facts': context.get('facts', {}), 'evidence': context.get('evidence', {}),
        'assumptions': context.get('assumptions', []), 'simplifications': context.get('ignored', []),
        'limits': context.get('limits', {}), 'source_manifest': manifest,
        'solver': solver, 'evaluation': study,
        'hourly_dispatch': hourly.to_dict(orient='records'),
        'training_schedule': jobs.to_dict(orient='records'),
    }
    sections = []
    def section(id, heading, body):
        sections.append(f'<section id="{id}"><h2>{esc(heading)}</h2>{body}</section>')
    section('brief', '1. Design basis', '<p>'+esc(context.get('text', ''))+'</p><p><strong>Applied objective:</strong> Minimum annual cost with the accepted constraints. Other candidate objectives are evaluated separately.</p>'+table(['Parameter', 'Value', 'Evidence / source'], ((label(k), v, context.get('evidence', {}).get(k, {}).get('quote', 'Model default / public estimate')) for k,v in context.get('facts', {}).items()))+table(['Accepted constraint', 'Value / interpretation'], ((label(k), json.dumps(v, ensure_ascii=False)) for k,v in context.get('limits', {}).items())))
    section('equipment', '2. Proposed equipment and investment', '<p>This is the cost-optimal audited baseline. It is not automatically the most resilient candidate.</p>'+table(['Equipment', 'Quantity', 'Unit', 'Design role'], ((v[0], capacities[k], v[1], v[2]) for k,v in CAPACITIES.items() if k in capacities))+table(['Investment / operation', 'Value', 'Unit'], [('Model CAPEX', solver.get('capex_usd'), 'USD'), ('Annual cost', baseline.get('annual_cost_usd'), 'USD/year'), ('Green electricity share', (baseline.get('green_fraction') or 0)*100, '%'), ('Required backup', context.get('facts', {}).get('backup_hours'), 'hours'), ('Grid import boundary', context.get('facts', {}).get('grid_kw'), 'kW'), ('Minimum grid headroom', solver.get('metrics', {}).get('minimum_grid_headroom_kw'), 'kW'), ('Optimization MIP gap', solver.get('mip_gap'), 'fraction')]))
    section('actions', '3. Design findings and next decisions', '<ul>'+''.join('<li>'+esc(r)+'</li>' for r in recommendations)+'</ul><p>Equipment quotes, electrical protection, cooling curves, local codes, site measurements and SLA approval are needed before detailed engineering.</p>')
    section('validation', '4. v4 execution record', '<p>These are planning scenario evaluations, not software unit tests. Completed with findings means the evaluation ran and found a limitation. Not applicable is never counted as passed.</p>'+table(['Stage', 'Evaluation', 'Execution / outcome', 'Evidence'], ((r['version'],r['name'],r['status'],r['details']) for r in checks))+table(['Audit', 'Criterion', 'Value'], ((name,label(k),v) for name in ['audit','independent_audit'] for k,v in solver.get(name, {}).items())))
    stress = study.get('stress', [])
    section('stress', '5. Hourly stress tests (v4.2)', '<p>Each stress day is repeated 365 times for conditional annualization only; event probabilities are not estimated. A redesign for one case does not ensure feasibility across all cases.</p>'+table(['Scenario / changes', 'Data type', 'Fixed-design outcome', 'Fixed annual cost USD', 'New-design outcome', 'New annual cost USD', 'New cooling kW'], ((r['name']+': '+r.get('description',''),r.get('label'),r.get('status'),r.get('annual_cost_usd'),r.get('redesign',{}).get('status'),r.get('redesign',{}).get('annual_cost_usd'),r.get('redesign',{}).get('capacities',{}).get('cooling_kw')) for r in stress))+'<h3>Shared equipment across all stress days</h3>'+table(['Metric','Result'], ((label(k),v) for k,v in study.get('stress_joint_redesign',{}).items()))+'<h3>Cooling-only upgrade diagnostic</h3>'+table(['Scenario','Diagnostic','Result'], ((r['name'],label(k),v) for r in stress for k,v in r.get('cooling_only_upgrade',{}).items())))
    section('faults', '6. Minute-level fault tests (v4.3)', '<p>Faults replay the baseline design and starting battery SOC. A conservation audit can pass while load is unserved. These tests do not estimate failure probabilities or transient stability.</p>'+table(['Fault','Start hour','Duration min','Outcome','Unserved kWh','UPS shortfall kWh','Conservation audit','Applicability note'], ((r.get('event',{}).get('name'),r.get('event',{}).get('start_hour'),r.get('event',{}).get('duration_min'),r.get('status'),r.get('unserved_kwh'),r.get('ups_bridge_shortfall_kwh'),r.get('audit_passed'),r.get('applicability_reason','')) for r in study.get('faults',[])))+'<h3>Battery precharge mitigation</h3>'+table(['Metric','Result'], ((label(k),v) for k,v in study.get('fault_mitigation',{}).items())))
    bargaining = study.get('bargaining',{})
    section('tradeoffs', '7. Candidate comparison and negotiation (v4.4)', '<p>Candidate IDs refer to audited alternatives on the same representative baseline. The baseline equipment above is not silently replaced by a negotiated candidate. This finite comparison is not a global Pareto frontier or game equilibrium.</p>'+table(['ID','Solver objective','Annual cost USD','Green share %','Training delay GPU-hour-hours','Worst service gap kWh','Audited'], ((r['id'],r.get('solver_objective'),r.get('annual_cost_usd'),(r.get('green_fraction') or 0)*100,r.get('delay_gpu_hour_hours'),r.get('worst_service_gap_proxy_kwh'),r.get('verified')) for r in bargaining.get('candidates',[])))+''.join('<h3>'+esc(label(name))+'</h3>'+table(['Round','Threshold','Accepted candidates','Selected candidate'], ((r.get('round'),r.get('agent_threshold'),r.get('accepted_candidate_ids'),r.get('selected_candidate_id')) for r in bargaining.get(name,{}).get('rounds',[]))) for name in ['equal_preferences','reliability_first'])+'<h3>Equipment in each candidate</h3>'+table(['Candidate','Equipment','Quantity','Unit'], ((r['id'],v[0],r.get('capacities',{}).get(k),v[1]) for r in bargaining.get('candidates',[]) for k,v in CAPACITIES.items())))
    replanning = study.get('replanning',{})
    section('replanning', '8. Demand-change replanning (v4.5)', '<p>'+esc(replanning.get('trigger','Not run'))+'</p>'+table(['Comparison','Solver status','Audited','Annual cost USD','Green share %','Worst service gap kWh'], ((label(k),v.get('status'),v.get('verified'),v.get('annual_cost_usd'),(v.get('green_fraction') or 0)*100,v.get('worst_service_gap_proxy_kwh')) for k,v in replanning.items() if isinstance(v,dict)))+'<h3>Equipment after demand change</h3>'+table(['Equipment','Old investment on new demand','New optimized investment','Unit'], ((v[0],replanning.get('old_design_on_new_input',{}).get('capacities',{}).get(k),replanning.get('new_design_on_new_input',{}).get('capacities',{}).get(k),v[1]) for k,v in CAPACITIES.items())))
    columns = [k for k in ['scenario','hour','load','grid','pv_load','pv_charge','charge','discharge','ep_after','eg_after','temp','cool','training_proc','proc'] if k in hourly.columns]
    section('operation', '9. Hourly operating plan', '<p>Perfect-foresight representative-day dispatch. Power is kW; ep_after and eg_after are end-of-hour solar-origin and grid-origin battery energy (kWh). Temp is °C; cool is cooling load (kW); training_proc and proc are training and total GPU-hours processed in the hour. The complete dispatch and training schedule are in the structured record.</p>'+table([label(k) for k in columns], ([r[k] for k in columns] for r in record['hourly_dispatch'])))
    notes = context.get('assumptions', []) + [r.get('impact','') for r in context.get('ignored',[])] + context.get('research',{}).get('errors',[])
    section('sources', '10. Assumptions, sources and scope', '<ul>'+''.join('<li>'+esc(n)+'</li>' for n in notes)+'</ul><p>Research scenario, not site certification, a reliability guarantee or real-time control. Financial figures use illustrative model economics.</p><pre>'+esc(json.dumps(manifest,indent=2,ensure_ascii=False,default=float))+'</pre>')
    section('record', '11. Complete reproducible record', '<details><summary>Expand solver, dispatch, scheduling and v4 evaluation data</summary><pre>'+esc(json.dumps(record,indent=2,ensure_ascii=False,default=float))+'</pre></details>')
    css = 'body{font:15px/1.6 system-ui,sans-serif;color:#142b3b;background:#f3f6f8;margin:0}main{max-width:1180px;margin:auto;padding:36px 24px}header,section{background:white;border:1px solid #dce5eb;border-radius:14px;padding:24px;margin-bottom:20px}h1{font-size:32px;line-height:1.2}h2{font-size:22px}h3{font-size:16px}nav{display:flex;gap:14px;flex-wrap:wrap;margin:20px 0}a{color:#116c65}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e1e8ec;vertical-align:top}th{background:#edf4f7}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.muted{color:#627383}li{margin-bottom:8px}.metrics{display:flex;gap:24px;flex-wrap:wrap}.metric strong{font-size:24px;display:block}@media print{body{background:white}main{padding:0}header,section{border-radius:0;break-inside:auto}nav{display:none}thead{display:table-header-group}tr{break-inside:avoid}details pre{display:block}a{color:inherit}}'
    html = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'"><title>AIDC Design Proposal</title><style>'+css+'</style></head><body><main><header><p>AIDC PLANNING SUITE · '+esc(study.get('version'))+'</p><h1>Design proposal · '+esc(point.get('name','Site'))+'</h1><p>Generated '+esc(generated)+' · Interpretation model: '+esc(model)+'</p><div class="metrics"><div class="metric"><span>Annual cost · USD/year</span><strong>'+esc(baseline.get('annual_cost_usd'))+'</strong></div><div class="metric"><span>CAPEX · USD</span><strong>'+esc(solver.get('capex_usd'))+'</strong></div><div class="metric"><span>Green electricity · %</span><strong>'+esc((baseline.get('green_fraction') or 0)*100)+'</strong></div></div><nav>'+''.join('<a href="#'+id+'">'+name+'</a>' for id,name in [('equipment','Equipment'),('actions','Findings'),('validation','v4 execution'),('stress','Stress'),('faults','Faults'),('tradeoffs','Alternatives'),('replanning','Replanning'),('sources','Sources')])+'</nav></header>'+''.join(sections)+'</main></body></html>'
    return {'html':html, 'validation':checks, 'recommendations':recommendations,
            'summary':{'site':point.get('name','Site'), 'annual_cost_usd':baseline.get('annual_cost_usd'), 'capex_usd':solver.get('capex_usd'), 'green_fraction':baseline.get('green_fraction'), 'capacities':capacities},
            'evaluation':study, 'generated_utc':generated}
