"""
data/fetcher.py
---------------
Descarga datos OHLCV de Binance usando ccxt.
Soporta múltiples marcos temporales y cachea los datos localmente.
"""

import os
import time
import ccxt
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

_MAX_RETRIES = 3
_RETRY_BACKOFF = [5, 15, 30]  # segundos entre reintentos

load_dotenv()

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# Duración en segundos de cada timeframe (para calcular el timestamp de inicio)
_TF_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200,
    "1d": 86400, "3d": 259200, "1w": 604800, "1M": 2592000,
}
_BINANCE_MAX_CANDLES = 1000  # límite por request de Binance


def get_exchange(testnet: bool = True) -> ccxt.binance:
    """
    Crea la instancia del exchange para EJECUTAR ÓRDENES.
    testnet=True  → Binance Testnet (paper trading, fondos simulados)
    testnet=False → Binance Real (dinero real, solo cuando estés listo)
    """
    exchange = ccxt.binance({
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_SECRET_KEY"),
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })

    if testnet:
        exchange.set_sandbox_mode(True)
        logger.info("Conectado a Binance TESTNET (paper trading)")
    else:
        logger.warning("Conectado a Binance REAL — ¡cuidado!")

    return exchange


def get_data_exchange() -> ccxt.binance:
    """
    Instancia de Binance para OBTENER DATOS DE PRECIO (sin API key).
    Siempre usa Binance real para tener precios correctos.
    Los datos OHLCV son públicos — no requieren autenticación.
    """
    exchange = ccxt.binance({"enableRateLimit": True})
    logger.debug("Exchange de datos: Binance real (público)")
    return exchange


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int = 500,
    exchange: ccxt.binance | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Descarga velas OHLCV para un símbolo y marco temporal.

    Args:
        symbol:    Par de trading, ej. "BTC/USDT"
        timeframe: Marco temporal, ej. "1m", "5m", "1d", "1w"
        limit:     Número de velas a descargar
        exchange:  Instancia de ccxt (se crea si no se pasa)
        use_cache: Si True, usa caché local para datos históricos

    Returns:
        DataFrame con columnas: timestamp, open, high, low, close, volume
    """
    cache_file = CACHE_DIR / f"{symbol.replace('/', '_')}_{timeframe}_{limit}.pkl"

    if use_cache and cache_file.exists():
        age_minutes = (time.time() - cache_file.stat().st_mtime) / 60
        # Refrescar caché si tiene más de 5 minutos (para timeframes cortos)
        max_age = 5 if timeframe in ("1m", "5m") else 60
        if age_minutes < max_age:
            logger.debug(f"Cargando desde caché: {cache_file.name}")
            return pd.read_pickle(cache_file)

    if exchange is None:
        exchange = get_exchange(testnet=True)

    logger.info(f"Descargando {limit} velas {timeframe} de {symbol}...")

    tf_secs = _TF_SECONDS.get(timeframe, 60)
    since_ms = int((time.time() - tf_secs * limit) * 1000)

    all_raw: list = []
    while len(all_raw) < limit:
        batch_limit = min(_BINANCE_MAX_CANDLES, limit - len(all_raw))

        for attempt, wait in enumerate([0] + _RETRY_BACKOFF, start=1):
            try:
                if wait:
                    logger.warning(f"Reintento {attempt}/{_MAX_RETRIES} para {symbol} {timeframe} en {wait}s...")
                    time.sleep(wait)
                batch = exchange.fetch_ohlcv(
                    symbol, timeframe=timeframe, since=since_ms, limit=batch_limit
                )
                break
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                if attempt > _MAX_RETRIES:
                    raise
                logger.warning(f"Error de red ({e}), reintentando...")

        if not batch:
            break
        all_raw.extend(batch)
        since_ms = batch[-1][0] + 1  # siguiente vela tras la última recibida
        if len(batch) < batch_limit:
            break  # no hay más datos disponibles

    raw = all_raw[-limit:]  # tomar solo las últimas `limit` velas

    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df.astype(float)

    if use_cache:
        df.to_pickle(cache_file)

    return df


def fetch_multi_timeframe(
    symbol: str,
    timeframes: list[str],
    limits: dict[str, int] | None = None,
    exchange: ccxt.binance | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Descarga datos para múltiples marcos temporales en una sola llamada.

    Args:
        symbol:     Par de trading
        timeframes: Lista de marcos, ej. ["1m", "5m", "1d", "1w"]
        limits:     Número de velas por marco, ej. {"1m": 500, "1d": 90}
        exchange:   Instancia de ccxt compartida

    Returns:
        Diccionario {timeframe: DataFrame}
    """
    default_limits = {"1m": 500, "5m": 300, "1d": 90, "1w": 52}
    limits = {**default_limits, **(limits or {})}

    if exchange is None:
        exchange = get_exchange(testnet=True)

    result = {}
    for tf in timeframes:
        result[tf] = fetch_ohlcv(
            symbol=symbol,
            timeframe=tf,
            limit=limits.get(tf, 500),
            exchange=exchange,
        )

    return result
