from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app_config import AppConfig, load_app_config
from database import Database, initialize_database
from dry_run_engine import (
    HourlySpendTracker,
    evaluate_dry_run_copy,
    format_dry_run_decision,
)
from gamma_client import get_market_metadata
from polymarket_constants import POLYMARKET_EXCHANGE_ADDRESSES
from terminal_ui import TerminalUI, configure_logger_for_ui
from trade_decoder import (
    decode_polymarket_trades,
    format_trade_for_console,
    trade_matches_watched_traders,
)
from trade_models import DecodedTrade, MarketMetadata


PROJECT_ROOT = Path(__file__).resolve().parent


def setup_logger() -> logging.Logger:
    """
    Настраивает базовый logger.

    На Этапе 3.3 мы перенастраиваем его так,
    чтобы логи уходили не напрямую в терминал, а в Rich UI.
    """
    logger = logging.getLogger("polycop")
    logger.setLevel(logging.INFO)

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


logger = setup_logger()


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


def build_mined_transactions_subscription() -> dict[str, Any]:
    """
    Собирает JSON-RPC запрос подписки для Alchemy.

    Слушаем все транзакции в Polymarket Exchange,
    а нужных трейдеров ищем уже внутри decoded maker/signer.
    """
    address_filters: list[dict[str, str]] = []

    for exchange_address in POLYMARKET_EXCHANGE_ADDRESSES:
        address_filters.append(
            {
                "to": exchange_address,
            }
        )

    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_subscribe",
        "params": [
            "alchemy_minedTransactions",
            {
                "addresses": address_filters,
                "includeRemoved": False,
                "hashesOnly": False,
            },
        ],
    }


def short_hash(value: str | None) -> str:
    """
    Сокращает длинный hash для вывода в консоль.
    """
    if not value:
        return "unknown"

    if len(value) <= 14:
        return value

    return f"{value[:8]}...{value[-6:]}"


