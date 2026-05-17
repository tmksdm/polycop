from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from web3 import Web3

from polymarket_abi import CTF_EXCHANGE_ABI
from polymarket_constants import USDC_DECIMALS
from trade_models import DecodedTrade


# Web3 здесь используется без RPC-провайдера.
# Нам не нужно отправлять запросы в сеть — только ABI-декодирование input data.
_web3 = Web3()
_exchange_contract = _web3.eth.contract(abi=CTF_EXCHANGE_ABI)


ORDER_FIELD_NAMES = [
    "salt",
    "maker",
    "signer",
    "tokenId",
    "makerAmount",
    "takerAmount",
    "side",
    "signatureType",
    "timestamp",
    "metadata",
    "builder",
    "signature",
]


def decode_polymarket_trades(transaction: dict[str, Any]) -> list[DecodedTrade]:
    """
    Пытается декодировать сырую транзакцию Polymarket Exchange.

    Возвращает список сделок.
    Почему список:
    - один вызов matchOrders может включать одного taker и несколько maker-ордеров;
    - нам важно видеть каждого участника отдельно.
    """
    input_data = transaction.get("input", "")

    if not isinstance(input_data, str) or not input_data.startswith("0x"):
        return []

    # Пустой input означает обычный перевод MATIC/ETH-style, не вызов функции.
    if len(input_data) <= 2:
        return []

    try:
        function, decoded_args = _exchange_contract.decode_function_input(input_data)
    except ValueError:
        # Это может быть функция, которой нет в нашем минимальном ABI.
        return []

    function_name = getattr(function, "fn_name", "")

    if function_name != "matchOrders":
        return []

    return _decode_match_orders(transaction=transaction, decoded_args=decoded_args)


def _decode_match_orders(
    transaction: dict[str, Any],
    decoded_args: Mapping[str, Any],
) -> list[DecodedTrade]:
    """
    Декодирует аргументы matchOrders(...) в список DecodedTrade.
    """
    tx_hash = str(transaction.get("hash") or "unknown")
    block_number = transaction.get("blockNumber")
    exchange_address = str(transaction.get("to") or "unknown")

    condition_id_raw = decoded_args.get("conditionId")
    condition_id = _bytes32_to_hex(condition_id_raw)

    taker_order_raw = decoded_args.get("takerOrder")
    maker_orders_raw = decoded_args.get("makerOrders", [])
    taker_fill_amount_raw = decoded_args.get("takerFillAmount", 0)
    maker_fill_amounts_raw = decoded_args.get("makerFillAmounts", [])

    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    trades: list[DecodedTrade] = []

    taker_order = _normalize_order(taker_order_raw)
    if taker_order is not None:
        taker_trade = _order_to_trade(
            tx_hash=tx_hash,
            block_number=block_number,
            exchange_address=exchange_address,
            condition_id=condition_id,
            role="taker",
            order=taker_order,
            fill_amount_raw=int(taker_fill_amount_raw),
            observed_at=observed_at,
        )

        if taker_trade is not None:
            trades.append(taker_trade)

    if isinstance(maker_orders_raw, Sequence):
        for index, maker_order_raw in enumerate(maker_orders_raw):
            maker_order = _normalize_order(maker_order_raw)
            if maker_order is None:
                continue

            fill_amount_raw = _safe_list_int(maker_fill_amounts_raw, index)

            maker_trade = _order_to_trade(
                tx_hash=tx_hash,
                block_number=block_number,
                exchange_address=exchange_address,
                condition_id=condition_id,
                role="maker",
                order=maker_order,
                fill_amount_raw=fill_amount_raw,
                observed_at=observed_at,
            )

            if maker_trade is not None:
                trades.append(maker_trade)

    return trades


def _normalize_order(raw_order: Any) -> dict[str, Any] | None:
    """
    Приводит Order из web3.py к обычному dict.

    В разных версиях web3.py tuple-структуры могут приходить как:
    - dict-like объект;
    - tuple/list в порядке полей ABI.

    Мы поддерживаем оба варианта, чтобы код был устойчивее.
    """
    if raw_order is None:
        return None

    if isinstance(raw_order, Mapping):
        return {field_name: raw_order.get(field_name) for field_name in ORDER_FIELD_NAMES}

    if isinstance(raw_order, Sequence) and not isinstance(raw_order, str | bytes | bytearray):
        if len(raw_order) < len(ORDER_FIELD_NAMES):
            return None

        return {
            field_name: raw_order[index]
            for index, field_name in enumerate(ORDER_FIELD_NAMES)
        }

    return None


