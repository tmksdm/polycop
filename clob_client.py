from __future__ import annotations

from dataclasses import dataclass

from eth_account import Account
from py_clob_client_v2 import ApiCreds, ClobClient

from polymarket_constants import POLYGON_CHAIN_ID


# Хост CLOB по умолчанию.
# Дублируем здесь как safety-дефолт: если в .env переменной CLOB_HOST нет,
# код всё равно знает, куда стучаться. В .env его можно переопределить.
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"


class ClobClientError(Exception):
    """
    Ошибка работы с CLOB.

    Своё исключение нужно, чтобы вызывающий код (main.py) мог отличить
    "проблему именно с CLOB" (нет сети/VPN, сервер недоступен)
    от любых других ошибок.
    """


@dataclass(frozen=True)
class CredsFingerprint:
    """
    "Отпечаток" L2-credentials для безопасного показа в логах/UI.

    Мы НИКОГДА не печатаем secret и passphrase целиком.
    Показываем только края, чтобы человек мог глазами убедиться,
    что creds вообще получены и не пустые — но не утекли в лог.
    """

    api_key: str          # api_key не секретен сам по себе, его можно показать
    secret_preview: str   # только края секрета, середина скрыта
    passphrase_preview: str


def _mask_middle(value: str, visible_edge: int = 4) -> str:
    """
    Превращает секрет в безопасный для показа вид: первые и последние
    visible_edge символов, середина заменена на "...".

    Пример: 'abcdef ...длинная строка... wxyz' -> 'abcd...wxyz'.
    Если строка короткая — показываем только её длину, без содержимого.
    """
    if not value:
        return "(пусто)"

    # Слишком короткую строку безопаснее не показывать вообще,
    # иначе "края" раскроют почти весь секрет.
    if len(value) <= visible_edge * 2:
        return f"(скрыто, длина {len(value)})"

    return f"{value[:visible_edge]}...{value[-visible_edge:]}"


class ClobReadOnlyClient:
    """
    Обёртка над py-clob-client-v2 в режиме ТОЛЬКО ЧТЕНИЕ.

    Намеренно НЕ содержит ни одного метода отправки/подписи ордеров.
    На Этапе 5.2 её задача — доказать, что мы умеем:
    - инициализировать клиент с нашим ключом (L1-auth);
    - получить у CLOB L2 API-creds;
    - узнать адрес нашего кошелька.

    Торговля появится отдельным шагом на Этапе 5.3.
    """

    def __init__(
        self,
        private_key: str,
        *,
        host: str = DEFAULT_CLOB_HOST,
        funder: str | None = None,
        chain_id: int = POLYGON_CHAIN_ID,
    ) -> None:
        """
        private_key — приватный ключ EOA (строка вида 0x...).
        host        — адрес CLOB API.
        funder      — адрес смарт-кошелька Polymarket (где лежат деньги).
                      На 5.2 не используется, но прокидываем заранее.
        chain_id    — 137 (Polygon mainnet).
        """
        clean_key = private_key.strip()
        if not clean_key:
            raise ClobClientError("Приватный ключ пустой.")

        # Вычисляем адрес кошелька ЛОКАЛЬНО из ключа.
        # Это чистая математика, без обращения к сети — поэтому безопасно
        # и работает даже без VPN.
        try:
            account = Account.from_key(clean_key)
        except Exception as error:
            # Не печатаем сам ключ в сообщении об ошибке — только факт, что он невалиден.
            raise ClobClientError(f"Не удалось прочитать приватный ключ: {error}")

        self._address: str = account.address
        self._host = host.strip() or DEFAULT_CLOB_HOST
        self._funder = funder.strip() if funder else None
        self._chain_id = chain_id

        # Поднимаем L1-клиент (умеет подписывать кошельком, нужен для деривации creds).
        # funder прокидываем только если он задан — иначе SDK работает по EOA.
        try:
            if self._funder:
                self._client = ClobClient(
                    host=self._host,
                    chain_id=self._chain_id,
                    key=clean_key,
                    funder=self._funder,
                )
            else:
                self._client = ClobClient(
                    host=self._host,
                    chain_id=self._chain_id,
                    key=clean_key,
                )
        except Exception as error:
            raise ClobClientError(f"Не удалось создать CLOB-клиент: {error}")

        # Сюда положим creds после успешной деривации.
        self._creds: ApiCreds | None = None

    @property
    def address(self) -> str:
        """Адрес нашего EOA-кошелька (0x...). Известен сразу, без сети."""
        return self._address

    @property
    def host(self) -> str:
        """Адрес CLOB API, к которому подключён клиент."""
        return self._host

    def derive_api_creds(self) -> CredsFingerprint:
        """
        Получает у CLOB L2 API-credentials по подписи нашего кошелька.

        Это ЕДИНСТВЕННЫЙ сетевой вызов в этом классе.
        Именно тут вылезет ошибка, если выключен VPN или CLOB недоступен.

        Возвращает безопасный "отпечаток" creds (без полных секретов).
        Сами creds сохраняются внутри объекта в self._creds.
        """
        try:
            creds = self._client.create_or_derive_api_key()
        except Exception as error:
            raise ClobClientError(
                "Не удалось получить API-ключи у CLOB. "
                "Частые причины: выключен VPN, регион заблокирован, "
                "или сервер Polymarket временно недоступен. "
                f"Техническая деталь: {error}"
            )

        self._creds = creds

        # Возвращаем безопасный отпечаток. Поля creds читаем аккуратно:
        # в SDK это объект ApiCreds с атрибутами api_key/api_secret/api_passphrase.
        return CredsFingerprint(
            api_key=getattr(creds, "api_key", "") or "",
            secret_preview=_mask_middle(getattr(creds, "api_secret", "") or ""),
            passphrase_preview=_mask_middle(getattr(creds, "api_passphrase", "") or ""),
        )

    def has_creds(self) -> bool:
        """True, если creds уже получены в этой сессии."""
        return self._creds is not None
