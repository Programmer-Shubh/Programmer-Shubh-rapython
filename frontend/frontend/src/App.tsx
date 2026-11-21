import { useState } from 'react';
import './index.css';
import { Header } from './components/layout/Header';
import { TradingCenter } from './pages/TradingCenter';
import { StrategyBacktest } from './pages/StrategyBacktest';
import { SavedStrategies } from './pages/SavedStrategies';

export default function App() {
  const [tab, setTab] = useState('backtest');
  return (
    <div className="min-h-screen bg-[#0b1220] text-slate-200">
      <Header activeTab={tab} onTab={setTab} />
      <main className="mx-auto max-w-[1600px] px-4 py-5">
        {tab === 'trading' && <TradingCenter />}
        {tab === 'backtest' && <StrategyBacktest />}
        {tab === 'saved' && <SavedStrategies />}
      </main>
    </div>
  );
}
