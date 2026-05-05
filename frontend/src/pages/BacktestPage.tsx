import { useState } from "react";
import { api } from "../api";
import type { BacktestRequest, BacktestResultOut } from "../types";
import { StatCard } from "../components/StatCard";
import { EquityChart } from "../components/EquityChart";

function isoDaysAgo(days: number): string {
  const d = new Date(Date.now() - days * 86400_000);
  return d.toISOString().slice(0, 10);
}

const EXIT_RULES = [
  { value: "holding_bars", label: "Holding bars" },
  { value: "bb_revert", label: "BB mid revert" },
  { value: "rsi_revert", label: "RSI revert" },
  { value: "tp_sl", label: "Take-profit / Stop-loss" },
];

function pct(v: number | null | undefined, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function BacktestPage() {
  const [form, setForm] = useState<BacktestRequest>({
    ticker: "SOXL",
    start_date: isoDaysAgo(60) + "T00:00:00Z",
    end_date: isoDaysAgo(0) + "T00:00:00Z",
    rsi_threshold: 35,
    bb_std: 2.0,
    rsi_period: 14,
    bb_period: 20,
    interval: "1h",
    exit_rule: "holding_bars",
    exit_params: { bars: 24 },
    fee_bps: 5,
    slippage_bps: 2,
    allow_overlap: false,
  });

  const [result, setResult] = useState<BacktestResultOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function update<K extends keyof BacktestRequest>(k: K, v: BacktestRequest[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function run(e: React.FormEvent) {
    e.preventDefault();
    try {
      setLoading(true);
      setError(null);
      const res = await api.runBacktest(form);
      setResult(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  const m = result?.details?.metrics;
  const equity = result?.details?.equity_curve ?? [];

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Backtest</h1>
          <div className="subtitle">Replay the dip-buy strategy against historical bars</div>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <form onSubmit={run} className="card" style={{ marginBottom: 24 }}>
        <div className="form-row">
          <div>
            <label className="field-label">Symbol</label>
            <input
              className="input"
              value={form.ticker}
              onChange={(e) => update("ticker", e.target.value)}
            />
          </div>
          <div>
            <label className="field-label">Start (UTC)</label>
            <input
              className="input"
              type="date"
              value={form.start_date.slice(0, 10)}
              onChange={(e) => update("start_date", e.target.value + "T00:00:00Z")}
            />
          </div>
          <div>
            <label className="field-label">End (UTC)</label>
            <input
              className="input"
              type="date"
              value={form.end_date.slice(0, 10)}
              onChange={(e) => update("end_date", e.target.value + "T00:00:00Z")}
            />
          </div>
          <div>
            <label className="field-label">Interval</label>
            <select
              className="select"
              value={form.interval}
              onChange={(e) => update("interval", e.target.value)}
            >
              <option value="1h">1h</option>
              <option value="4h">4h</option>
              <option value="1d">1d</option>
            </select>
          </div>
        </div>

        <div className="form-row">
          <div>
            <label className="field-label">RSI period</label>
            <input
              className="input tabular"
              type="number"
              value={form.rsi_period}
              onChange={(e) => update("rsi_period", Number(e.target.value))}
            />
          </div>
          <div>
            <label className="field-label">RSI threshold</label>
            <input
              className="input tabular"
              type="number"
              step="0.5"
              value={form.rsi_threshold}
              onChange={(e) => update("rsi_threshold", Number(e.target.value))}
            />
          </div>
          <div>
            <label className="field-label">BB period</label>
            <input
              className="input tabular"
              type="number"
              value={form.bb_period}
              onChange={(e) => update("bb_period", Number(e.target.value))}
            />
          </div>
          <div>
            <label className="field-label">BB std</label>
            <input
              className="input tabular"
              type="number"
              step="0.1"
              value={form.bb_std}
              onChange={(e) => update("bb_std", Number(e.target.value))}
            />
          </div>
        </div>

        <div className="form-row">
          <div>
            <label className="field-label">Exit rule</label>
            <select
              className="select"
              value={form.exit_rule}
              onChange={(e) => {
                const kind = e.target.value;
                const defaults: Record<string, Record<string, number>> = {
                  holding_bars: { bars: 24 },
                  bb_revert: { max_bars: 240 },
                  rsi_revert: { rsi_exit: 55, max_bars: 240 },
                  tp_sl: { take_profit: 0.05, stop_loss: 0.03, max_bars: 240 },
                };
                setForm((f) => ({ ...f, exit_rule: kind, exit_params: defaults[kind] }));
              }}
            >
              {EXIT_RULES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>
          {Object.entries(form.exit_params).map(([k, v]) => (
            <div key={k}>
              <label className="field-label">{k}</label>
              <input
                className="input tabular"
                type="number"
                step="0.001"
                value={v}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    exit_params: { ...f.exit_params, [k]: Number(e.target.value) },
                  }))
                }
              />
            </div>
          ))}
        </div>

        <div className="form-row">
          <div>
            <label className="field-label">Fee (bps)</label>
            <input
              className="input tabular"
              type="number"
              step="0.5"
              value={form.fee_bps}
              onChange={(e) => update("fee_bps", Number(e.target.value))}
            />
          </div>
          <div>
            <label className="field-label">Slippage (bps)</label>
            <input
              className="input tabular"
              type="number"
              step="0.5"
              value={form.slippage_bps}
              onChange={(e) => update("slippage_bps", Number(e.target.value))}
            />
          </div>
          <div style={{ display: "flex", alignItems: "flex-end" }}>
            <button className="btn btn-primary" disabled={loading} style={{ width: "100%" }}>
              {loading ? <span className="spinner" /> : "Run backtest"}
            </button>
          </div>
        </div>
      </form>

      {result && m && (
        <>
          <div className="stat-grid">
            <StatCard
              label="Total Return"
              value={pct(m.total_return)}
              tone={m.total_return >= 0 ? "pos" : "neg"}
              sub={`${m.trade_count} trades`}
            />
            <StatCard
              label="Win Rate"
              value={pct(m.win_rate)}
              sub={
                m.avg_win != null && m.avg_loss != null
                  ? `avg win ${pct(m.avg_win, 2)} / avg loss ${pct(m.avg_loss, 2)}`
                  : undefined
              }
            />
            <StatCard
              label="Max Drawdown"
              value={pct(m.mdd)}
              tone="neg"
              sub={result.details?.exit_rule_resolved}
            />
            <StatCard
              label="Sharpe / PF"
              value={
                m.sharpe != null
                  ? m.sharpe.toFixed(2)
                  : m.profit_factor != null
                    ? m.profit_factor.toFixed(2)
                    : "—"
              }
              sub={
                m.profit_factor != null
                  ? `Profit factor ${m.profit_factor.toFixed(2)}`
                  : undefined
              }
            />
          </div>

          <div className="card">
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                marginBottom: 12,
              }}
            >
              <div style={{ fontSize: 14, fontWeight: 600 }}>Equity Curve</div>
              <div className="dim" style={{ fontSize: 12 }}>
                {result.start_date.slice(0, 10)} → {result.end_date.slice(0, 10)}
              </div>
            </div>
            {equity.length > 0 ? (
              <EquityChart data={equity} />
            ) : (
              <div className="empty">No trades — equity stays flat.</div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
