export interface Prediction {
  timestamp: string;
  price: number;
  prob_up: number;
  prob_down: number;
  confidence: number;
  expected_move: number;
  signal: string;
  outcome?: number | null;
  actual_return?: number | null;
}

export interface Health {
  status: string;
  service?: string;
  symbol?: string;
  exchange?: string;
  exchange_connected?: boolean;
  model?: string;
  candles_1m?: number;
  latest_candle?: string;
  scheduler_running?: boolean;
  last_error?: string | null;
}

export interface CalibrationSummary {
  n_resolved: number;
  brier_score?: number;
  overall_accuracy?: number;
  long_signals?: number;
  long_accuracy?: number | null;
  short_signals?: number;
  short_accuracy?: number | null;
  mean_calibration_error?: number | null;
}

export interface CalibrationBin {
  bin: number;
  count: number;
  mean_predicted: number;
  mean_actual: number;
  calibration_error: number;
}

export interface CalibrationResponse {
  summary: CalibrationSummary;
  bins: CalibrationBin[];
}
