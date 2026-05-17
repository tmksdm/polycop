from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


MAX_TRACKED_TRADERS = 5


@dataclass(frozen=True)
class RiskConfig:
    """
    Риск-настройки копирования.

    ratio_percent — сколько процентов от сделки трейдера копируем.
    Например:
    - трейдер купил на $1000;
    - ratio_percent = 1;
    - наша DRY-RUN копия = $10.
    """

    ratio_percent: Decimal
    min_bet_usdc: Decimal
    hourly_limit_percent: Decimal
    dry_run_balance_usdc: Decimal
    min_price_cents: Decimal
    max_price_cents: Decimal


@dataclass(frozen=True)
class SellConfig:
    """
    Настройки продаж.

    На Этапе 3 мы их только читаем и валидируем.
    Реальная логика auto-sell/mirror-sell будет на Этапе 6.
    """

    sell_mode: str
    auto_sell_threshold_cents: Decimal
    sell_percentage: Decimal


@dataclass(frozen=True)
class AppConfig:
    """
    Полная конфигурация приложения.

    .env:
    - секреты;
    - режим DRY_RUN;
    - Alchemy WSS.

    config.json:
    - риск-настройки;
    - список трейдеров;
    - sell-настройки.
    """

    dry_run: bool
    alchemy_wss: str
    watched_traders: list[str]
    risk: RiskConfig
    sell: SellConfig
    config_path: Path
    warnings: list[str]


def load_app_config(project_root: Path) -> AppConfig:
    """
    Загружает настройки из:
    - .env;
    - config.json.

    Если config.json отсутствует или в нём ошибка,
    приложение не падает, а использует безопасные дефолты.
    """
    env_path = project_root / ".env"
    env_values = load_env_file(env_path)

    raw_config_path = env_values.get("CONFIG_PATH", "config.json").strip() or "config.json"
    config_path = Path(raw_config_path)

    if not config_path.is_absolute():
        config_path = project_root / config_path

    warnings: list[str] = []
    config_payload = _load_config_json(config_path, warnings)

    dry_run = parse_bool(env_values.get("DRY_RUN", "true"), default=True)
    alchemy_wss = env_values.get("ALCHEMY_POLYGON_WSS", "").strip()

    risk = _parse_risk_config(config_payload.get("risk", {}), warnings)
    sell = _parse_sell_config(config_payload.get("sell", {}), warnings)

    config_traders = _parse_traders_from_config(config_payload.get("traders", []), warnings)

    # Для совместимости со старым Этапом 2:
    # если в config.json трейдеры не указаны, берём WATCHED_TRADERS из .env.
    if config_traders:
        watched_traders = config_traders
    else:
        watched_traders = parse_address_list(env_values.get("WATCHED_TRADERS", ""), warnings)

    if len(watched_traders) > MAX_TRACKED_TRADERS:
        warnings.append(
            f"В списке трейдеров больше {MAX_TRACKED_TRADERS}. "
            f"Будут использованы только первые {MAX_TRACKED_TRADERS}."
        )
        watched_traders = watched_traders[:MAX_TRACKED_TRADERS]

    return AppConfig(
        dry_run=dry_run,
        alchemy_wss=alchemy_wss,
        watched_traders=watched_traders,
        risk=risk,
        sell=sell,
        config_path=config_path,
        warnings=warnings,
    )


def load_env_file(path: Path) -> dict[str, str]:
    """
    Минимальный загрузчик .env-файла.

    Он читает строки вида:
    KEY=value

    Секреты из .env никогда не должны попадать в Git.
    """
    values: dict[str, str] = {}

    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        clean_line = line.strip()

        if not clean_line or clean_line.startswith("#"):
            continue

        if "=" not in clean_line:
            continue

        key, value = clean_line.split("=", 1)
        values[key.strip()] = value.strip()

    return values


def parse_bool(value: str, default: bool = True) -> bool:
    """
    Превращает строку из .env в bool.
    """
    clean_value = value.strip().lower()

    if clean_value in {"1", "true", "yes", "y", "on"}:
        return True

    if clean_value in {"0", "false", "no", "n", "off"}:
        return False

    return default


def parse_address_list(raw_value: str, warnings: list[str]) -> list[str]:
    """
    Разбирает список EVM-адресов из строки.

    EVM-адрес — адрес в сетях Ethereum/Polygon,
    обычно выглядит как 0x + 40 hex-символов.
    """
    if not raw_value.strip():
        return []

    addresses: list[str] = []

    for item in raw_value.split(","):
        address = item.strip()

        if not address:
            continue

        if not is_probably_evm_address(address):
            warnings.append(f"Пропускаю некорректный адрес: {address}")
            continue

        addresses.append(address)

    return addresses


def is_probably_evm_address(address: str) -> bool:
    """
    Простая проверка EVM-адреса.
    """
    if not address.startswith("0x"):
        return False

    if len(address) != 42:
        return False

    hex_part = address[2:]

    try:
        int(hex_part, 16)
    except ValueError:
        return False

    return True


def _load_config_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    """
    Загружает config.json.

    Если файла нет — возвращает пустой dict,
    а дальше применятся дефолтные настройки.
    """
    if not path.exists():
        warnings.append(
            f"Файл {path.name} не найден. Использую дефолтные настройки. "
            f"Рекомендую создать его из config.example.json."
        )
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        warnings.append(f"Не смог разобрать {path.name}: {error}. Использую дефолты.")
        return {}

    if not isinstance(payload, dict):
        warnings.append(f"{path.name} должен содержать JSON-объект. Использую дефолты.")
        return {}

    return payload


