import clsx from 'clsx';

export function Badge({ children, tone = 'slate' }: { children: React.ReactNode; tone?: 'slate' | 'green' | 'red' | 'amber' | 'blue' | 'purple' }) {
  const tones: Record<string, string> = {
    slate: 'bg-slate-700/60 text-slate-200 border-slate-600',
    green: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
    red: 'bg-rose-500/15 text-rose-300 border-rose-500/40',
    amber: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
    blue: 'bg-sky-500/15 text-sky-300 border-sky-500/40',
    purple: 'bg-violet-500/15 text-violet-300 border-violet-500/40',
  };
  return <span className={clsx('inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tracking-wide', tones[tone])}>{children}</span>;
}

export function DataSourceBadge({ source }: { source?: string }) {
  const isTick = (source || '').toUpperCase().includes('TICK');
  return <Badge tone={isTick ? 'blue' : 'purple'}>{isTick ? 'TICK DATA' : '1M OPTION CHAIN'}</Badge>;
}

export function UserConfiguredBadge() {
  return <Badge tone="green">User Configured</Badge>;
}

export function SebiDisclaimer() {
  return (
    <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-200">
      <strong>SEBI Non-Advisory Disclaimer:</strong> This platform is for educational and research purposes only. It is not a
      SEBI-registered investment adviser and nothing herein constitutes investment advice or a recommendation to buy, sell or hold any
      derivative or security. Derivatives trading involves substantial risk of loss. Past performance is not indicative of future results.
    </div>
  );
}
