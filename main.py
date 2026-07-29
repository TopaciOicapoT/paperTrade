"""
main.py
-------
Punto de entrada principal del sistema de trading automatizado.

Comandos disponibles:
  python main.py backtest   — Simula la estrategia en datos históricos
  python main.py train      — Descarga histórico y entrena el modelo XGBoost
  python main.py paper      — Inicia el paper trader en tiempo real (Binance Testnet)
  python main.py levels     — Muestra los niveles clave actuales de cada par
"""

import sys
import yaml
import argparse
import pandas as pd
from pathlib import Path
from loguru import logger

CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"


def cargar_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────
# Comando: backtest
# ─────────────────────────────────────────────────
def cmd_backtest(args, config: dict):
    from data.fetcher import get_data_exchange, fetch_ohlcv
    from backtesting.engine import simular_trades, imprimir_resumen, comparar_resultados

    symbol = args.symbol or config["symbols"][0]
    exchange = get_data_exchange()  # Datos reales siempre para backtest

    logger.info(f"Iniciando backtest de {symbol} ({args.days} días)...")

    monthly_lookback = config["levels"]["monthly_lookback"]
    extra_days = monthly_lookback * 30 + 30

    df_5m = fetch_ohlcv(symbol, "5m", limit=args.days * 24 * 12, exchange=exchange)
    df_1d = fetch_ohlcv(symbol, "1d", limit=args.days + extra_days, exchange=exchange)
    df_1w = fetch_ohlcv(symbol, "1w", limit=52, exchange=exchange)

    common_params = dict(
        df_entry=df_5m,
        df_daily=df_1d,
        df_weekly=df_1w,
        symbol=symbol,
        monthly_lookback=monthly_lookback,
        tp_pct=config["risk"]["take_profit_pct"],
        sl_behind_pct=config["risk"]["sl_behind_level_pct"],
        volume_ratio_min=config["levels"]["volume_trigger_ratio"],
        volume_ratio_max=config["levels"].get("volume_trigger_ratio_max", 3.0),
        fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
        ml_threshold=config["model"]["probability_threshold"] if args.ml else 0.0,
        volatility_filter=args.vol_filter,
        leverage=args.leverage,
        initial_capital=args.capital,
        symbol_params=config.get("symbol_params"),  # overrides por símbolo
        rsi_overbought_block=config.get("rsi_overbought_block"),
    )

    if args.compare:
        # Correr ambos escenarios con los mismos datos (sin descargar dos veces)
        logger.info("Simulando SIN filtro failed retest (baseline)...")
        result_sin = simular_trades(**common_params, failed_retest_filter=False)

        logger.info("Simulando CON filtro failed retest...")
        result_con = simular_trades(**common_params, failed_retest_filter=True)

        imprimir_resumen(result_sin, label="SIN failed retest")
        imprimir_resumen(result_con, label="CON failed retest")
        comparar_resultados(result_sin, result_con)
    else:
        result = simular_trades(
            **common_params,
            failed_retest_filter=not args.no_failed_retest,
        )
        imprimir_resumen(result)


