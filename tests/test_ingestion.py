"""Tests del camino de ingesta: monotonicidad: "test determinista que detecte violaciones de
monotonicidad") y parseo del WebSocket (sección 10 avanzada: pipeline
WS con generador de eventos controlado, en su versión mínima).
"""

from src.domain.monotonicity import MonotonicityValidator
from src.ingestion.ws_client import parse_trade
from src.storage.database import get_connection, init_db, insert_trade


# ---------------------------------------------------------------------
# Monotonicidad: la demo del __main__, ahora como asserts permanentes.
# ---------------------------------------------------------------------

def test_monotonicidad_secuencia_con_desorden_fabricado():
    # Secuencia controlada: UNA violación exacta (1002 tras 1005).
    # Los timestamps repetidos NO son violación (no-decreciente).
    validator = MonotonicityValidator()
    secuencia = [
        ("BTCUSDT", 1000),
        ("BTCUSDT", 1005),
        ("ETHUSDT", 500),
        ("BTCUSDT", 1005),   # repetido: permitido
        ("BTCUSDT", 1002),   # RETROCESO: la violación
        ("ETHUSDT", 501),
        ("BTCUSDT", 1010),
    ]
    for symbol, ts in secuencia:
        validator.check(symbol, ts)

    assert len(validator.violations) == 1
    v = validator.violations[0]
    assert v.symbol == "BTCUSDT"
    assert v.backwards_ms == 3  # 1005 - 1002


def test_monotonicidad_no_baja_el_liston():
    # Tras 1005, llegan TRES ticks atrasados en orden entre sí:
    # 1002, 1003, 1004. Si el listón bajara, contaríamos 1;
    # el listón es el MÁXIMO visto => son 3 violaciones.
    validator = MonotonicityValidator()
    for ts in [1000, 1005, 1002, 1003, 1004]:
        validator.check("BTCUSDT", ts)
    assert len(validator.violations) == 3


def test_monotonicidad_simbolos_independientes():
    # El desorden de BTC no contamina a ETH: estado POR símbolo.
    validator = MonotonicityValidator()
    validator.check("BTCUSDT", 1000)
    validator.check("ETHUSDT", 10)     # muy "atrasado" respecto a BTC: irrelevante
    validator.check("BTCUSDT", 900)    # violación de BTC
    validator.check("ETHUSDT", 11)     # ETH sigue limpio
    assert len(validator.violations) == 1
    assert validator.violations[0].symbol == "BTCUSDT"


# ---------------------------------------------------------------------
# El camino del WebSocket con evento controlado: parse -> entidad ->
# persistencia idempotente. El "generador de eventos" en su versión
# mínima: un dict fabricado con el formato EXACTO de Binance.
# ---------------------------------------------------------------------

def fake_binance_trade_event(trade_id: int = 12345) -> dict:
    """Un evento 'trade' de Binance, fabricado (claves de una letra
    tal cual las envía el stream)."""
    return {
        "e": "trade", "s": "BTCUSDT", "t": trade_id,
        "p": "60000.50", "q": "0.01000000",
        "T": 1720260000000, "m": False,
    }


def test_parse_trade_traduce_el_formato_de_binance():
    # La frontera exchange->dominio, verificada: strings a floats,
    # claves crípticas a nombres con significado, ingestion_time
    # estampado por NOSOTROS (no viene de Binance).
    trade = parse_trade(fake_binance_trade_event(), ingestion_time_ms=1720260000450)
    assert trade.symbol == "BTCUSDT"
    assert trade.trade_id == 12345
    assert trade.price == 60000.50
    assert trade.qty == 0.01
    assert trade.event_time_ms == 1720260000000
    assert trade.latency_ms == 450          # 450 - 0: la resta de fronteras
    assert trade.is_buyer_maker is False


def test_camino_ws_persiste_idempotente(tmp_path):
    # El mismo evento llega DOS veces (reconexión simulada): la
    # segunda inserción debe ser rechazada por la primary key.
    # parse real + BD real; lo único falso es Binance. Reproducible:
    # mismo input => mismo estado final (sección 10 avanzada).
    conn = get_connection(tmp_path / "ws.db")
    init_db(conn)

    trade = parse_trade(fake_binance_trade_event(), ingestion_time_ms=1720260000450)
    kwargs = dict(symbol=trade.symbol, trade_id=trade.trade_id,
                  price=trade.price, qty=trade.qty,
                  event_time_ms=trade.event_time_ms,
                  is_buyer_maker=trade.is_buyer_maker)

    assert insert_trade(conn, **kwargs) is True    # primera: entra
    assert insert_trade(conn, **kwargs) is False   # segunda: ignorada
    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert total == 1
    conn.close()
