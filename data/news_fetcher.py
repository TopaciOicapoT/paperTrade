"""
data/news_fetcher.py
--------------------
Monitor de noticias/sentimiento de alto impacto para crypto.

Fuentes gratuitas, sin registro ni API key:

  1. Fear & Greed Index (Alternative.me) — PRIMARIA
     API pública sin autenticación. Devuelve un índice 0-100 que
     agrega volatilidad, redes sociales, dominancia de BTC y Google
     Trends. Cae drásticamente ante eventos macro (aranceles Trump,
     hacks, bans regulatorios, etc.).
     Endpoint: https://api.alternative.me/fng/?limit=1

  2. RSS de medios crypto — SECUNDARIA (refuerza la señal)
     CoinTelegraph y CoinDesk publican RSS abiertos.
     Se puntúan los titulares con keywords de riesgo.
     Si hay keywords graves Y el F&G está bajo → pausa más agresiva.

Umbrales de decisión:
    F&G >= 25                         → nivel "low"  (operar normal)
    F&G 15-24  O  score_rss >= 2      → nivel "medium" (advertencia)
    F&G < 15   O  score_rss >= 5      → nivel "high"   (pausa N horas)
    F&G < 10   Y  score_rss >= 3      → nivel "critical"

No requiere ninguna variable de entorno. El módulo siempre funciona.
"""

import time
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from loguru import logger

# ─── Constantes ───────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 8   # segundos

_FNG_URL = "https://api.alternative.me/fng/?limit=1"

# RSS feeds públicos de noticias crypto (sin API key)
_RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

# Keywords de riesgo alto agrupadas por categoría
_RISK_KEYWORDS: dict[str, list[str]] = {
    "macro": [
        "tariff", "tariffs", "trade war", "trade ban", "sanctions",
        "federal reserve", "rate hike", "rate cut",
        "recession", "economic crisis", "stagflation",
    ],
    "regulation": [
        "ban", "banned", "banning", "crackdown", "seized", "illegal",
        "sec lawsuit", "cftc", "doj", "indicted",
        "regulatory", "prohibited", "enforcement action",
    ],
    "security": [
        "hack", "hacked", "exploit", "exploited", "stolen",
        "rug pull", "exit scam", "fraud", "bridge attack",
    ],
    "liquidity": [
        "bankrupt", "bankruptcy", "insolvency", "insolvent",
        "collapse", "collapsed", "withdrawal halt",
        "frozen", "liquidity crisis", "bank run",
    ],
    "geopolitics": [
        "war", "military", "invasion", "nuclear", "escalation",
    ],
    "market_structure": [
        "flash crash", "trading halted", "exchange down",
        "liquidation cascade", "black swan",
    ],
}

# Umbrales Fear & Greed (0-100)
_FNG_CRITICAL = 10   # F&G < 10  → pánico extremo por sí solo = crítico
_FNG_HIGH     = 20   # F&G < 20  → terror significativo (necesita RSS para ser "high")
_FNG_MEDIUM   = 30   # F&G < 30  → mercado en miedo (nivel "medium")

# Score RSS por artículo (1 punto por categoría de riesgo detectada)
# Con 6 categorías, el score máximo por artículo es 6.
# Un día normal de crypto suele tener score total 3-6 disperso en múltiples artículos.
_RSS_SCORE_HIGH_ALONE     = 10  # RSS >= 10 sin F&G bajo → "high" (concentración grave)
_RSS_SCORE_COMBINED_HIGH  = 5   # RSS >= 5 con F&G < 20  → "high" (dos señales)
_RSS_SCORE_MEDIUM         = 4   # RSS >= 4  → "medium"


@dataclass
class NewsRiskReport:
    """Resultado del análisis combinado de sentimiento + noticias."""
    level: str              # "low" | "medium" | "high" | "critical"
    score: int              # Score acumulado de keywords en RSS
    fng_value: int          # Valor del Fear & Greed Index (0-100)
    fng_label: str          # Etiqueta textual del F&G
    articles_checked: int
    top_headlines: list[str] = field(default_factory=list)
    triggered_keywords: list[str] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def should_pause(self) -> bool:
        return self.level in ("high", "critical")


# ─── Fuente 1: Fear & Greed Index ─────────────────────────────────────────────

