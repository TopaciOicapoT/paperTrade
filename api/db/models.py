"""
api/db/models.py
----------------
Modelos ORM SQLAlchemy.
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from api.db.database import Base


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    direction = Column(String(5), nullable=False)       # long / short
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    quantity = Column(Float)
    risk_usdt = Column(Float)
    pnl_usdt = Column(Float)
    pnl_pct = Column(Float)
    fee_usdt = Column(Float)
    balance_after = Column(Float)
    exit_type = Column(String(10))                      # TP / SL / MANUAL
    leverage = Column(Integer, default=1)
    futures = Column(Boolean, default=False)
    ai_probability = Column(Float)
    volume_ratio = Column(Float)
    entry_time = Column(String(50))                     # ISO string del bot
    created_at = Column(DateTime, default=datetime.utcnow)
