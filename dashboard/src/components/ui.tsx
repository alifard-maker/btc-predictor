import type { ReactNode } from "react";

export function Card({
  title,
  children,
  className = "",
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      <h2 className="card-title">{title}</h2>
      {children}
    </section>
  );
}

export function SignalBadge({ signal }: { signal: string }) {
  const cls = signal.toUpperCase().replace(/\s+/g, "_");
  return <span className={`badge badge-${cls}`}>{signal}</span>;
}

export function formatPct(n: number) {
  return `${(n * 100).toFixed(1)}%`;
}

export function formatPrice(n: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

export function formatTime(ts: string) {
  return new Date(ts).toLocaleString();
}
