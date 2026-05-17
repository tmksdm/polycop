from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Deque

from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app_config import AppConfig
from dry_run_engine import DryRunDecision
from trade_models import DecodedTrade, MarketMetadata


@dataclass(frozen=True)
class RecentTradeRow:
    """
    Короткая строка для таблицы последних сделок.
    """

    side: str
    role: str
    price_cents: str
    size_usdc: str
    outcome: str
    market: str
    trader: str


@dataclass(frozen=True)
class RecentDecisionRow:
    """
    Короткая строка для таблицы последних DRY-RUN решений.
    """

    status: str
    side: str
    copy_size_usdc: str
    hourly: str
    reason: str


@dataclass(frozen=True)
class LogRow:
    """
    Одна строка лога внутри UI.
    """

    level: str
    message: str


class TerminalUI:
    """
    Live-интерфейс в терминале на Rich.

    Важно:
    - UI ничего не знает о WebSocket и блокчейне;
    - watcher просто сообщает ему события;
    - если позже мы захотим GUI, этот слой будет проще заменить.
    """

    def __init__(self, app_config: AppConfig, database_path: Path) -> None:
        self.app_config = app_config
        self.database_path = database_path

        self.started_at = datetime.now()

        self.websocket_status = "starting"
        self.websocket_status_style = "yellow"

        self.raw_tx_count = 0
        self.decoded_trade_count = 0
        self.would_copy_count = 0
        self.skip_count = 0
        self.sqlite_save_count = 0
        self.reconnect_count = 0

        self.last_event_at: datetime | None = None

        self.last_hourly_spent = Decimal("0")
        self.last_hourly_limit = (
            app_config.risk.dry_run_balance_usdc
            * app_config.risk.hourly_limit_percent
            / Decimal("100")
        )

        self.recent_trades: Deque[RecentTradeRow] = deque(maxlen=8)
        self.recent_decisions: Deque[RecentDecisionRow] = deque(maxlen=8)
        self.recent_logs: Deque[LogRow] = deque(maxlen=14)

    async def run(self, stop_event: asyncio.Event) -> None:
        """
        Запускает live-отрисовку UI.

        stop_event — сигнал остановки.
        Когда main.py его выставит, UI аккуратно завершится.
        """
        with Live(
            self.render(),
            refresh_per_second=4,
            screen=True,
            transient=False,
        ) as live:
            while not stop_event.is_set():
                live.update(self.render())
                await asyncio.sleep(0.25)

            # Финальное обновление перед выходом.
            live.update(self.render())

    def set_websocket_status(self, status: str, style: str = "white") -> None:
        """
        Обновляет статус WebSocket-соединения.
        """
        self.websocket_status = status
        self.websocket_status_style = style
        self._touch()

    def mark_reconnect(self) -> None:
        """
        Увеличивает счётчик переподключений.
        """
        self.reconnect_count += 1
        self._touch()

    def record_raw_transaction(self) -> None:
        """
        Фиксирует входящую raw-транзакцию Polymarket Exchange.
        """
        self.raw_tx_count += 1
        self._touch()

    def record_decoded_trades_count(self, count: int) -> None:
        """
        Фиксирует количество декодированных сделок.
        """
        self.decoded_trade_count += count
        self._touch()

    def record_sqlite_save(self) -> None:
        """
        Фиксирует успешное сохранение истории в SQLite.
        """
        self.sqlite_save_count += 1
        self._touch()

    def record_trade(
        self,
        *,
        trade: DecodedTrade,
        metadata: MarketMetadata | None,
    ) -> None:
        """
        Добавляет сделку в таблицу последних сделок.
        """
        market = "unknown"
        outcome = "unknown"

        if metadata is not None:
            market = metadata.question
            outcome = metadata.outcome or "unknown"

        row = RecentTradeRow(
            side=trade.side,
            role=trade.role.upper(),
            price_cents=f"{_fmt_decimal(trade.price * Decimal('100'))}¢",
            size_usdc=f"${_fmt_decimal(trade.size_usdc)}",
            outcome=_short_text(outcome, 16),
            market=_short_text(market, 46),
            trader=_short_address(trade.trader),
        )

        self.recent_trades.appendleft(row)
        self._touch()

    def record_decision(self, decision: DryRunDecision) -> None:
        """
        Добавляет DRY-RUN решение в таблицу последних решений.
        """
        if decision.accepted:
            self.would_copy_count += 1
            status = "WOULD_COPY"
        else:
            self.skip_count += 1
            status = "SKIP"

        self.last_hourly_spent = decision.hourly_spent_after
        self.last_hourly_limit = decision.hourly_limit_usdc

        row = RecentDecisionRow(
            status=status,
            side=decision.trade.side,
            copy_size_usdc=f"${_fmt_decimal(decision.copy_size_usdc)}",
            hourly=(
                f"${_fmt_decimal(decision.hourly_spent_after)}"
                f"/${_fmt_decimal(decision.hourly_limit_usdc)}"
            ),
            reason=_short_text(decision.reason, 54),
        )

        self.recent_decisions.appendleft(row)
        self._touch()

    def add_log(self, level: str, message: str) -> None:
        """
        Добавляет строку в панель логов.
        """
        self.recent_logs.appendleft(LogRow(level=level, message=message))
        self._touch()

    def render(self) -> Group:
        """
        Собирает весь экран из Rich-компонентов.
        """
        return Group(
            self._render_header(),
            self._render_main_grid(),
            self._render_footer(),
        )

    def _render_header(self) -> Panel:
        """
        Верхняя панель: название, режим и статус WebSocket.
        """
        mode = "DRY-RUN" if self.app_config.dry_run else "LIVE"
        mode_style = "green" if self.app_config.dry_run else "bold red"

        title = Text()
        title.append("Polymarket Shadow Trader", style="bold cyan")
        title.append("  |  ")
        title.append(f"Mode: {mode}", style=mode_style)
        title.append("  |  ")
        title.append("WebSocket: ")
        title.append(self.websocket_status, style=self.websocket_status_style)

        return Panel(
            Align.center(title),
            border_style="cyan",
        )

    def _render_main_grid(self) -> Table:
        """
        Основная сетка из трёх колонок:
        - слева статистика и трейдеры;
        - по центру сделки и DRY-RUN решения;
        - справа логи и конфиг.
        """
        grid = Table.grid(expand=True)
        grid.add_column(ratio=28)
        grid.add_column(ratio=44)
        grid.add_column(ratio=28)

        left = Group(
            self._render_stats_panel(),
            self._render_traders_panel(),
        )

        center = Group(
            self._render_trades_panel(),
            self._render_decisions_panel(),
        )

        right = Group(
            self._render_config_panel(),
            self._render_logs_panel(),
        )

        grid.add_row(left, center, right)
        return grid

    def _render_stats_panel(self) -> Panel:
        """
        Панель краткой статистики.
        """
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold")
        table.add_column()

        uptime = datetime.now() - self.started_at
        uptime_text = str(uptime).split(".")[0]

        last_event_text = "—"
        if self.last_event_at is not None:
            last_event_text = self.last_event_at.strftime("%H:%M:%S")

        table.add_row("Uptime", uptime_text)
        table.add_row("Last event", last_event_text)
        table.add_row("Raw tx", str(self.raw_tx_count))
        table.add_row("Decoded trades", str(self.decoded_trade_count))
        table.add_row("WOULD_COPY", f"[green]{self.would_copy_count}[/green]")
        table.add_row("SKIP", f"[yellow]{self.skip_count}[/yellow]")
        table.add_row("SQLite saves", str(self.sqlite_save_count))
        table.add_row("Reconnects", str(self.reconnect_count))
        table.add_row(
            "Hourly",
            (
                f"${_fmt_decimal(self.last_hourly_spent)}"
                f"/${_fmt_decimal(self.last_hourly_limit)}"
            ),
        )

        return Panel(
            table,
            title="Stats",
            border_style="green",
        )

    def _render_traders_panel(self) -> Panel:
        """
        Панель отслеживаемых трейдеров.
        """
        table = Table(show_header=True, header_style="bold cyan", expand=True)
        table.add_column("#", width=3)
        table.add_column("Trader")

        if not self.app_config.watched_traders:
            table.add_row("—", "Все decoded сделки")
        else:
            for index, trader in enumerate(self.app_config.watched_traders, start=1):
                table.add_row(str(index), _short_address(trader))

        return Panel(
            table,
            title="Watched traders",
            border_style="cyan",
        )

    def _render_trades_panel(self) -> Panel:
        """
        Панель последних декодированных сделок.
        """
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Side", width=5)
        table.add_column("Role", width=6)
        table.add_column("Price", width=8)
        table.add_column("Size", width=10)
        table.add_column("Outcome", width=14)
        table.add_column("Market")

        if not self.recent_trades:
            table.add_row("—", "—", "—", "—", "—", "Ждём сделки...")
        else:
            for row in self.recent_trades:
                side_style = "green" if row.side == "BUY" else "red"
                table.add_row(
                    f"[{side_style}]{row.side}[/{side_style}]",
                    row.role,
                    row.price_cents,
                    row.size_usdc,
                    row.outcome,
                    row.market,
                )

        return Panel(
            table,
            title="Latest trades",
            border_style="magenta",
        )

    def _render_decisions_panel(self) -> Panel:
        """
        Панель последних DRY-RUN решений.
        """
        table = Table(show_header=True, header_style="bold yellow", expand=True)
        table.add_column("Status", width=11)
        table.add_column("Side", width=5)
        table.add_column("Copy", width=10)
        table.add_column("Hourly", width=16)
        table.add_column("Reason")

        if not self.recent_decisions:
            table.add_row("—", "—", "—", "—", "Ждём DRY-RUN решения...")
        else:
            for row in self.recent_decisions:
                status_style = "green" if row.status == "WOULD_COPY" else "yellow"
                side_style = "green" if row.side == "BUY" else "red"

                table.add_row(
                    f"[{status_style}]{row.status}[/{status_style}]",
                    f"[{side_style}]{row.side}[/{side_style}]",
                    row.copy_size_usdc,
                    row.hourly,
                    row.reason,
                )

        return Panel(
            table,
            title="DRY-RUN decisions",
            border_style="yellow",
        )

    def _render_config_panel(self) -> Panel:
        """
        Панель текущих риск-настроек.

        Здесь же показываем режим хранения истории:
        - SQLite enabled — история сохраняется, потому что выбраны watched traders;
        - UI only — broad/debug режим, сделки показываются на экране,
          но не пишутся в SQLite, чтобы база не раздувалась.
        """
        risk = self.app_config.risk
        sell = self.app_config.sell

        if self.app_config.watched_traders:
            storage_text = "[green]SQLite enabled[/green]"
        else:
            storage_text = "[yellow]UI only[/yellow]"

        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold")
        table.add_column()

        table.add_row("Ratio", f"{risk.ratio_percent}%")
        table.add_row("Min bet", f"${risk.min_bet_usdc}")
        table.add_row("Hourly limit", f"{risk.hourly_limit_percent}%")
        table.add_row("DRY balance", f"${risk.dry_run_balance_usdc}")
        table.add_row("Price", f"{risk.min_price_cents}–{risk.max_price_cents}¢")
        table.add_row("Sell mode", sell.sell_mode)
        table.add_row("Auto-sell", f"{sell.auto_sell_threshold_cents}¢")
        table.add_row("Storage", storage_text)
        table.add_row("DB", _short_text(str(self.database_path), 38))

        return Panel(
            table,
            title="Config",
            border_style="blue",
        )


    def _render_logs_panel(self) -> Panel:
        """
        Панель последних логов.
        """
        text = Text()

        if not self.recent_logs:
            text.append("Логи появятся после запуска watcher...", style="dim")
        else:
            for row in self.recent_logs:
                style = _style_for_log_level(row.level)
                text.append(row.message, style=style)
                text.append("\n")

        return Panel(
            text,
            title="Logs",
            border_style="white",
        )

    def _render_footer(self) -> Panel:
        """
        Нижняя строка статуса.
        """
        footer = Text()
        footer.append("Ctrl+C", style="bold")
        footer.append(" — остановить приложение. ")
        footer.append("Сейчас реальные деньги не используются в DRY-RUN.", style="green")

        return Panel(
            footer,
            border_style="dim",
        )

    def _touch(self) -> None:
        """
        Обновляет время последнего события.
        """
        self.last_event_at = datetime.now()


