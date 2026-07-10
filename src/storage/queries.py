"""Consultas de lectura: de SQLite a pandas.

Puente entre el almacenamiento y la capa de análisis. Las estrategias
no saben SQL y el storage no sabe de DataFrames: este módulo es la
frontera (misma idea que parse_trade con Binance: las fronteras
concentran las traducciones).
"""

import sqlite3

import pandas as pd


def load_candles_df(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
) -> pd.DataFrame:
    """Carga las velas de un símbolo como DataFrame indexado por tiempo.

    Columnas: open, high, low, close, volume. Índice: DatetimeIndex
    UTC (convertido desde open_time_ms). El ORDER BY es importante:
    las estrategias asumen orden temporal ascendente; mejor
    garantizarlo en la consulta que confiar en el orden físico de
    la tabla.
    """
    df = pd.read_sql_query(
        """
        SELECT open_time_ms, open, high, low, close, volume
        FROM candles
        WHERE symbol = ? AND interval = ?
        ORDER BY open_time_ms ASC
        """,
        conn,
        params=(symbol, interval),
    )
    df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    return df.drop(columns="open_time_ms").set_index("open_time")
