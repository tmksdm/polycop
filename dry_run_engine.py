from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation

from app_config import AppConfig
from trade_models import DecodedTrade, MarketMetadata


@dataclass(frozen=True)
class DryRunDecision:
    """
    Результат DRY-RUN оценки сделки.

    accepted:
    - True = сделку бы скопировали;
    - False = сделку пропустили.

    reason — человекочитаемая причина.
    """

    accepted: bool
    reason: str
    trade: DecodedTrade
    metadata: MarketMetadata | None
    copy_size_usdc: Decimal
    copy_shares: Decimal
    hourly_limit_usdc: Decimal
    hourly_spent_before: Decimal
    hourly_spent_after: Decimal


class HourlySpendTracker:
    """
    Простой in-memory счётчик расходов за текущий час.

    In-memory означает:
    - хранится только в памяти Python;
    - после перезапуска сбрасывается.

    На этом шаге это нормально.
    Позже, когда подключим SQLite, будем хранить историю надёжнее.
    """

    def __init__(self) -> None:
        self._hour_key = self._current_hour_key()
        self._spent_usdc = Decimal("0")

    def get_spent_usdc(self) -> Decimal:
        """
        Возвращает потраченный DRY-RUN бюджет за текущий час.
        """
        self._reset_if_new_hour()
        return self._spent_usdc

    def reserve(self, amount_usdc: Decimal) -> None:
        """
        Резервирует сумму как будто мы её потратили.

        Важно:
        это только DRY-RUN учёт, реальные деньги не используются.
        """
        self._reset_if_new_hour()
        self._spent_usdc += amount_usdc

    def _reset_if_new_hour(self) -> None:
        """
        Если наступил новый час — сбрасываем счётчик.
        """
        current_hour_key = self._current_hour_key()

        if current_hour_key != self._hour_key:
            self._hour_key = current_hour_key
            self._spent_usdc = Decimal("0")

    @staticmethod
    def _current_hour_key() -> str:
        """
        Ключ часа в UTC.

        UTC используем, чтобы не зависеть от часового пояса Windows/VPS.
        """
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d-%H")


