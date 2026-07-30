"""Entidades del dominio: Trade y Candle.

Decisión central : la igualdad de una entidad se define por su
IDENTIDAD, no por sus datos. Dos Trade con el mismo (symbol, trade_id)
son EL MISMO trade aunque difieran en ingestion_time (p. ej. recibido
dos veces tras una reconexión). Así, la noción de "mismo trade" es
idéntica en Python (sets, dicts) y en SQLite (PRIMARY KEY): una sola
definición de identidad en todo el sistema.

El dominio NO importa nada externo (ni httpx, ni sqlite3): solo
stdlib. Esto lo hace testeable en aislamiento y es la semilla de la
separación dominio/infraestructura (memoria, sección 5).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, eq=False)
# frozen=True  => inmutable: los atributos no se pueden reasignar.
#   Un objeto HASHEABLE debe ser inmutable en
#   los campos que definen su hash. Si trade_id pudiera cambiar tras
#   meter el Trade en un set, el set lo "perdería" (quedó guardado
#   en el bucket de su hash viejo). frozen elimina esa clase de bug.
# eq=False     => le decimos a dataclass que NO genere su __eq__
#   automático (que compara TODOS los campos); definimos el nuestro.
class Trade:
    """Un trade ejecutado en el exchange (evento inmutable del mercado)."""

    symbol: str
    trade_id: int
    price: float
    qty: float
    event_time_ms: int       # timestamp del exchange (autoritativo para cálculos)
    ingestion_time_ms: int   # nuestro reloj al recibirlo (para medir latencia)
    is_buyer_maker: bool

    # ------------------------------------------------------------------
    # DUNDER #1 y #2: __eq__ y __hash__
    # con finalidad semántica: igualdad = identidad de mercado.
    # ------------------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Trade):
            return NotImplemented
            # NotImplemented (no confundir con NotImplementedError):
            # le dice a Python "yo no sé compararme con eso, prueba
            # al revés (other.__eq__(self)) o da False". Comparar
            # Trade == Candle da False limpiamente, no explota.
        return (self.symbol, self.trade_id) == (other.symbol, other.trade_id)

    def __hash__(self) -> int:
        # CONTRATO eq/hash: si a == b entonces hash(a) == hash(b).
        # Por eso el hash usa EXACTAMENTE los mismos campos que __eq__.
        # Romper este contrato rompe sets y dicts silenciosamente:
        # dos objetos "iguales" caerían en buckets distintos y el
        # set contendría "duplicados que son iguales".
        return hash((self.symbol, self.trade_id))

    @property
    def latency_ms(self) -> int:
        """Latencia exchange -> nosotros. Evidencia empírica para la
        sección 1 de la memoria (event time vs ingestion time)."""
        return self.ingestion_time_ms - self.event_time_ms


@dataclass(frozen=True, eq=False)
class Candle:
    """Una vela OHLCV. Identidad: (symbol, interval, open_time_ms)."""

    symbol: str
    interval: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Candle):
            return NotImplemented
        return (self.symbol, self.interval, self.open_time_ms) == (
            other.symbol, other.interval, other.open_time_ms
        )

    def __hash__(self) -> int:
        return hash((self.symbol, self.interval, self.open_time_ms))


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo: la igualdad por identidad en acción.
    # Ejecutar con:  python -m src.domain.models
    # --------------------------------------------------------------
    # El MISMO trade recibido dos veces (reconexión): distinto
    # ingestion_time, misma identidad de mercado.
    t1 = Trade("BTCUSDT", 123, 60000.5, 0.01, 1720260000000, 1720260000045, False)
    t2 = Trade("BTCUSDT", 123, 60000.5, 0.01, 1720260000000, 1720260000350, False)

    # Trade DISTINTO (otro trade_id):
    t3 = Trade("BTCUSDT", 124, 60001.0, 0.02, 1720260000100, 1720260000150, True)

    print(f"t1 == t2 (mismo trade, distinta recepción): {t1 == t2}")   # True
    print(f"t1 == t3 (trades distintos):                {t1 == t3}")   # False
    print(f"latencias: t1={t1.latency_ms}ms, t2={t2.latency_ms}ms")    # 45 / 350

    # Un set deduplica usando __eq__/__hash__: réplica en memoria
    # de lo que la PRIMARY KEY hace en SQLite.
    unicos = {t1, t2, t3}
    print(f"set({{t1, t2, t3}}) contiene {len(unicos)} trades")         # 2

    # frozen=True en acción: descomenta para ver el error.
   
    # t1.price = 99999.0   # -> FrozenInstanceError