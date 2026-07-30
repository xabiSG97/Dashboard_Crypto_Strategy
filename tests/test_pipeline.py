"""Test del pipeline histórico: carga -> persistencia -> procesado -> señal.

"Un test que valide el pipeline histórico
(carga → procesado → señal)". Y la pieza estrella (memoria, sección
10): el FAKE EXCHANGE.

Un fake exchange es una implementación de la MISMA interfaz que el
cliente real, pero que devuelve datos fabricados y controlados, sin
red. Permite probar el pipeline completo de forma:
- determinista (mismos datos siempre => mismos asserts posibles),
- rápida (sin latencia de red),
- robusta (funciona sin WiFi, en CI, con Binance caído),
- gratuita (cero rate limit).
Probamos NUESTRO código de verdad (SQL real, pandas real, estrategia
real); lo único falso es el mundo exterior.

Ejecutar:  python -m pytest tests/ -v
"""

from pathlib import Path

import pytest

from src.storage.database import get_connection, init_db, insert_candles
from src.storage.queries import load_candles_df
from src.strategies.classic import MomentumStrategy

HOUR_MS = 3_600_000


class FakeExchange:
    """Doble de pruebas del cliente REST: misma interfaz, datos fijos.

    Devuelve una serie alcista fabricada: el precio sube 10 en cada
    vela. Con esos datos, la señal esperada de momentum se calcula
    a mano en el propio test.
    """

    def __init__(self, symbol: str, closes: list[float]) -> None:
        self.symbol = symbol
        self.closes = closes

    async def fetch_klines(self, symbol: str, interval: str, start_ms: int) -> list[tuple]:
        # async para respetar la interfaz del cliente real: quien
        # consuma el fake no debe notar la diferencia (sustituibilidad).
        rows = []
        for i, close in enumerate(self.closes):
            open_time = start_ms + i * HOUR_MS
            open_price = self.closes[i - 1] if i > 0 else close
            rows.append((
                symbol, interval, open_time,
                open_price,                    # open
                max(open_price, close),        # high
                min(open_price, close),        # low
                close,                         # close
                1.0,                           # volume
                open_time + HOUR_MS - 1,       # close_time
            ))
        return rows


@pytest.mark.asyncio
async def test_pipeline_historico_completo(tmp_path: Path):
    """Ingesta sintética -> SQLite real -> DataFrame -> señal esperada.

    tmp_path: fixture de pytest que da una carpeta temporal NUEVA por
    test, destruida al terminar. Cada test tiene su propia BD:
    aislamiento total, el orden de ejecución no puede importar.
    """
    # --- 1. "Descarga" del fake exchange (sin red) ---
    # Cierres: 100, 110, ..., 190. En la última vela, con lookback=2:
    # 190/170 - 1 = +0.1176 > 0.05  =>  señal esperada: +1 (a mano).
    closes = [100.0 + 10 * i for i in range(10)]
    fake = FakeExchange("BTCUSDT", closes)
    rows = await fake.fetch_klines("BTCUSDT", "1h", start_ms=1_700_000_000_000)

    # --- 2. Persistencia (SQLite DE VERDAD: esquema, PK, WAL) ---
    conn = get_connection(tmp_path / "test.db")
    init_db(conn)
    inserted = insert_candles(conn, rows)
    assert inserted == 10  # las 10 velas entran

    # --- 2b. Idempotencia dentro del pipeline: reinsertar no duplica.
    # El mismo experimento que hiciste a mano el día 7, ahora como
    # assert automático que se verificará en cada ejecución para
    # siempre. Los experimentos buenos se convierten en tests.
    assert insert_candles(conn, rows) == 0
    total = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    assert total == 10

    # --- 3. Carga (SQL -> DataFrame, con orden temporal garantizado) ---
    df = load_candles_df(conn, "BTCUSDT", "1h")
    assert len(df) == 10
    assert df.index.is_monotonic_increasing  # el ORDER BY, probado

    # --- 4. Procesado -> señal conocida de antemano ---
    signals = MomentumStrategy(lookback=2, threshold=0.05).generate_signals(df)
    assert signals.iloc[-1] == 1

    conn.close()


@pytest.mark.asyncio
async def test_pipeline_detecta_mercado_bajista(tmp_path: Path):
    """El espejo: serie bajista fabricada => señal -1. Dos tests con
    resultados OPUESTOS sobre el mismo pipeline: si ambos pasan, la
    señal depende de los DATOS y no de un sesgo del código (una
    estrategia rota que siempre dijera +1 pasaría el primer test y
    caería aquí)."""
    closes = [190.0 - 10 * i for i in range(10)]  # 190, 180, ..., 100
    fake = FakeExchange("ETHUSDT", closes)
    rows = await fake.fetch_klines("ETHUSDT", "1h", start_ms=1_700_000_000_000)

    conn = get_connection(tmp_path / "test.db")
    init_db(conn)
    insert_candles(conn, rows)

    df = load_candles_df(conn, "ETHUSDT", "1h")
    # Última vela: 100/120 - 1 = -0.1667 < -0.05  =>  -1
    signals = MomentumStrategy(lookback=2, threshold=0.05).generate_signals(df)
    assert signals.iloc[-1] == -1

    conn.close()
