"""Coverage for loop.py cost-ledger functions + pricing.

The cost ledger is the only path that protects operators from runaway
Anthropic bills. If `today_spend` under-reports, the daily cap won't
kick in. If `_price_call` under-bills, the ledger lies. Both are
load-bearing for the no-cheating + financial-safety properties.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import loop as L  # type: ignore[import-not-found]


# ────────── _today_iso ──────────

def test_today_iso_format():
    out = L._today_iso()
    # YYYY-MM-DD
    assert len(out) == 10
    assert out[4] == "-" and out[7] == "-"
    int(out[:4]); int(out[5:7]); int(out[8:10])  # parses


# ────────── _record_cost ──────────

def test_record_cost_writes_jsonl_row(tmp_path):
    ledger = tmp_path / "led.jsonl"
    L._record_cost(ledger, task_id="t1", usd=0.0123, step=4)
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["task_id"] == "t1"
    assert rows[0]["step"] == 4
    assert rows[0]["usd"] == 0.0123
    assert "ts" in rows[0]
    assert "date" in rows[0]


def test_record_cost_creates_parent_dir(tmp_path):
    """Operator might point at a path whose parent doesn't exist yet."""
    ledger = tmp_path / "nested" / "deeper" / "led.jsonl"
    L._record_cost(ledger, "t1", 0.001, 1)
    assert ledger.exists()


def test_record_cost_appends_not_overwrites(tmp_path):
    ledger = tmp_path / "led.jsonl"
    L._record_cost(ledger, "t1", 0.01, 1)
    L._record_cost(ledger, "t2", 0.02, 1)
    L._record_cost(ledger, "t1", 0.03, 2)
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 3
    assert [r["task_id"] for r in rows] == ["t1", "t2", "t1"]


def test_record_cost_rounds_to_6_decimals(tmp_path):
    """We round at write time so the ledger stays human-readable."""
    ledger = tmp_path / "led.jsonl"
    L._record_cost(ledger, "t1", 0.0123456789, 1)
    row = json.loads(ledger.read_text().splitlines()[0])
    # 6-decimal round
    assert row["usd"] == 0.012346


def test_record_cost_records_today_iso(tmp_path):
    ledger = tmp_path / "led.jsonl"
    L._record_cost(ledger, "t1", 0.01, 1)
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["date"] == L._today_iso()


def test_record_cost_zero_usd_still_recorded(tmp_path):
    """Edge case: a cached-input step might cost $0; record it anyway."""
    ledger = tmp_path / "led.jsonl"
    L._record_cost(ledger, "t1", 0.0, 1)
    assert ledger.exists()
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["usd"] == 0.0


# ────────── today_spend ──────────

def test_today_spend_zero_when_no_ledger_file(tmp_path):
    """Pre-flight check should never crash on a fresh-install operator."""
    ledger = tmp_path / "missing.jsonl"
    assert L.today_spend(ledger) == 0.0


def test_today_spend_sums_today_only(tmp_path):
    ledger = tmp_path / "led.jsonl"
    # 2 today, 1 yesterday — only today's should count
    yesterday = "2025-01-01"
    today = L._today_iso()
    ledger.write_text(
        json.dumps({"date": today, "usd": 0.10}) + "\n"
        + json.dumps({"date": today, "usd": 0.20}) + "\n"
        + json.dumps({"date": yesterday, "usd": 99.0}) + "\n"
    )
    assert L.today_spend(ledger) == pytest.approx(0.30)


def test_today_spend_skips_malformed_lines(tmp_path):
    """Operator hand-editing the ledger shouldn't crash the loop."""
    ledger = tmp_path / "led.jsonl"
    today = L._today_iso()
    ledger.write_text(
        json.dumps({"date": today, "usd": 0.10}) + "\n"
        + "not json\n"
        + json.dumps({"date": today, "usd": 0.05}) + "\n"
    )
    assert L.today_spend(ledger) == pytest.approx(0.15)


def test_today_spend_skips_rows_missing_date_field(tmp_path):
    """Defensive — if a row is missing date, skip it (don't count it)."""
    ledger = tmp_path / "led.jsonl"
    ledger.write_text(json.dumps({"usd": 999}) + "\n")
    assert L.today_spend(ledger) == 0.0


def test_today_spend_skips_rows_with_string_usd(tmp_path):
    """A bad serializer might write usd as a string — coerce or skip."""
    ledger = tmp_path / "led.jsonl"
    today = L._today_iso()
    # float("not-a-number") raises ValueError → caught + skipped
    ledger.write_text(
        json.dumps({"date": today, "usd": "broken"}) + "\n"
        + json.dumps({"date": today, "usd": 0.05}) + "\n"
    )
    assert L.today_spend(ledger) == pytest.approx(0.05)


def test_today_spend_empty_file(tmp_path):
    ledger = tmp_path / "led.jsonl"
    ledger.write_text("")
    assert L.today_spend(ledger) == 0.0


def test_today_spend_handles_trailing_newline(tmp_path):
    """splitlines() naturally drops a trailing empty — make sure that's OK."""
    ledger = tmp_path / "led.jsonl"
    today = L._today_iso()
    ledger.write_text(json.dumps({"date": today, "usd": 0.07}) + "\n\n\n")
    assert L.today_spend(ledger) == pytest.approx(0.07)


