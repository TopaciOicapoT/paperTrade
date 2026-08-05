"""
api/routers/levels.py
---------------------
GET /api/levels  — estado actual de niveles mensuales + diagnóstico de filtros.

Para cada símbolo devuelve:
  - Precio, resistencia, soporte y distancias
  - Estado (dentro del rango, rompió soporte/resistencia)
  - Si hay señal: lista de filtros con estado PASS/BLOCK y explicación
  - can_enter: True solo si todos los filtros pasan
"""

import time
import numpy as np
import pandas as pd
from fastapi import APIRouter, Request
from loguru import logger

router = APIRouter(prefix="/api", tags=["levels"])


@router.get("/levels")
def get_levels(request: Request):
    trader = request.app.state.trader
    config = trader.config
    results = []
    # Usar config["symbols"] para reflejar cambios guardados sin reiniciar el bot
    symbols_to_check = config.get("symbols", trader.symbols)

    try:
        from data.fetcher import get_data_exchange, fetch_ohlcv
        from indicators.levels import (
            calcular_niveles_mensuales,
            detectar_triggers_disparandose,
            detectar_failed_retest,
            adaptar_failed_retest,
        )
    except Exception as e:
        return {"error": str(e), "symbols": []}

    exchange = get_data_exchange()
    monthly_lookback = config["levels"]["monthly_lookback"]
    sym_params = config.get("symbol_params", {})

    for symbol in symbols_to_check:
        try:
            entry = fetch_ohlcv(symbol, "1m", limit=500, exchange=exchange)
            daily = fetch_ohlcv(symbol, "1d", limit=monthly_lookback * 30 + 30, exchange=exchange)

            monthly = calcular_niveles_mensuales(daily, lookback_months=monthly_lookback)

            price = float(entry["close"].iloc[-1])
            dist_res = (monthly.resistance - price) / price * 100   # + = por encima del precio
            dist_sup = (price - monthly.support) / price * 100       # + = precio sobre soporte, - = roto

            if monthly.broke_resistance:
                status = "broke_resistance"
                signal = "long"
            elif monthly.broke_support:
                status = "broke_support"
                signal = "short"
            elif monthly.near_resistance:
                status = "near_resistance"
                signal = None
            elif monthly.near_support:
                status = "near_support"
                signal = None
            else:
                status = "inside"
                signal = None

            filters = []
            can_enter = False

            if signal:
                sp = sym_params.get(symbol, {})
                vol_min = sp.get("volume_trigger_ratio", config["levels"]["volume_trigger_ratio"])
                vol_max = sp.get("volume_trigger_ratio_max", config["levels"].get("volume_trigger_ratio_max", 3.0))

                # ── Futuros: SHORTs solo con leverage ──────────────────────────────
                if signal == "short" and not trader.futures_enabled:
                    filters.append({
                        "name": "Modo futuros",
                        "passed": False,
                        "reason": "Los SHORTs requieren futuros activos — en modo spot solo se opera LONG.",
                    })
                    results.append(_build(symbol, price, monthly, dist_res, dist_sup, status, signal, filters, False))
                    continue

                # ── F0: Volumen spike ───────────────────────────────────────────────
                hay_spike, vol_ratio = detectar_triggers_disparandose(
                    entry, volume_ratio_min=vol_min, volume_ratio_max=vol_max
                )
                if hay_spike:
                    filters.append({"name": "Volumen spike", "passed": True,
                                    "reason": f"Vol ratio {vol_ratio:.2f}× dentro del rango [{vol_min}×, {vol_max}×]"})
                else:
                    last_vol_ratio = float(
                        (entry["volume"].iloc[-1] * entry["close"].iloc[-1]) /
                        (entry["volume"] * entry["close"]).iloc[-50:].mean()
                    ) if len(entry) >= 50 else 0.0
                    if last_vol_ratio < vol_min:
                        filters.append({"name": "Volumen spike", "passed": False,
                                        "reason": f"Vol ratio actual {last_vol_ratio:.2f}× < mínimo requerido {vol_min}×. Falta impulso de otros traders."})
                    else:
                        filters.append({"name": "Volumen spike", "passed": False,
                                        "reason": f"Vol ratio {last_vol_ratio:.2f}× > máximo {vol_max}× (trampa de liquidez)."})

                # ── F1: Sesión UTC ──────────────────────────────────────────────────
                session_block = sp.get("session_block_hours")
                now_hour = pd.Timestamp.now(tz="UTC").hour
                if session_block:
                    lo_h, hi_h = session_block
                    if lo_h <= now_hour < hi_h:
                        filters.append({"name": "Sesión UTC", "passed": False,
                                        "reason": f"Hora actual {now_hour}h UTC está en la franja bloqueada {lo_h}-{hi_h}h. WR históricamente bajo en este horario para {symbol}."})
                    else:
                        filters.append({"name": "Sesión UTC", "passed": True,
                                        "reason": f"Hora {now_hour}h UTC fuera de la franja bloqueada {lo_h}-{hi_h}h."})
                else:
                    filters.append({"name": "Sesión UTC", "passed": True,
                                    "reason": "Sin restricción de sesión para este símbolo."})

                # ── F2: Volumen USDT normalizado (F3) ───────────────────────────────
                usdt_block = sp.get("usdt_norm_block_range")
                if usdt_block and len(entry) >= 50:
                    lo_v, hi_v = usdt_block
                    last_usdt = float(entry["volume"].iloc[-1]) * float(entry["close"].iloc[-1])
                    mean_usdt = (entry["volume"] * entry["close"]).iloc[-50:].mean()
                    norm = last_usdt / mean_usdt if mean_usdt > 0 else 1.0
                    if lo_v <= norm <= hi_v:
                        filters.append({"name": "Vol USDT normalizado (F3)", "passed": False,
                                        "reason": f"Vol normalizado {norm:.2f}× en zona trampa Q3 [{lo_v}×, {hi_v}×]. Esta zona tiene WR históricamente bajo."})
                    else:
                        filters.append({"name": "Vol USDT normalizado (F3)", "passed": True,
                                        "reason": f"Vol normalizado {norm:.2f}× fuera de la zona trampa [{lo_v}×, {hi_v}×]."})
                else:
                    filters.append({"name": "Vol USDT normalizado (F3)", "passed": True,
                                    "reason": "Sin restricción F3 para este símbolo."})

                # ── F3: Momentum 5 velas (F1) ───────────────────────────────────────
                mom_block = sp.get("momentum_q3_block")
                if mom_block and len(entry) >= 6:
                    lo_m, hi_m = mom_block
                    prev5 = float(entry["close"].iloc[-6])
                    curr  = float(entry["close"].iloc[-1])
                    mom5  = (curr - prev5) / prev5 * 100
                    if lo_m <= mom5 <= hi_m:
                        filters.append({"name": "Momentum 5v (F1)", "passed": False,
                                        "reason": f"Momentum {mom5:.2f}% en zona trampa Q3 [{lo_m}%, {hi_m}%]. Rango de momentum con peor WR histórico."})
                    else:
                        filters.append({"name": "Momentum 5v (F1)", "passed": True,
                                        "reason": f"Momentum {mom5:.2f}% fuera de zona trampa [{lo_m}%, {hi_m}%]."})
                else:
                    filters.append({"name": "Momentum 5v (F1)", "passed": True,
                                    "reason": "Sin restricción F1 para este símbolo."})

                # ── F4: RSI14 sobrecompra ───────────────────────────────────────────
                rsi_block = sp.get("rsi_overbought_block")
                if rsi_block and len(entry) >= 14:
                    closes = entry["close"].iloc[-14:].astype(float).values
                    deltas = np.diff(closes)
                    avg_gain = np.where(deltas > 0, deltas, 0.0).mean()
                    avg_loss = np.where(deltas < 0, -deltas, 0.0).mean()
                    rsi14 = (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
                    if rsi14 >= rsi_block:
                        filters.append({"name": "RSI14 sobrecompra (F4)", "passed": False,
                                        "reason": f"RSI14 {rsi14:.1f} ≥ umbral {rsi_block}. Entradas con RSI alto tienen WR 25.9% (vs 34% sin este filtro)."})
                    else:
                        filters.append({"name": "RSI14 sobrecompra (F4)", "passed": True,
                                        "reason": f"RSI14 {rsi14:.1f} < umbral {rsi_block}."})
                else:
                    filters.append({"name": "RSI14 sobrecompra (F4)", "passed": True,
                                    "reason": "Sin restricción F4 para este símbolo."})

                # ── Failed retest ───────────────────────────────────────────────────
                monthly_level = monthly.resistance if signal == "long" else monthly.support
                try:
                    use_fr, fr_pct = adaptar_failed_retest(entry, monthly_level, signal, lookback=500)
                    if use_fr:
                        passed_fr = detectar_failed_retest(entry, monthly_level, signal, lookback=60, min_bounce_pct=fr_pct)
                        if passed_fr:
                            filters.append({"name": "Failed retest", "passed": True,
                                            "reason": f"Régimen fakeout-prone detectado. Confirmado: el rebote fue ≥{fr_pct}% — rotura válida."})
                        else:
                            filters.append({"name": "Failed retest", "passed": False,
                                            "reason": f"Régimen fakeout-prone: el precio todavía no ha rebotado {fr_pct}% desde el nivel para confirmar rotura real vs fakeout."})
                    else:
                        filters.append({"name": "Failed retest", "passed": True,
                                        "reason": "Régimen clean-breaker: historial reciente sin fakeouts — se entra directamente en la rotura."})
                except Exception:
                    filters.append({"name": "Failed retest", "passed": True, "reason": "Auto-régimen no disponible."})

                # ── News circuit breaker ─────────────────────────────────────────────
                news_paused = time.time() < trader._news_paused_until
                if news_paused:
                    remaining = (trader._news_paused_until - time.time()) / 3600
                    filters.append({"name": "News circuit breaker", "passed": False,
                                    "reason": f"Pausa activa por evento macro — {remaining:.1f}h restantes."})
                else:
                    filters.append({"name": "News circuit breaker", "passed": True,
                                    "reason": "Sin eventos macro activos."})

                # ── Posición ya abierta en este símbolo ──────────────────────────────
                if symbol in trader.open_orders:
                    filters.append({"name": "Posición existente", "passed": False,
                                    "reason": f"Ya hay una posición abierta en {symbol}."})
                else:
                    filters.append({"name": "Posición existente", "passed": True,
                                    "reason": "Sin posición abierta — slot disponible."})

                # ── Límite de posiciones simultáneas ────────────────────────────────
                max_pos = config["risk"]["max_open_positions"]
                open_count = len(trader.open_orders)
                if open_count >= max_pos:
                    filters.append({"name": "Límite de posiciones", "passed": False,
                                    "reason": f"{open_count}/{max_pos} posiciones abiertas — cartera llena."})
                else:
                    filters.append({"name": "Límite de posiciones", "passed": True,
                                    "reason": f"{open_count}/{max_pos} posiciones abiertas — hay capacidad."})

                can_enter = all(f["passed"] for f in filters)

            results.append(_build(symbol, price, monthly, dist_res, dist_sup, status, signal, filters, can_enter))

        except Exception as e:
            logger.error(f"[Levels] Error en {symbol}: {e}")
            results.append({"symbol": symbol, "error": str(e)})

    return {"symbols": results}


def _build(symbol, price, monthly, dist_res, dist_sup, status, signal, filters, can_enter):
    blocking = [f["name"] for f in filters if not f["passed"]]
    return {
        "symbol":         symbol,
        "price":          round(price, 6),
        "resistance":     round(monthly.resistance, 6),
        "support":        round(monthly.support, 6),
        "dist_res_pct":   round(dist_res, 2),
        "dist_sup_pct":   round(dist_sup, 2),
        "status":         status,
        "signal":         signal,
        "filters":        filters,
        "can_enter":      can_enter,
        "blocking":       blocking,
    }
