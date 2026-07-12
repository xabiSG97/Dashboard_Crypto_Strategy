"""Cálculo de PnL del portafolio equiponderado (memoria, sección 4).

Diseño:
- El PnL se calcula SIEMPRE desde datos persistidos (SQLite), nunca
  desde estado en memoria del stream. Consistencia FUERTE para la
  contabilidad: mismo instante => mismo número, y sobrevive a
  reinicios. El precio "en vivo" del dashboard puede ser eventual;
  el PnL no. "Fuerte para lo que decide, eventual para lo que decora."
- Funciones puras sobre DataFrames: sin red, sin globals => triviales
  de testear con datos sintéticos (la misma filosofía de ayer).
"""

import sqlite3

import pandas as pd

from src.core.instrumentation import measure_latency
from src.storage.queries import load_candles_df
from src.strategies.base import STRATEGY_REGISTRY

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "SOLUSDT",
           "TRXUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT"]


def strategy_returns(candles: pd.DataFrame, signals: pd.Series) -> pd.Series:
    """Retornos de aplicar una serie de señales a un activo.

    LA LÍNEA MÁS IMPORTANTE DEL MÓDULO: positions = signals.shift(1).

    La señal de la vela t se conoce AL CIERRE de t. Es imposible
    haber estado posicionado DURANTE t según una señal que nació al
    terminar t: la posición efectiva en t es la señal de t-1.
    Sin el shift, el backtest compra a precios que ya no existían
    cuando la señal apareció: look-ahead de ejecución, la tercera
    piel del mismo bicho (tras pct_change(-n) y shift(-1)).
    Efecto típico: infla el PnL de toda estrategia que reaccione a
    movimientos, porque le regala exactamente una vela de ventaja.
    """
    positions = signals.shift(1).fillna(0)
    # pct_change(): retorno simple de cada vela. fillna(0) en la
    # primera (no hay vela previa => no hay retorno => no se opera).
    asset_returns = candles["close"].pct_change().fillna(0)
    return positions * asset_returns


@measure_latency
def portfolio_returns(
    conn: sqlite3.Connection,
    strategy_name: str,
    interval: str = "1h",
) -> pd.Series:
    """Retornos del portafolio EQUIPONDERADO bajo una estrategia.

    Equiponderado: cada activo pesa 1/N en cada vela => el retorno
    del portafolio es la MEDIA de los retornos por activo.
    Matiz honesto (documentado en la memoria): mantener pesos 1/N en
    cada vela implica REBALANCEO CONTINUO implícito (vender lo que
    sube, comprar lo que baja, cada hora, sin costes). Es la
    simplificación estándar académica; en real habría costes de
    transacción y bandas de rebalanceo.
    """
    cls = STRATEGY_REGISTRY[strategy_name]
    strategy = cls()

    per_asset: dict[str, pd.Series] = {}
    for symbol in SYMBOLS:
        candles = load_candles_df(conn, symbol, interval)
        if candles.empty:
            continue  # activo sin datos: no entra en la media
        signals = strategy.generate_signals(candles)
        per_asset[symbol] = strategy_returns(candles, signals)

    if not per_asset:
        return pd.Series(dtype=float)

    # DataFrame alinea por índice temporal; mean(axis=1) ignora NaN
    # (si un activo aún no existía en una fecha, la media se hace
    # con los que sí => el portafolio pesa 1/N_disponibles).
    return pd.DataFrame(per_asset).mean(axis=1)


def _period_return(returns: pd.Series, start: pd.Timestamp) -> float:
    """Retorno COMPUESTO desde `start` hasta el final.

    Componer, no sumar: dos velas de +1% no son +2% sino
    (1.01 * 1.01) - 1 = +2.01%. Con retornos pequeños la diferencia
    es mínima; con ventanas largas, sumar en vez de componer es un
    error de contabilidad clásico.
    """
    window = returns[returns.index >= start]
    if window.empty:
        return 0.0
    return float((1 + window).prod() - 1)


def pnl_summary(returns: pd.Series) -> dict[str, float]:
    """PnL diario, mensual, QTD, YTD y total (requisito del dashboard).

    Cada ventana = retorno compuesto desde el inicio de su periodo
    (UTC). Nota de expectativas: si el histórico es más corto que la
    ventana, esa ventana colapsa al total (todas empiezan "antes de
    los datos"). El mecanismo es el mismo con 7 días o con 7 años.
    """
    if returns.empty:
        return {k: 0.0 for k in ("daily", "monthly", "qtd", "ytd", "total")}

    now = returns.index[-1]
    day_start = now.normalize()                              # 00:00 de hoy
    month_start = now.normalize().replace(day=1)             # día 1 del mes
    quarter_month = 3 * ((now.month - 1) // 3) + 1           # 1, 4, 7 u 10
    quarter_start = now.normalize().replace(month=quarter_month, day=1)
    year_start = now.normalize().replace(month=1, day=1)

    return {
        "daily": _period_return(returns, day_start),
        "monthly": _period_return(returns, month_start),
        "qtd": _period_return(returns, quarter_start),
        "ytd": _period_return(returns, year_start),
        "total": float((1 + returns).prod() - 1),
    }
