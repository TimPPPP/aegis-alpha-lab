"""Unit coverage for Polygon loader behavior that does not hit Polygon."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from aegis.data import polygon_loader as loader_module


class _FakeClient:
    pass


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [pd.Timestamp("2025-01-02")],
            "raw_close": [100.0],
            "adj_close": [100.0],
            "volume": [1_000_000.0],
        }
    )


def test_load_polygon_daily_strict_mode_rejects_partial_ticker_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Week 1's fixed universe should not silently shrink on one API miss."""

    def _fake_meta(client: object, ticker: str, **kwargs: object) -> object:
        if ticker == "BAD":
            raise RuntimeError("not found")
        return loader_module._TickerMeta(  # pyright: ignore[reportPrivateUsage]
            ticker=ticker,
            ticker_type="CS",
            exchange="NYSE",
            shares_out=1_000_000.0,
        )

    monkeypatch.setattr(loader_module, "_fetch_ticker_meta", _fake_meta)
    monkeypatch.setattr(loader_module, "_fetch_daily_bars", lambda *_, **__: _bars())

    with pytest.raises(RuntimeError, match="did not return all requested tickers"):
        loader_module.load_polygon_daily(
            ["GOOD", "BAD"],
            start=date(2025, 1, 1),
            end=date(2025, 1, 31),
            client=_FakeClient(),  # type: ignore[arg-type]
            sleep_between_calls=0,
            require_all_tickers=True,
        )


def test_load_polygon_daily_tolerant_mode_keeps_successful_tickers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full-universe runs may keep going when a small number of names fail."""

    def _fake_meta(client: object, ticker: str, **kwargs: object) -> object:
        if ticker == "BAD":
            raise RuntimeError("not found")
        return loader_module._TickerMeta(  # pyright: ignore[reportPrivateUsage]
            ticker=ticker,
            ticker_type="CS",
            exchange="NYSE",
            shares_out=1_000_000.0,
        )

    monkeypatch.setattr(loader_module, "_fetch_ticker_meta", _fake_meta)
    monkeypatch.setattr(loader_module, "_fetch_daily_bars", lambda *_, **__: _bars())

    out = loader_module.load_polygon_daily(
        ["GOOD", "BAD"],
        start=date(2025, 1, 1),
        end=date(2025, 1, 31),
        client=_FakeClient(),  # type: ignore[arg-type]
        sleep_between_calls=0,
        require_all_tickers=False,
    )

    assert sorted(out["ticker"].unique()) == ["GOOD"]
