"""Las tres estrategias del enunciado: cruce de medias, momentum y
patrón de velas.

Contrato común (vigilado por StrategyMeta): DataFrame de velas entra,
Series de señales {-1, 0, +1} sale, alineada con el índice temporal.

CONCEPTO CENTRAL — look-ahead bias (sesgo de anticipación):
la señal de la vela t solo puede usar información disponible AL
CIERRE de t. Todas las operaciones de abajo (rolling, pct_change,
shift) miran hacia ATRÁS. El matiz de ejecución queda para el PnL:
una señal conocida al cierre de t se ejecuta al precio de t+1
(no se puede comprar a un precio que dejó de existir al conocerse).
Una estrategia con look-ahead gana siempre en backtest y pierde en
real: es EL bug cuantitativo por excelencia.
"""

import pandas as pd

from src.core.instrumentation import measure_latency
from src.strategies.base import Strategy

# Nota: importar este módulo REGISTRA las tres estrategias (efecto
# de StrategyMeta). El backend solo tendrá que importar y leer
# STRATEGY_REGISTRY.


class SMACrossStrategy(Strategy):
    """Cruce de medias móviles: tendencial clásica.

    SMA rápida > SMA lenta => el precio reciente empuja por encima
    de su historia larga => tendencia alcista => largo (+1).
    Por debajo => corto (-1).
    """

    name = "sma_cross"

    def __init__(self, fast: int = 20, slow: int = 50) -> None:
        if fast >= slow:
            # Validación de parámetros en construcción: una SMA
            # "rápida" más lenta que la "lenta" invierte las señales
            # silenciosamente. Fail-fast también en los parámetros.
            raise ValueError(f"fast ({fast}) debe ser < slow ({slow})")
        self.fast = fast
        self.slow = slow

    @measure_latency  # <- el decorador del enunciado, en el sistema real
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        fast_sma = candles["close"].rolling(self.fast).mean()
        slow_sma = candles["close"].rolling(self.slow).mean()

        # WARMUP: las primeras slow-1 velas no tienen SMA lenta
        # (rolling devuelve NaN hasta acumular ventana completa).
        # Cualquier comparación con NaN es False, así que ahí la
        # señal queda 0 = "sin opinión". Explícito mejor que
        # accidental: donde no hay datos suficientes, no se opera.
        signals = pd.Series(0, index=candles.index, dtype=int)
        signals[fast_sma > slow_sma] = 1
        signals[fast_sma < slow_sma] = -1
        return signals


class MomentumStrategy(Strategy):
    """Momentum: lo que subió tiende a seguir subiendo (a corto plazo).

    Retorno acumulado en `lookback` velas comparado contra un umbral.
    La zona muerta [-threshold, +threshold] evita operar sobre ruido:
    un +0.1% en 24h no es momentum, es respiración del mercado.
    """

    name = "momentum"

    def __init__(self, lookback: int = 24, threshold: float = 0.02) -> None:
        self.lookback = lookback
        self.threshold = threshold

    @measure_latency
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        # pct_change(n) = close_t / close_{t-n} - 1: mira n velas
        # HACIA ATRÁS. (pct_change(-n) miraría al futuro: look-ahead
        # instantáneo. El signo de un parámetro separa una estrategia
        # de una máquina del tiempo.)
        returns = candles["close"].pct_change(self.lookback)

        signals = pd.Series(0, index=candles.index, dtype=int)
        signals[returns > self.threshold] = 1
        signals[returns < -self.threshold] = -1
        return signals


class EngulfingStrategy(Strategy):
    """Patrón envolvente (engulfing): reversión en dos velas.

    Envolvente ALCISTA: tras una vela bajista, una vela alcista cuyo
    CUERPO cubre por completo el cuerpo de la anterior => la presión
    compradora ha absorbido a la vendedora => +1.
    Envolvente bajista: el espejo => -1.

    Solo cuerpos (open/close), no mechas: la definición clásica del
    patrón, y la menos ambigua de codificar.
    """

    name = "engulfing"

    @measure_latency
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        open_, close = candles["open"], candles["close"]
        # shift(1) = la vela ANTERIOR alineada con la actual.
        # (shift(-1) sería la siguiente: otra vez la frontera entre
        # estrategia y máquina del tiempo es un signo.)
        prev_open, prev_close = open_.shift(1), close.shift(1)

        bullish = (
            (prev_close < prev_open)      # la anterior fue bajista
            & (close > open_)             # la actual es alcista
            & (open_ <= prev_close)       # abre por debajo del cierre previo
            & (close >= prev_open)        # y cierra por encima de la apertura previa
        )                                 # => el cuerpo actual envuelve al previo
        bearish = (
            (prev_close > prev_open)
            & (close < open_)
            & (open_ >= prev_close)
            & (close <= prev_open)
        )

        signals = pd.Series(0, index=candles.index, dtype=int)
        signals[bullish] = 1
        signals[bearish] = -1
        return signals


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo sobre DATOS REALES (las velas que ya descargaste):
    #   python -m src.strategies.classic
    # Corre las tres estrategias vía el REGISTRO (como hará el
    # backend) e imprime el reparto de señales + el informe de
    # latencia del decorador trabajando en el sistema real.
    # --------------------------------------------------------------
    from src.core.instrumentation import latency_report
    from src.storage.database import get_connection
    from src.storage.queries import load_candles_df
    from src.strategies.base import STRATEGY_REGISTRY

    conn = get_connection()
    df = load_candles_df(conn, "BTCUSDT", "1h")
    print(f"BTCUSDT 1h: {len(df)} velas cargadas de SQLite\n")

    # Instanciamos DESDE EL REGISTRO, no importando clases a mano:
    # exactamente el mecanismo que usará FastAPI.
    for name, cls in STRATEGY_REGISTRY.items():
        strategy = cls()
        signals = strategy.generate_signals(df)
        counts = signals.value_counts().to_dict()
        print(f"{name:>10}: largo={counts.get(1, 0):>3}  "
              f"fuera={counts.get(0, 0):>3}  corto={counts.get(-1, 0):>3}  "
              f"| última señal: {signals.iloc[-1]:+d}")

    print("\nLatencia del decorador sobre generate_signals:")
    for fname, stats in latency_report().items():
        print(f"  {fname}: n={stats['count']}  p50={stats['p50_ms']}ms")
    conn.close()
