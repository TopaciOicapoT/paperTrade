# autoTrading — Bot de Trading Automatizado con IA

Bot de breakout de niveles mensuales para criptomonedas. Detecta cuando el precio rompe la resistencia o soporte del mes anterior con spike de volumen y entra en la dirección del breakout.

- **Estrategia**: breakout de máximos/mínimos mensuales + confirmación multi-timeframe + volumen 2.0-3.0× (ajustado por símbolo)
- **Anti-fakeout adaptativo**: filtro *failed retest* con auto-detección de régimen — mide el tamaño del rebote reciente por símbolo y calibra el umbral automáticamente cada ciclo
- **Cartera validada a 6 años**: 4 símbolos con 4 filtros per-símbolo (ADA, LINK, EGLD, ATOM de 26 probados) | €100 → €7,126 (+7,026%) en simulación compartida
- **Exchange**: Binance (datos reales públicos) / Binance Testnet (paper trading) → Bybit (futuro, trading real)
- **TP**: 3% desde entrada | **SL**: 1% desde entrada | **R:R**: 3:1
- **4 filtros per-símbolo**: F1 momentum, F2b sesión UTC, F3 volumen USDT Q3, F4 RSI14 sobrecompra
- **ML**: XGBoost para filtrar señales — mejora con cada semana de trades reales acumulados
- **Resiliencia**: estado persistido en disco, auto-reinicio ante crashes, circuit breaker diario
- **News circuit breaker**: pausa automática ante eventos macro sin API key (Fear & Greed + RSS)
- **Futuros**: soporte para USDT-M perpetuos con leverage configurable (default 3x)

---

## Requisitos

