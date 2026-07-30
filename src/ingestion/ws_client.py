"""Ingesta en tiempo real: WebSocket de Binance -> cola acotada -> SQLite.

Arquitectura productor-consumidor (memoria, secciones 3 y 6):

    [Binance WS] --> producer() --> asyncio.Queue(1000) --> consumer() --> SQLite
                     (encola)        ACOTADA = backpressure   (valida + persiste)

Decisiones clave:
- Conexión COMBINADA (10 streams, 1 socket): simple, un solo punto de
  reconexión. Límite conocido: con cientos de símbolos habría que
  particionar en varias conexiones (memoria, sección 8).
- Cola ACOTADA con política "bloquear al productor": si el consumidor
  se atasca, await queue.put() suspende la lectura del socket. No
  perdemos datos; a cambio, un atasco largo puede hacer que Binance
  nos desconecte por lentos (la reconexión + backfill REST rellenaría
  el hueco). El sesgo alternativo (drop oldest) mantendría la lectura
  viva pero introduciría huecos silenciosos en los datos. Elegimos
  integridad sobre continuidad (memoria, sección 3).
- Reconexión con backoff exponencial que SE RESETEA tras conexión
  estable (requisito del enunciado).
"""

import asyncio
import json
import logging
import time

import websockets

from src.domain.models import Trade
from src.domain.monotonicity import MonotonicityValidator
from src.storage.database import get_connection, init_db, insert_trade

logger = logging.getLogger(__name__)

WS_BASE = "wss://stream.binance.com:9443/stream"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "SOLUSDT",
           "TRXUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT"]


def build_stream_url(symbols: list[str]) -> str:
    """URL del stream combinado: un socket, N streams de trades.
    Binance exige los nombres de stream en minúsculas."""
    streams = "/".join(f"{s.lower()}@trade" for s in symbols)
    return f"{WS_BASE}?streams={streams}"


def parse_trade(data: dict, ingestion_time_ms: int) -> Trade:
    """Mapea el evento 'trade' de Binance a nuestra entidad de dominio.

    Formato de Binance (campos de una letra):
      s=symbol, t=trade_id, p=price (string), q=qty (string),
      T=trade time (ms, cuándo se ejecutó), m=is buyer maker.

    Esta función es la FRONTERA entre el mundo
    de Binance y nuestro dominio. Las claves crípticas ('p', 'q', 'T')
    no salen de aquí: el resto del sistema habla en Trade. Si mañana
    cambiáramos de exchange, se reescribe esta función, no el dominio
    (memoria, sección 5: no mezclar conceptos del broker con el dominio).
    """
    return Trade(
        symbol=data["s"],
        trade_id=int(data["t"]),
        price=float(data["p"]),
        qty=float(data["q"]),
        event_time_ms=int(data["T"]),
        ingestion_time_ms=ingestion_time_ms,
        is_buyer_maker=bool(data["m"]),
    )


