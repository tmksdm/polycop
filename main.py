from __future__ import annotations

import argparse
import asyncio

from clob_check_mode import run_clob_check_mode
from leaderboard_mode import run_leaderboard_mode
from logging_setup import setup_logger
from watcher import run_watcher_mode


# Настраиваем логгер один раз при старте, до выбора режима.
# Разовые режимы (лидерборд, clob-check) потом перенастроят его
# через configure_plain_logger, watcher — через configure_logger_for_ui.
setup_logger()


def parse_cli_args() -> argparse.Namespace:
    """
    Разбирает аргументы командной строки.

    Без аргументов — обычный режим watcher (как раньше).
    С флагом --leaderboard — режим лидерборда.
    С флагом --clob-check — read-only проверка CLOB.
    """
    parser = argparse.ArgumentParser(
        description="Polymarket Shadow Trader (polycop)",
    )

    parser.add_argument(
        "--leaderboard",
        action="store_true",
        help="Показать лидерборд активных трейдеров и выбрать кого копировать.",
    )

    parser.add_argument(
        "--clob-check",
        action="store_true",
        help="Read-only проверка связи с Polymarket CLOB (Этап 5.2). Ордера не отправляются.",
    )

    parser.add_argument(
        "--use-wallet-file",
        action="store_true",
        help="Для --clob-check: взять боевой ключ из зашифрованного файла (спросит пароль).",
    )

    parser.add_argument(
        "--use-temp-key",
        action="store_true",
        help="Для --clob-check: использовать одноразовый сгенерированный ключ (по умолчанию).",
    )

    parser.add_argument(
        "--period",
        choices=["DAY", "WEEK", "MONTH", "ALL"],
        default="MONTH",
        help="Период лидерборда (по умолчанию MONTH).",
    )

    parser.add_argument(
        "--order",
        choices=["PNL", "VOL"],
        default="PNL",
        help="По чему сортировать исходный топ (по умолчанию PNL).",
    )

    parser.add_argument(
        "--category",
        choices=[
            "OVERALL",
            "POLITICS",
            "SPORTS",
            "CRYPTO",
            "CULTURE",
            "MENTIONS",
            "WEATHER",
            "ECONOMICS",
            "TECH",
            "FINANCE",
        ],
        default="OVERALL",
        help="Категория рынков (по умолчанию OVERALL).",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Сколько трейдеров запросить из топа (1–50, по умолчанию 25).",
    )

    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Не ходить в /activity за свежестью (быстрее, но без freshness-score).",
    )

    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_cli_args()

    try:
        if cli_args.clob_check:
            # Этап 5.2 — read-only проверка CLOB.
            # Это синхронный режим, без asyncio: параллелить тут нечего.
            run_clob_check_mode(cli_args)
        elif cli_args.leaderboard:
            # Разовый режим лидерборда.
            asyncio.run(run_leaderboard_mode(cli_args))
        else:
            # Обычный режим watcher (как было раньше).
            asyncio.run(run_watcher_mode())
    except KeyboardInterrupt:
        # На Windows Ctrl+C иногда доходит сюда уже после отмены asyncio-задач.
        # Это нормальная остановка, не авария.
        pass
