#!/usr/bin/env python3
"""Generate Kalshi Crypto Desk pitch PDF from markdown source."""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "Kalshi_Crypto_Desk_Pitch.md"
PDF_PATH = ROOT / "docs" / "Kalshi_Crypto_Desk_Pitch.pdf"

# Reuse markdown → flowables parser from design report generator.
sys.path.insert(0, str(ROOT / "scripts"))
from generate_design_report_pdf import _styles, md_to_flowables  # noqa: E402


def build_pdf(md_path: Path, pdf_path: Path) -> None:
  md_text = md_path.read_text(encoding="utf-8")
  pdf_path.parent.mkdir(parents=True, exist_ok=True)
  doc = SimpleDocTemplate(
    str(pdf_path),
    pagesize=letter,
    leftMargin=0.75 * inch,
    rightMargin=0.75 * inch,
    topMargin=0.75 * inch,
    bottomMargin=0.75 * inch,
    title="Kalshi Crypto Desk — Pitch",
    author="BTC-Predictor",
  )
  st = _styles()
  subtitle = ParagraphStyle(
    "PitchSubtitle",
    parent=st["body"],
    fontSize=11,
    leading=14,
    textColor=colors.HexColor("#0f3460"),
  )
  story: list = [
    Paragraph("Kalshi Crypto Desk", st["title"]),
    Paragraph("Probabilistic auto-trading for Kalshi BTC/ETH &amp; index contracts", subtitle),
    Paragraph(
      f"Generated from {md_path.name} · btc-predictor · Beta 4.0.60",
      st["meta"],
    ),
    Spacer(1, 12),
    HRFlowable(width="100%", thickness=1, color=colors.HexColor("#0f3460")),
    Spacer(1, 12),
  ]
  story.extend(md_to_flowables(md_text))
  doc.build(story)
  print(f"Wrote {pdf_path} ({pdf_path.stat().st_size // 1024} KB)")


def main() -> int:
  md = MD_PATH
  pdf = PDF_PATH
  if len(sys.argv) > 1:
    md = Path(sys.argv[1])
  if len(sys.argv) > 2:
    pdf = Path(sys.argv[2])
  if not md.exists():
    print(f"Missing markdown: {md}", file=sys.stderr)
    return 1
  build_pdf(md, pdf)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
