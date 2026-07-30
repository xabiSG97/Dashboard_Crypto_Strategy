"""Tests de Position: el invariante, verificado

Cada caso está calculado a mano en el comentario ANTES del assert:
la misma disciplina de siempre — la respuesta se conoce de antemano.
"""

import pytest

from src.domain.position import Fill, Position, position_from_fills


def test_precio_medio_tras_dos_compras():
    # Compro 10 @ 100 y 10 @ 120.
    # avg = (10*100 + 10*120) / 20 = 110. qty = 20.
    pos = Position("BTCUSDT") + Fill("BTCUSDT", 10, 100.0) + Fill("BTCUSDT", 10, 120.0)
    assert pos.qty == 20
    assert pos.avg_price == 110.0
    assert pos.realized_pnl == 0.0  # comprar no realiza nada


def test_venta_parcial_no_toca_avg_y_realiza_pnl():
    # LA REGLA DE ORO: partiendo de 20 @ 110, vendo 5 @ 130.
    # - avg de lo que queda: SIGUE siendo 110 (vender no abarata tu entrada)
    # - realized: 5 * (130 - 110) = +100
    pos = Position("BTCUSDT", qty=20, avg_price=110.0) + Fill("BTCUSDT", -5, 130.0)
    assert pos.qty == 15
    assert pos.avg_price == 110.0
    assert pos.realized_pnl == 100.0


def test_cierre_total_realiza_todo():
    # 15 @ 110, vendo los 15 @ 105: realized = 15 * (105-110) = -75.
    # Posición plana => avg vuelve a 0 (sin cantidad no hay entrada).
    pos = Position("BTCUSDT", qty=15, avg_price=110.0) + Fill("BTCUSDT", -15, 105.0)
    assert pos.qty == 0
    assert pos.avg_price == 0.0
    assert pos.realized_pnl == -75.0


def test_flip_cruza_cero():
    # Largo 10 @ 100; vendo 15 @ 120.
    # Tramo 1: cierra los 10 => realized = 10 * (120-100) = +200.
    # Tramo 2: abre CORTO de 5 @ 120 (el precio del fill, no el avg viejo).
    pos = Position("BTCUSDT", qty=10, avg_price=100.0) + Fill("BTCUSDT", -15, 120.0)
    assert pos.qty == -5
    assert pos.avg_price == 120.0
    assert pos.realized_pnl == 200.0


def test_corto_gana_si_recompra_por_debajo():
    # El espejo direccional: corto 10 @ 100, recompro 10 @ 90.
    # realized = 10 * (90 - 100) * (-1) = +100. Los cortos ganan bajando.
    pos = Position("BTCUSDT", qty=-10, avg_price=100.0) + Fill("BTCUSDT", 10, 90.0)
    assert pos.qty == 0
    assert pos.realized_pnl == 100.0


def test_reconstruccion_desde_log_es_determinista():
    # Sección 5 avanzada: el estado es función pura del log.
    # Reconstruir dos veces el mismo log => posiciones IDÉNTICAS.
    fills = [
        Fill("ETHUSDT", 10, 100.0),
        Fill("ETHUSDT", 10, 120.0),
        Fill("ETHUSDT", -5, 130.0),
        Fill("ETHUSDT", -20, 90.0),   # flip a corto
    ]
    a = position_from_fills("ETHUSDT", fills)
    b = position_from_fills("ETHUSDT", fills)
    assert a == b  # dataclass compara campo a campo: estado idéntico


def test_fill_de_otro_simbolo_explota():
    # Corrupción silenciosa convertida en error ruidoso (el patrón
    # de siempre): sumar un fill de ETH a la posición de BTC.
    with pytest.raises(ValueError):
        Position("BTCUSDT", qty=1, avg_price=100.0) + Fill("ETHUSDT", 1, 50.0)
