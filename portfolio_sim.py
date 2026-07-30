"""
portfolio_sim.py
----------------
Simulación de cartera con capital compartido (€100 total).
Combina señales de ADA, LINK, EGLD y ATOM — un trade a la vez, capital 100% compuesto.
"""

import sys
import yaml
import pandas as pd
from pathlib import Path
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")   # silenciar INFO durante la sim

CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

from data.fetcher import get_data_exchange, fetch_ohlcv
from backtesting.engine import simular_trades

SYMBOLS       = ["ADA/USDT", "LINK/USDT", "EGLD/USDT", "ATOM/USDT"]
DAYS          = 3650   # 10 años
LEVERAGE      = 3
CAPITAL       = 100.0
MAX_POSITIONS = 3      # posiciones simultáneas máximas (33.33% por trade)
COMPARE_TREND = True   # True = muestra resultados con y sin trend filter

exchange = get_data_exchange()

monthly_lookback = config["levels"]["monthly_lookback"]
extra_days = monthly_lookback * 30 + 30

# ─────────────────────────────────────────────────
# Función: generar trades de todos los símbolos
# ─────────────────────────────────────────────────
def generar_trades(trend_filter: bool) -> tuple[pd.DataFrame, dict]:
    """Devuelve (df_trades, dataframes) — dataframes reutilizables entre llamadas."""
    trades = []
    dfs: dict = {}

    for sym in SYMBOLS:
        print(f"  Simulando {sym}{'  [trend filter ON]' if trend_filter else ''}...", flush=True)

        df_5m = fetch_ohlcv(sym, "5m", limit=DAYS * 24 * 12, exchange=exchange)
        df_1d = fetch_ohlcv(sym, "1d", limit=DAYS + extra_days, exchange=exchange)
        # Semanas suficientes para SMA200 semanal (200 + margen)
        df_1w = fetch_ohlcv(sym, "1w", limit=220, exchange=exchange)
        dfs[sym] = (df_5m, df_1d, df_1w)

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
            leverage=LEVERAGE,
            initial_capital=CAPITAL,
            failed_retest_filter=True,
            symbol_params=config.get("symbol_params"),
            rsi_overbought_block=config.get("rsi_overbought_block"),
            trend_filter=trend_filter,
        )

        for t in res.trades:
            if t.result not in ("win", "loss"):
                continue
            trades.append({
                "entry_ts": df_5m.index[t.entry_index],
                "exit_ts":  df_5m.index[t.exit_index],
                "pnl_pct":  t.pnl_pct,
                "symbol":   sym,
                "result":   t.result,
            })

    return pd.DataFrame(trades).sort_values("entry_ts").reset_index(drop=True), dfs


# ─────────────────────────────────────────────────
# 1. Obtener trades (sin filtro de tendencia)
# ─────────────────────────────────────────────────
df_trades, _ = generar_trades(trend_filter=False)

# ─────────────────────────────────────────────────
# 2. Función: simular capital compartido con N posiciones
# ─────────────────────────────────────────────────
def simular_cartera(df_trades: pd.DataFrame) -> tuple[float, list, list, int]:
    """Devuelve (capital_final, equity_curve, executed, skipped)."""
    capital      = CAPITAL
    equity_curve = [capital]
    executed: list[dict] = []
    skipped      = 0
    open_slots: list[dict] = []

    for _, row in df_trades.iterrows():
        # Cerrar slots que terminaron antes de este entry
        remaining = []
        for slot in open_slots:
            if slot["exit_ts"] <= row["entry_ts"]:
                pnl_usdt  = slot["allocated"] * slot["pnl_pct"] / 100.0
                capital  += pnl_usdt
                equity_curve.append(capital)
                executed.append(slot)
            else:
                remaining.append(slot)
        open_slots = remaining

        if len(open_slots) >= MAX_POSITIONS:
            skipped += 1
            continue
        if any(s["symbol"] == row["symbol"] for s in open_slots):
            skipped += 1
            continue

        open_slots.append({
            "entry_ts":  row["entry_ts"],
            "exit_ts":   row["exit_ts"],
            "allocated": capital / MAX_POSITIONS,
            "pnl_pct":   row["pnl_pct"],
            "symbol":    row["symbol"],
            "result":    row["result"],
        })

    for slot in open_slots:
        pnl_usdt  = slot["allocated"] * slot["pnl_pct"] / 100.0
        capital  += pnl_usdt
        equity_curve.append(capital)
        executed.append(slot)

    return capital, equity_curve, executed, skipped


