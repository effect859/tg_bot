"""Стоимость вызова модели в USD по usage ответа провайдера (без БД и сети).

Стоимость пишется в llm_calls.cost_usd. Если её нельзя посчитать достоверно —
неизвестная модель, нет разбивки записи в кэш по сроку хранения, разбивка не
сходится с суммой, нестандартный тариф или регион, некорректные числа, — результат
None (в БД — NULL), а не 0: ноль занизил бы себестоимость. Вызов без ответа
провайдера (тайм-аут, обрыв) стоимости тоже не получает, хотя провайдер мог его
оплатить: сумма известных стоимостей — нижняя граница расходов, сверка — с консолью
провайдера.

Токены считаются по полям usage Anthropic, каждое по своему тарифу: input_tokens —
только некэшированный остаток запроса, чтение из кэша и запись в него — отдельные
поля (полный вход — их сумма), поэтому двойного учёта нет. Размышления модели
входят в output_tokens и оплачиваются как выход.

Тарифы — platform.claude.com/docs/en/about-claude/pricing (проверено 2026-10-11).
Модель ищется по точному идентификатору из ответа провайдера.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.llm.base import LLMUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD за 1 млн токенов."""

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal


MODEL_PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5-5": ModelPrice(
        input=Decimal("2"),
        output=Decimal("10"),
        cache_write_5m=Decimal("2.50"),
        cache_write_1h=Decimal("4"),
        cache_read=Decimal("0.10"),
    ),
}

# Множитель всех тарифов по региону обработки. None — регион не сообщён: бот регион
# не запрашивает, а по умолчанию запрос обрабатывается глобально (обычная цена).
GEO_MULTIPLIERS: dict[str | None, Decimal] = {
    None: Decimal("1"),
    "global": Decimal("1"),
    "us": Decimal("1.1"),
}
# Бот не запрашивает приоритетный тариф и пакетную обработку: не сообщённый тариф —
# обычный. Остальные тарифы считаются по другим ценам — стоимость неизвестна.
STANDARD_SERVICE_TIERS = frozenset({None, "standard"})

# Точность и предел колонки llm_calls.cost_usd (Numeric(10, 6)).
COST_QUANTUM = Decimal("0.000001")
MAX_COST_USD = Decimal("9999.999999")
_MILLION = Decimal(1_000_000)


def cost_usd(model: str, usage: LLMUsage) -> Decimal | None:
    """Стоимость одного вызова в USD или None, если её нельзя посчитать достоверно."""
    price = MODEL_PRICES.get(model) if isinstance(model, str) else None
    if price is None:
        return None
    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_tokens,
        usage.cache_creation_tokens,
    )
    if not all(_is_count(count) for count in counts):
        return None
    if usage.service_tier not in STANDARD_SERVICE_TIERS:
        return None
    multiplier = GEO_MULTIPLIERS.get(usage.inference_geo)
    if multiplier is None:
        return None
    writes = _cache_writes(usage)
    if writes is None:
        return None
    write_5m, write_1h = writes

    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cached_tokens * price.cache_read
        + write_5m * price.cache_write_5m
        + write_1h * price.cache_write_1h
    ) / _MILLION
    cost = (total * multiplier).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)
    return cost if cost <= MAX_COST_USD else None


def log_call_usage(step: str, call_id: int, model: str, usage: LLMUsage) -> None:
    """Строка лога с расходом вызова: только числа, без текста запроса и ответа."""
    logger.info(
        "LLM call usage: step=%s call_id=%s model=%s input=%s output=%s cache_read=%s "
        "cache_write=%s cache_write_5m=%s cache_write_1h=%s service_tier=%s "
        "inference_geo=%s cost_usd=%s",
        step,
        call_id,
        model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_tokens,
        usage.cache_creation_tokens,
        usage.cache_write_5m_tokens,
        usage.cache_write_1h_tokens,
        usage.service_tier,
        usage.inference_geo,
        cost_usd(model, usage),
    )


def _cache_writes(usage: LLMUsage) -> tuple[int, int] | None:
    """Запись в кэш (5 минут, 1 час) или None, если тариф записи неизвестен."""
    write_5m, write_1h = usage.cache_write_5m_tokens, usage.cache_write_1h_tokens
    if write_5m is None and write_1h is None:
        # Разбивки нет: без записи тариф не нужен, с записью — неизвестен.
        return (0, 0) if usage.cache_creation_tokens == 0 else None
    if not (isinstance(write_5m, int) and isinstance(write_1h, int)):
        return None
    if not (_is_count(write_5m) and _is_count(write_1h)):
        return None
    if write_5m + write_1h != usage.cache_creation_tokens:
        return None
    return write_5m, write_1h


def _is_count(value: object) -> bool:
    # bool — подкласс int, но числом токенов не бывает.
    return type(value) is int and value >= 0