# ─────────────────────────────────────────────────
# Comando: bounce (backtest estrategia reversión)
# ─────────────────────────────────────────────────
def cmd_bounce(args, config: dict):
    from data.fetcher import get_data_exchange, fetch_ohlcv
    from backtesting.engine import simular_trades, simular_trades_bounce, imprimir_resumen

    symbols = [args.symbol] if args.symbol else config["symbols"]
    exchange = get_data_exchange()
    monthly_lookback = config["levels"]["monthly_lookback"]
    extra_days = monthly_lookback * 30 + 30

    for symbol in symbols:
        logger.info(f"Simulando bounce en {symbol} ({args.days} días)...")
        df_5m = fetch_ohlcv(symbol, "5m", limit=args.days * 24 * 12, exchange=exchange)
        # +200 días extra para la MA200 que usa el filtro de tendencia
        df_1d = fetch_ohlcv(symbol, "1d", limit=args.days + extra_days + 200, exchange=exchange)

        bounce = simular_trades_bounce(
            df_entry=df_5m,
            df_daily=df_1d,
            symbol=symbol,
            config=config,
            sl_behind_pct=config["risk"]["sl_behind_level_pct"],
            fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
        )

        # Si también se pide comparativa con breakout
        if args.compare:
            df_1w = fetch_ohlcv(symbol, "1w", limit=52, exchange=exchange)
            breakout = simular_trades(
                df_entry=df_5m,
                df_daily=df_1d,
                df_weekly=df_1w,
                symbol=symbol,
                monthly_lookback=monthly_lookback,
                tp_pct=config["risk"]["take_profit_pct"],
                sl_behind_pct=config["risk"]["sl_behind_level_pct"],
                volume_ratio_min=config["levels"]["volume_trigger_ratio"],
                volume_ratio_max=config["levels"].get("volume_trigger_ratio_max", 3.0),
                fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
            )
            logger.info(f"\n{'═'*55}")
            logger.info(f"COMPARATIVA {symbol} — {args.days} días con fees")
            logger.info(f"{'═'*55}")
            logger.info(f"  {'Estrategia':<20} {'Trades':>6} {'WR':>7} {'Retorno':>10} {'Max DD':>9}")
            logger.info(f"  {'-'*54}")
            logger.info(
                f"  {'Breakout (momentum)':<20} {breakout.total_trades:>6} "
                f"{breakout.win_rate:>7.1%} {breakout.total_return_pct:>+9.2f}% "
                f"{breakout.max_drawdown_pct:>+8.2f}%"
            )
            logger.info(
                f"  {'Bounce (mean-rev.)':<20} {bounce.total_trades:>6} "
                f"{bounce.win_rate:>7.1%} {bounce.total_return_pct:>+9.2f}% "
                f"{bounce.max_drawdown_pct:>+8.2f}%"
            )
            logger.info(f"{'═'*55}")
        else:
            logger.info(f"\nBOUNCE RESULTADO: {symbol}")
            imprimir_resumen(bounce)


# ─────────────────────────────────────────────────
# Comando: analyze
# ─────────────────────────────────────────────────
def cmd_analyze(args, config: dict):
    from data.fetcher import get_data_exchange, fetch_ohlcv
    from backtesting.engine import simular_trades, imprimir_resumen, analizar_fallos, analizar_combinado

    symbols = [args.symbol] if args.symbol else config["symbols"]
    exchange = get_data_exchange()
    monthly_lookback = config["levels"]["monthly_lookback"]
    extra_days = monthly_lookback * 30 + 30
    resultados = []

    for symbol in symbols:
        logger.info(f"Analizando {symbol} ({args.days} días)...")
        df_5m = fetch_ohlcv(symbol, "5m", limit=args.days * 24 * 12, exchange=exchange)
        df_1d = fetch_ohlcv(symbol, "1d", limit=args.days + extra_days, exchange=exchange)
        df_1w = fetch_ohlcv(symbol, "1w", limit=52, exchange=exchange)

        result = simular_trades(
            df_entry=df_5m,
            df_daily=df_1d,
            df_weekly=df_1w,
            symbol=symbol,
            monthly_lookback=monthly_lookback,
            tp_pct=config["risk"]["take_profit_pct"],
            sl_behind_pct=config["risk"]["sl_behind_level_pct"],
            volume_ratio_min=config["levels"]["volume_trigger_ratio"],
            volume_ratio_max=config["levels"].get("volume_trigger_ratio_max", 3.0),
            fee_pct=config.get("paper_trading", {}).get("fee_pct", 0.1),
            ml_threshold=config["model"]["probability_threshold"] if args.ml else 0.0,
            volatility_filter=getattr(args, "vol_filter", False),
            leverage=getattr(args, "leverage", 1),
        )
        imprimir_resumen(result)
        analizar_fallos(result)
        resultados.append(result)

    # Análisis global combinado solo cuando se procesan múltiples símbolos
    if len(resultados) > 1:
        analizar_combinado(resultados)


