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


def _download_since(
    symbol: str,
    timeframe: str,
    since_ms: int,
    exchange: ccxt.binance,
    max_candles: int | None = None,
) -> list:
    """Descarga todas las velas disponibles desde since_ms hasta ahora."""
    tf_secs = _TF_SECONDS.get(timeframe, 60)
    all_raw: list = []
    cursor_ms = since_ms
    while True:
        batch_limit = min(_BINANCE_MAX_CANDLES, max_candles - len(all_raw)) if max_candles else _BINANCE_MAX_CANDLES
        if batch_limit <= 0:
            break
        for attempt, wait in enumerate([0] + _RETRY_BACKOFF, start=1):
            try:
                if wait:
                    time.sleep(wait)
                batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor_ms, limit=batch_limit)
                break
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                if attempt > _MAX_RETRIES:
                    raise
                logger.warning(f"Reintento {attempt} para {symbol} {timeframe}: {e}")
        if not batch:
            break
        all_raw.extend(batch)
        cursor_ms = batch[-1][0] + 1
        if len(batch) < _BINANCE_MAX_CANDLES:
            break  # no hay más datos
    return all_raw


def _raw_to_df(raw: list) -> pd.DataFrame:
    """Convierte lista de candles raw a DataFrame con índice temporal."""
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df.astype(float)


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int = 500,
    exchange: ccxt.binance | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Descarga velas OHLCV con caché incremental persistente.

    El archivo de caché almacena TODO el histórico disponible para el símbolo
    y timeframe. En llamadas posteriores solo se descargan las velas nuevas,
    haciendo la segunda simulación casi instantánea.
    """
    # Caché persistente sin el limit en el nombre — se extiende automáticamente
    cache_file = CACHE_DIR / f"{symbol.replace('/', '_')}_{timeframe}.pkl"
    tf_secs = _TF_SECONDS.get(timeframe, 60)

    if exchange is None:
        exchange = get_data_exchange()

    now_ms = int(time.time() * 1000)
    need_since_ms = int((time.time() - tf_secs * (limit + 100)) * 1000)

    cached_df: pd.DataFrame | None = None
    if use_cache and cache_file.exists():
        try:
            cached_df = pd.read_pickle(cache_file)
            if len(cached_df) == 0:
                cached_df = None
        except Exception:
            cached_df = None

    if cached_df is not None:
        cache_start_ms = int(cached_df.index[0].timestamp() * 1000)
        cache_end_ms   = int(cached_df.index[-1].timestamp() * 1000)

        # ¿Necesitamos datos más antiguos que los que tenemos en caché?
        if cache_start_ms > need_since_ms + tf_secs * 1000 * 10:
            logger.info(f"[Cache] {symbol} {timeframe} — extendiendo hacia atrás hasta {pd.Timestamp(need_since_ms, unit='ms', tz='UTC').date()}")
            older_raw = _download_since(symbol, timeframe, need_since_ms, exchange,
                                        max_candles=(cache_start_ms - need_since_ms) // (tf_secs * 1000) + 200)
            if older_raw:
                older_df = _raw_to_df([r for r in older_raw if r[0] < cache_start_ms])
                if len(older_df):
                    cached_df = pd.concat([older_df, cached_df])
                    cached_df = cached_df[~cached_df.index.duplicated(keep="last")].sort_index()

        # Descargar candles nuevas desde el final del caché
        stale_secs = (now_ms - cache_end_ms) / 1000
        if stale_secs > tf_secs * 2:
            logger.info(f"[Cache] {symbol} {timeframe} — actualizando {stale_secs/3600:.1f}h de datos nuevos")
            new_raw = _download_since(symbol, timeframe, cache_end_ms + 1, exchange)
            if new_raw:
                new_df = _raw_to_df(new_raw)
                cached_df = pd.concat([cached_df, new_df])
                cached_df = cached_df[~cached_df.index.duplicated(keep="last")].sort_index()
        else:
            logger.debug(f"[Cache] {symbol} {timeframe} — datos al día ({stale_secs/60:.0f} min de retraso)")

        if use_cache:
            cached_df.to_pickle(cache_file)

        return cached_df.tail(limit)

    # Sin caché — descarga completa desde need_since_ms
    logger.info(f"Descargando historial completo {symbol} {timeframe} (primera vez)…")
    raw = _download_since(symbol, timeframe, need_since_ms, exchange)
    df = _raw_to_df(raw)
    if use_cache and len(df):
        df.to_pickle(cache_file)
    return df.tail(limit)


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
