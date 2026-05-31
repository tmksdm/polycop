from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_constants import DATA_API_BASE_URL


logger = logging.getLogger("polycop")


# Веса для композитного score.
# Сумма не обязана равняться 1, но так удобнее читать.
# Эти веса легко крутить под себя:
# - хочешь упор на прибыльность — подними PNL_WEIGHT;
# - хочешь только самых активных — подними FRESHNESS_WEIGHT.
PNL_WEIGHT = Decimal("0.5")
VOLUME_WEIGHT = Decimal("0.2")
FRESHNESS_WEIGHT = Decimal("0.3")

# Сколько дней считаем "недавней активностью".
# Трейдер, который не торговал дольше этого срока, получает freshness = 0.
ACTIVE_WINDOW_DAYS = 7

# Сколько последних событий тянуть из /activity на одного трейдера.
# Берём немного: нам нужна только свежесть, а не вся история.
# Это держит нагрузку на API низкой.
ACTIVITY_PROBE_LIMIT = 100

# Небольшая пауза между запросами /activity по разным трейдерам.
# Это вежливость к публичному API и страховка от rate limit.
ACTIVITY_PROBE_DELAY_SECONDS = 0.25

# Таймаут на один HTTP-запрос.
HTTP_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class LeaderboardEntry:
    """
    Одна строка нашего собственного лидерборда.

    Часть полей приходит из официального /v1/leaderboard,
    часть (freshness, score) мы считаем сами.
    """

    proxy_wallet: str
    username: str
    pnl_usdc: Decimal
    volume_usdc: Decimal

    # Сколько сделок у трейдера за окно активности (ACTIVE_WINDOW_DAYS).
    recent_trade_count: int

    # Сколько часов назад была последняя сделка.
    # None — если активности не нашли вообще.
    hours_since_last_trade: float | None

    # Композитный итоговый рейтинг (чем больше, тем лучше).
    score: Decimal

    # X (Twitter) username, если есть — помогает отличить реального человека.
    x_username: str | None

    # Verified badge на Polymarket.
    verified: bool


async def build_leaderboard(
    *,
    time_period: str = "MONTH",
    order_by: str = "PNL",
    category: str = "OVERALL",
    limit: int = 25,
    probe_activity: bool = True,
) -> list[LeaderboardEntry]:
    """
    Главная функция: собирает наш ранжированный лидерборд.

    Шаги:
    1. Тянем официальный топ из /v1/leaderboard.
    2. (опционально) Для каждого трейдера проверяем свежесть через /activity.
    3. Считаем композитный score.
    4. Сортируем по score и возвращаем.

    probe_activity=False позволяет быстро посмотреть голый топ
    без похода в /activity по каждому трейдеру (полезно для отладки).
    """
    raw_entries = await asyncio.to_thread(
        _fetch_leaderboard_sync,
        time_period,
        order_by,
        category,
        limit,
    )

    if not raw_entries:
        logger.warning("Лидерборд вернул пустой список. Проверь параметры или доступность Data API.")
        return []

    logger.info("Получено трейдеров из официального лидерборда: %s", len(raw_entries))

    # Считаем свежесть для каждого трейдера.
    freshness_by_wallet: dict[str, tuple[int, float | None]] = {}

    if probe_activity:
        logger.info("Проверяю свежесть активности по каждому трейдеру (это занимает несколько секунд)...")

        for raw in raw_entries:
            wallet = raw["proxy_wallet"]

            recent_count, hours_since = await asyncio.to_thread(
                _fetch_trader_freshness_sync,
                wallet,
            )

            freshness_by_wallet[wallet] = (recent_count, hours_since)

            # Вежливая пауза, чтобы не долбить публичный API.
            await asyncio.sleep(ACTIVITY_PROBE_DELAY_SECONDS)

    # Находим максимумы по выборке для нормализации.
    max_pnl = max((r["pnl"] for r in raw_entries), default=Decimal("0"))
    max_volume = max((r["volume"] for r in raw_entries), default=Decimal("0"))

    entries: list[LeaderboardEntry] = []

    for raw in raw_entries:
        wallet = raw["proxy_wallet"]
        recent_count, hours_since = freshness_by_wallet.get(wallet, (0, None))

        score = _compute_score(
            pnl=raw["pnl"],
            volume=raw["volume"],
            max_pnl=max_pnl,
            max_volume=max_volume,
            hours_since_last_trade=hours_since,
        )

        entries.append(
            LeaderboardEntry(
                proxy_wallet=wallet,
                username=raw["username"],
                pnl_usdc=raw["pnl"],
                volume_usdc=raw["volume"],
                recent_trade_count=recent_count,
                hours_since_last_trade=hours_since,
                score=score,
                x_username=raw["x_username"],
                verified=raw["verified"],
            )
        )

    # Сортируем по убыванию score.
    entries.sort(key=lambda e: e.score, reverse=True)

    return entries


