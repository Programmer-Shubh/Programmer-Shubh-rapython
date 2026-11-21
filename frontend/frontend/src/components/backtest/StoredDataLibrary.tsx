import { useEffect, useMemo, useState } from 'react';
import { Upload, Database, RefreshCw } from 'lucide-react';
import { api, type CatalogEntry } from '../../services/api';
import { Badge } from '../common/Badges';

export function StoredDataLibrary({ onUploaded }: { onUploaded?: () => void }) {
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState('');

  const load = async () => {
    try { setCatalog(await api.get<CatalogEntry[]>('/lakehouse/catalog')); } catch {}
  };
  useEffect(() => { load(); }, []);

  const grouped = useMemo(() => {
    const map = new Map<string, any>();
    for (const c of catalog) {
      const key = `${c.underlying}|${c.data_mode}`;
      const g = map.get(key) || { underlying: c.underlying, data_mode: c.data_mode, min: c.trade_date, max: c.trade_date, rows: 0, size: 0, days: new Set<string>() };
      g.min = c.trade_date < g.min ? c.trade_date : g.min;
      g.max = c.trade_date > g.max ? c.trade_date : g.max;
      g.rows += c.row_count;
      g.size += c.size_bytes;
      g.days.add(c.trade_date);
      map.set(key, g);
    }
    return Array.from(map.values()).map(g => ({ ...g, days: g.days.size, completeness: Math.min(100, Math.round((g.days.size / 22) * 100)) }));
  }, [catalog]);

  const upload = async (file: File) => {
    setUploading(true); setMsg('');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/lakehouse/upload', { method: 'POST', body: fd });
      const j = await res.json();
      setMsg(j.message || j.status);
      await load();
      onUploaded?.();
    } catch (e: any) { setMsg(e.message); }
    finally { setUploading(false); }
  };

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Database size={16} className="text-sky-400" /> Stored Option Data Library</div>
        <button onClick={load} className="text-slate-500 hover:text-slate-700"><RefreshCw size={14} /></button>
      </div>
      <div className="mb-3 grid gap-2">
        {grouped.length === 0 && <div className="text-xs text-slate-500">No cached option data yet. Upload CSV/Parquet or start the auto-recorder.</div>}
        {grouped.map((g, i) => (
          <div key={i} className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-slate-700">{g.underlying}</span>
              <Badge tone={g.data_mode === 'tick' ? 'blue' : 'purple'}>{g.data_mode === 'tick' ? 'TICK DATA' : '1M OPTION CHAIN'}</Badge>
              <span className="text-slate-500">{g.min} → {g.max}</span>
            </div>
            <div className="flex items-center gap-3 text-slate-500">
              <span>{g.rows.toLocaleString()} rows</span>
              <span>{(g.size / 1024 / 1024).toFixed(2)} MB</span>
              <span className={g.completeness >= 80 ? 'text-emerald-400' : g.completeness >= 40 ? 'text-amber-400' : 'text-rose-400'}>{g.completeness}% complete</span>
            </div>
          </div>
        ))}
      </div>
      <label className="flex cursor-pointer flex-col items-center justify-center rounded-lg border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-center hover:border-sky-500/50">
        <Upload size={18} className="mb-1 text-slate-500" />
        <span className="text-xs text-slate-600">{uploading ? 'Ingesting...' : 'Drag & drop CSV/Parquet or click to upload'}</span>
        <span className="text-[11px] text-slate-500">Idempotent — duplicate files are auto-detected by hash and skipped</span>
        <input type="file" accept=".csv,.parquet" className="hidden" onChange={e => e.target.files?.[0] && upload(e.target.files[0])} />
      </label>
      {msg && <div className="mt-2 text-[11px] text-sky-700">{msg}</div>}
    </div>
  );
}