def _order_to_trade(
    *,
    tx_hash: str,
    block_number: str | int | None,
    exchange_address: str,
    condition_id: str,
    role: str,
    order: dict[str, Any],
    fill_amount_raw: int,
    observed_at: str,
) -> DecodedTrade | None:
    """
    Превращает один Order в DecodedTrade.

    makerAmount/takerAmount:
    - BUY:
      makerAmount = сколько USDC пользователь готов отдать;
      takerAmount = сколько outcome shares пользователь хочет получить.
    - SELL:
      makerAmount = сколько outcome shares пользователь продаёт;
      takerAmount = сколько USDC пользователь хочет получить.

    price всегда считаем как USDC / share.
    """
    try:
        side_number = int(order["side"])
        token_id = int(order["tokenId"])
        maker_amount_raw = int(order["makerAmount"])
        taker_amount_raw = int(order["takerAmount"])
    except (TypeError, ValueError, KeyError):
        return None

    side = _side_to_text(side_number)

    if side == "UNKNOWN":
        return None

    if maker_amount_raw <= 0 or taker_amount_raw <= 0 or fill_amount_raw <= 0:
        return None

    try:
        price = _calculate_price(
            side=side,
            maker_amount_raw=maker_amount_raw,
            taker_amount_raw=taker_amount_raw,
        )

        size_usdc, shares = _calculate_filled_size(
            side=side,
            price=price,
            fill_amount_raw=fill_amount_raw,
        )
    except (DivisionByZero, InvalidOperation, ValueError):
        return None

    trader = str(order.get("maker") or "unknown")
    signer = str(order.get("signer") or "unknown")

    return DecodedTrade(
        tx_hash=tx_hash,
        block_number=block_number,
        exchange_address=exchange_address,
        condition_id=condition_id,
        token_id=token_id,
        trader=trader,
        signer=signer,
        role=role,
        side=side,
        price=price,
        size_usdc=size_usdc,
        shares=shares,
        observed_at=observed_at,
    )


def _calculate_price(
    *,
    side: str,
    maker_amount_raw: int,
    taker_amount_raw: int,
) -> Decimal:
    """
    Считает цену outcome token.

    Так как makerAmount и takerAmount используют одинаковую точность 1e6,
    для цены можно делить raw на raw.
    """
    maker_amount = Decimal(maker_amount_raw)
    taker_amount = Decimal(taker_amount_raw)

    if side == "BUY":
        # Покупатель отдаёт USDC и получает shares.
        return maker_amount / taker_amount

    if side == "SELL":
        # Продавец отдаёт shares и получает USDC.
        return taker_amount / maker_amount

    raise ValueError(f"Неизвестная сторона ордера: {side}")


def _calculate_filled_size(
    *,
    side: str,
    price: Decimal,
    fill_amount_raw: int,
) -> tuple[Decimal, Decimal]:
    """
    Считает фактически исполненный размер сделки.

    В matchOrders fillAmount всегда задан в единицах makerAmount конкретного ордера:
    - для BUY makerAmount = USDC;
    - для SELL makerAmount = shares.
    """
    fill_amount = Decimal(fill_amount_raw) / USDC_DECIMALS

    if side == "BUY":
        size_usdc = fill_amount
        shares = size_usdc / price
        return size_usdc, shares

    if side == "SELL":
        shares = fill_amount
        size_usdc = shares * price
        return size_usdc, shares

    raise ValueError(f"Неизвестная сторона ордера: {side}")


def _side_to_text(side_number: int) -> str:
    """
    Enum Side в контракте:
    0 = BUY
    1 = SELL
    """
    if side_number == 0:
        return "BUY"

    if side_number == 1:
        return "SELL"

    return "UNKNOWN"


def _bytes32_to_hex(value: Any) -> str:
    """
    Приводит bytes32 к hex-строке.
    """
    if isinstance(value, bytes):
        return "0x" + value.hex()

    if isinstance(value, str):
        return value

    return str(value)


def _safe_list_int(values: Any, index: int) -> int:
    """
    Безопасно достаёт int из списка.
    Если что-то пошло не так — возвращаем 0.
    """
    if not isinstance(values, Sequence):
        return 0

    if index >= len(values):
        return 0

    try:
        return int(values[index])
    except (TypeError, ValueError):
        return 0


def trade_matches_watched_traders(
    trade: DecodedTrade,
    watched_traders: list[str],
) -> bool:
    """
    Проверяет, относится ли сделка к одному из отслеживаемых трейдеров.

    Сравниваем и maker, и signer:
    - maker часто является proxy/safe кошельком Polymarket;
    - signer может быть EOA-адресом пользователя.
    EOA — обычный кошелёк, управляемый приватным ключом.
    """
    if not watched_traders:
        return True

    watched_lower = {address.lower() for address in watched_traders}

    return (
        trade.trader.lower() in watched_lower
        or trade.signer.lower() in watched_lower
    )


def format_trade_for_console(trade: DecodedTrade) -> str:
    """
    Делает короткую строку для вывода сделки в консоль.
    """
    price_cents = trade.price * Decimal("100")

    return (
        f"{trade.observed_at} | "
        f"tx={_short_hash(trade.tx_hash)} | "
        f"block={trade.block_number} | "
        f"{trade.role.upper()} {trade.side} | "
        f"price={_format_decimal(price_cents, 2)}¢ | "
        f"size=${_format_decimal(trade.size_usdc, 2)} | "
        f"shares={_format_decimal(trade.shares, 2)} | "
        f"trader={_short_address(trade.trader)} | "
        f"signer={_short_address(trade.signer)} | "
        f"token={_short_token_id(trade.token_id)}"
    )


def _format_decimal(value: Decimal, places: int) -> str:
    """
    Форматирует Decimal без scientific notation.
    """
    quant = Decimal("1").scaleb(-places)
    return format(value.quantize(quant), "f")


def _short_hash(value: str) -> str:
    if len(value) <= 16:
        return value

    return f"{value[:10]}...{value[-6:]}"


def _short_address(value: str) -> str:
    if len(value) <= 14:
        return value

    return f"{value[:6]}...{value[-4:]}"


def _short_token_id(value: int) -> str:
    text = str(value)

    if len(text) <= 14:
        return text

    return f"{text[:8]}...{text[-6:]}"
