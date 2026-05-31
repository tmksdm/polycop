from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app_config import MAX_TRACKED_TRADERS, is_probably_evm_address
from leaderboard_client import LeaderboardEntry


logger = logging.getLogger("polycop")

# Отдельная Console для лидерборда.
# Это НЕ live-дашборд watcher'а — здесь обычный вывод и input().
console = Console()


def render_leaderboard_table(
    entries: list[LeaderboardEntry],
    *,
    time_period: str,
    order_by: str,
) -> None:
    """
    Печатает таблицу лидерборда в терминал.

    Это разовый вывод, не live-режим.
    """
    table = Table(
        show_header=True,
        header_style="bold cyan",
        title=f"Polymarket Leaderboard  |  period={time_period}  order={order_by}",
        title_style="bold",
        expand=True,
    )

    table.add_column("#", width=3, justify="right")
    table.add_column("Score", width=8, justify="right")
    table.add_column("Trader")
    table.add_column("PnL", width=14, justify="right")
    table.add_column("Volume", width=14, justify="right")
    table.add_column("Recent 7d", width=10, justify="right")
    table.add_column("Last trade", width=12, justify="right")
    table.add_column("Wallet", width=16)

    for index, entry in enumerate(entries, start=1):
        # Имя трейдера + пометки, которые помогают отличить реального человека.
        name = Text(entry.username or "—")
        if entry.verified:
            name.append("  ✓", style="green")
        if entry.x_username:
            name.append(f"  @{entry.x_username}", style="dim")

        table.add_row(
            str(index),
            f"{entry.score}",
            name,
            _fmt_money(entry.pnl_usdc),
            _fmt_money(entry.volume_usdc),
            str(entry.recent_trade_count),
            _fmt_last_trade(entry.hours_since_last_trade),
            _short_wallet(entry.proxy_wallet),
        )

    console.print(table)


def prompt_and_save_selection(
    entries: list[LeaderboardEntry],
    config_path: Path,
) -> None:
    """
    Спрашивает у пользователя номера трейдеров и сохраняет их в config.json.

    Защиты:
    - принимаем только корректные номера из таблицы;
    - не больше MAX_TRACKED_TRADERS (5);
    - перед перезаписью делаем бэкап config.json.bak;
    - сохраняем остальные секции конфига (risk, sell) без изменений.
    """
    console.print()
    console.print(
        Panel(
            Text.from_markup(
                "Введи [bold]номера[/bold] трейдеров через запятую, чтобы добавить их в watched list.\n"
                f"Максимум [bold]{MAX_TRACKED_TRADERS}[/bold]. Пустой ввод — выйти без изменений.\n"
                "Пример: [cyan]1, 3, 5[/cyan]"
            ),
            title="Выбор трейдеров",
            border_style="cyan",
        )
    )

    raw_input_value = console.input("[bold cyan]Твой выбор:[/bold cyan] ").strip()

    if not raw_input_value:
        console.print("[yellow]Ничего не выбрано. config.json не изменён.[/yellow]")
        return

    selected_indexes = _parse_selection(raw_input_value, max_index=len(entries))

    if not selected_indexes:
        console.print("[red]Не понял ввод. Ожидаю номера через запятую, например 1, 3, 5.[/red]")
        console.print("[yellow]config.json не изменён.[/yellow]")
        return

    if len(selected_indexes) > MAX_TRACKED_TRADERS:
        console.print(
            f"[yellow]Выбрано больше {MAX_TRACKED_TRADERS}. "
            f"Беру первые {MAX_TRACKED_TRADERS}.[/yellow]"
        )
        selected_indexes = selected_indexes[:MAX_TRACKED_TRADERS]

    # По номерам достаём адреса.
    selected_wallets: list[str] = []
    for index in selected_indexes:
        wallet = entries[index - 1].proxy_wallet
        if is_probably_evm_address(wallet) and wallet not in selected_wallets:
            selected_wallets.append(wallet)

    if not selected_wallets:
        console.print("[red]Не удалось получить корректные адреса. config.json не изменён.[/red]")
        return

    _write_traders_to_config(config_path, selected_wallets)

    console.print()
    console.print(
        Panel(
            _build_success_text(selected_wallets, config_path),
            title="Готово",
            border_style="green",
        )
    )


def _build_success_text(wallets: list[str], config_path: Path) -> Text:
    """
    Собирает текст об успешном сохранении.
    """
    text = Text()
    text.append("В watched list записано трейдеров: ", style="bold")
    text.append(f"{len(wallets)}\n", style="green")

    for wallet in wallets:
        text.append(f"  • {wallet}\n")

    text.append("\nФайл: ", style="bold")
    text.append(f"{config_path}\n")
    text.append("Бэкап старого конфига: ", style="dim")
    text.append(f"{config_path}.bak\n", style="dim")
    text.append("\nТеперь запусти обычный режим: ", style="bold")
    text.append("python main.py", style="cyan")

    return text


def _parse_selection(raw_value: str, max_index: int) -> list[int]:
    """
    Разбирает строку вида "1, 3, 5" в список номеров.

    Игнорирует мусор, дубли и номера вне диапазона таблицы.
    Сохраняет порядок ввода.
    """
    result: list[int] = []

    for chunk in raw_value.replace(";", ",").split(","):
        token = chunk.strip()

        if not token:
            continue

        if not token.isdigit():
            continue

        number = int(token)

        if number < 1 or number > max_index:
            continue

        if number not in result:
            result.append(number)

    return result


def _write_traders_to_config(config_path: Path, wallets: list[str]) -> None:
    """
    Записывает выбранных трейдеров в config.json.

    Важно:
    - сохраняем существующие секции (risk, sell), чтобы не сбросить настройки;
    - делаем бэкап старого файла;
    - используем pathlib и UTF-8, никаких виндовых путей.
    """
    payload: dict[str, Any] = {}

    # Читаем существующий конфиг, если он есть, чтобы не потерять risk/sell.
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = existing
        except json.JSONDecodeError:
            logger.warning("Старый config.json повреждён. Создам новый, бэкап всё равно сохраню.")

        # Бэкап перед перезаписью.
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")
        backup_path.write_text(
            config_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    # Обновляем только секцию traders.
    payload["traders"] = wallets

    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fmt_money(value: Decimal) -> str:
    """
    Форматирует деньги с разделителями тысяч.

    Пример: 1234567.89 -> $1,234,568
    Округляем до целых долларов — для лидерборда копейки не нужны.
    """
    rounded = value.quantize(Decimal("1"))
    return f"${rounded:,}"


def _fmt_last_trade(hours_since: float | None) -> str:
    """
    Человекочитаемое "сколько назад торговал".
    """
    if hours_since is None:
        return "—"

    if hours_since < 1:
        return "<1h"

    if hours_since < 48:
        return f"{int(hours_since)}h"

    days = int(hours_since // 24)
    return f"{days}d"


def _short_wallet(wallet: str) -> str:
    """
    Сокращает адрес для таблицы.
    """
    if len(wallet) <= 14:
        return wallet

    return f"{wallet[:6]}...{wallet[-4:]}"
