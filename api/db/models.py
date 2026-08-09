"""
api/db/models.py
----------------
Modelos ORM SQLAlchemy.
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
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


class SignalEvent(Base):
    """Log de todas las señales detectadas por el bot (trades tomados y rechazados)."""
    __tablename__ = "signal_events"

    id            = Column(Integer, primary_key=True, index=True)
    timestamp     = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    symbol        = Column(String(20), nullable=False, index=True)
    strategy      = Column(String(20), nullable=False)
    event_type    = Column(String(30), nullable=False, index=True)
    direction     = Column(String(10))
    price         = Column(Float)
    level_name    = Column(String(50))
    volume_ratio  = Column(Float)
    rejection_reason = Column(String(200))
    details       = Column(Text)


class SavedSimulation(Base):
    """Simulación del Laboratorio guardada por el usuario."""
    __tablename__ = "saved_simulations"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    params_json = Column(Text)   # JSON con símbolos, capital, años, etc.
    result_json = Column(Text)   # JSON completo del resultado (sin filtros + con filtros)
    volume_ratio  = Column(Float)
    rejection_reason = Column(String(200))
    details       = Column(Text)                         # JSON extra (SL, TP, RSI, etc.)
