"use client";

import type { Prediction } from "@/lib/types";
import { Card, SignalBadge, formatPct, formatPrice, formatTime } from "./ui";

export function PredictionCard({ prediction }: { prediction: Prediction | null }) {
  if (!prediction) {
    return (
      <Card title="Next 5 minutes" className="prediction-card">
        <p className="muted">Waiting for first prediction…</p>
      </Card>
    );
  }

  const upPct = prediction.prob_up * 100;
  const downPct = prediction.prob_down * 100;

  return (
    <Card title="Next 5 minutes" className="prediction-card">
      <div className="price-row">
        <span className="btc-label">BTC</span>
        <span className="btc-price">{formatPrice(prediction.price)}</span>
        <SignalBadge signal={prediction.signal} />
      </div>

      <div className="prob-bars">
        <div className="prob-row">
          <span className="prob-label up">UP</span>
          <div className="bar-track">
            <div className="bar-fill up" style={{ width: `${upPct}%` }} />
          </div>
          <span className="prob-value">{upPct.toFixed(1)}%</span>
        </div>
        <div className="prob-row">
          <span className="prob-label down">DOWN</span>
          <div className="bar-track">
            <div className="bar-fill down" style={{ width: `${downPct}%` }} />
          </div>
          <span className="prob-value">{downPct.toFixed(1)}%</span>
        </div>
      </div>

      <div className="metrics-grid">
        <div className="metric">
          <span className="metric-label">Expected move</span>
          <span className={`metric-value ${prediction.expected_move >= 0 ? "up" : "down"}`}>
            {prediction.expected_move >= 0 ? "+" : ""}
            {formatPrice(prediction.expected_move)}
          </span>
        </div>
        <div className="metric">
          <span className="metric-label">Confidence</span>
          <span className="metric-value">{formatPct(prediction.confidence)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Updated</span>
          <span className="metric-value small">{formatTime(prediction.timestamp)}</span>
        </div>
      </div>
    </Card>
  );
}
