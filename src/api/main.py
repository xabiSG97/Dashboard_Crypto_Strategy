"""Backend FastAPI: expone precios, señales, PnL y métricas
(requisito mínimo 3 del enunciado).

Arquitectura:
- El backend LEE de SQLite: es un consumidor más de la fuente única
  de verdad. No habla con Binance ni mantiene estado propio: si se
  reinicia, no se pierde nada (el estado vive en los datos).
- Las estrategias llegan vía STRATEGY_REGISTRY: el backend no conoce
  ninguna clase concreta (el flujo plugin/discovery ensayado en la
  demo de estrategias, ahora en producción).

Arrancar:   uvicorn src.api.main:app --reload
Documentación interactiva automática:   http://127.0.0.1:8000/docs
"""

import sqlite3
from collections.abc import Iterator

from fastapi import Depends, FastAPI, HTTPException

# Importar classic REGISTRA las tres estrategias (efecto import-time
# de StrategyMeta). Sin este import, el registro estaría vacío.
import src.strategies.classic  # noqa: F401  (import por efecto, no por nombre)
from src.core.instrumentation import latency_report
from src.core.pnl import pnl_summary, portfolio_returns
from src.storage.database import DEFAULT_DB_PATH, get_connection
from src.storage.queries import load_candles_df
from src.strategies.base import STRATEGY_REGISTRY

app = FastAPI(
    title="Crypto Dashboard API",
    description="Precios, señales y PnL del portafolio equiponderado",
    version="0.1.0",
)


def get_db() -> Iterator[sqlite3.Connection]:
    """Dependencia de FastAPI: una conexión SQLite por petición.

    yield en vez de return: FastAPI ejecuta lo de ANTES del yield al
    entrar la petición, entrega la conexión al endpoint, y ejecuta lo
    de DESPUÉS (el close) al terminar, incluso si el endpoint lanza
    una excepción. Mismo patrón que un context manager: setup /
    uso / teardown garantizado.
    """
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/symbols")
def list_symbols(conn: sqlite3.Connection = Depends(get_db)) -> list[str]:
    """Símbolos disponibles (los que tienen velas en la BD).
    El dashboard construye su dropdown con esto: si mañana hay 12
    activos en la BD, el dropdown tendrá 12 sin tocar código."""
    rows = conn.execute("SELECT DISTINCT symbol FROM candles ORDER BY symbol").fetchall()
    return [r[0] for r in rows]


@app.get("/strategies")
def list_strategies() -> list[str]:
    """Estrategias registradas (leídas del registro de la metaclase)."""
    return list(STRATEGY_REGISTRY)


@app.get("/candles/{symbol}")
def get_candles(
    symbol: str,
    interval: str = "1h",
    limit: int = 500,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """Velas OHLCV para el gráfico candlestick del dashboard."""
    df = load_candles_df(conn, symbol.upper(), interval)
    if df.empty:
        raise HTTPException(404, f"Sin velas para {symbol} ({interval})")
    df = df.tail(limit).reset_index()
    df["open_time"] = df["open_time"].astype(str)  # JSON no tiene Timestamp
    return df.to_dict(orient="records")


@app.get("/signals/{symbol}")
def get_signals(
    symbol: str,
    strategy: str = "sma_cross",
    interval: str = "1h",
    limit: int = 500,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """Señales de una estrategia sobre un símbolo (para pintar
    entradas/salidas sobre el candlestick)."""
    if strategy not in STRATEGY_REGISTRY:
        # 404 con la lista de válidas: un error que enseña a usarse.
        raise HTTPException(404, f"Estrategia '{strategy}' no existe. "
                                 f"Disponibles: {list(STRATEGY_REGISTRY)}")
    df = load_candles_df(conn, symbol.upper(), interval)
    if df.empty:
        raise HTTPException(404, f"Sin velas para {symbol} ({interval})")

    signals = STRATEGY_REGISTRY[strategy]().generate_signals(df)
    out = signals.tail(limit)
    return [
        {"time": str(ts), "signal": int(sig)}
        for ts, sig in out.items()
    ]


@app.get("/pnl")
def get_pnl(
    strategy: str = "sma_cross",
    interval: str = "1h",
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """PnL del portafolio equiponderado: diario, mensual, QTD, YTD,
    total. Calculado SIEMPRE desde datos persistidos (consistencia
    fuerte, memoria sección 4)."""
    if strategy not in STRATEGY_REGISTRY:
        raise HTTPException(404, f"Estrategia '{strategy}' no existe. "
                                 f"Disponibles: {list(STRATEGY_REGISTRY)}")
    returns = portfolio_returns(conn, strategy, interval)
    return {"strategy": strategy, "interval": interval, **pnl_summary(returns)}


@app.get("/metrics")
def get_metrics(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Observabilidad: latencias del decorador + volumen de datos."""
    candles = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return {
        "candles_stored": candles,
        "trades_stored": trades,
        "latency": latency_report(),
    }
