from __future__ import annotations

import argparse
import logging
from pathlib import Path

from app_config import load_app_config
from leaderboard_client import build_leaderboard
from leaderboard_ui import prompt_and_save_selection, render_leaderboard_table
from logging_setup import configure_plain_logger


PROJECT_ROOT = Path(__file__).resolve().parent

logger = logging.getLogger("polycop")


async def run_leaderboard_mode(args: argparse.Namespace) -> None:
    """
    Режим лидерборда (Этап 4.1).

    Это отдельный разовый режим, не связанный с watcher'ом:
    - тянет топ-трейдеров из Polymarket Data API;
    - считает наш композитный score;
    - показывает таблицу;
    - предлагает добавить выбранных в config.json.

    Здесь НЕ запускается live-дашборд и НЕ трогается WebSocket.
    Поэтому логи здесь обычные, в терминал, а не в Rich Live.
    """
    # Раньше тут была инлайновая перенастройка логгера (та же, что в clob-check).
    # Вынесли её в logging_setup.configure_plain_logger, чтобы не дублировать.
    configure_plain_logger()

    app_config = load_app_config(PROJECT_ROOT)

    logger.info("Режим лидерборда")
    logger.info(
        "Параметры: period=%s | order=%s | category=%s | limit=%s | probe_activity=%s",
        args.period,
        args.order,
        args.category,
        args.limit,
        not args.no_probe,
    )

    entries = await build_leaderboard(
        time_period=args.period,
        order_by=args.order,
        category=args.category,
        limit=args.limit,
        probe_activity=not args.no_probe,
    )

    if not entries:
        logger.error("Лидерборд пустой. Возможно, Data API недоступен или параметры неверны.")
        return

    render_leaderboard_table(
        entries,
        time_period=args.period,
        order_by=args.order,
    )

    prompt_and_save_selection(entries, app_config.config_path)
