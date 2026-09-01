from __future__ import annotations

from abc import ABC, abstractmethod


class BrokerOrderRejectedError(RuntimeError):
    """The venue conclusively rejected an order before it could fill."""


class BrokerOrderUncertainError(RuntimeError):
    """The request may have reached a venue but its final outcome is unknown.

    ``external_order_id`` is deliberately retained so the caller can persist
    it with the durable intent before marking that intent as requiring manual
    reconciliation.  Never retry this exception automatically.
    """

    def __init__(self, message: str, external_order_id: str = ""):
        super().__init__(message)
        self.external_order_id = str(external_order_id or "")


class Broker(ABC):
    venue: str = "base"

    @abstractmethod
    def get_equity(self, prices: dict[str, float]) -> float:
        pass

    @abstractmethod
    def buy(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ):
        pass

    @abstractmethod
    def sell(
        self,
        symbol: str,
        qty: float,
        price_hint: float,
        client_order_id: str | None = None,
    ):
        pass