def _fetch_leaderboard_sync(
    time_period: str,
    order_by: str,
    category: str,
    limit: int,
) -> list[dict[str, Any]]:
    """
    Синхронный запрос к официальному /v1/leaderboard.

    Возвращает список словарей с уже распарсенными полями.
    Дальше эти словари превращаем в LeaderboardEntry.
    """
    # limit у API ограничен 50. Подстрахуемся.
    safe_limit = max(1, min(int(limit), 50))

    query = urllib.parse.urlencode(
        {
            "timePeriod": time_period,
            "orderBy": order_by,
            "category": category,
            "limit": safe_limit,
            "offset": 0,
        }
    )

    url = f"{DATA_API_BASE_URL}/v1/leaderboard?{query}"

    try:
        payload = _http_get_json(url)
    except urllib.error.HTTPError as error:
        logger.error("Leaderboard HTTP error %s", error.code)
        return []
    except urllib.error.URLError as error:
        logger.error("Leaderboard network error: %s", error)
        return []
    except TimeoutError:
        logger.error("Leaderboard timeout")
        return []
    except json.JSONDecodeError:
        logger.error("Leaderboard вернул не JSON")
        return []

    if not isinstance(payload, list):
        logger.error("Leaderboard вернул неожиданный формат (ожидал список)")
        return []

    result: list[dict[str, Any]] = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        wallet = _clean_str(item.get("proxyWallet"))

        # Без адреса кошелька строка бесполезна — копировать некого.
        if not wallet:
            continue

        result.append(
            {
                "proxy_wallet": wallet,
                "username": _clean_str(item.get("userName")) or "—",
                "pnl": _to_decimal(item.get("pnl")),
                "volume": _to_decimal(item.get("vol")),
                "x_username": _clean_str(item.get("xUsername")) or None,
                "verified": bool(item.get("verifiedBadge", False)),
            }
        )

    return result


