from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app_config import AppConfig
from dry_run_engine import DryRunDecision
from trade_models import DecodedTrade, MarketMetadata


class Database:
    """
    Простой слой работы с SQLite.

    SQLite — это локальная база данных в одном файле.
    Нам не нужен отдельный сервер базы данных: файл data/polycop.db
    будет лежать рядом с проектом и игнорироваться Git.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """
        Создаёт папку data/ и таблицы, если их ещё нет.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as connection:
            # WAL — более устойчивый режим записи SQLite.
            # Он может создать служебные файлы .db-wal/.db-shm,
            # поэтому они тоже должны быть в .gitignore.
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute("PRAGMA foreign_keys=ON;")

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    unique_key TEXT NOT NULL UNIQUE,
                    trade_index INTEGER NOT NULL DEFAULT 0,

                    tx_hash TEXT NOT NULL,
                    block_number TEXT,
                    exchange_address TEXT NOT NULL,

                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,

                    trader TEXT NOT NULL,
                    signer TEXT NOT NULL,
                    role TEXT NOT NULL,
                    side TEXT NOT NULL,

                    price TEXT NOT NULL,
                    size_usdc TEXT NOT NULL,
                    shares TEXT NOT NULL,

                    observed_at TEXT NOT NULL,

                    market_id TEXT,
                    market_question TEXT,
                    market_slug TEXT,
                    outcome TEXT,

                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trades_tx_hash
                    ON trades(tx_hash);

                CREATE INDEX IF NOT EXISTS idx_trades_trader
                    ON trades(trader);

                CREATE INDEX IF NOT EXISTS idx_trades_signer
                    ON trades(signer);

                CREATE INDEX IF NOT EXISTS idx_trades_observed_at
                    ON trades(observed_at);

                CREATE TABLE IF NOT EXISTS dry_run_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    trade_id INTEGER NOT NULL UNIQUE,

                    tx_hash TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,

                    copy_size_usdc TEXT NOT NULL,
                    copy_shares TEXT NOT NULL,

                    hourly_limit_usdc TEXT NOT NULL,
                    hourly_spent_before TEXT NOT NULL,
                    hourly_spent_after TEXT NOT NULL,

                    config_ratio_percent TEXT NOT NULL,
                    config_min_bet_usdc TEXT NOT NULL,
                    config_hourly_limit_percent TEXT NOT NULL,
                    config_dry_run_balance_usdc TEXT NOT NULL,
                    config_min_price_cents TEXT NOT NULL,
                    config_max_price_cents TEXT NOT NULL,
                    config_sell_mode TEXT NOT NULL,
                    config_auto_sell_threshold_cents TEXT NOT NULL,
                    config_sell_percentage TEXT NOT NULL,

                    created_at TEXT NOT NULL,

                    FOREIGN KEY (trade_id) REFERENCES trades(id)
                );

                CREATE INDEX IF NOT EXISTS idx_dry_run_decisions_tx_hash
                    ON dry_run_decisions(tx_hash);

                CREATE INDEX IF NOT EXISTS idx_dry_run_decisions_status
                    ON dry_run_decisions(status);

                CREATE INDEX IF NOT EXISTS idx_dry_run_decisions_created_at
                    ON dry_run_decisions(created_at);
                """
            )

            self._apply_schema_migrations(connection)
            connection.commit()

    def save_trade_and_dry_run_decision(
        self,
        *,
        trade: DecodedTrade,
        trade_index: int,
        metadata: MarketMetadata | None,
        decision: DryRunDecision,
        config: AppConfig,
    ) -> None:
        """
        Сохраняет одну сделку и связанное с ней DRY-RUN решение.

        trade_index — порядковый номер сделки внутри decoded-списка.
        Он нужен, потому что внутри одной транзакции могут быть несколько
        внешне одинаковых maker-сделок.
        """
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON;")

            trade_id = self._insert_or_get_trade(
                connection=connection,
                trade=trade,
                trade_index=trade_index,
                metadata=metadata,
            )

            self._insert_dry_run_decision_if_missing(
                connection=connection,
                trade_id=trade_id,
                trade=trade,
                decision=decision,
                config=config,
            )

            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        """
        Открывает соединение с SQLite.

        check_same_thread=False здесь не нужен, потому что пока мы работаем
        в одном основном asyncio-потоке и делаем короткие записи.
        """
        return sqlite3.connect(self.path)

    def _apply_schema_migrations(self, connection: sqlite3.Connection) -> None:
        """
        Небольшие миграции схемы.

        Миграция — это аккуратное обновление структуры базы, если файл базы
        уже был создан старой версией кода.
        """
        trade_columns = self._get_table_columns(connection, "trades")

        if "trade_index" not in trade_columns:
            connection.execute(
                """
                ALTER TABLE trades
                ADD COLUMN trade_index INTEGER NOT NULL DEFAULT 0;
                """
            )

    @staticmethod
    def _get_table_columns(
        connection: sqlite3.Connection,
        table_name: str,
    ) -> set[str]:
        """
        Возвращает имена колонок таблицы.
        """
        cursor = connection.execute(f"PRAGMA table_info({table_name});")
        rows = cursor.fetchall()

        return {str(row[1]) for row in rows}

    def _insert_or_get_trade(
        self,
        *,
        connection: sqlite3.Connection,
        trade: DecodedTrade,
        trade_index: int,
        metadata: MarketMetadata | None,
    ) -> int:
        """
        Добавляет сделку в таблицу trades или возвращает id уже существующей.
        """
        unique_key = self._build_trade_unique_key(
            trade=trade,
            trade_index=trade_index,
        )
        created_at = self._utc_now_iso()

        connection.execute(
            """
            INSERT OR IGNORE INTO trades (
                unique_key,
                trade_index,

                tx_hash,
                block_number,
                exchange_address,

                condition_id,
                token_id,

                trader,
                signer,
                role,
                side,

                price,
                size_usdc,
                shares,

                observed_at,

                market_id,
                market_question,
                market_slug,
                outcome,

                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                unique_key,
                trade_index,

                trade.tx_hash,
                str(trade.block_number) if trade.block_number is not None else None,
                trade.exchange_address,

                trade.condition_id,
                str(trade.token_id),

                trade.trader,
                trade.signer,
                trade.role,
                trade.side,

                str(trade.price),
                str(trade.size_usdc),
                str(trade.shares),

                trade.observed_at,

                metadata.market_id if metadata is not None else None,
                metadata.question if metadata is not None else None,
                metadata.slug if metadata is not None else None,
                metadata.outcome if metadata is not None else None,

                created_at,
            ),
        )

        cursor = connection.execute(
            "SELECT id FROM trades WHERE unique_key = ?;",
            (unique_key,),
        )
        row = cursor.fetchone()

        if row is None:
            raise RuntimeError("Не удалось сохранить или найти сделку в SQLite")

        return int(row[0])

    def _insert_dry_run_decision_if_missing(
        self,
        *,
        connection: sqlite3.Connection,
        trade_id: int,
        trade: DecodedTrade,
        decision: DryRunDecision,
        config: AppConfig,
    ) -> None:
        """
        Добавляет DRY-RUN решение.

        На одну сделку — одно DRY-RUN решение.
        Если оно уже есть, повторно не вставляем.
        """
        status = "WOULD_COPY" if decision.accepted else "SKIP"
        created_at = self._utc_now_iso()

        connection.execute(
            """
            INSERT OR IGNORE INTO dry_run_decisions (
                trade_id,

                tx_hash,
                accepted,
                status,
                reason,

                copy_size_usdc,
                copy_shares,

                hourly_limit_usdc,
                hourly_spent_before,
                hourly_spent_after,

                config_ratio_percent,
                config_min_bet_usdc,
                config_hourly_limit_percent,
                config_dry_run_balance_usdc,
                config_min_price_cents,
                config_max_price_cents,
                config_sell_mode,
                config_auto_sell_threshold_cents,
                config_sell_percentage,

                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                trade_id,

                trade.tx_hash,
                1 if decision.accepted else 0,
                status,
                decision.reason,

                str(decision.copy_size_usdc),
                str(decision.copy_shares),

                str(decision.hourly_limit_usdc),
                str(decision.hourly_spent_before),
                str(decision.hourly_spent_after),

                str(config.risk.ratio_percent),
                str(config.risk.min_bet_usdc),
                str(config.risk.hourly_limit_percent),
                str(config.risk.dry_run_balance_usdc),
                str(config.risk.min_price_cents),
                str(config.risk.max_price_cents),
                config.sell.sell_mode,
                str(config.sell.auto_sell_threshold_cents),
                str(config.sell.sell_percentage),

                created_at,
            ),
        )

    @staticmethod
    def _build_trade_unique_key(
        *,
        trade: DecodedTrade,
        trade_index: int,
    ) -> str:
        """
        Делает стабильный ключ сделки.

        В одной транзакции может быть несколько maker-сделок.
        Поэтому одного tx_hash недостаточно, а иногда совпадают даже
        trader/price/size/shares. Для этого добавляем trade_index.
        """
        return "|".join(
            [
                trade.tx_hash.lower(),
                str(trade_index),
                trade.exchange_address.lower(),
                trade.condition_id.lower(),
                str(trade.token_id),
                trade.trader.lower(),
                trade.signer.lower(),
                trade.role.lower(),
                trade.side.upper(),
                str(trade.price),
                str(trade.size_usdc),
                str(trade.shares),
            ]
        )

    @staticmethod
    def _utc_now_iso() -> str:
        """
        Текущее время в UTC в ISO-формате.

        UTC используем, чтобы потом одинаково работало на Windows и VPS.
        """
        return datetime.now(timezone.utc).isoformat()


def initialize_database(project_root: Path) -> Database:
    """
    Создаёт и инициализирует базу проекта.
    """
    database_path = project_root / "data" / "polycop.db"
    database = Database(database_path)
    database.initialize()
    return database