def format_raw_transaction(transaction: dict[str, Any]) -> str:
    """
    Делает короткую человекочитаемую строку из сырой транзакции.

    Сейчас эта функция остаётся полезной для отладки,
    но в спокойном UI-режиме мы не печатаем каждую raw tx в лог.
    """
    tx_hash = transaction.get("hash")
    from_address = transaction.get("from")
    to_address = transaction.get("to")
    block_number = transaction.get("blockNumber")
    input_data = transaction.get("input", "")

    input_size = 0
    if isinstance(input_data, str) and input_data.startswith("0x"):
        # В hex-строке 2 символа = 1 байт.
        # Минус 2, потому что "0x" — это префикс.
        input_size = max((len(input_data) - 2) // 2, 0)

    return (
        f"tx={short_hash(tx_hash)} | "
        f"block={block_number} | "
        f"from={from_address} | "
        f"to={to_address} | "
        f"input_bytes={input_size}"
    )


async def handle_polymarket_transaction(
    transaction: dict[str, Any],
    app_config: AppConfig,
    hourly_tracker: HourlySpendTracker,
    database: Database,
    terminal_ui: TerminalUI,
) -> None:
    """
    Обрабатывает одну транзакцию Polymarket Exchange.

    В спокойном UI-режиме:
    - raw tx считаем в статистике, но не спамим в лог;
    - последние сделки показываем в центральной таблице;
    - DRY-RUN решения показываем в центральной таблице;
    - в правый лог пишем только важное: ошибки, предупреждения, WOULD_COPY.

    Важная защита:
    - если watched_traders пустой, это broad/debug scan;
    - в таком режиме SQLite НЕ сохраняет сделки, чтобы база не раздувалась;
    - если watched_traders заполнен, сохраняем историю как раньше.
    """
    terminal_ui.record_raw_transaction()

    try:
        trades = decode_polymarket_trades(transaction)
    except Exception:
        logger.exception("Ошибка при декодировании транзакции")
        return

    # Большинство транзакций могут быть не тем matchOrders, который нам нужен.
    # Это нормальная ситуация, поэтому не спамим логом.
    if not trades:
        return

    visible_trades = [
        trade
        for trade in trades
        if trade_matches_watched_traders(trade, app_config.watched_traders)
    ]

    # Если фильтр трейдеров включён и в этой транзакции их нет —
    # просто пропускаем без шума.
    if app_config.watched_traders and not visible_trades:
        return

    market_metadata_by_trade_key = await _load_market_metadata_for_trades(visible_trades)

    terminal_ui.record_decoded_trades_count(len(visible_trades))

    # Историю сохраняем только в рабочем режиме,
    # когда выбран хотя бы один конкретный трейдер.
    should_save_sqlite_history = bool(app_config.watched_traders)

    for trade_index, trade in enumerate(visible_trades):
        trade_key = _market_metadata_key(trade)
        metadata = market_metadata_by_trade_key.get(trade_key)

        terminal_ui.record_trade(trade=trade, metadata=metadata)

        if app_config.dry_run:
            decision = evaluate_dry_run_copy(
                trade=trade,
                metadata=metadata,
                config=app_config,
                hourly_tracker=hourly_tracker,
            )

            terminal_ui.record_decision(decision)

            # В лог пишем только WOULD_COPY, потому что это важный сигнал.
            # SKIP виден в таблице решений, но не должен шуметь справа.
            if decision.accepted:
                logger.info("DRY-RUN | %s", format_dry_run_decision(decision))

            if should_save_sqlite_history:
                try:
                    database.save_trade_and_dry_run_decision(
                        trade=trade,
                        trade_index=trade_index,
                        metadata=metadata,
                        decision=decision,
                        config=app_config,
                    )

                    terminal_ui.record_sqlite_save()

                except Exception:
                    # Ошибка базы не должна валить watcher.
                    # Но её обязательно логируем, чтобы не потерять проблему.
                    logger.exception("Не удалось сохранить сделку в SQLite")
        else:
            # LIVE-режим появится на Этапе 5.
            # До этого момента мы специально не отправляем реальные ордера.
            logger.warning("LIVE режим ещё не реализован. Сделка не отправлена.")


async def _load_market_metadata_for_trades(
    trades: list[DecodedTrade],
) -> dict[str, MarketMetadata]:
    """
    Загружает метаданные рынков для списка сделок.

    Важно:
    - вопрос рынка общий для condition_id;
    - outcome зависит от token_id.

    Поэтому результат храним по ключу:
    condition_id.lower() + ":" + token_id
    """
    result: dict[str, MarketMetadata] = {}

    first_trade_by_key: dict[str, DecodedTrade] = {}

    for trade in trades:
        trade_key = _market_metadata_key(trade)

        if trade_key not in first_trade_by_key:
            first_trade_by_key[trade_key] = trade

    for trade_key, trade in first_trade_by_key.items():
        try:
            metadata = await get_market_metadata(
                condition_id=trade.condition_id,
                token_id=trade.token_id,
            )
        except Exception:
            logger.exception(
                "Неожиданная ошибка при запросе Gamma API для condition_id=%s token_id=%s",
                _short_text(trade.condition_id, max_length=18),
                _short_text(str(trade.token_id), max_length=18),
            )
            continue

        # Если Gamma API не нашёл рынок — это не критическая ошибка.
        # Сделку всё равно показываем как market=unknown/outcome=unknown.
        if metadata is None:
            continue

        result[trade_key] = metadata

    return result


def format_enriched_trade_for_console(
    trade: DecodedTrade,
    metadata: MarketMetadata | None,
) -> str:
    """
    Форматирует сделку вместе с информацией о рынке.

    Сейчас используется в основном для отладки.
    UI показывает сделку таблично.
    """
    base_line = format_trade_for_console(trade)

    if metadata is None:
        return f"{base_line} | market=unknown | outcome=unknown"

    question = _short_text(metadata.question, max_length=90)
    outcome = metadata.outcome or "unknown"

    if metadata.slug:
        return f'{base_line} | market="{question}" | outcome={outcome} | slug={metadata.slug}'

    return f'{base_line} | market="{question}" | outcome={outcome}'


def _market_metadata_key(trade: DecodedTrade) -> str:
    """
    Делает ключ для metadata конкретного outcome.

    condition_id один на рынок,
    token_id указывает на конкретный outcome внутри рынка.
    """
    return f"{trade.condition_id.lower()}:{trade.token_id}"


def _short_text(value: str, max_length: int) -> str:
    """
    Обрезает длинный текст для консоли.
    """
    if len(value) <= max_length:
        return value

    return f"{value[: max_length - 3]}..."


async def watch_mined_transactions(
    alchemy_wss: str,
    app_config: AppConfig,
    hourly_tracker: HourlySpendTracker,
    database: Database,
    terminal_ui: TerminalUI,
) -> None:
    """
    Подключается к Alchemy WebSocket и слушает mined-транзакции.

    WebSocket — это постоянное соединение.
    В отличие от обычного HTTP-запроса, оно остаётся открытым,
    и Alchemy сам присылает нам новые события.
    """
    subscription_request = build_mined_transactions_subscription()

    terminal_ui.set_websocket_status("connecting", "yellow")

    logger.info("Подключаюсь к Alchemy WebSocket...")
    logger.info("Alchemy WSS: %s", mask_secret_url(alchemy_wss))

    async with websockets.connect(alchemy_wss, ping_interval=20, ping_timeout=20) as websocket:
        terminal_ui.set_websocket_status("connected", "green")
        logger.info("WebSocket подключен")

        await websocket.send(json.dumps(subscription_request))
        logger.info("Запрос подписки отправлен")

        first_response_raw = await websocket.recv()
        first_response = json.loads(first_response_raw)

        if "error" in first_response:
            terminal_ui.set_websocket_status("subscription error", "red")
            raise RuntimeError(f"Alchemy вернул ошибку подписки: {first_response['error']}")

        subscription_id = first_response.get("result")

        terminal_ui.set_websocket_status("subscribed", "green")
        logger.info("Подписка активна, subscription_id=%s", subscription_id)

        if app_config.watched_traders:
            logger.info(
                "Фильтр трейдеров включён: ищем адреса внутри decoded maker/signer: %s",
                ", ".join(app_config.watched_traders),
            )
        else:
            logger.warning(
                "Broad scan mode: watched traders пустой. "
                "UI показывает все decoded сделки, но SQLite history отключена, "
                "чтобы не раздувать базу."
            )

        logger.info("Фильтр контрактов: %s", ", ".join(POLYMARKET_EXCHANGE_ADDRESSES))
        logger.info("Жду транзакции... Для остановки нажми Ctrl+C")

        while True:
            message_raw = await websocket.recv()
            message = json.loads(message_raw)

            # Alchemy присылает события в формате eth_subscription.
            if message.get("method") != "eth_subscription":
                logger.debug("Получено служебное сообщение: %s", message)
                continue

            params = message.get("params", {})
            result = params.get("result", {})

            removed = result.get("removed", False)
            transaction = result.get("transaction", {})

            # removed=True бывает при редкой ситуации re-org,
            # когда блок был временно принят, а потом исключён из основной цепочки.
            if removed:
                logger.warning("Транзакция была removed/re-org: %s", transaction)
                continue

            if not isinstance(transaction, dict):
                logger.warning("Неожиданный формат transaction: %s", transaction)
                continue

            await handle_polymarket_transaction(
                transaction=transaction,
                app_config=app_config,
                hourly_tracker=hourly_tracker,
                database=database,
                terminal_ui=terminal_ui,
            )


async def run_watcher_forever(
    alchemy_wss: str,
    app_config: AppConfig,
    hourly_tracker: HourlySpendTracker,
    database: Database,
    terminal_ui: TerminalUI,
) -> None:
    """
    Запускает watcher с простым reconnect.

    Если WebSocket оборвался — ждём несколько секунд и подключаемся снова.
    Более умный exponential backoff сделаем позже на этапе устойчивости.
    """
    reconnect_delay_seconds = 5

    while True:
        try:
            await watch_mined_transactions(
                alchemy_wss=alchemy_wss,
                app_config=app_config,
                hourly_tracker=hourly_tracker,
                database=database,
                terminal_ui=terminal_ui,
            )
        except ConnectionClosed as error:
            terminal_ui.mark_reconnect()
            terminal_ui.set_websocket_status(
                f"reconnect in {reconnect_delay_seconds}s",
                "yellow",
            )
            logger.warning(
                "WebSocket соединение закрыто: %s. Переподключение через %s секунд...",
                error,
                reconnect_delay_seconds,
            )
        except OSError as error:
            terminal_ui.mark_reconnect()
            terminal_ui.set_websocket_status(
                f"reconnect in {reconnect_delay_seconds}s",
                "yellow",
            )
            logger.warning(
                "Сетевая ошибка: %s. Переподключение через %s секунд...",
                error,
                reconnect_delay_seconds,
            )
        except RuntimeError as error:
            terminal_ui.set_websocket_status("stopped", "red")
            logger.error("Ошибка выполнения: %s", error)
            logger.error("Останавливаю watcher, потому что это не похоже на временный сетевой сбой")
            return
        except json.JSONDecodeError as error:
            terminal_ui.mark_reconnect()
            terminal_ui.set_websocket_status(
                f"reconnect in {reconnect_delay_seconds}s",
                "yellow",
            )
            logger.warning(
                "Не смог разобрать JSON от Alchemy: %s. Переподключение через %s секунд...",
                error,
                reconnect_delay_seconds,
            )
        except Exception:
            terminal_ui.mark_reconnect()
            terminal_ui.set_websocket_status(
                f"reconnect in {reconnect_delay_seconds}s",
                "yellow",
            )
            logger.exception(
                "Неожиданная ошибка. Переподключение через %s секунд...",
                reconnect_delay_seconds,
            )

        await asyncio.sleep(reconnect_delay_seconds)


async def main() -> None:
    """
    Главная асинхронная функция приложения.

    На текущем шаге Этапа 3 она:
    - читает .env;
    - читает config.json;
    - инициализирует SQLite;
    - запускает Rich terminal UI;
    - подключается к Alchemy;
    - декодирует сделки;
    - применяет фильтры;
    - показывает DRY-RUN решение;
    - сохраняет историю в data/polycop.db только если выбран watched trader.
    """
    app_config = load_app_config(PROJECT_ROOT)
    hourly_tracker = HourlySpendTracker()
    database = initialize_database(PROJECT_ROOT)

    terminal_ui = TerminalUI(
        app_config=app_config,
        database_path=database.path,
    )

    # После этого logger пишет не обычными строками в терминал,
    # а в правую панель Logs внутри Rich UI.
    configure_logger_for_ui(logger, terminal_ui)

    stop_ui_event = asyncio.Event()
    ui_task = asyncio.create_task(terminal_ui.run(stop_ui_event))

    try:
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info("Polycop started")
        logger.info("Start time: %s", started_at)
        logger.info("Mode: %s", "DRY-RUN" if app_config.dry_run else "LIVE")
        logger.info("Config path: %s", app_config.config_path)
        logger.info("SQLite DB: %s", database.path)

        for warning in app_config.warnings:
            logger.warning("Config warning: %s", warning)

        logger.info(
            "Risk config: ratio=%s%% | min_bet=$%s | hourly_limit=%s%% of $%s | price=%s–%s¢",
            app_config.risk.ratio_percent,
            app_config.risk.min_bet_usdc,
            app_config.risk.hourly_limit_percent,
            app_config.risk.dry_run_balance_usdc,
            app_config.risk.min_price_cents,
            app_config.risk.max_price_cents,
        )

        logger.info(
            "Sell config: mode=%s | auto_sell_threshold=%s¢ | sell_percentage=%s%%",
            app_config.sell.sell_mode,
            app_config.sell.auto_sell_threshold_cents,
            app_config.sell.sell_percentage,
        )

        if app_config.watched_traders:
            logger.info("SQLite history: enabled for watched traders")
        else:
            logger.warning(
                "SQLite history: disabled because watched traders list is empty"
            )

        if not app_config.dry_run:
            logger.warning("LIVE режим пока не реализован. Деньги не используются.")

        if not app_config.alchemy_wss:
            terminal_ui.set_websocket_status("missing WSS", "red")
            logger.error("Alchemy WSS не настроен")
            logger.error("Добавь ALCHEMY_POLYGON_WSS в локальный .env")
            return

        if not app_config.alchemy_wss.startswith("wss://"):
            terminal_ui.set_websocket_status("bad WSS", "red")
            logger.error("Некорректный Alchemy WSS")
            logger.error("Ожидаю URL, который начинается с wss://")
            return

        await run_watcher_forever(
            alchemy_wss=app_config.alchemy_wss,
            app_config=app_config,
            hourly_tracker=hourly_tracker,
            database=database,
            terminal_ui=terminal_ui,
        )
    finally:
        # Аккуратно останавливаем UI.
        stop_ui_event.set()
        await ui_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
