#!/usr/bin/env python3
"""Generate BTC-Predictor bot design report PDF from markdown source."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
  HRFlowable,
  PageBreak,
  Paragraph,
  Preformatted,
  SimpleDocTemplate,
  Spacer,
  Table,
  TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "BTC_Predictor_Bot_Design_Report.md"
PDF_PATH = ROOT / "docs" / "BTC_Predictor_Bot_Design_Report.pdf"


def _escape_xml(text: str) -> str:
  return (
    text.replace("&", "&amp;")
    .replace("<", "&lt;")
    .replace(">", "&gt;")
  )


def _styles():
  base = getSampleStyleSheet()
  return {
    "title": ParagraphStyle(
      "DocTitle",
      parent=base["Title"],
      fontSize=22,
      spaceAfter=14,
      textColor=colors.HexColor("#1a1a2e"),
    ),
    "h1": ParagraphStyle(
      "H1",
      parent=base["Heading1"],
      fontSize=16,
      spaceBefore=16,
      spaceAfter=8,
      textColor=colors.HexColor("#16213e"),
    ),
    "h2": ParagraphStyle(
      "H2",
      parent=base["Heading2"],
      fontSize=13,
      spaceBefore=12,
      spaceAfter=6,
      textColor=colors.HexColor("#0f3460"),
    ),
    "h3": ParagraphStyle(
      "H3",
      parent=base["Heading3"],
      fontSize=11,
      spaceBefore=8,
      spaceAfter=4,
    ),
    "body": ParagraphStyle(
      "Body",
      parent=base["BodyText"],
      fontSize=9,
      leading=12,
      alignment=TA_LEFT,
    ),
    "bullet": ParagraphStyle(
      "Bullet",
      parent=base["BodyText"],
      fontSize=9,
      leading=12,
      leftIndent=14,
      bulletIndent=4,
    ),
    "code": ParagraphStyle(
      "Code",
      parent=base["Code"],
      fontSize=8,
      leading=10,
      fontName="Courier",
      backColor=colors.HexColor("#f4f4f4"),
      leftIndent=8,
      rightIndent=8,
    ),
    "formula": ParagraphStyle(
      "Formula",
      parent=base["Code"],
      fontSize=9,
      leading=11,
      fontName="Courier",
      leftIndent=12,
      textColor=colors.HexColor("#333333"),
    ),
    "meta": ParagraphStyle(
      "Meta",
      parent=base["Normal"],
      fontSize=8,
      textColor=colors.grey,
    ),
  }


def _parse_table(lines: list[str]) -> Table | None:
  if len(lines) < 2 or "|" not in lines[0]:
    return None
  rows = []
  for i, line in enumerate(lines):
    if not line.strip().startswith("|"):
      break
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if i == 1 and all(set(c) <= {"-", ":"} for c in cells):
      continue
    rows.append(cells)
  if not rows:
    return None
  col_count = max(len(r) for r in rows)
  data = []
  for row in rows:
    padded = row + [""] * (col_count - len(row))
    data.append([Paragraph(_escape_xml(c), _styles()["body"]) for c in padded])
  tbl = Table(data, repeatRows=1)
  tbl.setStyle(
    TableStyle([
      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
      ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#16213e")),
      ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
      ("FONTSIZE", (0, 0), (-1, -1), 8),
      ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
      ("VALIGN", (0, 0), (-1, -1), "TOP"),
      ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
      ("LEFTPADDING", (0, 0), (-1, -1), 4),
      ("RIGHTPADDING", (0, 0), (-1, -1), 4),
      ("TOPPADDING", (0, 0), (-1, -1), 3),
      ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])
  )
  return tbl


def md_to_flowables(md_text: str) -> list:
  st = _styles()
  flow: list = []
  lines = md_text.splitlines()
  i = 0
  in_code = False
  code_buf: list[str] = []

  while i < len(lines):
    line = lines[i]

    if line.strip().startswith("```"):
      if in_code:
        flow.append(Preformatted("\n".join(code_buf), st["code"]))
        flow.append(Spacer(1, 6))
        code_buf = []
        in_code = False
      else:
        in_code = True
      i += 1
      continue

    if in_code:
      code_buf.append(line)
      i += 1
      continue

    if line.strip().startswith("|"):
      table_lines = []
      while i < len(lines) and lines[i].strip().startswith("|"):
        table_lines.append(lines[i])
        i += 1
      tbl = _parse_table(table_lines)
      if tbl:
        flow.append(Spacer(1, 4))
        flow.append(tbl)
        flow.append(Spacer(1, 8))
      continue

    if line.startswith("# "):
      if flow and not isinstance(flow[-1], PageBreak):
        flow.append(PageBreak())
      flow.append(Paragraph(_escape_xml(line[2:].strip()), st["h1"]))
      i += 1
      continue
    if line.startswith("## "):
      flow.append(Paragraph(_escape_xml(line[3:].strip()), st["h2"]))
      i += 1
      continue
    if line.startswith("### "):
      flow.append(Paragraph(_escape_xml(line[4:].strip()), st["h3"]))
      i += 1
      continue

    if line.strip() == "---":
      flow.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
      flow.append(Spacer(1, 6))
      i += 1
      continue

    if line.strip().startswith("- "):
      text = _escape_xml(line.strip()[2:])
      text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
      text = re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", text)
      flow.append(Paragraph(f"• {text}", st["bullet"]))
      i += 1
      continue

    if line.strip().startswith("$$"):
      formula_lines = [line.strip().strip("$")]
      i += 1
      while i < len(lines) and not lines[i].strip().endswith("$$"):
        formula_lines.append(lines[i])
        i += 1
      if i < len(lines):
        formula_lines.append(lines[i].strip().strip("$"))
        i += 1
      flow.append(Preformatted("\n".join(formula_lines), st["formula"]))
      flow.append(Spacer(1, 4))
      continue

    stripped = line.strip()
    if stripped:
      text = _escape_xml(stripped)
      text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
      text = re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", text)
      flow.append(Paragraph(text, st["body"]))
    i += 1

  return flow


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
    title="BTC-Predictor Bot Design Report",
    author="BTC-Predictor",
  )
  st = _styles()
  story: list = [
    Paragraph("BTC-Predictor", st["title"]),
    Paragraph("Trading Bot Architecture &amp; Design Report", st["h2"]),
    Paragraph(
      f"Generated from {md_path.name} · Repository: btc-predictor",
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
