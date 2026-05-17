from __future__ import annotations

import asyncio
from datetime import datetime


async def main() -> None:
    """
    Главная асинхронная функция приложения.

    Сейчас это просто тест:
    проверяем, что Python и asyncio работают.
    """
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("Polycop started")
    print(f"Start time: {started_at}")
    print("Mode: DRY-RUN")
    print("Hello async world")

    # Небольшая асинхронная пауза.
    # Позже здесь будет ожидание событий из блокчейна.
    await asyncio.sleep(1)

    print("Async check finished successfully")


if __name__ == "__main__":
    asyncio.run(main())