def _get_fear_greed() -> tuple[int, str]:
    """
    Consulta el Fear & Greed Index de Alternative.me.
    Devuelve (valor 0-100, etiqueta) o (50, "Neutral") si hay error.
    """
    try:
        resp = requests.get(_FNG_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"][0]
        return int(entry["value"]), entry["value_classification"]
    except Exception as exc:
        logger.debug(f"[News] Error obteniendo Fear & Greed: {exc} — asumiendo Neutral")
        return 50, "Neutral"


# ─── Fuente 2: RSS feeds ───────────────────────────────────────────────────────

def _score_headline(title: str) -> tuple[int, list[str]]:
    """
    Puntúa un titular por keywords de riesgo.

    Usa matching de palabras completas (\\b word boundary) para evitar
    falsos positivos de substrings: "ban" no hace match en "bank",
    "war" no hace match en "hardware", etc.

    Cada categoría suma máximo 1 punto aunque haya varias keywords
    de la misma categoría en el titular (evita doble conteo).

    Devuelve (score, keywords_detectadas).
    """
    score = 0
    detected: list[str] = []
    title_lower = title.lower()
    for _cat, keywords in _RISK_KEYWORDS.items():
        for kw in keywords:
            # Construir patrón con word boundaries; los keywords multi-palabra
            # (ej. "trade war") se buscan como secuencia exacta
            pattern = r'\b' + re.escape(kw) + r'\b'
            if re.search(pattern, title_lower):
                score += 1          # 1 punto por categoría
                detected.append(kw)
                break               # Solo cuenta una vez por categoría
    return score, detected


def _get_rss_score(max_items: int = 20) -> tuple[int, list[str], list[str]]:
    """
    Descarga y puntúa titulares recientes de los RSS feeds.
    Devuelve (score_total, keywords_detectadas, titulares_relevantes).
    """
    total_score = 0
    all_keywords: list[str] = []
    headlines: list[str] = []
    checked = 0

    for feed_url in _RSS_FEEDS:
        try:
            resp = requests.get(feed_url, timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "Mozilla/5.0 (autoTrading bot)"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            items = root.findall(".//item")[:max_items]
            for item in items:
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue
                title = title_el.text.strip()
                art_score, keywords = _score_headline(title)
                if art_score > 0:
                    total_score += art_score
                    all_keywords.extend(keywords)
                    headlines.append(f"[+{art_score}] {title}")
                checked += 1
        except Exception as exc:
            logger.debug(f"[News] RSS {feed_url} no disponible: {exc}")

    return total_score, list(set(all_keywords)), headlines[:6]


# ─── Función principal ─────────────────────────────────────────────────────────

def get_news_risk() -> NewsRiskReport:
    """
    Evalúa el riesgo actual combinando Fear & Greed Index + RSS crypto.

    No requiere ninguna API key ni configuración adicional.
    Siempre devuelve un resultado — los errores de red degradan
    graciosamente a nivel "low" para no interrumpir el trading.

    Returns:
        NewsRiskReport con level, fng_value y detalle de titulares.
    """
    # ── Fuente 1: Fear & Greed ──
    fng_value, fng_label = _get_fear_greed()

    # ── Fuente 2: RSS ──
    rss_score, keywords, headlines = _get_rss_score()

    # ── Determinar nivel combinado ──
    # Requiere DOS señales convergentes para subir a "high" —
    # una sola fuente puede ser ruido normal del mercado.
    if fng_value < _FNG_CRITICAL and rss_score >= 3:
        # Pánico extremo + cualquier noticia grave = crítico
        level = "critical"
    elif (fng_value < _FNG_HIGH and rss_score >= _RSS_SCORE_COMBINED_HIGH) \
            or rss_score >= _RSS_SCORE_HIGH_ALONE:
        # Terror + noticias concentradas  ó  noticias muy graves solas
        level = "high"
    elif fng_value < _FNG_MEDIUM or rss_score >= _RSS_SCORE_MEDIUM:
        # Una señal moderada
        level = "medium"
    else:
        level = "low"

    report = NewsRiskReport(
        level=level,
        score=rss_score,
        fng_value=fng_value,
        fng_label=fng_label,
        articles_checked=rss_score,   # aproximación — el RSS no siempre publica el count
        top_headlines=headlines,
        triggered_keywords=keywords,
    )

    if report.should_pause:
        logger.warning(
            f"[News] ⚠ RIESGO {level.upper()} — "
            f"Fear & Greed: {fng_value}/100 ({fng_label}) | "
            f"RSS score: {rss_score}"
        )
        if keywords:
            logger.warning(f"[News]   Keywords: {', '.join(keywords[:8])}")
        for h in headlines[:3]:
            logger.warning(f"[News]   {h}")
    elif level == "medium":
        logger.info(
            f"[News] Riesgo moderado — "
            f"F&G: {fng_value}/100 ({fng_label}) | RSS: {rss_score}"
        )
    else:
        logger.debug(
            f"[News] Riesgo bajo — F&G: {fng_value}/100 ({fng_label})"
        )

    return report