async def producer(queue: asyncio.Queue, symbols: list[str]) -> None:
    """Lee del WebSocket y encola trades. Reconecta con backoff.

    Bucle exterior = vida del proceso; bucle interior = vida de UNA
    conexión. Cada excepción de red rompe el interior, se espera el
    backoff, y se reintenta. Backoff exponencial 1->2->4->...->60s
    con RESETEO tras conexión estable (si no se resetea, un sistema
    con caídas ocasionales acabaría esperando 60s por cada microcorte).
    """
    url = build_stream_url(symbols)
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(url) as ws:
                logger.info("WebSocket conectado (%d streams)", len(symbols))
                backoff = 1.0  # conexión lograda => reseteamos el castigo

                async for raw in ws:
                    # ingestion_time SE ESTAMPA AQUÍ, al recibir del
                    # socket, ANTES de encolar. Así latency_ms mide
                    # exchange+red. Si se estampara en el consumidor,
                    # mediría también nuestra cola y mezclaría dos
                    # señales distintas (latencia externa vs lag interno).
                    ingestion_ms = int(time.time() * 1000)
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    if data.get("e") != "trade":
                        continue  # ping/otros eventos: no nos interesan

                    trade = parse_trade(data, ingestion_ms)

                    # LA LÍNEA MÁS IMPORTANTE DEL MÓDULO:
                    # si la cola está llena, este await SUSPENDE al
                    # productor (cesión cooperativa) hasta que el
                    # consumidor haga hueco. Eso ES el backpressure:
                    # el productor no puede adelantar indefinidamente
                    # al consumidor porque la cola acotada los acopla.
                    await queue.put(trade)

        except (websockets.ConnectionClosed, OSError) as exc:
            # Errores de RED: transitorios, se reintenta con backoff.
            # (La misma clasificación transitorio/permanente del REST:
            # un bug de parseo NO se captura aquí; debe explotar.)
            logger.warning("Conexión perdida (%s). Reintento en %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def consumer(queue: asyncio.Queue, stats: dict) -> None:
    """Desencola trades, valida monotonicidad y persiste.

    Sqlite3 es SÍNCRONO, así que
    insert_trade bloquea el event loop unos microsegundos por trade.
    A nuestro caudal (decenas/s) es despreciable; a miles/s habría
    que sacarlo del loop (asyncio.to_thread) o insertar por lotes.
    Trade-off medido y documentado, no ignorado.
    """
    conn = get_connection()
    init_db(conn)
    validator = MonotonicityValidator()

    while True:
        trade = await queue.get()

        if validator.check(trade.symbol, trade.event_time_ms):
            stats["violations"] += 1

        inserted = insert_trade(
            conn,
            symbol=trade.symbol,
            trade_id=trade.trade_id,
            price=trade.price,
            qty=trade.qty,
            event_time_ms=trade.event_time_ms,
            is_buyer_maker=trade.is_buyer_maker,
        )
        stats["inserted" if inserted else "duplicates"] += 1
        stats["latency_sum_ms"] += trade.latency_ms
        total = stats["inserted"] + stats["duplicates"]

        # Instrumentación en vivo cada 100 trades: qsize() es NUESTRA
        # MÉTRICA DE LAG (memoria, sección 3). Cola estable cerca de 0
        # = consumidor al día; cola creciendo = retraso acumulándose.
        if total % 100 == 0:
            print(
                f"[{total:>6} trades] cola={queue.qsize():>4} | "
                f"insertados={stats['inserted']} dups={stats['duplicates']} "
                f"violaciones={stats['violations']} | "
                f"latencia media={stats['latency_sum_ms'] // max(total, 1)}ms"
            )

        queue.task_done()


async def run_ingestion(symbols: list[str], queue_size: int = 1000) -> None:
    """Arranca productor y consumidor concurrentes sobre la cola acotada."""
    queue: asyncio.Queue[Trade] = asyncio.Queue(maxsize=queue_size)
    stats = {"inserted": 0, "duplicates": 0, "violations": 0, "latency_sum_ms": 0}

    # gather: ambas corrutinas conviven en el mismo event loop, un
    # solo hilo. El productor espera red, el consumidor espera cola;
    # mientras uno espera, el otro trabaja (cesión cooperativa).
    await asyncio.gather(
        producer(queue, symbols),
        consumer(queue, stats),
    )


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo en vivo:  python -m src.ingestion.ws_client
    # Conecta a Binance y persiste trades reales. Cada 100 trades
    # imprime estadísticas. Parar con Ctrl+C.
    # Déjalo correr unos minutos: los contadores de duplicados y
    # violaciones, y la latencia media observada, son EVIDENCIA
    # EMPÍRICA para las secciones 1 y 3 de la memoria.
    # --------------------------------------------------------------
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Conectando al stream de Binance (Ctrl+C para parar)...")
    try:
        asyncio.run(run_ingestion(SYMBOLS))
    except KeyboardInterrupt:
        print("\nIngesta detenida.")
