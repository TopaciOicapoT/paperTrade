"""
backtesting/engine.py
---------------------
Motor de backtesting para la estrategia de breakout de niveles clave.
Simula operaciones históricas para evaluar la estrategia ANTES de usar
dinero real o el paper trader.

Uso básico:
    python -m backtesting.engine --symbol BTC/USDT --days 60
"""

import argparse
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from loguru import logger
from pathlib import Path

from data.fetcher import get_data_exchange, fetch_ohlcv, fetch_multi_timeframe
from indicators.levels import calcular_niveles_mensuales, detectar_triggers_disparandose, evaluar_bounce, detectar_failed_retest, adaptar_failed_retest

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_index: int
    exit_index: int = -1
    exit_price: float = 0.0
    result: str = "open"        # "win", "loss", "open"
    pnl_pct: float = 0.0
    level_name: str = ""
    # ── Metadatos para análisis de fallos ──
    volume_ratio: float = 0.0         # Spike de volumen en el momento de entrada
    level_distance_pct: float = 0.0   # Distancia entry → nivel mensual (%)
    bars_to_exit: int = 0             # Velas hasta resolver el trade


@dataclass
class BacktestResult:
    symbol: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)  # Normalizada (empieza en 1.0)
    initial_capital: float = 1000.0


def simular_trades(
    df_entry: pd.DataFrame,
    df_daily: pd.DataFrame,
    df_weekly: pd.DataFrame | None,
    symbol: str,
    monthly_lookback: int = 6,
    tp_pct: float = 3.0,
    sl_behind_pct: float = 1.0,
    volume_ratio_min: float = 2.0,
    volume_ratio_max: float = 3.0,
    fee_pct: float = 0.1,
    ml_threshold: float = 0.0,
    volatility_filter: bool = False,
    volatility_sigma: float = 3.0,
    volatility_window: int = 20,
    leverage: int = 1,
    initial_capital: float = 1000.0,
    failed_retest_filter: bool = True,
    symbol_params: dict | None = None,
    rsi_overbought_block: float | None = None,
    trend_filter: bool = False,
    _adx_min: float = 0,
    _daily_vol_min: float = 0.0,
    _crypto_trend: bool = False,
    _crypto_slope_window: int = 7,
    _crypto_min_slope: float = 1.10,
    _crypto_min_absolute: float = 25.0,
    _breakout_cooldown: int = 0,    # cooldown en barras post-SL (0 = desactivado)
) -> BacktestResult:
    """
    Simula la estrategia de breakout mensual sobre datos históricos.

    Args:
        ml_threshold:       Si > 0, carga el modelo XGBoost y solo toma trades
                            donde la probabilidad de éxito supera este umbral.
        volatility_filter:  Si True, simula el comportamiento del news circuit breaker
                            usando volatilidad histórica como proxy de noticias de impacto.
                            Un día con rango diario > volatility_sigma σ se marca como
                            "evento de noticias" y se bloquean nuevas entradas ese día.
                            Justificación: las noticias macro de impacto siempre generan
                            un spike de volatilidad anormal — es el mismo efecto que
                            detectaría el news circuit breaker en tiempo real.
        volatility_sigma:   Número de desviaciones estándar para considerar un día anormal.
        volatility_window:  Ventana de días para calcular la media/std de volatilidad.
    """
    # Silenciar DEBUG de indicators.levels durante el bucle principal
    logger.disable("indicators.levels")

    # ── Cargar modelo ML si se solicita ──
    predictor = None
    if ml_threshold > 0:
        try:
            from models.predictor import Predictor
            predictor = Predictor(threshold=ml_threshold)
            if predictor.model_disponible():
                logger.info(f"Filtro ML activo | umbral: {ml_threshold}")
            else:
                logger.warning("Modelo ML no encontrado — backtest sin filtro IA")
                predictor = None
        except Exception as e:
            logger.warning(f"No se pudo cargar el modelo ML: {e}")
            predictor = None

    result = BacktestResult(symbol=symbol, initial_capital=initial_capital)
    equity_curve = [1.0]
    open_trade: Trade | None = None
    lookback_days = monthly_lookback * 30

    # ── Parámetros efectivos: globales + overrides del símbolo ──
    sym_ov = (symbol_params or {}).get(symbol, {})
    # failed_retest_filter puede ser: "auto" | True | False
    # El valor del parámetro de la función (failed_retest_filter) es bool y actúa como
    # override de CLI (--no-failed-retest / --compare). Lo convertimos a "auto" si es True.
    fr_cli = failed_retest_filter      # bool que viene del CLI
    fr_setting = sym_ov.get(
        "failed_retest_filter",
        "auto" if fr_cli else False,   # False si se pasó --no-failed-retest
    )
    eff_bounce_pct      = sym_ov.get("failed_retest_min_bounce_pct", 0.3)
    eff_retest_lookback = sym_ov.get("failed_retest_lookback", 60)
    eff_auto_lookback   = sym_ov.get("failed_retest_auto_lookback", 500)
    eff_vol_min         = sym_ov.get("volume_trigger_ratio", volume_ratio_min)
    eff_vol_max         = sym_ov.get("volume_trigger_ratio_max", volume_ratio_max)
    if sym_ov:
        logger.info(
            f"{symbol} | Params personalizados: "
            f"failed_retest={fr_setting}, "
            f"bounce_pct={eff_bounce_pct}%, "
            f"vol={eff_vol_min}-{eff_vol_max}×"
        )

    # ── Pre-calcular niveles por día ──
    # NOTA: el nivel del día D se asigna al día D+1 para evitar look-ahead bias
    # (el cierre EOD de D solo se conoce al final de D, no durante las velas de D)
    daily_levels_cache: dict = {}
    for idx in range(lookback_days, len(df_daily) - 1):
        daily_window = df_daily.iloc[idx - lookback_days: idx + 1]
        try:
            lvl = calcular_niveles_mensuales(daily_window, lookback_months=monthly_lookback)
            next_date = df_daily.index[idx + 1].date()   # ← aplicar al día siguiente
            daily_levels_cache[next_date] = lvl
        except Exception:
            pass

    logger.enable("indicators.levels")
    logger.info(f"Niveles pre-calculados para {len(daily_levels_cache)} días")

    # ── Pre-calcular tendencia semanal (SMA50 vs SMA200) ──
    trend_series: pd.Series | None = None
    if trend_filter and df_weekly is not None and len(df_weekly) >= 50:
        sma50  = df_weekly["close"].rolling(50).mean()
        sma200 = df_weekly["close"].rolling(200).mean()
        trend_series = (sma50 > sma200)
        n_bull = trend_series.sum()
        logger.info(
            f"[TrendFilter] SMA50w > SMA200w en {n_bull}/{len(trend_series)} semanas "
            f"({n_bull/len(trend_series)*100:.0f}% alcista)"
        )

    # ── Pre-calcular ADX estándar, filtro crypto y ratio de volumen diario ──
    adx_min       = sym_ov.get("adx_min", _adx_min)
    daily_vol_min = sym_ov.get("daily_vol_min_ratio", _daily_vol_min)
    adx_series_daily: pd.Series | None = None
    daily_vol_ratio: pd.Series | None = None
    crypto_trend_ok: pd.Series | None = None
    if adx_min > 0 and df_daily is not None and len(df_daily) >= 30:
        from indicators.technical import calcular_adx_series
        adx_series_daily = calcular_adx_series(df_daily)
    if _crypto_trend and df_daily is not None and len(df_daily) >= 35:
        from indicators.technical import calcular_crypto_trend_series
        crypto_trend_ok = calcular_crypto_trend_series(
            df_daily,
            slope_window=_crypto_slope_window,
            min_slope=_crypto_min_slope,
            min_absolute=_crypto_min_absolute,
        )
        logger.info(f"[CryptoTrend] Pre-calculado | slope_w={_crypto_slope_window} min_slope={_crypto_min_slope} min_abs={_crypto_min_absolute}")
    if daily_vol_min > 0 and df_daily is not None and len(df_daily) >= 21:
        daily_vol_ratio = df_daily["volume"] / df_daily["volume"].rolling(20).mean().shift(1)

    # ── Pre-calcular días de alta volatilidad (proxy de noticias) ──
    # Un día es "caliente" si su rango (high-low)/low supera la media + N*std
    # de los últimos `volatility_window` días. En esos días el news circuit breaker
    # habría pausado las nuevas entradas en tiempo real.
    hot_dates: set = set()
    if volatility_filter:
        daily_ranges = (df_daily["high"] - df_daily["low"]) / df_daily["low"] * 100
        rolling_mean = daily_ranges.rolling(volatility_window).mean()
        rolling_std  = daily_ranges.rolling(volatility_window).std()
        threshold_series = rolling_mean + volatility_sigma * rolling_std
        hot_mask = daily_ranges > threshold_series
        hot_dates = {ts.date() for ts in df_daily.index[hot_mask]}
        logger.info(
            f"[VolFilter] {len(hot_dates)} días de alta volatilidad detectados "
            f"(>{volatility_sigma}σ) — se saltarán nuevas entradas esos días"
        )

    # ── Bucle principal por vela de entrada ──
    auto_regime_cache: dict = {}
    # Cooldown por nivel: evita re-entrar en el mismo nivel tras un SL
    # Clave: f"{direction}_{level_rounded}" → índice de la última pérdida
    _loss_cooldown_bars = sym_ov.get("breakout_loss_cooldown_bars", _breakout_cooldown)
    _level_loss_bar: dict[str, int] = {}

    for i in range(50, len(df_entry) - 1):
        window_entry = df_entry.iloc[max(0, i - 200): i + 1]
        current = df_entry.iloc[i]
        trade_just_closed = False

        # ── Verificar si trade abierto tocó SL o TP ──
        if open_trade is not None:
            high = float(current["high"])
            low = float(current["low"])

            hit_tp = hit_sl = False
            if open_trade.direction == "long":
                hit_tp = high >= open_trade.take_profit
                hit_sl = low <= open_trade.stop_loss
            else:
                hit_tp = low <= open_trade.take_profit
                hit_sl = high >= open_trade.stop_loss

            if hit_tp or hit_sl:
                open_trade.result = "win" if hit_tp else "loss"
                open_trade.exit_price = open_trade.take_profit if hit_tp else open_trade.stop_loss
                open_trade.exit_index = i

                pnl = (open_trade.exit_price - open_trade.entry_price) / open_trade.entry_price
                if open_trade.direction == "short":
                    pnl = -pnl
                pnl *= leverage
                pnl -= (fee_pct / 100) * 2
                open_trade.pnl_pct = round(pnl * 100, 4)
                open_trade.bars_to_exit = i - open_trade.entry_index

                # Registrar cooldown si fue un SL
                if open_trade.result == "loss" and _loss_cooldown_bars > 0:
                    _loss_key = f"{open_trade.direction}_{round(open_trade.stop_loss, 4)}"
                    _level_loss_bar[_loss_key] = i

                result.trades.append(open_trade)
                equity_curve.append(equity_curve[-1] * (1 + pnl))
                open_trade = None
                trade_just_closed = True

        if open_trade is not None or trade_just_closed:
            continue

        # ── Filtro de volatilidad (proxy de noticias) ──
        current_date = df_entry.index[i].date()
        if hot_dates and current_date in hot_dates:
            continue   # Simula pausa del news circuit breaker

        # ── Obtener niveles mensuales del día (desde caché) ──
        monthly = daily_levels_cache.get(current_date)
        if monthly is None:
            continue

        # ¿El precio rompió el nivel mensual?
        if not (monthly.broke_resistance or monthly.broke_support):
            continue

        direction = "long" if monthly.broke_resistance else "short"

        # ── Filtro de tendencia semanal (SMA50w vs SMA200w) ──
        if trend_series is not None:
            trend_val = trend_series.asof(df_entry.index[i])
            if pd.isna(trend_val):
                continue
            if direction == "long"  and not trend_val:
                continue
            if direction == "short" and trend_val:
                continue

        if direction == "short" and leverage == 1:
            continue

        monthly_level = monthly.resistance if direction == "long" else monthly.support

        # ── Cooldown post-SL: no re-entrar en el mismo nivel tras una pérdida ──
        if _loss_cooldown_bars > 0:
            _loss_key = f"{direction}_{round(monthly_level, 4)}"
            if i - _level_loss_bar.get(_loss_key, 0) < _loss_cooldown_bars:
                continue
        if adx_series_daily is not None and adx_min > 0:
            adx_val = adx_series_daily.asof(df_entry.index[i])
            if pd.isna(adx_val) or float(adx_val) < adx_min:
                continue

        # ── Filtro de tendencia cripto ────────────────────────────────────
        if crypto_trend_ok is not None:
            trend_val = crypto_trend_ok.asof(df_entry.index[i])
            if not bool(trend_val):
                continue

        # ── Filtro volumen diario — mercado dormido ───────────────────────────
        if daily_vol_ratio is not None and daily_vol_min > 0:
            dvr = daily_vol_ratio.asof(df_entry.index[i])
            if not pd.isna(dvr) and float(dvr) < daily_vol_min:
                continue

        # Failed retest — tres modos: "auto" | True | False
        if fr_setting is False:
            pass  # clean breaker explícito — no filtrar
        else:
            if fr_setting is True:
                use_fr, fr_pct = True, eff_bounce_pct
            else:  # "auto" — calibrar según comportamiento reciente (cache por día)
                regime_key = (round(monthly_level, 8), direction, current_date)
                if regime_key not in auto_regime_cache:
                    use_fr, fr_pct = adaptar_failed_retest(
                        window_entry, monthly_level, direction,
                        lookback=eff_auto_lookback,
                    )
                    auto_regime_cache[regime_key] = (use_fr, fr_pct)
                    logger.info(
                        f"{symbol} | Auto-régimen [{current_date}] nivel={monthly_level:.4f} "
                        f"→ {'FILTRO ON' if use_fr else 'CLEAN BREAK'} umbral={fr_pct}%"
                    )
                else:
                    use_fr, fr_pct = auto_regime_cache[regime_key]
            if use_fr and not detectar_failed_retest(
                window_entry, monthly_level, direction,
                lookback=eff_retest_lookback,
                min_bounce_pct=fr_pct,
            ):
                continue

        # Confirmar spike de volumen (triggers de otros traders)
        hay_spike, volume_ratio = detectar_triggers_disparandose(
            window_entry, volume_ratio_min=eff_vol_min,
            volume_ratio_max=eff_vol_max,
        )
        if not hay_spike:
            continue

        # ── Filtro de sesión (hora UTC) ─────────────────────────────────────────
        # Valido en validate_filters.py: LINK Europa(08-14h) +47.9%; EGLD AM+Noche +34.3%
        session_block = sym_ov.get("session_block_hours")
        if session_block:
            lo_h, hi_h = session_block
            current_hour = df_entry.index[i].hour
            if lo_h <= current_hour < hi_h:
                continue

        # ── Filtro de volumen USDT normalizado (zona trampa Q3) ──────────────
        # Valido en validate_filters.py: ADA +16.4%, EGLD +20.1%
        # La zona 2.1-2.7× la media es la de peor WR (fakeout con volumen medio-alto)
        usdt_block = sym_ov.get("usdt_norm_block_range")
        if usdt_block:
            lo_v, hi_v = usdt_block
            usdt_vol  = float(current["volume"]) * float(current["close"])
            # Usar las últimas 50 velas como referencia (= 250min en 5m) — igual que el live bot
            usdt_mean = (window_entry["volume"] * window_entry["close"]).iloc[-50:].mean()
            usdt_norm_val = usdt_vol / usdt_mean if usdt_mean > 0 else 1.0
            if lo_v <= usdt_norm_val <= hi_v:
                continue

        # ── Filtro de momentum 5 velas (zona trampa Q3) ──────────────────────
        # Valido en validate_filters.py: SOLO ADA +29.1% (LINK/EGLD empeoran con este filtro)
        mom_block = sym_ov.get("momentum_q3_block")
        if mom_block and i >= 5:
            lo_m, hi_m = mom_block
            prev5 = float(df_entry.iloc[i - 5]["close"])
            momentum_5 = (float(current["close"]) - prev5) / prev5 * 100
            if lo_m <= momentum_5 <= hi_m:
                continue

        # ── Filtro RSI14 sobrecompra (analyze_features_v2.py: RSI>70 → +30.6% equity) ──
        # RSI>70 global: WR 25.9% vs 34.2% sin esos trades; eliminar mejora equity +30.6%
        eff_rsi_block = sym_ov.get("rsi_overbought_block", rsi_overbought_block)
        if eff_rsi_block is not None and i >= 14:
            closes14 = df_entry["close"].iloc[i - 13: i + 1].astype(float).values
            deltas = np.diff(closes14)
            avg_gain = np.where(deltas > 0, deltas, 0.0).mean()
            avg_loss = np.where(deltas < 0, -deltas, 0.0).mean()
            rsi14 = (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
            if rsi14 >= eff_rsi_block:
                continue

        # ── Filtro ML (si está activo) ──
        if predictor is not None:
            autorizado, prob = predictor.autorizar_entrada(window_entry)
            if not autorizado:
                continue

        # Calcular SL y TP — ambos anclados al precio de ENTRADA
        entry_price = float(current["close"])
        # (no al nivel mensual, que puede estar lejos y generar SL de 10-15%)
        sl_dist = entry_price * (sl_behind_pct / 100)
        tp_dist = entry_price * (tp_pct / 100)

        if direction == "long":
            sl = entry_price - sl_dist
            tp = entry_price + tp_dist
        else:
            sl = entry_price + sl_dist
            tp = entry_price - tp_dist

        open_trade = Trade(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=tp,
            entry_index=i,
            level_name=f"monthly_{'resistance' if direction == 'long' else 'support'}",
            volume_ratio=round(volume_ratio, 2),
            level_distance_pct=round(
                abs(entry_price - monthly_level) / monthly_level * 100, 3
            ),
        )
        logger.info(
            f"Trade abierto: {direction.upper()} @ {entry_price:.2f} | "
            f"SL: {sl:.2f} | TP: {tp:.2f} | Vol ratio: {volume_ratio:.2f}x"
        )

    # ── Calcular métricas finales ──
    closed = [t for t in result.trades if t.result != "open"]
    wins = [t for t in closed if t.result == "win"]
    losses = [t for t in closed if t.result == "loss"]

    result.total_trades = len(closed)
    result.wins = len(wins)
    result.losses = len(losses)
    result.win_rate = len(wins) / len(closed) if closed else 0

    win_pnls = [t.pnl_pct for t in wins]
    loss_pnls = [abs(t.pnl_pct) for t in losses]

    result.avg_win_pct = float(np.mean(win_pnls)) if win_pnls else 0
    result.avg_loss_pct = float(np.mean(loss_pnls)) if loss_pnls else 0

    total_wins = sum(win_pnls)
    total_losses = sum(loss_pnls)
    result.profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

    result.total_return_pct = round((equity_curve[-1] - 1) * 100, 2)

    # Max drawdown
    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    result.max_drawdown_pct = round(float(drawdown.min()) * 100, 2)

    result.equity_curve = equity_curve
    return result


def imprimir_resumen(result: BacktestResult, label: str = ""):
    cap = result.initial_capital
    eq  = result.equity_curve
    closed = [t for t in result.trades if t.result != "open"]

    title = f"BACKTEST: {result.symbol}"
    if label:
        title += f"  [{label}]"

    logger.info("=" * 62)
    logger.info(title)
    logger.info("=" * 62)

    # ── Curva de equity trade a trade ──
    if eq and len(eq) > 1 and closed:
        logger.info(
            f"  {'#':>3}  {'Dir':<6}  {'Resultado':<6}  {'PnL%':>7}  "
            f"{'Capital $':>11}  {'Cambio $':>10}"
        )
        logger.info(f"  {'─'*3}  {'─'*6}  {'─'*6}  {'─'*7}  {'─'*11}  {'─'*10}")
        for i, t in enumerate(closed):
            cap_after  = eq[i + 1] * cap
            delta      = (eq[i + 1] - eq[i]) * cap
            icon       = "WIN   " if t.result == "win" else "LOSS  "
            delta_str  = f"+${delta:>8.2f}" if delta >= 0 else f"-${abs(delta):>8.2f}"
            logger.info(
                f"  #{i+1:>3}  {t.direction.upper():<6}  {icon}  "
                f"{t.pnl_pct:>+7.2f}%  ${cap_after:>10.2f}  {delta_str}"
            )
        logger.info(f"  {'─'*62}")

    # ── Resumen numérico ──
    final   = eq[-1] * cap if eq else cap
    net     = final - cap
    net_str = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
    dd_usd  = cap * abs(result.max_drawdown_pct / 100)

    logger.info(f"  Operaciones:    {result.total_trades}  ({result.wins} WIN / {result.losses} LOSS)")
    logger.info(f"  Win rate:       {result.win_rate:.1%}")
    logger.info(f"  Avg W / Avg L:  +{result.avg_win_pct:.2f}% / -{result.avg_loss_pct:.2f}%")
    logger.info(f"  Profit factor:  {result.profit_factor:.2f}")
    logger.info(f"  Capital inicial: ${cap:>9.2f}")
    logger.info(f"  Capital final:   ${final:>9.2f}  ({result.total_return_pct:+.2f}%)")
    logger.info(f"  Ganancia neta:   {net_str}")
    logger.info(f"  Max drawdown:    -{dd_usd:.2f}$  ({result.max_drawdown_pct:.2f}%)")
    logger.info("=" * 62)


def comparar_resultados(sin_filtro: BacktestResult, con_filtro: BacktestResult):
    """Muestra tabla comparativa: sin failed retest vs con failed retest."""
    cap = sin_filtro.initial_capital
    final_sin = sin_filtro.equity_curve[-1] * cap if sin_filtro.equity_curve else cap
    final_con = con_filtro.equity_curve[-1] * cap if con_filtro.equity_curve else cap
    net_sin   = final_sin - cap
    net_con   = final_con - cap

    def fmt_pct(v):  return f"{v:>+8.2f}%"
    def fmt_usd(v):  return f"${v:>9.2f}"
    def fmt_net(v):  return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

    logger.info("")
    logger.info("=" * 66)
    logger.info(f"  COMPARATIVA — {sin_filtro.symbol}  |  Capital inicial: ${cap:.2f}")
    logger.info("=" * 66)
    logger.info(f"  {'Métrica':<28}  {'SIN filtro':>14}  {'CON failed retest':>16}")
    logger.info(f"  {'─'*28}  {'─'*14}  {'─'*16}")
    logger.info(f"  {'Operaciones':<28}  {sin_filtro.total_trades:>14}  {con_filtro.total_trades:>16}")
    logger.info(f"  {'Win rate':<28}  {sin_filtro.win_rate:>13.1%}  {con_filtro.win_rate:>15.1%}")
    logger.info(f"  {'Avg win':<28}  +{sin_filtro.avg_win_pct:>12.2f}%  +{con_filtro.avg_win_pct:>14.2f}%")
    logger.info(f"  {'Avg loss':<28}  -{sin_filtro.avg_loss_pct:>12.2f}%  -{con_filtro.avg_loss_pct:>14.2f}%")
    logger.info(f"  {'Profit factor':<28}  {sin_filtro.profit_factor:>14.2f}  {con_filtro.profit_factor:>16.2f}")
    logger.info(f"  {'Retorno %':<28}  {fmt_pct(sin_filtro.total_return_pct):>14}  {fmt_pct(con_filtro.total_return_pct):>16}")
    logger.info(f"  {'Capital final $':<28}  {fmt_usd(final_sin):>14}  {fmt_usd(final_con):>16}")
    logger.info(f"  {'Ganancia neta $':<28}  {fmt_net(net_sin):>14}  {fmt_net(net_con):>16}")
    logger.info(f"  {'Max drawdown':<28}  {fmt_pct(sin_filtro.max_drawdown_pct):>14}  {fmt_pct(con_filtro.max_drawdown_pct):>16}")
    logger.info(f"  {'─'*66}")

    diff = final_con - final_sin
    if diff > 0:
        logger.success(
            f"  → Failed retest es MÁS rentable: +${diff:.2f} extra "
            f"({diff/cap*100:.1f}% del capital inicial)"
        )
    elif diff < 0:
        logger.warning(
            f"  → Sin filtro es más rentable: +${abs(diff):.2f} extra "
            f"(más trades compensan el WR más bajo)"
        )
    else:
        logger.info("  → Ambas estrategias tienen el mismo resultado.")
    logger.info("=" * 66)



def analizar_fallos(result: BacktestResult) -> pd.DataFrame:
    """
    Analiza los trades cerrados buscando patrones en los fallos.

    Examina tres ejes:
      1. Volumen — ¿los spikes más débiles fallan más?
      2. Distancia al nivel mensual — ¿entradas lejos del nivel son peores?
      3. Dirección — ¿longs o shorts tienen mayor éxito?
      4. Velocidad — ¿los trades lentos acaban en SL?

    Returns:
        DataFrame con una fila por trade y columnas de contexto.
        Imprime el análisis por sección.
    """
    closed = [t for t in result.trades if t.result != "open"]
    if not closed:
        logger.warning("No hay trades cerrados para analizar.")
        return pd.DataFrame()

    df = pd.DataFrame([{
        "result":           t.result,
        "direction":        t.direction,
        "pnl_pct":          t.pnl_pct,
        "volume_ratio":     t.volume_ratio,
        "level_dist_pct":   t.level_distance_pct,
        "bars_to_exit":     t.bars_to_exit,
    } for t in closed])

    sep = "─" * 55
    logger.info(sep)
    logger.info(f"ANÁLISIS DE FALLOS: {result.symbol}  ({len(df)} trades)")
    logger.info(sep)

    # ── 1. Por buckets de volumen ─────────────────────────────
    df["vol_bucket"] = pd.cut(
        df["volume_ratio"],
        bins=[0, 2.0, 2.5, 3.0, 99],
        labels=["1.8-2.0x", "2.0-2.5x", "2.5-3.0x", ">3.0x"],
    )
    vol_stats = (
        df.groupby("vol_bucket", observed=True)
        .agg(trades=("result", "count"),
             wins=("result", lambda x: (x == "win").sum()),
             win_rate=("result", lambda x: (x == "win").mean()))
        .assign(win_rate=lambda d: d["win_rate"].map("{:.0%}".format))
    )
    logger.info("Volumen del spike vs resultado:")
    for bucket, row in vol_stats.iterrows():
        logger.info(f"  {bucket:>8}  →  {row['trades']:>3} trades  |  {row['wins']} wins  |  WR {row['win_rate']}")

    # ── 2. Por distancia al nivel mensual ─────────────────────
    logger.info(sep)
    df["dist_bucket"] = pd.cut(
        df["level_dist_pct"],
        bins=[0, 0.1, 0.3, 0.6, 99],
        labels=["<0.1%", "0.1-0.3%", "0.3-0.6%", ">0.6%"],
    )
    dist_stats = (
        df.groupby("dist_bucket", observed=True)
        .agg(trades=("result", "count"),
             wins=("result", lambda x: (x == "win").sum()),
             win_rate=("result", lambda x: (x == "win").mean()))
        .assign(win_rate=lambda d: d["win_rate"].map("{:.0%}".format))
    )
    logger.info("Distancia entry→nivel mensual vs resultado:")
    for bucket, row in dist_stats.iterrows():
        logger.info(f"  {bucket:>10}  →  {row['trades']:>3} trades  |  {row['wins']} wins  |  WR {row['win_rate']}")

    # ── 3. Por dirección ──────────────────────────────────────
    logger.info(sep)
    dir_stats = (
        df.groupby("direction")
        .agg(trades=("result", "count"),
             wins=("result", lambda x: (x == "win").sum()),
             win_rate=("result", lambda x: (x == "win").mean()),
             avg_pnl=("pnl_pct", "mean"))
        .assign(win_rate=lambda d: d["win_rate"].map("{:.0%}".format),
                avg_pnl=lambda d: d["avg_pnl"].map("{:+.2f}%".format))
    )
    logger.info("Dirección vs resultado:")
    for direction, row in dir_stats.iterrows():
        logger.info(f"  {direction:>5}  →  {row['trades']:>3} trades  |  WR {row['win_rate']}  |  Avg PnL {row['avg_pnl']}")

    # ── 4. Por velocidad de resolución ────────────────────────
    logger.info(sep)
    med_wins  = int(df[df["result"] == "win"]["bars_to_exit"].median()) if (df["result"] == "win").any() else 0
    med_loss  = int(df[df["result"] == "loss"]["bars_to_exit"].median()) if (df["result"] == "loss").any() else 0
    logger.info("Velocidad de resolución (velas de 5m):")
    logger.info(f"  Wins  →  mediana {med_wins:>4} velas  (~{med_wins*5//60}h {med_wins*5%60}m)")
    logger.info(f"  Losses→  mediana {med_loss:>4} velas  (~{med_loss*5//60}h {med_loss*5%60}m)")

    # ── 5. Recomendación de filtros ───────────────────────────
    logger.info(sep)
    logger.info("Filtros sugeridos según los datos:")

    # Volumen: bucket con mejor WR
    best_vol = vol_stats["win_rate"].idxmax() if not vol_stats.empty else None
    if best_vol:
        logger.info(f"  • Mejor bucket de volumen: {best_vol} — considera subir volume_trigger_ratio")

    # Distancia: si trades lejanos al nivel tienen peor WR, advertir
    if not dist_stats.empty and ">0.6%" in dist_stats.index:
        far_wr_str = dist_stats.loc[">0.6%", "win_rate"]
        logger.info(f"  • Entradas >0.6% del nivel: WR {far_wr_str} — posible fakeout tardío")

    # Dirección: si una tiene WR < 25%, advertir
    for direction, row in dir_stats.iterrows():
        wr_val = df[df["direction"] == direction]["result"].apply(lambda x: x == "win").mean()
        if wr_val < 0.25:
            logger.info(f"  • {direction.upper()} tiene WR {wr_val:.0%} — considera deshabilitar esta dirección")

    logger.info(sep)

    # Guardar CSV detallado
    out = RESULTS_DIR / f"fallos_{result.symbol.replace('/', '_')}.csv"
    df.to_csv(out, index=False)
    logger.info(f"CSV detallado guardado en: {out}")

    return df


def analizar_combinado(resultados: list[BacktestResult]) -> None:
    """
    Agrega los trades de todos los símbolos y encuentra patrones comunes
    de éxito y fallo en toda la cartera.

    Responde a:
      - ¿Qué nivel de volumen es rentable en la mayoría de pares?
      - ¿Long o short falla más en el conjunto?
      - ¿Los fakeouts rápidos (pérdidas en <30m) son la causa principal?
      - ¿Qué símbolos arrastran el rendimiento global?
    """
    all_dfs = []
    for r in resultados:
        closed = [t for t in r.trades if t.result != "open"]
        if not closed:
            continue
        rows = [{
            "symbol":          t.symbol,
            "result":          t.result,
            "direction":       t.direction,
            "pnl_pct":         t.pnl_pct,
            "volume_ratio":    t.volume_ratio,
            "level_dist_pct":  t.level_distance_pct,
            "bars_to_exit":    t.bars_to_exit,
        } for t in closed]
        all_dfs.append(pd.DataFrame(rows))

    if not all_dfs:
        logger.warning("No hay trades para el análisis combinado.")
        return

    df = pd.concat(all_dfs, ignore_index=True)
    total = len(df)
    wins  = (df["result"] == "win").sum()
    sep = "═" * 60

    logger.info(sep)
    logger.info(f"ANÁLISIS COMBINADO — {len(resultados)} símbolos  |  {total} trades  |  WR global {wins/total:.1%}")
    logger.info(sep)

    # ── 1. Volumen: ¿dónde está el punto de corte rentable? ──────────
    df["vol_bucket"] = pd.cut(
        df["volume_ratio"],
        bins=[0, 2.0, 2.5, 3.0, 99],
        labels=["1.8-2.0x", "2.0-2.5x", "2.5-3.0x", ">3.0x"],
    )
    vol = (
        df.groupby("vol_bucket", observed=True)
        .agg(trades=("result", "count"),
             wins=("result", lambda x: (x == "win").sum()),
             wr=("result", lambda x: (x == "win").mean()),
             avg_pnl=("pnl_pct", "mean"))
    )
    logger.info("Volumen del spike (todos los pares):")
    for bucket, row in vol.iterrows():
        tag = " ← BAJO BREAKEVEN" if row["wr"] < 0.30 else (" ← ÓPTIMO" if row["wr"] == vol["wr"].max() else "")
        logger.info(
            f"  {bucket:>8}  |  {row['trades']:>4} trades  |  WR {row['wr']:.0%}  "
            f"|  Avg PnL {row['avg_pnl']:+.2f}%{tag}"
        )

    # ── 2. Dirección: long vs short en el conjunto ────────────────────
    logger.info("─" * 60)
    dir_g = (
        df.groupby("direction")
        .agg(trades=("result", "count"),
             wr=("result", lambda x: (x == "win").mean()),
             avg_pnl=("pnl_pct", "mean"),
             total_pnl=("pnl_pct", "sum"))
    )
    logger.info("Dirección (todos los pares):")
    for d, row in dir_g.iterrows():
        tag = " ← DESACTIVAR" if row["wr"] < 0.25 else ""
        logger.info(
            f"  {d:>5}  |  {row['trades']:>4} trades  |  WR {row['wr']:.0%}  "
            f"|  Avg PnL {row['avg_pnl']:+.2f}%  |  PnL total {row['total_pnl']:+.1f}%{tag}"
        )

    # ── 3. Velocidad de fallo: ¿cuántos son fakeouts inmediatos? ──────
    logger.info("─" * 60)
    df["fakeout"] = df["bars_to_exit"] <= 6   # ≤30 min en velas de 5m
    losses = df[df["result"] == "loss"]
    if len(losses):
        fakeout_pct = losses["fakeout"].mean()
        logger.info(f"Velocidad de resolución (pérdidas):")
        logger.info(f"  Fakeouts ≤30min : {fakeout_pct:.0%} de las pérdidas  ({losses['fakeout'].sum()} trades)")
        logger.info(f"  Mediana losses  : {int(losses['bars_to_exit'].median())} velas  (~{int(losses['bars_to_exit'].median())*5//60}h {int(losses['bars_to_exit'].median())*5%60}m)")
        logger.info(f"  Mediana wins    : {int(df[df['result']=='win']['bars_to_exit'].median())} velas  (~{int(df[df['result']=='win']['bars_to_exit'].median())*5//60}h {int(df[df['result']=='win']['bars_to_exit'].median())*5%60}m)")

    # ── 4. Ranking de símbolos: quién arrastra el rendimiento ─────────
    logger.info("─" * 60)
    sym_g = (
        df.groupby("symbol")
        .agg(trades=("result", "count"),
             wr=("result", lambda x: (x == "win").mean()),
             total_pnl=("pnl_pct", "sum"))
        .sort_values("total_pnl", ascending=False)
    )
    logger.info("Ranking de símbolos (PnL total acumulado):")
    for sym, row in sym_g.iterrows():
        bar = "█" * int(abs(row["total_pnl"]) / 3)
        sign = "+" if row["total_pnl"] >= 0 else "-"
        logger.info(
            f"  {sym:>12}  |  WR {row['wr']:.0%}  |  {sign}{abs(row['total_pnl']):5.1f}%  {bar}"
        )

    # ── 5. Recomendaciones globales ───────────────────────────────────
    logger.info("─" * 60)
    logger.info("Recomendaciones basadas en el conjunto:")

    best_vol_bucket = vol["wr"].idxmax()
    worst_vol_wr    = vol.loc["1.8-2.0x", "wr"] if "1.8-2.0x" in vol.index else None
    if worst_vol_wr is not None and worst_vol_wr < 0.30:
        wasted = int(vol.loc["1.8-2.0x", "trades"])
        logger.info(
            f"  1. Subir volume_trigger_ratio a 2.0 — elimina {wasted} trades "
            f"con WR {worst_vol_wr:.0%} (debajo del breakeven del 30%)"
        )

    for d, row in dir_g.iterrows():
        if row["wr"] < 0.27:
            logger.info(f"  2. Los {d.upper()}s tienen WR {row['wr']:.0%} — estudiar desactivarlos o añadir filtro de tendencia")

    if len(losses) and losses["fakeout"].mean() > 0.4:
        logger.info(
            f"  3. {losses['fakeout'].mean():.0%} de las pérdidas son fakeouts ≤30min — "
            f"añadir confirmación de cierre de vela antes de entrar"
        )

    # Símbolos que aportan pérdidas netas
    losers = sym_g[sym_g["total_pnl"] < 0]
    if not losers.empty:
        names = ", ".join(losers.index.tolist())
        logger.info(f"  4. Símbolos con PnL negativo: {names} — considera eliminarlos del config")

    logger.info(sep)

    # Guardar CSV combinado
    out = RESULTS_DIR / "fallos_COMBINADO.csv"
    df.to_csv(out, index=False)
    logger.info(f"CSV combinado guardado en: {out}")


    out = RESULTS_DIR / "fallos_COMBINADO.csv"
    df.to_csv(out, index=False)
    logger.info(f"CSV combinado guardado en: {out}")


# ─────────────────────────────────────────────────
# Estrategia 3: Retest post-breakout
# ─────────────────────────────────────────────────

def simular_trades_retest(
    df_entry: pd.DataFrame,
    df_daily: pd.DataFrame,
    df_weekly: pd.DataFrame | None,
    symbol: str,
    monthly_lookback: int = 6,
    tp_pct: float = 3.0,
    sl_behind_pct: float = 0.5,
    fee_pct: float = 0.1,
    leverage: int = 1,
    initial_capital: float = 1000.0,
    retest_lookback: int = 200,
    retest_min_move_pct: float = 0.5,
    retest_tolerance_pct: float = 0.35,
    retest_pullback_vol_max: float = 1.5,
    symbol_params: dict | None = None,
    _adx_min: float = 0,
    _daily_vol_min: float = 0.0,
    _crypto_trend: bool = False,
    _crypto_slope_window: int = 7,
    _crypto_min_slope: float = 1.10,
    _crypto_min_absolute: float = 25.0,
) -> "BacktestResult":
    """Entrada en pullback/retest post-breakout. Parámetros sobreescribibles por símbolo."""
    sp = (symbol_params or {}).get(symbol, {})
    if sp:
        retest_min_move_pct   = sp.get("retest_min_move_pct",   retest_min_move_pct)
        retest_tolerance_pct  = sp.get("retest_tolerance_pct",  retest_tolerance_pct)
        retest_pullback_vol_max = sp.get("retest_pullback_vol_max", retest_pullback_vol_max)

    adx_min       = sp.get("adx_min", _adx_min)
    daily_vol_min = sp.get("daily_vol_min_ratio", _daily_vol_min)

    logger.disable("indicators.levels")

    result = BacktestResult(symbol=symbol, initial_capital=initial_capital)
    equity_curve = [1.0]
    open_trade: Trade | None = None
    lookback_days = monthly_lookback * 30

    # ── Pre-calcular niveles por día ──
    daily_levels_cache: dict = {}
    for idx in range(lookback_days, len(df_daily) - 1):
        daily_window = df_daily.iloc[idx - lookback_days: idx + 1]
        try:
            lvl = calcular_niveles_mensuales(daily_window, lookback_months=monthly_lookback)
            next_date = df_daily.index[idx + 1].date()
            daily_levels_cache[next_date] = lvl
        except Exception:
            pass

    logger.enable("indicators.levels")
    logger.info(f"[Retest] Niveles pre-calculados para {len(daily_levels_cache)} días")

    # ── Pre-calcular ADX, filtro crypto y volumen diario ──
    adx_series_rt: pd.Series | None = None
    daily_vol_rt: pd.Series | None = None
    crypto_trend_rt: pd.Series | None = None
    if adx_min > 0 and len(df_daily) >= 30:
        from indicators.technical import calcular_adx_series
        adx_series_rt = calcular_adx_series(df_daily)
    if _crypto_trend and len(df_daily) >= 35:
        from indicators.technical import calcular_crypto_trend_series
        crypto_trend_rt = calcular_crypto_trend_series(
            df_daily, slope_window=_crypto_slope_window,
            min_slope=_crypto_min_slope, min_absolute=_crypto_min_absolute,
        )
    if daily_vol_min > 0 and len(df_daily) >= 21:
        daily_vol_rt = df_daily["volume"] / df_daily["volume"].rolling(20).mean().shift(1)

    COOLDOWN_BARS = 288
    last_retest_bar: dict[str, int] = {}

    for i in range(retest_lookback + 50, len(df_entry) - 1):
        current = df_entry.iloc[i]
        trade_just_closed = False

        # ── Gestionar trade abierto ──
        if open_trade is not None:
            high = float(current["high"])
            low  = float(current["low"])
            hit_tp = hit_sl = False
            if open_trade.direction == "long":
                hit_tp = high >= open_trade.take_profit
                hit_sl = low  <= open_trade.stop_loss
            else:
                hit_tp = low  <= open_trade.take_profit
                hit_sl = high >= open_trade.stop_loss

            if hit_tp or hit_sl:
                open_trade.result     = "win" if hit_tp else "loss"
                open_trade.exit_price = open_trade.take_profit if hit_tp else open_trade.stop_loss
                open_trade.exit_index = i
                pnl = (open_trade.exit_price - open_trade.entry_price) / open_trade.entry_price
                if open_trade.direction == "short":
                    pnl = -pnl
                pnl *= leverage
                pnl -= (fee_pct / 100) * 2
                open_trade.pnl_pct    = round(pnl * 100, 4)
                open_trade.bars_to_exit = i - open_trade.entry_index
                result.trades.append(open_trade)
                equity_curve.append(equity_curve[-1] * (1 + pnl))
                open_trade = None
                trade_just_closed = True

        if open_trade is not None or trade_just_closed:
            continue

        current_date = df_entry.index[i].date()
        monthly = daily_levels_cache.get(current_date)
        if monthly is None:
            continue

        # ADX estándar, filtro cripto y volumen diario
        if adx_series_rt is not None and adx_min > 0:
            adx_v = adx_series_rt.asof(df_entry.index[i])
            if pd.isna(adx_v) or float(adx_v) < adx_min:
                continue
        if crypto_trend_rt is not None:
            if not bool(crypto_trend_rt.asof(df_entry.index[i])):
                continue
        if daily_vol_rt is not None and daily_vol_min > 0:
            dvr = daily_vol_rt.asof(df_entry.index[i])
            if not pd.isna(dvr) and float(dvr) < daily_vol_min:
                continue

        current_price = float(current["close"])
        vol_mean = float(df_entry["volume"].iloc[max(0, i-50):i].mean()) or 1.0

        # ── Probar retest de resistencia (LONG) y de soporte (SHORT) ──
        for direction, level in [("long", monthly.resistance), ("short", monthly.support)]:
            if direction == "short" and leverage == 1:
                continue  # short requiere futuros

            level_key = f"{direction}_{round(level, 6)}"

            # Cooldown: no re-entrar en el mismo nivel hasta COOLDOWN_BARS
            if last_retest_bar.get(level_key, 0) > i - COOLDOWN_BARS:
                continue

            tol = level * retest_tolerance_pct / 100

            # 1. El precio actual está cerca del nivel
            if abs(current_price - level) > tol:
                continue

            # 2. La vela cierra en el lado correcto del nivel
            if direction == "long" and current_price < level - tol * 0.5:
                continue
            if direction == "short" and current_price > level + tol * 0.5:
                continue

            # 3. En el pasado reciente, el precio se alejó del nivel (breakout confirmado)
            recent_closes = df_entry["close"].iloc[max(0, i - retest_lookback): i].astype(float)
            if direction == "long":
                # El precio estuvo por encima del nivel en algún momento reciente
                max_above = (recent_closes - level).max()
                if max_above < level * retest_min_move_pct / 100:
                    continue
            else:
                # El precio estuvo por debajo del nivel en algún momento reciente
                min_below = (level - recent_closes).max()
                if min_below < level * retest_min_move_pct / 100:
                    continue

            # 4. Volumen del pullback bajo (profit-taking, no pánico)
            pullback_vol = float(current["volume"])
            if pullback_vol > vol_mean * retest_pullback_vol_max:
                continue

            # ── Calcular SL y TP ──
            entry_price = current_price
            sl_offset = entry_price * (sl_behind_pct / 100)
            tp_dist   = entry_price * (tp_pct / 100)

            if direction == "long":
                stop_loss   = entry_price - sl_offset
                take_profit = entry_price + tp_dist
            else:
                stop_loss   = entry_price + sl_offset
                take_profit = entry_price - tp_dist

            open_trade = Trade(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                entry_index=i,
                level_name=f"retest_{'resistance' if direction == 'long' else 'support'}",
                volume_ratio=round(pullback_vol / vol_mean, 2),
                level_distance_pct=round(abs(entry_price - level) / level * 100, 3),
            )
            last_retest_bar[level_key] = i
            logger.info(
                f"[Retest] {direction.upper()} {symbol} @ {entry_price:.4f} | "
                f"Nivel: {level:.4f} | Vol pullback: {pullback_vol/vol_mean:.2f}×"
            )
            break  # una sola entrada por vela

    # ── Métricas finales ──
    closed = [t for t in result.trades if t.result != "open"]
    wins   = [t for t in closed if t.result == "win"]
    losses = [t for t in closed if t.result == "loss"]

    result.total_trades     = len(closed)
    result.wins             = len(wins)
    result.losses           = len(losses)
    result.win_rate         = len(wins) / len(closed) if closed else 0
    win_pnls                = [t.pnl_pct for t in wins]
    loss_pnls               = [abs(t.pnl_pct) for t in losses]
    result.avg_win_pct      = float(np.mean(win_pnls)) if win_pnls else 0
    result.avg_loss_pct     = float(np.mean(loss_pnls)) if loss_pnls else 0
    total_wins              = sum(win_pnls)
    total_losses            = sum(loss_pnls)
    result.profit_factor    = total_wins / total_losses if total_losses > 0 else float("inf")
    result.total_return_pct = round((equity_curve[-1] - 1) * 100, 2)
    equity                  = np.array(equity_curve)
    peak                    = np.maximum.accumulate(equity)
    result.max_drawdown_pct = round(float(((equity - peak) / peak).min()) * 100, 2)
    result.equity_curve     = equity_curve
    return result


# ─────────────────────────────────────────────────
# Estrategia 2: Bounce (reversión a la media)
# ─────────────────────────────────────────────────

def simular_trades_bounce(
    df_entry: pd.DataFrame,
    df_daily: pd.DataFrame,
    symbol: str,
    config: dict,
    sl_behind_pct: float = 1.0,
    fee_pct: float = 0.1,
) -> BacktestResult:
    """
    Simula la estrategia de rebote en nivel mensual.

    Señal de entrada:
      - La mecha de la vela actual penetra la resistencia/soporte mensual.
      - El cierre queda en el lado opuesto (rechazo real del nivel).
      - TP = punto medio entre resistencia y soporte (fair value del rango).
      - SL = sl_behind_pct% más allá del nivel rechazado.

    A diferencia del breakout, esta estrategia opera CONTRA el nivel
    (fade), esperando que el precio vuelva hacia el centro del rango.
    """
    logger.disable("indicators.levels")

    result = BacktestResult(symbol=symbol)
    equity_curve = [1.0]
    open_trade: Trade | None = None
    lookback_days = config.get("levels", {}).get("monthly_lookback", 6) * 30

    # Cooldown por tipo de nivel: evita re-entrar en el mismo nivel
    # mientras el precio oscila alrededor de él.
    COOLDOWN_BARS = 576   # 48h a 5m/barra
    level_cooldown: dict[str, int] = {}

    # Pre-calcular niveles por día
    daily_levels_cache: dict = {}

    for idx in range(lookback_days, len(df_daily) - 1):
        daily_window = df_daily.iloc[idx - lookback_days: idx + 1]
        try:
            from indicators.levels import calcular_niveles_mensuales as _calc
            lvl = _calc(daily_window, lookback_months=config.get("levels", {}).get("monthly_lookback", 6))
            next_date = df_daily.index[idx + 1].date()
            daily_levels_cache[next_date] = lvl
        except Exception:
            pass

    logger.enable("indicators.levels")
    logger.info(f"[Bounce] Niveles pre-calculados para {len(daily_levels_cache)} días")

    for i in range(50, len(df_entry) - 1):
        current = df_entry.iloc[i]
        trade_just_closed = False

        # ── Verificar si trade abierto tocó SL o TP ──
        if open_trade is not None:
            high = float(current["high"])
            low  = float(current["low"])

            hit_tp = hit_sl = False
            if open_trade.direction == "long":
                hit_tp = high >= open_trade.take_profit
                hit_sl = low  <= open_trade.stop_loss
            else:
                hit_tp = low  <= open_trade.take_profit
                hit_sl = high >= open_trade.stop_loss

            if hit_tp or hit_sl:
                open_trade.result     = "win" if hit_tp else "loss"
                open_trade.exit_price = open_trade.take_profit if hit_tp else open_trade.stop_loss
                open_trade.exit_index = i

                pnl = (open_trade.exit_price - open_trade.entry_price) / open_trade.entry_price
                if open_trade.direction == "short":
                    pnl = -pnl
                pnl -= (fee_pct / 100) * 2
                open_trade.pnl_pct    = round(pnl * 100, 4)
                open_trade.bars_to_exit = i - open_trade.entry_index

                result.trades.append(open_trade)
                equity_curve.append(equity_curve[-1] * (1 + pnl))

                # Activar cooldown en el nivel que causó este trade
                level_cooldown[open_trade.level_name] = i + COOLDOWN_BARS
                open_trade = None
                trade_just_closed = True

        if open_trade is not None or trade_just_closed:
            continue

        # ── Obtener niveles y tendencia del día ──
        current_date = df_entry.index[i].date()
        monthly = daily_levels_cache.get(current_date)
        if monthly is None:
            continue

        resistance = monthly.resistance
        support    = monthly.support
        midpoint   = (resistance + support) / 2

        candle_high  = float(current["high"])
        candle_low   = float(current["low"])
        candle_close = float(current["close"])

        min_range_pct = config.get("levels", {}).get("bounce_min_range_pct", 2.0)
        wick_min_pct  = config.get("levels", {}).get("bounce_wick_min_pct", 0.10)

        range_pct = (resistance - support) / support * 100
        if range_pct < min_range_pct:
            continue

        direction    = None
        entry_price  = candle_close
        level_hit    = None
        level_key    = None

        # Mecha mínima más exigente para señales de rebote (reduce falsas señales)
        wick_min_pct_effective = max(wick_min_pct, 0.30)

        # Rebote en resistencia → SHORT
        if candle_high >= resistance and candle_close < resistance:
            wick = (candle_high - resistance) / resistance * 100
            if wick >= wick_min_pct_effective:
                direction = "short"
                level_hit = resistance
                level_key = "bounce_resistance"

        # Rebote en soporte → LONG
        elif candle_low <= support and candle_close > support:
            wick = (support - candle_low) / support * 100
            if wick >= wick_min_pct_effective:
                direction = "long"
                level_hit = support
                level_key = "bounce_support"

        if direction is None:
            continue

        # Respetar cooldown: no re-entrar en el mismo nivel tan pronto
        if level_cooldown.get(level_key, 0) > i:
            continue

        # TP = midpoint, SL = justo al otro lado del nivel rechazado
        if direction == "short":
            tp = midpoint
            sl = level_hit * (1 + sl_behind_pct / 100)
        else:
            tp = midpoint
            sl = level_hit * (1 - sl_behind_pct / 100)

        # Requiere mínimo R:R 1.5 para operar
        tp_dist      = abs(tp - entry_price)
        sl_dist_real = abs(sl - entry_price)
        if sl_dist_real <= 0 or tp_dist / sl_dist_real < 1.5:
            continue

        open_trade = Trade(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=round(sl, 8),
            take_profit=round(tp, 8),
            entry_index=i,
            level_name=level_key,
            volume_ratio=0.0,
            level_distance_pct=round(abs(entry_price - level_hit) / level_hit * 100, 3),
        )
        logger.info(
            f"[Bounce] {direction.upper()} {symbol} @ {entry_price:.4f} | "
            f"TP midpoint: {tp:.4f} | SL: {sl:.4f} | R:R ~{tp_dist/sl_dist_real:.1f}"
        )

    # Métricas finales (misma lógica que breakout)
    closed = [t for t in result.trades if t.result != "open"]
    wins   = [t for t in closed if t.result == "win"]
    losses = [t for t in closed if t.result == "loss"]

    result.total_trades = len(closed)
    result.wins         = len(wins)
    result.losses       = len(losses)
    result.win_rate     = len(wins) / len(closed) if closed else 0

    win_pnls  = [t.pnl_pct for t in wins]
    loss_pnls = [abs(t.pnl_pct) for t in losses]

    result.avg_win_pct  = float(np.mean(win_pnls))  if win_pnls  else 0
    result.avg_loss_pct = float(np.mean(loss_pnls)) if loss_pnls else 0

    total_wins   = sum(win_pnls)
    total_losses = sum(loss_pnls)
    result.profit_factor    = total_wins / total_losses if total_losses > 0 else float("inf")
    result.total_return_pct = round((equity_curve[-1] - 1) * 100, 2)

    equity = np.array(equity_curve)
    peak   = np.maximum.accumulate(equity)
    result.max_drawdown_pct = round(float(((equity - peak) / peak).min()) * 100, 2)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest de estrategia de breakout")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--days", type=int, default=60, help="Días de historia a simular")
    args = parser.parse_args()

    exchange = get_data_exchange()   # Datos públicos reales para backtest

    limit_1m = args.days * 24 * 60         # 1m candles
    limit_5m = args.days * 24 * 12         # 5m candles

    logger.info(f"Descargando datos para backtest de {args.days} días...")

    df_1m = fetch_ohlcv(args.symbol, "1m", limit=min(limit_1m, 1000), exchange=exchange)
    df_5m = fetch_ohlcv(args.symbol, "5m", limit=min(limit_5m, 500), exchange=exchange)
    df_1d = fetch_ohlcv(args.symbol, "1d", limit=90, exchange=exchange)
    df_1w = fetch_ohlcv(args.symbol, "1w", limit=52, exchange=exchange)

    result = simular_trades(df_5m, df_1d, df_1w, symbol=args.symbol)
    imprimir_resumen(result)

    # Guardar trades en CSV
    trades_df = pd.DataFrame([vars(t) for t in result.trades])
    out_path = RESULTS_DIR / f"backtest_{args.symbol.replace('/', '_')}.csv"
    trades_df.to_csv(out_path, index=False)
    logger.info(f"Trades guardados en {out_path}")
