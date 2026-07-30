"""Posición agregada por símbolo (memoria, sección 5).

INVARIANTE :
- avg_price es SIEMPRE el precio medio ponderado por volumen de la
  cantidad actualmente abierta;
- realized_pnl solo cambia al REDUCIR o cerrar posición;
- qty * avg_price es el coste base de lo abierto.

Regla de oro de los partial fills:
- AUMENTAR posición re-pondera el precio medio.
- REDUCIR posición NO toca el precio medio de lo que queda: vender
  no abarata tu entrada; materializa PnL (realized += cantidad
  reducida * (precio de venta - avg_price) * signo).

Dunder #3: Position + Fill -> Position NUEVA.
Inmutable como Trade: sumar un fill no muta el estado, produce el
siguiente. Consecuencia: reconstruir el portafolio desde el log de
trades es un reduce de sumas => determinista por construcción
(la avanzada de la sección 5, resuelta por diseño).

Dominio puro: solo stdlib.
"""

from dataclasses import dataclass
from functools import reduce


@dataclass(frozen=True)
class Fill:
    """Una ejecución propia: cantidad CON SIGNO (+compra, -venta)."""

    symbol: str
    qty: float          # +10 = compra 10; -4 = vende 4
    price: float


@dataclass(frozen=True)
class Position:
    """Posición abierta de un símbolo. qty=0 => posición plana."""

    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def __add__(self, fill: Fill) -> "Position":
        """Aplica un fill y devuelve la SIGUIENTE posición.

        Tres casos, en orden de aparición en la vida real:
        1) Mismo sentido (o posición plana): re-ponderar avg_price.
        2) Sentido contrario sin cruzar cero: reducir. avg intacto,
           realized_pnl materializa la diferencia.
        3) Sentido contrario cruzando cero (flip): se cierra todo lo
           abierto (realizando PnL) y el sobrante abre posición nueva
           al precio del fill.
        """
        if not isinstance(fill, Fill):
            return NotImplemented
        if fill.symbol != self.symbol:
            # Sumar fills de otro símbolo sería corrupción silenciosa
            # de la posición: fail-fast ruidoso, como siempre.
            raise ValueError(f"Fill de {fill.symbol} sobre posición de {self.symbol}")

        # Caso 1: plana, o fill en el MISMO sentido que la posición.
        if self.qty == 0 or (self.qty > 0) == (fill.qty > 0):
            new_qty = self.qty + fill.qty
            # Media ponderada por volumen: el coste viejo + el nuevo,
            # repartido entre la cantidad total.
            new_avg = (self.qty * self.avg_price + fill.qty * fill.price) / new_qty
            return Position(self.symbol, new_qty, new_avg, self.realized_pnl)

        # Sentido contrario: ¿reduce o cruza cero?
        if abs(fill.qty) <= abs(self.qty):
            # Caso 2: reducción (parcial o cierre exacto).
            # PnL realizado sobre la cantidad reducida. El signo de
            # self.qty orienta la resta: en largos ganas si vendes
            # por encima del avg; en cortos, si recompras por debajo.
            reduced = abs(fill.qty)
            direction = 1 if self.qty > 0 else -1
            pnl = reduced * (fill.price - self.avg_price) * direction
            new_qty = self.qty + fill.qty
            # avg_price NO cambia... salvo cierre total, donde una
            # posición plana no tiene precio de entrada.
            new_avg = self.avg_price if new_qty != 0 else 0.0
            return Position(self.symbol, new_qty, new_avg,
                            self.realized_pnl + pnl)

        # Caso 3: flip. Primero cierra TODO lo abierto (caso 2 con
        # cierre exacto), luego abre el sobrante al precio del fill.
        closing = Fill(self.symbol, -self.qty, fill.price)
        flat = self + closing
        remainder = Fill(self.symbol, fill.qty + self.qty, fill.price)
        return flat + remainder

    @property
    def cost_basis(self) -> float:
        """Coste de lo abierto: qty * avg_price (parte del invariante)."""
        return self.qty * self.avg_price

    def unrealized_pnl(self, mark_price: float) -> float:
        """PnL latente al precio de mercado dado. Separado del
        realizado: uno es opinión del mercado, el otro es historia."""
        return self.qty * (mark_price - self.avg_price)


def position_from_fills(symbol: str, fills: list[Fill]) -> Position:
    """Reconstrucción determinista del estado desde el log de fills
    (memoria, sección 5 avanzada): mismo log => misma posición,
    SIEMPRE. El estado es una función pura de la historia; por eso
    puede reconstruirse, auditarse y testearse."""
    return reduce(lambda pos, f: pos + f, fills, Position(symbol))
