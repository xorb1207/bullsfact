export type SignalStrength = "none" | "weak" | "strong";

export interface WatchlistItem {
  id: number;
  ticker: string;
  source: string;
  added_at: string;
  active: boolean;
  price?: number | null;
  rsi?: number | null;
  bb_lower?: number | null;
  bb_mid?: number | null;
  bb_upper?: number | null;
  signal?: SignalStrength | null;
  error?: string | null;
}

export interface AlertItem {
  id: number;
  ticker: string;
  strength: SignalStrength;
  price: number;
  rsi: number | null;
  bb_lower: number | null;
  source: string;
  reasons?: string[] | null;
  sent_at: string;
}

export interface AlertsPage {
  items: AlertItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface BacktestRequest {
  ticker: string;
  start_date: string;
  end_date: string;
  rsi_threshold: number;
  bb_std: number;
  rsi_period: number;
  bb_period: number;
  interval: string;
  exit_rule: string;
  exit_params: Record<string, number>;
  fee_bps: number;
  slippage_bps: number;
  allow_overlap: boolean;
}

export interface BacktestMetrics {
  win_rate: number;
  total_return: number;
  mdd: number;
  sharpe: number | null;
  profit_factor: number | null;
  trade_count: number;
  avg_win: number | null;
  avg_loss: number | null;
  avg_holding_bars: number | null;
}

export interface BacktestResultOut {
  id: number;
  ticker: string;
  start_date: string;
  end_date: string;
  strategy_params: Record<string, unknown>;
  win_rate: number | null;
  mdd: number | null;
  total_return: number | null;
  trade_count: number | null;
  details: {
    metrics?: BacktestMetrics;
    trades?: Array<{
      entry_time: string;
      exit_time: string;
      entry_price: number;
      exit_price: number;
      return: number;
      strength: string;
      holding_bars: number;
    }>;
    equity_curve?: Array<[string, number]>;
    exit_rule_resolved?: string;
  } | null;
  created_at: string;
}
