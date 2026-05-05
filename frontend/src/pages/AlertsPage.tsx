import { useEffect, useState } from "react";
import { api } from "../api";
import type { AlertsPage as AlertsPageData } from "../types";
import { SignalBadge } from "../components/SignalBadge";

const PAGE_SIZE = 25;

function fmtTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleString("en-GB", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function AlertsPage() {
  const [data, setData] = useState<AlertsPageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [tickerFilter, setTickerFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setLoading(true);
      const res = await api.listAlerts({
        ticker: tickerFilter || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setData(res);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [offset, tickerFilter]);

  const total = data?.total ?? 0;
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Alert History</h1>
          <div className="subtitle">{total} signals delivered to date</div>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="toolbar">
        <input
          className="input"
          placeholder="Filter by symbol…"
          value={tickerFilter}
          onChange={(e) => {
            setTickerFilter(e.target.value);
            setOffset(0);
          }}
        />
        <button className="btn btn-ghost" onClick={load}>
          Refresh
        </button>
      </div>

      <div className="card" style={{ padding: 0 }}>
        {loading ? (
          <div className="empty">
            <span className="spinner" /> &nbsp; Loading…
          </div>
        ) : !data || data.items.length === 0 ? (
          <div className="empty">No alerts yet.</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Symbol</th>
                <th>Signal</th>
                <th style={{ textAlign: "right" }}>Price</th>
                <th style={{ textAlign: "right" }}>RSI</th>
                <th style={{ textAlign: "right" }}>BB Lower</th>
                <th>Reasons</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id}>
                  <td className="muted">{fmtTime(a.sent_at)}</td>
                  <td>
                    <span className="ticker">{a.ticker}</span>
                    <span className="source-tag">{a.source}</span>
                  </td>
                  <td>
                    <SignalBadge strength={a.strength} />
                  </td>
                  <td style={{ textAlign: "right" }} className="tabular">
                    ${a.price.toFixed(4)}
                  </td>
                  <td style={{ textAlign: "right" }} className="tabular">
                    {a.rsi != null ? a.rsi.toFixed(1) : "—"}
                  </td>
                  <td style={{ textAlign: "right" }} className="tabular muted">
                    {a.bb_lower != null ? `$${a.bb_lower.toFixed(4)}` : "—"}
                  </td>
                  <td className="muted" style={{ fontSize: 12 }}>
                    {a.reasons?.join(" · ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="pagination">
        <span>
          Page {page} of {totalPages}
        </span>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="btn"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ‹ Prev
          </button>
          <button
            className="btn"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next ›
          </button>
        </div>
      </div>
    </div>
  );
}
