#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QT Chain Scanner v6  -  escanea los perpetuos USDT de BingX (o Binance) Futures en 5m y envia
por Telegram senales LONG / SHORT listas para abrir A MANO. NO opera: solo avisa.

Logica (replica del Pine "QT Chain Map + Entradas v3"):
  - 7 ciclos (Yearly, Monthly, Weekly, Daily, 90 Min, Micro, Nano) en hora de Nueva York
  - Cadenas (3+ ciclos adyacentes en el mismo cuarto, mas puente Q4->Q1)
  - Barrido del Q1 del ciclo de 90 min + vela de confirmacion fuerte
  - Sesgo por True Opens (Daily y Session)
  - SL / TP1 (parcial) / TP2

Mejoras v4 (pensadas para trading manual):
  - Mensaje claro: entrada, "no entres si pasa de X", SL, TP1, TP2, % y R de cada nivel
  - Calculadora de posicion: cantidad, nocional y apalancamiento segun tu cuenta y riesgo
  - Puntuacion 1-5 de cada senal (cadena, sesgo, barrido limpio, vela fuerte) + filtro MIN_SCORE
  - Botones en Telegram: abrir el par en el exchange / TradingView
  - Seguimiento: avisa (respondiendo al mensaje original) cuando toca TP1, TP2, SL o caduca
  - Resumen diario con resultados simulados en R
  - Filtros: riesgo maximo, cooldown por moneda, limite diario, monedas excluidas
  - Todas las opciones se pueden cambiar con Variables de Railway (ver README)

Novedades v6: soporte BingX (por defecto, todas las monedas USDT con volumen suficiente, sin los perpetuos
TradFi), AUTOAJUSTE walk-forward con validacion (ver seccion AUTOAJUSTE), proteccion contra velas desfasadas,
--check-exchange para verificar la conexion.

Mejoras v5 (diagnostico, validacion con datos y filtros con base en la investigacion):
  - --backtest: reproduce la estrategia sobre el historico del exchange (comisiones incluidas) y
    muestra winrate, R medio, profit factor, drawdown y resultados por sesion/score/lado
  - --compare: prueba variantes (sin cadena, sin sesgo, otras sesiones, tendencia 1H, volumen...)
    y las separa en 1a/2a mitad del periodo para detectar sobreajuste
  - Embudo de filtros: cuenta que filtro bloquea mas senales (--backtest y aviso periodico)
  - Aviso "sigo activo" cada HEARTBEAT_HOURS con lo que esta frenando las senales
  - Filtros nuevos: tendencia 1H (TREND_FILTER), volumen relativo (MIN_REL_VOL), comisiones (MAX_FEE_R)
  - Puntuacion 0-7 (suma tendencia 1H a favor y volumen relativo alto)
  - Resultados simulados NETOS de comisiones; caducidad valorada a precio de mercado

NO incluye (frente al Pine): filtro SMT con 2o activo, True Open Weekly, noticias.

Uso:
    python qt_scanner_bot.py --selftest            # prueba offline de la logica
    python qt_scanner_bot.py --test-telegram       # mensaje de prueba + senal de ejemplo
    python qt_scanner_bot.py --once --dry-run      # un escaneo, solo imprime
    python qt_scanner_bot.py --check-exchange      # verifica la conexion con BingX/Binance y el formato de datos
    python qt_scanner_bot.py --autotune            # ejecuta una vez el autoajuste y muestra el ranking (no aplica)
    python qt_scanner_bot.py --backtest --days 14  # backtest + embudo de filtros
    python qt_scanner_bot.py --backtest --compare  # ademas compara variantes de la estrategia
    python qt_scanner_bot.py                       # bucle continuo cada 5 min
Aviso: no es consejo financiero. Prueba en demo / papel antes de usar dinero real.
"""
import argparse
import contextlib
import hashlib
import hmac
import io
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # el selftest funciona sin requests
    requests = None

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
LEVELS = ["Yearly", "Monthly", "Weekly", "Daily", "90 Min", "Micro", "Nano"]
SESSION_NAMES = ["Asia", "Londres", "NY AM", "NY PM"]
BAR_MS = 300_000
SCORE_MAX = 7
log = logging.getLogger("qt")


# ============================================================================
#  CONFIGURACION (cada campo se puede sobrescribir con una variable de entorno
#  con el mismo nombre en MAYUSCULAS, p. ej. min_score -> MIN_SCORE)
# ============================================================================
@dataclass
class Config:
    exchange: str = "bingx"                      # "bingx" | "binance"
    base_url: str = "https://open-api.bingx.com" # env: BASE_URL (por defecto segun el exchange)
    include_tradfi: bool = False                 # BingX: incluir perpetuos de acciones/materias primas/forex
    interval: str = "5m"                         # fijo (la logica de ciclos es de 5m)
    klines_limit: int = 499                      # fijo (~41 h de velas de 5m, peso 2 en la API)
    min_quote_volume: float = 20_000_000.0       # volumen 24h minimo en USDT
    max_symbols: int = 0                         # 0 = sin limite
    exclude_symbols: tuple = ()                  # ej. "BTCDOMUSDT,USDCUSDT"
    workers: int = 8
    # cadena
    lowest_level: str = "90 Min"                 # "90 Min" | "Micro" | "Nano"
    min_depth: int = 3
    need_level: str = "Daily"                    # "Ninguno" o un nivel
    chain_kind: str = "Ambas"                    # "Ambas" | "Normal" | "Puente"
    require_chain: bool = True
    # sesion: indices del Q diario permitidos (0 Asia, 1 Londres, 2 NY AM, 3 NY PM)
    sessions: tuple = (1, 2)
    # sesgo
    bias_mode: str = "true_opens"                # "none" | "true_opens"
    to_logic: str = "manipulacion"               # "manipulacion" | "tendencia"
    use_to_daily: bool = True
    use_to_session: bool = True
    # calidad de entrada
    max_bars_sweep: int = 6
    min_sweep_atr: float = 0.2
    max_sweep_atr: float = 3.0
    body_pct: float = 40.0
    min_q1_atr: float = 0.8
    max_q1_atr: float = 10.0
    sl_buf_atr: float = 0.1
    # filtros v5
    trend_filter: bool = False                   # exige tendencia 1H a favor (precio vs EMA y pendiente)
    trend_ema_bars: int = 240                    # EMA en velas de 5m (240 = EMA 20 de 1H aprox.)
    min_rel_vol: float = 0.0                     # volumen vela confirmacion / media 20 velas (0 = off)
    fee_pct: float = 0.05                        # comision taker por lado en % (BingX/Binance base: 0.05)
    slip_pct: float = 0.02                       # deslizamiento estimado por lado en % (backtest, autoajuste y filtro de costes)
    max_slip_r: float = 0.20                     # señal en vivo: descarta si el impacto REAL en el libro (ida y vuelta) > X R (0 = off)
    max_open_per_side: int = 5                   # maximo de señales abiertas en el mismo lado (las altcoins se mueven juntas; 0 = sin limite)
    btc_filter: bool = False                     # no operar contra la tendencia 1H de BTC
    max_fee_r: float = 0.35                      # descarta si comisiones ida y vuelta > X R (0 = off)
    rr: float = 2.0
    partial_r: float = 1.0
    partial_pct: float = 50.0                    # % de la posicion que se cierra en TP1
    min_risk_ticks: int = 4
    allow_long: bool = True
    allow_short: bool = True
    # filtros de envio (nuevos)
    min_score: int = 0                           # 0-5, recomendado 3 si recibes demasiadas
    max_risk_pct: float = 3.0                    # descarta senales con SL a mas de X% (0 = off)
    symbol_cooldown_min: int = 90                # minutos sin repetir senal en la misma moneda
    max_signals_per_day: int = 0                 # 0 = sin limite
    max_signals_per_scan: int = 10
    entry_chase_r: float = 0.3                   # "no entres si ya se movio mas de 0.3R"
    # tu cuenta (para la calculadora de posicion; 0 = no mostrar)
    account_size: float = 0.0                    # USDT
    risk_per_trade_pct: float = 1.0              # % de la cuenta que arriesgas por trade
    max_leverage: float = 10.0                   # tope de apalancamiento sugerido
    # seguimiento y resumen
    track_signals: bool = True
    track_hours: float = 6.0
    daily_summary_hour: int = 18                 # hora NY del resumen, -1 = desactivado
    heartbeat_hours: float = 8.0                 # aviso "sigo activo" + que filtra mas (0 = off)
    # autoajuste (walk-forward) -- ver seccion AUTOAJUSTE
    autotune: bool = True                        # reevalua variantes con datos recientes y adopta la validada
    autotune_mode: str = "apply"                 # "apply" aplica el cambio | "suggest" solo lo propone por Telegram
    autotune_hours: float = 24.0                 # cada cuantas horas reajusta
    autotune_days: int = 21                      # dias de historico (2/3 entrenamiento, 1/3 validacion)
    autotune_top: int = 0                        # monedas usadas para ajustar (0 = TODAS). Si limitas: 1/3 liquidas + muestra rotativa del resto
    autotune_max_minutes: float = 25.0           # presupuesto de tiempo del autoajuste
    autotune_min_trades: int = 40                # operaciones minimas simuladas para fiarse de una variante
    autotune_z: float = 2.2                      # cota inferior = R medio - z * error estandar
    autotune_min_gain_r: float = 0.10            # mejora minima (en R) sobre la actual para cambiar


DEFAULT_URL = {"bingx": "https://open-api.bingx.com", "binance": "https://fapi.binance.com"}
ENV_ALIASES = {}
ENV_SKIP = {"interval", "klines_limit", "base_url"}


def _to_bool(v):
    return v.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def apply_env(cfg):
    """Configura el bot desde las Variables de Railway sin tocar el codigo."""
    for f in fields(cfg):
        if f.name in ENV_SKIP:
            continue
        name = ENV_ALIASES.get(f.name, f.name.upper())
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            continue
        raw = raw.strip()
        cur = getattr(cfg, f.name)
        try:
            if isinstance(cur, bool):
                val = _to_bool(raw)
            elif isinstance(cur, int):
                val = int(float(raw))
            elif isinstance(cur, float):
                val = float(raw)
            elif isinstance(cur, tuple):
                items = [x.strip() for x in raw.split(",") if x.strip()]
                val = tuple(int(x) for x in items) if f.name == "sessions" else tuple(x.upper() for x in items)
            else:
                val = raw
        except ValueError:
            log.warning("valor invalido %s=%r: se usa el de por defecto", name, raw)
            continue
        setattr(cfg, f.name, val)
    cfg.exchange = cfg.exchange.strip().lower()
    explicit = os.environ.get("BASE_URL", "").strip()
    if not explicit and cfg.exchange == "binance":
        explicit = os.environ.get("BINANCE_BASE_URL", "").strip()
    cfg.base_url = (explicit or DEFAULT_URL.get(cfg.exchange, cfg.base_url)).rstrip("/")
    return cfg


def validate(cfg):
    errs = []
    if cfg.exchange not in DEFAULT_URL:
        errs.append("EXCHANGE debe ser bingx o binance (es %r)" % cfg.exchange)
    if cfg.autotune_mode not in ("apply", "suggest"):
        errs.append("AUTOTUNE_MODE debe ser apply o suggest (es %r)" % cfg.autotune_mode)
    if cfg.autotune_days < 7 or cfg.autotune_top < 0 or 0 < cfg.autotune_top < 5:
        errs.append("AUTOTUNE_DAYS >= 7 y AUTOTUNE_TOP = 0 (todas) o >= 5")
    if cfg.lowest_level not in LEVELS[4:]:
        errs.append("LOWEST_LEVEL debe ser '90 Min', 'Micro' o 'Nano' (es %r)" % cfg.lowest_level)
    if cfg.need_level != "Ninguno" and cfg.need_level not in LEVELS:
        errs.append("NEED_LEVEL debe ser 'Ninguno' o uno de %s (es %r)" % (LEVELS, cfg.need_level))
    if cfg.chain_kind not in ("Ambas", "Normal", "Puente"):
        errs.append("CHAIN_KIND debe ser Ambas, Normal o Puente (es %r)" % cfg.chain_kind)
    if cfg.bias_mode not in ("none", "true_opens"):
        errs.append("BIAS_MODE debe ser none o true_opens (es %r)" % cfg.bias_mode)
    if cfg.to_logic not in ("manipulacion", "tendencia"):
        errs.append("TO_LOGIC debe ser manipulacion o tendencia (es %r)" % cfg.to_logic)
    if any(s not in (0, 1, 2, 3) for s in cfg.sessions) or not cfg.sessions:
        errs.append("SESSIONS solo admite 0,1,2,3 (es %r)" % (cfg.sessions,))
    if not 0 <= cfg.partial_pct <= 100:
        errs.append("PARTIAL_PCT debe estar entre 0 y 100")
    if cfg.rr <= 0 or cfg.partial_r <= 0:
        errs.append("RR y PARTIAL_R deben ser > 0")
    if min(cfg.fee_pct, cfg.max_fee_r, cfg.slip_pct, cfg.max_slip_r, cfg.max_open_per_side) < 0:
        errs.append("FEE_PCT, SLIP_PCT, MAX_FEE_R, MAX_SLIP_R y MAX_OPEN_PER_SIDE no pueden ser negativos")
    if not 20 <= cfg.trend_ema_bars <= 400:
        errs.append("TREND_EMA_BARS debe estar entre 20 y 400")
    return errs


# ============================================================================
#  CUARTOS DE LOS 7 CICLOS (hora NY, dia de trading 18:00 -> 18:00)
# ============================================================================
def quarters(open_ms):
    utc = datetime.fromtimestamp(open_ms / 1000.0, tz=UTC)
    local = utc.astimezone(NY)
    shifted = (utc + timedelta(hours=6)).astimezone(NY)   # 18:00 NY -> 00:00 del dia de trading
    yr, mo, dom = shifted.year, shifted.month, shifted.day

    q_y = min((mo - 1) // 3, 3)
    first_wd = date(yr, mo, 1).weekday()                  # lunes = 0
    first_mon = 1 + (7 - first_wd) % 7                    # dia del primer lunes del mes
    q_mo = 3 if dom < first_mon else min((dom - first_mon) // 7, 3)
    wd = shifted.weekday()
    q_w = wd if wd <= 3 else -1                           # Lun-Jue = Q1-Q4, resto = Q0 (fuera)

    mins = (local.hour * 60 + local.minute - 1080 + 1440) % 1440
    q_d = min(mins // 360, 3)
    q_90 = min((mins % 360) // 90, 3)
    m90 = mins % 90
    q_mi = min(int(m90 // 22.5), 3)
    q_na = min(int((m90 % 22.5) // 5.625), 3)
    return {
        "qs": [q_y, q_mo, q_w, q_d, q_90, q_mi, q_na],
        "mins": mins, "m90": m90,
        "qD": q_d, "q90": q_90, "qMi": q_mi,
        "local": local,
    }


def detect_chain(qs, cfg):
    """Misma logica que el Pine: rachas de cuartos iguales (>= min_depth) y puente Q4->Q1."""
    n = LEVELS.index(cfg.lowest_level) + 1
    need_idx = -1 if cfg.need_level == "Ninguno" else min(LEVELS.index(cfg.need_level), n - 1)
    best = None
    i = 0
    while i < n:
        qi = qs[i]
        if qi < 0:
            i += 1
            continue
        j = i
        while j + 1 < n and qs[j + 1] == qi:
            j += 1
        d1 = j - i + 1
        inc1 = need_idx < 0 or (i <= need_idx <= j)
        if cfg.chain_kind != "Puente" and d1 >= cfg.min_depth and inc1 and (best is None or d1 > best["depth"]):
            best = {"depth": d1, "start": i, "end": j, "q": qi, "bridge": False}
        if qi == 3 and j + 1 < n and qs[j + 1] == 0 and cfg.chain_kind != "Normal":
            d2 = d1 + 1
            inc2 = need_idx < 0 or (i <= need_idx <= j + 1)
            if d2 >= cfg.min_depth and inc2 and (best is None or d2 > best["depth"]):
                best = {"depth": d2, "start": i, "end": j + 1, "q": 3, "bridge": True}
        i = j + 1
    return best


def chain_text(ch):
    if not ch:
        return "sin cadena"
    q = "Q4→Q1" if ch["bridge"] else "Q%d" % (ch["q"] + 1)
    return "Prof %d | %s a %s | %s" % (ch["depth"], LEVELS[ch["start"]], LEVELS[ch["end"]], q)


# ============================================================================
#  INDICADORES Y TRUE OPENS
# ============================================================================
def atr_series(bars, n=14):
    out = [None] * len(bars)
    trs = []
    atr = None
    prev_c = None
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if prev_c is None else max(b["h"] - b["l"], abs(b["h"] - prev_c), abs(b["l"] - prev_c))
        trs.append(tr)
        if i == n - 1:
            atr = sum(trs[:n]) / n
        elif i >= n:
            atr = (atr * (n - 1) + tr) / n
        out[i] = atr
        prev_c = b["c"]
    return out


def cost_r(cfg, entry, risk):
    """Costes ida y vuelta (comision taker + deslizamiento estimado, ambos por lado) expresados en R."""
    return 2.0 * (cfg.fee_pct + cfg.slip_pct) / (risk / entry * 100.0)


def ema_series(values, n):
    """EMA con semilla SMA; None hasta tener n valores."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def find_true_open(bars, mins_arr, idx, kind):
    """Daily: vela de las 00:00 NY (mins=360). Session: inicio de sesion + 90 min (mins % 360 == 90)."""
    for j in range(idx, -1, -1):
        m = mins_arr[j]
        if (kind == "daily" and m == 360) or (kind == "session" and m % 360 == 90):
            return bars[j]["o"]
    return None


