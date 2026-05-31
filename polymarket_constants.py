from __future__ import annotations

from decimal import Decimal


# Polygon Mainnet.
POLYGON_CHAIN_ID = 137

# Основные контракты Polymarket Exchange на Polygon.
# Источник: официальная документация Polymarket Contracts.
CTF_EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"

POLYMARKET_EXCHANGE_ADDRESSES = [
    CTF_EXCHANGE_ADDRESS,
    NEG_RISK_CTF_EXCHANGE_ADDRESS,
]

# В Polymarket суммы USDC и shares обычно считаются с точностью 6 знаков.
# То есть 1 USDC = 1_000_000 raw units.
USDC_DECIMALS = Decimal("1000000")

# Gamma API — метаданные рынков (вопрос, outcomes, slug).
GAMMA_API_BASE_URL = "https://gamma-api.polymarket.com"

# Data API — публичные данные: лидерборд, активность, позиции.
# Этап 4: используем для лидерборда активных трейдеров.
DATA_API_BASE_URL = "https://data-api.polymarket.com"
