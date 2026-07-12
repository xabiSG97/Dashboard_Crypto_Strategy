"""Cliente REST de Binance: descarga de velas históricas (klines).

Decisiones clave (memoria, secciones sobre tolerancia a fallos):
- tenacity para reintentos con exponential backoff + jitter ante
  429, 418 y timeouts (requisito explícito del enunciado).
- Paginación: Binance limita a 1000 velas/petición; iteramos
  avanzando el cursor temporal hasta cubrir el rango pedido.
- Concurrencia limitada: 10 símbolos en paralelo pero con un
  semáforo de 3 => aprovechamos asyncio sin invitar al rate limit.
"""

import asyncio
import time

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

BASE_URL = "https://api.binance.com"
KLINES_ENDPOINT = "/api/v3/klines"
MAX_LIMIT = 1000  # máximo de velas por petición que permite Binance

# Símbolos del proyecto (spot, par USDT)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "SOLUSDT",
           "TRXUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT"]


class RetryableAPIError(Exception):
    """Errores que MERECEN reintento: rate limit (429), ban temporal (418),
    o errores 5xx del servidor. Un 400 (petición mal formada) NO está aquí:
    reintentar una petición inválida es repetir el mismo error más veces.

    CARNE DE ENTREVISTA: la primera decisión de toda política de retry
    es clasificar errores en transitorios (reintentar) vs permanentes
    (fallar rápido y arreglar el código).
    """


@retry(
    # Solo reintentamos errores clasificados como transitorios:
    retry=retry_if_exception_type((RetryableAPIError, httpx.TimeoutException)),
    # Backoff exponencial CON JITTER: 1s, 2s, 4s... + ruido aleatorio.
    # El jitter evita el "thundering herd": si N clientes fallan a la
    # vez y todos reintentan a los X segundos exactos, provocan otra
    # ola de rate limit. El ruido los desincroniza.
    wait=wait_exponential_jitter(initial=1, max=60),
    stop=stop_after_attempt(6),
    reraise=True,  # tras agotar intentos, propaga la excepción original
)
async def _fetch_klines_page(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list]:
    """Descarga UNA página (hasta 1000 velas). Los reintentos viven aquí,
    en la unidad mínima de trabajo: si falla la página 7 de 20, se
    reintenta SOLO la página 7, no la descarga entera.
    """
    resp = await client.get(
        KLINES_ENDPOINT,
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": MAX_LIMIT,
        },
    )

    # --- Manejo explícito de los códigos del enunciado ---
    if resp.status_code == 429:
        # Rate limit. Binance puede indicar cuánto esperar:
        retry_after = int(resp.headers.get("Retry-After", "1"))
        # Respetamos su indicación ANTES de que tenacity aplique su
        # backoff. Ignorar Retry-After es el camino directo al 418.
        await asyncio.sleep(retry_after)
        raise RetryableAPIError(f"429 rate limit en {symbol} (Retry-After={retry_after}s)")

    if resp.status_code == 418:
        # Ban temporal de IP por ignorar 429s repetidamente.
        # No debería ocurrir si respetamos Retry-After; si ocurre,
        # el backoff exponencial (hasta 60s) nos aparta del fuego.
        retry_after = int(resp.headers.get("Retry-After", "60"))
        await asyncio.sleep(retry_after)
        raise RetryableAPIError(f"418 IP ban temporal en {symbol} ({retry_after}s)")

    if resp.status_code >= 500:
        raise RetryableAPIError(f"{resp.status_code} error de servidor en {symbol}")

    # Cualquier otro error (4xx) es culpa nuestra: fallar rápido.
    resp.raise_for_status()
    return resp.json()


async def fetch_klines(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int | None = None,
) -> list[tuple]:
    """Descarga TODAS las velas del rango, paginando.

    Devuelve tuplas listas para storage.insert_candles():
    (symbol, interval, open_time_ms, open, high, low, close, volume, close_time_ms)
    """
    if end_ms is None:
        end_ms = int(time.time() * 1000)

    rows: list[tuple] = []
    cursor = start_ms

    while cursor < end_ms:
        page = await _fetch_klines_page(client, symbol, interval, cursor, end_ms)
        if not page:
            break  # rango sin datos (p.ej. antes del listado del activo)

        for k in page:
            # Formato de Binance por vela (lista posicional):
            # [0] open_time, [1] open, [2] high, [3] low, [4] close,
            # [5] volume, [6] close_time, ... (el resto no lo usamos)
            # Los precios llegan como STRINGS ("60000.50") para no
            # perder precisión en el JSON; convertimos a float aquí
            # (decisión float documentada en el checkpoint anterior).
            rows.append((
                symbol, interval, int(k[0]),
                float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                float(k[5]), int(k[6]),
            ))

        # CARNE DE ENTREVISTA — condición de parada de la paginación:
        # si la página vino INCOMPLETA (<1000), no hay más datos: fin.
        # Si vino llena, puede haber más: avanzamos el cursor a
        # close_time de la última vela + 1 ms (el +1 evita repedir
        # la última vela; aunque nuestra BD la ignoraría por la PK,
        # mejor no desperdiciar rate limit en datos repetidos).
        if len(page) < MAX_LIMIT:
            break
        cursor = int(page[-1][6]) + 1

    return rows


async def download_history(
    symbols: list[str],
    interval: str,
    start_ms: int,
    max_concurrent: int = 3,
) -> dict[str, list[tuple]]:
    """Descarga el histórico de varios símbolos con concurrencia limitada.

    CARNE DE ENTREVISTA — por qué semáforo y no gather a pelo:
    los 10 símbolos a la vez multiplican por 10 el consumo de rate
    limit instantáneo. El semáforo deja como máximo 3 descargas
    en vuelo; las demás corrutinas esperan su turno SIN bloquear
    el event loop (asyncio.Semaphore es cooperativo).
    Es control de flujo del lado cliente: mejor autolimitarse que
    negociar con un 429.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        async def bounded_fetch(symbol: str) -> tuple[str, list[tuple]]:
            async with semaphore:
                candles = await fetch_klines(client, symbol, interval, start_ms)
                return symbol, candles

        # gather lanza las 10 corrutinas; el semáforo las va dejando
        # pasar de 3 en 3. Si una falla tras agotar reintentos, gather
        # propaga la excepción (fail-fast: preferimos enterarnos).
        results = await asyncio.gather(*(bounded_fetch(s) for s in symbols))

    return dict(results)


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo: descarga 7 días de velas 1h de los 10 símbolos y las
    # persiste. Ejecutar DOS VECES demuestra otra vez la idempotencia:
    # la segunda ejecución insertará 0 velas nuevas (o solo las velas
    # que hayan cerrado entre ambas ejecuciones).
    # Ejecutar con:  python -m src.ingestion.rest_client
    # --------------------------------------------------------------
    from src.storage.database import get_connection, init_db, insert_candles

    async def main() -> None:
        DAYS = 90
        start_ms = int(time.time() * 1000) - DAYS * 24 * 3600 * 1000
        print(f"Descargando {DAYS} días de velas 1h para 10 símbolos...")
        data = await download_history(SYMBOLS, "1h", start_ms)

        conn = get_connection()
        init_db(conn)
        for symbol, rows in data.items():
            inserted = insert_candles(conn, rows)
            print(f"{symbol}: {len(rows)} velas descargadas, {inserted} insertadas")
        total = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        print(f"Total de velas en la BD: {total}")
        conn.close()

    asyncio.run(main())