def bias_for(bars, mins_arr, idx, qi, cfg):
    """Devuelve (bull_ok, bear_ok, niveles_usados[(nombre, precio)])."""
    if cfg.bias_mode == "none":
        return True, True, []
    levels = []
    if cfg.use_to_daily and qi["qD"] >= 1:
        levels.append(("TO Daily", find_true_open(bars, mins_arr, idx, "daily")))
    if cfg.use_to_session and qi["q90"] >= 1:
        levels.append(("TO Sesión", find_true_open(bars, mins_arr, idx, "session")))
    used = [(nm, lv) for nm, lv in levels if lv is not None]
    if not used:
        return False, False, []
    c = bars[idx]["c"]
    all_above = all(c > lv for _, lv in used)
    all_below = all(c < lv for _, lv in used)
    if cfg.to_logic == "manipulacion":
        return all_below, all_above, used      # LONG bajo TO, SHORT sobre TO
    return all_above, all_below, used          # tendencia


# ============================================================================
#  MOTOR DE ENTRADA (barrido del Q1 del ciclo de 90 min)
# ============================================================================
GATES = ("sesion", "cadena", "q1", "barrido", "vela", "sesgo", "tendencia", "btc", "volumen", "una_por_ciclo")
GATE_TEXT = {
    "sesion": "fuera de las sesiones elegidas",
    "cadena": "sin cadena QT abierta",
    "q1": "rango del Q1 fuera de limites",
    "barrido": "sin barrido valido del Q1",
    "vela": "sin vela de confirmacion fuerte",
    "sesgo": "sesgo True Open en contra",
    "tendencia": "tendencia 1H en contra",
    "btc": "BTC en contra",
    "volumen": "volumen relativo bajo",
    "liquidez": "liquidez insuficiente (spread/impacto en el libro)",
    "concentracion": "demasiadas señales abiertas en el mismo lado",
    "una_por_ciclo": "ya hubo senal en este ciclo",
    "riesgo": "SL demasiado grande/pequeno o comisiones altas",
}


def _bump(d, k, n=1):
    d[k] = d.get(k, 0) + n


def _eval_gates(gates, funnel):
    """True si pasan todas. Con funnel: cuenta el embudo secuencial y 'solo falla este filtro'."""
    fails = [n for n, ok in gates if not ok]
    if funnel is not None:
        _bump(funnel, "oportunidades")
        for n, ok in gates:
            if not ok:
                break
            _bump(funnel, "seq:" + n)
        if len(fails) == 1:
            _bump(funnel, "only:" + fails[0])
    return not fails


def prepare_series(bars, cfg):
    """Calculos que no dependen de los filtros (se reutilizan entre variantes del backtest)."""
    qinfos = [quarters(b["t"]) for b in bars]
    return {
        "atr": atr_series(bars),
        "qinfos": qinfos,
        "mins": [q["mins"] for q in qinfos],
        "closes": [b["c"] for b in bars],
        "ema": ema_series([b["c"] for b in bars], cfg.trend_ema_bars),
        "vols": [b.get("v", 0.0) for b in bars],
    }


def trend_of(bars_c, ema, i):
    """+1 alcista, -1 bajista, 0 neutra/sin datos (precio vs EMA y pendiente de la EMA)."""
    e = ema[i]
    if e is not None and i >= 12 and ema[i - 12] is not None:
        if bars_c[i] > e and e > ema[i - 12]:
            return 1
        if bars_c[i] < e and e < ema[i - 12]:
            return -1
    return 0


def btc_trend_map(bars, n):
    """{t_vela: tendencia} de BTC para el filtro de contexto."""
    closes = [b["c"] for b in bars]
    ema = ema_series(closes, n)
    return {b["t"]: trend_of(closes, ema, i) for i, b in enumerate(bars)}


def scan_series(symbol, bars, tick, cfg, only_last=True, funnel=None, prep=None, btc=None):
    """Recorre las velas CERRADAS [{t,o,h,l,c,v}].
    only_last=True  -> solo senales de la ultima vela (modo en vivo).
    only_last=False -> senales de todas las velas (backtest); cada senal lleva 'bar_i'."""
    if len(bars) < 40:
        return []
    p = prep or prepare_series(bars, cfg)
    atr, qinfos, mins_arr, ema, vols = p["atr"], p["qinfos"], p["mins"], p["ema"], p["vols"]
    closes = p["closes"]
    last = len(bars) - 1
    i0 = 0
    if only_last:
        cs = bars[last]["t"] - int(qinfos[last]["m90"]) * 60_000
        i0 = last
        while i0 - 1 >= 0 and bars[i0 - 1]["t"] >= cs:
            i0 -= 1

    signals = []
    chain_cache = {}
    cur_cycle, valid = None, False
    q1_hi = q1_lo = c_hi = c_lo = None
    sweep_bar_hi = sweep_bar_lo = None
    done_s = done_l = False

    for i in range(i0, last + 1):
        b = bars[i]
        qi = qinfos[i]
        cyc = b["t"] - int(qi["m90"]) * 60_000
        if cyc != cur_cycle:                                   # empieza un ciclo de 90 min
            cur_cycle = cyc
            valid = not (i == 0 and b["t"] != cyc)             # ciclo incompleto al inicio del historico
            q1_hi, q1_lo, c_hi, c_lo = b["h"], b["l"], b["h"], b["l"]
            sweep_bar_hi = sweep_bar_lo = None
            done_s = done_l = False
        else:
            c_hi = max(c_hi, b["h"])
            c_lo = min(c_lo, b["l"])
            if qi["qMi"] == 0:
                q1_hi = max(q1_hi, b["h"])
                q1_lo = min(q1_lo, b["l"])
        if not valid:
            continue
        in_window = qi["qMi"] in (1, 2)
        if in_window:
            if b["h"] > q1_hi and sweep_bar_hi is None:
                sweep_bar_hi = i
            if b["l"] < q1_lo and sweep_bar_lo is None:
                sweep_bar_lo = i
        a = atr[i]
        if a is None or not in_window:
            continue

        key = tuple(qi["qs"])
        if key not in chain_cache:
            chain_cache[key] = detect_chain(qi["qs"], cfg)
        chain = chain_cache[key]
        seq_ok = (not cfg.require_chain) or chain is not None
        session_ok = qi["qD"] in cfg.sessions
        q1_range = q1_hi - q1_lo
        q1_ok = cfg.min_q1_atr * a <= q1_range <= cfg.max_q1_atr * a
        depth_hi = c_hi - q1_hi
        depth_lo = q1_lo - c_lo
        fresh_hi = sweep_bar_hi is not None and i - sweep_bar_hi <= cfg.max_bars_sweep
        fresh_lo = sweep_bar_lo is not None and i - sweep_bar_lo <= cfg.max_bars_sweep
        sweep_ok_hi = fresh_hi and cfg.min_sweep_atr * a <= depth_hi <= cfg.max_sweep_atr * a
        sweep_ok_lo = fresh_lo and cfg.min_sweep_atr * a <= depth_lo <= cfg.max_sweep_atr * a
        rng = b["h"] - b["l"]
        body = abs(b["c"] - b["o"])
        strong_bear = b["c"] < b["o"] and rng > 0 and body >= cfg.body_pct / 100.0 * rng
        strong_bull = b["c"] > b["o"] and rng > 0 and body >= cfg.body_pct / 100.0 * rng
        bull_bias, bear_bias, bias_levels = bias_for(bars, mins_arr, i, qi, cfg)

        trend = trend_of(closes, ema, i)                       # +1 alcista, -1 bajista, 0 neutra/sin datos
        rv = 0.0
        if i >= 21:
            base = sum(vols[i - 20:i]) / 20.0
            if base > 0:
                rv = vols[i] / base
        vol_ok = cfg.min_rel_vol <= 0 or rv >= cfg.min_rel_vol

        count = funnel is not None and (not only_last or i == last)
        emit_here = (not only_last) or i == last
        extra = {
            "chain_depth": chain["depth"] if chain else 0,
            "bias_levels": bias_levels,
            "body_ratio": (body / rng) if rng > 0 else 0.0,
            "rel_vol": rv,
        }
        for side in ("SHORT", "LONG"):
            if side == "SHORT":
                if not cfg.allow_short:
                    continue
                gates = [("sesion", session_ok), ("cadena", seq_ok), ("q1", q1_ok), ("barrido", sweep_ok_hi),
                         ("vela", strong_bear and b["c"] < q1_hi), ("sesgo", bear_bias),
                         ("tendencia", (not cfg.trend_filter) or trend == -1),
                         ("btc", (not cfg.btc_filter) or not btc or btc.get(b["t"], 0) != 1), ("volumen", vol_ok),
                         ("una_por_ciclo", not done_s)]
            else:
                if not cfg.allow_long:
                    continue
                gates = [("sesion", session_ok), ("cadena", seq_ok), ("q1", q1_ok), ("barrido", sweep_ok_lo),
                         ("vela", strong_bull and b["c"] > q1_lo), ("sesgo", bull_bias),
                         ("tendencia", (not cfg.trend_filter) or trend == 1),
                         ("btc", (not cfg.btc_filter) or not btc or btc.get(b["t"], 0) != -1), ("volumen", vol_ok),
                         ("una_por_ciclo", not done_l)]
            if not _eval_gates(gates, funnel if count else None):
                continue
            if side == "SHORT":
                done_s = True
                ex = dict(extra, sweep_atr=depth_hi / a, q1_level=q1_hi, trend_ok=(trend == -1))
                sl = c_hi + cfg.sl_buf_atr * a
            else:
                done_l = True
                ex = dict(extra, sweep_atr=depth_lo / a, q1_level=q1_lo, trend_ok=(trend == 1))
                sl = c_lo - cfg.sl_buf_atr * a
            if not emit_here:
                continue
            sig = _build_signal(symbol, side, b, sl, tick, cfg, chain, qi, cyc, ex)
            if count:
                _bump(funnel, "seq:senal" if sig else "seq:riesgo")
                if not sig:
                    _bump(funnel, "only:riesgo")
            if sig:
                sig["bar_i"] = i
                signals.append(sig)
    return signals


def analyze(symbol, bars, tick, cfg, funnel=None, btc=None):
    """Modo en vivo: senales de la ultima vela cerrada."""
    return scan_series(symbol, bars, tick, cfg, only_last=True, funnel=funnel, btc=btc)


def score_signal(ex, cfg):
    """Puntuacion 0-7: cadena, cadena profunda, sesgo True Open, barrido limpio, vela fuerte,
    tendencia 1H a favor y volumen relativo alto."""
    s = 0
    if ex["chain_depth"] >= 1:
        s += 1
    if ex["chain_depth"] >= 4:
        s += 1
    if cfg.bias_mode != "none":
        s += 1
    if 0.3 <= ex["sweep_atr"] <= 1.5:
        s += 1
    if ex["body_ratio"] >= 0.6:
        s += 1
    if ex.get("trend_ok"):
        s += 1
    if ex.get("rel_vol", 0.0) >= 1.5:
        s += 1
    return s


def _build_signal(symbol, side, bar, sl, tick, cfg, chain, qi, cycle_start_ms, ex):
    entry = bar["c"]
    risk = (sl - entry) if side == "SHORT" else (entry - sl)
    if risk <= 0 or risk < cfg.min_risk_ticks * tick:
        return None
    risk_pct = risk / entry * 100.0
    if cfg.max_risk_pct > 0 and risk_pct > cfg.max_risk_pct:
        return None
    fee_r = cost_r(cfg, entry, risk)                          # comisiones + deslizamiento, ida y vuelta, en R
    if cfg.max_fee_r > 0 and fee_r > cfg.max_fee_r:
        return None
    sgn = -1 if side == "SHORT" else 1
    above = (side == "SHORT") == (cfg.to_logic == "manipulacion")
    return {
        "symbol": symbol, "side": side, "entry": entry, "sl": sl,
        "tp1": entry + sgn * risk * cfg.partial_r,
        "tp2": entry + sgn * risk * cfg.rr,
        "max_entry": entry + sgn * risk * cfg.entry_chase_r,   # LONG: tope superior / SHORT: tope inferior
        "risk_pct": risk_pct,
        "fee_r": fee_r,
        "chain": chain_text(chain),
        "session": SESSION_NAMES[qi["qD"]],
        "time_ny": qi["local"].strftime("%Y-%m-%d %H:%M"),
        "cycle_start_ms": cycle_start_ms,
        "entry_t": bar["t"],
        "tick": tick,
        "step": 0.0,
        "score": score_signal(ex, cfg),
        "q1_level": ex["q1_level"],
        "bias_levels": ex["bias_levels"],
        "bias_rel": "sobre" if above else "bajo",
        "rel_vol": ex.get("rel_vol", 0.0),
        "trend_ok": bool(ex.get("trend_ok")),
    }


# ============================================================================
#  FORMATO DE MENSAJES (Telegram, HTML)
# ============================================================================
def fmt_price(x, tick):
    dec = 0 if tick >= 1 else max(0, math.ceil(-math.log10(tick) - 1e-9))
    return ("%." + str(dec) + "f") % x


def position_size(s, cfg):
    """Cantidad segun riesgo fijo por trade. None si no hay ACCOUNT_SIZE."""
    if cfg.account_size <= 0:
        return None
    dist = abs(s["entry"] - s["sl"])
    risk_usdt = cfg.account_size * cfg.risk_per_trade_pct / 100.0
    qty = risk_usdt / dist
    cap = cfg.account_size * cfg.max_leverage
    capped = qty * s["entry"] > cap
    if capped:
        qty = cap / s["entry"]
    step = s.get("step") or 0.0
    if step > 0:
        qty = math.floor(qty / step + 1e-9) * step
    if qty <= 0:
        return {"qty": 0.0}
    notional = qty * s["entry"]
    return {
        "qty": qty, "notional": notional, "risk_usdt": qty * dist, "capped": capped,
        "lev": max(1, math.ceil(notional / cfg.account_size - 1e-9)),
        "target_risk": risk_usdt,
    }


