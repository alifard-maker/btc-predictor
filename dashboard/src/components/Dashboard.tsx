"use client";

import { useCallback, useEffect, useState } from "react";
import {
  getApiUrl,
  getCalibration,
  getHealth,
  getLatestPrediction,
  getPredictions,
} from "@/lib/api";
import type { CalibrationResponse, Health, Prediction } from "@/lib/types";
import { CalibrationPanel } from "@/components/CalibrationPanel";
import { HistoryTable } from "@/components/HistoryTable";
import { PredictionCard } from "@/components/PredictionCard";
import { StatusPanel } from "@/components/StatusPanel";

const POLL_MS = 5_000;

export function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [prediction, setPrediction] = useState<Prediction | null>(null);
  const [history, setHistory] = useState<Prediction[]>([]);
  const [calibration, setCalibration] = useState<CalibrationResponse | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, p, hist, cal] = await Promise.all([
        getHealth(),
        getLatestPrediction(),
        getPredictions(30),
        getCalibration(),
      ]);
      setHealth(h);
      setPrediction(p);
      setHistory(hist);
      setCalibration(cal);
      setLastRefresh(new Date());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch data");
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <div className="dashboard">
      <header className="header">
        <div>
          <h1>BTC Predictor</h1>
          <p className="subtitle">Probabilistic signals · Stage 1 · No trading</p>
        </div>
        <div className="header-meta">
          <span className="api-pill">{getApiUrl()}</span>
          {lastRefresh && (
            <span className="refresh-time">
              Updated {lastRefresh.toLocaleTimeString()}
            </span>
          )}
          <button type="button" className="refresh-btn" onClick={refresh}>
            Refresh
          </button>
        </div>
      </header>

      {error && <div className="banner error">{error}</div>}

      <div className="grid-main">
        <PredictionCard prediction={prediction} />
        <StatusPanel health={health} />
      </div>

      <CalibrationPanel data={calibration} />
      <HistoryTable predictions={history} />

      <footer className="footer">
        Predictions update every 15 min · Dashboard refreshes every 5 sec
      </footer>
    </div>
  );
}
