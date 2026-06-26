"use client";

import type { Prediction } from "@/lib/types";
import { Card, SignalBadge, formatPct, formatPrice, formatTime } from "./ui";

export function HistoryTable({ predictions }: { predictions: Prediction[] }) {
  return (
    <Card title="Prediction history">
      {predictions.length === 0 ? (
        <p className="muted">No predictions logged yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Price</th>
                <th>UP</th>
                <th>Signal</th>
                <th>Outcome</th>
                <th>Return</th>
              </tr>
            </thead>
            <tbody>
              {predictions.map((p, i) => (
                <tr key={`${p.timestamp}-${i}`}>
                  <td>{formatTime(p.timestamp)}</td>
                  <td>{formatPrice(p.price)}</td>
                  <td className="up">{formatPct(p.prob_up)}</td>
                  <td><SignalBadge signal={p.signal} /></td>
                  <td>
                    {p.outcome == null
                      ? "—"
                      : p.outcome === 1
                        ? <span className="up">UP</span>
                        : <span className="down">DOWN</span>}
                  </td>
                  <td>
                    {p.actual_return == null
                      ? "—"
                      : <span className={p.actual_return >= 0 ? "up" : "down"}>
                          {(p.actual_return * 100).toFixed(3)}%
                        </span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