def imprimir_resultado(label: str, df_trades: pd.DataFrame,
                       capital: float, equity_curve: list, executed: list, skipped: int):
    executed_df = pd.DataFrame(executed)
    wins   = (executed_df["result"] == "win").sum()
    losses = (executed_df["result"] == "loss").sum()
    total  = len(executed_df)
    wr     = wins / total * 100 if total else 0

    max_dd = 0.0
    peak   = equity_curve[0]
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak - v) / peak
        if dd > max_dd: max_dd = dd

    print(f"\n{'='*52}")
    print(f"  {label}")
    print(f"  SIMULACIÓN — {DAYS} días ({DAYS//365} años) | {MAX_POSITIONS} posiciones | {LEVERAGE}x")
    print(f"{'='*52}")
    print(f"  Capital inicial : €{CAPITAL:.2f}")
    print(f"  Capital final   : €{capital:.2f}  ({(capital/CAPITAL - 1)*100:+.1f}%)")
    print(f"  Trades ejecutados: {total}  (de {len(df_trades)} señales totales)")
    print(f"  Trades saltados  : {skipped}  (colisión temporal)")
    print(f"  Win rate         : {wr:.1f}%  ({wins}W / {losses}L)")
    print(f"  Max drawdown     : -{max_dd*100:.1f}%  (€{min(equity_curve):.2f} mínimo)")
    print(f"\n  Por símbolo:")
    for sym in SYMBOLS:
        sym_t = executed_df[executed_df["symbol"] == sym]
        if len(sym_t) == 0: continue
        sw = (sym_t["result"] == "win").sum()
        print(f"    {sym:12s} → {len(sym_t):3d} trades  WR {sw/len(sym_t)*100:.0f}%")

    equity_ts = [executed_df["exit_ts"].iloc[i] for i in range(len(executed_df))]
    print(f"\n  Evolución por año:")
    for yr in range(df_trades["entry_ts"].min().year, df_trades["exit_ts"].max().year + 1):
        cutoff = pd.Timestamp(f"{yr}-12-31", tz="UTC")
        pts = [(ts, eq) for ts, eq in zip(equity_ts, equity_curve[1:]) if ts <= cutoff]
        if not pts: continue
        cap_yr = pts[-1][1]
        print(f"    {yr}: €{cap_yr:.2f}  ({(cap_yr/CAPITAL - 1)*100:+.1f}%)")


# ─────────────────────────────────────────────────
# 3. Ejecutar simulación sin trend filter
# ─────────────────────────────────────────────────
print(f"\nTotal señales generadas: {len(df_trades)}")
capital, equity_curve, executed, skipped = simular_cartera(df_trades)
imprimir_resultado("SIN FILTRO DE TENDENCIA", df_trades, capital, equity_curve, executed, skipped)

# ─────────────────────────────────────────────────
# 3. Simular capital compartido — hasta MAX_POSITIONS simultáneas
#    Cada slot recibe capital/MAX_POSITIONS al abrirse (fracción fija)
# ─────────────────────────────────────────────────
capital      = CAPITAL
equity_curve = [capital]
executed     = []
skipped      = 0

# Cada slot: {exit_ts, entry_ts, allocated, pnl_pct, symbol, result}
open_slots: list[dict] = []

