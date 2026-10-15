"""Стоимость вызова модели (app.llm.pricing): тарифы, разбивка кэша, неизвестное — None."""

from decimal import Decimal
from typing import Any

import pytest

from app.llm.base import LLMUsage
from app.llm.pricing import MODEL_PRICES, cost_usd, log_call_usage

MODEL = "claude-sonnet-5-5"


def _usage(**fields: Any) -> LLMUsage:
    values: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    values.update(fields)
    return LLMUsage(**values)


def test_official_prices_for_default_model() -> None:
    """Тарифы со страницы цен Anthropic (2026-10-11), USD за 1 млн токенов."""
    price = MODEL_PRICES[MODEL]
    assert (price.input, price.output) == (Decimal("2"), Decimal("10"))
    assert (price.cache_write_5m, price.cache_write_1h) == (Decimal("2.50"), Decimal("4"))
    assert price.cache_read == Decimal("0.10")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"input_tokens": 1_000_000}, "2.000000"),
        ({"output_tokens": 1_000_000}, "10.000000"),
        ({"cached_tokens": 1_000_000}, "0.100000"),
        (
            {
                "cache_creation_tokens": 1_000_000,
                "cache_write_5m_tokens": 1_000_000,
                "cache_write_1h_tokens": 0,
            },
            "2.500000",
        ),
        (
            {
                "cache_creation_tokens": 1_000_000,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 1_000_000,
            },
            "4.000000",
        ),
    ],
    ids=["input", "output", "cache-read", "cache-write-5m", "cache-write-1h"],
)
def test_each_category_has_its_own_price(fields: dict[str, int], expected: str) -> None:
    assert cost_usd(MODEL, _usage(**fields)) == Decimal(expected)


def test_all_categories_summed_without_double_counting() -> None:
    """input_tokens — только некэшированный остаток: кэш по своим тарифам, один раз."""
    usage = _usage(
        input_tokens=1000,
        output_tokens=500,
        cached_tokens=2000,
        cache_creation_tokens=3000,
        cache_write_5m_tokens=1000,
        cache_write_1h_tokens=2000,
    )

    # 1000·2 + 500·10 + 2000·0,10 + 1000·2,50 + 2000·4 = 17 700 (USD·10⁻⁶).
    assert cost_usd(MODEL, usage) == Decimal("0.017700")


def test_zero_tokens_cost_zero() -> None:
    cost = cost_usd(MODEL, _usage())

    assert cost == Decimal("0") and cost is not None


def test_rounding_to_column_precision() -> None:
    # 1 токен чтения из кэша — 0,0000001 USD: меньше точности колонки.
    assert cost_usd(MODEL, _usage(cached_tokens=1)) == Decimal("0.000000")
    assert cost_usd(MODEL, _usage(cached_tokens=5)) == Decimal("0.000001")  # 0,0000005 вверх
    assert cost_usd(MODEL, _usage(input_tokens=1)) == Decimal("0.000002")


def test_cost_is_decimal_with_six_places() -> None:
    cost = cost_usd(MODEL, _usage(input_tokens=123, output_tokens=45))

    assert isinstance(cost, Decimal)
    assert cost.as_tuple().exponent == -6


def test_us_inference_costs_more() -> None:
    usage = _usage(input_tokens=1000, output_tokens=500, inference_geo="us")

    assert cost_usd(MODEL, usage) == Decimal("0.007700")  # 0,007 · 1,1


@pytest.mark.parametrize("geo", [None, "global"])
def test_default_inference_has_standard_price(geo: str | None) -> None:
    usage = _usage(input_tokens=1000, output_tokens=500, inference_geo=geo)

    assert cost_usd(MODEL, usage) == Decimal("0.007000")


@pytest.mark.parametrize("tier", [None, "standard"])
def test_standard_service_tier(tier: str | None) -> None:
    assert cost_usd(MODEL, _usage(input_tokens=1000, service_tier=tier)) == Decimal("0.002000")


# --- Неизвестно — None, а не 0 ---------------------------------------------------------


@pytest.mark.parametrize("model", ["fake-model-2026", "claude-sonnet-5-5-20260101", "", None])
def test_unknown_model_has_no_cost(model: Any) -> None:
    assert cost_usd(model, _usage(input_tokens=1000)) is None


def test_cache_write_without_ttl_breakdown_has_no_cost() -> None:
    """Тариф записи зависит от срока хранения: без разбивки он неизвестен (OD-6)."""
    assert cost_usd(MODEL, _usage(cache_creation_tokens=1000)) is None


def test_no_cache_write_needs_no_breakdown() -> None:
    usage = _usage(input_tokens=1000, cached_tokens=1000)

    assert cost_usd(MODEL, usage) == Decimal("0.002100")


@pytest.mark.parametrize(
    "split",
    [(1000, 1000), (0, 999), (None, 1000), (1000, None), (-1, 1001), (True, 999)],
    ids=["sum-too-big", "sum-too-small", "no-5m", "no-1h", "negative", "bool"],
)
def test_inconsistent_cache_breakdown_has_no_cost(split: tuple[Any, Any]) -> None:
    write_5m, write_1h = split
    usage = _usage(
        cache_creation_tokens=1000,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )

    assert cost_usd(MODEL, usage) is None


@pytest.mark.parametrize("tier", ["priority", "batch", "flex"])
def test_non_standard_service_tier_has_no_cost(tier: str) -> None:
    assert cost_usd(MODEL, _usage(input_tokens=1000, service_tier=tier)) is None


def test_unknown_inference_geo_has_no_cost() -> None:
    assert cost_usd(MODEL, _usage(input_tokens=1000, inference_geo="eu")) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"input_tokens": -1},
        {"output_tokens": -5},
        {"cached_tokens": 1.5},
        {"input_tokens": True},
        {"cache_creation_tokens": None},
    ],
    ids=["negative-input", "negative-output", "float", "bool", "none"],
)
def test_invalid_token_counts_have_no_cost(fields: dict[str, Any]) -> None:
    assert cost_usd(MODEL, _usage(**fields)) is None


def test_cost_above_column_limit_has_no_cost() -> None:
    # 10⁹ выходных токенов — 10 000 USD: больше Numeric(10, 6).
    assert cost_usd(MODEL, _usage(output_tokens=1_000_000_000)) is None


def test_usage_log_has_numbers_only(caplog: pytest.LogCaptureFixture) -> None:
    usage = _usage(
        input_tokens=10,
        output_tokens=20,
        cached_tokens=30,
        cache_creation_tokens=40,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=40,
        service_tier="standard",
        inference_geo="global",
    )

    with caplog.at_level("INFO", logger="app.llm.pricing"):
        log_call_usage("report_generation", 7, MODEL, usage)

    [record] = caplog.records
    message = record.getMessage()
    assert "step=report_generation call_id=7" in message
    assert "input=10 output=20 cache_read=30 cache_write=40" in message
    assert "cache_write_5m=0 cache_write_1h=40" in message
    assert "cost_usd=0.000383" in message  # 10·2 + 20·10 + 30·0,10 + 40·4 = 383
