"""Tests de invariantes de PnL 

Un INVARIANTE es una propiedad que debe cumplirse para CUALQUIER
dato, no un caso concreto: "señal plana => PnL cero", "la señal
nacida en la vela del salto no captura el salto". Testear invariantes
protege contra clases enteras de bugs, no contra ejemplos sueltos.
"""

import pandas as pd

from src.core.pnl import pnl_summary, strategy_returns


def make_candles(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame({"close": closes}, index=index)


def test_invariante_sin_senal_no_hay_pnl():
    # Señal 0 en todas las velas => posición 0 siempre => retorno 0
    # SIEMPRE, se mueva el mercado lo que se mueva. Si este test
    # falla, hay PnL fantasma: dinero apareciendo sin operar.
    candles = make_candles([100, 150, 80, 200, 50])  # mercado loco
    signals = pd.Series(0, index=candles.index)
    returns = strategy_returns(candles, signals)
    assert (returns == 0).all()


def test_invariante_largo_permanente_compone_retornos():
    # Cierres 100 -> 110 -> 121 (dos velas de +10%), señal +1 siempre.
    # positions = shift(1) => [0, 1, 1]: la primera vela NO se opera
    # (la señal de la vela 0 se ejecuta en la 1).
    # Total = (1.10 * 1.10) - 1 = +21% exacto.
    candles = make_candles([100, 110, 121])
    signals = pd.Series(1, index=candles.index)
    returns = strategy_returns(candles, signals)
    total = (1 + returns).prod() - 1
    assert abs(total - 0.21) < 1e-12


def test_invariante_la_senal_del_salto_no_captura_el_salto():
    # EL test anti-look-ahead de la capa de ejecución:
    # cierres [100, 100, 200] (salto de +100% en la última vela) y
    # la señal aparece JUSTO en la vela del salto: [0, 0, 1].
    # positions = shift(1) => [0, 0, 0]: nunca posicionados.
    # PnL total = 0. Un PnL > 0 aquí sería la máquina del tiempo:
    # cobrar un movimiento que solo se conoció al terminar.
    candles = make_candles([100, 100, 200])
    signals = pd.Series([0, 0, 1], index=candles.index)
    returns = strategy_returns(candles, signals)
    assert (returns == 0).all()

    # Y el contraste: si la señal nace UNA vela antes ([0, 1, 0]),
    # la posición SÍ está activa durante el salto y lo captura entero.
    signals_early = pd.Series([0, 1, 0], index=candles.index)
    returns_early = strategy_returns(candles, signals_early)
    assert abs((1 + returns_early).prod() - 1 - 1.0) < 1e-12  # +100%


def test_invariante_corto_es_el_espejo_del_largo():
    # Mismo mercado, señales opuestas => retornos opuestos vela a
    # vela (con retornos simples): r_corto = -r_largo.
    candles = make_candles([100, 110, 99, 120])
    long_r = strategy_returns(candles, pd.Series(1, index=candles.index))
    short_r = strategy_returns(candles, pd.Series(-1, index=candles.index))
    assert ((long_r + short_r).abs() < 1e-12).all()


def test_pnl_summary_serie_vacia_devuelve_ceros():
    # Robustez del borde: sin datos, el resumen no explota ni
    # inventa: todo a 0. (Un activo recién añadido sin histórico
    # pasaría por aquí.)
    summary = pnl_summary(pd.Series(dtype=float))
    assert summary == {"daily": 0.0, "monthly": 0.0, "qtd": 0.0,
                       "ytd": 0.0, "total": 0.0}
