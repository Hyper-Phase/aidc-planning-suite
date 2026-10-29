"""Step 3: construct the four-objective MILP and solve with SciPy or COPT."""
from __future__ import annotations
from dataclasses import asdict
from types import SimpleNamespace
import time
import numpy as np
import pandas as pd
from scipy.optimize import milp, Bounds, LinearConstraint
from scipy.sparse import coo_matrix
from utils import Boundary, Expr, _crf, audit_plan, independent_audit, make_scenarios
from remote_features import collect
from scenario_builder import build_scenarios
from variants import VERSIONS


def solve_milp(model, coefficients, seconds):
    """Use the same coefficient matrix and bounds with either MILP backend."""
    if model.backend == "scipy":
        return milp(coefficients, integrality=np.array(model.integer),
                    bounds=Bounds(model.lb, model.ub),
                    constraints=LinearConstraint(model.A, model.lo, model.hi),
                    options={"time_limit": seconds, "mip_rel_gap": 1e-5})
    if model.backend != "copt":
        raise ValueError(f"Unknown solver backend: {model.backend}")
    import coptpy as cp
    from coptpy import COPT

    with cp.Envr() as env:
        with env.createModel("aidc_planning") as problem:
            problem.setParam(COPT.Param.TimeLimit, seconds)
            problem.setParam(COPT.Param.RelGap, 1e-5)
            variables = [problem.addVar(lb=float(lo) if np.isfinite(lo) else -COPT.INFINITY,
                                        ub=float(hi) if np.isfinite(hi) else COPT.INFINITY,
                                        vtype=COPT.INTEGER if integer else COPT.CONTINUOUS,
                                        name=name)
                         for name, lo, hi, integer in zip(model.names, model.lb, model.ub, model.integer)]
            for row, lo, hi in zip(model.rows, model.lo, model.hi):
                expr = cp.quicksum(float(value) * variables[index] for index, value in row.items())
                if np.isfinite(lo) and np.isfinite(hi) and lo == hi:
                    problem.addConstr(expr == float(lo))
                else:
                    if np.isfinite(lo):
                        problem.addConstr(expr >= float(lo))
                    if np.isfinite(hi):
                        problem.addConstr(expr <= float(hi))
            problem.setObjective(cp.quicksum(float(value) * variables[index]
                                             for index, value in enumerate(coefficients) if value),
                                 sense=COPT.MINIMIZE)
            problem.solve()
            optimal = problem.status == COPT.OPTIMAL
            infeasible = problem.status == COPT.INFEASIBLE
            return SimpleNamespace(status=0 if optimal else 2 if infeasible else 1,
                                   x=np.array([var.x for var in variables]) if optimal else None,
                                   mip_gap=float(problem.bestgap) if optimal else None,
                                   message=f"COPT status {problem.status}")


class LinearModel:
    def __init__(self, backend='scipy'):
        self.backend = backend
        self.names, self.lb, self.ub, self.integer = [], [], [], []
        self.rows, self.lo, self.hi = [], [], []

    def var(self, name, lb=0., ub=np.inf, integer=False):
        i = len(self.names)
        self.names.append(name); self.lb.append(lb); self.ub.append(ub)
        self.integer.append(int(integer))
        return Expr({i: 1.})

    def con(self, expr, lo=-np.inf, hi=np.inf):
        self.rows.append(expr.d); self.lo.append(lo-expr.c); self.hi.append(hi-expr.c)

    def eq(self, a, b=0.):
        self.con(a-b, 0., 0.)

    def le(self, a, b):
        self.con(a-b, hi=0.)

    def solve(self, objective, seconds=40):
        rr, cc, vv = [], [], []
        for i, d in enumerate(self.rows):
            for j, v in d.items():
                rr.append(i); cc.append(j); vv.append(v)
        self.A = coo_matrix((vv, (rr, cc)), shape=(len(self.rows), len(self.names))).tocsc()
        c = np.zeros(len(self.names))
        for k, v in objective.d.items():
            c[k] = v
        start = time.perf_counter()
        r = solve_milp(self, c, seconds)
        return r, time.perf_counter()-start