# ────────── _price_call ──────────

def test_price_call_sonnet_4_5_pricing():
    """Sonnet 4.5: $3/MTok input, $15/MTok output (as of 2026-05)."""
    cost = L._price_call("claude-sonnet-4-5", in_tokens=1_000_000, out_tokens=0)
    assert cost == pytest.approx(3.0)


def test_price_call_sonnet_4_5_output_only():
    cost = L._price_call("claude-sonnet-4-5", in_tokens=0, out_tokens=1_000_000)
    assert cost == pytest.approx(15.0)


def test_price_call_sonnet_4_5_mixed():
    """500K in + 100K out = 0.5*3 + 0.1*15 = 1.5 + 1.5 = 3.0"""
    cost = L._price_call("claude-sonnet-4-5", in_tokens=500_000, out_tokens=100_000)
    assert cost == pytest.approx(3.0)


def test_price_call_opus_4_5_is_5x_sonnet():
    """Opus is 5x the price of Sonnet on both rails."""
    sonnet = L._price_call("claude-sonnet-4-5", in_tokens=1_000_000, out_tokens=1_000_000)
    opus = L._price_call("claude-opus-4-5", in_tokens=1_000_000, out_tokens=1_000_000)
    assert opus == pytest.approx(sonnet * 5)


def test_price_call_zero_tokens_is_zero_usd():
    assert L._price_call("claude-sonnet-4-5", in_tokens=0, out_tokens=0) == 0.0


def test_price_call_unknown_model_falls_back_to_sonnet(caplog):
    """When Anthropic ships a new model name we haven't priced, we charge
    at Sonnet rates AND log a warning so the operator can audit."""
    import logging
    with caplog.at_level(logging.WARNING):
        cost = L._price_call("claude-sonnet-9000", in_tokens=1_000_000, out_tokens=0)
    assert cost == pytest.approx(3.0)
    # The warning was emitted
    assert any("claude-sonnet-9000" in r.message for r in caplog.records)


def test_price_call_realistic_step_cost():
    """A typical Computer Use step: ~10K input (history + screenshot), ~500
    output (a tool_use block). At Sonnet rates that's ~$0.04."""
    cost = L._price_call("claude-sonnet-4-5", in_tokens=10_000, out_tokens=500)
    # 10K/1M*3 + 500/1M*15 = 0.03 + 0.0075 = 0.0375
    assert cost == pytest.approx(0.0375, abs=1e-6)


def test_price_call_handles_small_token_counts():
    """Even a 1-token request should price correctly (no zero-division)."""
    cost = L._price_call("claude-sonnet-4-5", in_tokens=1, out_tokens=1)
    expected = (1 / 1_000_000.0) * 3.0 + (1 / 1_000_000.0) * 15.0
    assert cost == pytest.approx(expected)


# ────────── Pricing table integrity ──────────

def test_pricing_table_has_required_models():
    """Sonnet 4.5 + Opus 4.5 are the documented MODEL env defaults."""
    assert "claude-sonnet-4-5" in L.PRICING_USD_PER_MTOK
    assert "claude-opus-4-5" in L.PRICING_USD_PER_MTOK


def test_pricing_table_has_input_and_output_keys():
    for model, prices in L.PRICING_USD_PER_MTOK.items():
        assert "input" in prices, f"{model} missing input price"
        assert "output" in prices, f"{model} missing output price"
        assert prices["input"] > 0
        assert prices["output"] > 0


def test_pricing_table_output_always_greater_than_input():
    """Anthropic pricing rule of thumb — if we ever flip these by accident
    the budget enforcement would be way off."""
    for model, prices in L.PRICING_USD_PER_MTOK.items():
        assert prices["output"] >= prices["input"], \
            f"{model}: output ({prices['output']}) < input ({prices['input']})"


# ────────── Module-level config ──────────

def test_model_default_is_sonnet_4_5(monkeypatch):
    """If ANTHROPIC_MODEL is unset, we default to Sonnet 4.5 (the cheaper
    tier — Opus only on operator opt-in)."""
    import importlib
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    importlib.reload(L)
    assert L.MODEL == "claude-sonnet-4-5"


def test_model_env_override(monkeypatch):
    import importlib
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-5")
    importlib.reload(L)
    assert L.MODEL == "claude-opus-4-5"


def test_computer_tool_version_default(monkeypatch):
    """The 2025-11 tool version is the current stable Anthropic ships."""
    import importlib
    monkeypatch.delenv("COMPUTER_TOOL_VERSION", raising=False)
    importlib.reload(L)
    assert L.COMPUTER_TOOL_VERSION == "computer_20251124"


def test_anthropic_beta_default(monkeypatch):
    """The beta header pin is fresh-as-of 2025-01 (Anthropic's release tag)."""
    import importlib
    monkeypatch.delenv("ANTHROPIC_BETA", raising=False)
    importlib.reload(L)
    assert L.ANTHROPIC_BETA == "computer-use-2025-01-24"