- Python 3.11+
- Cuenta en [Binance Testnet](https://testnet.binance.vision/) para el paper trader

---

## Instalación

```powershell
# Clonar / abrir carpeta del proyecto
cd C:\Users\Marcos\Desktop\web\autoTrading

# Crear entorno virtual
python -m venv .venv

# Activar entorno
.\.venv\Scripts\Activate.ps1

# Instalar dependencias
pip install -r requirements.txt --index-url https://pypi.org/simple/
```

### Configurar credenciales

```powershell
Copy-Item .env.example .env
```

Editar `.env` con las API keys de Binance Testnet:

```
BINANCE_API_KEY=tu_api_key_testnet_aqui
BINANCE_SECRET_KEY=tu_secret_key_testnet_aqui
```

> Las API keys de testnet spot se obtienen en https://testnet.binance.vision/ (login con GitHub)

**Futuros testnet (si usas `futures.enabled: true`):**

La cuenta de futuros testnet es **separada** de la spot. Créala en [testnet.binancefuture.com](https://testnet.binancefuture.com) y actualiza las API keys en `.env`.

**News circuit breaker:**

Ya está activo por defecto. Usa el [Fear & Greed Index de Alternative.me](https://alternative.me/crypto/fear-and-greed-index/) y RSS de CoinTelegraph/CoinDesk — **sin API key ni registro**. Si quieres desactivarlo:

```yaml
news:
  enabled: false
```

---

## Comandos

Todos los comandos se ejecutan desde la raíz del proyecto con el entorno activado.

### Ver niveles actuales

```powershell
.\.venv\Scripts\python.exe main.py levels
```

### Backtest

```powershell
# Un símbolo, últimos 365 días
.\.venv\Scripts\python.exe main.py backtest --symbol ADA/USDT --days 365

# Con filtro ML activo (requiere modelo entrenado)
.\.venv\Scripts\python.exe main.py backtest --symbol ADA/USDT --days 365 --ml

# Simular futuros 3x (multiplica PnL por apalancamiento)
.\.venv\Scripts\python.exe main.py backtest --symbol ADA/USDT --days 365 --leverage 3

# Filtro de volatilidad: saltar días con spike >3σ (proxy de eventos macro)
.\.venv\Scripts\python.exe main.py backtest --symbol ADA/USDT --days 365 --vol-filter

# Curva de equity en dólares — ver capital $1000 subir y bajar trade a trade
.\.venv\Scripts\python.exe main.py backtest --symbol ADA/USDT --days 365 --leverage 3 --capital 1000

# Comparar CON y SIN filtro failed retest (mismos datos, dos simulaciones)
.\.venv\Scripts\python.exe main.py backtest --symbol ADA/USDT --days 365 --leverage 3 --capital 1000 --compare

# Baseline sin filtro failed retest
.\.venv\Scripts\python.exe main.py backtest --symbol ADA/USDT --days 365 --leverage 3 --no-failed-retest
```

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `--symbol` | primer símbolo del config | Par a testear |
| `--days` | `30` | Días de historia |
| `--ml` | desactivado | Aplica el filtro XGBoost en la simulación |
| `--leverage` | `1` | Apalancamiento (1 = spot, 3 = futuros 3x, etc.) |
| `--vol-filter` | desactivado | Salta días con volatilidad anormal (proxy de noticias) |
| `--capital` | `1000` | Capital inicial en $ para mostrar curva de equity trade a trade |
| `--compare` | desactivado | Corre ambos escenarios (con/sin failed retest) y muestra tabla comparativa |
| `--no-failed-retest` | desactivado | Desactiva el filtro failed retest para ver el baseline |

### Análisis de fallos

Corre el backtest y además muestra un desglose de por qué fallaron los trades: volumen, distancia al nivel, dirección y velocidad de resolución. Cuando se ejecuta sin `--symbol` agrega todos los pares en un análisis combinado.

```powershell
# Un par
.\.venv\Scripts\python.exe main.py analyze --symbol ADA/USDT --days 365

# Todos los pares del config + análisis combinado
.\.venv\Scripts\python.exe main.py analyze --days 365
```

Los CSV con el detalle trade a trade se guardan en `backtesting/results/`.

### Backtest de rebote (bounce)

Segunda estrategia — detecta rebotes en el nivel mensual cuando el precio lo toca, penetra brevemente con la mecha y cierra por encima (soporte) o por debajo (resistencia). TP en el midpoint del rango.

```powershell
# Un símbolo
.\.venv\Scripts\python.exe main.py bounce --symbol ADA/USDT --days 365

# Todos los pares del config
.\.venv\Scripts\python.exe main.py bounce --days 365

# Comparar breakout vs bounce en el mismo gráfico de resultados
.\.venv\Scripts\python.exe main.py bounce --symbol ADA/USDT --days 365 --compare
```

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `--symbol` | todos | Par a simular |
| `--days` | `365` | Días de historia |
| `--compare` | desactivado | Muestra resultados de breakout y bounce juntos |

> La estrategia bounce está en fase de forward testing. En mercados tendenciales el SL se activa con frecuencia; funciona mejor en rangos laterales.

### Entrenar modelo ML

```powershell
# Todos los símbolos del config (recomendado)
.\.venv\Scripts\python.exe main.py train
```

El modelo se guarda en `models/saved/xgb_breakout.joblib`. **Reentrenar cada semana** mientras el paper trader acumula trades reales — el modelo mejora con cada ciclo.

### Paper trader

Opera en tiempo real contra Binance Testnet. Requiere las API keys en `.env`.

```powershell
# Iniciar con auto-reinicio ante crashes y arranque automático tras reboot
powershell -ExecutionPolicy Bypass -File .\start_bot.ps1
```

- Ciclo de escaneo: cada 60 segundos
- Detener limpiamente: `Ctrl+C` (no reinicia)
- Logs en tiempo real: `logs/paper_trading.log`
- Estado persistido: `logs/paper_state.json` (balance + posiciones abiertas)

**Ver estado en tiempo real:**

```powershell
# Últimas líneas del log
Get-Content logs\paper_trading.log -Tail 20 -Wait

# Balance y posiciones abiertas
.\.venv\Scripts\python.exe -c "import json; s=json.load(open('logs/paper_state.json')); print('Balance:', s['balance_usdt']); print('Abiertas:', list(s['open_orders'].keys())); print('Trades:', len(s['trade_log']))"
```

**Ciclo de mejora continua:**

```
paper trader corre → acumula trades → reentrenar cada semana → mejor modelo → repite
```

```powershell
# Reentrenar después de una semana
.\.venv\Scripts\python.exe main.py train
```

---

## Configuración

El archivo principal es `config/config.yaml`.

```yaml
# Pares activos — validados a 6 años (26 símbolos probados, 4 superan el filtro)
symbols:
  - "ADA/USDT"    # WR 32.9% (6yr+filtros) | PF 1.35 | $1k→$4,086 | F1+F3
  - "LINK/USDT"   # WR 31.2% (6yr+filtros) | PF 1.25 | $1k→$2,610 | F2b
  - "EGLD/USDT"   # WR 38.3% (6yr+filtros) | PF 1.71 | $1k→$3,712 | F2b+F3+F4(RSI)
  - "ATOM/USDT"   # WR 32.7% (6yr+filtros) | PF 1.34 | $1k→$1,806 | F2b+F3+F1+F4(RSI)
  # Portfolio €100 capital compartido 6yr: €7,126 (+7,026%) | WR 33.7% | MaxDD -66.3%

# Filtros per-símbolo (4 tipos validados con post-hoc simulation + backtest real)
# F1: Momentum 5 velas — bloquear zona trampa Q3 [+0.30%, +1.60%]
# F2b: Sesión UTC óptima — bloquear horas con WR históricamente bajo
# F3: Volumen USDT normalizado — bloquear zona trampa Q3 [2.1×, 2.7×]
# F4: RSI14 sobrecompra — bloquear entradas con RSI ≥ 70 (solo EGLD+ATOM)
symbol_params:
  "ADA/USDT":
    momentum_q3_block: [0.30, 1.60]      # F1
    usdt_norm_block_range: [2.1, 2.7]    # F3
  "LINK/USDT":
    failed_retest_filter: false           # Clean breaker histórico
    volume_trigger_ratio_max: 2.8
    session_block_hours: [8, 14]          # F2b: bloquear sesión europea
  "EGLD/USDT":
    volume_trigger_ratio_max: 2.8
    session_block_hours: [14, 24]         # F2b: bloquear sesión americana+noche
    usdt_norm_block_range: [2.1, 2.7]    # F3
    rsi_overbought_block: 70              # F4: RSI14 sobrecompra
  "ATOM/USDT":
    session_block_hours: [0, 8]           # F2b: bloquear madrugada UTC
    usdt_norm_block_range: [2.1, 2.7]    # F3
    momentum_q3_block: [0.30, 1.60]      # F1
    rsi_overbought_block: 70              # F4: RSI14 sobrecompra

levels:
  monthly_lookback: 6
  volume_trigger_ratio: 2.0
  volume_trigger_ratio_max: 3.0
  failed_retest_filter: "auto"
  failed_retest_lookback: 60
  failed_retest_auto_lookback: 500
  failed_retest_min_bounce_pct: 0.3

risk:
  take_profit_pct: 3.0
  sl_behind_level_pct: 1.0
  max_open_positions: 1

paper_trading:
  initial_balance_usdt: 1000.0
  fee_pct: 0.1

futures:
  enabled: true
  leverage: 3

guardrails:
  max_daily_loss_pct: 3.0
  max_drawdown_pct: 15.0

news:
  enabled: true
  check_interval_minutes: 15
  pause_hours: 4
```

---

## Resiliencia del sistema

El bot está diseñado para sobrevivir reinicios y caídas:

| Mecanismo | Descripción |
|-----------|-------------|
| `start_bot.ps1` | Wrapper que reinicia el proceso automáticamente si cae por error |
| Startup de Windows | Acceso directo en la carpeta Startup — arranca al iniciar sesión |
| `logs/paper_state.json` | Balance + posiciones abiertas guardados tras cada trade (escritura atómica) |
| Retry con backoff | Reintentos automáticos (5s → 15s → 30s) ante errores de red con Binance |
| Error boundary | Excepciones en un símbolo no matan el loop — se loguean y continúa |
| Circuit breaker diario | Para el trading si la pérdida diaria supera el umbral configurado |
| News circuit breaker | Pausa nuevas entradas N horas si Fear & Greed < 15 o RSS score ≥ 5 (sin API key) |
| Liquidation guard | En modo futuros, cancela el trade si el SL está más allá del precio de liquidación |

---

## Estructura del proyecto

```
autoTrading/
├── main.py                     # Punto de entrada — todos los comandos
├── start_bot.ps1               # Wrapper con auto-reinicio
├── config/
│   └── config.yaml             # Parámetros de la estrategia
├── backtesting/
│   ├── engine.py               # Motor de simulación histórica + análisis de fallos
│   └── results/                # CSV con trades detallados por símbolo
├── data/
│   ├── fetcher.py              # Descarga OHLCV desde Binance (con retry y caché)
│   └── news_fetcher.py         # News circuit breaker: Fear & Greed + RSS (sin API key)
├── execution/
│   └── paper_trader.py         # Paper trader en tiempo real (spot y futuros)
├── indicators/
│   ├── levels.py               # Niveles mensuales + breakout + bounce
│   └── technical.py            # RSI, ATR, features para el modelo ML
├── models/
│   ├── trainer.py              # Entrenamiento XGBoost
│   ├── predictor.py            # Inferencia en tiempo real
│   └── saved/                  # Modelo, scaler y métricas (.joblib / .json)
├── risk/
│   └── manager.py              # Sizing de posición y cálculo de SL/TP
├── logs/
│   ├── paper_trading.log       # Log en tiempo real
│   └── paper_state.json        # Estado persistido (balance + posiciones abiertas)
├── .env                        # API keys (NO subir a Git)
└── .env.example                # Plantilla de credenciales
```

---

## Resultados — validación a 6 años (con comisiones, futuros 3x)

> Se probaron **26 símbolos** durante 6 años. Solo 4 superan el filtro de consistencia con sus filtros per-símbolo optimizados.

### Cartera final — 4 símbolos validados (con filtros F1-F4)

| Símbolo | Trades (6yr) | WR | PF | $1k→ (6yr) | Filtros activos |
|---------|-------------|----|----|-----------|----------------|
| **ADA/USDT** | ~170 | 32.9% | 1.35 | **$4,086** | F1 momentum + F3 vol |
| **LINK/USDT** | ~150 | 31.2% | 1.25 | **$2,610** | F2b sesión 8-14h |
| **EGLD/USDT** | ~130 | 38.3% | 1.71 | **$3,712** | F2b sesión 14-24h + F3 + F4 RSI |
| **ATOM/USDT** | ~110 | 32.7% | 1.34 | **$1,806** | F2b sesión 0-8h + F3 + F1 + F4 RSI |

**Portfolio €100 capital compartido**: €100 → **€7,126 (+7,026% en 6 años)** | WR 33.7% | MaxDD -66.3%

| Año | Capital acumulado |
|-----|------------------|
| 2020 | +3% |
| 2021 | +326% |
| 2022 | +993% |
| 2023 | +1,221% |
| 2024 | +1,827% |
| 2025 | +2,969% |
| 2026 (jul) | +7,026% |

### Símbolos descartados (26 probados en total)

| Símbolo | WR mejor | PF mejor | Razón del descarte |
|---------|---------|---------|-------------------|
| BTC, SOL | <28% | ~1.02 | WR cerca de breakeven |
| DOT, LTC | <26% | <0.95 | PF < 1.0 |
| ICP, NEAR | <27% | <1.0 | Negativos |
| XLM, ALGO | <30% | <1.1 | WR insuficiente con filtros |
| VET | 30.4% | — | 76% trades filtrados → overfitting |
| FIL, HBAR, INJ | <29% | <1.1 | Baseline demasiado bajo para rescatar |

### Cómo funciona el auto-régimen

En cada potencial entrada, el bot analiza las últimas 500 velas 1m y mide cómo se comportaron las últimas roturas del mismo nivel:

```
Si ≥50% de las roturas recientes tuvieron rebote ≥0.15%:
    → modo fakeout-prone → filtro ON con umbral calibrado

Si <50%:
    → modo clean breaker → entrar en la rotura directa
```

### Por qué fallaban los trades — evolución de los filtros

| Filtro | Hallazgo | Implementación |
|--------|----------|----------------|
| Volumen <2.0× | WR 28% — bajo breakeven | `volume_trigger_ratio: 2.0` global |
| Volumen >2.8× en LINK/EGLD | WR 21-24% — trampa de liquidez | `volume_trigger_ratio_max: 2.8` por símbolo |
| Sesión europea 8-14h UTC en LINK | WR 16.7% — peor bucket | F2b: `session_block_hours: [8,14]` |
| Sesión americana 14-24h UTC en EGLD | WR bajo | F2b: `session_block_hours: [14,24]` |
| Volumen USDT normalizado 2.1-2.7× | Zona trampa Q3 en ADA/EGLD/ATOM | F3: `usdt_norm_block_range: [2.1,2.7]` |
| Momentum 5v en rango +0.3%..+1.6% | Zona trampa Q3 en ADA/ATOM | F1: `momentum_q3_block: [0.3,1.6]` |
| RSI14 ≥ 70 en EGLD/ATOM | WR 25.9% — sobrecompra en entrada | F4: `rsi_overbought_block: 70` |
| Fakeouts ≤30min | 35% de las pérdidas | *failed retest* adaptativo |

---

## Roadmap

### ✅ Fase 1 — Estrategia base (completada)
- [x] Breakout de niveles mensuales + confirmación multi-timeframe
- [x] Filtro de volumen 2.0-3.0× con overrides por símbolo
- [x] Failed retest adaptativo (auto-detección de régimen)
- [x] Futuros USDT-M perpetuos con leverage configurable (3x)
- [x] News circuit breaker (Fear & Greed + RSS, sin API key)
- [x] Estado persistido, auto-reinicio, circuit breaker diario
- [x] Modelo XGBoost para filtrar señales de entrada

### ✅ Fase 2 — Optimización de filtros per-símbolo (completada: 2026-07)
- [x] `analyze_features_v2.py` — análisis de 10 dimensiones sobre 4 símbolos × 6 años (707 trades)
- [x] **F1** Momentum 5v: zona trampa Q3 [+0.30%, +1.60%] → ADA y ATOM
- [x] **F2b** Sesión UTC óptima por símbolo: LINK 8-14h, EGLD 14-24h, ATOM 0-8h
- [x] **F3** Volumen USDT normalizado: bloquear zona trampa Q3 [2.1×, 2.7×]
- [x] **F4** RSI14 sobrecompra ≥ 70: EGLD (+2%) y ATOM (+13.9%) mejoran; ADA/LINK no
- [x] Validación post-hoc sin look-ahead bias (`validate_filters.py`)
- [x] Portfolio sim €100 → €7,126 (+7,026%) en 6 años con capital compartido

### ✅ Fase 3 — Búsqueda de nuevos símbolos (completada: 2026-07)
- [x] **Tanda 1** (XLM, ALGO, VET): todos rechazados — WR máximo 30.4% con 76% trades filtrados (overfitting)
- [x] **Tanda 2** (FIL, HBAR, INJ): todos rechazados — baseline demasiado bajo para rescatar
- [x] **Historial de rechazos** (no añadir): ICP, NEAR, BTC, SOL, DOT, XLM, ALGO, VET, FIL, HBAR, INJ
- [x] Criterio fijo: WR ≥ 32% Y PF ≥ 1.25 en backtest real `main.py` con filtros

### ⏳ Fase 4 — Paper trading en vivo (iniciada: 2026-07-29)
- [x] Paper trader arrancado en Binance Testnet — 4 símbolos, 4 filtros activos
- [x] Ciclo 60s, futuros 3x, balance 1000 USDT inicial
- [ ] Acumular ≥ 50 trades con WR cercana al backtest (32-38%)
- [ ] Reentrenar XGBoost cada semana con trades acumulados
- [ ] Validar que los filtros per-símbolo se comportan igual en tiempo real
- [ ] Verificar ausencia de bugs de ejecución durante al menos 4 semanas

### 🔜 Fase 5 — Migración a exchange real (pendiente)

> Binance no permite futuros en ciertas regiones. La solución es **Bybit** vía CCXT.
> Los datos de mercado (OHLCV, volumen) seguirán viniendo de Binance — solo cambia el canal de ejecución.
> Los filtros y el modelo NO necesitan recalibrarse: se basan en precio, no en datos propietarios del exchange.

**Pasos para migrar a Bybit:**
- [ ] Crear cuenta Bybit + KYC básico
- [ ] Generar API keys con permiso `Derivatives Trading` únicamente
- [ ] Depositar USDT en Bybit (wallet de futuros)
- [ ] Actualizar `data/fetcher.py` — `get_exchange()` → `ccxt.bybit` con `defaultType: linear`
- [ ] Añadir sección `bybit:` en `config.yaml` con api_key / secret
- [ ] Probar 2 semanas en Bybit testnet antes de activar real
- [ ] Cambiar `testnet: false` y empezar con capital pequeño (~$100) para validar ejecución

**Cambios de código estimados:** ~20 líneas en `fetcher.py` + sección `config.yaml`. El resto del bot es agnóstico al exchange.

### 🔜 Fase 6 — Mejoras futuras (ideas)
- [ ] Filtro de tendencia: solo LONGs cuando SMA50 semanal alcista (SHORTs tienen WR 34.7% vs LONGs 30.0%)
- [ ] Simular slippage en backtest (~0.05% adicional por entrada)
- [ ] Validar tamaño mínimo de orden por exchange
- [ ] Sincronizar balance real al arrancar desde el exchange
- [ ] Dashboard web ligero para monitorizar trades en tiempo real
