"""Capa de almacenamiento: SQLite con inserciones idempotentes.

Decisiones clave (ver memoria, secciones 1 y 2):
- UNIQUE + INSERT OR IGNORE => idempotencia garantizada por la BD,
  no por lógica de aplicación (menos superficie de bugs).
- Modo WAL => lecturas concurrentes (dashboard) mientras se escribe (ingestor).
- Se guardan event_time (exchange) e ingestion_time (nuestro reloj):
  su diferencia mide latencia y evidencia mensajes fuera de orden.
"""

import sqlite3
import time
from pathlib import Path

# Ruta por defecto de la BD: raíz del proyecto /data/market.db
# (la carpeta data/ está en .gitignore: versionamos código, no datos)
DEFAULT_DB_PATH = Path("data/market.db")


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Abre una conexión a SQLite con la configuración del proyecto.

    CARNE DE ENTREVISTA — los tres PRAGMA:
    - journal_mode=WAL: escrituras van a un log aparte (write-ahead),
      lo que permite que N lectores lean MIENTRAS un escritor escribe.
      Con el modo por defecto (rollback journal), cada escritura
      bloquea a los lectores.
    - synchronous=NORMAL: con WAL, equilibrio seguro entre durabilidad
      y velocidad (fsync en checkpoints, no en cada commit).
    - foreign_keys=ON: SQLite las trae DESACTIVADAS por defecto por
      compatibilidad histórica; las activamos por higiene.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Crea el esquema si no existe. Idempotente: se puede llamar siempre al arrancar."""
    conn.executescript(
        """
        -- ============================================================
        -- TRADES: ticks en tiempo real del WebSocket de Binance
        -- ============================================================
        -- Primary identity de un trade: (symbol, trade_id).
        -- Binance garantiza trade_id creciente y único POR símbolo,
        -- por eso la unicidad es compuesta: el trade_id 12345 de
        -- BTCUSDT y el 12345 de ETHUSDT son trades distintos.
        CREATE TABLE IF NOT EXISTS trades (
            symbol          TEXT    NOT NULL,
            trade_id        INTEGER NOT NULL,
            price           REAL    NOT NULL,
            qty             REAL    NOT NULL,
            event_time_ms   INTEGER NOT NULL,  -- timestamp de Binance (epoch ms)
            ingestion_time_ms INTEGER NOT NULL, -- nuestro reloj al recibirlo (epoch ms)
            is_buyer_maker  INTEGER NOT NULL,  -- 0/1 (SQLite no tiene BOOLEAN)
            PRIMARY KEY (symbol, trade_id)
        ) WITHOUT ROWID;
        -- WITHOUT ROWID: la clave primaria compuesta ES el índice de
        -- la tabla (clustered). Ahorra un índice duplicado y las
        -- búsquedas por (symbol, trade_id) van directas.

        -- NOTA sobre REAL para precios (pregunta trampa de entrevista):
        -- REAL es float64 => precisión limitada (~15-16 dígitos).
        -- Para un sistema de contabilidad real usaríamos enteros en
        -- la unidad mínima o Decimal. Para análisis y visualización,
        -- float es el estándar (pandas trabaja en float64 igualmente).
        -- Decisión consciente y documentada en la memoria.

        -- ============================================================
        -- CANDLES: histórico OHLCV descargado por REST
        -- ============================================================
        -- Identidad de una vela: símbolo + intervalo + apertura.
        CREATE TABLE IF NOT EXISTS candles (
            symbol        TEXT    NOT NULL,
            interval      TEXT    NOT NULL,   -- '1m', '1h', '1d'...
            open_time_ms  INTEGER NOT NULL,
            open          REAL    NOT NULL,
            high          REAL    NOT NULL,
            low           REAL    NOT NULL,
            close         REAL    NOT NULL,
            volume        REAL    NOT NULL,
            close_time_ms INTEGER NOT NULL,
            PRIMARY KEY (symbol, interval, open_time_ms)
        ) WITHOUT ROWID;

        -- Índice para la consulta más frecuente del dashboard:
        -- "últimos N trades de un símbolo ordenados por tiempo".
        CREATE INDEX IF NOT EXISTS idx_trades_symbol_time
            ON trades (symbol, event_time_ms);
        """
    )
    conn.commit()


def insert_trade(
    conn: sqlite3.Connection,
    symbol: str,
    trade_id: int,
    price: float,
    qty: float,
    event_time_ms: int,
    is_buyer_maker: bool,
) -> bool:
    """Inserta un trade de forma idempotente.

    Devuelve True si se insertó, False si ya existía (duplicado ignorado).

    CARNE DE ENTREVISTA: INSERT OR IGNORE se apoya en la PRIMARY KEY
    (symbol, trade_id). Si el trade ya existe, SQLite lo descarta
    ATÓMICAMENTE: no hay hueco entre "comprobar si existe" e "insertar"
    donde otro proceso pueda colarse (el clásico race condition del
    patrón check-then-insert hecho a mano en Python).
    """
    ingestion_time_ms = int(time.time() * 1000)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO trades
            (symbol, trade_id, price, qty, event_time_ms, ingestion_time_ms, is_buyer_maker)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        # Placeholders (?) SIEMPRE, nunca f-strings: previene inyección
        # SQL y deja que SQLite cachee el plan de la consulta.
        (symbol, trade_id, price, qty, event_time_ms, ingestion_time_ms, int(is_buyer_maker)),
    )
    conn.commit()
    # rowcount == 1 si insertó; 0 si el OR IGNORE descartó el duplicado.
    return cursor.rowcount == 1


def insert_candles(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Inserta un lote de velas históricas de forma idempotente.

    `rows`: tuplas (symbol, interval, open_time_ms, open, high, low,
    close, volume, close_time_ms) — el formato que producirá el
    cliente REST.

    Devuelve cuántas velas se insertaron de verdad (las repetidas se
    ignoran). executemany + una sola transacción: insertar 1000 velas
    en un commit es órdenes de magnitud más rápido que 1000 commits.
    """
    cursor = conn.executemany(
        """
        INSERT OR IGNORE INTO candles
            (symbol, interval, open_time_ms, open, high, low, close, volume, close_time_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return cursor.rowcount


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo de idempotencia — ejecutar con:  python -m src.storage.database
    # Inserta el MISMO trade dos veces y demuestra que la segunda
    # se ignora. Este mini-experimento es tu ejemplo empírico para
    # la sección 2 de la memoria.
    # --------------------------------------------------------------
    conn = get_connection(Path("data/demo.db"))
    init_db(conn)

    trade = dict(symbol="BTCUSDT", trade_id=1, price=60000.5, qty=0.01,
                 event_time_ms=1720260000000, is_buyer_maker=False)

    primera = insert_trade(conn, **trade)
    segunda = insert_trade(conn, **trade)  # mismo trade => debe ignorarse

    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"Primera inserción: {'insertado' if primera else 'ignorado'}")   # insertado
    print(f"Segunda inserción: {'insertado' if segunda else 'ignorado'}")   # ignorado
    print(f"Trades en la tabla: {total}")                                    # 1
    conn.close()
