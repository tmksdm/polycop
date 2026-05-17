from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from polymarket_constants import GAMMA_API_BASE_URL
from trade_models import MarketMetadata


logger = logging.getLogger("polycop")


@dataclass(frozen=True)
class GammaMarketRecord:
    """
    Внутренняя модель рынка из Gamma API.

    Почему не используем сразу MarketMetadata:
    - MarketMetadata содержит outcome для конкретного token_id;
    - а один рынок содержит несколько token_id;
    - поэтому в cache нужно хранить весь список token_ids/outcomes.
    """

    condition_id: str
    market_id: str | None
    question: str
    slug: str | None
    token_ids: list[str]
    outcomes: list[str]


# Простой in-memory cache:
# condition_id.lower() -> GammaMarketRecord или None.
#
# None тоже кэшируем, чтобы не спрашивать Gamma API бесконечно
# по рынкам, которые он не нашёл.
_market_cache: dict[str, GammaMarketRecord | None] = {}


async def get_market_metadata(
    *,
    condition_id: str,
    token_id: int,
) -> MarketMetadata | None:
    """
    Асинхронно получает метаданные рынка из Gamma API.

    Важно:
    - рынок кэшируется по condition_id;
    - outcome каждый раз вычисляется по конкретному token_id.
    """
    cache_key = condition_id.lower()

    if cache_key in _market_cache:
        cached_record = _market_cache[cache_key]

        if cached_record is None:
            return None

        return _record_to_metadata(
            record=cached_record,
            token_id=token_id,
        )

    record = await asyncio.to_thread(
        _fetch_market_record_sync,
        condition_id,
    )

    _market_cache[cache_key] = record

    if record is None:
        return None

    return _record_to_metadata(
        record=record,
        token_id=token_id,
    )


def _fetch_market_record_sync(condition_id: str) -> GammaMarketRecord | None:
    """
    Синхронная часть запроса к Gamma API.

    Пробуем несколько вариантов query-параметра, потому что у Gamma API
    исторически встречались разные имена фильтров в документации/ответах.
    """
    clean_condition_id = condition_id.strip()

    if not clean_condition_id:
        return None

    candidate_urls = _build_candidate_market_urls(clean_condition_id)

    for url in candidate_urls:
        try:
            payload = _http_get_json(url)
        except urllib.error.HTTPError as error:
            logger.warning("Gamma API HTTP error %s для %s", error.code, _safe_url(url))
            continue
        except urllib.error.URLError as error:
            logger.warning("Gamma API network error для %s: %s", _safe_url(url), error)
            continue
        except TimeoutError:
            logger.warning("Gamma API timeout для %s", _safe_url(url))
            continue
        except json.JSONDecodeError:
            logger.warning("Gamma API вернул не JSON для %s", _safe_url(url))
            continue

        market = _extract_first_market(payload)

        if market is None:
            continue

        record = _market_json_to_record(
            market=market,
            requested_condition_id=clean_condition_id,
        )

        if record is not None:
            return record

    return None


def _build_candidate_market_urls(condition_id: str) -> list[str]:
    """
    Собирает варианты URL для поиска рынка по condition_id.

    Основной ожидаемый вариант:
    /markets?condition_ids=<condition_id>
    """
    ids_to_try = [condition_id]

    # Иногда API/индексы могут хранить conditionId без 0x.
    # Добавляем fallback без префикса.
    if condition_id.startswith("0x"):
        ids_to_try.append(condition_id[2:])

    param_names = [
        "condition_ids",
        "condition_id",
        "conditionIds",
        "conditionId",
    ]

    urls: list[str] = []

    for raw_id in ids_to_try:
        encoded_id = urllib.parse.quote(raw_id, safe="")

        for param_name in param_names:
            urls.append(f"{GAMMA_API_BASE_URL}/markets?{param_name}={encoded_id}")

    return urls


