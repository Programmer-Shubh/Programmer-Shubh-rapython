import clsx from 'clsx';

export function Badge({ children, tone = 'slate' }: { children: React.ReactNode; tone?: 'slate' | 'green' | 'red' | 'amber' | 'blue' | 'purple' }) {
  const tones: Record<string, string> = {
    slate: 'bg-slate-100 text-slate-700 border-slate-300',
    green: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    red: 'bg-rose-50 text-rose-700 border-rose-200',
    amber: 'bg-amber-50 text-amber-700 border-amber-200',
    blue: 'bg-sky-50 text-sky-700 border-sky-200',
    purple: 'bg-violet-50 text-violet-700 border-violet-200',
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
    <div className="rounded-lg border border-amber-500/30 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-800">
      <strong>SEBI Non-Advisory Disclaimer:</strong> This platform is for educational and research purposes only. It is not a
      SEBI-registered investment adviser and nothing herein constitutes investment advice or a recommendation to buy, sell or hold any
      derivative or security. Derivatives trading involves substantial risk of loss. Past performance is not indicative of future results.
    </div>
  );
}