def _parse_risk_config(payload: Any, warnings: list[str]) -> RiskConfig:
    """
    Читает и валидирует risk-настройки.
    """
    if not isinstance(payload, dict):
        payload = {}

    ratio_percent = _decimal_in_range(
        value=payload.get("ratio_percent"),
        default=Decimal("1"),
        minimum=Decimal("0.01"),
        maximum=Decimal("100"),
        field_name="risk.ratio_percent",
        warnings=warnings,
    )

    min_bet_usdc = _decimal_in_range(
        value=payload.get("min_bet_usdc"),
        default=Decimal("100"),
        minimum=Decimal("1"),
        maximum=Decimal("100000"),
        field_name="risk.min_bet_usdc",
        warnings=warnings,
    )

    hourly_limit_percent = _decimal_in_range(
        value=payload.get("hourly_limit_percent"),
        default=Decimal("20"),
        minimum=Decimal("0"),
        maximum=Decimal("100"),
        field_name="risk.hourly_limit_percent",
        warnings=warnings,
    )

    dry_run_balance_usdc = _decimal_in_range(
        value=payload.get("dry_run_balance_usdc"),
        default=Decimal("100"),
        minimum=Decimal("1"),
        maximum=Decimal("1000000"),
        field_name="risk.dry_run_balance_usdc",
        warnings=warnings,
    )

    min_price_cents = _decimal_in_range(
        value=payload.get("min_price_cents"),
        default=Decimal("3"),
        minimum=Decimal("0"),
        maximum=Decimal("100"),
        field_name="risk.min_price_cents",
        warnings=warnings,
    )

    max_price_cents = _decimal_in_range(
        value=payload.get("max_price_cents"),
        default=Decimal("97"),
        minimum=Decimal("0"),
        maximum=Decimal("100"),
        field_name="risk.max_price_cents",
        warnings=warnings,
    )

    if min_price_cents > max_price_cents:
        warnings.append(
            "risk.min_price_cents больше risk.max_price_cents. "
            "Возвращаю безопасный диапазон 3–97¢."
        )
        min_price_cents = Decimal("3")
        max_price_cents = Decimal("97")

    return RiskConfig(
        ratio_percent=ratio_percent,
        min_bet_usdc=min_bet_usdc,
        hourly_limit_percent=hourly_limit_percent,
        dry_run_balance_usdc=dry_run_balance_usdc,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
    )


def _parse_sell_config(payload: Any, warnings: list[str]) -> SellConfig:
    """
    Читает sell-настройки.

    На Этапе 3 они нужны только для будущей совместимости.
    """
    if not isinstance(payload, dict):
        payload = {}

    raw_sell_mode = str(payload.get("sell_mode", "Full")).strip().lower()

    allowed_modes = {
        "ignore": "Ignore",
        "full": "Full",
        "percentage": "Percentage",
    }

    if raw_sell_mode not in allowed_modes:
        warnings.append(
            f"sell.sell_mode={raw_sell_mode!r} неизвестен. "
            "Использую Full."
        )
        sell_mode = "Full"
    else:
        sell_mode = allowed_modes[raw_sell_mode]

    auto_sell_threshold_cents = _decimal_in_range(
        value=payload.get("auto_sell_threshold_cents"),
        default=Decimal("99"),
        minimum=Decimal("50"),
        maximum=Decimal("100"),
        field_name="sell.auto_sell_threshold_cents",
        warnings=warnings,
    )

    sell_percentage = _decimal_in_range(
        value=payload.get("sell_percentage"),
        default=Decimal("100"),
        minimum=Decimal("1"),
        maximum=Decimal("100"),
        field_name="sell.sell_percentage",
        warnings=warnings,
    )

    return SellConfig(
        sell_mode=sell_mode,
        auto_sell_threshold_cents=auto_sell_threshold_cents,
        sell_percentage=sell_percentage,
    )


def _parse_traders_from_config(payload: Any, warnings: list[str]) -> list[str]:
    """
    Читает список трейдеров из config.json.
    """
    if payload is None:
        return []

    if not isinstance(payload, list):
        warnings.append("config.json поле traders должно быть списком. Игнорирую traders.")
        return []

    traders: list[str] = []

    for item in payload:
        address = str(item).strip()

        if not address:
            continue

        if not is_probably_evm_address(address):
            warnings.append(f"Пропускаю некорректный адрес из traders: {address}")
            continue

        traders.append(address)

    return traders


def _decimal_in_range(
    *,
    value: Any,
    default: Decimal,
    minimum: Decimal,
    maximum: Decimal,
    field_name: str,
    warnings: list[str],
) -> Decimal:
    """
    Безопасно читает Decimal и проверяет диапазон.
    """
    parsed = _to_decimal(value)

    if parsed is None:
        return default

    if parsed < minimum or parsed > maximum:
        warnings.append(
            f"{field_name}={parsed} вне диапазона {minimum}–{maximum}. "
            f"Использую дефолт {default}."
        )
        return default

    return parsed


def _to_decimal(value: Any) -> Decimal | None:
    """
    Превращает int/float/str в Decimal.

    Decimal используем вместо float, потому что это деньги.
    Для денег float может давать неприятные ошибки округления.
    """
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
