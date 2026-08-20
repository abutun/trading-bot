from __future__ import annotations

from abc import ABC, abstractmethod


class Broker(ABC):
    venue: str = "base"

    @abstractmethod
    def get_equity(self, prices: dict[str, float]) -> float:
        pass

    @abstractmethod
    def buy(self, symbol: str, qty: float, price_hint: float):
        pass

    @abstractmethod
    def sell(self, symbol: str, qty: float, price_hint: float):
        pass
