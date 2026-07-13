"""Docker image must ship eth_aligned_index_hourly_bot.yaml for SPX/NDX paper trials."""

from __future__ import annotations

from pathlib import Path


def test_dockerfile_copies_index_hourly_overlay():
  dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
  text = dockerfile.read_text(encoding="utf-8")
  assert "eth_aligned_index_hourly_bot.yaml" in text


def test_index_overlay_file_exists_at_repo_root():
  root = Path(__file__).resolve().parents[1]
  assert (root / "eth_aligned_index_hourly_bot.yaml").is_file()
