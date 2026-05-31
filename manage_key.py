from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from app_config import load_env_file
from key_vault import (
    KeyVaultError,
    encrypted_key_exists,
    load_encrypted_key,
    save_encrypted_key,
)


PROJECT_ROOT = Path(__file__).resolve().parent

# Путь к зашифрованному файлу по умолчанию.
# Можно переопределить в .env через WALLET_KEY_FILE.
DEFAULT_KEY_FILE = "secrets/wallet.key.enc"


def resolve_key_file_path() -> Path:
    """
    Определяет путь к зашифрованному файлу ключа.

    Берёт WALLET_KEY_FILE из .env, если он там есть,
    иначе использует secrets/wallet.key.enc.
    """
    env_values = load_env_file(PROJECT_ROOT / ".env")
    raw_path = env_values.get("WALLET_KEY_FILE", DEFAULT_KEY_FILE).strip() or DEFAULT_KEY_FILE

    key_path = Path(raw_path)
    if not key_path.is_absolute():
        key_path = PROJECT_ROOT / key_path

    return key_path


def looks_like_private_key(value: str) -> bool:
    """
    Очень лёгкая проверка формата приватного ключа EVM.

    Приватный ключ — это 64 hex-символа, иногда с префиксом 0x.
    Мы НЕ логируем сам ключ, проверяем только форму.
    """
    clean = value.strip()
    if clean.startswith("0x") or clean.startswith("0X"):
        clean = clean[2:]

    if len(clean) != 64:
        return False

    try:
        int(clean, 16)
    except ValueError:
        return False

    return True


def command_encrypt() -> int:
    """
    Шифрует приватный ключ и сохраняет в файл.

    Пользователь вводит ключ и пароль скрытно (символы не видны).
    """
    key_path = resolve_key_file_path()

    # Если файл уже есть — не перезаписываем молча, спрашиваем подтверждение.
    if encrypted_key_exists(key_path):
        print(f"Внимание: файл с ключом уже существует: {key_path}")
        answer = input("Перезаписать его? Введите 'yes' для подтверждения: ").strip().lower()
        if answer != "yes":
            print("Отменено. Файл не изменён.")
            return 1

    print("Вставьте приватный ключ кошелька. Символы НЕ будут отображаться на экране.")
    print("(Это нормально — так ключ не попадёт в историю терминала.)")
    private_key = getpass.getpass("Приватный ключ: ")

    if not looks_like_private_key(private_key):
        print(
            "Это не похоже на приватный ключ EVM "
            "(ожидается 64 hex-символа, опционально с префиксом 0x)."
        )
        print("Ничего не сохранено.")
        return 1

    # Пароль вводим дважды, чтобы не зашифровать опечаткой.
    password = getpass.getpass("Придумайте пароль для шифрования: ")
    password_repeat = getpass.getpass("Повторите пароль: ")

    if password != password_repeat:
        print("Пароли не совпадают. Ничего не сохранено.")
        return 1

    if len(password) < 8:
        print("Пароль слишком короткий. Используйте минимум 8 символов.")
        print("Ничего не сохранено.")
        return 1

    try:
        save_encrypted_key(key_path, private_key, password)
    except KeyVaultError as error:
        print(f"Ошибка шифрования: {error}")
        return 1

    print()
    print(f"Готово. Зашифрованный ключ сохранён в: {key_path}")
    print("Этот файл НЕ попадёт в Git (папка secrets/ в .gitignore).")
    print("Запомните пароль — без него ключ не восстановить.")
    return 0


def command_verify() -> int:
    """
    Проверяет, что зашифрованный файл расшифровывается по паролю.

    Сам ключ на экран НЕ выводим — показываем только подтверждение
    и безопасный "отпечаток" (первые и последние символы), чтобы
    ты мог сверить, что это нужный кошелёк.
    """
    key_path = resolve_key_file_path()

    if not encrypted_key_exists(key_path):
        print(f"Файл с ключом не найден: {key_path}")
        print("Сначала зашифруйте ключ командой: python manage_key.py encrypt")
        return 1

    password = getpass.getpass("Введите пароль для проверки: ")

    try:
        private_key = load_encrypted_key(key_path, password)
    except KeyVaultError as error:
        print(f"Проверка не пройдена: {error}")
        return 1

    # Безопасный отпечаток: показываем только края, никогда не весь ключ.
    clean = private_key.strip()
    fingerprint = f"{clean[:4]}...{clean[-4:]}" if len(clean) >= 8 else "***"

    print("Расшифровка успешна. Пароль верный.")
    print(f"Отпечаток ключа (для сверки кошелька): {fingerprint}")
    return 0


def main() -> int:
    """
    Точка входа утилиты.

    Поддерживает две команды:
    - encrypt: зашифровать и сохранить ключ;
    - verify:  проверить расшифровку по паролю.
    """
    parser = argparse.ArgumentParser(
        description="Утилита шифрования приватного ключа кошелька (AES-256-GCM)."
    )
    parser.add_argument(
        "command",
        choices=["encrypt", "verify"],
        help="encrypt — зашифровать ключ; verify — проверить расшифровку.",
    )
    args = parser.parse_args()

    if args.command == "encrypt":
        return command_encrypt()

    if args.command == "verify":
        return command_verify()

    return 1


if __name__ == "__main__":
    # sys.exit с кодом возврата — чтобы при ошибке скрипт завершался ненулевым кодом.
    sys.exit(main())