def format_message(s, cfg):
    t = s["tick"]

    def fp(x):
        return fmt_price(x, t)

    def pct(x):
        return abs(x - s["entry"]) / s["entry"] * 100.0

    is_long = s["side"] == "LONG"
    icon = "🟢" if is_long else "🔴"
    stars = "⭐" * s["score"] + "☆" * (SCORE_MAX - s["score"])
    pf = cfg.partial_pct
    lines = [
        "%s <b>%s · %s</b>   %s %d/%d" % (icon, s["side"], s["symbol"], stars, s["score"], SCORE_MAX),
    ]
    if RUNTIME.get("edge") is False:
        lines.append("⚠️ <i>Experimental: el ultimo autoajuste no encontro ventaja validada.</i>")
    lines += [
        "",
        "📍 Entrada: <code>%s</code>  (mercado o limit)" % fp(s["entry"]),
        "     No entres %s de <code>%s</code>" % ("por encima" if is_long else "por debajo", fp(s["max_entry"])),
        "🛑 SL: <code>%s</code>  (-%.2f%%)" % (fp(s["sl"]), pct(s["sl"])),
        "🎯 TP1: <code>%s</code>  (+%.2f%% · %.1fR)  → cierra %.0f%% y SL a entrada" % (
            fp(s["tp1"]), pct(s["tp1"]), cfg.partial_r, pf),
        "🏁 TP2: <code>%s</code>  (+%.2f%% · %.1fR)  → cierra el resto" % (fp(s["tp2"]), pct(s["tp2"]), cfg.rr),
    ]
    ps = position_size(s, cfg)
    if ps is not None:
        lines.append("")
        if ps["qty"] <= 0:
            lines.append("💼 Con tu riesgo no alcanza el tamaño minimo del contrato.")
        else:
            lines.append("💼 Cuenta %.0f USDT · riesgo %.1f%% (%.2f USDT)" % (
                cfg.account_size, cfg.risk_per_trade_pct, ps["target_risk"]))
            lines.append("     Cantidad: <code>%s</code> | nocional ~%.0f USDT | apalancamiento ≥ %dx" % (
                fmt_price(ps["qty"], s["step"] or 1e-4), ps["notional"], ps["lev"]))
            if ps["capped"]:
                lines.append("     ⚠️ Limitado por MAX_LEVERAGE: riesgo real %.2f USDT" % ps["risk_usdt"])
    lines.append("")
    lines.append("🧩 Cadena: %s" % s["chain"])
    if s["bias_levels"]:
        lv = " y ".join("%s %s" % (nm, fp(p)) for nm, p in s["bias_levels"])
        lines.append("🧭 Sesgo: precio %s %s" % (s["bias_rel"], lv))
    lines.append("🧹 Barrido del %s del Q1 (%s) y vela de confirmacion" % (
        "minimo" if is_long else "maximo", fp(s["q1_level"])))
    lines.append("📊 Volumen vela x%.1f · Tendencia 1H %s" % (
        s.get("rel_vol", 0.0), "a favor" if s.get("trend_ok") else "neutra o en contra"))
    if s.get("fee_r") is not None:
        lines.append("💸 Costes ida y vuelta ≈ %.2fR (comision %.2f%% + deslizamiento %.2f%% por lado)" % (
            s["fee_r"], cfg.fee_pct, cfg.slip_pct))
    if "liq" in s:
        lq = s["liq"]
        if lq is None:
            lines.append("💧 Liquidez no verificada (libro de ordenes no disponible)")
        else:
            lines.append("💧 Liquidez: spread %.3f%% · impacto de %.0f USDT %.3f%% ≈ %.2fR ida y vuelta" % (
                lq["spread"], lq["notional"], lq["impact"], lq["cost_r"]))
    lines.append("🕐 %s · vela 5m cerrada %s NY" % (s["session"], s["time_ny"]))
    lines.append("<i>Solo aviso, no es consejo financiero.</i>")
    return "\n".join(lines)


def format_event(sig, kind, r, cfg):
    fp = lambda x: fmt_price(x, sig["tick"])  # noqa: E731
    head = "%s %s" % (sig["symbol"], sig["side"])
    if kind == "TP1":
        return ("🎯 <b>TP1 alcanzado</b> · %s\nCierra %.0f%% y mueve el SL a la entrada (<code>%s</code>). "
                "Deja correr el resto hasta TP2 <code>%s</code>." % (head, cfg.partial_pct, fp(sig["entry"]), fp(sig["tp2"])))
    if kind == "TP2":
        return "🏆 <b>TP2 alcanzado</b> · %s\nCierra el resto. Resultado simulado: %+.2fR (neto de comisiones)." % (head, r)
    if kind == "SL":
        return "🛑 <b>SL tocado</b> · %s\nResultado simulado: %+.2fR (neto de comisiones)." % (head, r)
    if kind == "BE":
        return "⚪ <b>Cerrada en BE tras TP1</b> · %s\nResultado simulado: %+.2fR (neto de comisiones)." % (head, r)
    return ("⌛ <b>Senal caducada</b> · %s\nSin resolver tras %.0f h; deja de seguirse. Valorada a precio de "
            "cierre: %+.2fR (neto). Si sigues dentro, gestiona el trade tu." % (head, cfg.track_hours, r))


def buttons_for(symbol, exchange="bingx"):
    if exchange == "bingx":
        return [
            {"text": "📈 BingX Futures", "url": "https://bingx.com/en/perpetual/%s" % symbol},
            {"text": "📊 TradingView", "url": "https://www.tradingview.com/chart/?symbol=BINGX%%3A%s.P" % symbol.replace("-", "")},
        ]
    return [
        {"text": "📈 Binance Futures", "url": "https://www.binance.com/en/futures/%s" % symbol},
        {"text": "📊 TradingView", "url": "https://www.tradingview.com/chart/?symbol=BINANCE%%3A%s.P" % symbol},
    ]


# ============================================================================
#  EXCHANGE (BingX / Binance) + TELEGRAM
# ============================================================================
def http_get(url, params=None, tries=3, headers=None):
    for k in range(tries):
        try:
            r = requests.get(url, params=params or {}, headers=headers, timeout=10)
            if r.status_code in (418, 429):
                time.sleep(min(int(r.headers.get("Retry-After", "5")), 60))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if k == tries - 1:
                raise
            time.sleep(1 + k)
    raise RuntimeError("limite de peticiones: reintentos agotados")


# ---- BingX (perpetuos USDT, simbolos tipo BTC-USDT) ----
_thr_lock = threading.Lock()
_thr_next = [0.0]


def _throttle(rate=30.0):
    """Limita las peticiones a BingX a ~30/s (el limite de datos de mercado es mayor, asi hay margen)."""
    with _thr_lock:
        now = time.time()
        slot = max(now, _thr_next[0])
        _thr_next[0] = slot + 1.0 / rate
    if slot > now:
        time.sleep(slot - now)


def bx_get(cfg, path, params=None):
    """GET a BingX. Si defines BINGX_API_KEY y BINGX_API_SECRET firma la peticion (solo lectura basta);
    los datos de mercado normalmente funcionan sin clave."""
    for k in range(4):
        _throttle()
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)               # BingX lo pide en todas las peticiones
        headers = None
        key = os.environ.get("BINGX_API_KEY", "").strip()
        sec = os.environ.get("BINGX_API_SECRET", "").strip()
        if key and sec:
            qs = "&".join("%s=%s" % (n, p[n]) for n in p)
            p["signature"] = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
            headers = {"X-BX-APIKEY": key}
        j = http_get(cfg.base_url + path, p, headers=headers)
        code = j.get("code") if isinstance(j, dict) else None
        if code in (None, 0, "0"):
            return j.get("data") if isinstance(j, dict) and "data" in j else j
        if str(code) == "100410":                              # limite de peticiones
            time.sleep(5 + 5 * k)
            continue
        raise RuntimeError("BingX codigo %s: %s" % (code, str(j.get("msg", ""))[:160]))
    raise RuntimeError("BingX: limite de peticiones, reintentos agotados")


def norm_symbol(sym, exchange):
    """BTCUSDT -> BTC-USDT en BingX; BTC-USDT -> BTCUSDT en Binance."""
    sym = sym.strip().upper()
    if exchange == "bingx" and "-" not in sym and sym.endswith("USDT") and len(sym) > 4:
        return sym[:-4] + "-USDT"
    if exchange == "binance":
        return sym.replace("-", "")
    return sym


TRADFI_RE = re.compile(r"^(NCSK|NCCO|NCFX|NCSI|NCIN)|2USD$")


def is_tradfi(symbol):
    """Perpetuos de acciones / materias primas / indices / forex de BingX (no son cripto 24/7)."""
    return bool(TRADFI_RE.search(symbol.split("-")[0].upper()))


def _bx_bar(x):
    """Acepta velas como lista [t,o,h,l,c,v,...] o como dict {time,open,high,low,close,volume}."""
    if isinstance(x, dict):
        t, o, h, l, c, v = x["time"], x["open"], x["high"], x["low"], x["close"], x.get("volume", 0)
    else:
        t, o, h, l, c = x[0], x[1], x[2], x[3], x[4]
        v = x[5] if len(x) > 5 else 0
    t = int(float(t))
    if t < 100_000_000_000:                                    # segundos -> ms
        t *= 1000
    c = float(c)
    return {"t": t, "o": float(o), "h": float(h), "l": float(l), "c": c, "v": float(v) * c}   # v ~ USDT


def _bx_bars(rows):
    """Ordena ascendente y elimina duplicados (BingX no garantiza el orden)."""
    out = {}
    for x in rows or []:
        b = _bx_bar(x)
        out[b["t"]] = b
    return [out[t] for t in sorted(out)]


def load_symbols_bingx(cfg, only=None):
    meta, syms = {}, []
    for c in bx_get(cfg, "/openApi/swap/v2/quote/contracts") or []:
        sym = str(c.get("symbol", "")).upper()
        if not sym.endswith("-USDT"):
            continue
        if str(c.get("status", 1)) in ("0", "5", "25"):          # offline / pre-lanzamiento / suspendido
            continue
        if str(c.get("apiStateOpen", "true")).lower() == "false":
            continue
        if not cfg.include_tradfi and is_tradfi(sym):
            continue
        pp = int(float(c.get("pricePrecision", 4) or 4))
        qp = int(float(c.get("quantityPrecision", 0) or 0))
        meta[sym] = {"tick": 10.0 ** -pp, "step": 10.0 ** -qp}
        syms.append(sym)
    vols = {}
    try:
        for x in bx_get(cfg, "/openApi/swap/v2/quote/ticker") or []:
            q = x.get("quoteVolume")
            if q in (None, ""):
                q = float(x.get("volume", 0) or 0) * float(x.get("lastPrice", 0) or 0)
            vols[str(x["symbol"]).upper()] = float(q)
    except Exception as e:
        log.warning("sin volumen 24h (%s): no se filtra por volumen", e)
    if vols and max(vols.values()) <= 0:
        log.warning("el ticker no trae volumen: no se filtra por volumen")
        vols = {}
    excl = {norm_symbol(x, "bingx") for x in cfg.exclude_symbols}
    if only:
        only = {norm_symbol(x, "bingx") for x in only}
        syms = [x for x in syms if x in only]
    elif vols:
        syms = [x for x in syms if vols.get(x, 0.0) >= cfg.min_quote_volume and x not in excl]
    else:
        syms = [x for x in syms if x not in excl]
    syms.sort(key=lambda x: -vols.get(x, 0.0))
    if cfg.max_symbols:
        syms = syms[:cfg.max_symbols]
    return syms, meta


def fetch_bars_bingx(cfg, symbol):
    rows = bx_get(cfg, "/openApi/swap/v3/quote/klines",
                  {"symbol": symbol, "interval": cfg.interval, "limit": cfg.klines_limit})
    now_ms = int(time.time() * 1000)
    return [b for b in _bx_bars(rows) if b["t"] + BAR_MS <= now_ms]      # solo velas cerradas


def fetch_history_bingx(cfg, symbol, days):
    """Pagina hacia atras con endTime (1440 velas por peticion)."""
    end = int(time.time() * 1000)
    start = end - int((days + 1) * 86_400_000)
    out, cur_end, prev_oldest = {}, end, None
    for _ in range(60):
        bars = _bx_bars(bx_get(cfg, "/openApi/swap/v3/quote/klines",
                               {"symbol": symbol, "interval": cfg.interval, "endTime": cur_end, "limit": 1440}))
        if not bars:
            break
        for b in bars:
            out[b["t"]] = b
        oldest = bars[0]["t"]
        if oldest <= start or (prev_oldest is not None and oldest >= prev_oldest):
            break
        prev_oldest, cur_end = oldest, oldest - 1
    return [out[t] for t in sorted(out) if t >= start and t + BAR_MS <= end]


# ---- Binance ----
def load_symbols_binance(cfg, only=None):
    """Devuelve (symbols, meta) con meta[symbol] = {"tick": .., "step": ..}."""
    info = http_get(cfg.base_url + "/fapi/v1/exchangeInfo")
    meta, syms = {}, []
    for s in info["symbols"]:
        if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            m = {"tick": 0.0001, "step": 0.0}
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    m["tick"] = float(f["tickSize"])
                elif f.get("filterType") == "LOT_SIZE":
                    m["step"] = float(f["stepSize"])
            meta[s["symbol"]] = m
            syms.append(s["symbol"])
    vols = {x["symbol"]: float(x["quoteVolume"]) for x in http_get(cfg.base_url + "/fapi/v1/ticker/24hr")}
    if only:
        syms = [s for s in syms if s in only]
    else:
        syms = [s for s in syms if vols.get(s, 0.0) >= cfg.min_quote_volume and s not in cfg.exclude_symbols]
    syms.sort(key=lambda s: -vols.get(s, 0.0))
    if cfg.max_symbols:
        syms = syms[:cfg.max_symbols]
    return syms, meta


def _row_to_bar(r):
    return {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
            "v": float(r[7])}                              # v = volumen en USDT


def fetch_bars_binance(cfg, symbol):
    rows = http_get(cfg.base_url + "/fapi/v1/klines",
                    {"symbol": symbol, "interval": cfg.interval, "limit": cfg.klines_limit})
    now_ms = int(time.time() * 1000)
    return [_row_to_bar(r) for r in rows if int(r[6]) < now_ms]   # solo velas cerradas


def fetch_history_binance(cfg, symbol, days):
    end = int(time.time() * 1000)
    cur = end - int((days + 1) * 86_400_000)
    out = []
    while cur < end:
        rows = http_get(cfg.base_url + "/fapi/v1/klines",
                        {"symbol": symbol, "interval": cfg.interval, "startTime": cur, "limit": 1500})
        if not rows:
            break
        out.extend(_row_to_bar(r) for r in rows)
        nxt = int(rows[-1][0]) + BAR_MS
        if nxt <= cur or len(rows) < 1500:
            break
        cur = nxt
        time.sleep(0.2)
    return [b for b in out if b["t"] + BAR_MS <= end]


# ---- seleccion por exchange ----
def load_symbols(cfg, only=None):
    return (load_symbols_bingx if cfg.exchange == "bingx" else load_symbols_binance)(cfg, only)


def fetch_bars(cfg, symbol):
    return (fetch_bars_bingx if cfg.exchange == "bingx" else fetch_bars_binance)(cfg, symbol)


def fetch_history(cfg, symbol, days):
    """Velas de 5m cerradas de los ultimos 'days' dias (+1 dia de calentamiento para la EMA)."""
    return (fetch_history_bingx if cfg.exchange == "bingx" else fetch_history_binance)(cfg, symbol, days)


