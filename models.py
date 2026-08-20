from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Signal:
    action: int  # 1 = buy, -1 = sell/close, 0 = hold
    stop_loss: float = 0.0
    take_profit: float = 0.0


@dataclass
class Position:
    pair_id: str
    venue: str
    symbol: str
    qty: float
    entry_price: float
    stop_loss: float = 0.0
    take_profit: float = 0.0
    opened_at: str = field(default_factory=_utcnow_iso)


@dataclass
class ExecutionResult:
    qty: float
    price: float
    fee: float = 0.0
    order_id: str = ""