def solve_plan(b=None, weights=(1., 0., 0., 0.), epsilon=None, fixed=None, scenarios=None, minimax=None, green_ratio_reference=None, backend='scipy', variant='v3.1', features=None):
    """Joint integer capacity / continuous workload scheduling MILP.

    weights apply to [annual cost/20M, brown kWh/20M, wait GPU-h*h/10M,
    thermal degree^2*h/500000]. epsilon keys are these normalized objectives.
    Optional fixed freezes ALL investment variables for recourse evaluation.
    """
    b = b or Boundary(); b.validate()
    flags = VERSIONS[variant]
    features = features or {}
    weights = np.asarray(weights, float)
    if weights.shape != (4,) or not np.isfinite(weights).all() or (weights < 0).any() or not weights.sum():
        raise ValueError('four finite nonnegative weights required')
    if scenarios is None:
        raise ValueError('planning requires explicit scenarios')
    meta = {}
    if not scenarios or not np.isclose(sum(s['weight'] for s in scenarios),365):
        raise ValueError('scenario day weights must sum to 365')
    if len({s['name'] for s in scenarios})!=len(scenarios):
        raise ValueError('scenario names must be unique')
    for s in scenarios:
        arrays=[np.asarray(s[k],float) for k in ('arrivals','pv','price','ambient')]
        if any(a.shape!=(24,) or not np.isfinite(a).all() for a in arrays):
            raise ValueError('each scenario requires 24 finite hourly observations')
        if (arrays[0]<0).any() or (arrays[1]<0).any() or (arrays[1]>1).any() or (arrays[2]<0).any():
            raise ValueError('negative workload/price or invalid PV factor')
        if not np.isfinite(s['weight']) or s['weight']<=0 or not np.isfinite(s['burst']) or s['burst']<0:
            raise ValueError('invalid scenario weight/burst')
    m = LinearModel(backend)
    spec = {'servers': (np.ceil(b.min_gpus/8), b.max_gpus//8, True),
            'pv_kw': (0, b.roof_m2/5, False), 'battery_kw': (0, 12000, False),
            'battery_kwh': (0, 60000, False), 'transformer_units': (b.redundancy+1, 20, True),
            'ups_units': (b.redundancy+1, 30, True), 'ups_battery_kwh': (0, 20000, False),
            'diesel_units': (0, 20 if b.allow_diesel else 0, True),
            'fuel_litres': (0, 100000, False), 'cooling_kw': (0, 5000, False)}
    if fixed is not None and set(fixed) != set(spec):
        raise ValueError('fixed design must contain every investment variable')
    cap = {k: m.var(k, *v) for k,v in spec.items()}
    if fixed is not None:
        for k in cap:
            m.eq(cap[k], float(fixed[k]))
    n = cap['servers']*8
    # Adopt week7 equipment costs. Cooling is a new, illustrative capital item.
    unit_cost = dict(servers=250000., pv_kw=900., battery_kw=120., battery_kwh=250.,
                    transformer_units=90000., ups_units=120000., ups_battery_kwh=400.,
                    diesel_units=350000., fuel_litres=1., cooling_kw=500.)
    years = dict(servers=4, pv_kw=25, battery_kw=10, battery_kwh=10,
                 transformer_units=25, ups_units=10, ups_battery_kwh=10,
                 diesel_units=20, fuel_litres=20, cooling_kw=15)
    capital = sum(cap[k]*unit_cost[k] for k in cap)
    annual = sum(cap[k]*unit_cost[k]*(_crf(years[k])+.02) for k in cap)
    m.le(capital, b.budget_usd)
    m.le(cap['battery_kw'], cap['battery_kwh'])
    m.le(cap['battery_kwh'], cap['battery_kw']*8)
    # Conservative rated-load sizing, retained in BOTH nominal and burst variants.
    design = n*.85*1.3+50
    critical = design*b.critical_fraction
    m.le(design, (cap['transformer_units']-b.redundancy)*1000*.8*.95)
    m.le(critical, (cap['ups_units']-b.redundancy)*500)
    m.le(critical*b.ups_minutes/60, cap['ups_battery_kwh']*.8*.97)
    diesel_on = m.var('diesel_on', ub=1, integer=True)
    m.le(cap['diesel_units'], diesel_on*20)
    m.le(diesel_on, cap['diesel_units'])
    if b.ups_minutes < 1:
        m.eq(diesel_on, 0.)
    dg = m.var('night_outage_diesel_kw')
    od = m.var('night_outage_battery_kw')
    reserve = m.var('reserved_battery_kwh')
    m.eq(dg+od, critical)
    m.le(dg, (cap['diesel_units']-b.redundancy*diesel_on)*1000)
    m.le(od, cap['battery_kw'])
    m.eq(reserve, od*b.backup_hours/.95)
    m.le(reserve, cap['battery_kwh']*.8)
    m.le(dg*b.backup_hours*.27, cap['fuel_litres'])
    annual += dg*b.backup_hours*.27*1.2  # one outage per year, illustrative
    annual += reserve/.95*.12  # explicit recharge allowance, not a scheduled recovery model
    totals = {k: Expr() for k in ('brown','delay','risk','load','green','grid','pv','charge','discharge')}
    records, jobs = [], []
    for s in scenarios:
        T = len(s['arrivals']); w = s['weight']
        # v3.2 onward: distinct GPU-hour arrivals and a cyclic 24-hour backlog.
        allocation = {}
        for a in range(T):
            slots = range(a, min(T + b.max_delay_h, a+b.max_delay_h+1) if flags['cross_day']
                          else min(T, a+b.max_delay_h+1))
            for t in slots:
                allocation[a,t] = m.var(f'{s["name"]}_job_{a}_{t}')
                totals['delay'] += w*(t-a)*allocation[a,t]
                jobs.append((s['name'], a, t, allocation[a,t]))
            train_arrival = s['training_arrivals'][a] if flags['separate_jobs'] else b.training_share*s['arrivals'][a]
            m.eq(sum(allocation[a,t] for t in slots), train_arrival)
        backlog = [m.var('training_backlog') for _ in range(T+1)] if flags['cross_day'] else None
        if backlog:
            m.eq(backlog[0], sum(value for (a,t),value in allocation.items() if t >= T))
            m.eq(backlog[-1], backlog[0])
        ep = [m.var('pv_soc') for _ in range(T+1)]
        eg = [m.var('grid_soc') for _ in range(T+1)]
        temp = [m.var('temperature', 18, b.hard_temp_c) for _ in range(T+1)]
        m.eq(ep[-1], ep[0]); m.eq(eg[-1], eg[0]); m.eq(temp[-1], temp[0])
        burst = n*float(s['burst'])*b.training_share
        for t in range(T+1):
            m.le(ep[t]+eg[t], cap['battery_kwh']*.9)
            m.le(cap['battery_kwh']*.1+reserve+burst*b.burst_duration_h/.95, ep[t]+eg[t])
        for t in range(T):
            train_proc = sum(value for (a, tt), value in allocation.items() if tt % T == t)
            infer_arrival = s['inference_arrivals'][t] if flags['separate_jobs'] else (1-b.training_share)*s['arrivals'][t]
            proc = infer_arrival + train_proc
            if backlog:
                m.eq(backlog[t+1], backlog[t] + s['training_arrivals'][t] - train_proc)
            m.le(proc, (n-8*b.redundancy)*b.utilization)
            active_train_kw = features['power']['active_gpu_kw_mean'] if flags['h100_power'] else .7
            it = (n*(.14+.15) + train_proc*(active_train_kw-.14) + infer_arrival*(.7-.14)
                  if flags['separate_jobs'] else n*(.14+.15) + proc*(.7-.14))
            cool = m.var('cool_kw'); m.le(cool, cap['cooling_kw'])
            heat = m.var('resistive_heat_kw', ub=1000) if flags['multi_day'] else Expr()
            load = it + cool + heat + 50
            # Reduced aggregate RC model, with assumed parameters and cyclic thermal state.
            m.eq(temp[t+1], b.thermal_decay*temp[t]+(1-b.thermal_decay)*float(s['ambient'][t])
                 +(it+heat-cool*b.cooling_cop)/b.thermal_mass_kwh_per_c)
            excess = m.var('temperature_excess', ub=b.hard_temp_c-b.soft_temp_c)
            risk = m.var('temperature_squared_excess')
            m.le(temp[t+1]-b.soft_temp_c, excess)
            # Convex piecewise-linear chords upper-bound z^2 on [0, hard-soft].
            knots = np.linspace(0, b.hard_temp_c-b.soft_temp_c, 9)
            for l,r in zip(knots[:-1],knots[1:]):
                m.le(excess*(l+r)-l*r, risk)
            x = {k:m.var(k) for k in ('grid_load','grid_charge','pv_load','pv_charge',
                                      'pv_curtail','pv_dis','grid_dis')}
            charge = x['pv_charge']+x['grid_charge']
            discharge = x['pv_dis']+x['grid_dis']
            grid = x['grid_load']+x['grid_charge']
            mode = m.var('charge_mode', ub=1, integer=True)
            m.le(charge+burst, cap['battery_kw']); m.le(discharge+burst, cap['battery_kw'])
            m.le(charge, mode*12000); m.le(discharge, (1-mode)*12000)
            m.eq(x['pv_load']+x['pv_charge']+x['pv_curtail'], cap['pv_kw']*float(s['pv'][t]))
            m.eq(x['grid_load']+x['pv_load']+discharge, load)
            m.le(grid, b.grid_kw)
            m.le(grid, (cap['transformer_units']-b.redundancy)*1000*.8*.95)
            m.eq(ep[t+1],ep[t]+.95*x['pv_charge']-x['pv_dis']/.95)
            m.eq(eg[t+1],eg[t]+.95*x['grid_charge']-x['grid_dis']/.95)
            green = x['pv_load']+x['pv_dis']  # only delivered on-site renewable energy
            totals['load'] += w*load; totals['green'] += w*green
            totals['brown'] += w*(load-green); totals['grid'] += w*grid
            totals['pv'] += w*(x['pv_load']+x['pv_charge'])
            totals['charge'] += w*charge; totals['discharge'] += w*discharge
            totals['risk'] += w*risk
            annual += w*(float(s['price'][t])*grid+.015*(charge+discharge))
            records.append(dict(scenario=s['name'],hour=t,weight=w,arrivals=float(s['arrivals'][t]),
                                training_arrivals=float(s['training_arrivals'][t]) if flags['separate_jobs'] else b.training_share*float(s['arrivals'][t]),
                                inference_arrivals=float(infer_arrival),training_proc=train_proc,
                                backlog_before=backlog[t] if backlog else 0.,backlog_after=backlog[t+1] if backlog else 0.,
                                proc=proc,it=it,cool=cool,heat=heat,load=load,grid=grid,green=green,
                                temp_before=temp[t],temp=temp[t+1],ambient=float(s['ambient'][t]),
                                ep_before=ep[t],ep_after=ep[t+1],eg_before=eg[t],eg_after=eg[t+1],
                                charge=charge,discharge=discharge,burst=burst,price=float(s['price'][t]),
                                pv_available=cap['pv_kw']*float(s['pv'][t]),**x))
    m.le(totals['load']*b.renewable_fraction, totals['green'])
    raw = [annual, totals['brown'], totals['delay'], totals['risk']]
    scales = np.array([20e6,20e6,10e6,500000.])
    objectives = [f/scale for f,scale in zip(raw,scales)]
    for k, v in (epsilon or {}).items():
        idx = ['cost','brown','delay','risk'].index(k)
        if not np.isfinite(v) or v<0: raise ValueError('finite nonnegative epsilon required')
        m.le(objectives[idx], float(v))
    obj = sum(float(w)*f for w,f in zip(weights,objectives))
    if green_ratio_reference is not None:
        if not np.isfinite(green_ratio_reference) or not 0 <= green_ratio_reference <= 1:
            raise ValueError('green ratio reference must be between 0 and 1')
        # Dinkelbach subproblem: max(green/load) via min(q*load-green).
        obj = (float(green_ratio_reference)*totals['load']-totals['green'])/20e6
    if minimax is not None:
        ideal,span=map(lambda a:np.asarray(a,float),minimax)
        if ideal.shape!=(4,) or span.shape!=(4,) or not np.isfinite(ideal).all() or not np.isfinite(span).all() or (span<=0).any():
            raise ValueError('four finite ideal values and positive spans required')
        theta=m.var('worst_normalized_regret',lb=-np.inf)
        for j in range(4):m.le((objectives[j]-ideal[j])/span[j],theta)
        obj=theta
    # HiGHS drops extremely tiny costs; scale the WHOLE objective uniformly so
    # dollar-level tank/asset terms survive normalization and tie-breaks.
    r, elapsed = m.solve(obj*1e6)
    primary_gap = float(r.mip_gap) if r.x is not None else None
    primary_value = obj.value(r.x) if r.x is not None else None
    # Explicit second pass instead of numerically invisible tiny penalties.
    # Preserve primary optimum within 1e-7 normalized units, then minimize all
    # normalized metrics to avoid gratuitous tank capacity/epigraph inflation.
    tie_status = 'not_run'
    if r.status==0:
        m.le(obj, primary_value+1e-7)
        tied, extra = m.solve(sum(objectives)*1e6)
        elapsed += extra
        tie_status = 'optimal' if tied.status==0 else 'failed_kept_primary'
        if tied.status==0: r=tied
    result = {'status': 'optimal' if r.status==0 else 'infeasible' if r.status==2 else 'not_proven_optimal',
              'solver_status': int(r.status), 'solver_message': r.message, 'seconds': elapsed,
              'boundary': asdict(b), 'weights': weights.tolist(), 'epsilon': epsilon or {},
              'objective_kind':'green_ratio_subproblem' if green_ratio_reference is not None else 'global_pure_minimax' if minimax is not None else 'weighted_sum',
              'green_ratio_reference':green_ratio_reference,
              'minimax_normalization':{'ideal':ideal.tolist(),'span':span.tolist()} if minimax is not None else None,
              'primary_objective':primary_value,'primary_mip_gap':primary_gap,
              'tie_break':tie_status,'primary_tolerance':1e-7,
              'provenance': meta, 'backend':backend, 'variant':variant,
              'active_train_gpu_kw':features['power']['active_gpu_kw_mean'] if flags['h100_power'] else .7,
              'scope':'reduced active-power MILP; perfect-foresight hourly recourse; illustrative economics/thermal model'}
    if r.x is None:
        return result, pd.DataFrame(), pd.DataFrame()
    v = lambda z: z.value(r.x) if isinstance(z,Expr) else z
    result.update(capacities={k:v(x) for k,x in cap.items()},gpu_count=v(n),capex_usd=v(capital),
                  annual_cost_usd=v(annual),objectives=[v(f) for f in objectives],
                  mip_gap=float(r.mip_gap), reserve_kwh=v(reserve),outage_diesel_kw=v(dg),outage_battery_kw=v(od),
                  metrics={k:v(x) for k,x in totals.items()})
    df = pd.DataFrame([{k:v(x) for k,x in row.items()} for row in records])
    jf = pd.DataFrame([(s,a,t,v(z)) for s,a,t,z in jobs], columns=['scenario','arrival','release','gpu_hours'])
    result['metrics']['green_fraction_delivered'] = v(totals['green'])/v(totals['load'])
    result['metrics']['E_rec_supply_ratio'] = v(totals['pv'])/v(totals['grid']+totals['pv']+totals['discharge'])
    result['metrics']['thermal_squared_degree_hours'] = float((df.weight*np.maximum(df.temp-b.soft_temp_c,0)**2).sum())
    weights_by_scenario = {scenario['name']: scenario['weight'] for scenario in scenarios}
    result['metrics']['cross_day_training_gpu_hours'] = float(sum(
        row.gpu_hours * weights_by_scenario[row.scenario] for row in jf.itertuples() if row.release >= 24))
    result['metrics']['training_arrivals_gpu_hours'] = float((df.weight*df.training_arrivals).sum())
    result['metrics']['inference_arrivals_gpu_hours'] = float((df.weight*df.inference_arrivals).sum())
    result['metrics']['carbon_proxy_kg_at_0_4_kg_per_kwh'] = float(v(totals['brown']) * .4)
    result['metrics']['minimum_grid_headroom_kw'] = float(b.grid_kw - df.grid.max())
    if flags['h100_power']:
        delta = features['power']['active_gpu_kw_p95'] - features['power']['active_gpu_kw_mean']
        result['metrics']['p95_power_shock_headroom_kw'] = float(
            (b.grid_kw - df.grid - delta * df.training_proc).min())
    result['audit'] = audit_plan(result,df,jf)
    ax = m.A@r.x
    result['audit']['matrix_max_violation'] = float(max(0,np.max(np.array(m.lo)-ax),np.max(ax-np.array(m.hi))))
    result['audit']['integer_max_error'] = float(np.max(np.abs(r.x[np.array(m.integer,dtype=bool)]-np.round(r.x[np.array(m.integer,dtype=bool)]))))
    result['audit']['passed'] = bool(all(v<=1e-4 for k,v in result['audit'].items() if k!='passed'))
    return result, df, jf



def certified(boundary, scenarios, weights, backend='scipy', variant='v3.1', features=None, **kwargs):
    pack = solve_plan(boundary, weights, scenarios=scenarios, backend=backend,
                      variant=variant, features=features, **kwargs)
    result = pack[0]
    result["independent_audit"] = independent_audit(pack, scenarios)
    result["verified_feasible"] = bool(result.get("status") == "optimal" and result.get("audit", {}).get("passed") and result["independent_audit"]["passed"])
    return pack


def build_and_solve_model(context):
    """Construct the site boundary, representative scenario, and cost baseline."""
    if context["missing"]:
        return None
    facts, limits = context["facts"], context["limits"]
    n = int(facts["gpu_count"])
    boundary = Boundary(min_gpus=n, max_gpus=n, roof_m2=facts["roof_m2"],
                        grid_kw=facts["grid_kw"], backup_hours=facts["backup_hours"],
                        ups_minutes=facts["ups_minutes"], training_share=facts["training_share"],
                        growth=facts["growth"],
                        budget_usd=limits.get("budget_usd", {}).get("value", 1e12),
                        renewable_fraction=limits.get("renewable_fraction", {}).get("value", 0.),
                        max_delay_h=int(limits.get("max_delay_h", {}).get("value", 23.)))
    variant = context.get("variant", "v3.1")
    point = context.get("research", {}).get("point") or context.get("point")
    features = context.get("features")
    if features is None:
        features = collect(variant, point=point)
        context["features"] = features
    scenarios = build_scenarios(boundary, facts, variant, features)
    if boundary.max_delay_h == 0:
        hourly_capacity = (n - 8 * boundary.redundancy) * boundary.utilization
        peak_arrival = max(
            max(train + infer for train, infer in zip(s["training_arrivals"], s["inference_arrivals"]))
            if "training_arrivals" in s else max(s["arrivals"])
            for s in scenarios
        )
        if peak_arrival > hourly_capacity + 1e-8:
            context["preflight_issues"] = [
                f"Nondeferrable peak work {peak_arrival:.2f} GPU-h/h exceeds usable "
                f"processing capacity {hourly_capacity:.2f} GPU-h/h."
            ]
            context["solver_status"] = "input_infeasible"
            return None
    cost = certified(boundary, scenarios, (1, 0, 0, 0), backend=context["backend"],
                     variant=variant, features=features)
    context["solver_status"] = cost[0]["status"]
    context["solver_message"] = cost[0].get("solver_message")
    return {"boundary": boundary, "scenarios": scenarios, "cost": cost}

