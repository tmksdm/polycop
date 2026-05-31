from __future__ import annotations

import logging


def setup_logger() -> logging.Logger:
    """
    Настраивает базовый logger проекта.

    В watcher-режиме (Rich UI) этот логгер потом перенастраивается
    через configure_logger_for_ui, чтобы писать в правую панель UI,
    а не напрямую в терминал.

    Логгер всегда берётся по имени "polycop": getLogger с одинаковым
    именем из любого файла возвращает ОДИН И ТОТ ЖЕ объект. Поэтому
    не важно, из какого модуля мы его настроили — все остальные модули
    увидят те же настройки.
    """
    logger = logging.getLogger("polycop")
    logger.setLevel(logging.INFO)

    # Если обработчик уже прицеплен (например, при повторном импорте) —
    # не добавляем второй, иначе сообщения будут дублироваться.
    if logger.handlers:
        return logger

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    return logger


def configure_plain_logger() -> None:
    """
    Перенастраивает logger под разовый режим без Rich Live
    (лидерборд, clob-check).

    Почему это нужно:
    при импорте уже мог сработать setup_logger() и прицепить обработчик.
    Если просто добавить ещё один, каждое сообщение печатается дважды.
    Поэтому сначала убираем все старые "трубы", ставим одну свою,
    и отключаем propagate, чтобы сообщение не уходило ещё и к корневому
    логгеру (иначе снова дубли).

    Раньше это была приватная _configure_plain_logger в main.py.
    Сделали публичной (без подчёркивания), потому что её теперь
    вызывают из других модулей.
    """
    logger = logging.getLogger("polycop")

    logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def mask_secret_url(url: str) -> str:
    """
    Маскирует секретный Alchemy URL перед выводом.

    Мы никогда не печатаем API key целиком.
    """
    if "/" not in url:
        return "***"

    prefix, secret = url.rsplit("/", 1)

    if len(secret) <= 8:
        masked_secret = "***"
    else:
        masked_secret = f"{secret[:3]}...{secret[-3:]}"

    return f"{prefix}/{masked_secret}"
