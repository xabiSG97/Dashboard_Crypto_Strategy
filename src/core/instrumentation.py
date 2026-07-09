"""Instrumentación: decorador de medición de latencia.

Requisito del enunciado: "un decorador personalizado aplicado en el
sistema real" (el retry con backoff no cuenta: lo cubre tenacity).
Este decorador responde además, literalmente, a la memoria (sección 7
avanzada): "diseña un decorador que mida latencia de funciones
críticas sin alterar su firma", y alimenta la parte de observabilidad
(percentiles p50/p95/p99).

Uso:
    @measure_latency
    async def fetch_klines(...): ...

    @measure_latency
    def calcular_pnl(...): ...

    print(latency_report())
"""

import functools
import inspect
import statistics
import time
from collections import defaultdict, deque
from collections.abc import Callable

# Registro de mediciones por nombre de función.
# deque con maxlen: ventana deslizante de las últimas 10k mediciones.
# CARNE DE ENTREVISTA: sin maxlen, un proceso de larga vida acumularía
# mediciones sin límite => fuga de memoria. Es la MISMA idea que la
# cola acotada del pipeline: toda estructura que crece con el tiempo
# necesita una cota. Los patrones se repiten a todas las escalas.
_LATENCIES: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10_000))


def measure_latency(func: Callable) -> Callable:
    """Envuelve una función (sync o async) midiendo su duración en ms.

    CARNE DE ENTREVISTA — anatomía de un decorador:
    - measure_latency recibe la FUNCIÓN y devuelve un wrapper que la
      sustituye. El wrapper acepta (*args, **kwargs) y delega: no
      sabe ni le importa qué parámetros tiene la función real.
    - @functools.wraps(func) copia __name__, __doc__, __module__ y la
      firma al wrapper. Sin él, fetch_klines pasaría a llamarse
      'wrapper', su docstring desaparecería y cualquier introspección
      (FastAPI, sphinx, debugging) vería al impostor, no al original.
      "Sin alterar su firma" = functools.wraps.

    CARNE DE ENTREVISTA — el caso async:
    si envolviéramos una corrutina con el wrapper síncrono, mediríamos
    el tiempo de CREAR el objeto corrutina (nanosegundos), porque
    llamar a una función async no la ejecuta: devuelve una corrutina
    que alguien tendrá que await-ear. Para medir la ejecución real,
    el wrapper debe ser async y hacer `await func(...)` él mismo.
    Detectamos el caso con inspect.iscoroutinefunction.
    """
    name = func.__qualname__

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                # finally: medimos TAMBIÉN si la función lanza una
                # excepción. Las llamadas que fallan también tardan,
                # y a menudo son las más lentas (timeouts).
                _LATENCIES[name].append((time.perf_counter() - start) * 1000)

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            _LATENCIES[name].append((time.perf_counter() - start) * 1000)

    return sync_wrapper


def latency_report() -> dict[str, dict[str, float]]:
    """Percentiles por función: p50 (mediana), p95, p99.

    CARNE DE ENTREVISTA — por qué percentiles y no la media:
    la media esconde las colas. Una función con p50=2ms y p99=800ms
    tiene media ~10ms ("todo bien") pero 1 de cada 100 llamadas es
    una catástrofe. p50 dice "lo típico", p95/p99 dicen "lo que
    sufren tus peores usuarios". En trading, la cola ES el riesgo.
    """
    report = {}
    for name, samples in _LATENCIES.items():
        if len(samples) < 2:
            continue
        data = sorted(samples)
        # statistics.quantiles con n=100 => percentiles 1..99
        percentiles = statistics.quantiles(data, n=100)
        report[name] = {
            "count": len(data),
            "p50_ms": round(percentiles[49], 3),
            "p95_ms": round(percentiles[94], 3),
            "p99_ms": round(percentiles[98], 3),
        }
    return report


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo:  python -m src.core.instrumentation
    # Una función rápida con llamadas lentas ocasionales: la media
    # engaña, los percentiles no.
    # --------------------------------------------------------------
    import random

    @measure_latency
    def operacion_tipica() -> None:
        # 95% de las veces ~1ms; 5% de las veces ~50ms (la cola)
        time.sleep(0.05 if random.random() < 0.05 else 0.001)

    for _ in range(200):
        operacion_tipica()

    # Verificación de functools.wraps: el nombre sobrevive al envoltorio
    print(f"__name__ tras decorar: {operacion_tipica.__name__}")  # operacion_tipica

    for fname, stats in latency_report().items():
        print(f"{fname}: n={stats['count']}  p50={stats['p50_ms']}ms  "
              f"p95={stats['p95_ms']}ms  p99={stats['p99_ms']}ms")
    # Observa la diferencia p50 vs p99: esa brecha es la "cola" que
    # la media esconde.