def scan_all(cfg, symbols, meta, funnel=None, btc=None):
    found = []
    stale = 0
    exp_t = int(time.time() * 1000) // BAR_MS * BAR_MS - BAR_MS      # ultima vela de 5m que ya deberia estar cerrada
    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        futs = {ex.submit(fetch_bars, cfg, s): s for s in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                m = meta.get(sym, {})
                bars = f.result()
                if not bars or bars[-1]["t"] < exp_t:                # el exchange aun no publica la vela: no operar con datos viejos
                    stale += 1
                    continue
                for s in analyze(sym, bars, m.get("tick", 0.0001), cfg, funnel, btc):
                    s["step"] = m.get("step", 0.0)
                    found.append(s)
            except Exception as e:  # una moneda con error no debe parar el escaneo
                log.warning("%s: %s", sym, e)
    if stale:
        log.info("%d de %d monedas sin la ultima vela cerrada todavia (se omiten en este escaneo)", stale, len(symbols))
    return found


def send_telegram(text, reply_to=None, buttons=None):
    """Envia a todos los chats de TELEGRAM_CHAT_ID (separados por coma).
    Devuelve {chat_id: message_id}."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        raise RuntimeError("Faltan TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID")
    ids, last_err = {}, None
    for chat in chats:
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to and str(chat) in reply_to:
            payload["reply_to_message_id"] = reply_to[str(chat)]
            payload["allow_sending_without_reply"] = True
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        r = None
        for _ in range(3):
            r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token, json=payload, timeout=10)
            if r.status_code == 429:
                try:
                    wait = r.json().get("parameters", {}).get("retry_after", 3)
                except ValueError:
                    wait = 3
                time.sleep(min(wait, 30))
                continue
            break
        if r is not None and r.ok:
            try:
                ids[str(chat)] = r.json()["result"]["message_id"]
            except (ValueError, KeyError):
                ids[str(chat)] = 0
        else:
            last_err = "Telegram %s: %s" % (r.status_code if r is not None else "?", r.text[:200] if r is not None else "")
    if not ids:
        raise RuntimeError(last_err or "no se pudo enviar")
    return ids


def plain(msg):
    for tag in ("<b>", "</b>", "<code>", "</code>", "<i>", "</i>"):
        msg = msg.replace(tag, "")
    return msg


def emit(text, dry_run, reply_to=None, buttons=None):
    """Envia (o imprime en dry-run). Devuelve dict de ids, o None si fallo el envio."""
    if dry_run:
        print(plain(text), "\n")
        return {}
    try:
        return send_telegram(text, reply_to=reply_to, buttons=buttons)
    except Exception as e:
        log.error("no se pudo enviar a Telegram: %s", e)
        return None


# ============================================================================
#  ESTADO PERSISTENTE (duplicados, senales activas, estadisticas)
# ============================================================================
STATE_FILE = os.environ.get("STATE_FILE") or os.environ.get("SENT_FILE") or "state.json"


def new_stats():
    return {"n": 0, "tp2": 0, "be": 0, "sl": 0, "exp": 0, "r": 0.0}


def load_state():
    base = {"sent": {}, "active": {}, "last_symbol": {}, "daily": {"date": "", "n": 0},
            "stats": {}, "totals": new_stats(), "last_summary": "",
            "funnel": {}, "last_hb": 0.0, "hb_n": 0, "tuned": {}, "edge": None, "last_tune": 0.0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in base:
                if k in d:
                    base[k] = d[k]
    except (OSError, ValueError):
        pass
    cutoff = time.time() - 2 * 86400
    base["sent"] = {k: v for k, v in base["sent"].items() if v > cutoff}
    base["stats"] = dict(sorted(base["stats"].items())[-14:])
    return base


def save_state(st):
    folder = os.path.dirname(STATE_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def today_ny():
    return datetime.now(NY).strftime("%Y-%m-%d")


def bump_stats(st, field, r=0.0):
    day = st["stats"].setdefault(today_ny(), new_stats())
    for box in (day, st["totals"]):
        box[field] += 1
        box["r"] += r


def make_active(s, ids, now):
    keys = ("symbol", "side", "entry", "sl", "tp1", "tp2", "tick", "entry_t", "score")
    a = {k: s[k] for k in keys}
    a.update({"sl_now": s["sl"], "last_t": s["entry_t"], "created": now, "status": "open", "msg": ids,
              "fee_r": s.get("fee_r", 0.0), "last_c": s["entry"]})
    return a


# ============================================================================
#  SEGUIMIENTO DE SENALES (TP1 / TP2 / SL / caducidad), simulado con velas de 5m
# ============================================================================
def evaluate_signal(sig, bars, cfg, now):
    """Actualiza 'sig' y devuelve eventos [(tipo, R)]. Si SL y TP caen en la misma vela, cuenta el SL."""
    events = []
    is_long = sig["side"] == "LONG"
    pf = cfg.partial_pct / 100.0
    for b in bars:
        if b["t"] <= sig["last_t"]:
            continue
        sig["last_t"] = b["t"]
        sig["last_c"] = b["c"]
        sl = sig["sl_now"]
        sl_hit = b["l"] <= sl if is_long else b["h"] >= sl
        tp1_hit = b["h"] >= sig["tp1"] if is_long else b["l"] <= sig["tp1"]
        tp2_hit = b["h"] >= sig["tp2"] if is_long else b["l"] <= sig["tp2"]
        if sl_hit:
            if sig["status"] == "tp1":
                events.append(("BE", pf * cfg.partial_r))
            else:
                events.append(("SL", -1.0))
            sig["status"] = "closed"
            return events
        if tp2_hit:
            if sig["status"] == "open":
                events.append(("TP1", 0.0))
            events.append(("TP2", pf * cfg.partial_r + (1.0 - pf) * cfg.rr))
            sig["status"] = "closed"
            return events
        if tp1_hit and sig["status"] == "open":
            sig["status"] = "tp1"
            sig["sl_now"] = sig["entry"]
            events.append(("TP1", 0.0))
    if sig["status"] != "closed" and now - sig["created"] > cfg.track_hours * 3600:
        risk = abs(sig["entry"] - sig["sl"])
        sgn = 1.0 if is_long else -1.0
        mtm = (sig.get("last_c", sig["entry"]) - sig["entry"]) * sgn / risk if risk > 0 else 0.0
        if sig["status"] == "tp1":
            events.append(("EXP", pf * cfg.partial_r + (1.0 - pf) * max(mtm, 0.0)))
        else:
            events.append(("EXP", mtm))                       # valorada a precio de cierre
        sig["status"] = "closed"
    return events


STAT_FIELD = {"TP2": "tp2", "BE": "be", "SL": "sl", "EXP": "exp"}


def track_active(cfg, st):
    active = st["active"]
    for aid in list(active):
        sig = active[aid]
        try:
            bars = fetch_bars(cfg, sig["symbol"])
        except Exception as e:
            log.warning("seguimiento %s: %s", sig["symbol"], e)
            continue
        for kind, r in evaluate_signal(sig, bars, cfg, time.time()):
            if kind in STAT_FIELD:
                r -= sig.get("fee_r", 0.0)                    # resultado final NETO de comisiones
            log.info("%s %s -> %s", sig["symbol"], sig["side"], kind)
            emit(format_event(sig, kind, r, cfg), False, reply_to=sig.get("msg"))
            if kind in STAT_FIELD:
                bump_stats(st, STAT_FIELD[kind], r)
        if sig["status"] == "closed":
            del active[aid]


def maybe_summary(cfg, st):
    if cfg.daily_summary_hour < 0:
        return
    now = datetime.now(NY)
    today = now.strftime("%Y-%m-%d")
    if now.hour < cfg.daily_summary_hour or st["last_summary"] == today:
        return
    d = st["stats"].get(today, new_stats())
    t = st["totals"]
    msg = ("📊 <b>Resumen del dia (NY)</b>\n"
           "Senales: %d | TP2: %d | BE/parcial: %d | SL: %d | Caducadas: %d\n"
           "Resultado simulado: <b>%+.2fR</b>\n"
           "Acumulado desde el inicio: %d senales, %+.2fR\n"
           "<i>Simulado con velas de 5m, neto de comisiones (SL primero si coinciden). Tu resultado real puede variar.</i>") % (
        d["n"], d["tp2"], d["be"], d["sl"], d["exp"], d["r"], t["n"], t["r"])
    if emit(msg, False) is not None:
        st["last_summary"] = today


# ============================================================================
#  BUCLE PRINCIPAL
# ============================================================================
def run_once(cfg, symbols, meta, dry_run):
    st = load_state()
    if cfg.autotune and not dry_run:
        apply_tuned(cfg, st.get("tuned"))                      # parametros validados por el ultimo autoajuste
        RUNTIME["edge"] = st.get("edge")
        absorb_tune(cfg, st)
        maybe_launch_autotune(cfg, st, symbols, meta)
    if cfg.track_signals and not dry_run:
        track_active(cfg, st)
    funnel = {}
    btc = None
    if cfg.btc_filter:
        try:
            btc = btc_trend_map(fetch_bars(cfg, norm_symbol("BTCUSDT", cfg.exchange)), cfg.trend_ema_bars)
        except Exception as e:
            log.warning("filtro BTC desactivado en este escaneo (sin datos de BTC): %s", e)
    sigs = scan_all(cfg, symbols, meta, funnel, btc)
    sigs = [s for s in sigs if s["score"] >= cfg.min_score
            and (cfg.max_risk_pct <= 0 or s["risk_pct"] <= cfg.max_risk_pct)]
    sigs.sort(key=lambda s: (-s["score"], -s.get("rel_vol", 0.0), s["risk_pct"]))
    sent_side = {"LONG": 0, "SHORT": 0}

    now = time.time()
    today = today_ny()
    if st["daily"].get("date") != today:
        st["daily"] = {"date": today, "n": 0}
    n_sent = 0
    for s in sigs:
        key = "%s|%s|%s" % (s["symbol"], s["cycle_start_ms"], s["side"])
        if key in st["sent"]:
            continue
        if n_sent >= cfg.max_signals_per_scan:
            break
        if cfg.max_signals_per_day and st["daily"]["n"] >= cfg.max_signals_per_day:
            log.info("limite diario de senales alcanzado (%d)", cfg.max_signals_per_day)
            break
        if now - st["last_symbol"].get(s["symbol"], 0) < cfg.symbol_cooldown_min * 60:
            continue
        if cfg.max_open_per_side:
            # con seguimiento, "active" ya incluye las enviadas en este escaneo; sin el, solo se cuentan las de este escaneo
            same = (sum(1 for a in st["active"].values() if a.get("side") == s["side"])
                    if cfg.track_signals else sent_side[s["side"]])
            if same >= cfg.max_open_per_side:
                _bump(funnel, "only:concentracion")
                continue
        if cfg.max_slip_r > 0:
            ok_liq, info = exec_check(cfg, s)
            if ok_liq is False:
                log.info("%s %s descartada por liquidez (%s)", s["side"], s["symbol"],
                         "libro insuficiente" if info["impact"] == float("inf") else "coste %.2fR" % info["cost_r"])
                _bump(funnel, "only:liquidez")
                continue
            s["liq"] = info
        log.info("SEÑAL %s %s (%d/%d)", s["side"], s["symbol"], s["score"], SCORE_MAX)
        ids = emit(format_message(s, cfg), dry_run, buttons=buttons_for(s["symbol"], cfg.exchange))
        if ids is None:
            continue
        n_sent += 1
        sent_side[s["side"]] += 1
        if dry_run:
            continue
        st["sent"][key] = now
        st["last_symbol"][s["symbol"]] = now
        st["daily"]["n"] += 1
        st["hb_n"] = st.get("hb_n", 0) + 1
        bump_stats(st, "n")
        if cfg.track_signals:
            st["active"][key] = make_active(s, ids, now)
    if dry_run:
        print(funnel_text(funnel), "\n")
    else:
        for k, v in funnel.items():
            _bump(st["funnel"], k, v)
        maybe_summary(cfg, st)
        maybe_heartbeat(cfg, st, len(symbols))
        save_state(st)
    log.info("escaneo terminado: %d monedas, %d señales nuevas, %d en seguimiento",
             len(symbols), n_sent, len(st["active"]))


# ---- liquidez real en el momento de la señal (libro de ordenes) ----
DEFAULT_NOTIONAL = 1000.0


def parse_book(raw):
    bids = sorted(((float(x[0]), float(x[1])) for x in raw.get("bids", [])), reverse=True)
    asks = sorted((float(x[0]), float(x[1])) for x in raw.get("asks", []))
    return bids, asks


def book_cost(bids, asks, side, notional):
    """(spread_pct, impacto_pct): impacto = precio medio de ejecucion de 'notional' USDT frente al punto medio
    (incluye medio spread). impacto None si el libro consultado no alcanza para llenar la orden."""
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2.0
    spread = (ba - bb) / mid * 100.0
    need, got, paid = notional / mid, 0.0, 0.0
    for price, qty in (asks if side == "LONG" else bids):
        take = min(qty, need - got)
        got += take
        paid += take * price
        if got >= need - 1e-12:
            break
    if got < need * 0.999:
        return spread, None
    return spread, abs(paid / got - mid) / mid * 100.0


def fetch_book(cfg, symbol):
    if cfg.exchange == "bingx":
        for lim in (50, 20):                                   # si el limite no es valido, reintenta con otro
            try:
                return parse_book(bx_get(cfg, "/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": lim}))
            except Exception as e:
                err = e
        raise err
    return parse_book(http_get(cfg.base_url + "/fapi/v1/depth", {"symbol": symbol, "limit": 50}))


def exec_check(cfg, s):
    """(ok, info). ok None = no se pudo verificar (no bloquea). Mide el coste real de entrar y salir al tamano de tu orden."""
    ps = position_size(s, cfg)
    notional = ps["notional"] if ps and ps.get("notional") else DEFAULT_NOTIONAL
    try:
        bc = book_cost(*fetch_book(cfg, s["symbol"]), s["side"], notional)
    except Exception as e:
        log.warning("libro de %s no disponible: %s", s["symbol"], e)
        return None, None
    if bc is None:
        return None, None
    spread, impact = bc
    if impact is None:
        return False, {"spread": spread, "impact": float("inf"), "notional": notional, "cost_r": float("inf")}
    c_r = 2.0 * impact / s["risk_pct"]
    return c_r <= cfg.max_slip_r, {"spread": spread, "impact": impact, "notional": notional, "cost_r": c_r}


def funnel_text(f, top=4):
    """Resume el embudo: oportunidades y los filtros que, ellos solos, mas senales bloquean."""
    n = f.get("oportunidades", 0)
    if n == 0:
        return "Sin oportunidades evaluadas (fuera de la ventana Q2/Q3 de los ciclos de 90 min)."
    only = sorted(((k[5:], v) for k, v in f.items() if k.startswith("only:")), key=lambda x: -x[1])[:top]
    lines = ["Oportunidades evaluadas: %d | senales emitidas: %d" % (n, f.get("seq:senal", 0))]
    if only:
        lines.append("Casi validas que fallan SOLO por un filtro (relajarlo daria mas senales):")
        for name, v in only:
            lines.append("  - %s: %d" % (GATE_TEXT.get(name, name), v))
    seq = [(g, f.get("seq:" + g, 0)) for g in GATES]
    lines.append("Embudo: " + " > ".join("%s %d" % (g, v) for g, v in seq))
    return "\n".join(lines)


def maybe_heartbeat(cfg, st, n_symbols):
    if cfg.heartbeat_hours <= 0:
        return
    now = time.time()
    if not st.get("last_hb"):
        st["last_hb"] = now                                   # primer arranque: el primer aviso llega tras X horas
        return
    if now - st["last_hb"] < cfg.heartbeat_hours * 3600:
        return
    f = st.get("funnel", {})
    only = sorted(((k[5:], v) for k, v in f.items() if k.startswith("only:")), key=lambda x: -x[1])[:3]
    msg = ["💓 <b>QT Scanner activo</b> · %s" % cfg.exchange.upper(),
           "Config: " + describe_key(cfg_key(cfg)),
           "Monedas: %d | senales desde el ultimo aviso: %d | en seguimiento: %d" % (
               n_symbols, st.get("hb_n", 0), len(st["active"]))]
    if only:
        msg.append("Lo que mas frena senales ahora (casi validas que fallan solo por esto):")
        msg += ["• %s: %d" % (GATE_TEXT.get(nm, nm), v) for nm, v in only]
    else:
        msg.append("Sin oportunidades casi validas en este periodo.")
    if emit("\n".join(msg), False) is not None:
        st["last_hb"] = now
        st["hb_n"] = 0
        st["funnel"] = {}


_last_alert = {}


def notify(key, text, dry_run, every=3600):
    """Telegram, como maximo una vez por 'every' segundos para cada clave."""
    now = time.time()
    if dry_run or now - _last_alert.get(key, 0) < every:
        return
    _last_alert[key] = now
    try:
        send_telegram(text)
    except Exception as e:
        log.error("no se pudo avisar por Telegram: %s", e)


def start_text(cfg, n):
    acct = ("cuenta %.0f USDT, riesgo %.1f%%" % (cfg.account_size, cfg.risk_per_trade_pct)
            if cfg.account_size > 0 else "calculadora de posicion desactivada (falta ACCOUNT_SIZE)")
    return ("🚀 <b>QT Scanner iniciado</b> · %s\n"
            "Monedas: %d | sesiones: %s\n"
            "Sesgo: %s | cadena: %s | puntuacion minima: %d/%d\n"
            "RR %.1f | TP1 %.1fR (%.0f%%) | riesgo max SL %.1f%%\n"
            "%s\n"
            "Seguimiento: %s | resumen diario: %s | autoajuste: %s") % (
        cfg.exchange.upper(), n, ", ".join(SESSION_NAMES[i] for i in cfg.sessions), cfg.bias_mode,
        "obligatoria" if cfg.require_chain else "opcional", cfg.min_score, SCORE_MAX,
        cfg.rr, cfg.partial_r, cfg.partial_pct, cfg.max_risk_pct, acct,
        "on" if cfg.track_signals else "off",
        ("%d:00 NY" % cfg.daily_summary_hour) if cfg.daily_summary_hour >= 0 else "off",
        ("cada %.0f h (%s)" % (cfg.autotune_hours, cfg.autotune_mode)) if cfg.autotune else "off")


def loop(cfg, dry_run, only):
    symbols, meta, last_load = [], {}, 0
    announced = False
    while True:
        try:
            if not symbols or time.time() - last_load > 1800:      # refresca la lista cada 30 min
                symbols, meta = load_symbols(cfg, only)
                last_load = time.time()
                log.info("%d monedas a escanear", len(symbols))
                if not announced:
                    announced = True
                    notify("start", start_text(cfg, len(symbols)), dry_run, every=0)
            t_scan = time.time()
            run_once(cfg, symbols, meta, dry_run)
            took = time.time() - t_scan
            if took > 240:
                log.warning("el escaneo tardo %.0fs (>4 min): reduce MAX_SYMBOLS o sube MIN_QUOTE_VOLUME", took)
        except Exception as e:
            log.error("error en el escaneo: %s", e)
            hint = ""
            if "451" in str(e) or "403" in str(e):
                hint = "\n%s bloquea la region del servidor: cambia la region en Railway (p. ej. Europa)." % cfg.exchange.capitalize()
            elif "BingX codigo" in str(e):
                hint = "\nSi el codigo habla de firma/timestamp, define BINGX_API_KEY y BINGX_API_SECRET (clave de solo lectura)."
            notify("scan_error", "⚠️ QT Scanner: error en el escaneo: %s%s" % (str(e)[:200], hint), dry_run)
        now = time.time()
        nxt = (int(now // 300) + 1) * 300 + 6                      # 6 s despues del cierre de la vela de 5m
        time.sleep(max(1, nxt - now))


# ============================================================================
#  BACKTEST (historico de Binance, comisiones incluidas)
# ============================================================================
def simulate_trade(s, future, cfg):
    """Gestion del trade con velas de 5m (SL primero si coinciden). Devuelve (tipo, R neto de comisiones)."""
    risk = abs(s["entry"] - s["sl"])
    sgn = 1 if s["side"] == "LONG" else -1
    a = {"side": s["side"], "entry": s["entry"], "sl": s["sl"], "sl_now": s["sl"],
         "tp1": s["entry"] + sgn * risk * cfg.partial_r, "tp2": s["entry"] + sgn * risk * cfg.rr,
         "last_t": s["entry_t"], "last_c": s["entry"], "created": 0.0, "status": "open"}
    ev = evaluate_signal(a, future, cfg, 0.0)
    if a["status"] != "closed":
        ev += evaluate_signal(a, [], cfg, cfg.track_hours * 3600 + 1.0)
    kind, r = [e for e in ev if e[0] in STAT_FIELD][-1]
    fee_r = cost_r(cfg, s["entry"], risk)
    return kind, r - fee_r


def trade_stats(rs):
    n = len(rs)
    if n == 0:
        return None
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    avg = sum(rs) / n
    sd = (sum((r - avg) ** 2 for r in rs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    peak = eq = mdd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return {"n": n, "win": 100.0 * sum(1 for r in rs if r > 0) / n, "avg": avg,
            "pf": (gp / gl) if gl > 0 else float("inf"), "sum": sum(rs),
            "ci": 1.96 * sd / math.sqrt(n), "mdd": mdd}


def backtest_variant(cfg, data, meta, preps, warm_t, funnel=None):
    """Genera los trades simulados de una configuracion sobre el historico descargado."""
    trades = []
    horizon = int(cfg.track_hours * 12)
    for sym, bars in data.items():
        tick = meta.get(sym, {}).get("tick", 0.0001)
        sigs = scan_series(sym, bars, tick, cfg, only_last=False, funnel=funnel, prep=preps[sym])
        sigs = [s for s in sigs if s["entry_t"] >= warm_t and s["score"] >= cfg.min_score
                and s["bar_i"] + horizon < len(bars)]
        sigs.sort(key=lambda s: s["entry_t"])
        last_t = -10 ** 15
        for s in sigs:
            if s["entry_t"] - last_t < cfg.symbol_cooldown_min * 60_000:
                continue                                          # mismo cooldown que en vivo
            last_t = s["entry_t"]
            future = bars[s["bar_i"] + 1: s["bar_i"] + 1 + horizon]
            kind, r = simulate_trade(s, future, cfg)
            trades.append({"r": r, "kind": kind, "t": s["entry_t"], "side": s["side"], "symbol": sym,
                           "session": s["session"], "score": s["score"], "sig": s, "future": future})
    return trades


def _row(name, trades, mid_t):
    st = trade_stats([t["r"] for t in trades])
    if st is None:
        return "%-44s %5d   (sin senales)" % (name[:44], 0)
    h1 = trade_stats([t["r"] for t in trades if t["t"] < mid_t])
    h2 = trade_stats([t["r"] for t in trades if t["t"] >= mid_t])
    f = lambda x: ("%+.2f" % x["avg"]) if x else "  -  "  # noqa: E731
    pf = "%.2f" % st["pf"] if st["pf"] != float("inf") else " inf"
    flag = "  <- positivo con 95%" if st["avg"] - st["ci"] > 0 else ""
    return "%-44s %5d %5.1f%% %+6.2f ±%.2f %5s %+8.1f %7s %7s%s" % (
        name[:44], st["n"], st["win"], st["avg"], st["ci"], pf, st["sum"], f(h1), f(h2), flag)


ROW_HEAD = "%-44s %5s %6s %6s %5s %5s %8s %7s %7s" % ("", "n", "win", "R neto", "±95%", "PF", "suma R", "1a mit", "2a mit")

VARIANTS = [
    ("tu configuracion", {}),
    ("sin cadena", {"require_chain": False}),
    ("sin sesgo True Open", {"bias_mode": "none"}),
    ("sin cadena ni sesgo", {"require_chain": False, "bias_mode": "none"}),
    ("sin cadena + 4 sesiones", {"require_chain": False, "sessions": (0, 1, 2, 3)}),
    ("sin cadena + TO modo tendencia", {"require_chain": False, "to_logic": "tendencia"}),
    ("sin cadena + tendencia 1H obligatoria", {"require_chain": False, "trend_filter": True}),
    ("sin cadena + volumen relativo >= 1.5", {"require_chain": False, "min_rel_vol": 1.5}),
    ("sin cadena + score >= 4", {"require_chain": False, "min_score": 4}),
    ("sin cadena + score >= 5", {"require_chain": False, "min_score": 5}),
]

TARGET_VARIANTS = [
    ("tu gestion actual", {}),
    ("sin parcial, TP 1.5R", {"partial_pct": 0.0, "partial_r": 99.0, "rr": 1.5}),
    ("sin parcial, TP 2R", {"partial_pct": 0.0, "partial_r": 99.0, "rr": 2.0}),
    ("sin parcial, TP 3R", {"partial_pct": 0.0, "partial_r": 99.0, "rr": 3.0}),
    ("50% en 1R + TP 2R", {"partial_pct": 50.0, "partial_r": 1.0, "rr": 2.0}),
    ("50% en 1R + TP 3R", {"partial_pct": 50.0, "partial_r": 1.0, "rr": 3.0}),
    ("50% en 1.5R + TP 3R", {"partial_pct": 50.0, "partial_r": 1.5, "rr": 3.0}),
]


def _group(trades, key, label):
    groups = {}
    for t in trades:
        groups.setdefault(t[key], []).append(t["r"])
    print("\nPor %s:" % label)
    for k in sorted(groups, key=lambda x: str(x)):
        st = trade_stats(groups[k])
        print("  %-10s n=%-4d win %5.1f%%  R neto %+.2f  suma %+.1f" % (k, st["n"], st["win"], st["avg"], st["sum"]))


def run_backtest(cfg, only, days, top, compare):
    symbols, meta = load_symbols(cfg, only)
    if top:
        symbols = symbols[:top]
    print("Descargando %d dias de velas 5m de %d monedas (puede tardar 1-3 min)..." % (days, len(symbols)))
    data = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_history, cfg, s, days): s for s in symbols}
        for f in as_completed(futs):
            try:
                bars = f.result()
            except Exception as e:
                log.warning("%s: %s", futs[f], e)
                continue
            if len(bars) > 300:
                data[futs[f]] = bars
    if not data:
        sys.exit("No se pudo descargar historico (revisa EXCHANGE, la region del servidor y BASE_URL).")
    t_end = max(b[-1]["t"] for b in data.values())
    warm_t = t_end - days * 86_400_000
    mid_t = warm_t + (t_end - warm_t) // 2
    preps = {sym: prepare_series(bars, cfg) for sym, bars in data.items()}
    print("%d monedas con datos | periodo evaluado: %d dias | comisiones: %.3f%% por lado\n" % (
        len(data), days, cfg.fee_pct))

    funnel = {}
    base = backtest_variant(cfg, data, meta, preps, warm_t, funnel)
    print("EMBUDO DE FILTROS (tu configuracion)")
    print(funnel_text(funnel, top=6), "\n")

    print("RESULTADOS (R neto de comisiones; R = tu riesgo por trade)")
    print(ROW_HEAD)
    rows = [("tu configuracion", base)]
    if compare:
        for name, kw in VARIANTS[1:]:
            rows.append((name, backtest_variant(replace(cfg, **kw), data, meta, preps, warm_t)))
    for name, tr in rows:
        print(_row(name, tr, mid_t))

    if base:
        print("\nGESTION DEL TRADE (mismas entradas de tu configuracion)")
        print(ROW_HEAD)
        for name, kw in TARGET_VARIANTS:
            c2 = replace(cfg, **kw)
            trs = [dict(t, r=simulate_trade(t["sig"], t["future"], c2)[1]) for t in base]
            print(_row(name, trs, mid_t))
        _group(base, "session", "sesion")
        _group(base, "side", "lado")
        _group(base, "score", "puntuacion")
        print("\nFrecuencia: %.2f senales/dia en %d monedas" % (len(base) / float(days), len(data)))

    print("\nLECTURA: una variante solo merece confianza si (1) n >= 100-200, (2) el R neto es positivo en la 1a y en la\n"
          "2a mitad, y (3) el intervalo ±95%% no incluye 0. Con %d variantes probadas, alguna saldra positiva por azar:\n"
          "confirma la elegida en demo/papel unos dias antes de usar dinero real. Un backtest no garantiza nada.\n"
          "Limites: entrada al cierre de la vela (sin deslizamiento), SL primero si SL y TP caen en la misma vela." % len(rows))


# ============================================================================
#  AUTOAJUSTE (walk-forward con salvaguardas contra el sobreajuste)
# ============================================================================
# Cada AUTOTUNE_HOURS el bot descarga AUTOTUNE_DAYS dias de una MUESTRA de monedas (1/3 las mas liquidas + el resto
# al azar, con semilla diaria: cada dia otra muestra, asi con el tiempo se cubre toda la lista), simula una rejilla
# FIJA de 216 variantes con costes (comision + deslizamiento) y elige la mejor segun estas reglas:
#   1) >= AUTOTUNE_MIN_TRADES operaciones y R neto > 0 en AMBOS tramos: entrenamiento (primeros 2/3) y
#      validacion (ultimo 1/3);
#   2) cota inferior (R medio - Z * error estandar) sobre todo el periodo > 0;
#   3) solo sustituye a la actual si mejora su cota inferior en >= AUTOTUNE_MIN_GAIN_R (histeresis);
#   4) si NINGUNA cumple, no inventa nada: mantiene la actual y marca las señales como "sin ventaja validada".
# Probar 216 variantes genera falsos positivos por azar: estas reglas los reducen, no los eliminan.
TUNE_ENTRIES = [(c, t, b, ss) for c in (True, False) for t in (False, True) for b in (False, True)
                for ss in ((1, 2), (1, 2, 3), (0, 1, 2, 3))]
TUNE_SCORES = (3, 4, 5)
TUNE_EXITS = [(2.0, 1.0, 50.0), (3.0, 1.0, 50.0), (1.5, 99.0, 0.0)]      # (rr, partial_r, partial_pct)
TIER_NAMES = ("liquidas", "medias", "cola")
RUNTIME = {"edge": None}                                                # True / False / None (sin evaluar aun)
_tune = {"running": False, "result": None}


def cfg_key(cfg):
    return (bool(cfg.require_chain), bool(cfg.trend_filter), bool(cfg.btc_filter), tuple(cfg.sessions),
            int(cfg.min_score), float(cfg.rr), float(cfg.partial_r), float(cfg.partial_pct))


def key_dict(key):
    return {"require_chain": key[0], "trend_filter": key[1], "btc_filter": key[2], "sessions": list(key[3]),
            "min_score": key[4], "rr": key[5], "partial_r": key[6], "partial_pct": key[7]}


def apply_tuned(cfg, d):
    """Aplica un dict de parametros ajustados (tambien el leido de state.json)."""
    if not d:
        return
    cfg.require_chain = bool(d["require_chain"])
    cfg.trend_filter = bool(d["trend_filter"])
    cfg.btc_filter = bool(d.get("btc_filter", False))
    cfg.sessions = tuple(int(x) for x in d["sessions"])
    cfg.min_score = int(d["min_score"])
    cfg.rr = float(d["rr"])
    cfg.partial_r = float(d["partial_r"])
    cfg.partial_pct = float(d["partial_pct"])


def describe_key(key):
    ex = "sin parcial, TP %.1fR" % key[5] if key[7] <= 0 else "%.0f%% en %.1fR + TP %.1fR" % (key[7], key[6], key[5])
    return "cadena %s · tendencia 1H %s · BTC %s · sesiones %s · score ≥ %d · %s" % (
        "obligatoria" if key[0] else "opcional", "obligatoria" if key[1] else "opcional",
        "a favor" if key[2] else "indiferente", ",".join(SESSION_NAMES[i] for i in key[3]), key[4], ex)


def pick_sample(symbols, n, seed):
    """(simbolo, nivel_de_liquidez) para ajustar. Si hay <= n monedas, todas. Si no: el primer tercio del cupo son las
    mas liquidas y el resto una muestra aleatoria (semilla diaria) del resto de la lista, que ya viene ordenada por volumen."""
    total = len(symbols)
    tier = lambda i: 0 if i < total / 3.0 else (1 if i < 2 * total / 3.0 else 2)  # noqa: E731
    if total <= n:
        return [(s_, tier(i)) for i, s_ in enumerate(symbols)]
    k = max(1, n // 3)
    idx = list(range(k)) + sorted(random.Random(seed).sample(range(k, total), n - k))
    return [(symbols[i], tier(i)) for i in idx]


def autotune_collect(base, sample, meta, days, btc, deadline):
    """Descarga y simula, moneda a moneda (memoria baja). Devuelve (resultados, n_monedas, t_ini, t_fin)."""
    horizon = int(base.track_hours * 12)
    entries = [e for e in TUNE_ENTRIES if btc or not e[2]]
    results, n_ok, t_hi = {}, 0, 0
    for sym, tier in sample:
        if time.time() > deadline:
            log.warning("autoajuste: presupuesto de tiempo agotado tras %d monedas", n_ok)
            break
        time.sleep(0.15)                                       # no competir con el escaneo en vivo por el limite de peticiones
        try:
            bars = fetch_history(base, sym, days)
        except Exception as e:
            log.warning("autoajuste %s: %s", sym, e)
            continue
        if len(bars) < 600:
            continue
        n_ok += 1
        t_hi = max(t_hi, bars[-1]["t"])
        warm_t = bars[-1]["t"] - days * 86_400_000
        prep = prepare_series(bars, base)
        tick = meta.get(sym, {}).get("tick", 0.0001)
        for chain, trend, use_btc, ss in entries:
            c0 = replace(base, require_chain=chain, trend_filter=trend, btc_filter=use_btc, sessions=ss, min_score=0)
            sigs = [x for x in scan_series(sym, bars, tick, c0, only_last=False, prep=prep, btc=btc if use_btc else None)
                    if x["entry_t"] >= warm_t and x["bar_i"] + horizon < len(bars)]
            sigs.sort(key=lambda x: x["entry_t"])
            for ms in TUNE_SCORES:
                accepted, last_t = [], -10 ** 15
                for x in sigs:
                    if x["score"] < ms or x["entry_t"] - last_t < base.symbol_cooldown_min * 60_000:
                        continue
                    last_t = x["entry_t"]
                    accepted.append(x)
                for rr, pr, pp in TUNE_EXITS:
                    ce = replace(base, rr=rr, partial_r=pr, partial_pct=pp)
                    lst = results.setdefault((chain, trend, use_btc, ss, ms, rr, pr, pp), [])
                    for x in accepted:
                        fut = bars[x["bar_i"] + 1: x["bar_i"] + 1 + horizon]
                        lst.append((x["entry_t"], simulate_trade(x, fut, ce)[1], tier))
    t_lo = t_hi - days * 86_400_000
    return results, n_ok, t_lo, t_hi


def tier_stats(trs):
    """{nivel: (n, R medio)} para operaciones con nivel de liquidez."""
    out = {}
    for x in trs:
        if len(x) > 2:
            out.setdefault(x[2], []).append(x[1])
    return {t: (len(v), sum(v) / len(v)) for t, v in out.items()}


def tune_select(cfg, results, t_split):
    """Aplica las reglas de validacion. Devuelve (stats, eligible, best, inc, decision, edge)."""
    stats = {}
    for key, trs in results.items():
        st = trade_stats([x[1] for x in trs])
        if not st:
            continue
        tr = trade_stats([x[1] for x in trs if x[0] < t_split])
        va = trade_stats([x[1] for x in trs if x[0] >= t_split])
        se = st["ci"] / 1.96
        stats[key] = {"st": st, "tr": tr, "va": va, "lcb": st["avg"] - cfg.autotune_z * se}
    need_va = max(10, cfg.autotune_min_trades // 3)
    eligible = [k for k, v in stats.items()
                if v["st"]["n"] >= cfg.autotune_min_trades and v["va"] and v["va"]["n"] >= need_va
                and v["tr"] and v["tr"]["avg"] > 0 and v["va"]["avg"] > 0 and v["lcb"] > 0]
    best = max(eligible, key=lambda k: stats[k]["lcb"]) if eligible else None
    inc = cfg_key(cfg)
    if best is None:
        return stats, eligible, best, inc, "keep", False
    if inc in eligible and (best == inc or stats[best]["lcb"] < stats[inc]["lcb"] + cfg.autotune_min_gain_r):
        return stats, eligible, best, inc, "keep", True
    return stats, eligible, best, inc, "switch", True


def autotune_run(base, symbols, meta):
    """symbols: lista COMPLETA ordenada por volumen (la muestra se elige aqui)."""
    t0 = time.time()
    seed = int(t0 // 86400)
    sample = pick_sample(symbols, base.autotune_top or len(symbols), seed)
    random.Random(seed).shuffle(sample)                       # si se agota el tiempo, el corte no sesga hacia las liquidas
    btc = None
    try:
        bb = fetch_history(base, norm_symbol("BTCUSDT", base.exchange), base.autotune_days)
        if len(bb) >= 600:
            btc = btc_trend_map(bb, base.trend_ema_bars)
    except Exception as e:
        log.warning("autoajuste sin variantes BTC (no hay datos de BTC): %s", e)
    results, n_ok, t_lo, t_hi = autotune_collect(base, sample, meta, base.autotune_days, btc,
                                                 t0 + base.autotune_max_minutes * 60)
    if n_ok == 0:
        raise RuntimeError("autoajuste: no se pudo descargar historico de ninguna moneda")
    t_split = t_lo + (t_hi - t_lo) * 2 // 3
    stats, eligible, best, inc, decision, edge = tune_select(base, results, t_split)
    return {"stats": stats, "eligible": eligible, "best": best, "inc": inc, "decision": decision, "edge": edge,
            "tiers": {"best": tier_stats(results[best]) if best else {}, "inc": tier_stats(results.get(inc, []))},
            "n_symbols": n_ok, "n_total": len(symbols), "days": base.autotune_days,
            "secs": time.time() - t0, "when": time.time()}


def autotune_job(base, symbols, meta):
    try:
        _tune["result"] = autotune_run(base, symbols, meta)
    except Exception as e:                                    # nunca debe tumbar el bot
        log.error("autoajuste fallido: %s", e)
        _tune["result"] = {"error": str(e)}
    finally:
        _tune["running"] = False


def maybe_launch_autotune(cfg, st, symbols, meta):
    if not cfg.autotune or _tune["running"] or not symbols:
        return
    now = time.time()
    if now - st.get("last_tune", 0.0) < cfg.autotune_hours * 3600:
        return
    st["last_tune"] = now
    _tune["running"] = True
    log.info("autoajuste: lanzado (%d de %d monedas, %d dias)",
             min(len(symbols), cfg.autotune_top) if cfg.autotune_top else len(symbols), len(symbols), cfg.autotune_days)
    threading.Thread(target=autotune_job, args=(replace(cfg), list(symbols), dict(meta)), daemon=True).start()


def _stat_line(v):
    f = lambda x: ("%+.2fR" % x["avg"]) if x else "-"  # noqa: E731
    return "%d ops · acierto %.0f%% · R neto %+.2f ±%.2f · entren. %s · valid. %s" % (
        v["st"]["n"], v["st"]["win"], v["st"]["avg"], v["st"]["ci"], f(v["tr"]), f(v["va"]))


def tier_line(ts):
    if not ts:
        return ""
    return "Por liquidez: " + " · ".join("%s %+.2fR (n=%d)" % (TIER_NAMES[t], avg, n) for t, (n, avg) in sorted(ts.items()))


def tune_message(cfg, res, applied):
    st, best, inc = res["stats"], res["best"], res["inc"]
    head = "🧠 <b>Autoajuste</b> · %d de %d monedas · %d dias" % (res["n_symbols"], res.get("n_total", res["n_symbols"]), res["days"])
    tiers = res.get("tiers", {})
    if res["decision"] == "switch":
        verb = "Aplicado" if applied else "Propuesta (AUTOTUNE_MODE=suggest, no aplicada)"
        lines = [head, "%s: <b>%s</b>" % (verb, describe_key(best)), _stat_line(st[best]), tier_line(tiers.get("best"))]
        if inc in st:
            lines += ["Antes: %s" % describe_key(inc), _stat_line(st[inc])]
        else:
            lines.append("Antes: %s (sin operaciones suficientes)" % describe_key(inc))
    elif res["edge"]:
        lines = [head, "Se mantiene la configuracion actual (validada): %s" % describe_key(inc), _stat_line(st[inc]),
                 tier_line(tiers.get("inc"))]
    else:
        lines = [head, "⚠️ <b>Ninguna variante muestra ventaja validada</b> en los ultimos %d dias "
                 "(tras costes, en entrenamiento y validacion)." % res["days"],
                 "Las señales siguen llegando con la configuracion actual, pero marcadas como experimentales. "
                 "No significa que vaya a perder: significa que no hay evidencia de que gane."]
        if inc in st:
            lines += [_stat_line(st[inc]), tier_line(tiers.get("inc"))]
    lines.append("<i>Simulado con velas de 5m, neto de comisiones y deslizamiento estimado. Probar muchas variantes puede dar falsos positivos.</i>")
    return "\n".join(x for x in lines if x)


def absorb_tune(cfg, st, dry_run=False):
    """Recoge el resultado del hilo de autoajuste (lo llama run_once con el estado ya cargado)."""
    res = _tune["result"]
    if res is None:
        return
    _tune["result"] = None
    if "error" in res:
        return
    prev_edge = st.get("edge")
    st["edge"] = res["edge"]
    RUNTIME["edge"] = res["edge"]
    applied = False
    if res["decision"] == "switch" and cfg.autotune_mode == "apply":
        st["tuned"] = key_dict(res["best"])
        apply_tuned(cfg, st["tuned"])
        applied = True
    changed = res["decision"] == "switch" or prev_edge != res["edge"]
    log.info("autoajuste: decision=%s edge=%s (%.0fs)", res["decision"], res["edge"], res["secs"])
    if changed and not dry_run:
        emit(tune_message(cfg, res, applied), False)


def print_tune_report(cfg, res, top=12):
    st = res["stats"]
    print("AUTOAJUSTE · %d de %d monedas · %d dias · %.0fs" % (res["n_symbols"], res.get("n_total", res["n_symbols"]),
                                                              res["days"], res["secs"]))
    rows = sorted(st.items(), key=lambda kv: -kv[1]["lcb"])
    print("%-4s %-106s %5s %6s %7s %7s %7s" % ("", "variante", "n", "win", "R neto", "cota", "valid."))
    for key, v in rows[:top]:
        mark = "*" if key in res["eligible"] else " "
        mine = "<-- actual" if key == res["inc"] else ""
        print("%-4s %-106s %5d %5.0f%% %+7.2f %+7.2f %7s %s" % (
            mark, describe_key(key)[:106], v["st"]["n"], v["st"]["win"], v["st"]["avg"], v["lcb"],
            ("%+.2f" % v["va"]["avg"]) if v["va"] else "-", mine))
    print("\n(*) = cumple todas las reglas de validacion: %d de %d variantes" % (len(res["eligible"]), len(st)))
    if res["inc"] in st and res["inc"] not in [k for k, _ in rows[:top]]:
        print("Tu configuracion actual: %s | %s" % (describe_key(res["inc"]), _stat_line(st[res["inc"]])))
    tiers = res.get("tiers", {})
    if res["best"] and tier_line(tiers.get("best")):
        print("Mejor validada -> " + tier_line(tiers.get("best")))
    if tier_line(tiers.get("inc")):
        print("Actual         -> " + tier_line(tiers.get("inc")))
    print("DECISION:", {"switch": "cambiar a la mejor validada", "keep": "mantener la actual"}[res["decision"]],
          "| ventaja validada:", "SI" if res["edge"] else "NO")


# ============================================================================
#  AUTOTEST OFFLINE
# ============================================================================
def _ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC).timestamp() * 1000)


def sample_signal():
    """Senal de ejemplo para --test-telegram."""
    return {"symbol": "BTCUSDT", "side": "LONG", "entry": 100000.0, "sl": 99500.0, "tp1": 100500.0,
            "tp2": 101000.0, "max_entry": 100150.0, "risk_pct": 0.5, "chain": "Prof 4 | Daily a Nano | Q2",
            "session": "NY AM", "time_ny": datetime.now(NY).strftime("%Y-%m-%d %H:%M"), "cycle_start_ms": 0,
            "entry_t": 0, "tick": 0.1, "step": 0.001, "score": 4, "q1_level": 99650.0,
            "bias_levels": [("TO Daily", 100300.0), ("TO Sesión", 100200.0)], "bias_rel": "bajo",
            "fee_r": 0.2, "rel_vol": 1.8, "trend_ok": True}


def selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        print("%-52s %s" % (name, "OK" if cond else "FALLO"))
        ok = ok and bool(cond)

    # 1) Ejemplos del texto "Quarter Sequence 102"
    cfg = Config(lowest_level="Nano", need_level="Ninguno")
    q = quarters(_ms(2026, 7, 15, 10, 0))["qs"]
    ch = detect_chain(q, cfg)
    check("cadena 15 jul 2026 10:00 NY (%s)" % chain_text(ch),
          ch and ch["depth"] == 5 and ch["start"] == 2 and ch["end"] == 6 and ch["q"] == 2)
    q = quarters(_ms(2026, 9, 23, 9, 58))["qs"]
    ch = detect_chain(q, cfg)
    check("cadena 23 sep 2026 09:58 NY (%s)" % chain_text(ch), ch and ch["depth"] == 7 and ch["q"] == 2)
    check("viernes: Weekly fuera (Q0)", quarters(_ms(2026, 7, 17, 10, 0))["qs"][2] == -1)

    # 2) Senal sintetica: SHORT tras barrer el Q1 de un ciclo de 90 min (miercoles 15 jul 2026, NY AM)
    cfg = Config(require_chain=False, bias_mode="none", sessions=(1, 2))
    bars = []
    t = _ms(2026, 7, 15, 3, 0)
    while t < _ms(2026, 7, 15, 7, 30):                     # ruido para el ATR (>= 40 velas)
        bars.append({"t": t, "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0})
        t += BAR_MS
    for m in (30, 35, 40, 45, 50):                         # Q1 del ciclo 07:30 (rango 99.8-100.4)
        bars.append({"t": _ms(2026, 7, 15, 7, m), "o": 100.0, "h": 100.4, "l": 99.8, "c": 100.1})
    bars.append({"t": _ms(2026, 7, 15, 7, 55), "o": 100.5, "h": 100.9, "l": 99.95, "c": 100.05})
    sigs = analyze("TESTUSDT", bars, 0.01, cfg)
    check("senal sintetica SHORT", len(sigs) == 1 and sigs[0]["side"] == "SHORT"
          and sigs[0]["sl"] > 100.9 and sigs[0]["tp2"] < 100.05)
    if sigs:
        s = sigs[0]
        check("puntuacion 0-%d y max_entry bajo la entrada" % SCORE_MAX,
              0 <= s["score"] <= SCORE_MAX and s["max_entry"] < s["entry"])
        print(plain(format_message(s, Config(account_size=1000))), "\n")
    bars2 = bars[:-1] + [{"t": _ms(2026, 7, 15, 7, 55), "o": 100.1, "h": 100.3, "l": 99.9, "c": 100.0}]
    check("sin barrido -> sin senal", analyze("TESTUSDT", bars2, 0.01, cfg) == [])

    # 3) Calculadora de posicion
    s = sample_signal()
    s.update(entry=100.0, sl=99.0, step=0.001)
    ps = position_size(s, Config(account_size=1000, risk_per_trade_pct=1.0, max_leverage=10))
    check("tamano: riesgo 10 USDT / 1 USDT = 10 uds, 1x", abs(ps["qty"] - 10) < 1e-6 and ps["lev"] == 1)
    ps = position_size(s, Config(account_size=1000, risk_per_trade_pct=10.0, max_leverage=5))
    check("tamano limitado por apalancamiento maximo", ps["capped"] and abs(ps["qty"] - 50) < 1e-6 and ps["lev"] == 5)
    check("sin ACCOUNT_SIZE no hay calculadora", position_size(s, Config()) is None)

    # 4) Seguimiento de senales
    cfg = Config()                                          # partial 50% a 1R, TP2 a 2R
    mk = lambda side: make_active({"symbol": "T", "side": side, "entry": 100.0,  # noqa: E731
                                   "sl": 99.0 if side == "LONG" else 101.0,
                                   "tp1": 101.0 if side == "LONG" else 99.0,
                                   "tp2": 102.0 if side == "LONG" else 98.0,
                                   "tick": 0.01, "entry_t": 0, "score": 3, "fee_r": 0.2}, {}, 0)

    def bar(i, h, l):
        return {"t": i * BAR_MS, "o": 100, "h": h, "l": l, "c": 100}

    a = mk("LONG")
    e1 = evaluate_signal(a, [bar(1, 101.2, 99.5)], cfg, 60)
    e2 = evaluate_signal(a, [bar(2, 102.1, 100.5)], cfg, 120)
    check("LONG: TP1 y luego TP2 (+1.5R)", [x[0] for x in e1] == ["TP1"] and e2 == [("TP2", 1.5)] and a["status"] == "closed")
    a = mk("LONG")
    e = evaluate_signal(a, [bar(1, 100.5, 98.9)], cfg, 60)
    check("LONG: SL (-1R)", e == [("SL", -1.0)])
    a = mk("LONG")
    e = evaluate_signal(a, [bar(1, 101.2, 99.5), bar(2, 100.4, 99.9)], cfg, 60)
    check("LONG: TP1 y luego BE (+0.5R)", e == [("TP1", 0.0), ("BE", 0.5)])
    a = mk("LONG")
    e = evaluate_signal(a, [bar(1, 102.5, 98.5)], cfg, 60)
    check("misma vela SL y TP2 -> cuenta SL", e == [("SL", -1.0)])
    a = mk("SHORT")
    e = evaluate_signal(a, [bar(1, 100.5, 98.9), bar(2, 99.8, 97.9)], cfg, 60)
    check("SHORT: TP1 y TP2", [x[0] for x in e] == ["TP1", "TP2"])
    a = mk("LONG")
    e = evaluate_signal(a, [bar(1, 100.5, 99.5)], cfg, 7 * 3600)
    check("caducidad tras TRACK_HOURS", [x[0] for x in e] == ["EXP"])

    # 4b) Caducidad valorada a precio de cierre
    a = mk("LONG")
    e = evaluate_signal(a, [{"t": BAR_MS, "o": 100, "h": 100.6, "l": 99.7, "c": 100.5}], cfg, 7 * 3600)
    check("caducidad a mercado: cierra +0.5R sobre entrada", e and e[0][0] == "EXP" and abs(e[0][1] - 0.5) < 1e-9)

    # 4c) Filtro de comisiones: SL muy corto -> comisiones ida y vuelta > 0.35R -> descartada
    bar0 = {"t": 0, "o": 100.0, "h": 100.1, "l": 99.9, "c": 100.0}
    qi0 = quarters(_ms(2026, 7, 15, 8, 0))
    exb = {"chain_depth": 0, "bias_levels": [], "body_ratio": 0.5, "sweep_atr": 0.5, "q1_level": 100.0,
           "rel_vol": 2.0, "trend_ok": True}
    check("comisiones: SL 0.2% (0.5R de fees) descartado",
          _build_signal("T", "LONG", bar0, 99.8, 0.01, Config(), None, qi0, 0, exb) is None)
    sg = _build_signal("T", "LONG", bar0, 99.5, 0.01, Config(), None, qi0, 0, exb)
    check("costes: SL 0.5% pasa; (0.05%+0.02%)x2 / 0.5% = 0.28R",
          sg is not None and abs(sg["fee_r"] - 0.28) < 1e-9)
    sg0 = _build_signal("T", "LONG", bar0, 99.5, 0.01, Config(slip_pct=0.0), None, qi0, 0, exb)
    check("costes: sin deslizamiento = 0.20R", sg0 is not None and abs(sg0["fee_r"] - 0.2) < 1e-9)
    check("puntuacion suma tendencia 1H y volumen", sg is not None and sg["score"] == score_signal(exb, Config()) == 4)

    # 4d) EMA
    em = ema_series([1.0] * 30, 20)
    check("EMA: None antes de n y constante despues", em[18] is None and abs(em[19] - 1.0) < 1e-12 and abs(em[29] - 1.0) < 1e-12)

    # 4e) Modo historico == modo en vivo (misma logica en todas las velas) y embudo coherente
    import random
    rnd = random.Random(7)
    loose = Config(require_chain=False, bias_mode="none", sessions=(0, 1, 2, 3), body_pct=20.0, min_sweep_atr=0.05,
                   max_fee_r=0.0, max_risk_pct=0.0, min_risk_ticks=1)
    syn, price, t = [], 100.0, _ms(2026, 7, 14, 18, 0)
    for _ in range(3 * 288):
        o = price
        c = o * (1 + rnd.gauss(0, 0.0025))
        hh = max(o, c) * (1 + abs(rnd.gauss(0, 0.0012)))
        ll = min(o, c) * (1 - abs(rnd.gauss(0, 0.0012)))
        syn.append({"t": t, "o": o, "h": hh, "l": ll, "c": c, "v": 1000 * (1 + rnd.random())})
        price, t = c, t + BAR_MS
    fl = {}
    full = scan_series("SYN", syn, 0.0001, loose, only_last=False, funnel=fl)
    full_set = {(x["bar_i"], x["side"], round(x["entry"], 8), round(x["sl"], 8)) for x in full}
    live_set, live_idx = set(), set(i for i, *_ in full_set)
    for j in sorted(live_idx | set(range(80, len(syn), 5))):
        for x in analyze("SYN", syn[:j + 1], 0.0001, loose):
            live_set.add((j, x["side"], round(x["entry"], 8), round(x["sl"], 8)))
    check("backtest == vivo en %d senales sinteticas (y 0 extra)" % len(full_set),
          len(full_set) > 0 and live_set == {x for x in full_set if x[0] in live_idx | set(range(80, len(syn), 5))})
    check("embudo: senales == seq:senal y only<=oportunidades",
          fl.get("seq:senal", 0) == len(full)
          and all(v <= fl["oportunidades"] for k, v in fl.items() if k.startswith("only:")))
    trs = [simulate_trade(x, syn[x["bar_i"] + 1: x["bar_i"] + 73], loose)[1] for x in full if x["bar_i"] + 73 < len(syn)]
    st_ = trade_stats(trs)
    check("simulacion de trades y estadisticas (n=%d)" % len(trs), st_ is not None and st_["n"] == len(trs) and st_["mdd"] >= 0)
    check("trend_filter reduce (o iguala) las senales",
          len(scan_series("SYN", syn, 0.0001, replace(loose, trend_filter=True), only_last=False)) <= len(full))

    # 6) BingX: formatos de velas, TradFi, simbolos
    d_ = _bx_bar({"time": 1700000000000, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"})
    l_ = _bx_bar([1700000000, "1", "2", "0.5", "1.5", "10"])
    check("BingX vela dict == lista, segundos -> ms, volumen ~ USDT",
          d_["t"] == l_["t"] == 1700000000000 and abs(d_["v"] - 15.0) < 1e-9 and d_["h"] == 2.0)
    bb = _bx_bars([{"time": 1700000300000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                   {"time": 1700000000000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
                   {"time": 1700000000000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}])
    check("BingX velas: orden ascendente y sin duplicados", [x["t"] for x in bb] == [1700000000000, 1700000300000])
    check("TradFi detectado (NCSK/NCCO/..2USD) y cripto no",
          is_tradfi("NCSKTSLA2USD-USDT") and is_tradfi("NCCOGOLD2USD-USDT")
          and not any(is_tradfi(x) for x in ("BTC-USDT", "1000PEPE-USDT", "NCT-USDT", "SOL-USDT")))
    check("norm_symbol BingX/Binance", norm_symbol("btcusdt", "bingx") == "BTC-USDT"
          and norm_symbol("BTC-USDT", "binance") == "BTCUSDT" and norm_symbol("ETH-USDT", "bingx") == "ETH-USDT")

    g = globals()
    saved = {k: g[k] for k in ("bx_get", "http_get", "fetch_history", "emit", "fetch_bars", "fetch_book", "scan_all", "STATE_FILE")}
    try:
        contracts = [
            {"symbol": "BTC-USDT", "status": 1, "pricePrecision": 1, "quantityPrecision": 4, "apiStateOpen": "true"},
            {"symbol": "ETH-USDT", "status": 1, "pricePrecision": 2, "quantityPrecision": 3, "apiStateOpen": "true"},
            {"symbol": "OLD-USDT", "status": 0, "pricePrecision": 2, "quantityPrecision": 1},
            {"symbol": "CLS-USDT", "status": 1, "pricePrecision": 2, "quantityPrecision": 1, "apiStateOpen": "false"},
            {"symbol": "BTC-USD", "status": 1, "pricePrecision": 2, "quantityPrecision": 1},
            {"symbol": "NCSKAAPL2USD-USDT", "status": 1, "pricePrecision": 2, "quantityPrecision": 1},
            {"symbol": "LOW-USDT", "status": 1, "pricePrecision": 4, "quantityPrecision": 0},
        ]
        tickers = [{"symbol": "BTC-USDT", "quoteVolume": "5000000000"}, {"symbol": "ETH-USDT", "volume": "1000", "lastPrice": "3000000"},
                   {"symbol": "NCSKAAPL2USD-USDT", "quoteVolume": "30000000"}, {"symbol": "LOW-USDT", "quoteVolume": "100000"},
                   {"symbol": "OLD-USDT", "quoteVolume": "9e9"}, {"symbol": "CLS-USDT", "quoteVolume": "9e9"}]
        NOWB = int(time.time() * 1000) // BAR_MS * BAR_MS
        store = [{"time": NOWB - (6000 - i) * BAR_MS, "open": "100", "high": "101", "low": "99", "close": "100.5",
                  "volume": "10"} for i in range(6001)]            # la ultima (i=6000) es la vela ABIERTA

        def fake_bx(cfg_, path, params=None):
            params = params or {}
            if path.endswith("/quote/contracts"):
                return contracts
            if path.endswith("/quote/ticker"):
                return tickers
            if path.endswith("/quote/depth"):
                return {"bids": [["99.95", "500"], ["99.9", "800"]], "asks": [["100.05", "500"], ["100.1", "800"]]}
            if path.endswith("/quote/klines"):
                end_ = params.get("endTime", NOWB + BAR_MS)
                rows = [x for x in store if x["time"] <= end_][-int(params.get("limit", 500)):]
                return list(reversed(rows))                         # BingX puede devolverlas en orden descendente
            raise AssertionError(path)
        g["bx_get"] = fake_bx
        cb = Config(min_quote_volume=1e7)
        sy, mt = load_symbols_bingx(cb, [])
        check("BingX contratos: filtra offline/no-USDT/TradFi/poco volumen y ordena por volumen",
              sy == ["BTC-USDT", "ETH-USDT"] and abs(mt["BTC-USDT"]["tick"] - 0.1) < 1e-12
              and abs(mt["ETH-USDT"]["step"] - 0.001) < 1e-12)
        sy2, _ = load_symbols_bingx(replace(cb, include_tradfi=True, exclude_symbols=("ETHUSDT",)), [])
        check("BingX: include_tradfi y EXCLUDE_SYMBOLS en formato BTCUSDT", sy2 == ["BTC-USDT", "NCSKAAPL2USD-USDT"])
        sy3, _ = load_symbols_bingx(cb, ["ethusdt"])
        check("BingX: --symbols acepta ETHUSDT", sy3 == ["ETH-USDT"])
        bars_ = fetch_bars_bingx(cb, "BTC-USDT")
        check("BingX fetch_bars: ascendente y sin la vela abierta (499 pedidas -> 498 cerradas)",
              len(bars_) == 498 and bars_[-1]["t"] == NOWB - BAR_MS and all(a_["t"] < b_["t"] for a_, b_ in zip(bars_, bars_[1:])))
        hist = fetch_history_bingx(cb, "BTC-USDT", 10)
        check("BingX historico paginado hacia atras (10 dias = %d velas)" % len(hist),
              abs(len(hist) - 11 * 288) <= 1 and hist[-1]["t"] == NOWB - BAR_MS
              and len({x["t"] for x in hist}) == len(hist))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok_ = check_exchange(Config(min_quote_volume=1e7))
        check("--check-exchange con BingX simulado: TODO OK", ok_ and "TODO OK" in buf.getvalue())

        # firma HMAC
        seen = {}

        def fake_http(url, params=None, tries=3, headers=None):
            seen.update(url=url, params=dict(params or {}), headers=headers)
            return seen.get("reply", {"code": 0, "data": [1]})
        g["http_get"] = fake_http
        g["bx_get"] = saved["bx_get"]
        os.environ.pop("BINGX_API_KEY", None)
        os.environ.pop("BINGX_API_SECRET", None)
        saved_thr = _thr_next[0]
        bx_get(cb, "/openApi/swap/v2/quote/contracts", {"symbol": "BTC-USDT"})
        check("BingX sin claves: peticion publica con timestamp y sin firma",
              "timestamp" in seen["params"] and "signature" not in seen["params"] and not seen["headers"])
        os.environ.update(BINGX_API_KEY="k123", BINGX_API_SECRET="s456")
        bx_get(cb, "/openApi/swap/v2/quote/contracts", {"symbol": "BTC-USDT"})
        pp = dict(seen["params"])
        sig = pp.pop("signature")
        qs = "&".join("%s=%s" % (n, pp[n]) for n in pp)
        check("BingX con claves: cabecera X-BX-APIKEY y HMAC-SHA256 correcto",
              seen["headers"] == {"X-BX-APIKEY": "k123"}
              and sig == hmac.new(b"s456", qs.encode(), hashlib.sha256).hexdigest() and "timestamp" in pp)
        os.environ.pop("BINGX_API_KEY", None)
        os.environ.pop("BINGX_API_SECRET", None)
        seen["reply"] = {"code": 100001, "msg": "signature mismatch"}
        try:
            bx_get(cb, "/x")
            raised = False
        except RuntimeError as e_:
            raised = "100001" in str(e_)
        check("BingX: codigo de error de la API -> excepcion con el mensaje", raised)
        g["http_get"] = saved["http_get"]

        # 7) Autoajuste: reglas de seleccion
        ct = Config(autotune_min_trades=40, autotune_z=2.0, autotune_min_gain_r=0.1)
        inc = cfg_key(ct)
        other = lambda **kw: cfg_key(replace(ct, **kw))  # noqa: E731
        mk_res = lambda base_, amp, n=60, shift=0: [(i * 33 + shift, base_ + (amp if i % 2 else -amp)) for i in range(n)]  # noqa: E731
        good, good2, bad = other(min_score=4), other(min_score=5), other(min_score=3, rr=3.0)
        unstable, tiny = other(rr=1.5), other(trend_filter=True)
        res_ = {good: mk_res(0.40, 0.10), good2: mk_res(0.15, 0.10), bad: mk_res(-0.2, 0.1),
                unstable: [(i * 33, 1.0 if i < 30 else -0.7) for i in range(60)], tiny: mk_res(0.5, 0.05, n=10), inc: mk_res(0.05, 0.9)}
        stt, elig, best, inc_, dec, edge = tune_select(ct, res_, 1000)
        check("autoajuste: elige la mejor variante validada (descarta negativas, inestables y con pocas operaciones)",
              best == good and set(elig) >= {good, good2} and unstable not in elig and bad not in elig and tiny not in elig)
        check("autoajuste: actual no validada + candidata validada -> cambiar con ventaja", dec == "switch" and edge is True)
        ct2 = replace(ct, min_score=4)           # incumbente == good
        _, _, best2, _, dec2, edge2 = tune_select(ct2, res_, 1000)
        check("autoajuste: si la actual ya es la mejor validada -> mantener", dec2 == "keep" and edge2 is True and best2 == good)
        res_h = dict(res_)
        res_h[cfg_key(replace(ct, min_score=5))] = mk_res(0.46, 0.10)      # algo mejor que 'good', pero < min_gain
        ct3 = replace(ct, min_score=4)
        _, _, best3, _, dec3, _ = tune_select(ct3, res_h, 1000)
        check("autoajuste: mejora menor que AUTOTUNE_MIN_GAIN_R -> histeresis, no cambia", dec3 == "keep")
        res_h[cfg_key(replace(ct, min_score=5))] = mk_res(0.80, 0.10)
        _, _, best4, _, dec4, _ = tune_select(ct3, res_h, 1000)
        check("autoajuste: mejora grande y validada -> cambia", dec4 == "switch" and best4 == cfg_key(replace(ct, min_score=5)))
        _, el0, b0, _, d0, e0 = tune_select(ct, {bad: mk_res(-0.2, 0.1), inc: mk_res(-0.1, 0.5)}, 1000)
        check("autoajuste: ninguna validada -> mantiene y marca edge=False", b0 is None and d0 == "keep" and e0 is False)
        tuned = json.loads(json.dumps(key_dict(good)))
        c4 = Config()
        apply_tuned(c4, tuned)
        check("autoajuste: parametros ajustados sobreviven a JSON (state.json)", cfg_key(c4) == good and isinstance(c4.sessions, tuple))

        # absorcion del resultado (aplicar / solo proponer / error) y aviso en señales
        sent_ = []
        g["emit"] = lambda text, dry, *a_, **k_: sent_.append(text)
        full_res = {"stats": stt, "eligible": elig, "best": good, "inc": inc, "decision": "switch", "edge": True,
                    "n_symbols": 5, "days": 21, "secs": 1.0, "when": time.time()}
        ca = replace(ct, autotune_mode="apply")
        sa = {"edge": None}
        _tune["result"] = dict(full_res)
        absorb_tune(ca, sa)
        check("autoajuste apply: cambia la configuracion, la guarda y avisa por Telegram",
              cfg_key(ca) == good and sa["tuned"]["min_score"] == 4 and sa["edge"] is True and len(sent_) == 1 and "Aplicado" in sent_[0])
        cs = replace(ct, autotune_mode="suggest")
        ss_ = {"edge": None}
        sent_.clear()
        _tune["result"] = dict(full_res)
        absorb_tune(cs, ss_)
        check("autoajuste suggest: propone pero NO cambia la configuracion",
              cfg_key(cs) == inc and "tuned" not in ss_ and len(sent_) == 1 and "no aplicada" in sent_[0])
        sent_.clear()
        _tune["result"] = {"error": "boom"}
        absorb_tune(ca, {"edge": None})
        check("autoajuste: un error del hilo no rompe nada ni envia mensajes", not sent_ and _tune["result"] is None)
        RUNTIME["edge"] = False
        check("señal marcada como experimental cuando no hay ventaja validada",
              "Experimental" in format_message(sample_signal(), Config()))
        RUNTIME["edge"] = True
        check("sin aviso cuando la ventaja esta validada", "Experimental" not in format_message(sample_signal(), Config()))
        RUNTIME["edge"] = None


        # 8) Liquidez real (libro de ordenes) y concentracion
        book = {"bids": [["99.8", "10"], ["99.9", "5"]], "asks": [["100.2", "10"], ["100.1", "5"]]}
        bids_, asks_ = parse_book(book)
        check("parse_book: ordena bids desc y asks asc", bids_[0][0] == 99.9 and asks_[0][0] == 100.1)
        sp_, im_ = book_cost(bids_, asks_, "LONG", 1000.0)
        check("book_cost LONG 1000 USDT: spread 0.20%, impacto 0.15%", abs(sp_ - 0.2) < 1e-9 and abs(im_ - 0.15) < 1e-9)
        sp_, im_ = book_cost(bids_, asks_, "SHORT", 1000.0)
        check("book_cost SHORT 1000 USDT: impacto 0.15%", abs(im_ - 0.15) < 1e-9)
        check("book_cost: libro insuficiente -> impacto None", book_cost(bids_, asks_, "LONG", 5000.0)[1] is None
              and book_cost([], [], "LONG", 10.0) is None)
        sg_ = sample_signal()
        g["fetch_book"] = lambda cfg_, sym_: (bids_, asks_)
        ok1, inf1 = exec_check(Config(account_size=500, max_slip_r=0.20), sg_)      # nocional 1000 USDT
        ok2, _ = exec_check(Config(account_size=500, max_slip_r=1.00), sg_)
        check("exec_check: impacto 0.15%% (%.2fR ida y vuelta) > 0.20R rechaza; con 1.00R acepta" % inf1["cost_r"],
              ok1 is False and ok2 is True and 0.5 < inf1["cost_r"] < 0.7 and abs(inf1["notional"] - 1000.0) < 1e-6)
        g["fetch_book"] = lambda cfg_, sym_: (bids_[:1], asks_[:1])
        ok3, inf3 = exec_check(Config(account_size=500), sg_)
        check("exec_check: libro que no alcanza para tu orden -> rechaza", ok3 is False and inf3["impact"] == float("inf"))

        def boom(cfg_, sym_):
            raise RuntimeError("sin libro")
        g["fetch_book"] = boom
        check("exec_check: sin libro disponible no bloquea (None)", exec_check(Config(account_size=500), sg_) == (None, None))

        # run_once con señales fabricadas: concentracion, liquidez, sin verificar
        import tempfile
        saved_state = g["STATE_FILE"]
        g["STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")
        sent2 = []
        g["emit"] = lambda text, dry, *a_, **k_: (sent2.append(text), {"1": len(sent2)})[1]

        g["fetch_bars"] = lambda cfg_, sym_: []                      # el seguimiento no debe tocar la red

        def fab(n, side="LONG", off=0):
            out = []
            for i in range(n):
                x = dict(sample_signal(), symbol="C%d-USDT" % (i + off), side=side, cycle_start_ms=1000 + i + off,
                         score=5 - (i % 2), rel_vol=1.0 + i / 10.0)
                out.append(x)
            return out
        cq = Config(autotune=False, heartbeat_hours=0, daily_summary_hour=-1, max_slip_r=0.0, max_open_per_side=3,
                    min_score=0, symbol_cooldown_min=0)
        g["scan_all"] = lambda *a_, **k_: fab(7)
        run_once(cq, [], {}, False)
        check("concentracion: maximo 3 señales abiertas por lado (7 candidatas -> 3 enviadas)", len(sent2) == 3)
        sent2.clear()
        g["scan_all"] = lambda *a_, **k_: fab(5, "LONG", 100)
        run_once(cq, [], {}, False)
        check("concentracion: con 3 abiertas no se envian mas LONG, y si SHORT",
              not sent2)
        g["scan_all"] = lambda *a_, **k_: fab(2, "SHORT")
        run_once(cq, [], {}, False)
        check("concentracion: el cupo es por lado (SHORT sigue entrando)", len(sent2) == 2)
        g["STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")
        sent2.clear()
        cl = replace(cq, max_slip_r=0.20, max_open_per_side=0, account_size=1000)
        g["scan_all"] = lambda *a_, **k_: fab(2)
        g["fetch_book"] = lambda cfg_, sym_: (bids_, asks_)
        run_once(cl, [], {}, False)
        check("liquidez: libro caro -> las señales se descartan y no llegan a Telegram", not sent2)
        g["fetch_book"] = boom
        run_once(cl, [], {}, False)
        check("liquidez: libro no disponible -> llega con aviso 'no verificada'",
              len(sent2) == 2 and all("no verificada" in t_ for t_ in sent2))
        g["fetch_book"] = lambda cfg_, sym_: ([(99.99, 1e6)], [(100.01, 1e6)])
        sent2.clear()
        g["STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")
        run_once(cl, [], {}, False)
        check("liquidez: libro profundo -> llega con spread e impacto", len(sent2) == 2 and all("💧 Liquidez: spread" in t_ for t_ in sent2))
        g["STATE_FILE"] = saved_state
        g["scan_all"] = saved["scan_all"]
        g["fetch_bars"] = saved["fetch_bars"]
        g["fetch_book"] = saved["fetch_book"]

        # 9) Filtro BTC en el motor
        base_sigs = scan_series("SYN", syn, 0.0001, loose, only_last=False)
        n_long = sum(1 for x in base_sigs if x["side"] == "LONG")
        n_short = len(base_sigs) - n_long
        lb = replace(loose, btc_filter=True)
        down = {b["t"]: -1 for b in syn}
        up = {b["t"]: 1 for b in syn}
        sd_ = scan_series("SYN", syn, 0.0001, lb, only_last=False, btc=down)
        su_ = scan_series("SYN", syn, 0.0001, lb, only_last=False, btc=up)
        check("filtro BTC: BTC bajista bloquea LONG y deja SHORT (%d/%d)" % (n_long, n_short),
              n_long > 0 and n_short > 0 and not any(x["side"] == "LONG" for x in sd_)
              and sum(1 for x in sd_ if x["side"] == "SHORT") == n_short)
        check("filtro BTC: BTC alcista bloquea SHORT y deja LONG",
              not any(x["side"] == "SHORT" for x in su_) and sum(1 for x in su_ if x["side"] == "LONG") == n_long)
        check("filtro BTC: sin datos de BTC o neutro no bloquea nada",
              len(scan_series("SYN", syn, 0.0001, lb, only_last=False, btc=None)) == len(base_sigs)
              and len(scan_series("SYN", syn, 0.0001, lb, only_last=False, btc={b["t"]: 0 for b in syn})) == len(base_sigs))
        bm = btc_trend_map([dict(b, c=100.0 + i * 0.1) for i, b in enumerate(syn)], 240)
        check("btc_trend_map: serie creciente -> tendencia +1 al final", bm[syn[-1]["t"]] == 1 and bm[syn[10]["t"]] == 0)

        # 10) Muestra rotativa y presupuesto del autoajuste
        names = ["S%03d-USDT" % i for i in range(300)]
        sm1, sm2, sm3 = pick_sample(names, 90, 1), pick_sample(names, 90, 1), pick_sample(names, 90, 2)
        check("pick_sample: n exacto, sin duplicados, 1/3 mas liquidas siempre incluidas, determinista",
              len(sm1) == 90 and len({x[0] for x in sm1}) == 90 and [x[0] for x in sm1[:30]] == names[:30] and sm1 == sm2)
        check("pick_sample: otra semilla -> otra muestra del resto; incluye cola (nivel 2)",
              [x[0] for x in sm1] != [x[0] for x in sm3] and {x[1] for x in sm1} == {0, 1, 2})
        check("pick_sample: pocas monedas -> todas", [x[0] for x in pick_sample(names[:10], 90, 1)] == names[:10])
        r0, n0, _, _ = autotune_collect(Config(), [("AAA-USDT", 0)], {}, 21, None, 0.0)
        check("autoajuste: presupuesto de tiempo agotado -> no procesa monedas", n0 == 0 and r0 == {})
        old_state = json.loads(json.dumps({k_: v_ for k_, v_ in key_dict(good).items() if k_ != "btc_filter"}))
        c5 = Config(btc_filter=True)
        apply_tuned(c5, old_state)
        check("autoajuste: state.json antiguo (sin btc_filter) se aplica sin romper", c5.btc_filter is False and cfg_key(c5)[4] == 4)
        ts_ = tier_stats([(1, 0.5, 0), (2, -0.5, 0), (3, 1.0, 2), (4, 9.9)])
        check("tier_stats: agrupa por nivel y tolera tuplas sin nivel", ts_ == {0: (2, 0.0), 2: (1, 1.0)})
        stt3, el3, b3, _, d3, e3 = tune_select(ct, {good: [(i * 33, 0.4 + (0.1 if i % 2 else -0.1), i % 3) for i in range(60)]}, 1000)
        check("tune_select acepta operaciones con nivel de liquidez", b3 == good and e3 is True)

        # autoajuste de punta a punta con datos sinteticos (sin red)
        import random as _r
        def synth(seed):
            rn = _r.Random(seed)
            pr, tt, out = 100.0, _ms(2026, 7, 13, 18, 0), []
            for _ in range(5 * 288):
                o_ = pr
                c_ = o_ * (1 + rn.gauss(0, 0.0025))
                out.append({"t": tt, "o": o_, "h": max(o_, c_) * (1 + abs(rn.gauss(0, 0.0012))),
                            "l": min(o_, c_) * (1 - abs(rn.gauss(0, 0.0012))), "c": c_, "v": 1000 * (1 + rn.random())})
                pr, tt = c_, tt + BAR_MS
            return out
        g["fetch_history"] = lambda cfg_, sym_, days_: synth(sum(map(ord, sym_)))
        rr_ = autotune_run(Config(), ["AAA-USDT", "BBB-USDT"], {})
        check("autoajuste end-to-end sintetico: %d variantes evaluadas y decision coherente" % len(rr_["stats"]),
              len(rr_["stats"]) > 60 and rr_["decision"] in ("keep", "switch") and isinstance(rr_["edge"], bool)
              and rr_["n_symbols"] == 2)
        g["fetch_bars"] = lambda cfg_, sym_: synth(1)                     # velas de 2026-07: viejas respecto a "ahora"
        fl_ = {}
        check("velas desfasadas (el exchange aun no publico la ultima): no se opera con ellas",
              scan_all(Config(require_chain=False, bias_mode="none", sessions=(0, 1, 2, 3)), ["AAA-USDT"], {}, fl_) == []
              and not fl_)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_tune_report(Config(), rr_)
        check("informe del autoajuste se imprime", "DECISION" in buf.getvalue())
    finally:
        g.update(saved)
        for k_ in ("BINGX_API_KEY", "BINGX_API_SECRET"):
            os.environ.pop(k_, None)
        _tune["result"] = None
        RUNTIME["edge"] = None

    # 5) Variables de entorno
    os.environ.update({"MIN_SCORE": "3", "SESSIONS": "1,2,3", "ALLOW_SHORT": "false", "EXCLUDE_SYMBOLS": "abc,xyz",
                       "ACCOUNT_SIZE": "500"})
    c = apply_env(Config())
    check("apply_env lee todas las variables", c.min_score == 3 and c.sessions == (1, 2, 3) and c.allow_short is False
          and c.exclude_symbols == ("ABC", "XYZ") and c.account_size == 500.0 and validate(c) == [])
    for k in ("MIN_SCORE", "SESSIONS", "ALLOW_SHORT", "EXCLUDE_SYMBOLS", "ACCOUNT_SIZE"):
        os.environ.pop(k, None)

    print("\nRESULTADO:", "TODO OK" if ok else "HAY FALLOS")
    return 0 if ok else 1


# ============================================================================
def check_exchange(cfg):
    """Verifica conexion, formato de datos y frescura. Pensado para ejecutarlo la primera vez en tu entorno."""
    from concurrent.futures import ThreadPoolExecutor as TPE
    ok = True
    print("Exchange: %s | %s | firma: %s" % (cfg.exchange, cfg.base_url,
          "si (BINGX_API_KEY)" if os.environ.get("BINGX_API_KEY") else "no (datos publicos)"))
    try:
        if cfg.exchange == "bingx":
            raw = bx_get(cfg, "/openApi/swap/v2/quote/contracts") or []
            usdt = [c for c in raw if str(c.get("symbol", "")).upper().endswith("-USDT")]
            tf = [c["symbol"] for c in usdt if is_tradfi(str(c["symbol"]).upper())]
            print("Contratos: %d | USDT: %d | TradFi excluidos: %d %s" % (
                len(raw), len(usdt), len(tf), tf[:6] if not cfg.include_tradfi else "(incluidos)"))
        syms, meta = load_symbols(cfg, [])
        print("Monedas que pasan el filtro de volumen (>= %.0f USDT): %d  | primeras: %s" % (
            cfg.min_quote_volume, len(syms), ", ".join(syms[:5])))
        if not syms:
            print("!! 0 monedas: baja MIN_QUOTE_VOLUME"); ok = False
            return ok
        sym = norm_symbol("BTCUSDT", cfg.exchange)
        sym = sym if sym in syms else syms[0]
        t0 = time.time()
        bars = fetch_bars(cfg, sym)
        print("Velas %s: %d en %.2fs | ultima vela: %s UTC | OHLCV %.6g %.6g %.6g %.6g vol~%.0f USDT" % (
            sym, len(bars), time.time() - t0,
            datetime.fromtimestamp(bars[-1]["t"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            bars[-1]["o"], bars[-1]["h"], bars[-1]["l"], bars[-1]["c"], bars[-1]["v"]))
        lag = time.time() - (bars[-1]["t"] + BAR_MS) / 1000.0
        gaps = sum(1 for a, b in zip(bars, bars[1:]) if b["t"] - a["t"] != BAR_MS)
        print("Retraso de la ultima vela cerrada: %.0fs (<= 330s es normal) | huecos en la serie: %d" % (lag, gaps))
        if len(bars) < 300 or lag > 600:
            print("!! pocas velas o datos desfasados"); ok = False
        h = fetch_history(cfg, sym, 2)
        print("Historico 2 dias: %d velas (esperadas ~%d)" % (len(h), 3 * 288))
        if len(h) < 2 * 288:
            print("!! el historico llega corto: el backtest/autoajuste no sera fiable"); ok = False
        picks = sorted({0, len(syms) // 4, len(syms) // 2, (3 * len(syms)) // 4, len(syms) - 1})
        print("Coste REAL de ejecucion (libro de ordenes) para %.0f USDT, por nivel de liquidez:" % DEFAULT_NOTIONAL)
        got_book = 0
        for i in picks:
            try:
                bc = book_cost(*fetch_book(cfg, syms[i]), "LONG", DEFAULT_NOTIONAL)
                got_book += 1
                print("   #%-4d %-14s spread %.3f%% | impacto %s" % (
                    i + 1, syms[i], bc[0], ("%.3f%%" % bc[1]) if bc[1] is not None else "libro insuficiente"))
            except Exception as e:
                print("   #%-4d %-14s libro no disponible (%s)" % (i + 1, syms[i], str(e)[:80]))
        if not got_book:
            print("!! sin libro de ordenes: el filtro de liquidez (MAX_SLIP_R) quedara sin verificar"); ok = False
        sample = syms[:24]
        t0 = time.time()
        with TPE(max_workers=cfg.workers) as ex:
            res = list(ex.map(lambda x: len(fetch_bars(cfg, x)), sample))
        dt = time.time() - t0
        est = dt / max(1, len(sample)) * len(syms)
        print("Escaneo de %d monedas en %.1fs -> estimado para las %d: ~%.0fs (debe ser < 240s)" % (
            len(sample), dt, len(syms), est))
        if est > 240:
            print("!! demasiado lento: sube MIN_QUOTE_VOLUME o fija MAX_SYMBOLS"); ok = False
    except Exception as e:
        print("!! ERROR: %s" % e)
        ok = False
    print("\nRESULTADO:", "TODO OK" if ok else "REVISAR LO MARCADO CON !!")
    return ok



def main():
    ap = argparse.ArgumentParser(description="QT Chain Scanner (BingX/Binance Futures -> Telegram)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--once", action="store_true", help="un solo escaneo y salir")
    ap.add_argument("--dry-run", action="store_true", help="no envia a Telegram, solo imprime")
    ap.add_argument("--check-exchange", action="store_true", help="verifica conexion y formato de datos del exchange")
    ap.add_argument("--autotune", action="store_true", help="ejecuta una vez el autoajuste y muestra el ranking (no aplica)")
    ap.add_argument("--backtest", action="store_true", help="backtest sobre el historico del exchange + embudo de filtros")
    ap.add_argument("--compare", action="store_true", help="con --backtest: prueba variantes de la estrategia")
    ap.add_argument("--days", type=int, default=14, help="dias de historico del backtest (defecto 14)")
    ap.add_argument("--top", type=int, default=40, help="monedas mas liquidas en el backtest (defecto 40, 0 = todas)")
    ap.add_argument("--symbols", default="", help="lista separada por comas, ej. BTC-USDT,ETH-USDT (tambien vale BTCUSDT)")
    ap.add_argument("--min-volume", type=float, default=None, help="volumen 24h minimo en USDT")
    ap.add_argument("--no-chain", action="store_true", help="no exigir cadena abierta")
    ap.add_argument("--bias", choices=["none", "true_opens"], default=None)
    ap.add_argument("--sessions", default=None, help="Q diarios permitidos: 0 Asia,1 Londres,2 NY AM,3 NY PM (ej. 1,2)")
    ap.add_argument("--loglevel", default=os.environ.get("LOG_LEVEL", "INFO"))
    a = ap.parse_args()
    logging.basicConfig(level=a.loglevel.upper(), format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    if a.selftest:
        sys.exit(selftest())
    if requests is None:
        sys.exit("Falta 'requests': pip install requests tzdata")

    cfg = apply_env(Config())
    if a.min_volume is not None:
        cfg.min_quote_volume = a.min_volume
    if a.no_chain:
        cfg.require_chain = False
    if a.bias:
        cfg.bias_mode = a.bias
    if a.sessions:
        cfg.sessions = tuple(int(x) for x in a.sessions.split(",") if x.strip() != "")
    errs = validate(cfg)
    if errs:
        sys.exit("Configuracion invalida:\n- " + "\n- ".join(errs))
    only = [norm_symbol(x, cfg.exchange) for x in a.symbols.split(",") if x.strip()]

    if a.test_telegram:
        send_telegram("✅ QT Scanner conectado. Las señales llegarán a este chat.\nAsí se verá una señal (EJEMPLO, no operar):")
        s = sample_signal()
        send_telegram("🧪 <b>EJEMPLO</b>\n\n" + format_message(s, cfg), buttons=buttons_for(s["symbol"], cfg.exchange))
        print("Mensajes de prueba enviados.")
        return
    if a.check_exchange:
        sys.exit(0 if check_exchange(cfg) else 1)
    if a.autotune:
        symbols, meta = load_symbols(cfg, only)
        print("Autoajuste sobre %d de %d monedas (puede tardar varios minutos)..." % (
            min(len(symbols), cfg.autotune_top) if cfg.autotune_top else len(symbols), len(symbols)))
        print_tune_report(cfg, autotune_run(cfg, symbols, meta))
        return
    if a.backtest:
        run_backtest(cfg, only, max(2, a.days), a.top, a.compare)
        return
    if a.once:
        symbols, meta = load_symbols(cfg, only)
        run_once(cfg, symbols, meta, a.dry_run)
    else:
        loop(cfg, a.dry_run, only)


if __name__ == "__main__":
    main()
