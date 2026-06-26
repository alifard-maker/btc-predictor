"use client";

import type { Health } from "@/lib/types";
import { Card } from "./ui";

export function StatusPanel({ health }: { health: Health | null }) {
  if (!health) {
    return (
      <Card title="System status">
        <p className="muted">Connecting…</p>
      </Card>
    );
  }

  const items = [
    { label: "API", value: health.status, ok: health.status === "ok" },
    { label: "Scheduler", value: health.scheduler_running ? "Running" : "Stopped", ok: !!health.scheduler_running },
    { label: "Exchange", value: health.exchange ?? "—", ok: !!health.exchange_connected },
    { label: "Model", value: health.model ?? "—", ok: true },
    { label: "Symbol", value: health.symbol ?? "—", ok: true },
    { label: "1m candles", value: String(health.candles_1m ?? 0), ok: (health.candles_1m ?? 0) > 0 },
  ];

  return (
    <Card title="System status">
      <ul className="status-list">
        {items.map((item) => (
          <li key={item.label}>
            <span className={`dot ${item.ok ? "ok" : "warn"}`} />
            <span className="status-label">{item.label}</span>
            <span className="status-value">{item.value}</span>
          </li>
        ))}
      </ul>
      {health.last_error && (
        <p className="error-text">Last error: {health.last_error}</p>
      )}
    </Card>
  );
}
