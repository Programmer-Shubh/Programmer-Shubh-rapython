import { useEffect, useState } from 'react';
import { api } from '../services/api';
import { Badge, SebiDisclaimer, UserConfiguredBadge } from '../components/common/Badges';

export function TradingCenter() {
  const [mode, setMode] = useState<'paper' | 'live'>('paper');
  const [rec, setRec] = useState<any>(null);

  const load = async () => {
    try {
      const [m, r] = await Promise.all([api.get<any>('/trading/mode'), api.get<any>('/recorder/status')]);
      setMode(m.mode); setRec(r);
    } catch {}
  };
  useEffect(() => { load(); }, []);

  const toggleMode = async () => {
    const next = mode === 'paper' ? 'live' : 'paper';
    const m = await api.post<any>(`/trading/mode?mode=${next}`);
    setMode(m.mode);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">Trading Center</h1>
          <p className="text-sm text-slate-500">Paper/Live execution with async ring-buffered auto-recorder. Live ticks are persisted to the Parquet lakehouse without blocking order execution.</p>
        </div>
        <div className="flex items-center gap-2">
          <UserConfiguredBadge />
          <Badge tone={mode === 'paper' ? 'amber' : 'red'}>{mode.toUpperCase()}</Badge>
          <button onClick={toggleMode} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-200">Toggle Paper/Live</button>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="text-xs text-slate-500">Auto-Recorder</div>
          <div className="mt-1 text-lg font-semibold text-slate-900">{rec?.running ? 'Active' : 'Paused'}</div>
          <div className="mt-2 text-xs text-slate-500">Latest recorded: {rec?.latest_recorded_date || '—'}</div>
          <div className="text-xs text-slate-500">Tracked symbols: {(rec?.tracked_symbols || []).join(', ') || '—'}</div>
          <div className="text-xs text-slate-500">Cached: {rec?.cached_storage_mb?.toFixed?.(2) || '0.00'} MB</div>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="text-xs text-slate-500">Execution Safeguards</div>
          <ul className="mt-2 space-y-1 text-xs text-slate-600">
            <li>• Real bid/ask fills (no Black-Scholes)</li>
            <li>• Zero-volume / illiquid tick guards</li>
            <li>• Next-tick / next-bar latency model</li>
            <li>• SL / Trailing SL / TP / 15:15 exit</li>
          </ul>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="text-xs text-slate-500">Data Lakehouse</div>
          <ul className="mt-2 space-y-1 text-xs text-slate-600">
            <li>• Hive-partitioned Parquet (ZSTD)</li>
            <li>• DuckDB predicate pushdown + row-group pruning</li>
            <li>• Atomic nightly compaction</li>
            <li>• Idempotent CSV/Parquet upload dedupe</li>
          </ul>
        </div>
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <div className="text-sm font-semibold text-slate-900">Offline-First Design</div>
        <p className="mt-1 text-sm text-slate-500">Backtests and strategy analytics run entirely from the local lakehouse. Broker disconnects, expired tokens, or market closure do not block backtesting.</p>
      </div>

      <SebiDisclaimer />
    </div>
  );
}
