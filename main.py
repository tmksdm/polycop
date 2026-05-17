from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_env_file(path: Path) -> dict[str, str]:
    """
    Минимальный загрузчик .env-файла.

    Он читает строки вида:
    KEY=value

    Мы пока не используем стороннюю библиотеку python-dotenv,
    чтобы на Этапе 0 не добавлять лишние зависимости.
    """
    values: dict[str, str] = {}

    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        clean_line = line.strip()

        # Пропускаем пустые строки и комментарии.
        if not clean_line or clean_line.startswith("#"):
            continue

        # Если в строке нет "=", это не переменная окружения.
        if "=" not in clean_line:
            continue

        key, value = clean_line.split("=", 1)
        values[key.strip()] = value.strip()

    return values


def mask_secret_url(url: str) -> str:
    """
    Маскирует секретный URL перед выводом в консоль.

    Пример:
    wss://polygon-mainnet.g.alchemy.com/v2/abcdef123456

    Превратится в:
    wss://polygon-mainnet.g.alchemy.com/v2/abc...456

    Так мы можем понять, что URL прочитался,
    но не светим API key целиком.
    """
    if "/" not in url:
        return "***"

    prefix, secret = url.rsplit("/", 1)

    if len(secret) <= 8:
        masked_secret = "***"
    else:
        masked_secret = f"{secret[:3]}...{secret[-3:]}"

    return f"{prefix}/{masked_secret}"


async def main() -> None:
    """
    Главная асинхронная функция приложения.

    Сейчас она:
    - загружает локальный .env;
    - проверяет базовые настройки;
    - показывает, что asyncio работает.
    """
    env_values = load_env_file(ENV_PATH)

    alchemy_wss = env_values.get("ALCHEMY_POLYGON_WSS", "")
    dry_run_raw = env_values.get("DRY_RUN", "true").lower()
    dry_run = dry_run_raw in {"1", "true", "yes", "y"}

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("Polycop started")
    print(f"Start time: {started_at}")
    print(f"Mode: {'DRY-RUN' if dry_run else 'LIVE'}")

    if not alchemy_wss:
        print("Alchemy WSS: not configured")
        print("Please add ALCHEMY_POLYGON_WSS to .env")
    elif not alchemy_wss.startswith("wss://"):
        print("Alchemy WSS: invalid format")
        print("Expected URL starting with wss://")
    else:
        print(f"Alchemy WSS: {mask_secret_url(alchemy_wss)}")

    print("Hello async world")

    # Небольшая асинхронная пауза.
    # Позже здесь будет ожидание событий из блокчейна.
    await asyncio.sleep(1)

    print("Async check finished successfully")


if __name__ == "__main__":
    asyncio.run(main())
