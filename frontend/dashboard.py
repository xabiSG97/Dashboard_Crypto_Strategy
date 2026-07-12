"""Dashboard Streamlit del portafolio de criptoactivos.

Decisiones de arquitectura (memoria, sección de frontend):
- El dashboard habla SOLO con la API de FastAPI, nunca con SQLite:
  es un cliente más (igual que Swagger). Las garantías de consistencia
  viven en el backend; puentearlo sería duplicar lógica sin garantías.
- "Tiempo real" por POLLING (refresco de 2s): el requisito de
  WebSocket se satisface en la INGESTA (donde importa la frescura de
  datos); para visualización humana, 2s es funcionalmente equivalente
  y elimina la complejidad de sockets en Streamlit. Frescura de
  datos != frescura de píxeles.
- CACHING con TTL de dos velocidades (requisito avanzado "caching
  básico"): lo casi-estático (símbolos, estrategias) expira a 300s;
  lo vivo (velas, PnL, trades) a 2s. Qué se cachea + política de
  expiración, explícitos.

Arrancar (con el backend ya corriendo en otra terminal):
    streamlit run frontend/dashboard.py
"""

import time

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

API = "http://127.0.0.1:8000"

st.set_page_config(page_title="Crypto Dashboard", layout="wide")


# ---------------------------------------------------------------------
# Capa de acceso a la API: dos cachés, dos velocidades.
# st.cache_data memoriza el resultado por (función + argumentos) y lo
# reutiliza hasta que caduque el ttl: N widgets pidiendo lo mismo en
# el mismo refresco = 1 sola petición HTTP real.
# ---------------------------------------------------------------------

@st.cache_data(ttl=300)
def api_static(path: str) -> list:
    """Datos que apenas cambian: símbolos, estrategias. TTL 5 min."""
    r = httpx.get(f"{API}{path}", timeout=10)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=2)
def api_live(path: str, **params) -> list | dict:
    """Datos vivos: velas, señales, PnL, trades. TTL 2 s (alineado
    con el ciclo de refresco: cada rerun ve datos frescos)."""
    r = httpx.get(f"{API}{path}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------
# Sidebar: los filtros que pide el enunciado (activo, estrategia,
# rango temporal) + el interruptor de tiempo real.
# ---------------------------------------------------------------------

st.sidebar.title("⚙️ Controles")

try:
    symbols = api_static("/symbols")
    strategies = api_static("/strategies")
except httpx.ConnectError:
    st.error("No se puede conectar con la API. ¿Está corriendo uvicorn "
             "en http://127.0.0.1:8000?")
    st.stop()

symbol = st.sidebar.selectbox("Activo", symbols)
strategy = st.sidebar.selectbox("Estrategia", strategies)
# Filtro temporal: cuántas velas hacia atrás (24h ... 90 días).
limit = st.sidebar.slider("Ventana (velas 1h)", min_value=24,
                          max_value=2160, value=336, step=24)
auto_refresh = st.sidebar.checkbox("🔴 Tiempo real (refresco 2s)", value=False)

st.title(f"📊 {symbol} — estrategia: {strategy}")

# ---------------------------------------------------------------------
# Fila de PnL del PORTAFOLIO equiponderado (no del activo suelto):
# diario, mensual, QTD, YTD, total — los cinco del enunciado.
# ---------------------------------------------------------------------

pnl = api_live("/pnl", strategy=strategy)
cols = st.columns(5)
for col, (label, key) in zip(cols, [("Diario", "daily"), ("Mensual", "monthly"),
                                     ("QTD", "qtd"), ("YTD", "ytd"),
                                     ("Total", "total")]):
    value = pnl[key] * 100
    col.metric(f"PnL {label} (portafolio)", f"{value:+.2f}%")

# ---------------------------------------------------------------------
# Candlestick histórico + señales + indicadores.
# ---------------------------------------------------------------------

candles = pd.DataFrame(api_live(f"/candles/{symbol}", limit=limit))
candles["open_time"] = pd.to_datetime(candles["open_time"])

signals = pd.DataFrame(api_live(f"/signals/{symbol}", strategy=strategy,
                                limit=limit))
signals["time"] = pd.to_datetime(signals["time"])

fig = go.Figure()
fig.add_trace(go.Candlestick(
    x=candles["open_time"], open=candles["open"], high=candles["high"],
    low=candles["low"], close=candles["close"], name=symbol,
))

# Indicadores: para el cruce de medias, pintamos las dos SMAs.
# Cálculo en el frontend: LEGÍTIMO porque es decoración visual
# (ayuda a entender el gráfico), no decisión. El PnL jamás se
# calcularía aquí: "fuerte para lo que decide, eventual (y local)
# para lo que decora".
if strategy == "sma_cross":
    for window, color in [(20, "#f39c12"), (50, "#8e44ad")]:
        sma = candles["close"].rolling(window).mean()
        fig.add_trace(go.Scatter(x=candles["open_time"], y=sma,
                                 name=f"SMA{window}", line=dict(color=color)))

# Señales sobre el precio: triángulo verde = +1, rojo = -1.
# NOTA (carne de entrevista): el marcador se pinta en la vela donde
# la señal NACE (t). El PnL, en cambio, ejecuta en t+1 (shift).
# No es una inconsistencia: el gráfico responde "¿cuándo lo supe?"
# y el PnL responde "¿cuándo pude actuar?". Dos preguntas, dos t.
merged = candles.merge(signals, left_on="open_time", right_on="time")
buys = merged[merged["signal"] == 1]
sells = merged[merged["signal"] == -1]
fig.add_trace(go.Scatter(
    x=buys["open_time"], y=buys["low"] * 0.998, mode="markers",
    marker=dict(symbol="triangle-up", size=11, color="#2ecc71"),
    name="Señal largo (+1)",
))
fig.add_trace(go.Scatter(
    x=sells["open_time"], y=sells["high"] * 1.002, mode="markers",
    marker=dict(symbol="triangle-down", size=11, color="#e74c3c"),
    name="Señal corto (-1)",
))
fig.update_layout(height=520, xaxis_rangeslider_visible=False,
                  margin=dict(l=10, r=10, t=30, b=10))
st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------------
# Último precio + tabla de trades en vivo (del WebSocket, vía API).
# ---------------------------------------------------------------------

left, right = st.columns([1, 2])

with left:
    last_close = candles["close"].iloc[-1]
    prev_close = candles["close"].iloc[-2]
    delta = (last_close / prev_close - 1) * 100
    st.metric(f"Último cierre {symbol}", f"{last_close:,.4f}",
              f"{delta:+.2f}% vs vela anterior")
    st.caption("Se actualiza al cierre de cada vela 1h; los ticks "
               "en vivo llegan a la tabla de la derecha.")

with right:
    st.subheader("Últimos trades (ingesta WebSocket)")
    trades = api_live(f"/trades/{symbol}", limit=15)
    if trades:
        st.dataframe(pd.DataFrame(trades), use_container_width=True,
                     hide_index=True)
    else:
        st.info("Sin trades en la BD para este símbolo. Arranca el "
                "ingestor (`python -m src.ingestion.ws_client`) para "
                "ver el flujo en vivo.")

# ---------------------------------------------------------------------
# El "tiempo real": si está activado, esta ejecución del script
# duerme 2s y se relanza. Cada rerun repinta TODO con datos frescos
# (los TTL de 2s ya han caducado). Streamlit es un bucle de render:
# no hay estado que actualizar, hay mundo que redibujar.
# ---------------------------------------------------------------------

if auto_refresh:
    time.sleep(2)
    st.rerun()