for _, row in df_trades.iterrows():
    # Cerrar slots que terminaron antes de este entry
    remaining = []
    for slot in open_slots:
        if slot["exit_ts"] <= row["entry_ts"]:
            pnl_usdt  = slot["allocated"] * slot["pnl_pct"] / 100.0
            capital  += pnl_usdt
            equity_curve.append(capital)
            executed.append(slot)
        else:
            remaining.append(slot)
    open_slots = remaining

    # Saltar si ya tenemos el máximo de posiciones abiertas
    if len(open_slots) >= MAX_POSITIONS:
        skipped += 1
        continue

    # Saltar si el mismo símbolo ya tiene una posición abierta
    if any(s["symbol"] == row["symbol"] for s in open_slots):
        skipped += 1
        continue

    # Abrir nueva posición — asignar capital / MAX_POSITIONS
    open_slots.append({
        "entry_ts":  row["entry_ts"],
        "exit_ts":   row["exit_ts"],
        "allocated": capital / MAX_POSITIONS,
        "pnl_pct":   row["pnl_pct"],
        "symbol":    row["symbol"],
        "result":    row["result"],
    })

# Cerrar slots restantes al final del periodo
for slot in open_slots:
    pnl_usdt  = slot["allocated"] * slot["pnl_pct"] / 100.0
    capital  += pnl_usdt
    equity_curve.append(capital)
    executed.append(slot)

executed_df = pd.DataFrame(executed)

# ─────────────────────────────────────────────────
# 4. Resultados
# ─────────────────────────────────────────────────
wins   = (executed_df["result"] == "win").sum()
losses = (executed_df["result"] == "loss").sum()
total  = len(executed_df)
wr     = wins / total * 100 if total else 0

max_dd = 0.0
peak   = equity_curve[0]
for v in equity_curve:
    if v > peak:
        peak = v
    dd = (peak - v) / peak
    if dd > max_dd:
        max_dd = dd

print(f"\n{'='*52}")
print(f"  SIMULACIÓN CARTERA COMPARTIDA — {DAYS} días ({DAYS//365} años)")
print(f"  Posiciones simultáneas: {MAX_POSITIONS} | Leverage: {LEVERAGE}x")
print(f"{'='*52}")
print(f"  Capital inicial : €{CAPITAL:.2f}")
print(f"  Capital final   : €{capital:.2f}  ({(capital/CAPITAL - 1)*100:+.1f}%)")
print(f"  Trades ejecutados: {total}  (de {len(df_trades)} señales totales)")
print(f"  Trades saltados  : {skipped}  (colisión temporal)")
print(f"  Win rate         : {wr:.1f}%  ({wins}W / {losses}L)")
print(f"  Max drawdown     : -{max_dd*100:.1f}%  (€{min(equity_curve):.2f} mínimo)")
print(f"\n  Por símbolo ejecutado:")
for sym in SYMBOLS:
    sym_trades = executed_df[executed_df["symbol"] == sym]
    if len(sym_trades) == 0:
        continue
    sw = (sym_trades["result"] == "win").sum()
    sl_ = (sym_trades["result"] == "loss").sum()
    print(f"    {sym:12s} → {len(sym_trades):3d} trades  WR {sw/len(sym_trades)*100:.0f}%")

# ─────────────────────────────────────────────────
# 4. Comparación con trend filter (si está activo)
# ─────────────────────────────────────────────────
if COMPARE_TREND:
    print("\n" + "─"*52)
    print("  Descargando trades con TREND FILTER activo...")
    df_trades_tf, _ = generar_trades(trend_filter=True)
    print(f"  Señales con trend filter: {len(df_trades_tf)}")
    cap_tf, eq_tf, ex_tf, sk_tf = simular_cartera(df_trades_tf)
    imprimir_resultado("CON FILTRO DE TENDENCIA (SMA50w > SMA200w)", df_trades_tf,
                       cap_tf, eq_tf, ex_tf, sk_tf)