def _fetch_trader_freshness_sync(wallet: str) -> tuple[int, float | None]:
    """
    Лёгкая проверка свежести одного трейдера через /activity.

    Возвращает кортеж:
    - сколько сделок (type=TRADE) за последние ACTIVE_WINDOW_DAYS дней;
    - сколько часов назад была самая свежая сделка (None если не нашли).

    Мы НЕ тянем всю историю. Берём только последние ACTIVITY_PROBE_LIMIT событий,
    отсортированные по времени (API по умолчанию отдаёт сначала свежие).
    Этого достаточно, чтобы понять, активен ли трейдер сейчас.
    """
    query = urllib.parse.urlencode(
        {
            "user": wallet,
            "limit": ACTIVITY_PROBE_LIMIT,
            "offset": 0,
            "type": "TRADE",
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
    )

    url = f"{DATA_API_BASE_URL}/activity?{query}"

    try:
        payload = _http_get_json(url)
    except urllib.error.HTTPError as error:
        logger.warning("Activity HTTP error %s для %s", error.code, _short_wallet(wallet))
        return (0, None)
    except urllib.error.URLError as error:
        logger.warning("Activity network error для %s: %s", _short_wallet(wallet), error)
        return (0, None)
    except TimeoutError:
        logger.warning("Activity timeout для %s", _short_wallet(wallet))
        return (0, None)
    except json.JSONDecodeError:
        logger.warning("Activity вернул не JSON для %s", _short_wallet(wallet))
        return (0, None)

    if not isinstance(payload, list) or not payload:
        return (0, None)

    now_ts = int(time.time())
    window_seconds = ACTIVE_WINDOW_DAYS * 24 * 60 * 60

    recent_count = 0
    newest_ts: int | None = None

    for event in payload:
        if not isinstance(event, dict):
            continue

        ts = event.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue

        ts = int(ts)

        # Запоминаем самую свежую сделку.
        if newest_ts is None or ts > newest_ts:
            newest_ts = ts

        # Считаем сделки внутри окна активности.
        if now_ts - ts <= window_seconds:
            recent_count += 1

    if newest_ts is None:
        return (0, None)

    hours_since = max(0.0, (now_ts - newest_ts) / 3600.0)

    return (recent_count, hours_since)


def _compute_score(
    *,
    pnl: Decimal,
    volume: Decimal,
    max_pnl: Decimal,
    max_volume: Decimal,
    hours_since_last_trade: float | None,
) -> Decimal:
    """
    Считает композитный score из доступных данных.

    Главная идея — нормализация:
    каждую метрику приводим к диапазону 0..1 как "долю от максимума в выборке".
    Иначе один кит с PnL в миллионы перекосит весь рейтинг.

    Метрики:
    - pnl_score: насколько трейдер прибыльный относительно лучшего;
    - volume_score: насколько он активный по деньгам относительно лучшего;
    - freshness_score: 1.0 если торговал только что, плавно падает до 0
      на границе окна активности, и 0 если давно молчит.

    Отрицательный PnL даёт pnl_score = 0 (убыточных не поощряем).
    """
    pnl_score = _normalize(pnl, max_pnl)
    volume_score = _normalize(volume, max_volume)
    freshness_score = _freshness_score(hours_since_last_trade)

    total = (
        pnl_score * PNL_WEIGHT
        + volume_score * VOLUME_WEIGHT
        + freshness_score * FRESHNESS_WEIGHT
    )

    # Округляем до 4 знаков, чтобы score красиво выглядел в таблице.
    return total.quantize(Decimal("0.0001"))


def _normalize(value: Decimal, maximum: Decimal) -> Decimal:
    """
    Приводит значение к диапазону 0..1 как долю от максимума.

    Отрицательные значения (например убыточный PnL) обнуляем:
    нам не нужны убыточные трейдеры в топе.
    """
    if maximum <= 0:
        return Decimal("0")

    if value <= 0:
        return Decimal("0")

    ratio = value / maximum

    if ratio > 1:
        return Decimal("1")

    return ratio


def _freshness_score(hours_since_last_trade: float | None) -> Decimal:
    """
    Превращает "сколько часов назад торговал" в оценку 0..1.

    - торговал прямо сейчас -> близко к 1.0;
    - на границе окна активности (ACTIVE_WINDOW_DAYS) -> 0.0;
    - давно молчит или активности нет -> 0.0.

    Это линейное затухание. Простое и предсказуемое.
    """
    if hours_since_last_trade is None:
        return Decimal("0")

    window_hours = Decimal(ACTIVE_WINDOW_DAYS * 24)
    hours = Decimal(str(hours_since_last_trade))

    if hours >= window_hours:
        return Decimal("0")

    if hours <= 0:
        return Decimal("1")

    # Чем меньше прошло часов, тем ближе к 1.
    return (window_hours - hours) / window_hours


def _http_get_json(url: str) -> Any:
    """
    Делает HTTP GET и возвращает JSON.

    Тот же паттерн, что и в gamma_client.py:
    добавляем User-Agent, потому что некоторые API не любят пустые клиенты.
    """
    request = urllib.request.Request(
        url=url,
        headers={
            "Accept": "application/json",
            "User-Agent": "polycop-local-dev/0.1",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8")

    return json.loads(body)


def _to_decimal(value: Any) -> Decimal:
    """
    Безопасно превращает число из JSON в Decimal.

    Для денег используем Decimal, а не float.
    Если значение битое — возвращаем 0.
    """
    if value is None:
        return Decimal("0")

    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _clean_str(value: Any) -> str:
    """
    Приводит значение к обрезанной строке.
    """
    if value is None:
        return ""

    return str(value).strip()


def _short_wallet(wallet: str) -> str:
    """
    Сокращает адрес для логов.
    """
    if len(wallet) <= 14:
        return wallet

    return f"{wallet[:6]}...{wallet[-4:]}"
