"""
api/routers/events.py
---------------------
GET /api/events  — log de señales detectadas por el bot (trades tomados + rechazados).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.db.database import get_db
from api.db.models import SignalEvent

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
def get_events(
    db: Session = Depends(get_db),
    limit: int = Query(default=200, le=500),
    symbol: str | None = None,
    event_type: str | None = None,
):
    """
    Devuelve el log de señales detectadas por el bot.
    Incluye trades abiertos y señales rechazadas (capacity, filtros, IA, riesgo).
    """
    q = db.query(SignalEvent).order_by(SignalEvent.timestamp.desc())
    if symbol:
        q = q.filter(SignalEvent.symbol == symbol)
    if event_type:
        q = q.filter(SignalEvent.event_type == event_type)
    rows = q.limit(limit).all()

    return [
        {
            "id":               r.id,
            "timestamp":        r.timestamp.isoformat() if r.timestamp else None,
            "symbol":           r.symbol,
            "strategy":         r.strategy,
            "event_type":       r.event_type,
            "direction":        r.direction,
            "price":            r.price,
            "level_name":       r.level_name,
            "volume_ratio":     r.volume_ratio,
            "rejection_reason": r.rejection_reason,
            "details":          r.details,
        }
        for r in rows
    ]
