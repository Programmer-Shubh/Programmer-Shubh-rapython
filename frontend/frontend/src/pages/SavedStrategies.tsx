import { useEffect, useState } from 'react';
import { Save, RefreshCw } from 'lucide-react';
import { api, type BacktestRun, type Strategy, type Trade } from '../services/api';
import { Badge, DataSourceBadge, SebiDisclaimer, UserConfiguredBadge } from '../components/common/Badges';

function SummaryPill({ run }: { run: BacktestRun | null }) {
  if (!run) return <div className="text-xs text-slate-500">No saved backtest yet.</div>;
  const m = run.metrics || {};
  const pnl = Number(m.net_pnl || 0);
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs">
      <Badge tone="blue">Saved BT</Badge>
      <DataSourceBadge source={run.data_source} />
      <span className="text-slate-600">{m.trades ?? 0} trades</span>
      <span className={pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}>Net P&L ₹{pnl.toFixed(2)}</span>
      <span className="text-slate-600">Win {m.win_rate ?? 0}%</span>
      <span className="text-slate-600">PF {m.profit_factor ?? '—'}</span>
      <span className="text-slate-500">{new Date(run.created_at).toLocaleString()}</span>
    </div>
  );
}

export function SavedStrategies() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [latestRuns, setLatestRuns] = useState<Record<number, BacktestRun | null>>({});
  const [expanded, setExpanded] = useState<number | null>(null);
  const [trades, setTrades] = useState<Record<number, Trade[]>>({});
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState<number | null>(null);

  const load = async () => {
    const s = await api.get<Strategy[]>('/strategies');
    setStrategies(s);
    const entries = await Promise.all(s.map(async st => [st.id, await api.get<BacktestRun | null>(`/strategies/${st.id}/latest-run`)] as const));
    const map: Record<number, BacktestRun | null> = {};
    entries.forEach(([id, r]) => { map[id] = r; });
    setLatestRuns(map);
    const n: Record<number, string> = {};
    s.forEach(st => { n[st.id] = st.user_notes || ''; });
    setNotes(n);
  };

  useEffect(() => { load(); }, []);

  const toggle = async (id: number) => {
    if (expanded === id) { setExpanded(null); return; }
    setExpanded(id);
    if (!trades[id]) {
      const t = await api.get<Trade[]>(`/strategies/${id}/latest-trades`);
      setTrades(prev => ({ ...prev, [id]: t }));
    }
  };

  const saveNotes = async (id: number) => {
    setSaving(id);
    try { await api.patch(`/strategies/${id}`, { user_notes: notes[id] }); }
    finally { setSaving(null); }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">Saved Strategies</h1>
          <p className="text-sm text-slate-500">Cards and Trade History pull from the same latest saved backtest record (single source of truth). User Notes are preserved separately.</p>
        </div>
        <button onClick={load} className="inline-flex items-center gap-2 rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-200"><RefreshCw size={13}/> Refresh</button>
      </div>

      <div className="grid gap-4">
        {strategies.map(st => {
          const run = latestRuns[st.id];
          const m = run?.metrics || {};
          const trows = trades[st.id] || [];
          return (
            <div key={st.id} className="rounded-xl border border-slate-200 bg-white p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="text-lg font-semibold text-slate-900">{st.name}</h3>
                    {st.is_user_configured && <UserConfiguredBadge />}
                    {run && <DataSourceBadge source={run.data_source} />}
                  </div>
                  <div className="mt-1 text-xs text-slate-500">Strike: {st.config?.strike_offset} · Expiry: {st.config?.expiry_type} · SL {st.config?.sl_points} / TP {st.config?.tp_points} / Trail {st.config?.trailing_sl_points} · Latency {st.config?.latency}</div>
                </div>
                <button onClick={() => toggle(st.id)} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-200">{expanded === st.id ? 'Hide Details' : 'View Trade History'}</button>
              </div>

              <div className="mt-3"><SummaryPill run={run} /></div>

              {run && (
                <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-6 text-xs">
                  {[
                    ['Net P&L ₹', m.net_pnl], ['Trades', m.trades], ['Win %', m.win_rate], ['Profit Factor', m.profit_factor],
                    ['Sharpe', m.sharpe], ['Max DD ₹', m.max_drawdown],
                  ].map(([k,v]) => (
                    <div key={String(k)} className="rounded-md border border-slate-200 bg-white p-2">
                      <div className="text-slate-500">{k}</div>
                      <div className={`font-semibold ${Number(v) < 0 ? 'text-rose-400' : 'text-slate-700'}`}>{String(v ?? '—')}</div>
                    </div>
                  ))}
                </div>
              )}

              <div className="mt-3">
                <label className="text-[11px] text-slate-500">User Notes (preserved separately; not overwritten by BT summary)</label>
                <div className="mt-1 flex gap-2">
                  <textarea value={notes[st.id] || ''} onChange={e => setNotes(p => ({ ...p, [st.id]: e.target.value }))} className="min-h-[56px] w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-xs text-slate-700" />
                  <button onClick={() => saveNotes(st.id)} disabled={saving === st.id} className="inline-flex h-9 items-center gap-1 rounded-md bg-slate-700 px-3 text-xs text-slate-900 hover:bg-slate-300"><Save size={13}/>{saving===st.id?'Saving':'Save'}</button>
                </div>
              </div>

              {expanded === st.id && (
                <div className="mt-4 overflow-x-auto rounded-lg border border-slate-200">
                  <table className="min-w-full text-left text-xs">
                    <thead className="bg-white/70 text-slate-500">
                      <tr>
                        {['Entry Date','Entry Time','Exit Date','Exit Time','Symbol','Type','Strike','Expiry','Entry ₹','Exit ₹','Status','P&L','MAE','MFE','BT'].map(h => <th key={h} className="px-2 py-2 font-medium">{h}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {trows.length === 0 && <tr><td colSpan={15} className="px-3 py-4 text-center text-slate-500">No trades in latest saved backtest.</td></tr>}
                      {trows.map(t => (
                        <tr key={t.id} className="border-t border-slate-200 text-slate-600">
                          <td className="px-2 py-1.5">{t.entry_date}</td>
                          <td className="px-2 py-1.5">{t.entry_time}</td>
                          <td className="px-2 py-1.5">{t.exit_date}</td>
                          <td className="px-2 py-1.5">{t.exit_time}</td>
                          <td className="px-2 py-1.5">{t.symbol}</td>
                          <td className="px-2 py-1.5"><Badge tone={t.trade_type==='CE'?'green':'red'}>{t.trade_type}</Badge></td>
                          <td className="px-2 py-1.5">{t.strike}</td>
                          <td className="px-2 py-1.5">{t.expiry}</td>
                          <td className="px-2 py-1.5">{t.entry_price}</td>
                          <td className="px-2 py-1.5">{t.exit_price}</td>
                          <td className="px-2 py-1.5">{t.status}</td>
                          <td className={`px-2 py-1.5 font-semibold ${t.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{t.pnl.toFixed(2)}</td>
                          <td className="px-2 py-1.5">{t.mae}</td>
                          <td className="px-2 py-1.5">{t.mfe}</td>
                          <td className="px-2 py-1.5"><DataSourceBadge source={run?.data_source} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <SebiDisclaimer />
    </div>
  );
}