def _http_get_json(url: str) -> Any:
    """
    Делает HTTP GET и возвращает JSON.

    User-Agent добавляем, потому что некоторые API не любят совсем пустые клиенты.
    """
    request = urllib.request.Request(
        url=url,
        headers={
            "Accept": "application/json",
            "User-Agent": "polycop-local-dev/0.1",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")

    return json.loads(body)


def _extract_first_market(payload: Any) -> dict[str, Any] | None:
    """
    Gamma /markets обычно возвращает список рынков.

    Но на всякий случай поддерживаем и dict-ответ.
    """
    if isinstance(payload, list):
        if not payload:
            return None

        first_item = payload[0]
        if isinstance(first_item, dict):
            return first_item

        return None

    if isinstance(payload, dict):
        return payload

    return None


def _market_json_to_record(
    *,
    market: dict[str, Any],
    requested_condition_id: str,
) -> GammaMarketRecord | None:
    """
    Превращает JSON рынка Gamma API во внутреннюю запись GammaMarketRecord.
    """
    question = _first_non_empty_string(
        market.get("question"),
        market.get("title"),
        market.get("name"),
    )

    if question is None:
        return None

    condition_id = _first_non_empty_string(
        market.get("conditionId"),
        market.get("condition_id"),
        requested_condition_id,
    )

    if condition_id is None:
        condition_id = requested_condition_id

    market_id = _optional_string(market.get("id"))
    slug = _optional_string(market.get("slug"))

    token_ids = _parse_jsonish_list(market.get("clobTokenIds"))
    outcomes = _parse_jsonish_list(market.get("outcomes"))

    return GammaMarketRecord(
        condition_id=condition_id,
        market_id=market_id,
        question=question,
        slug=slug,
        token_ids=token_ids,
        outcomes=outcomes,
    )


def _record_to_metadata(
    *,
    record: GammaMarketRecord,
    token_id: int,
) -> MarketMetadata:
    """
    Превращает закэшированный рынок в MarketMetadata для конкретного token_id.
    """
    outcome = _find_outcome_for_token(
        token_id=token_id,
        token_ids=record.token_ids,
        outcomes=record.outcomes,
    )

    return MarketMetadata(
        condition_id=record.condition_id,
        market_id=record.market_id,
        question=record.question,
        slug=record.slug,
        outcome=outcome,
    )


def _parse_jsonish_list(value: Any) -> list[str]:
    """
    Gamma часто возвращает поля outcomes/clobTokenIds как JSON-строку:

    outcomes='["Yes","No"]'
    clobTokenIds='["123","456"]'

    Но иногда API может вернуть уже готовый list.
    Поддерживаем оба случая.
    """
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item) for item in value]

    if isinstance(value, str):
        clean_value = value.strip()

        if not clean_value:
            return []

        try:
            parsed = json.loads(clean_value)
        except json.JSONDecodeError:
            return []

        if isinstance(parsed, list):
            return [str(item) for item in parsed]

    return []


def _find_outcome_for_token(
    *,
    token_id: int,
    token_ids: Sequence[str],
    outcomes: Sequence[str],
) -> str | None:
    """
    По token_id пытаемся понять outcome.

    Пример:
    clobTokenIds = ["111", "222"]
    outcomes = ["Yes", "No"]

    Если token_id == 111 -> outcome = Yes.
    """
    token_id_text = str(token_id)

    for index, current_token_id in enumerate(token_ids):
        if str(current_token_id) != token_id_text:
            continue

        if index < len(outcomes):
            return str(outcomes[index])

        return None

    return None


def _first_non_empty_string(*values: Any) -> str | None:
    """
    Возвращает первую непустую строку.
    """
    for value in values:
        if not isinstance(value, str):
            continue

        clean_value = value.strip()

        if clean_value:
            return clean_value

    return None


def _optional_string(value: Any) -> str | None:
    """
    Приводит значение к строке, если оно не пустое.
    """
    if value is None:
        return None

    clean_value = str(value).strip()

    if not clean_value:
        return None

    return clean_value


def _safe_url(url: str) -> str:
    """
    Безопасный URL для логов.

    Здесь нет секретов, но оставляем отдельную функцию,
    чтобы привычка не логировать лишнее сохранялась.
    """
    return url
