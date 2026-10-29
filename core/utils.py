"""Shared data structures, text parsing, scenarios, and independent audits."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import json
import math
import os
import time
from contextvars import ContextVar

REQUEST_AGENT = ContextVar("request_agent", default=None)
import numpy as np


class Expr:
    """Small affine expression builder; no generated code is evaluated."""
    def __init__(self, d=None, c=0.):
        self.d, self.c = d or {}, float(c)

    def __add__(self, b):
        b = b if isinstance(b, Expr) else Expr(c=b)
        d = self.d.copy()
        for k, v in b.d.items():
            d[k] = d.get(k, 0.) + v
        return Expr(d, self.c + b.c)

    __radd__ = __add__

    def __neg__(self):
        return self * -1

    def __sub__(self, b):
        return self + (-b)

    def __rsub__(self, b):
        return -self + b

    def __mul__(self, b):
        return Expr({k: v * float(b) for k, v in self.d.items()}, self.c * b)

    __rmul__ = __mul__

    def __truediv__(self, b):
        return self * (1 / b)

    def value(self, x):
        return self.c + sum(v * x[k] for k, v in self.d.items())



@dataclass(frozen=True)
class Boundary:
    min_gpus: int = 2000
    max_gpus: int = 4504
    budget_usd: float = 150_000_000.
    roof_m2: float = 30_000.
    grid_kw: float = 6500.
    renewable_fraction: float = .4
    critical_fraction: float = .75
    backup_hours: float = 4.
    ups_minutes: float = 15.
    redundancy: int = 1
    max_delay_h: int = 4
    utilization: float = .85
    growth: float = 1.
    cooling_cop: float = 4.
    thermal_mass_kwh_per_c: float = 500.
    thermal_decay: float = .92
    soft_temp_c: float = 27.
    hard_temp_c: float = 35.
    training_share: float = .8
    fluctuation: bool = True
    burst_duration_h: float = 1/60
    allow_diesel: bool = True

    def validate(self):
        for k, v in asdict(self).items():
            if isinstance(v, (int, float)) and not np.isfinite(v):
                raise ValueError(f'{k}: nonfinite')
        for k in ('min_gpus', 'max_gpus', 'max_delay_h', 'redundancy'):
            if type(getattr(self, k)) is not int:
                raise ValueError(f'{k}: integer required')
        if not (0 < self.min_gpus <= self.max_gpus and self.redundancy >= 0 and
                0 <= self.max_delay_h < 24):
            raise ValueError('invalid count or delay')
        for k in ('renewable_fraction', 'critical_fraction', 'training_share'):
            if not 0 <= getattr(self, k) <= 1:
                raise ValueError(k)
        for k in ('budget_usd', 'roof_m2', 'grid_kw', 'backup_hours', 'ups_minutes',
                  'cooling_cop', 'thermal_mass_kwh_per_c', 'burst_duration_h'):
            if getattr(self, k) <= 0:
                raise ValueError(k)
        if not (0 < self.utilization <= 1 and self.growth >= 1 and
                0 < self.thermal_decay < 1 and 18 < self.soft_temp_c < self.hard_temp_c):
            raise ValueError('invalid thermal/utilization/growth boundary')



def _crf(years, rate=.08):
    return rate*(1+rate)**years/((1+rate)**years-1)



def audit_plan(result, df, jobs):
    """Recompute physical relations from exported dispatch, not solver constraints."""
    b = Boundary(**result['boundary']); c = result['capacities']
    positive = lambda x: float(max(0,np.max(np.asarray(x))))
    absmax = lambda x: float(np.max(np.abs(np.asarray(x))))
    errors = {
        'power_balance': absmax(df.grid_load+df.pv_load+df.pv_dis+df.grid_dis-df.load),
        'pv_balance': absmax(df.pv_load+df.pv_charge+df.pv_curtail-df.pv_available),
        'pv_soc': absmax(df.ep_after-df.ep_before-.95*df.pv_charge+df.pv_dis/.95),
        'grid_soc': absmax(df.eg_after-df.eg_before-.95*df.grid_charge+df.grid_dis/.95),
        'thermal_balance': absmax(df.temp-b.thermal_decay*df.temp_before-(1-b.thermal_decay)*df.ambient
                                 -(df.it+df.heat-df.cool*b.cooling_cop)/b.thermal_mass_kwh_per_c),
        'temperature_limit': positive(df.temp-b.hard_temp_c),
        'battery_power': positive(np.maximum(df.charge,df.discharge)+df.burst-c['battery_kw']),
        'simultaneous_charge_discharge': positive(np.minimum(df.charge,df.discharge)),
        'green_target': positive(b.renewable_fraction-(df.green*df.weight).sum()/(df.load*df.weight).sum()),
        'gpu_capacity': positive(df.proc-(result['gpu_count']-8*b.redundancy)*b.utilization),
        'grid_limit': positive(df.grid-b.grid_kw),
        'cooling_capacity': positive(df.cool-c['cooling_kw']),
        'budget': positive(result['capex_usd']-b.budget_usd),
        'deadline': positive(jobs.release-jobs.arrival-b.max_delay_h),
        'early_execution': positive(jobs.arrival-jobs.release),
        'reserve_soc': positive(.1*c['battery_kwh']+result['reserve_kwh']+df.burst*b.burst_duration_h/.95-df.ep_before-df.eg_before),
        'max_soc': positive(df.ep_before+df.eg_before-.9*c['battery_kwh']),
    }
    for s, d in df.groupby('scenario'):
        j = jobs[jobs.scenario==s]
        errors[f'{s}_job_conservation'] = absmax(j.groupby('arrival').gpu_hours.sum().reindex(range(len(d)),fill_value=0).to_numpy()-d.training_arrivals.to_numpy())
        dispatched = j.assign(dispatch_hour=j.release % len(d)).groupby('dispatch_hour').gpu_hours.sum().reindex(range(len(d)),fill_value=0).to_numpy()
        errors[f'{s}_processed_conservation'] = absmax(dispatched+d.inference_arrivals.to_numpy()-d.proc.to_numpy())
        if result.get('variant') != 'v3.1':
            errors[f'{s}_backlog_balance'] = absmax(d.backlog_after-d.backlog_before-d.training_arrivals+d.training_proc)
            errors[f'{s}_backlog_cyclic'] = abs(float(d.backlog_before.iloc[0]-d.backlog_after.iloc[-1]))
        for source in ('ep','eg'):
            errors[f'{s}_{source}_cyclic'] = abs(float(d[f'{source}_before'].iloc[0]-d[f'{source}_after'].iloc[-1]))
        errors[f'{s}_temp_cyclic'] = abs(float(d.temp_before.iloc[0]-d.temp.iloc[-1]))
    critical=(result['gpu_count']*.85*1.3+50)*b.critical_fraction
    errors['outage_balance']=abs(result['outage_diesel_kw']+result['outage_battery_kw']-critical)
    errors['outage_fuel']=positive(result['outage_diesel_kw']*b.backup_hours*.27-c['fuel_litres'])
    errors['outage_energy']=positive(result['outage_battery_kw']*b.backup_hours/.95-result['reserve_kwh'])
    errors['outage_inverter']=positive(result['outage_battery_kw']-c['battery_kw'])
    errors['outage_diesel_capacity']=positive(result['outage_diesel_kw']-max(0,c['diesel_units']-b.redundancy)*1000)
    errors['ups_power']=positive(critical-(c['ups_units']-b.redundancy)*500)
    errors['ups_energy']=positive(critical*b.ups_minutes/60-c['ups_battery_kwh']*.8*.97)
    errors['rated_transformer']=positive(critical/max(b.critical_fraction,1e-12)-(c['transformer_units']-b.redundancy)*760) if b.critical_fraction else 0.
    errors['operating_transformer']=positive(df.grid-(c['transformer_units']-b.redundancy)*760)
    return errors



SCALES = [20e6, 20e6, 10e6, 500000.]



UNIT_COST=dict(servers=250000.,pv_kw=900.,battery_kw=120.,battery_kwh=250.,transformer_units=90000.,
               ups_units=120000.,ups_battery_kwh=400.,diesel_units=350000.,fuel_litres=1.,cooling_kw=500.)



YEARS=dict(servers=4,pv_kw=25,battery_kw=10,battery_kwh=10,transformer_units=25,ups_units=10,
           ups_battery_kwh=10,diesel_units=20,fuel_litres=20,cooling_kw=15)



def independent_audit(pack,scenarios,fixed=None):
    r,d,j=pack
    if d.empty:return dict(passed=False,reason='no incumbent')
    errors=audit_plan(r,d,j);c=r['capacities'];b=Boundary(**r['boundary'])
    mx=lambda x:float(np.max(np.abs(np.asarray(x))))
    errors['capex_recomputed']=abs(r['capex_usd']-sum(c[k]*v for k,v in UNIT_COST.items()))
    ann=sum(c[k]*UNIT_COST[k]*(.08*1.08**YEARS[k]/(1.08**YEARS[k]-1)+.02) for k in c)
    ann+=r['outage_diesel_kw']*b.backup_hours*.27*1.2+r['reserve_kwh']/.95*.12
    ann+=float((d.weight*(d.price*d.grid+.015*(d.charge+d.discharge))).sum())
    errors['annual_cost_recomputed']=abs(ann-r['annual_cost_usd'])
    if r.get('variant') == 'v3.1':
        errors['it_power']=mx(d.it-r['gpu_count']*.29-d.proc*.56)
    else:
        errors['it_power']=mx(d.it-r['gpu_count']*.29-d.training_proc*(r.get('active_train_gpu_kw',.7)-.14)-d.inference_arrivals*.56)
    errors['load_composition']=mx(d.load-d.it-d.cool-d.heat-50)
    errors['grid_composition']=mx(d.grid-d.grid_load-d.grid_charge)
    errors['green_composition']=mx(d.green-d.pv_load-d.pv_dis)
    errors['charge_composition']=mx(d.charge-d.pv_charge-d.grid_charge)
    errors['discharge_composition']=mx(d.discharge-d.pv_dis-d.grid_dis)
    errors['gpu_count']=abs(r['gpu_count']-8*c['servers'])
    errors['fixed_gpu_scope']=max(0.,b.min_gpus-r['gpu_count'],r['gpu_count']-b.max_gpus)
    errors['equipment_integrality']=max(abs(c[k]-round(c[k])) for k in ('servers','transformer_units','ups_units','diesel_units'))
    errors['roof']=max(0.,c['pv_kw']-b.roof_m2/5)
    errors['battery_duration']=max(0.,c['battery_kw']-c['battery_kwh'],c['battery_kwh']-8*c['battery_kw'])
    errors['nonnegative']=max(0.,-float(d[['grid_load','grid_charge','pv_load','pv_charge','pv_curtail','pv_dis','grid_dis','ep_before','eg_before']].min().min()),-float(j.gpu_hours.min()),-min(c.values()))
    errors['scenario_count']=abs(len(d)-24*len(scenarios))
    errors['scenario_names']=float(set(d.scenario)!=set(s['name'] for s in scenarios))
    for s in scenarios:
        dd=d[d.scenario==s['name']].sort_values('hour')
        if len(dd)!=24:errors['scenario_count']=1;continue
        for key,col in [('arrivals','arrivals'),('price','price'),('ambient','ambient')]:errors[s['name']+'_'+key]=mx(dd[col]-np.asarray(s[key]))
        if 'training_arrivals' in s:
            errors[s['name']+'_training_input']=mx(dd.training_arrivals-np.asarray(s['training_arrivals']))
            errors[s['name']+'_inference_input']=mx(dd.inference_arrivals-np.asarray(s['inference_arrivals']))
        errors[s['name']+'_pv_input']=mx(dd.pv_available-c['pv_kw']*np.asarray(s['pv']))
        errors[s['name']+'_weight']=mx(dd.weight-s['weight'])
        errors[s['name']+'_burst']=mx(dd.burst-r['gpu_count']*s['burst']*b.training_share)
        for state in ('ep','eg','temp'):
            before=state+'_before';after=state+'_after' if state!='temp' else 'temp'
            errors[s['name']+'_'+state+'_continuity']=mx(dd[before].to_numpy()[1:]-dd[after].to_numpy()[:-1])
    brown=float((d.weight*(d.load-d.green)).sum())
    wait=sum(float(d[d.scenario==s].weight.iloc[0]) * float(((g.release-g.arrival)*g.gpu_hours).sum()) for s,g in j.groupby('scenario'))
    errors['brown_metric']=abs(brown-r['metrics']['brown'])
    errors['delay_metric']=abs(wait-r['metrics']['delay'])
    excess=np.maximum(d.temp.to_numpy()-b.soft_temp_c,0)
    knots=np.linspace(0,b.hard_temp_c-b.soft_temp_c,9)
    risk=float((d.weight.to_numpy()*np.max(np.stack([excess*(lo+hi)-lo*hi for lo,hi in zip(knots[:-1],knots[1:])]),axis=0)).sum())
    # An epigraph may exceed its minimal value within solver tolerance; it must
    # never understate thermal exposure. Exported physical square is exact.
    errors['thermal_epigraph_understatement']=max(0.,risk-r['metrics']['risk'])
    errors['thermal_square_metric']=abs(float((d.weight.to_numpy()*excess**2).sum())-r['metrics']['thermal_squared_degree_hours'])
    errors['thermal_objective']=abs(r['objectives'][3]-r['metrics']['risk']/SCALES[3])
    errors['objective_metrics']=mx(np.asarray(r['objectives'][:3])-np.asarray([ann,brown,wait])/SCALES[:3])
    errors['fixed_capacities']=max(abs(c[k]-fixed[k]) for k in c) if fixed else 0.
    return dict(passed=bool(all(np.isfinite(v) and v<=1e-4 for v in errors.values())),errors=errors,
                scope='independent exported-data audit plus legacy solver matrix audit; not AC/transient certification')


















def make_scenarios(boundary, inputs: dict) -> list[dict]:
    """One explicit-input representative day with documented analytic shapes."""
    hours = range(24)
    train = [.64 + .08 * math.cos(2 * math.pi * (h - 12) / 24) for h in hours]
    infer = [.38 + .28 * (h >= 18 or h < 6) for h in hours]
    return [{
        "name": "user_input_representative_day", "weight": 365.0,
        "arrivals": [boundary.min_gpus * boundary.growth *
                     (boundary.training_share * train[h] + (1 - boundary.training_share) * infer[h])
                     for h in hours],
        "pv": [max(0.0, inputs["pv_peak_cf"] * math.sin(math.pi * (h - 6) / 12)) for h in hours],
        "price": [inputs["price_usd_kwh"]] * 24,
        "ambient": [inputs["ambient_mean_c"] + inputs["ambient_swing_c"] *
                    math.sin(2 * math.pi * (h - 8) / 24) for h in hours],
        "burst": inputs["burst_kw_gpu"],
    }]



class APIAgent:
    """Uses the same OpenRouter LangChain route as week7. Environment-only keys."""
    def __init__(self, model=None):
        self.model=model or os.environ.get('OPENROUTER_MODEL','deepseek/deepseek-v4.1-flash')

    def complete(self, system, user):
        agent = REQUEST_AGENT.get()
        if agent is not None:
            return agent.complete(system, user)
        if not os.environ.get('OPENROUTER_API_KEY','').strip():
            raise RuntimeError('OPENROUTER_API_KEY is missing')
        from langchain_openrouter import ChatOpenRouter
        client=ChatOpenRouter(model=self.model,temperature=0).bind(timeout_ms=120_000,retries=None)
        start=time.perf_counter()
        response=client.invoke([('system',system),('human',user)])
        content=response.content
        if not isinstance(content,str):
            content=''.join(x.get('text','') for x in content if isinstance(x,dict))
        return {'raw':content,'seconds':time.perf_counter()-start,'model':self.model,
                'usage':getattr(response,'usage_metadata',None),'provider':'OpenRouter'}



def parse_json(text):
    s=text.strip()
    if s.startswith('```'):
        s=s.split('\n',1)[1].rsplit('```',1)[0].strip()
    return json.loads(s)