def evaluate_dry_run_copy(
    *,
    trade: DecodedTrade,
    metadata: MarketMetadata | None,
    config: AppConfig,
    hourly_tracker: HourlySpendTracker,
) -> DryRunDecision:
    """
    Проверяет сделку по фильтрам и считает размер копии.

    На Этапе 3 мы ничего не отправляем на биржу.
    Только пишем в лог, что бот сделал бы в будущем LIVE-режиме.
    """
    risk = config.risk

    hourly_limit_usdc = (
        risk.dry_run_balance_usdc * risk.hourly_limit_percent / Decimal("100")
    )

    hourly_spent_before = hourly_tracker.get_spent_usdc()
    hourly_spent_after = hourly_spent_before

    zero = Decimal("0")

    if trade.size_usdc < risk.min_bet_usdc:
        return DryRunDecision(
            accepted=False,
            reason=(
                f"trader size ${_fmt(trade.size_usdc)} меньше min_bet "
                f"${_fmt(risk.min_bet_usdc)}"
            ),
            trade=trade,
            metadata=metadata,
            copy_size_usdc=zero,
            copy_shares=zero,
            hourly_limit_usdc=hourly_limit_usdc,
            hourly_spent_before=hourly_spent_before,
            hourly_spent_after=hourly_spent_after,
        )

    price_cents = trade.price * Decimal("100")

    if price_cents < risk.min_price_cents or price_cents > risk.max_price_cents:
        return DryRunDecision(
            accepted=False,
            reason=(
                f"price {_fmt(price_cents)}¢ вне диапазона "
                f"{_fmt(risk.min_price_cents)}–{_fmt(risk.max_price_cents)}¢"
            ),
            trade=trade,
            metadata=metadata,
            copy_size_usdc=zero,
            copy_shares=zero,
            hourly_limit_usdc=hourly_limit_usdc,
            hourly_spent_before=hourly_spent_before,
            hourly_spent_after=hourly_spent_after,
        )

    if trade.side not in {"BUY", "SELL"}:
        return DryRunDecision(
            accepted=False,
            reason=f"неизвестная сторона сделки: {trade.side}",
            trade=trade,
            metadata=metadata,
            copy_size_usdc=zero,
            copy_shares=zero,
            hourly_limit_usdc=hourly_limit_usdc,
            hourly_spent_before=hourly_spent_before,
            hourly_spent_after=hourly_spent_after,
        )

    copy_size_usdc = trade.size_usdc * risk.ratio_percent / Decimal("100")

    try:
        copy_shares = copy_size_usdc / trade.price
    except (DivisionByZero, InvalidOperation):
        return DryRunDecision(
            accepted=False,
            reason="не смог посчитать shares: цена равна нулю или некорректна",
            trade=trade,
            metadata=metadata,
            copy_size_usdc=zero,
            copy_shares=zero,
            hourly_limit_usdc=hourly_limit_usdc,
            hourly_spent_before=hourly_spent_before,
            hourly_spent_after=hourly_spent_after,
        )

    if trade.side == "SELL":
        if config.sell.sell_mode == "Ignore":
            return DryRunDecision(
                accepted=False,
                reason="SELL сделка пропущена, потому что sell_mode=Ignore",
                trade=trade,
                metadata=metadata,
                copy_size_usdc=zero,
                copy_shares=zero,
                hourly_limit_usdc=hourly_limit_usdc,
                hourly_spent_before=hourly_spent_before,
                hourly_spent_after=hourly_spent_after,
            )

        # На Этапе 3 мы только симулируем SELL.
        # Реально продавать можно будет после появления менеджера позиций на Этапе 6.
        return DryRunDecision(
            accepted=True,
            reason=(
                f"DRY-RUN SELL simulation, sell_mode={config.sell.sell_mode}; "
                "реальное mirror-sell будет на Этапе 6"
            ),
            trade=trade,
            metadata=metadata,
            copy_size_usdc=copy_size_usdc,
            copy_shares=copy_shares,
            hourly_limit_usdc=hourly_limit_usdc,
            hourly_spent_before=hourly_spent_before,
            hourly_spent_after=hourly_spent_after,
        )

    # Ниже логика BUY.
    remaining_budget = hourly_limit_usdc - hourly_spent_before

    if copy_size_usdc > remaining_budget:
        return DryRunDecision(
            accepted=False,
            reason=(
                f"hourly limit exceeded: нужно ${_fmt(copy_size_usdc)}, "
                f"доступно ${_fmt(max(remaining_budget, zero))}"
            ),
            trade=trade,
            metadata=metadata,
            copy_size_usdc=copy_size_usdc,
            copy_shares=copy_shares,
            hourly_limit_usdc=hourly_limit_usdc,
            hourly_spent_before=hourly_spent_before,
            hourly_spent_after=hourly_spent_after,
        )

    hourly_tracker.reserve(copy_size_usdc)
    hourly_spent_after = hourly_tracker.get_spent_usdc()

    return DryRunDecision(
        accepted=True,
        reason="passed filters",
        trade=trade,
        metadata=metadata,
        copy_size_usdc=copy_size_usdc,
        copy_shares=copy_shares,
        hourly_limit_usdc=hourly_limit_usdc,
        hourly_spent_before=hourly_spent_before,
        hourly_spent_after=hourly_spent_after,
    )


def format_dry_run_decision(decision: DryRunDecision) -> str:
    """
    Форматирует DRY-RUN решение для лога.
    """
    trade = decision.trade
    status = "WOULD COPY" if decision.accepted else "SKIP"

    market_text = "market=unknown"
    outcome_text = "outcome=unknown"

    if decision.metadata is not None:
        market_text = f'market="{_short_text(decision.metadata.question, 80)}"'
        outcome_text = f"outcome={decision.metadata.outcome or 'unknown'}"

    return (
        f"{status} | "
        f"{trade.side} | "
        f"copy_size=${_fmt(decision.copy_size_usdc)} | "
        f"copy_shares={_fmt(decision.copy_shares)} | "
        f"hourly=${_fmt(decision.hourly_spent_after)}/"
        f"${_fmt(decision.hourly_limit_usdc)} | "
        f"{market_text} | "
        f"{outcome_text} | "
        f"reason={decision.reason}"
    )


def _fmt(value: Decimal) -> str:
    """
    Аккуратно форматирует Decimal для логов.
    """
    quant = Decimal("0.01")
    return format(value.quantize(quant), "f")


def _short_text(value: str, max_length: int) -> str:
    """
    Обрезает длинный текст рынка.
    """
    if len(value) <= max_length:
        return value

    return f"{value[: max_length - 3]}..."
