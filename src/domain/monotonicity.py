"""Validador de monotonicidad temporal (memoria, sección 1).

Vigila que los event_time de cada símbolo no retrocedan. No corrige
ni descarta nada: OBSERVA y REGISTRA. La decisión de qué hacer con
un tick fuera de orden pertenece a quien consume los datos; separar
detección de política es lo que hace al validador reutilizable y
trivialmente testeable.

Detalles de diseño:
- Timestamps IGUALES no son violación: varios trades en el mismo
  milisegundo son normales en mercados líquidos. Solo el RETROCESO
  estricto viola la monotonicidad (no-decreciente != creciente).
- Estado POR SÍMBOLO: cada stream tiene su propia línea temporal;
  que BTC vaya por t=100 no dice nada sobre dónde debe ir ETH.
- Dominio puro: solo stdlib => testeable sin red ni base de datos.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonotonicityViolation:
    """Registro inmutable de una violación detectada (evidencia para
    la memoria: ejemplos concretos de desorden observado)."""

    symbol: str
    expected_min_ms: int   # el último timestamp visto (el listón)
    observed_ms: int       # lo que llegó (por debajo del listón)

    @property
    def backwards_ms(self) -> int:
        """Cuántos ms 'retrocedió el tiempo'. Magnitud del desorden."""
        return self.expected_min_ms - self.observed_ms


class MonotonicityValidator:
    """Detecta retrocesos de event_time por símbolo.

    Uso:
        validator = MonotonicityValidator()
        violation = validator.check("BTCUSDT", event_time_ms)
        if violation: ...  # decidir política (log, contar, corregir)
    """

    def __init__(self) -> None:
        # Último event_time visto por símbolo. Es TODO el estado que
        # necesitamos: la monotonicidad solo compara con el máximo
        # anterior, no requiere recordar la historia completa.
        self._last_seen_ms: dict[str, int] = {}
        self.violations: list[MonotonicityViolation] = []

    def check(self, symbol: str, event_time_ms: int) -> MonotonicityViolation | None:
        """Valida un timestamp. Devuelve la violación si la hay, o None.

        La condición es `<` estricto, no `<=`.
        Igualdad permitida (varios eventos en el mismo ms), solo el
        retroceso es anomalía. "Monotonicidad no-decreciente".
        """
        last = self._last_seen_ms.get(symbol)

        if last is not None and event_time_ms < last:
            violation = MonotonicityViolation(
                symbol=symbol,
                expected_min_ms=last,
                observed_ms=event_time_ms,
            )
            self.violations.append(violation)
            logger.warning(
                "Monotonicidad violada en %s: llegó t=%d tras haber visto t=%d "
                "(retroceso de %d ms)",
                symbol, event_time_ms, last, violation.backwards_ms,
            )
            # OJO: NO actualizamos _last_seen_ms con el valor menor.
            # El listón se queda en el máximo visto: si llegan tres
            # ticks atrasados seguidos, son tres violaciones, no una.
            return violation

        # Camino normal (avanza o repite): subimos el listón.
        self._last_seen_ms[symbol] = event_time_ms
        return None


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo determinista: una secuencia con desorden fabricado.
    # Ejecutar con:  python -m src.domain.monotonicity
    # Esta demo es el embrión del "test determinista que detecte
    # violaciones de monotonicidad" que pide la memoria (sección 1
    # avanzada): input controlado -> output conocido de antemano.
    # --------------------------------------------------------------
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    validator = MonotonicityValidator()
    secuencia = [
        ("BTCUSDT", 1000),
        ("BTCUSDT", 1005),
        ("ETHUSDT", 500),    # otro símbolo: línea temporal independiente
        ("BTCUSDT", 1005),   # igual al anterior: PERMITIDO (no-decreciente)
        ("BTCUSDT", 1002),   # RETROCESO: violación (llega tarde un tick)
        ("ETHUSDT", 501),    # ETH sigue bien: el lío de BTC no le afecta
        ("BTCUSDT", 1010),   # BTC se recupera
    ]

    for symbol, ts in secuencia:
        validator.check(symbol, ts)

    print(f"\nViolaciones detectadas: {len(validator.violations)}")     # 1
    for v in validator.violations:
        print(f"  {v.symbol}: retrocedió {v.backwards_ms} ms "
              f"(esperaba >= {v.expected_min_ms}, llegó {v.observed_ms})")
