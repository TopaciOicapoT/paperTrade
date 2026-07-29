"""
models/trainer.py
-----------------
Entrena un modelo XGBoost para clasificar breakouts:
  - Clase 1 (éxito): El breakout continuó y alcanzó el TP
  - Clase 0 (fakeout): El precio regresó al nivel roto (stop-loss)

El modelo aprende con TUS datos históricos de Binance.
Cuanto más tiempo corra el sistema y más operaciones registre,
más preciso se vuelve (aprendizaje continuo).
"""

import json
import joblib
import numpy as np
import pandas as pd
from pathlib import Path
from loguru import logger
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.preprocessing import StandardScaler
import xgboost as xgb

from indicators.technical import FEATURE_COLUMNS, calcular_features

MODELS_DIR = Path(__file__).parent / "saved"
MODELS_DIR.mkdir(exist_ok=True)

MODEL_PATH = MODELS_DIR / "xgb_breakout.joblib"
SCALER_PATH = MODELS_DIR / "scaler.joblib"
METRICS_PATH = MODELS_DIR / "metrics.json"


def etiquetar_breakouts(
    df: pd.DataFrame,
    direction: str,
    level_price: float,
    reward_ratio: float = 2.0,
    sl_pct: float = 0.5,
) -> pd.Series:
    """
    Etiqueta si un breakout fue exitoso o fakeout mirando las velas siguientes.

    Lógica:
    - SL = nivel roto ± sl_pct%
    - TP = entry + (SL_distance × reward_ratio)
    - Si en las próximas N velas toca TP antes que SL → etiqueta 1 (éxito)
    - Si toca SL antes que TP → etiqueta 0 (fakeout)

    Args:
        df:          DataFrame OHLCV con features calculadas
        direction:   "long" o "short"
        level_price: Precio del nivel roto
        reward_ratio: Ratio riesgo/beneficio (default 1:2)
        sl_pct:      % de distancia al stop-loss desde el nivel

    Returns:
        Serie de etiquetas binarias (0 o 1)
    """
    labels = pd.Series(index=df.index, dtype=float)
    lookahead = 30  # Velas hacia adelante para evaluar el resultado

    for i in range(len(df) - lookahead):
        entry_price = float(df["close"].iloc[i])
        # SL/TP calculados desde entry_price para coincidir con el engine
        sl_distance = entry_price * (sl_pct / 100)
        tp_distance = sl_distance * reward_ratio

        if direction == "long":
            sl_price = entry_price - sl_distance
            tp_price = entry_price + tp_distance
            future_highs = df["high"].iloc[i + 1: i + lookahead + 1]
            future_lows = df["low"].iloc[i + 1: i + lookahead + 1]

            hit_tp = (future_highs >= tp_price).any()
            hit_sl = (future_lows <= sl_price).any()
        else:
            sl_price = entry_price + sl_distance
            tp_price = entry_price - tp_distance
            future_highs = df["high"].iloc[i + 1: i + lookahead + 1]
            future_lows = df["low"].iloc[i + 1: i + lookahead + 1]

            hit_tp = (future_lows <= tp_price).any()
            hit_sl = (future_highs >= sl_price).any()

        if hit_tp and not hit_sl:
            labels.iloc[i] = 1.0
        elif hit_sl:
            labels.iloc[i] = 0.0
        # Si ninguno → NaN (descartado en entrenamiento)

    return labels


def preparar_dataset(
    df_con_features: pd.DataFrame,
    labels: pd.Series,
) -> tuple[pd.DataFrame, pd.Series]:
    """Limpia y prepara el dataset para entrenamiento."""
    X = df_con_features[FEATURE_COLUMNS].copy()
    y = labels.copy()

    # Alinear índices y eliminar NaN
    mask = y.notna() & X.notna().all(axis=1)
    X = X[mask]
    y = y[mask]

    logger.info(f"Dataset listo: {len(X)} muestras | Tasa éxito: {y.mean():.1%}")
    return X, y


def entrenar_modelo(
    X: pd.DataFrame,
    y: pd.Series,
    test_size: float = 0.2,
) -> tuple[xgb.XGBClassifier, StandardScaler, dict]:
    """
    Entrena el modelo XGBoost y lo guarda en disco.

    Returns:
        (modelo, scaler, métricas)
    """
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=42, stratify=y
    )

    # Normalizar features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # XGBoost con parámetros para evitar overfitting en datos financieros
    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        scale_pos_weight=len(y_train[y_train == 0]) / max(len(y_train[y_train == 1]), 1),
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
    )

    model.fit(
        X_train_scaled,
        y_train,
        eval_set=[(X_test_scaled, y_test)],
        verbose=False,
    )

    # Métricas
    y_pred = model.predict(X_test_scaled)
    y_prob = model.predict_proba(X_test_scaled)[:, 1]
    auc = roc_auc_score(y_test, y_prob)

    metrics = {
        "auc_roc": round(auc, 4),
        "n_samples": len(X),
        "success_rate": round(float(y.mean()), 4),
        "report": classification_report(y_test, y_pred, output_dict=True),
    }

    logger.success(f"Modelo entrenado | AUC-ROC: {auc:.4f} | Samples: {len(X)}")

    # Guardar en disco
    joblib.dump(model, MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)
    with open(METRICS_PATH, "w") as f:
        json.dump(metrics, f, indent=2)

    return model, scaler, metrics


def cargar_modelo() -> tuple[xgb.XGBClassifier, StandardScaler] | tuple[None, None]:
    """Carga el modelo y scaler guardados. Retorna (None, None) si no existen."""
    if MODEL_PATH.exists() and SCALER_PATH.exists():
        model = joblib.load(MODEL_PATH)
        scaler = joblib.load(SCALER_PATH)
        logger.info("Modelo XGBoost cargado desde disco")
        return model, scaler
    logger.warning("No hay modelo guardado todavía — entrena primero con trainer.py")
    return None, None
