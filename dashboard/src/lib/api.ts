import type { CalibrationResponse, Health, Prediction } from "./types";

const API_URL =
  process.env.NEXT_PUBLIC_API_URL ??
  "https://btc-predictor-production-f460.up.railway.app";

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

export function getApiUrl() {
  return API_URL;
}

export async function getHealth(): Promise<Health> {
  return fetchJson<Health>("/health");
}

export async function getLatestPrediction(): Promise<Prediction | null> {
  try {
    return await fetchJson<Prediction>("/api/prediction/latest");
  } catch {
    return null;
  }
}

export async function getPredictions(limit = 50): Promise<Prediction[]> {
  return fetchJson<Prediction[]>(`/api/predictions?limit=${limit}`);
}

export async function getCalibration(): Promise<CalibrationResponse> {
  return fetchJson<CalibrationResponse>("/api/calibration");
}
