import type { Metadata } from "next";
import { Dashboard } from "@/components/Dashboard";
import "./globals.css";

export const metadata: Metadata = {
  title: "BTC Predictor",
  description: "Live BTC direction probabilities and calibration",
};

export default function Home() {
  return (
    <main>
      <Dashboard />
    </main>
  );
}