# ─────────────────────────────────────────────────
# Comando: train
# ─────────────────────────────────────────────────
def cmd_train(args, config: dict):
    from data.fetcher import get_data_exchange, fetch_ohlcv
    from indicators.technical import calcular_features
    from indicators.levels import calcular_niveles_mensuales
    from models.trainer import etiquetar_breakouts, preparar_dataset, entrenar_modelo
    import pandas as pd

    # Si se pasa --symbol, entrenar solo ese; si no, usar todos los del config
    symbols = [args.symbol] if args.symbol else config["symbols"]
    exchange = get_data_exchange()

    monthly_lookback = config["levels"]["monthly_lookback"]
    tp_pct = config["risk"]["take_profit_pct"]
    sl_behind = config["risk"]["sl_behind_level_pct"]

    all_X = []
    all_y = []

    for symbol in symbols:
        logger.info(f"── Procesando {symbol} ──")
        try:
            df_5m = fetch_ohlcv(symbol, "5m", limit=4000, exchange=exchange)
            df_1d = fetch_ohlcv(symbol, "1d", limit=monthly_lookback * 30 + 60, exchange=exchange)

            df_features = calcular_features(df_5m)

            logger.disable("indicators.levels")
            monthly = calcular_niveles_mensuales(df_1d, lookback_months=monthly_lookback)
            logger.enable("indicators.levels")

            labels_long = etiquetar_breakouts(
                df_features, direction="long",
                level_price=monthly.resistance,
                reward_ratio=tp_pct / sl_behind,
                sl_pct=sl_behind,
            )
            labels_short = etiquetar_breakouts(
                df_features, direction="short",
                level_price=monthly.support,
                reward_ratio=tp_pct / sl_behind,
                sl_pct=sl_behind,
            )

            if monthly.broke_resistance:
                labels = labels_long
            elif monthly.broke_support:
                labels = labels_short
            else:
                labels = labels_long.fillna(labels_short)

            X, y = preparar_dataset(df_features, labels)
            all_X.append(X)
            all_y.append(y)
            logger.info(f"  {symbol}: {len(X)} muestras etiquetadas")

        except Exception as e:
            logger.error(f"  Error procesando {symbol}: {e}")
            continue

    if not all_X:
        logger.error("No se pudieron obtener datos de ningún símbolo")
        return

    import numpy as np
    X_combined = pd.concat(all_X, ignore_index=True)
    y_combined = pd.concat(all_y, ignore_index=True)

    total = len(X_combined)
    wins = int(y_combined.sum())
    logger.info(
        f"Dataset combinado: {total} muestras de {len(symbols)} símbolos "
        f"| Tasa éxito: {wins/total*100:.1f}%"
    )

    if total < config["model"]["min_training_samples"]:
        logger.warning(
            f"Solo {total} muestras (mínimo recomendado: "
            f"{config['model']['min_training_samples']})"
        )

    logger.info(f"Entrenando modelo XGBoost con {total} muestras...")
    model, scaler, metrics = entrenar_modelo(X_combined, y_combined)

    logger.success(
        f"Entrenamiento completado | "
        f"AUC-ROC: {metrics['auc_roc']} | "
        f"Accuracy: {metrics.get('accuracy', 'N/A')} | "
        f"Muestras: {metrics['n_samples']} ({len(symbols)} símbolos)"
    )


# ─────────────────────────────────────────────────
# Comando: paper
# ─────────────────────────────────────────────────
def cmd_paper(args, config: dict):
    from execution.paper_trader import PaperTrader

    trader = PaperTrader(config)
    logger.info("Iniciando paper trading... (Ctrl+C para detener)")
    try:
        trader.ejecutar(interval_seconds=60)
    except KeyboardInterrupt:
        logger.info("Paper trader detenido por el usuario")
        logger.info(f"Balance final: {trader.balance_usdt:.2f} USDT")
        logger.info(f"Total operaciones: {len(trader.trade_log)}")
        sys.exit(0)  # salida limpia — start_bot.ps1 no reiniciará


