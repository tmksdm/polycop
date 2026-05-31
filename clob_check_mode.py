from __future__ import annotations

import argparse
import getpass
import logging
from pathlib import Path

from eth_account import Account

from app_config import load_app_config, load_env_file
from clob_client import ClobClientError, ClobReadOnlyClient, DEFAULT_CLOB_HOST
from key_vault import KeyVaultError, load_encrypted_key
from logging_setup import configure_plain_logger


PROJECT_ROOT = Path(__file__).resolve().parent

logger = logging.getLogger("polycop")


def _obtain_private_key_for_check(args: argparse.Namespace) -> str:
    """
    Возвращает приватный ключ для read-only проверки CLOB.

    Две ветки:

    1) Одноразовый ключ (--use-temp-key, дефолт на Этапе 5.2):
       генерируем новый случайный кошелёк прямо в памяти.
       На нём нет и не будет денег. Никуда не сохраняем.
       Идеально для проверки, что наш код умеет говорить с CLOB,
       не трогая боевой кошелёк.

    2) Боевой ключ из зашифрованного файла (--use-wallet-file):
       спрашиваем пароль через getpass (скрытый ввод) и расшифровываем
       WALLET_KEY_FILE. Эта ветка пригодится на Этапе 5.3+,
       когда у тебя появится реальный кошелёк.

    Сам ключ нигде не печатается и не логируется.
    """
    # Боевая ветка: читаем зашифрованный ключ из файла.
    if args.use_wallet_file:
        env_values = _read_env_values()

        raw_key_file = env_values.get("WALLET_KEY_FILE", "secrets/wallet.key.enc").strip()
        key_file_path = Path(raw_key_file)
        if not key_file_path.is_absolute():
            key_file_path = PROJECT_ROOT / key_file_path

        if not key_file_path.exists():
            raise ClobClientError(
                f"Зашифрованный файл ключа не найден: {key_file_path}. "
                "Сначала создай его: python manage_key.py encrypt"
            )

        # getpass прячет ввод пароля — он не виден на экране и не попадает в историю.
        password = getpass.getpass("Пароль для расшифровки кошелька: ")

        try:
            private_key = load_encrypted_key(key_file_path, password)
        except KeyVaultError as error:
            raise ClobClientError(f"Не удалось расшифровать ключ: {error}")

        logger.info("Ключ загружен из зашифрованного файла.")
        return private_key

    # Дефолтная ветка Этапа 5.2: одноразовый сгенерированный ключ.
    temp_account = Account.create()
    logger.info("Сгенерирован одноразовый тестовый кошелёк (без денег, нигде не сохраняется).")
    # .key — это bytes приватного ключа; .hex() даёт строку 0x... для SDK.
    return temp_account.key.hex()


def _read_env_values() -> dict[str, str]:
    """
    Читает .env через тот же парсер, что и app_config.

    Нужен, чтобы достать CLOB_HOST / POLYMARKET_FUNDER / WALLET_KEY_FILE
    без дублирования логики разбора .env.
    """
    return load_env_file(PROJECT_ROOT / ".env")


def run_clob_check_mode(args: argparse.Namespace) -> None:
    """
    Этап 5.2 — read-only проверка связи с Polymarket CLOB.

    Что делает:
    - получает приватный ключ (одноразовый или из файла);
    - поднимает ClobReadOnlyClient (L1-клиент);
    - печатает адрес кошелька (локально, без сети);
    - запрашивает у CLOB L2 API-creds (единственный сетевой вызов);
    - печатает безопасный отпечаток creds.

    Ни одного ордера здесь не отправляется — обёртка этого попросту не умеет.
    Это обычный синхронный режим (не asyncio): SDK сам ходит в сеть,
    а нам тут параллелить нечего.
    """
    _configure_plain_logger_local()

    logger.info("Режим проверки CLOB (read-only)")

    env_values = _read_env_values()
    host = env_values.get("CLOB_HOST", "").strip() or DEFAULT_CLOB_HOST
    funder = env_values.get("POLYMARKET_FUNDER", "").strip() or None

    logger.info("CLOB host: %s", host)
    if funder:
        logger.info("Funder (smart wallet): %s", funder)
    else:
        logger.info("Funder не задан — работаем по EOA (это нормально для 5.2).")

    # Получаем ключ (внутри сам решит: одноразовый или из файла).
    try:
        private_key = _obtain_private_key_for_check(args)
    except ClobClientError as error:
        logger.error("%s", error)
        return

    # Поднимаем read-only клиент.
    try:
        client = ClobReadOnlyClient(
            private_key=private_key,
            host=host,
            funder=funder,
        )
    except ClobClientError as error:
        logger.error("Не удалось инициализировать CLOB-клиент: %s", error)
        return

    logger.info("Адрес кошелька (EOA): %s", client.address)
    logger.info("Запрашиваю L2 API-ключи у CLOB...")
    logger.info("Если зависнет на этом шаге — почти наверняка выключен VPN.")

    # Единственный сетевой вызов.
    try:
        fingerprint = client.derive_api_creds()
    except ClobClientError as error:
        logger.error("%s", error)
        return

    logger.info("API-ключи успешно получены.")
    logger.info("  api_key:    %s", fingerprint.api_key)
    logger.info("  secret:     %s", fingerprint.secret_preview)
    logger.info("  passphrase: %s", fingerprint.passphrase_preview)
    logger.info("Проверка CLOB пройдена. Ни одной транзакции не отправлено.")


def _configure_plain_logger_local() -> None:
    """
    Тонкая обёртка над logging_setup.configure_plain_logger.

    Оставлена отдельной функцией только для читабельности этого модуля.
    """
    configure_plain_logger()
