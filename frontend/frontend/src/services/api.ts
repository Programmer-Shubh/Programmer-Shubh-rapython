export const API_BASE = '/api';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body ? JSON.stringify(body) : undefined }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

export type Strategy = {
  id: number;
  name: string;
  user_notes: string;
  config: Record<string, any>;
  is_user_configured: boolean;
  created_at: string;
  updated_at: string;
};

export type BacktestRun = {
  id: number;
  strategy_id: number;
  status: string;
  data_source: string;
  data_mode: string;
  metrics: Record<string, any>;
  summary: Record<string, any>;
  created_at: string;
};

export type Trade = {
  id: number;
  entry_date: string;
  entry_time: string;
  exit_date: string;
  exit_time: string;
  symbol: string;
  trade_type: string;
  strike: number;
  expiry: string;
  entry_price: number;
  exit_price: number;
  status: string;
  pnl: number;
  mae: number;
  mfe: number;
  exit_reason?: string;
  is_oos?: boolean;
};

export type CatalogEntry = {
  underlying: string;
  data_mode: string;
  expiry: string;
  trade_date: string;
  row_count: number;
  min_timestamp: string | null;
  max_timestamp: string | null;
  size_bytes: number;
};

export type RecorderStatus = {
  running: boolean;
  buffer_size: number;
  latest_recorded_date: string | null;
  tracked_symbols: string[];
  cached_storage_mb: number;
  mode: string | null;
};
