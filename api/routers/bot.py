"""
api/routers/bot.py
------------------
Endpoints de control del bot y WebSocket de estado en tiempo real.

GET  /api/state         → snapshot completo del bot
POST /api/emergency     → cierra todo y detiene el trading
POST /api/resume        → reactiva el trading tras un halt
GET  /ws                → WebSocket — push de estado cada 5 segundos
"""

import asyncio
import json
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from api.schemas import BotSnapshot, CloseResult, MessageResponse

router = APIRouter(prefix="/api", tags=["bot"])

# Manager de conexiones WebSocket activas
class _ConnectionManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, payload: dict):
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.remove(ws)

    @property
    def count(self) -> int:
        return len(self._clients)


manager = _ConnectionManager()


def _get_trader(request: Request):
    return request.app.state.trader


@router.get("/state", response_model=BotSnapshot)
def get_state(request: Request):
    return _get_trader(request).get_snapshot()


@router.post("/emergency", response_model=MessageResponse)
def emergency_stop(request: Request):
    trader = _get_trader(request)
    closed = trader.cerrar_todo_y_parar()
    symbols = [c["symbol"] for c in closed]
    return MessageResponse(
        message=f"EMERGENCY STOP ejecutado. {len(closed)} posición(es) cerrada(s). Trading detenido.",
        detail={"closed_symbols": symbols},
    )


@router.post("/resume", response_model=MessageResponse)
def resume_trading(request: Request):
    trader = _get_trader(request)
    trader.reanudar()
    return MessageResponse(message="Trading reanudado.")


@router.get("/ws-clients")
def ws_clients():
    return {"connected": manager.count}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, request: Request):
    await manager.connect(websocket)
    trader = websocket.app.state.trader
    try:
        while True:
            snapshot = trader.get_snapshot()
            await websocket.send_json(snapshot)
            # Escuchar mensajes del cliente o esperar 5s
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
