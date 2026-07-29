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

SYMBOLS   = ["ADA/USDT", "LINK/USDT", "EGLD/USDT", "ATOM/USDT"]
DAYS      = 2190
LEVERAGE  = 3
CAPITAL   = 100.0   # capital compartido en euros/dólares

exchange = get_data_exchange()

# ─────────────────────────────────────────────────
# 1. Obtener trades de cada símbolo con timestamps
# ─────────────────────────────────────────────────
all_trades = []   # lista de (entry_ts, exit_ts, pnl_pct, symbol)

monthly_lookback = config["levels"]["monthly_lookback"]
extra_days = monthly_lookback * 30 + 30

for sym in SYMBOLS:
    print(f"  Simulando {sym}...", flush=True)

    df_5m = fetch_ohlcv(sym, "5m", limit=DAYS * 24 * 12, exchange=exchange)
    df_1d = fetch_ohlcv(sym, "1d", limit=DAYS + extra_days, exchange=exchange)
    df_1w = fetch_ohlcv(sym, "1w", limit=52, exchange=exchange)

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
        failed_retest_filter=True,   # usa config (auto / overrides)
        symbol_params=config.get("symbol_params"),
        rsi_overbought_block=config.get("rsi_overbought_block"),
    )

    # Mapear entry_index / exit_index → timestamps reales
    for t in res.trades:
        if t.result not in ("win", "loss"):
            continue   # ignorar trades abiertos al final del periodo
        entry_ts = df_5m.index[t.entry_index]
        exit_ts  = df_5m.index[t.exit_index]
        all_trades.append({
            "entry_ts": entry_ts,
            "exit_ts":  exit_ts,
            "pnl_pct":  t.pnl_pct,   # ya incluye leverage y fees
            "symbol":   sym,
            "result":   t.result,
        })

# ─────────────────────────────────────────────────
# 2. Ordenar por timestamp de entrada
# ─────────────────────────────────────────────────
df_trades = pd.DataFrame(all_trades).sort_values("entry_ts").reset_index(drop=True)
print(f"\nTotal señales generadas: {len(df_trades)}")

# ─────────────────────────────────────────────────
# 3. Simular capital compartido — 1 trade a la vez
# ─────────────────────────────────────────────────
capital      = CAPITAL
equity_curve = [capital]
executed     = []
skipped      = 0
current_exit = pd.Timestamp.min.tz_localize("UTC")   # timestamp de fin del trade activo

for _, row in df_trades.iterrows():
    if row["entry_ts"] <= current_exit:
        # Ya hay un trade abierto que no ha cerrado → skip
        skipped += 1
        continue

    # Aplicar el trade
    pnl_frac = row["pnl_pct"] / 100.0
    capital  *= (1 + pnl_frac)
    current_exit = row["exit_ts"]
    equity_curve.append(capital)
    executed.append(row)

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

# Hitos año a año
print(f"\n  Evolución por año:")
year_start = df_trades["entry_ts"].min().year
for yr in range(year_start, year_start + (DAYS // 365) + 1):
    yr_trades = executed_df[
        (executed_df["entry_ts"].dt.year < yr + 1)
    ]
    if len(yr_trades) == 0:
        continue
    # capital al final de ese año
    cap_yr = CAPITAL
    for _, r in yr_trades.iterrows():
        if r["entry_ts"].year <= yr:
            cap_yr *= (1 + r["pnl_pct"] / 100)
    print(f"    {yr}: €{cap_yr:.2f}  ({(cap_yr/CAPITAL - 1)*100:+.1f}%)")
