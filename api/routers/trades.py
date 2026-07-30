"""
api/routers/trades.py
---------------------
Endpoints de trades y posiciones.

GET    /api/positions                 → posiciones abiertas actuales
POST   /api/positions/{symbol}/close  → cierre manual de un símbolo
GET    /api/history                   → historial de trades cerrados (DB o memoria)
GET    /api/history/stats             → estadísticas agregadas
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from api.db.database import get_db

DB = Annotated[Session, Depends(get_db)]
from api.db.models import Trade
from api.schemas import CloseResult, MessageResponse, OpenOrder

router = APIRouter(prefix="/api", tags=["trades"])


def _get_trader(request: Request):
    return request.app.state.trader


@router.get("/positions")
def get_positions(request: Request):
    trader = _get_trader(request)
    return {"positions": trader.open_orders}


@router.post(
    "/positions/{symbol}/close",
    response_model=CloseResult,
    responses={404: {"description": "Posición no encontrada"}, 503: {"description": "Sin precio de mercado"}},
)
def close_position(symbol: str, request: Request, db: DB):
    trader = _get_trader(request)
    # Normalizar el símbolo (el frontend puede enviar ADA-USDT o ADA_USDT)
    symbol = symbol.replace("-", "/").replace("_", "/").upper()

    if symbol not in trader.open_orders:
        raise HTTPException(status_code=404, detail=f"No hay posición abierta para {symbol}")

    result = trader.cerrar_posicion_manual(symbol)
    if result is None:
        raise HTTPException(status_code=503, detail="No se pudo obtener precio de mercado")

    # Persistir en DB
    _persist_trade(db, result)

    return CloseResult(
        symbol=result["symbol"],
        exit_price=result["exit_price"],
        exit_type=result["exit_type"],
        pnl_usdt=result["pnl_usdt"],
        pnl_pct=result["pnl_pct"],
        balance_after=result["balance_after"],
    )


@router.get("/history")
def get_history(
    request: Request,
    db: DB,
    limit: int = 100,
    offset: int = 0,
):
    """Devuelve el historial de trades. Prioriza DB si tiene datos, si no usa memoria."""
    db_count = db.query(Trade).count()

    if db_count > 0:
        rows = db.query(Trade).order_by(Trade.id.desc()).offset(offset).limit(limit).all()
        return {
            "source": "db",
            "total": db_count,
            "trades": [_trade_to_dict(t) for t in rows],
        }

    # Fallback: trade_log en memoria (orden inverso → más recientes primero)
    trader = _get_trader(request)
    log = list(reversed(trader.trade_log))
    total = len(log)
    return {
        "source": "memory",
        "total": total,
        "trades": log[offset : offset + limit],
    }


@router.get("/history/stats")
def get_stats(request: Request, db: DB):
    """Estadísticas rápidas: WR, PF, PnL total, mejor/peor trade."""
    trader = _get_trader(request)
    log = trader.trade_log

    if not log:
        return {"message": "Sin trades aún"}

    wins   = [t for t in log if t.get("exit_type") == "TP"]
    losses = [t for t in log if t.get("exit_type") == "SL"]
    manual = [t for t in log if t.get("exit_type") == "MANUAL"]
    total  = len(log)

    gross_win  = sum(t.get("pnl_usdt", 0) for t in wins)
    gross_loss = abs(sum(t.get("pnl_usdt", 0) for t in losses + manual if t.get("pnl_usdt", 0) < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else None

    all_pnl = [t.get("pnl_usdt", 0) for t in log]
    return {
        "total_trades":  total,
        "wins":          len(wins),
        "losses":        len(losses),
        "manual_closes": len(manual),
        "win_rate_pct":  round(len(wins) / total * 100, 1) if total else 0,
        "profit_factor": round(pf, 2) if pf else None,
        "total_pnl_usdt": round(sum(all_pnl), 2),
        "best_trade_usdt":  round(max(all_pnl), 2) if all_pnl else 0,
        "worst_trade_usdt": round(min(all_pnl), 2) if all_pnl else 0,
        "avg_pnl_usdt":     round(sum(all_pnl) / total, 2) if total else 0,
    }


# ── helpers ──────────────────────────────────────────────────────────────────

def _persist_trade(db: Session, trade: dict):
    row = Trade(
        symbol=trade.get("symbol"),
        direction=trade.get("direction"),
        entry_price=trade.get("entry_price"),
        exit_price=trade.get("exit_price"),
        stop_loss=trade.get("stop_loss"),
        take_profit=trade.get("take_profit"),
        quantity=trade.get("quantity"),
        risk_usdt=trade.get("risk_usdt"),
        pnl_usdt=trade.get("pnl_usdt"),
        pnl_pct=trade.get("pnl_pct"),
        fee_usdt=trade.get("fee_usdt"),
        balance_after=trade.get("balance_after"),
        exit_type=trade.get("exit_type"),
        leverage=trade.get("leverage"),
        futures=trade.get("futures"),
        ai_probability=trade.get("ai_probability"),
        volume_ratio=trade.get("volume_ratio"),
        entry_time=str(trade.get("entry_time", "")),
    )
    db.add(row)
    db.commit()


def _trade_to_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "direction": t.direction,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "exit_type": t.exit_type,
        "pnl_usdt": t.pnl_usdt,
        "pnl_pct": t.pnl_pct,
        "balance_after": t.balance_after,
        "leverage": t.leverage,
        "ai_probability": t.ai_probability,
        "entry_time": t.entry_time,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }
