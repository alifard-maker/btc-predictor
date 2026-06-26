"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CalibrationResponse } from "@/lib/types";
import { Card, formatPct } from "./ui";

export function CalibrationPanel({ data }: { data: CalibrationResponse | null }) {
  if (!data || data.summary.n_resolved === 0) {
    return (
      <Card title="Calibration">
        <p className="muted">
          Not enough resolved predictions yet. Check back after predictions have had 5+ minutes to resolve.
        </p>
      </Card>
    );
  }

  const { summary, bins } = data;
  const chartData = bins.map((b) => ({
    bin: `Bin ${b.bin + 1}`,
    predicted: +(b.mean_predicted * 100).toFixed(1),
    actual: +(b.mean_actual * 100).toFixed(1),
  }));

  return (
    <Card title="Calibration">
      <p className="calibration-desc">
        When the model says X% UP, does price actually go up ~X% of the time?
      </p>

      <div className="calibration-stats">
        <div className="cal-stat">
          <span className="cal-stat-label">Resolved</span>
          <span className="cal-stat-value">{summary.n_resolved}</span>
        </div>
        {summary.brier_score != null && (
          <div className="cal-stat">
            <span className="cal-stat-label">Brier score</span>
            <span className="cal-stat-value">{summary.brier_score.toFixed(4)}</span>
          </div>
        )}
        {summary.overall_accuracy != null && (
          <div className="cal-stat">
            <span className="cal-stat-label">Accuracy</span>
            <span className="cal-stat-value">{formatPct(summary.overall_accuracy)}</span>
          </div>
        )}
        {summary.mean_calibration_error != null && (
          <div className="cal-stat">
            <span className="cal-stat-label">Cal. error</span>
            <span className="cal-stat-value">{formatPct(summary.mean_calibration_error)}</span>
          </div>
        )}
      </div>

      {chartData.length > 0 && (
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a2a3a" />
              <XAxis dataKey="bin" stroke="#888" fontSize={12} />
              <YAxis stroke="#888" fontSize={12} unit="%" />
              <Tooltip
                contentStyle={{ background: "#1a1a24", border: "1px solid #333" }}
              />
              <Legend />
              <Bar dataKey="predicted" name="Predicted UP %" fill="#22c55e" radius={[4, 4, 0, 0]} />
              <Bar dataKey="actual" name="Actual UP %" fill="#6366f1" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}
