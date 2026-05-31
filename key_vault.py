from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


# Длины служебных полей в байтах.
# Эти значения "зашиты" в формат файла: при расшифровке мы откусываем
# ровно столько байт обратно, поэтому менять их задним числом нельзя —
# иначе старые зашифрованные файлы перестанут читаться.
SALT_LENGTH = 16   # соль для PBKDF2 (16 байт = 128 бит, более чем достаточно)
NONCE_LENGTH = 12  # nonce для AES-GCM (12 байт — стандартный размер для GCM)

# Сколько итераций делает PBKDF2.
# Чем больше — тем медленнее перебор пароля злоумышленником, но тем
# дольше наш собственный запуск. 600_000 — рекомендация OWASP для PBKDF2-HMAC-SHA256.
PBKDF2_ITERATIONS = 600_000

# Длина ключа шифрования, который мы получаем из пароля.
# 32 байта = 256 бит — это и есть "256" в названии AES-256.
KEY_LENGTH = 32


class KeyVaultError(Exception):
    """
    Общая ошибка хранилища ключа.

    Мы используем своё исключение, чтобы вызывающий код мог
    ловить именно "проблемы с ключом", а не любые ошибки подряд.
    """


def _derive_encryption_key(password: str, salt: bytes) -> bytes:
    """
    Превращает текстовый пароль в 32-байтовый ключ шифрования.

    Используем PBKDF2-HMAC-SHA256 — функцию "растягивания" пароля.
    Она намеренно медленная (PBKDF2_ITERATIONS повторений), чтобы
    перебор паролей был дорогим для злоумышленника.

    salt подмешивается к паролю, поэтому одинаковые пароли
    дают разные ключи в разных файлах.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    # encode("utf-8") — переводим строку пароля в байты,
    # потому что криптография работает с байтами, не со строками.
    return kdf.derive(password.encode("utf-8"))


def encrypt_private_key(private_key: str, password: str) -> bytes:
    """
    Шифрует приватный ключ паролем и возвращает байты для записи в файл.

    Формат результата (всё склеено подряд):
    [ salt: 16 байт ][ nonce: 12 байт ][ зашифрованный ключ + тег целостности ]

    salt и nonce не секретны — они нужны только для расшифровки,
    поэтому спокойно хранятся прямо в файле.
    """
    if not password:
        raise KeyVaultError("Пароль не может быть пустым.")

    # Нормализуем приватный ключ: убираем лишние пробелы по краям.
    clean_private_key = private_key.strip()

    if not clean_private_key:
        raise KeyVaultError("Приватный ключ пустой.")

    # Генерируем свежие случайные salt и nonce для КАЖДОГО шифрования.
    # os.urandom — криптографически стойкий источник случайности ОС.
    salt = os.urandom(SALT_LENGTH)
    nonce = os.urandom(NONCE_LENGTH)

    encryption_key = _derive_encryption_key(password, salt)

    aesgcm = AESGCM(encryption_key)

    # AES-GCM шифрует данные и добавляет "тег целостности".
    # Если потом файл изменят хоть на байт — расшифровка упадёт.
    ciphertext = aesgcm.encrypt(
        nonce,
        clean_private_key.encode("utf-8"),
        None,  # associated_data: дополнительные данные нам не нужны
    )

    # Склеиваем всё в одну строку байтов в строго фиксированном порядке.
    return salt + nonce + ciphertext


def decrypt_private_key(encrypted_blob: bytes, password: str) -> str:
    """
    Расшифровывает приватный ключ из байтов файла по паролю.

    Если пароль неверный ИЛИ файл повреждён/подменён —
    поднимаем KeyVaultError с понятным сообщением.
    """
    if not password:
        raise KeyVaultError("Пароль не может быть пустым.")

    # Минимально файл должен содержать salt + nonce + хоть что-то зашифрованное.
    minimal_length = SALT_LENGTH + NONCE_LENGTH + 1
    if len(encrypted_blob) < minimal_length:
        raise KeyVaultError("Файл с ключом повреждён или имеет неверный формат.")

    # Откусываем обратно ровно те же куски, что записали при шифровании.
    salt = encrypted_blob[:SALT_LENGTH]
    nonce = encrypted_blob[SALT_LENGTH : SALT_LENGTH + NONCE_LENGTH]
    ciphertext = encrypted_blob[SALT_LENGTH + NONCE_LENGTH :]

    encryption_key = _derive_encryption_key(password, salt)

    aesgcm = AESGCM(encryption_key)

    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    except InvalidTag:
        # Именно сюда мы попадаем при неверном пароле или испорченном файле.
        # GCM не может отличить эти два случая — и это нормально.
        raise KeyVaultError(
            "Не удалось расшифровать ключ. "
            "Скорее всего, неверный пароль или файл повреждён."
        )

    return plaintext.decode("utf-8")


def save_encrypted_key(path: Path, private_key: str, password: str) -> None:
    """
    Шифрует приватный ключ и записывает его в файл по пути path.

    Папку secrets/ создаём при необходимости.
    Файл всегда перезаписывается целиком.
    """
    encrypted_blob = encrypt_private_key(private_key, password)

    # Создаём родительскую папку (например secrets/), если её ещё нет.
    # parents=True — создаст всю цепочку папок, exist_ok=True — не упадёт, если уже есть.
    path.parent.mkdir(parents=True, exist_ok=True)

    # wb = write binary: пишем сырые байты, а не текст.
    path.write_bytes(encrypted_blob)


def load_encrypted_key(path: Path, password: str) -> str:
    """
    Читает зашифрованный файл и возвращает расшифрованный приватный ключ.
    """
    if not path.exists():
        raise KeyVaultError(f"Файл с зашифрованным ключом не найден: {path}")

    encrypted_blob = path.read_bytes()
    return decrypt_private_key(encrypted_blob, password)


def encrypted_key_exists(path: Path) -> bool:
    """
    Проверяет, существует ли уже зашифрованный файл с ключом.
    """
    return path.exists()