# ─────────────────────────────────────────────────
# Comando: levels
# ─────────────────────────────────────────────────
def cmd_levels(args, config: dict):
    from data.fetcher import get_data_exchange, fetch_ohlcv
    from indicators.levels import calcular_niveles_mensuales

    exchange = get_data_exchange()  # Datos reales siempre (públicos, sin API key)
    lookback = config["levels"]["monthly_lookback"]

    for symbol in config["symbols"]:
        df_1d = fetch_ohlcv(symbol, "1d", limit=lookback * 30 + 10, exchange=exchange)
        levels = calcular_niveles_mensuales(df_1d, lookback_months=lookback)

        dist_res = abs(levels.current_price - levels.resistance) / levels.resistance * 100
        dist_sup = abs(levels.current_price - levels.support) / levels.support * 100

        estado = "DENTRO DEL RANGO"
        if levels.broke_resistance:
            estado = ">>> ROMPIÓ RESISTENCIA MENSUAL (señal LONG potencial)"
        elif levels.broke_support:
            estado = "<<< ROMPIÓ SOPORTE MENSUAL (señal SHORT potencial)"
        elif levels.near_resistance:
            estado = "⚡ Cerca de resistencia — vigilar rotura"
        elif levels.near_support:
            estado = "⚡ Cerca de soporte — vigilar rotura"

        print(f"\n{'='*50}")
        print(f"  {symbol} — Niveles mensuales ({lookback} meses)")
        print(f"{'='*50}")
        print(f"  Precio actual:        {levels.current_price:.4f}")
        print(f"  Resistencia mensual:  {levels.resistance:.4f}  (+{dist_res:.2f}%)")
        print(f"  Soporte mensual:      {levels.support:.4f}  (-{dist_sup:.2f}%)")
        print(f"\n  Estado: {estado}")


# ─────────────────────────────────────────────────
# Entrada principal
# ─────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot de trading automatizado con IA")
    subparsers = parser.add_subparsers(dest="command")

    # backtest
    p_bt = subparsers.add_parser("backtest", help="Simula la estrategia en datos históricos")
    p_bt.add_argument("--symbol", default=None, help="Par a backtestear (ej. BTC/USDT)")
    p_bt.add_argument("--days", type=int, default=30, help="Días de historia (default 30)")
    p_bt.add_argument("--ml", action="store_true", help="Activar filtro ML en el backtest")
    p_bt.add_argument("--vol-filter", action="store_true",
                      help="Simular news circuit breaker: saltar días con volatilidad >3σ")
    p_bt.add_argument("--leverage", type=int, default=1,
                      help="Apalancamiento para simular futuros (default 1 = spot)")
    p_bt.add_argument("--capital", type=float, default=1000.0,
                      help="Capital inicial en $ para mostrar curva de equity (default 1000)")
    p_bt.add_argument("--no-failed-retest", action="store_true",
                      help="Desactiva el filtro failed retest (muestra baseline sin el filtro)")
    p_bt.add_argument("--compare", action="store_true",
                      help="Compara CON y SIN filtro failed retest en paralelo")

    # analyze
    p_an = subparsers.add_parser("analyze", help="Analiza patrones de fallos en la estrategia")
    p_an.add_argument("--symbol", default=None, help="Par a analizar (todos si se omite)")
    p_an.add_argument("--days", type=int, default=365, help="Días de historia (default 365)")
    p_an.add_argument("--ml", action="store_true", help="Activar filtro ML en el análisis")
    p_an.add_argument("--vol-filter", action="store_true",
                      help="Simular news circuit breaker: saltar días con volatilidad >3σ")

    # bounce
    p_bo = subparsers.add_parser("bounce", help="Backtest de estrategia de rebote en nivel mensual")
    p_bo.add_argument("--symbol", default=None, help="Par a simular (todos si se omite)")
    p_bo.add_argument("--days", type=int, default=365, help="Días de historia (default 365)")
    p_bo.add_argument("--compare", action="store_true", help="Comparar con la estrategia de breakout")

    # train
    p_tr = subparsers.add_parser("train", help="Entrena el modelo XGBoost")
    p_tr.add_argument("--symbol", default=None, help="Par para extraer datos de entrenamiento")

    # paper
    subparsers.add_parser("paper", help="Inicia el paper trader en Binance Testnet")

    # levels
    subparsers.add_parser("levels", help="Muestra los niveles clave actuales")

    args = parser.parse_args()
    config = cargar_config()

    commands = {
        "backtest": cmd_backtest,
        "analyze":  cmd_analyze,
        "bounce":   cmd_bounce,
        "train": cmd_train,
        "paper": cmd_paper,
        "levels": cmd_levels,
    }

    if args.command not in commands:
        parser.print_help()
        sys.exit(1)

    commands[args.command](args, config)
