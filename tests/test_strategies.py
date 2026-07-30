"""Tests unitarios de estrategias.

Filosofía : datos sintéticos DETERMINISTAS donde
la señal esperada se calcula a mano ANTES de ejecutar. Si la
estrategia dice otra cosa, la estrategia está mal. Esta es la defensa
estructural contra el look-ahead y los errores silenciosos: un dataset
donde la respuesta correcta se conoce de antemano no puede ser
engañado por números "plausibles".

Nota de diseño: usamos ventanas diminutas (fast=2, slow=3) en vez de
las de producción (20/50). La LÓGICA probada es idéntica; la
aritmética esperada cabe en la cabeza. Poder hacerlo es consecuencia
de que los parámetros van en el constructor: diseño para testabilidad.

Ejecutar:  python -m pytest tests/ -v
"""

import pandas as pd

from src.strategies.classic import EngulfingStrategy, MomentumStrategy, SMACrossStrategy


def make_candles(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    """Fabrica un DataFrame de velas mínimo a partir de los cierres.

    Si no se dan aperturas, cada vela abre en el cierre anterior
    (mercado continuo sin gaps). high/low/volume son de relleno:
    las estrategias actuales no los usan, pero el contrato los exige.
    """
    if opens is None:
        opens = [closes[0]] + closes[:-1]
    index = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, closes)],
            "low": [min(o, c) for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [1.0] * len(closes),
        },
        index=index,
    )


# ---------------------------------------------------------------------
# SMA cross
# ---------------------------------------------------------------------

def test_sma_cross_señal_larga_cuando_rapida_supera_lenta():
    # Cierres: [10, 10, 10, 20, 30, 40]
    # En la última vela: SMA2 = (30+40)/2 = 35 ; SMA3 = (20+30+40)/3 = 30
    # 35 > 30 => señal esperada: +1. Calculado a mano, sin fe.
    df = make_candles([10, 10, 10, 20, 30, 40])
    signals = SMACrossStrategy(fast=2, slow=3).generate_signals(df)
    assert signals.iloc[-1] == 1


def test_sma_cross_señal_corta_cuando_rapida_bajo_lenta():
    # Cierres: [40, 40, 40, 30, 20, 10]
    # Última vela: SMA2 = 15 ; SMA3 = 20  =>  15 < 20  =>  -1
    df = make_candles([40, 40, 40, 30, 20, 10])
    signals = SMACrossStrategy(fast=2, slow=3).generate_signals(df)
    assert signals.iloc[-1] == -1


def test_sma_cross_warmup_sin_señal():
    # Con slow=3, las velas 0 y 1 no tienen SMA3 (NaN) => señal 0.
    # "Donde no hay datos suficientes, no se opera" hecho assert.
    df = make_candles([10, 20, 30, 40])
    signals = SMACrossStrategy(fast=2, slow=3).generate_signals(df)
    assert signals.iloc[0] == 0
    assert signals.iloc[1] == 0


def test_sma_cross_rechaza_parametros_invertidos():
    # fast >= slow invertiría la semántica de las señales de forma
    # silenciosa: el constructor debe explotar ANTES (fail-fast).
    import pytest

    with pytest.raises(ValueError):
        SMACrossStrategy(fast=50, slow=20)


# ---------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------

def test_momentum_señal_larga_sobre_umbral():
    # lookback=2, threshold=0.05
    # Última vela: 120/100 - 1 = +0.20 > 0.05  =>  +1
    df = make_candles([100, 100, 100, 120])
    signals = MomentumStrategy(lookback=2, threshold=0.05).generate_signals(df)
    assert signals.iloc[-1] == 1


def test_momentum_zona_muerta_sin_señal():
    # Última vela: 101/100 - 1 = +0.01 < 0.05  =>  0
    # La zona muerta existe para no operar sobre ruido: probado.
    df = make_candles([100, 100, 100, 101])
    signals = MomentumStrategy(lookback=2, threshold=0.05).generate_signals(df)
    assert signals.iloc[-1] == 0


def test_momentum_señal_corta_bajo_umbral():
    # Última vela: 80/100 - 1 = -0.20 < -0.05  =>  -1
    df = make_candles([100, 100, 100, 80])
    signals = MomentumStrategy(lookback=2, threshold=0.05).generate_signals(df)
    assert signals.iloc[-1] == -1


# ---------------------------------------------------------------------
# Engulfing
# ---------------------------------------------------------------------

def test_engulfing_alcista_detectado():
    # Vela previa BAJISTA: open=110, close=100 (cuerpo 100-110)
    # Vela actual ALCISTA: open=99, close=111 (cuerpo 99-111)
    # El cuerpo actual envuelve al previo => +1 en la última vela.
    df = make_candles(closes=[100, 111], opens=[110, 99])
    signals = EngulfingStrategy().generate_signals(df)
    assert signals.iloc[-1] == 1


def test_engulfing_bajista_detectado():
    # Espejo exacto: previa alcista envuelta por actual bajista => -1
    df = make_candles(closes=[110, 99], opens=[100, 111])
    signals = EngulfingStrategy().generate_signals(df)
    assert signals.iloc[-1] == -1


def test_engulfing_cuerpo_no_envolvente_sin_señal():
    # Vela previa bajista open=110 close=100; actual alcista
    # open=105 close=108: sube, pero su cuerpo NO cubre el previo
    # (no llega ni a 110 ni baja de 100) => 0.
    # Este test protege la DEFINICIÓN del patrón: subir no basta.
    df = make_candles(closes=[100, 108], opens=[110, 105])
    signals = EngulfingStrategy().generate_signals(df)
    assert signals.iloc[-1] == 0
