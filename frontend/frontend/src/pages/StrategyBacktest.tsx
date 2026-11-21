import { useEffect, useMemo, useState } from 'react';
import { Play, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { api, type BacktestRun, type CatalogEntry, type Strategy } from '../services/api';
import { Badge, DataSourceBadge, SebiDisclaimer } from '../components/common/Badges';
import { StoredDataLibrary } from '../components/backtest/StoredDataLibrary';

export function StrategyBacktest() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [selected, setSelected] = useState<number | ''>('');
  const [dataMode, setDataMode] = useState<'tick' | '1m_chain'>('tick');
  const [fromDate, setFromDate] = useState('2026-11-18');
  const [toDate, setToDate] = useState('2026-11-20');
  const [wf, setWf] = useState(true);
  const [isStart, setIsStart] = useState('2026-11-18');
  const [isEnd, setIsEnd] = useState('2026-11-19');
  const [oosStart, setOosStart] = useState('2026-11-20');
  const [oosEnd, setOosEnd] = useState('2026-11-20');
  const [job, setJob] = useState<any>(null);
  const [run, setRun] = useState<BacktestRun | null>(null);
  const [error, setError] = useState('');

  const load = async () => {
    try {
      const [s, c] = await Promise.all([api.get<Strategy[]>('/strategies'), api.get<CatalogEntry[]>('/lakehouse/catalog')]);
      setStrategies(s); setCatalog(c);
      if (s.length && selected === '') setSelected(s[0].id);
    } catch (e: any) { setError(e.message); }
  };
  useEffect(() => { load(); }, []);

  const missingDates = useMemo(() => {
    if (!fromDate || !toDate) return [];
    const have = new Set(catalog.filter(c => c.data_mode === dataMode).map(c => c.trade_date));
    const out: string[] = [];
    const d = new Date(fromDate + 'T00:00:00');
    const end = new Date(toDate + 'T00:00:00');
    while (d <= end) {
      const iso = d.toISOString().slice(0, 10);
      if (!have.has(iso)) out.push(iso);
      d.setDate(d.getDate() + 1);
    }
    return out;
  }, [catalog, dataMode, fromDate, toDate]);

  const runBacktest = async () => {
    if (!selected) return;
    setError(''); setJob({ status: 'queued', progress: 0, message: 'Queued...' }); setRun(null);
    try {
      const res = await api.post<any>('/backtest/run', {
        strategy_id: selected, data_mode: dataMode, use_walk_forward: wf,
        is_start: wf ? isStart : fromDate, is_end: wf ? isEnd : toDate,
        oos_start: wf ? oosStart : null, oos_end: wf ? oosEnd : null,
      });
      const poll = async () => {
        const j = await api.get<any>(`/backtest/jobs/${res.job_id}`);
        setJob(j);
        if (j.status === 'completed') {
          const r = await api.get<BacktestRun>(`/backtest/runs/${j.result.run_id}`);
          setRun(r);
          return;
        }
        if (j.status === 'failed') { setError(j.message || 'Backtest failed'); return; }
        setTimeout(poll, 1000);
      };
      poll();
    } catch (e: any) { setError(e.message); setJob(null); }
  };

  const m = run?.metrics || {};
  const wfData = m.walk_forward;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">Strategy Backtest</h1>
          <p className="text-sm text-slate-500">Real Option Tick / 1-Min Chain Data only. No synthetic Black-Scholes pricing. 100% offline from local Parquet lakehouse.</p>
        </div>
        <div className="flex gap-2"><Badge tone="blue">Zero-Copy DuckDB</Badge><Badge tone="purple">Hive Parquet</Badge><Badge tone="green">Offline Ready</Badge></div>
      </div>

      <StoredDataLibrary onUploaded={load} />

      <div className="grid gap-4 lg:grid-cols-[360px_1fr]">
        <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
          <div className="text-sm font-semibold text-slate-900">Backtest Configuration</div>
          <label className="block text-xs text-slate-500">Strategy
            <select value={selected} onChange={e => setSelected(Number(e.target.value))} className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm text-slate-700">
              {strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          <label className="block text-xs text-slate-500">Data Mode
            <select value={dataMode} onChange={e => setDataMode(e.target.value as any)} className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm text-slate-700">
              <option value="tick">High-Frequency Option Tick Data</option>
              <option value="1m_chain">1-Minute Option Chain Data</option>
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs text-slate-500">From <input type="date" value={fromDate} onChange={e=>setFromDate(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm text-slate-700"/></label>
            <label className="text-xs text-slate-500">To <input type="date" value={toDate} onChange={e=>setToDate(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm text-slate-700"/></label>
          </div>
          <label className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={wf} onChange={e=>setWf(e.target.checked)} /> In-Sample / Out-of-Sample (Walk-Forward) Split</label>
          {wf && (
            <div className="grid grid-cols-2 gap-2 rounded-lg border border-slate-200 bg-white p-2">
              <label className="text-[11px] text-slate-500">IS Start <input type="date" value={isStart} onChange={e=>setIsStart(e.target.value)} className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs"/></label>
              <label className="text-[11px] text-slate-500">IS End <input type="date" value={isEnd} onChange={e=>setIsEnd(e.target.value)} className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs"/></label>
              <label className="text-[11px] text-slate-500">OOS Start <input type="date" value={oosStart} onChange={e=>setOosStart(e.target.value)} className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs"/></label>
              <label className="text-[11px] text-slate-500">OOS End <input type="date" value={oosEnd} onChange={e=>setOosEnd(e.target.value)} className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs"/></label>
            </div>
          )}

          {missingDates.length > 0 && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-50 p-2 text-[11px] text-amber-800">
              <div className="flex items-center gap-1 font-semibold"><AlertTriangle size={12}/> Missing dates not cached ({dataMode})</div>
              <div className="mt-1 text-amber-700">{missingDates.slice(0,8).join(', ')}{missingDates.length>8 ? ` +${missingDates.length-8} more` : ''}</div>
              <div className="mt-1 text-amber-800/80">Action: upload CSV/Parquet for these dates or run incremental historical sync when broker is connected.</div>
            </div>
          )}

          <button onClick={runBacktest} disabled={!selected || (job && job.status==='running')}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-sky-600 px-4 py-2.5 text-sm font-semibold text-slate-900 hover:bg-sky-500 disabled:opacity-50">
            <Play size={15} /> Run Backtest (Offline)
          </button>

          {job && (
            <div className="space-y-1">
              <div className="flex justify-between text-[11px] text-slate-500"><span>{job.message}</span><span>{job.progress?.toFixed?.(0) ?? 0}%</span></div>
              <div className="h-2 rounded-full bg-slate-200"><div className="h-2 rounded-full bg-sky-500 transition-all" style={{ width: `${job.progress || 0}%` }} /></div>
            </div>
          )}
          {error && <div className="text-xs text-rose-400">{error}</div>}
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="mb-3 flex items-center justify-between">
            <div className="text-sm font-semibold text-slate-900">Backtest Results</div>
            {run && <div className="flex items-center gap-2"><DataSourceBadge source={run.data_source} /><Badge tone="slate">Run #{run.id}</Badge></div>}
          </div>
          {!run && <div className="text-sm text-slate-500">Run a backtest to see institutional analytics (MAE/MFE, Expectancy, Profit Factor, Sharpe, Calmar, Max DD, Walk-Forward).</div>}
          {run && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-6">
                {[
                  ['Trades', m.trades], ['Net P&L ₹', m.net_pnl], ['Win %', m.win_rate], ['Profit Factor', m.profit_factor],
                  ['Expectancy ₹', m.expectancy], ['Avg MAE', m.avg_mae], ['Avg MFE', m.avg_mfe], ['Sharpe', m.sharpe],
                  ['Calmar', m.calmar], ['Max DD ₹', m.max_drawdown], ['Max DD %', m.max_drawdown_pct],
                ].map(([k,v]) => (
                  <div key={String(k)} className="rounded-lg border border-slate-200 bg-white p-3">
                    <div className="text-[11px] text-slate-500">{k}</div>
                    <div className={`text-lg font-semibold ${Number(v) < 0 ? 'text-rose-400' : 'text-slate-100'}`}>{v ?? '—'}</div>
                  </div>
                ))}
              </div>
              {wfData && (
                <div className="grid gap-3 md:grid-cols-2">
                  {[['In-Sample', wfData.in_sample], ['Out-of-Sample', wfData.out_of_sample]].map(([label, d]: any) => (
                    <div key={label} className="rounded-lg border border-slate-200 bg-white p-3">
                      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-900">{label} <Badge tone={label==='In-Sample'?'blue':'purple'}>{label}</Badge></div>
                      <div className="grid grid-cols-3 gap-2 text-xs">
                        {Object.entries(d || {}).map(([k,v]) => (
                          <div key={k}><div className="text-slate-500">{k}</div><div className="text-slate-700">{String(v)}</div></div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <div className="rounded-lg border border-emerald-500/20 bg-emerald-50 p-3 text-xs text-emerald-800 flex items-center gap-2"><CheckCircle2 size={14}/> Execution model: real bid/ask fills, slippage %, zero-volume guard, next-tick/bar latency, point-based SL/TP/Trailing, 15:15 intraday exit — on real option contract series only.</div>
            </div>
          )}
        </div>
      </div>

      <SebiDisclaimer />
    </div>
  );
}
