from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DecodedTrade:
    """
    Одна расшифрованная сделка из функции matchOrders(...).

    Важно:
    - trader — адрес maker из ордера. Часто это proxy/safe кошелёк Polymarket.
    - signer — адрес, который подписал ордер. Иногда именно он ближе к "реальному" пользователю.
    - role — taker или maker внутри конкретного matchOrders.
    - side — BUY или SELL относительно token_id.
    """

    tx_hash: str
    block_number: str | int | None
    exchange_address: str

    condition_id: str
    token_id: int

    trader: str
    signer: str
    role: str
    side: str

    price: Decimal
    size_usdc: Decimal
    shares: Decimal

    observed_at: str


@dataclass(frozen=True)
class MarketMetadata:
    """
    Короткая информация о рынке из Gamma API.

    condition_id — on-chain идентификатор рынка.
    question — вопрос рынка, например "Will BTC hit $120k in May?"
    slug — URL-friendly имя рынка на сайте Polymarket.
    outcome — конкретный outcome для token_id, например YES / NO.
    """

    condition_id: str
    market_id: str | None
    question: str
    slug: str | None
    outcome: str | None
