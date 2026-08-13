"""
backtesting/portfolio.py
------------------------
Función de simulación de cartera reutilizable desde la API y CLI.
Extraída de portfolio_sim.py para poder invocarse programáticamente.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Callable


def run_simulation(
    symbols: list[str],
    capital: float,
    max_positions: int,
    days: int,
    leverage: int,
    config: dict,
    apply_filters: bool = True,
    progress_cb: Callable[[str], None] | None = None,
    date_from: str | None = None,
    date_to:   str | None = None,
    strategy_entries: list[dict] | None = None,  # nuevo: [{symbol, strategy, ...params}]
) -> dict:
    """
    Simula la cartera con capital compartido y N posiciones simultáneas.

    strategy_entries: si se provee, cada entrada es {symbol, strategy, ...params} donde
    los params son específicos de esa estrategia (filtros breakout O params retest).
    La misma criptomoneda puede aparecer varias veces con distintas estrategias.
    """
    from data.fetcher import get_data_exchange, fetch_ohlcv
    from backtesting.engine import simular_trades

    exchange = get_data_exchange()
    monthly_lookback = config["levels"]["monthly_lookback"]
    extra_days = monthly_lookback * 30 + 30

    # Parámetros de filtros globales (ADX estándar, crypto trend, vol diario)
    lvl = config.get("levels", {})
    _crypto_kw = dict(
        _adx_min=lvl.get("adx_min", 0) if apply_filters else 0,
        _daily_vol_min=lvl.get("daily_vol_min_ratio", 0.0) if apply_filters else 0.0,
        _crypto_trend=lvl.get("crypto_trend_filter", False) if apply_filters else False,
        _crypto_slope_window=lvl.get("crypto_trend_slope_window", 7),
        _crypto_min_slope=lvl.get("crypto_trend_min_slope", 1.10),
        _crypto_min_absolute=lvl.get("crypto_trend_min_absolute", 25.0),
        _breakout_cooldown=lvl.get("breakout_loss_cooldown_bars", 0) if apply_filters else 0,
    )

    # Modo strategy_entries: agrupa por símbolo para descargar datos una sola vez
    sym_to_entries: dict[str, list[dict]] = {}
    if strategy_entries is not None:
        for e in strategy_entries:
            s = e["symbol"]
            if s not in sym_to_entries:
                sym_to_entries[s] = []
            sym_to_entries[s].append(e)
        symbols = list(sym_to_entries.keys())

    sym_params = config.get("symbol_params", {}) if apply_filters else None
    rsi_block  = config.get("rsi_overbought_block") if apply_filters else None

    all_trades: list[dict] = []
    actual_days = days

    # Pre-calcular fetch_days (igual para todos los símbolos)
    if date_from:
        from datetime import date as _date
        fetch_days = (_date.today() - _date.fromisoformat(date_from)).days + extra_days + 60
    else:
        fetch_days = days + extra_days + 60

    # Descargar datos de todos los símbolos en paralelo (I/O bound)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _fetch_sym(sym):
        return sym, (
            fetch_ohlcv(sym, "5m", limit=fetch_days * 24 * 12, exchange=exchange),
            fetch_ohlcv(sym, "1d", limit=fetch_days + extra_days, exchange=exchange),
            fetch_ohlcv(sym, "1w", limit=220, exchange=exchange),
        )

    sym_data: dict = {}
    with ThreadPoolExecutor(max_workers=min(len(symbols), 4)) as pool:
        futures = {pool.submit(_fetch_sym, s): s for s in symbols}
        for fut in as_completed(futures):
            s, dfs = fut.result()
            sym_data[s] = dfs

    for idx, sym in enumerate(symbols, 1):
        if progress_cb:
            progress_cb(f"SYM:{idx}:{len(symbols)}:{sym}")

        df_5m, df_1d, df_1w = sym_data[sym]

        if date_from or date_to:
            if date_from:
                ts_from = pd.Timestamp(date_from, tz="UTC")
                df_5m = df_5m[df_5m.index >= ts_from]
            if date_to:
                ts_to = pd.Timestamp(date_to, tz="UTC") + pd.Timedelta(days=1)
                df_5m = df_5m[df_5m.index < ts_to]

        if len(df_5m) > 0:
            span = (df_5m.index[-1] - df_5m.index[0]).days
            actual_days = min(actual_days, span)

        # ── Modo strategy_entries: cada entrada define símbolo + estrategia + params propios ──
        if strategy_entries is not None:
            _common_kw = dict(
                df_daily=df_1d, df_weekly=df_1w, symbol=sym,
                monthly_lookback=monthly_lookback,
                tp_pct=config["risk"]["take_profit_pct"],
                fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
                leverage=leverage, initial_capital=capital,
            )
            for entry in sym_to_entries[sym]:
                strategy  = entry["strategy"]
                ep        = {k: v for k, v in entry.items() if k not in ("symbol", "strategy")}
                entry_key = f"{sym} · {strategy}"
                entry_trades: list = []

                if strategy == "breakout":
                    bp = ep if apply_filters else {}
                    res = simular_trades(
                        df_entry=df_5m,
                        sl_behind_pct=config["risk"]["sl_behind_level_pct"],
                        volume_ratio_min=config["levels"]["volume_trigger_ratio"],
                        volume_ratio_max=config["levels"].get("volume_trigger_ratio_max", 3.0),
                        ml_threshold=0.0, volatility_filter=False,
                        failed_retest_filter=bp.get("failed_retest_filter", True) if apply_filters else False,
                        symbol_params={sym: bp} if bp else None,
                        rsi_overbought_block=bp.get("rsi_overbought_block") if apply_filters else None,
                        **_crypto_kw,
                        **_common_kw,
                    )
                    entry_trades.extend(res.trades)

                elif strategy == "retest":
                    from backtesting.engine import simular_trades_retest
                    res_rt = simular_trades_retest(
                        df_entry=df_5m,
                        sl_behind_pct=0.5,
                        symbol_params={sym: ep},
                        **_crypto_kw,
                        **_common_kw,
                    )
                    entry_trades.extend(res_rt.trades)

                for t in entry_trades:
                    if t.result not in ("win", "loss"):
                        continue
                    all_trades.append({
                        "entry_ts":    df_5m.index[t.entry_index],
                        "exit_ts":     df_5m.index[t.exit_index],
                        "pnl_pct":     t.pnl_pct,
                        "symbol":      entry_key,   # estadísticas por estrategia
                        "base_symbol": sym,         # gestión de slots (evita doble exposición al activo)
                        "result":      t.result,
                        "direction":   t.direction,
                        "entry_price": round(t.entry_price, 6),
                        "exit_price":  round(t.exit_price, 6),
                        "volume_ratio": t.volume_ratio,
                        "bars_to_exit": t.bars_to_exit,
                    })

                if progress_cb:
                    closed = [t for t in entry_trades if t.result in ("win", "loss")]
                    wins   = sum(1 for t in closed if t.result == "win")
                    wr_est = round(wins / len(closed) * 100, 0) if closed else 0
                    longs  = sum(1 for t in closed if t.direction == "long")
                    shorts = len(closed) - longs
                    progress_cb(f"SYMDONE:{entry_key}:{len(closed)}:{wr_est}:{longs}:{shorts}")
            continue  # siguiente símbolo — saltar el bloque legacy

        # ── Modo legacy: symbol_params desde config ──────────────────────────
        sp_sym = (sym_params or {}).get(sym, {})
        global_strats = config.get("strategies", ["breakout"])
        active_strats = sp_sym.get("strategies", global_strats)

        sym_trades_all: list = []

        if "breakout" in active_strats:
            res = simular_trades(
                df_entry=df_5m,
                df_daily=df_1d,
                df_weekly=df_1w,
                symbol=sym,
                monthly_lookback=monthly_lookback,
                tp_pct=config["risk"]["take_profit_pct"],
                sl_behind_pct=config["risk"]["sl_behind_level_pct"],
                volume_ratio_min=config["levels"]["volume_trigger_ratio"],
                volume_ratio_max=config["levels"].get("volume_trigger_ratio_max", 3.0),
                fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
                ml_threshold=0.0,
                volatility_filter=False,
                leverage=leverage,
                initial_capital=capital,
                failed_retest_filter=apply_filters,
                symbol_params=sym_params,
                rsi_overbought_block=rsi_block,
                **_crypto_kw,
            )
            sym_trades_all.extend(res.trades)

        if "retest" in active_strats:
            from backtesting.engine import simular_trades_retest
            res_rt = simular_trades_retest(
                df_entry=df_5m,
                df_daily=df_1d,
                df_weekly=df_1w,
                symbol=sym,
                monthly_lookback=monthly_lookback,
                tp_pct=config["risk"]["take_profit_pct"],
                sl_behind_pct=0.5,
                fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
                leverage=leverage,
                initial_capital=capital,
                symbol_params=sym_params,
                **_crypto_kw,
            )
            sym_trades_all.extend(res_rt.trades)

        for t in sym_trades_all:
            if t.result not in ("win", "loss"):
                continue
            all_trades.append({
                "entry_ts":    df_5m.index[t.entry_index],
                "exit_ts":     df_5m.index[t.exit_index],
                "pnl_pct":     t.pnl_pct,
                "symbol":      sym,
                "base_symbol": sym,
                "result":      t.result,
                "direction":   t.direction,
                "entry_price": round(t.entry_price, 6),
                "exit_price":  round(t.exit_price, 6),
                "volume_ratio": t.volume_ratio,
                "bars_to_exit": t.bars_to_exit,
            })

        if progress_cb:
            closed = [t for t in sym_trades_all if t.result in ("win", "loss")]
            wins   = sum(1 for t in closed if t.result == "win")
            wr_est = round(wins / len(closed) * 100, 0) if closed else 0
            longs  = sum(1 for t in closed if t.direction == "long")
            shorts = len(closed) - longs
            progress_cb(f"SYMDONE:{sym}:{len(closed)}:{wr_est}:{longs}:{shorts}")

    if not all_trades:
        return {"error": "Sin trades para el período solicitado", "actual_days": actual_days}

    df = pd.DataFrame(all_trades).sort_values("entry_ts").reset_index(drop=True)

    # ── Simulación de cartera con N posiciones simultáneas ─────────────────────
    cap = capital
    equity_curve = [cap]
    executed: list[dict] = []
    skipped = 0
    open_slots: list[dict] = []

    for _, row in df.iterrows():
        remaining = []
        for slot in open_slots:
            if slot["exit_ts"] <= row["entry_ts"]:
                cap += slot["allocated"] * slot["pnl_pct"] / 100.0
                equity_curve.append(cap)
                executed.append(slot)
            else:
                remaining.append(slot)
        open_slots = remaining

        if len(open_slots) >= max_positions:
            skipped += 1
            continue
        # Prevenir doble exposición al mismo activo (aplica tanto a legacy como a strategy_entries)
        row_base = row.get("base_symbol", row["symbol"])
        if any(s.get("base_symbol", s["symbol"]) == row_base for s in open_slots):
            skipped += 1
            continue

        open_slots.append({
            "entry_ts":  row["entry_ts"],
            "exit_ts":   row["exit_ts"],
            "allocated": cap / max_positions,
            "pnl_pct":   row["pnl_pct"],
            "symbol":    row["symbol"],
            "base_symbol": row.get("base_symbol", row["symbol"]),
            "result":    row["result"],
            "direction": row.get("direction", ""),
            "entry_price": row.get("entry_price", 0),
            "exit_price":  row.get("exit_price", 0),
            "volume_ratio": row.get("volume_ratio", 0),
            "bars_to_exit": row.get("bars_to_exit", 0),
        })

    for slot in open_slots:
        cap += slot["allocated"] * slot["pnl_pct"] / 100.0
        equity_curve.append(cap)
        executed.append(slot)

    exec_df = pd.DataFrame(executed) if executed else pd.DataFrame()
    wins   = int((exec_df["result"] == "win").sum())  if len(exec_df) else 0
    losses = int((exec_df["result"] == "loss").sum()) if len(exec_df) else 0
    total  = wins + losses
    wr     = wins / total * 100 if total else 0

    eq_arr = np.array(equity_curve)
    peak   = np.maximum.accumulate(eq_arr)
    max_dd = float(((eq_arr - peak) / peak).min()) * 100

    # ── Por símbolo / entry_key ────────────────────────────────────────────────
    # Con strategy_entries los trades usan "ADA/USDT · breakout" como clave;
    # en modo legacy usan el símbolo base. En ambos casos iteramos sobre los
    # valores únicos que realmente aparecen en exec_df.
    entry_keys = list(dict.fromkeys(t["symbol"] for t in all_trades)) if all_trades else symbols
    por_simbolo = []
    for ek in entry_keys:
        st = exec_df[exec_df["symbol"] == ek] if len(exec_df) else pd.DataFrame()
        sw = int((st["result"] == "win").sum()) if len(st) else 0
        sl = int((st["result"] == "loss").sum()) if len(st) else 0
        por_simbolo.append({
            "symbol":   ek,
            "trades":   len(st),
            "wins":     sw,
            "losses":   sl,
            "win_rate": round(sw / len(st) * 100, 1) if len(st) else 0,
        })

    # ── Por año ────────────────────────────────────────────────────────────────
    por_anio: list[dict] = []
    if executed and len(df) > 0:
        equity_ts = [exec_df["exit_ts"].iloc[i] for i in range(len(exec_df))]
        for yr in range(df["entry_ts"].min().year, df["exit_ts"].max().year + 1):
            cutoff = pd.Timestamp(f"{yr}-12-31", tz="UTC")
            pts = [eq_v for ts, eq_v in zip(equity_ts, equity_curve[1:]) if ts <= cutoff]
            if not pts:
                continue
            cap_yr = pts[-1]
            por_anio.append({
                "year":    yr,
                "capital": round(cap_yr, 2),
                "pnl_pct": round((cap_yr / capital - 1) * 100, 1),
            })

    # ── Análisis de volumen ────────────────────────────────────────────────────
    analisis_vol: list[dict] = []
    if len(exec_df) and "volume_ratio" in exec_df.columns:
        for lbl, lo, hi in [("<2.0×", 0, 2.0), ("2.0-2.5×", 2.0, 2.5), ("2.5-3.0×", 2.5, 3.0), (">3.0×", 3.0, 99)]:
            b = exec_df[(exec_df["volume_ratio"] > lo) & (exec_df["volume_ratio"] <= hi)]
            if len(b) == 0:
                continue
            bw = (b["result"] == "win").sum()
            analisis_vol.append({"bucket": lbl, "trades": int(len(b)), "wins": int(bw),
                                  "win_rate": round(bw / len(b) * 100, 1)})

    # ── Análisis de dirección ─────────────────────────────────────────────────
    analisis_dir: list[dict] = []
    if len(exec_df) and "direction" in exec_df.columns:
        for d in ["long", "short"]:
            dt = exec_df[exec_df["direction"] == d]
            if len(dt) == 0:
                continue
            dw = (dt["result"] == "win").sum()
            analisis_dir.append({"direction": d, "trades": int(len(dt)), "wins": int(dw),
                                  "win_rate": round(dw / len(dt) * 100, 1)})

    # ── Curva de equity (max 300 puntos para el front) ────────────────────────
    step = max(1, len(equity_curve) // 300)
    eq_sampled = [round(v, 2) for v in equity_curve[::step]]

    return {
        "actual_days":       actual_days,
        "actual_years":      round(actual_days / 365, 1),
        "capital_inicial":   capital,
        "capital_final":     round(cap, 2),
        "pnl_usdt":          round(cap - capital, 2),
        "pnl_pct":           round((cap / capital - 1) * 100, 1),
        "total_signals":     len(df),
        "total_trades":      total,
        "skipped":           skipped,
        "wins":              wins,
        "losses":            losses,
        "win_rate":          round(wr, 1),
        "max_drawdown_pct":  round(max_dd, 1),
        "min_equity":        round(float(min(equity_curve)), 2),
        "por_simbolo":       por_simbolo,
        "por_anio":          por_anio,
        "analisis_volumen":  analisis_vol,
        "analisis_direccion": analisis_dir,
        "equity_curve":      eq_sampled,
        "trades_por_simbolo": {
            ek: [
                {
                    "entry_ts":    str(s["entry_ts"])[:19].replace("T", " "),
                    "exit_ts":     str(s["exit_ts"])[:19].replace("T", " "),
                    "direction":   s.get("direction", ""),
                    "result":      s["result"],
                    "pnl_pct":     s["pnl_pct"],
                    "entry_price": s.get("entry_price", 0),
                    "exit_price":  s.get("exit_price", 0),
                    "volume_ratio": round(s.get("volume_ratio", 0), 2),
                }
                # Más recientes primero; limitado a 500 por símbolo
                for s in sorted(
                    [s for s in executed if s["symbol"] == ek],
                    key=lambda x: x["entry_ts"], reverse=True
                )[:500]
            ]
            for ek in entry_keys
        },
        "trades_total_por_simbolo": {
            ek: sum(1 for s in executed if s["symbol"] == ek)
            for ek in entry_keys
        },
    }


def run_filter_analysis(
    symbols: list[str],
    days: int,
    leverage: int,
    config: dict,
    progress_cb: Callable[[str], None] | None = None,
    date_from: str | None = None,
    date_to:   str | None = None,
    strategy_entries: list[dict] | None = None,  # nuevo: misma lista que run_simulation
) -> dict:
    """
    Para entradas breakout, testa cada filtro individualmente.
    Con strategy_entries procesa solo las entradas de estrategia breakout.
    """
    from data.fetcher import get_data_exchange, fetch_ohlcv
    from backtesting.engine import simular_trades

    exchange = get_data_exchange()
    monthly_lookback = config["levels"]["monthly_lookback"]
    extra_days = monthly_lookback * 30 + 30
    sym_params_all = config.get("symbol_params", {})

    STD_MOMENTUM   = [0.30, 1.60]
    STD_USDT_BLOCK = [2.1, 2.7]
    STD_RSI        = 70.0

    result: dict = {}

    # Con strategy_entries: solo procesar entradas breakout; la clave de resultado es entry_key
    if strategy_entries is not None:
        breakout_entries = [e for e in strategy_entries if e.get("strategy") == "breakout"]
        # Reemplazar symbols y sym_params con los datos de las entradas breakout
        symbols = [e["symbol"] for e in breakout_entries]
        sym_params_all = {
            e["symbol"]: {k: v for k, v in e.items() if k not in ("symbol", "strategy")}
            for e in breakout_entries
        }

    # Pre-calcular fetch_days y descargar todos los símbolos en paralelo
    if date_from:
        from datetime import date as _date
        fetch_days = (_date.today() - _date.fromisoformat(date_from)).days + extra_days + 60
    else:
        fetch_days = days + extra_days + 60

    def _fetch_sym_fa(sym):
        df_5m = fetch_ohlcv(sym, "5m", limit=fetch_days * 24 * 12, exchange=exchange)
        df_1d = fetch_ohlcv(sym, "1d", limit=fetch_days + extra_days, exchange=exchange)
        df_1w = fetch_ohlcv(sym, "1w", limit=220, exchange=exchange)
        if date_from or date_to:
            if date_from:
                ts_from = pd.Timestamp(date_from, tz="UTC")
                df_5m = df_5m[df_5m.index >= ts_from]
            if date_to:
                ts_to = pd.Timestamp(date_to, tz="UTC") + pd.Timedelta(days=1)
                df_5m = df_5m[df_5m.index < ts_to]
        return sym, (df_5m, df_1d, df_1w)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    _data_cache: dict = {}
    with ThreadPoolExecutor(max_workers=min(len(symbols), 4)) as pool:
        futures = {pool.submit(_fetch_sym_fa, s): s for s in symbols}
        for fut in as_completed(futures):
            s, dfs = fut.result()
            _data_cache[s] = dfs

    for sym in symbols:
        ek = f"{sym} · breakout" if strategy_entries is not None else sym
        sp = sym_params_all.get(sym, {})

        if progress_cb:
            progress_cb(f"FILTERANAL:{ek}")

        df_5m, df_1d, df_1w = _data_cache[sym]

        common = dict(
            df_entry=df_5m, df_daily=df_1d, df_weekly=df_1w,
            symbol=sym, monthly_lookback=monthly_lookback,
            tp_pct=config["risk"]["take_profit_pct"],
            sl_behind_pct=config["risk"]["sl_behind_level_pct"],
            volume_ratio_min=config["levels"]["volume_trigger_ratio"],
            volume_ratio_max=config["levels"].get("volume_trigger_ratio_max", 3.0),
            fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
            leverage=leverage, initial_capital=1000,
        )

        def _stats(res) -> tuple[int, int, float]:
            closed = [t for t in res.trades if t.result in ("win", "loss")]
            wins = sum(1 for t in closed if t.result == "win")
            return len(closed), wins, (round(wins / len(closed) * 100, 1) if closed else 0.0)

        # Baseline sin ningún filtro
        base_t, base_w, base_wr = _stats(simular_trades(
            **common, failed_retest_filter=False, symbol_params=None, rsi_overbought_block=None,
        ))
        sym_res: dict = {"baseline": {"trades": base_t, "wins": base_w, "win_rate": base_wr}}

        def _add(fname: str, configured: bool = False, **kwargs):
            t, w, wr = _stats(simular_trades(**common, **kwargs))
            sym_res[fname] = {
                "trades": t, "wins": w, "win_rate": wr,
                "wr_delta": round(wr - base_wr, 1),
                "trades_filtered": base_t - t,
                "configured": configured,   # True = ya está en config.yaml
            }

        # F1 Momentum — usa valor configurado si existe, si no el estándar
        mom_val = sp.get("momentum_q3_block", STD_MOMENTUM)
        configured_f1 = "momentum_q3_block" in sp
        label_f1 = f"F1 Momentum [{mom_val[0]}-{mom_val[1]}%]"
        _add(label_f1, configured=configured_f1, failed_retest_filter=False,
             symbol_params={sym: {"momentum_q3_block": mom_val}}, rsi_overbought_block=None)

        # F3 Vol USDT normalizado
        usdt_val = sp.get("usdt_norm_block_range", STD_USDT_BLOCK)
        configured_f3 = "usdt_norm_block_range" in sp
        label_f3 = f"F3 Vol USDT [{usdt_val[0]}-{usdt_val[1]}×]"
        _add(label_f3, configured=configured_f3, failed_retest_filter=False,
             symbol_params={sym: {"usdt_norm_block_range": usdt_val}}, rsi_overbought_block=None)

        # F4 RSI14 sobrecompra
        rsi_val = sp.get("rsi_overbought_block", STD_RSI)
        configured_f4 = "rsi_overbought_block" in sp
        _add(f"F4 RSI14 ≥{rsi_val}", configured=configured_f4, failed_retest_filter=False,
             symbol_params=None, rsi_overbought_block=rsi_val)

        # Failed retest adaptativo — configured=False si está explícitamente desactivado
        fr_active = sp.get("failed_retest_filter") is not False
        _add("Failed retest (auto)", configured=fr_active, failed_retest_filter=True,
             symbol_params=None, rsi_overbought_block=None)

        # F2b Sesión UTC — solo si está configurado (requiere análisis histórico)
        if "session_block_hours" in sp:
            lo_h, hi_h = sp["session_block_hours"]
            _add(f"F2b Sesión {lo_h}-{hi_h}h UTC", configured=True,
                 failed_retest_filter=False,
                 symbol_params={sym: {"session_block_hours": sp["session_block_hours"]}},
                 rsi_overbought_block=None)

        # Vol máximo — solo si configurado
        if "volume_trigger_ratio_max" in sp:
            _add(f"Vol máximo ≤{sp['volume_trigger_ratio_max']}×", configured=True,
                 failed_retest_filter=False,
                 symbol_params={sym: {"volume_trigger_ratio_max": sp["volume_trigger_ratio_max"]}},
                 rsi_overbought_block=None)

        result[ek] = sym_res

    return result
