from __future__ import annotations


# Минимальный ABI CTF Exchange V2.
# Сейчас нам нужна только функция matchOrders(...).
#
# Solidity-сигнатура:
# matchOrders(
#   bytes32 conditionId,
#   Order takerOrder,
#   Order[] makerOrders,
#   uint256 takerFillAmount,
#   uint256[] makerFillAmounts,
#   uint256 takerFeeAmount,
#   uint256[] makerFeeAmounts
# )
#
# Order:
# {
#   uint256 salt;
#   address maker;
#   address signer;
#   uint256 tokenId;
#   uint256 makerAmount;
#   uint256 takerAmount;
#   uint8 side;
#   uint8 signatureType;
#   uint256 timestamp;
#   bytes32 metadata;
#   bytes32 builder;
#   bytes signature;
# }
ORDER_COMPONENTS = [
    {"name": "salt", "type": "uint256"},
    {"name": "maker", "type": "address"},
    {"name": "signer", "type": "address"},
    {"name": "tokenId", "type": "uint256"},
    {"name": "makerAmount", "type": "uint256"},
    {"name": "takerAmount", "type": "uint256"},
    {"name": "side", "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
    {"name": "timestamp", "type": "uint256"},
    {"name": "metadata", "type": "bytes32"},
    {"name": "builder", "type": "bytes32"},
    {"name": "signature", "type": "bytes"},
]


CTF_EXCHANGE_ABI = [
    {
        "type": "function",
        "name": "matchOrders",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {
                "name": "takerOrder",
                "type": "tuple",
                "components": ORDER_COMPONENTS,
            },
            {
                "name": "makerOrders",
                "type": "tuple[]",
                "components": ORDER_COMPONENTS,
            },
            {"name": "takerFillAmount", "type": "uint256"},
            {"name": "makerFillAmounts", "type": "uint256[]"},
            {"name": "takerFeeAmount", "type": "uint256"},
            {"name": "makerFeeAmounts", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "preapproveOrder",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "order",
                "type": "tuple",
                "components": ORDER_COMPONENTS,
            }
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "invalidatePreapprovedOrder",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "orderHash", "type": "bytes32"},
        ],
        "outputs": [],
    },
]
