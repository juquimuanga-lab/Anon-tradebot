import React, { useEffect, useState } from "react";
import "./App.css";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;

function App() {
  const [health, setHealth] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [healthRes, metricsRes] = await Promise.all([
          fetch(`${BACKEND_URL}/api/health`),
          fetch(`${BACKEND_URL}/api/metrics`),
        ]);
        setHealth(await healthRes.json());
        setMetrics(await metricsRes.json());
        setError(null);
      } catch (e) {
        setError("Cannot reach backend");
      }
    };
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="terminal" data-testid="dashboard-root">
      <div className="scanlines" />
      <header className="terminal-header">
        <span className="dot" />
        <h1>ANONCOIN_SNIPER // status_terminal</h1>
      </header>

      {error && <div className="error-banner" data-testid="dashboard-error">{error}</div>}

      <section className="grid">
        <div className="card" data-testid="mode-card">
          <div className="label">MODE</div>
          <div className={`value ${health?.mode === "live" ? "live" : "paper"}`}>
            {health ? health.mode.toUpperCase() : "..."}
          </div>
        </div>
        <div className="card" data-testid="trading-card">
          <div className="label">TRADING</div>
          <div className={`value ${health?.trading_enabled ? "on" : "off"}`}>
            {health ? (health.trading_enabled ? "ENABLED" : "PAUSED") : "..."}
          </div>
        </div>
        <div className="card" data-testid="scanned-card">
          <div className="label">TOKENS SCANNED</div>
          <div className="value">{metrics ? metrics.tokens_scanned : "..."}</div>
        </div>
        <div className="card" data-testid="qualified-card">
          <div className="label">TOKENS QUALIFIED</div>
          <div className="value">{metrics ? metrics.tokens_qualified : "..."}</div>
        </div>
        <div className="card" data-testid="trades-card">
          <div className="label">TRADES PLACED</div>
          <div className="value">{metrics ? metrics.trades_placed : "..."}</div>
        </div>
        <div className="card" data-testid="winrate-card">
          <div className="label">WIN RATE</div>
          <div className="value">{metrics ? `${metrics.win_rate_pct}%` : "..."}</div>
        </div>
        <div className="card" data-testid="pnl-card">
          <div className="label">TOTAL PNL</div>
          <div className={`value ${metrics && metrics.total_pnl < 0 ? "negative" : ""}`}>
            {metrics ? `${metrics.total_pnl >= 0 ? "+" : ""}${metrics.total_pnl}` : "..."}
          </div>
        </div>
        <div className="card" data-testid="errors-card">
          <div className="label">ERROR COUNT</div>
          <div className="value">{metrics ? metrics.error_count : "..."}</div>
        </div>
      </section>

      <footer className="terminal-footer">
        Control the bot entirely from Telegram. This screen is read-only observability.
      </footer>
    </div>
  );
}

export default App;
