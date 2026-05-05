import { useEffect, useState } from "react";
import { api } from "../api";
import type { WatchlistItem } from "../types";
import { SignalBadge } from "../components/SignalBadge";

function fmt(v: number | null | undefined, digits = 2, prefix = "") {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return prefix + v.toFixed(digits);
}

export function WatchlistPage() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setLoading(true);
      setItems(await api.listWatchlist(true));
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim()) return;
    try {
      setAdding(true);
      await api.addWatchlist(input.trim());
      setInput("");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setAdding(false);
    }
  }

  async function remove(ticker: string) {
    if (!confirm(`Remove ${ticker} from watchlist?`)) return;
    try {
      await api.removeWatchlist(ticker);
      await load();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Watchlist</h1>
          <div className="subtitle">
            Live RSI · Bollinger Bands · Signal status across {items.length} symbols
          </div>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card" style={{ padding: 0 }}>
        <form onSubmit={add} className="toolbar" style={{ padding: 16, marginBottom: 0 }}>
          <input
            className="input"
            placeholder="Add symbol (SOXL, ETH-USD, ETH/USDT)…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
          <button className="btn btn-primary" disabled={adding}>
            {adding ? <span className="spinner" /> : "Add"}
          </button>
          <button type="button" className="btn btn-ghost" onClick={load}>
            Refresh
          </button>
        </form>

        {loading ? (
          <div className="empty">
            <span className="spinner" /> &nbsp; Loading…
          </div>
        ) : items.length === 0 ? (
          <div className="empty">No symbols yet. Add one above.</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th style={{ textAlign: "right" }}>Price</th>
                <th style={{ textAlign: "right" }}>RSI</th>
                <th style={{ textAlign: "right" }}>BB Lower</th>
                <th style={{ textAlign: "right" }}>BB Mid</th>
                <th>Signal</th>
                <th style={{ width: 80 }}></th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.id}>
                  <td>
                    <span className="ticker">{it.ticker}</span>
                    <span className="source-tag">{it.source}</span>
                    {it.error && (
                      <div style={{ fontSize: 11, color: "var(--danger)", marginTop: 4 }}>
                        {it.error}
                      </div>
                    )}
                  </td>
                  <td style={{ textAlign: "right" }} className="tabular">
                    {fmt(it.price, 4, "$")}
                  </td>
                  <td
                    style={{ textAlign: "right" }}
                    className={`tabular ${
                      it.rsi != null && it.rsi < 35
                        ? "neg"
                        : it.rsi != null && it.rsi > 70
                          ? "pos"
                          : ""
                    }`}
                  >
                    {fmt(it.rsi, 1)}
                  </td>
                  <td style={{ textAlign: "right" }} className="tabular muted">
                    {fmt(it.bb_lower, 4, "$")}
                  </td>
                  <td style={{ textAlign: "right" }} className="tabular muted">
                    {fmt(it.bb_mid, 4, "$")}
                  </td>
                  <td>
                    <SignalBadge strength={it.signal} />
                  </td>
                  <td>
                    <button
                      className="btn btn-ghost btn-danger"
                      onClick={() => remove(it.ticker)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
