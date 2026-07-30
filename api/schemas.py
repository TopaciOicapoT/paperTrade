"""
api/schemas.py
--------------
Modelos Pydantic para requests y responses de la API.
"""

from __future__ import annotations
from typing import Any
from pydantic import BaseModel


class OpenOrder(BaseModel):
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    risk_usdt: float
    reward_usdt: float
    tp_pct: float
    sl_pct: float
    leverage: int
    futures: bool
    ai_probability: float
    volume_ratio: float
    entry_time: str
    monthly_level: float | None = None

    model_config = {"extra": "allow"}


class BotSnapshot(BaseModel):
    balance_usdt: float
    balance_peak: float
    initial_balance: float
    pnl_usdt: float
    pnl_pct: float
    drawdown_pct: float
    trading_halted: bool
    stop_requested: bool
    news_paused: bool
    news_paused_until: float | None
    open_positions: int
    total_trades: int
    wins: int
    losses: int
    manual_closes: int
    win_rate: float
    leverage: int
    futures_enabled: bool
    mode: str
    symbols: list[str]
    open_orders: dict[str, Any]


class TradeRecord(BaseModel):
    id: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float | None
    stop_loss: float | None
    take_profit: float | None
    quantity: float | None
    risk_usdt: float | None
    pnl_usdt: float | None
    pnl_pct: float | None
    fee_usdt: float | None
    balance_after: float | None
    exit_type: str | None
    leverage: int | None
    futures: bool | None
    ai_probability: float | None
    volume_ratio: float | None
    entry_time: str | None

    model_config = {"from_attributes": True}


class CloseResult(BaseModel):
    symbol: str
    exit_price: float
    exit_type: str
    pnl_usdt: float
    pnl_pct: float
    balance_after: float


class MessageResponse(BaseModel):
    message: str
    detail: Any = None
