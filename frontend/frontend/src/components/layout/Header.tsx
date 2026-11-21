import { useEffect, useState } from 'react';
import { Activity, Database, PauseCircle, PlayCircle } from 'lucide-react';
import { api, type RecorderStatus } from '../../services/api';
import { Badge } from '../common/Badges';

export function Header({ activeTab, onTab }: { activeTab: string; onTab: (t: string) => void }) {
  const [rec, setRec] = useState<RecorderStatus | null>(null);

  const load = async () => {
    try { setRec(await api.get<RecorderStatus>('/recorder/status')); } catch {}
  };
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []);

  const tabs = [
    { id: 'trading', label: 'Trading Center' },
    { id: 'backtest', label: 'Strategy Backtest' },
    { id: 'saved', label: 'Saved Strategies' },
  ];

  return (
    <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur">
      <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-4 px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-sky-100 text-sky-700"><Activity size={18} /></div>
          <div>
            <div className="text-lg font-bold tracking-tight text-slate-900">RaTrade</div>
            <div className="text-[11px] text-slate-500">Institutional Quant Backtesting</div>
          </div>
        </div>
        <nav className="flex items-center gap-1 rounded-lg bg-slate-100 p-1">
          {tabs.map(t => (
            <button key={t.id} onClick={() => onTab(t.id)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${activeTab === t.id ? 'bg-sky-100 text-sky-700' : 'text-slate-600 hover:bg-slate-200'}`}>
              {t.label}
            </button>
          ))}
        </nav>
        <div className="flex items-center gap-2 text-xs">
          <div className="flex items-center gap-2 rounded-lg border border-slate-300 bg-slate-100 px-3 py-1.5">
            {rec?.running ? <PlayCircle size={14} className="text-emerald-400" /> : <PauseCircle size={14} className="text-slate-500" />}
            <span className={rec?.running ? 'text-emerald-300' : 'text-slate-600'}>
              Auto-Recorder {rec?.running ? 'Active' : 'Paused'}
            </span>
            <span className="text-slate-500">|</span>
            <span className="text-slate-600">Latest: {rec?.latest_recorded_date || '—'}</span>
            <span className="text-slate-500">|</span>
            <span className="text-slate-600">{rec?.tracked_symbols?.length || 0} symbols</span>
            <span className="text-slate-500">|</span>
            <span className="text-slate-600 flex items-center gap-1"><Database size={12} />{rec?.cached_storage_mb?.toFixed(2) || '0.00'} MB</span>
          </div>
          <Badge tone="amber">100% Offline BT</Badge>
        </div>
      </div>
    </header>
  );
}