class TerminalUILogHandler(logging.Handler):
    """
    logging.Handler, который отправляет логи в TerminalUI.

    То есть вместо обычной печати в терминал строки попадают
    в правую панель Logs.
    """

    def __init__(self, terminal_ui: TerminalUI) -> None:
        super().__init__()
        self.terminal_ui = terminal_ui

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.terminal_ui.add_log(record.levelname, message)
        except Exception:
            # Логгер не должен ронять приложение.
            self.handleError(record)


def configure_logger_for_ui(logger: logging.Logger, terminal_ui: TerminalUI) -> None:
    """
    Перенастраивает logger так, чтобы он писал в Rich UI.

    Это нужно потому, что обычные print/logging строки ломают live-экран.
    """
    logger.handlers.clear()

    ui_handler = TerminalUILogHandler(terminal_ui)
    ui_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    ui_handler.setFormatter(formatter)

    logger.addHandler(ui_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _fmt_decimal(value: Decimal) -> str:
    """
    Форматирует Decimal до двух знаков.
    """
    return format(value.quantize(Decimal("0.01")), "f")


def _short_address(address: str) -> str:
    """
    Сокращает EVM-адрес для таблиц.
    """
    if len(address) <= 14:
        return address

    return f"{address[:6]}...{address[-4:]}"


def _short_text(value: str, max_length: int) -> str:
    """
    Обрезает длинный текст.
    """
    if len(value) <= max_length:
        return value

    return f"{value[: max_length - 3]}..."


def _style_for_log_level(level: str) -> str:
    """
    Цвет строки лога по уровню.
    """
    if level == "ERROR":
        return "bold red"

    if level == "WARNING":
        return "yellow"

    if level == "INFO":
        return "white"

    return "dim"
