import type {
  AlertsPage,
  BacktestRequest,
  BacktestResultOut,
  WatchlistItem,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  health: () => request<{ status: string }>("/health"),

  // Watchlist
  listWatchlist: (includeIndicators = true) =>
    request<WatchlistItem[]>(
      `/watchlist?include_indicators=${includeIndicators}`,
    ),
  addWatchlist: (ticker: string) =>
    request<WatchlistItem>("/watchlist", {
      method: "POST",
      body: JSON.stringify({ ticker }),
    }),
  removeWatchlist: (ticker: string) =>
    request<void>(`/watchlist/${encodeURIComponent(ticker)}`, {
      method: "DELETE",
    }),

  // Alerts
  listAlerts: (params: { ticker?: string; limit: number; offset: number }) => {
    const q = new URLSearchParams();
    q.set("limit", String(params.limit));
    q.set("offset", String(params.offset));
    if (params.ticker) q.set("ticker", params.ticker);
    return request<AlertsPage>(`/alerts?${q.toString()}`);
  },

  // Backtest
  runBacktest: (body: BacktestRequest) =>
    request<BacktestResultOut>("/backtest", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getBacktest: (id: number) => request<BacktestResultOut>(`/backtest/${id}`),
  listBacktests: (ticker?: string) => {
    const q = new URLSearchParams();
    if (ticker) q.set("ticker", ticker);
    return request<BacktestResultOut[]>(`/backtest?${q.toString()}`);
  },
};
