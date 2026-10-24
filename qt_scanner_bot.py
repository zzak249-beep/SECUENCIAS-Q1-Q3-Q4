#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QT Chain Scanner v6  -  escanea TODOS los perpetuos USDT de BingX (y/o Binance) en 5m y envia
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
  - Botones en Telegram: abrir Binance Futures / TradingView
  - Seguimiento: avisa (respondiendo al mensaje original) cuando toca TP1, TP2, SL o caduca
  - Resumen diario con resultados simulados en R
  - Filtros: riesgo maximo, cooldown por moneda, limite diario, monedas excluidas
  - Todas las opciones se pueden cambiar con Variables de Railway (ver README)

Mejoras v5 (diagnostico, validacion con datos y filtros con base en la investigacion):
  - --backtest: reproduce la estrategia sobre historico de Binance (comisiones incluidas) y
    muestra winrate, R medio, profit factor, drawdown y resultados por sesion/score/lado
  - --compare: prueba variantes (sin cadena, sin sesgo, otras sesiones, tendencia 1H, volumen...)
    y las separa en 1a/2a mitad del periodo para detectar sobreajuste
  - Embudo de filtros: cuenta que filtro bloquea mas senales (--backtest y aviso periodico)
  - Aviso "sigo activo" cada HEARTBEAT_HOURS con lo que esta frenando las senales
  - Filtros nuevos: tendencia 1H (TREND_FILTER), volumen relativo (MIN_REL_VOL), comisiones (MAX_FEE_R)
  - Puntuacion 0-7 (suma tendencia 1H a favor y volumen relativo alto)
  - Resultados simulados NETOS de comisiones; caducidad valorada a precio de mercado

Mejoras v6 (estrategia, ejecucion manual y validacion):
  - BingX: todos los perpetuos USDT activos (EXCHANGE=bingx|binance|both), con respaldo de rutas y limitador
  - Costes realistas: comision + deslizamiento por lado en todos los resultados simulados (SLIPPAGE_PCT)
  - Filtros nuevos opcionales (se validan con --backtest --compare antes de activarlos):
      REQUIRE_MSS (la vela de confirmacion rompe la estructura reciente), BTC_FILTER (no ir contra BTC 1H)
  - Control del precio ANTES de avisar (FRESH_CHECK): no envia senales que ya se alejaron de la entrada
  - Limite de exposicion: MAX_OPEN_PER_SIDE / MAX_OPEN_TOTAL (evita 10 shorts correlacionados a la vez)
  - Validacion fuera de muestra en --compare: la variante elegida con la 1a mitad se juzga en la 2a
  - Puntuacion 0-8 (suma ruptura de estructura)

NO incluye (frente al Pine): filtro SMT con 2o activo, True Open Weekly, noticias.

Uso:
    python qt_scanner_bot.py --selftest            # prueba offline de la logica
    python qt_scanner_bot.py --check               # comprueba la conexion con el exchange (monedas y velas)
    python qt_scanner_bot.py --test-telegram       # mensaje de prueba + senal de ejemplo
    python qt_scanner_bot.py --once --dry-run      # un escaneo, solo imprime
    python qt_scanner_bot.py --backtest --days 14  # backtest + embudo de filtros (necesita Binance)
    python qt_scanner_bot.py --backtest --compare  # ademas compara variantes de la estrategia
    python qt_scanner_bot.py                       # bucle continuo cada 5 min
Aviso: no es consejo financiero. Prueba en demo / papel antes de usar dinero real.
"""
import argparse
import json
import logging
import math
import os
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
SCORE_MAX = 8
log = logging.getLogger("qt")


# ============================================================================
#  CONFIGURACION (cada campo se puede sobrescribir con una variable de entorno
#  con el mismo nombre en MAYUSCULAS, p. ej. min_score -> MIN_SCORE)
# ============================================================================
@dataclass
class Config:
    exchange: str = "bingx"                      # "bingx" | "binance" | "both"
    base_url: str = "https://fapi.binance.com"   # env: BINANCE_BASE_URL
    bingx_url: str = "https://open-api.bingx.com"  # env: BINGX_BASE_URL
    bingx_rps: float = 8.0                       # peticiones/segundo a BingX (baja si ves limites)
    include_non_crypto: bool = False             # BingX: incluir perpetuos de acciones/materias primas/forex
    interval: str = "5m"                         # fijo (la logica de ciclos es de 5m)
    klines_limit: int = 499                      # fijo (~41 h de velas de 5m)
    scan_delay_sec: float = 5.0                  # segundos de espera tras el cierre de la vela de 5m
    # volumen 24h minimo en USDT. -1 = automatico (BingX 1M, Binance 20M). 0 = analizar todas
    min_quote_volume: float = -1.0
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
    fee_pct: float = 0.05                        # comision taker por lado en % (Binance base: 0.05)
    max_fee_r: float = 0.35                      # descarta si costes ida y vuelta > X R (0 = off)
    slippage_pct: float = 0.02                   # deslizamiento estimado por lado en % (entrada a mercado)
    require_mss: bool = False                    # la vela de confirmacion rompe el extremo de las 3 velas previas
    btc_filter: bool = False                     # no operar en contra de la tendencia 1H de BTC
    fresh_check: bool = True                     # antes de avisar, comprueba el precio actual
    max_open_per_side: int = 4                   # max señales abiertas (en seguimiento) por lado, 0 = sin limite
    max_open_total: int = 8                      # max señales abiertas en total, 0 = sin limite
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


ENV_ALIASES = {"base_url": "BINANCE_BASE_URL", "bingx_url": "BINGX_BASE_URL"}
ENV_SKIP = {"interval", "klines_limit"}


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
    cfg.base_url = cfg.base_url.rstrip("/")
    cfg.bingx_url = cfg.bingx_url.rstrip("/")
    cfg.exchange = cfg.exchange.strip().lower()
    return cfg


def validate(cfg):
    errs = []
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
    if cfg.fee_pct < 0 or cfg.max_fee_r < 0:
        errs.append("FEE_PCT y MAX_FEE_R no pueden ser negativos")
    if not 20 <= cfg.trend_ema_bars <= 400:
        errs.append("TREND_EMA_BARS debe estar entre 20 y 400")
    if cfg.exchange not in ("bingx", "binance", "both"):
        errs.append("EXCHANGE debe ser bingx, binance o both (es %r)" % cfg.exchange)
    if cfg.bingx_rps <= 0:
        errs.append("BINGX_RPS debe ser > 0")
    if cfg.slippage_pct < 0:
        errs.append("SLIPPAGE_PCT no puede ser negativo")
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
GATES = ("sesion", "cadena", "q1", "barrido", "vela", "mss", "sesgo", "tendencia", "btc", "volumen", "una_por_ciclo")
GATE_TEXT = {
    "sesion": "fuera de las sesiones elegidas",
    "cadena": "sin cadena QT abierta",
    "q1": "rango del Q1 fuera de limites",
    "barrido": "sin barrido valido del Q1",
    "vela": "sin vela de confirmacion fuerte",
    "sesgo": "sesgo True Open en contra",
    "tendencia": "tendencia 1H en contra",
    "mss": "sin ruptura de estructura",
    "btc": "BTC 1H en contra",
    "volumen": "volumen relativo bajo",
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


def trend_series(bars, cfg):
    """Tendencia por vela: +1 alcista (cierre > EMA y EMA subiendo), -1 bajista, 0 neutra/sin datos."""
    ema = ema_series([b["c"] for b in bars], cfg.trend_ema_bars)
    out = [0] * len(bars)
    for i, b in enumerate(bars):
        e = ema[i]
        if e is not None and i >= 12 and ema[i - 12] is not None:
            if b["c"] > e and e > ema[i - 12]:
                out[i] = 1
            elif b["c"] < e and e < ema[i - 12]:
                out[i] = -1
    return out


def prepare_series(bars, cfg):
    """Calculos que no dependen de los filtros (se reutilizan entre variantes del backtest)."""
    qinfos = [quarters(b["t"]) for b in bars]
    return {
        "atr": atr_series(bars),
        "qinfos": qinfos,
        "mins": [q["mins"] for q in qinfos],
        "trend": trend_series(bars, cfg),
        "vols": [b.get("v", 0.0) for b in bars],
    }


def scan_series(symbol, bars, tick, cfg, only_last=True, funnel=None, prep=None, ref=None):
    """Recorre las velas CERRADAS [{t,o,h,l,c,v}]. 'ref' = tendencia 1H de BTC (int en vivo, dict t->tendencia en backtest).
    only_last=True  -> solo senales de la ultima vela (modo en vivo).
    only_last=False -> senales de todas las velas (backtest); cada senal lleva 'bar_i'."""
    if len(bars) < 40:
        return []
    p = prep or prepare_series(bars, cfg)
    atr, qinfos, mins_arr, trends, vols = p["atr"], p["qinfos"], p["mins"], p["trend"], p["vols"]
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

        trend = trends[i]                                      # +1 alcista, -1 bajista, 0 neutra/sin datos
        mss_bear = mss_bull = False                            # ruptura de estructura: cierra mas alla de las 3 velas previas
        if i >= 3:
            mss_bear = b["c"] < min(x["l"] for x in bars[i - 3:i])
            mss_bull = b["c"] > max(x["h"] for x in bars[i - 3:i])
        rt = 0                                                 # tendencia 1H de BTC (0 = neutra o sin datos)
        if ref is not None:
            rt = ref.get(b["t"], 0) if isinstance(ref, dict) else ref
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
            "btc": rt if ref is not None else None,
        }
        for side in ("SHORT", "LONG"):
            if side == "SHORT":
                if not cfg.allow_short:
                    continue
                gates = [("sesion", session_ok), ("cadena", seq_ok), ("q1", q1_ok), ("barrido", sweep_ok_hi),
                         ("vela", strong_bear and b["c"] < q1_hi), ("mss", (not cfg.require_mss) or mss_bear),
                         ("sesgo", bear_bias), ("tendencia", (not cfg.trend_filter) or trend == -1),
                         ("btc", (not cfg.btc_filter) or rt != 1), ("volumen", vol_ok),
                         ("una_por_ciclo", not done_s)]
            else:
                if not cfg.allow_long:
                    continue
                gates = [("sesion", session_ok), ("cadena", seq_ok), ("q1", q1_ok), ("barrido", sweep_ok_lo),
                         ("vela", strong_bull and b["c"] > q1_lo), ("mss", (not cfg.require_mss) or mss_bull),
                         ("sesgo", bull_bias), ("tendencia", (not cfg.trend_filter) or trend == 1),
                         ("btc", (not cfg.btc_filter) or rt != -1), ("volumen", vol_ok),
                         ("una_por_ciclo", not done_l)]
            if not _eval_gates(gates, funnel if count else None):
                continue
            if side == "SHORT":
                done_s = True
                ex = dict(extra, sweep_atr=depth_hi / a, q1_level=q1_hi, trend_ok=(trend == -1), mss=mss_bear)
                sl = c_hi + cfg.sl_buf_atr * a
            else:
                done_l = True
                ex = dict(extra, sweep_atr=depth_lo / a, q1_level=q1_lo, trend_ok=(trend == 1), mss=mss_bull)
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


def analyze(symbol, bars, tick, cfg, funnel=None, ref=None):
    """Modo en vivo: senales de la ultima vela cerrada."""
    return scan_series(symbol, bars, tick, cfg, only_last=True, funnel=funnel, ref=ref)


def score_signal(ex, cfg):
    """Puntuacion 0-8: cadena, cadena profunda, sesgo True Open, barrido limpio, vela fuerte,
    tendencia 1H a favor, volumen relativo alto y ruptura de estructura."""
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
    if ex.get("mss"):
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
    fee_r = 2.0 * (cfg.fee_pct + cfg.slippage_pct) / risk_pct                      # comisiones ida y vuelta expresadas en R
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
        "mss": bool(ex.get("mss")),
        "btc": ex.get("btc"),
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
    if qty <= 0 or qty < (s.get("min_qty") or 0.0):
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
        "%s <b>%s · %s</b> · %s   %s %d/%d" % (icon, s["side"], s["symbol"],
                                               EXCHANGES[s.get("exchange", "bingx")].label, stars, s["score"], SCORE_MAX),
        "",
        "📍 Entrada: <code>%s</code>  (mercado o limit)" % fp(s["entry"]),
        "     No entres %s de <code>%s</code>" % ("por encima" if is_long else "por debajo", fp(s["max_entry"])),
        "🛑 SL: <code>%s</code>  (-%.2f%%)" % (fp(s["sl"]), pct(s["sl"])),
        "🎯 TP1: <code>%s</code>  (+%.2f%% · %.1fR)  → cierra %.0f%% y SL a entrada" % (
            fp(s["tp1"]), pct(s["tp1"]), cfg.partial_r, pf),
        "🏁 TP2: <code>%s</code>  (+%.2f%% · %.1fR)  → cierra el resto" % (fp(s["tp2"]), pct(s["tp2"]), cfg.rr),
    ]
    if s.get("price_now"):
        lines.insert(4, "     Precio ahora: <code>%s</code>" % fp(s["price_now"]))
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
    if s.get("btc") is not None:
        lines.append("₿ BTC 1H: %s" % {1: "alcista", -1: "bajista", 0: "neutra"}.get(s["btc"], "neutra"))
    if s.get("mss"):
        lines.append("🔨 Ruptura de estructura confirmada (rompe el extremo de las 3 velas previas)")
    if s.get("fee_r") is not None:
        lines.append("💸 Costes ida y vuelta ≈ %.2fR (comision %.2f%% + deslizamiento %.2f%% por lado)" % (
            s["fee_r"], cfg.fee_pct, cfg.slippage_pct))
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


def buttons_for(s):
    return EXCHANGES[s.get("exchange", "bingx")].buttons(s["symbol"])


# ============================================================================
#  EXCHANGES (BingX / Binance) + TELEGRAM
# ============================================================================
def norm_sym(x):
    """BTC-USDT, BTCUSDT, btc/usdt -> BTCUSDT"""
    return str(x).upper().replace("-", "").replace("_", "").replace("/", "")


def _to_int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def http_get(url, params=None, tries=3):
    for k in range(tries):
        try:
            r = requests.get(url, params=params or {}, timeout=10)
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


def _row_to_bar(r):
    return {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
            "v": float(r[7])}                              # v = volumen en USDT


class BinanceAPI:
    """Binance USDT-M Futures (API publica, sin clave)."""
    name = "binance"
    label = "Binance"
    btc_symbol = "BTCUSDT"

    @staticmethod
    def load_symbols(cfg, only=None):
        """Devuelve (symbols, meta) con meta[symbol] = {"tick", "step", "min_qty"}."""
        info = http_get(cfg.base_url + "/fapi/v1/exchangeInfo")
        meta, syms = {}, []
        for s_ in info["symbols"]:
            if s_.get("contractType") == "PERPETUAL" and s_.get("status") == "TRADING" and s_.get("quoteAsset") == "USDT":
                m = {"tick": 0.0001, "step": 0.0, "min_qty": 0.0}
                for f in s_.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        m["tick"] = float(f["tickSize"])
                    elif f.get("filterType") == "LOT_SIZE":
                        m["step"] = float(f["stepSize"])
                        m["min_qty"] = float(f.get("minQty", 0))
                meta[s_["symbol"]] = m
                syms.append(s_["symbol"])
        vols = {x["symbol"]: float(x["quoteVolume"]) for x in http_get(cfg.base_url + "/fapi/v1/ticker/24hr")}
        minv = cfg.min_quote_volume if cfg.min_quote_volume >= 0 else 20_000_000.0
        excl = {norm_sym(x) for x in cfg.exclude_symbols}
        if only:
            only_n = {norm_sym(x) for x in only}
            syms = [x for x in syms if norm_sym(x) in only_n]
        else:
            syms = [x for x in syms if vols.get(x, 0.0) >= minv and norm_sym(x) not in excl]
        syms.sort(key=lambda x: -vols.get(x, 0.0))
        if cfg.max_symbols:
            syms = syms[:cfg.max_symbols]
        return syms, meta

    @staticmethod
    def fetch_bars(cfg, symbol):
        rows = http_get(cfg.base_url + "/fapi/v1/klines",
                        {"symbol": symbol, "interval": cfg.interval, "limit": cfg.klines_limit})
        now_ms = int(time.time() * 1000)
        return [_row_to_bar(r) for r in rows if int(r[6]) < now_ms]   # solo velas cerradas

    @staticmethod
    def fetch_history(cfg, symbol, days):
        return binance_history(cfg, symbol, days)

    @staticmethod
    def last_price(cfg, symbol):
        return float(http_get(cfg.base_url + "/fapi/v1/ticker/price", {"symbol": symbol})["price"])

    @staticmethod
    def buttons(symbol):
        return [
            {"text": "📈 Binance Futures", "url": "https://www.binance.com/en/futures/%s" % symbol},
            {"text": "📊 TradingView", "url": "https://www.tradingview.com/chart/?symbol=BINANCE%%3A%s.P" % symbol},
        ]


# ---------------------------------------------------------------- BingX (swap USDT-M, API publica)
class RateLimiter:
    """Reparte las peticiones de todos los hilos a 'rps' por segundo."""

    def __init__(self, rps):
        self.interval = 1.0 / max(rps, 0.1)
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.interval
        if t > now:
            time.sleep(t - now)


_BINGX_LIMITER = None
_BINGX_LOCK = threading.Lock()
_BINGX_KLINE_PATHS = ["/openApi/swap/v3/quote/klines", "/openApi/swap/v2/quote/klines"]
_BINGX_KLINE_OK = None          # ruta de klines que ha funcionado (se recuerda tras el primer acierto)


def bingx_limiter(cfg):
    global _BINGX_LIMITER
    with _BINGX_LOCK:
        if _BINGX_LIMITER is None:
            _BINGX_LIMITER = RateLimiter(cfg.bingx_rps)
        return _BINGX_LIMITER


def bingx_get(cfg, path, params=None, tries=4):
    """GET publico a BingX. Devuelve el campo 'data'. Reintenta ante limites o fallos de red,
    y usa el dominio .pro como respaldo del .com."""
    lim = bingx_limiter(cfg)
    bases = [cfg.bingx_url]
    if cfg.bingx_url == "https://open-api.bingx.com":
        bases.append("https://open-api.bingx.pro")
    last = "sin respuesta"
    for k in range(tries):
        for base in bases:
            lim.wait()
            q = dict(params or {})
            q["timestamp"] = int(time.time() * 1000)
            try:
                r = requests.get(base + path, params=q, timeout=15)
            except requests.RequestException as e:
                last = str(e)[:150]
                continue
            if r.status_code in (403, 451):
                raise RuntimeError("BingX HTTP %d: la region del servidor esta bloqueada (403/451)" % r.status_code)
            if r.status_code in (418, 429):
                last = "HTTP %d (limite de peticiones)" % r.status_code
                time.sleep(min(2.0 * (k + 1), 20))
                break
            try:
                js = r.json()
            except ValueError:
                last = "respuesta no JSON (HTTP %d)" % r.status_code
                continue
            try:
                code = int(js.get("code", 0))
            except (TypeError, ValueError, AttributeError):
                code = -1
            if code == 0:
                return js.get("data")
            msg = str(js.get("msg", ""))
            low = msg.lower()
            if code == 100410 or "rate limit" in low or "too many" in low or "frequen" in low:
                last = "limite de peticiones (%s)" % msg[:80]
                time.sleep(min(2.0 * (k + 1), 20))
                break
            raise RuntimeError("BingX error %s: %s" % (code, msg[:200]))
        else:                       # ningun dominio respondio bien: espera y reintenta
            time.sleep(1 + k)
    raise RuntimeError("BingX: %s" % last)


def parse_bingx_klines(rows, now_ms):
    """Acepta velas como dict (open/high/low/close/volume/time) o como lista [t,o,h,l,c,v,...].
    BingX puede devolverlas de la mas nueva a la mas antigua e incluye la vela en curso:
    se ordenan y solo se devuelven las cerradas (t + 5 min <= now_ms)."""
    out = {}
    for r in rows or []:
        try:
            if isinstance(r, dict):
                t = int(r.get("time", r.get("openTime", r.get("t"))))
                o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
                v = _to_float(r.get("volume"))
            else:
                t = int(r[0])
                o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                v = _to_float(r[5]) if len(r) > 5 else 0.0
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if t + BAR_MS > now_ms:     # vela todavia abierta
            continue
        out[t] = {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}
    return [out[t] for t in sorted(out)]


def parse_bingx_contracts(contracts, tickers, cfg, only=None):
    """Filtra los contratos USDT activos y devuelve (symbols, meta) ordenados por volumen."""
    vols = {}
    for t in tickers or []:
        try:
            vols[t["symbol"]] = _to_float(t.get("quoteVolume"))
        except (KeyError, AttributeError, TypeError):
            continue
    minv = cfg.min_quote_volume if cfg.min_quote_volume >= 0 else 1_000_000.0
    excl = {norm_sym(x) for x in cfg.exclude_symbols}
    only_n = {norm_sym(x) for x in only} if only else None
    use_vol = bool(vols)
    if not use_vol and minv > 0 and only_n is None:
        log.warning("BingX: sin datos de volumen 24h; se ignora MIN_QUOTE_VOLUME")
    syms, meta = [], {}
    for c in contracts or []:
        sym = str(c.get("symbol", ""))
        if not sym.endswith("-USDT") or str(c.get("status", "1")) != "1":
            continue
        if not cfg.include_non_crypto and sym.startswith("NC") and "2USD" in sym:
            continue                # acciones / materias primas / forex tokenizados
        n = norm_sym(sym)
        if n in excl:
            continue
        if only_n is not None:
            if n not in only_n:
                continue
        elif use_vol and vols.get(sym, 0.0) < minv:
            continue
        pp = _to_int(c.get("pricePrecision"))
        qp = _to_int(c.get("quantityPrecision"))
        meta[sym] = {
            "tick": 10.0 ** (-pp) if pp is not None and pp >= 0 else 0.0001,
            "step": 10.0 ** (-qp) if qp is not None and qp >= 0 else 0.0,
            "min_qty": _to_float(c.get("tradeMinQuantity")),
        }
        syms.append(sym)
    syms.sort(key=lambda x: -vols.get(x, 0.0))
    if cfg.max_symbols:
        syms = syms[:cfg.max_symbols]
    return syms, meta


def _bingx_klines(cfg, symbol, interval, limit, extra=None):
    """Pide velas probando la ruta v3 y, si no funciona, la v2. Devuelve las filas crudas."""
    global _BINGX_KLINE_OK
    paths = [_BINGX_KLINE_OK] if _BINGX_KLINE_OK else list(_BINGX_KLINE_PATHS)
    last_err = None
    for path in paths:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        params.update(extra or {})
        try:
            rows = bingx_get(cfg, path, params)
        except RuntimeError as e:
            if "403/451" in str(e):
                raise
            last_err = e
            continue
        if rows:
            _BINGX_KLINE_OK = path
            return rows
        last_err = RuntimeError("BingX: sin velas para %s en %s" % (symbol, path))
    raise last_err or RuntimeError("BingX: no se pudieron leer velas de %s" % symbol)


class BingXAPI:
    """BingX perpetuos USDT-M (API publica, sin clave). Simbolos tipo BTC-USDT."""
    name = "bingx"
    label = "BingX"
    btc_symbol = "BTC-USDT"

    @staticmethod
    def load_symbols(cfg, only=None):
        contracts = bingx_get(cfg, "/openApi/swap/v2/quote/contracts")
        try:
            tickers = bingx_get(cfg, "/openApi/swap/v2/quote/ticker")
        except RuntimeError as e:
            log.warning("BingX: no se pudo leer el volumen 24h (%s)", e)
            tickers = []
        if isinstance(tickers, dict):
            tickers = [tickers]
        return parse_bingx_contracts(contracts, tickers, cfg, only)

    @staticmethod
    def fetch_bars(cfg, symbol):
        rows = _bingx_klines(cfg, symbol, cfg.interval, cfg.klines_limit)
        bars = parse_bingx_klines(rows, int(time.time() * 1000))
        if not bars:
            raise RuntimeError("BingX: sin velas cerradas para %s" % symbol)
        return bars

    @staticmethod
    def fetch_history(cfg, symbol, days):
        """Historico de 5m en ventanas de <=1440 velas (asi el orden/limite de la respuesta no importa)."""
        end = int(time.time() * 1000)
        cur = end - int((days + 1) * 86_400_000)
        out = {}
        while cur < end:
            win_end = min(cur + 1400 * BAR_MS, end)
            try:
                rows = _bingx_klines(cfg, symbol, cfg.interval, 1440, {"startTime": cur, "endTime": win_end})
            except RuntimeError as e:
                if "403/451" in str(e):
                    raise
                rows = []
            for b in parse_bingx_klines(rows, end):
                out[b["t"]] = b
            cur = win_end
        return [out[t] for t in sorted(out)]

    @staticmethod
    def last_price(cfg, symbol):
        """Ultimo precio = cierre de la vela de 1m en curso."""
        rows = _bingx_klines(cfg, symbol, "1m", 2)
        bars = parse_bingx_klines(rows, 10 ** 18)           # incluye la vela abierta
        if not bars:
            raise RuntimeError("BingX: sin precio para %s" % symbol)
        return bars[-1]["c"]

    @staticmethod
    def buttons(symbol):
        return [
            {"text": "📈 BingX", "url": "https://bingx.com/en/perpetual/%s" % symbol},
            {"text": "📊 TradingView", "url": "https://www.tradingview.com/chart/?symbol=BINGX%%3A%s.P" % norm_sym(symbol)},
        ]


EXCHANGES = {"bingx": BingXAPI, "binance": BinanceAPI}


def active_exchanges(cfg):
    return ["bingx", "binance"] if cfg.exchange == "both" else [cfg.exchange]


def scan_all(cfg, ex, symbols, meta, funnel=None, btc=None):
    """Descarga y analiza las monedas de un exchange. 'btc' = tendencia 1H de BTC (None = sin dato)."""
    found = []
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futs = {pool.submit(ex.fetch_bars, cfg, x): x for x in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                m = meta.get(sym, {})
                ref = None if norm_sym(sym) == "BTCUSDT" else btc
                for sg in analyze(sym, f.result(), m.get("tick", 0.0001), cfg, funnel, ref=ref):
                    sg["exchange"] = ex.name
                    sg["step"] = m.get("step", 0.0)
                    sg["min_qty"] = m.get("min_qty", 0.0)
                    found.append(sg)
            except Exception as e:  # una moneda con error no debe parar el escaneo
                log.warning("%s %s: %s", ex.label, sym, e)
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
            "funnel": {}, "last_hb": 0.0, "hb_n": 0}
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
              "fee_r": s.get("fee_r", 0.0), "last_c": s["entry"], "exchange": s.get("exchange", "binance")})
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
            bars = EXCHANGES[sig.get("exchange", "binance")].fetch_bars(cfg, sig["symbol"])
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
def stale_reason(s, price):
    """None si la senal sigue vigente al precio actual; texto si ya no conviene entrar."""
    sgn = 1 if s["side"] == "LONG" else -1
    if (price - s["max_entry"]) * sgn > 0:
        return "el precio ya se alejo de la entrada (%.6g vs limite %.6g)" % (price, s["max_entry"])
    if (price - s["sl"]) * sgn <= 0:
        return "el precio ya toco el SL (%.6g)" % s["sl"]
    return None


def open_blocked(st, side, cfg):
    """Limite de exposicion: devuelve el motivo si ya hay demasiadas senales abiertas."""
    act = [a for a in st["active"].values() if a.get("status") != "closed"]
    if cfg.max_open_total and len(act) >= cfg.max_open_total:
        return "ya hay %d señales abiertas (MAX_OPEN_TOTAL)" % len(act)
    n = sum(1 for a in act if a["side"] == side)
    if cfg.max_open_per_side and n >= cfg.max_open_per_side:
        return "ya hay %d %s abiertas (MAX_OPEN_PER_SIDE)" % (n, side)
    return None


def btc_trend_now(cfg, ex):
    """Tendencia 1H de BTC en este exchange (+1/-1/0), o None si no se pudo calcular."""
    try:
        return trend_series(ex.fetch_bars(cfg, ex.btc_symbol), cfg)[-1]
    except Exception as e:
        log.warning("filtro BTC desactivado en este escaneo (%s)", e)
        return None


def run_once(cfg, markets, dry_run):
    st = load_state()
    if cfg.track_signals and not dry_run:
        track_active(cfg, st)
    funnel = {}
    t0 = time.time()
    sigs, total = [], 0
    for exname, (symbols, meta) in markets.items():
        ex = EXCHANGES[exname]
        total += len(symbols)
        btc = btc_trend_now(cfg, ex) if cfg.btc_filter else None
        sigs += scan_all(cfg, ex, symbols, meta, funnel, btc)
    dur = time.time() - t0
    if dur > 240:
        log.warning("el escaneo tardo %.0f s (casi un ciclo de 5 min): sube BINGX_RPS/WORKERS, "
                    "baja MAX_SYMBOLS o sube MIN_QUOTE_VOLUME", dur)
    sigs = [x for x in sigs if x["score"] >= cfg.min_score
            and (cfg.max_risk_pct <= 0 or x["risk_pct"] <= cfg.max_risk_pct)]
    sigs.sort(key=lambda x: (-x["score"], x["risk_pct"]))

    now = time.time()
    today = today_ny()
    if st["daily"].get("date") != today:
        st["daily"] = {"date": today, "n": 0}
    n_sent = n_stale = n_blocked = 0
    for s in sigs:
        key = "%s|%s|%s|%s" % (s["exchange"], s["symbol"], s["cycle_start_ms"], s["side"])
        coin = norm_sym(s["symbol"])                       # misma moneda en distintos exchanges = 1 cooldown
        if key in st["sent"]:
            continue
        if n_sent >= cfg.max_signals_per_scan:
            break
        if cfg.max_signals_per_day and st["daily"]["n"] >= cfg.max_signals_per_day:
            log.info("limite diario de senales alcanzado (%d)", cfg.max_signals_per_day)
            break
        if now - st["last_symbol"].get(coin, 0) < cfg.symbol_cooldown_min * 60:
            continue
        if cfg.track_signals:
            why = open_blocked(st, s["side"], cfg)
            if why:
                n_blocked += 1
                log.info("no se avisa %s %s: %s", s["side"], s["symbol"], why)
                continue
        if cfg.fresh_check:
            try:
                price = EXCHANGES[s["exchange"]].last_price(cfg, s["symbol"])
            except Exception as e:
                price = None
                log.debug("sin precio actual de %s: %s", s["symbol"], e)
            if price:
                reason = stale_reason(s, price)
                if reason:
                    n_stale += 1
                    log.info("descartada %s %s: %s", s["side"], s["symbol"], reason)
                    continue
                s["price_now"] = price
        log.info("SEÑAL %s %s %s (%d/%d)", s["side"], s["symbol"], s["exchange"], s["score"], SCORE_MAX)
        ids = emit(format_message(s, cfg), dry_run, buttons=buttons_for(s))
        if ids is None:
            continue
        n_sent += 1
        if dry_run:
            continue
        st["sent"][key] = now
        st["last_symbol"][coin] = now
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
        maybe_heartbeat(cfg, st, total)
        save_state(st)
    log.info("escaneo terminado: %d monedas en %.0f s, %d señales nuevas (%d vencidas por precio, %d por exposicion), "
             "%d en seguimiento", total, dur, n_sent, n_stale, n_blocked, len(st["active"]))


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
    msg = ["💓 <b>QT Scanner activo</b>",
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


def start_text(cfg, markets):
    acct = ("cuenta %.0f USDT, riesgo %.1f%%" % (cfg.account_size, cfg.risk_per_trade_pct)
            if cfg.account_size > 0 else "calculadora de posicion desactivada (falta ACCOUNT_SIZE)")
    exs = ", ".join("%s (%d monedas)" % (EXCHANGES[n].label, len(v[0])) for n, v in markets.items())
    filt = [t for t, on in (("MSS", cfg.require_mss), ("BTC 1H", cfg.btc_filter), ("tendencia 1H", cfg.trend_filter)) if on]
    return ("🚀 <b>QT Scanner iniciado</b>\n"
            "Exchange: %s\n"
            "Sesiones: %s\n"
            "Sesgo: %s | cadena: %s | puntuacion minima: %d/%d\n"
            "Filtros extra: %s\n"
            "RR %.1f | TP1 %.1fR (%.0f%%) | riesgo max SL %.1f%%\n"
            "%s\n"
            "Seguimiento: %s | resumen diario: %s") % (
        exs, ", ".join(SESSION_NAMES[i] for i in cfg.sessions), cfg.bias_mode,
        "obligatoria" if cfg.require_chain else "opcional", cfg.min_score, SCORE_MAX,
        ", ".join(filt) or "ninguno", cfg.rr, cfg.partial_r, cfg.partial_pct, cfg.max_risk_pct, acct,
        "on" if cfg.track_signals else "off",
        ("%d:00 NY" % cfg.daily_summary_hour) if cfg.daily_summary_hour >= 0 else "off")


def load_markets(cfg, only, dry_run, old=None):
    """Carga la lista de monedas de cada exchange activo. Si uno falla, conserva la lista anterior."""
    markets = {}
    for name in active_exchanges(cfg):
        ex = EXCHANGES[name]
        try:
            symbols, meta = ex.load_symbols(cfg, only)
            markets[name] = (symbols, meta)
            log.info("%s: %d monedas a escanear", ex.label, len(symbols))
        except Exception as e:
            log.error("%s: no se pudo cargar la lista de monedas: %s", ex.label, e)
            if old and name in old:
                markets[name] = old[name]
            hint = ""
            if "451" in str(e) or "403" in str(e):
                hint = "\nEl exchange bloquea la region del servidor: cambia la region en Railway (p. ej. Europa)."
            notify("load_" + name, "⚠️ QT Scanner: no se pudo cargar %s: %s%s" % (ex.label, str(e)[:200], hint), dry_run)
    if not markets:
        raise RuntimeError("no se pudo cargar ningun exchange")
    return markets


def loop(cfg, dry_run, only):
    markets, last_load = {}, 0
    announced = False
    while True:
        try:
            if not markets or time.time() - last_load > 1800:      # refresca la lista cada 30 min
                markets = load_markets(cfg, only, dry_run, markets)
                last_load = time.time()
                if not announced:
                    announced = True
                    notify("start", start_text(cfg, markets), dry_run, every=0)
            run_once(cfg, markets, dry_run)
        except Exception as e:
            log.error("error en el escaneo: %s", e)
            hint = ""
            if "451" in str(e) or "403" in str(e):
                hint = "\nEl exchange bloquea la region del servidor: cambia la region en Railway (p. ej. Europa)."
            notify("scan_error", "⚠️ QT Scanner: error en el escaneo: %s%s" % (str(e)[:200], hint), dry_run)
        now = time.time()
        nxt = (int(now // 300) + 1) * 300 + cfg.scan_delay_sec     # justo despues del cierre de la vela de 5m
        time.sleep(max(1, nxt - now))


def check_exchange(cfg, only):
    """Diagnostico: --check. Comprueba monedas, precision, velas y precio actual de cada exchange activo."""
    for name in active_exchanges(cfg):
        ex = EXCHANGES[name]
        print("== %s ==" % ex.label)
        symbols, meta = ex.load_symbols(cfg, only)
        print("monedas que pasan los filtros: %d" % len(symbols))
        for sym in symbols[:5]:
            m = meta[sym]
            print("  %-22s tick=%s step=%s min_qty=%s" % (sym, m["tick"], m["step"], m.get("min_qty")))
        if not symbols:
            continue
        t0 = time.time()
        bars = ex.fetch_bars(cfg, symbols[0])
        print("%s: %d velas cerradas de 5m en %.1f s" % (symbols[0], len(bars), time.time() - t0))
        for b in bars[-3:]:
            print("   %s  o=%s h=%s l=%s c=%s v=%s" % (
                datetime.fromtimestamp(b["t"] / 1000.0, tz=NY).strftime("%Y-%m-%d %H:%M NY"),
                b["o"], b["h"], b["l"], b["c"], b.get("v")))
        age = time.time() - (bars[-1]["t"] + BAR_MS) / 1000.0
        print("la ultima vela cerro hace %.0f s (debe ser < 300 s)" % age)
        try:
            print("precio actual de %s: %s" % (symbols[0], ex.last_price(cfg, symbols[0])))
        except Exception as e:
            print("precio actual: FALLO (%s) -> FRESH_CHECK se omitira" % e)
        if name == "bingx":
            print("tiempo estimado de un escaneo completo: ~%.0f s (%d monedas a %.1f peticiones/s)" % (
                len(symbols) / cfg.bingx_rps, len(symbols), cfg.bingx_rps))


# ============================================================================
#  BACKTEST (historico de Binance, comisiones incluidas)
# ============================================================================
def binance_history(cfg, symbol, days):
    """Velas de 5m cerradas de los ultimos 'days' dias (+1 dia de calentamiento para la EMA)."""
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
    fee_r = 2.0 * (cfg.fee_pct + cfg.slippage_pct) / (risk / s["entry"] * 100.0)
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


def backtest_variant(cfg, data, meta, preps, warm_t, funnel=None, ref_map=None):
    """Genera los trades simulados de una configuracion sobre el historico descargado."""
    trades = []
    horizon = int(cfg.track_hours * 12)
    for sym, bars in data.items():
        tick = meta.get(sym, {}).get("tick", 0.0001)
        ref = None if norm_sym(sym) == "BTCUSDT" else ref_map
        sigs = scan_series(sym, bars, tick, cfg, only_last=False, funnel=funnel, prep=preps[sym], ref=ref)
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
    ("sin cadena + ruptura de estructura (MSS)", {"require_chain": False, "require_mss": True}),
    ("sin cadena + filtro BTC 1H", {"require_chain": False, "btc_filter": True}),
    ("sin cadena + MSS + BTC", {"require_chain": False, "require_mss": True, "btc_filter": True}),
    ("sin cadena + MSS + tendencia 1H", {"require_chain": False, "require_mss": True, "trend_filter": True}),
    ("sin cadena + MSS + BTC + tendencia + vol 1.5",
     {"require_chain": False, "require_mss": True, "btc_filter": True, "trend_filter": True, "min_rel_vol": 1.5}),
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


def walk_forward_text(rows, mid_t, min_n=30):
    """Validacion fuera de muestra: elige la variante con mejor limite inferior del IC en la 1a mitad
    y muestra como le fue en la 2a mitad, que no se uso para elegirla."""
    best = None
    for name, tr in rows:
        s1 = trade_stats([t["r"] for t in tr if t["t"] < mid_t])
        if s1 is None or s1["n"] < min_n:
            continue
        score = s1["avg"] - s1["ci"]
        if best is None or score > best[0]:
            best = (score, name, s1, tr)
    if best is None:
        return None
    _, name, s1, tr = best
    s2 = trade_stats([t["r"] for t in tr if t["t"] >= mid_t])
    head = "VALIDACION FUERA DE MUESTRA\n  Mejor variante segun la 1a mitad: %s (n=%d, R neto %+.2f)\n" % (
        name, s1["n"], s1["avg"])
    if s2 is None:
        return head + "  La 2a mitad no tiene senales: no se puede validar."
    verdict = ("se mantiene positiva (aun asi, prueba unos dias en demo)" if s2["avg"] > 0
               else "NO se mantiene (R neto <= 0): lo mas probable es que fuese azar")
    return head + "  Resultado en la 2a mitad, no usada para elegirla: n=%d, R neto %+.2f ±%.2f -> %s" % (
        s2["n"], s2["avg"], s2["ci"], verdict)


def run_backtest(cfg, only, days, top, compare):
    ex = EXCHANGES["bingx" if cfg.exchange == "both" else cfg.exchange]
    symbols, meta = ex.load_symbols(cfg, only)
    if top:
        symbols = symbols[:top]
    btc = ex.btc_symbol
    to_get = list(symbols) + ([btc] if btc not in symbols else [])    # BTC tambien como referencia del filtro BTC
    print("[%s] Descargando %d dias de velas 5m de %d monedas (puede tardar 1-3 min)..." % (
        ex.label, days, len(to_get)))
    data = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(ex.fetch_history, cfg, x, days): x for x in to_get}
        for f in as_completed(futs):
            try:
                bars = f.result()
            except Exception as e:
                log.warning("%s: %s", futs[f], e)
                continue
            if len(bars) > 300:
                data[futs[f]] = bars
    scan_data = {x: v for x, v in data.items() if x in symbols}
    if not scan_data:
        sys.exit("No se pudo descargar historico (revisa la region / la URL del exchange).")
    ref_map = None
    if btc in data:
        ref_map = {b["t"]: tr for b, tr in zip(data[btc], trend_series(data[btc], cfg))}
    elif cfg.btc_filter or compare:
        print("AVISO: no hay historico de %s: las variantes con filtro BTC se ejecutan sin filtro." % btc)
    t_end = max(b[-1]["t"] for b in scan_data.values())
    warm_t = t_end - days * 86_400_000
    mid_t = warm_t + (t_end - warm_t) // 2
    preps = {sym: prepare_series(bars, cfg) for sym, bars in scan_data.items()}
    print("%d monedas con datos | periodo evaluado: %d dias | costes por lado: comision %.3f%% + deslizamiento %.3f%%\n" % (
        len(scan_data), days, cfg.fee_pct, cfg.slippage_pct))

    funnel = {}
    base = backtest_variant(cfg, scan_data, meta, preps, warm_t, funnel, ref_map)
    print("EMBUDO DE FILTROS (tu configuracion)")
    print(funnel_text(funnel, top=6), "\n")

    print("RESULTADOS (R neto de costes; R = tu riesgo por trade)")
    print(ROW_HEAD)
    rows = [("tu configuracion", base)]
    if compare:
        for name, kw in VARIANTS[1:]:
            rows.append((name, backtest_variant(replace(cfg, **kw), scan_data, meta, preps, warm_t, None, ref_map)))
    for name, tr in rows:
        print(_row(name, tr, mid_t))
    if compare:
        wf = walk_forward_text(rows, mid_t)
        print("\n" + (wf or "VALIDACION FUERA DE MUESTRA: ninguna variante tiene n >= 30 en la 1a mitad (usa mas --days o --top)."))

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
        print("\nFrecuencia: %.2f senales/dia en %d monedas" % (len(base) / float(days), len(scan_data)))

    print("\nLECTURA: una variante solo merece confianza si (1) n >= 100-200, (2) el R neto es positivo en la 1a y en la\n"
          "2a mitad, y (3) el intervalo ±95%% no incluye 0. Con %d variantes probadas, alguna saldra positiva por azar:\n"
          "usa la VALIDACION FUERA DE MUESTRA y confirma la elegida en demo/papel unos dias antes de usar dinero real.\n"
          "Limites: entrada al cierre de la vela (el deslizamiento se aproxima con SLIPPAGE_PCT), SL primero si SL y TP caen\n"
          "en la misma vela, y no modela FRESH_CHECK ni los limites de exposicion del modo en vivo." % len(rows))


# ============================================================================
#  AUTOTEST OFFLINE
# ============================================================================
def _ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC).timestamp() * 1000)


def sample_signal():
    """Senal de ejemplo para --test-telegram."""
    return {"exchange": "bingx", "min_qty": 0.0001, "mss": True, "btc": 1, "price_now": 100020.0,
            "symbol": "BTC-USDT", "side": "LONG", "entry": 100000.0, "sl": 99500.0, "tp1": 100500.0,
            "tp2": 101000.0, "max_entry": 100150.0, "risk_pct": 0.5, "chain": "Prof 4 | Daily a Nano | Q2",
            "session": "NY AM", "time_ny": datetime.now(NY).strftime("%Y-%m-%d %H:%M"), "cycle_start_ms": 0,
            "entry_t": 0, "tick": 0.1, "step": 0.001, "score": 4, "q1_level": 99650.0,
            "bias_levels": [("TO Daily", 100300.0), ("TO Sesión", 100200.0)], "bias_rel": "bajo",
            "fee_r": 0.2, "rel_vol": 1.8, "trend_ok": True}


def selftest():
    global requests, _BINGX_LIMITER
    import types
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
    check("costes: SL 0.5% pasa y fee_r = 0.28R (0.05% + 0.02% por lado)", sg is not None and abs(sg["fee_r"] - 0.28) < 1e-9)
    sg0 = _build_signal("T", "LONG", bar0, 99.5, 0.01, Config(slippage_pct=0.0), None, qi0, 0, exb)
    check("sin deslizamiento fee_r vuelve a 0.2R", sg0 is not None and abs(sg0["fee_r"] - 0.2) < 1e-9)
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

    # 5) Variables de entorno
    env = {"MIN_SCORE": "3", "SESSIONS": "1,2,3", "ALLOW_SHORT": "false", "EXCLUDE_SYMBOLS": "abc,xyz",
           "ACCOUNT_SIZE": "500", "EXCHANGE": "BOTH", "BINGX_RPS": "5", "INCLUDE_NON_CRYPTO": "true",
           "REQUIRE_MSS": "true", "BTC_FILTER": "true", "SLIPPAGE_PCT": "0.03", "MAX_OPEN_PER_SIDE": "2"}
    os.environ.update(env)
    c = apply_env(Config())
    check("apply_env lee todas las variables", c.min_score == 3 and c.sessions == (1, 2, 3) and c.allow_short is False
          and c.exclude_symbols == ("ABC", "XYZ") and c.account_size == 500.0 and validate(c) == []
          and c.exchange == "both" and c.bingx_rps == 5.0 and c.include_non_crypto is True
          and c.require_mss is True and c.btc_filter is True and c.slippage_pct == 0.03 and c.max_open_per_side == 2)
    for k in env:
        os.environ.pop(k, None)
    check("EXCHANGE invalido se rechaza", validate(Config(exchange="kraken")) != [])

    # 6) Filtros nuevos sobre el caso sintetico (SHORT tras barrer el Q1, miercoles 15 jul 2026, NY AM)
    cfg6 = Config(require_chain=False, bias_mode="none", sessions=(1, 2))
    short_bars = bars[:-1] + [{"t": _ms(2026, 7, 15, 7, 55), "o": 100.5, "h": 100.9, "l": 99.6, "c": 99.7}]
    check("MSS: cierre por debajo de las 3 velas previas -> señal con mss=True",
          (lambda r_: len(r_) == 1 and r_[0]["mss"] is True)(analyze("T", short_bars, 0.01, replace(cfg6, require_mss=True))))
    check("MSS obligatorio bloquea el caso sin ruptura",
          analyze("T", bars, 0.01, replace(cfg6, require_mss=True)) == []
          and len(analyze("T", bars, 0.01, cfg6)) == 1)
    check("BTC 1H alcista bloquea el SHORT; bajista o neutra lo dejan pasar",
          analyze("T", bars, 0.01, replace(cfg6, btc_filter=True), ref=1) == []
          and len(analyze("T", bars, 0.01, replace(cfg6, btc_filter=True), ref=-1)) == 1
          and len(analyze("T", bars, 0.01, replace(cfg6, btc_filter=True), ref=0)) == 1
          and len(analyze("T", bars, 0.01, replace(cfg6, btc_filter=True), ref=None)) == 1)
    check("BTC filtro desactivado ignora la tendencia de BTC", len(analyze("T", bars, 0.01, cfg6, ref=1)) == 1)
    sg_l = {"side": "LONG", "entry": 100.0, "sl": 99.0, "max_entry": 100.3}
    sg_s = {"side": "SHORT", "entry": 100.0, "sl": 101.0, "max_entry": 99.7}
    check("precio fresco: vigente / ya se alejo / toco el SL (LONG y SHORT)",
          stale_reason(sg_l, 100.1) is None and stale_reason(sg_l, 100.5) is not None
          and stale_reason(sg_l, 98.9) is not None and stale_reason(sg_s, 99.8) is None
          and stale_reason(sg_s, 99.5) is not None and stale_reason(sg_s, 101.2) is not None)
    st6 = {"active": {str(i): {"side": "LONG", "status": "open"} for i in range(4)}}
    check("exposicion: 4 LONG abiertos bloquea LONG pero no SHORT",
          open_blocked(st6, "LONG", Config()) is not None and open_blocked(st6, "SHORT", Config()) is None)
    st6["active"].update({str(10 + i): {"side": "SHORT", "status": "open"} for i in range(4)})
    check("exposicion: total 8 bloquea todo; 0 = sin limite",
          open_blocked(st6, "SHORT", Config()) is not None
          and open_blocked(st6, "LONG", Config(max_open_per_side=0, max_open_total=0)) is None)
    tr_ = trend_series([{"c": 100.0 + i} for i in range(400)], Config())
    check("trend_series: alcista en una serie creciente, 0 al principio", tr_[-1] == 1 and tr_[10] == 0)
    good = [{"r": 0.5 if k % 3 else -0.2, "t": k} for k in range(60)]
    bad = [{"r": 0.4 if k < 30 else -0.5, "t": k} for k in range(60)]
    wf = walk_forward_text([("buena", good), ("espuria", bad)], 30)
    check("fuera de muestra: elige la mejor de la 1a mitad y la juzga en la 2a",
          wf is not None and "espuria" in wf and "NO se mantiene" in wf)

    # 7) BingX: velas, contratos, reintentos
    now_ms = 10 * BAR_MS + 1000
    rows = [{"time": i * BAR_MS, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "9"}
            for i in range(10, -1, -1)]                    # BingX: la mas nueva primero, con la vela en curso
    bd = parse_bingx_klines(rows, now_ms)
    check("BingX velas (dict): orden y vela abierta descartada",
          len(bd) == 10 and bd[0]["t"] == 0 and bd[-1]["t"] == 9 * BAR_MS and bd[-1]["v"] == 9.0)
    rows_l = [[i * BAR_MS, "1", "2", "0.5", "1.5", "9", (i + 1) * BAR_MS - 1] for i in range(11)]
    bl = parse_bingx_klines(rows_l, now_ms)
    check("BingX velas (lista)", len(bl) == 10 and bl[-1]["h"] == 2.0 and bl[-1]["c"] == 1.5 and bl[-1]["v"] == 9.0)
    check("BingX precio actual incluye la vela abierta", parse_bingx_klines(rows, 10 ** 18)[-1]["t"] == 10 * BAR_MS)

    contracts = [
        {"symbol": "BTC-USDT", "status": 1, "pricePrecision": 1, "quantityPrecision": 4, "tradeMinQuantity": 0.0001},
        {"symbol": "ETH-USDT", "status": 1, "pricePrecision": 2, "quantityPrecision": 3, "tradeMinQuantity": 0.001},
        {"symbol": "LOWV-USDT", "status": 1, "pricePrecision": 5, "quantityPrecision": 0, "tradeMinQuantity": 1},
        {"symbol": "NCSKTSLA2USD-USDT", "status": 1, "pricePrecision": 2, "quantityPrecision": 2,
         "tradeMinQuantity": 0.01},
        {"symbol": "OLD-USDT", "status": 0, "pricePrecision": 2, "quantityPrecision": 2, "tradeMinQuantity": 1},
        {"symbol": "BTC-USD", "status": 1, "pricePrecision": 1, "quantityPrecision": 1, "tradeMinQuantity": 1},
    ]
    tickers = [{"symbol": "BTC-USDT", "quoteVolume": "900000000"}, {"symbol": "ETH-USDT", "quoteVolume": "50000000"},
               {"symbol": "LOWV-USDT", "quoteVolume": "1000"}, {"symbol": "NCSKTSLA2USD-USDT", "quoteVolume": "5000000"},
               {"symbol": "OLD-USDT", "quoteVolume": "9000000"}]
    syms, meta = parse_bingx_contracts(contracts, tickers, Config())
    check("BingX contratos: filtra volumen, acciones, inactivos y no-USDT",
          syms == ["BTC-USDT", "ETH-USDT"] and abs(meta["BTC-USDT"]["tick"] - 0.1) < 1e-12
          and abs(meta["BTC-USDT"]["step"] - 0.0001) < 1e-12 and meta["BTC-USDT"]["min_qty"] == 0.0001)
    syms, _ = parse_bingx_contracts(contracts, tickers, Config(min_quote_volume=0, include_non_crypto=True))
    check("BingX con MIN_QUOTE_VOLUME=0 analiza todas las activas",
          syms == ["BTC-USDT", "ETH-USDT", "NCSKTSLA2USD-USDT", "LOWV-USDT"])
    syms, _ = parse_bingx_contracts(contracts, tickers, Config(exclude_symbols=("ETHUSDT",)))
    check("BingX EXCLUDE_SYMBOLS acepta ETHUSDT o ETH-USDT", syms == ["BTC-USDT"])
    syms, _ = parse_bingx_contracts(contracts, tickers, Config(), only=["ethusdt"])
    check("BingX --symbols acepta ETHUSDT", syms == ["ETH-USDT"])

    class FR:
        def __init__(self, status, js):
            self.status_code, self._js = status, js

        def json(self):
            if self._js is None:
                raise ValueError("no json")
            return self._js

    seq = []
    real_req, real_sleep = requests, time.sleep
    requests = types.SimpleNamespace(get=lambda url, params=None, timeout=0: seq.pop(0), RequestException=OSError)
    time.sleep = lambda secs: None
    _BINGX_LIMITER = None
    try:
        tcfg = Config(bingx_rps=1000)
        seq[:] = [FR(200, {"code": 100410, "msg": "rate limit exceeded"}), FR(429, None),
                  FR(200, {"code": 0, "data": [1, 2]})]
        check("BingX reintenta ante limite de peticiones", bingx_get(tcfg, "/x") == [1, 2])
        for resp, needle, name in ((FR(200, {"code": 109400, "msg": "bad"}), "109400", "BingX propaga errores de la API"),
                                   (FR(451, None), "451", "BingX 451 explica el bloqueo de region")):
            seq[:] = [resp]
            try:
                bingx_get(tcfg, "/x")
                got = False
            except RuntimeError as e:
                got = needle in str(e)
            check(name, got)
    finally:
        requests, time.sleep = real_req, real_sleep
        _BINGX_LIMITER = None
    msg = plain(format_message(sample_signal(), Config()))
    check("mensaje muestra exchange, precio ahora, BTC, MSS y costes",
          "BTC-USDT" in msg and "BingX" in msg and "Precio ahora" in msg and "BTC 1H" in msg
          and "Ruptura de estructura" in msg and "deslizamiento" in msg)

    print("\nRESULTADO:", "TODO OK" if ok else "HAY FALLOS")
    return 0 if ok else 1


# ============================================================================
def main():
    ap = argparse.ArgumentParser(description="QT Chain Scanner (BingX / Binance Futures -> Telegram)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--once", action="store_true", help="un solo escaneo y salir")
    ap.add_argument("--check", action="store_true", help="comprueba la conexion con el exchange y sale")
    ap.add_argument("--exchange", choices=["bingx", "binance", "both"], default=None)
    ap.add_argument("--dry-run", action="store_true", help="no envia a Telegram, solo imprime")
    ap.add_argument("--backtest", action="store_true", help="backtest sobre el historico del exchange + embudo de filtros")
    ap.add_argument("--compare", action="store_true", help="con --backtest: prueba variantes de la estrategia")
    ap.add_argument("--days", type=int, default=14, help="dias de historico del backtest (defecto 14)")
    ap.add_argument("--top", type=int, default=40, help="monedas mas liquidas en el backtest (defecto 40, 0 = todas)")
    ap.add_argument("--symbols", default="", help="lista separada por comas, ej. BTC-USDT,ETH-USDT")
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
    if a.exchange:
        cfg.exchange = a.exchange
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
    only = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    if a.test_telegram:
        send_telegram("✅ QT Scanner conectado. Las señales llegarán a este chat.\nAsí se verá una señal (EJEMPLO, no operar):")
        s = sample_signal()
        send_telegram("🧪 <b>EJEMPLO</b>\n\n" + format_message(s, cfg), buttons=buttons_for(s))
        print("Mensajes de prueba enviados.")
        return
    if a.backtest:
        run_backtest(cfg, only, max(2, a.days), a.top, a.compare)
        return
    if a.check:
        check_exchange(cfg, only)
        return
    if a.once:
        run_once(cfg, load_markets(cfg, only, a.dry_run), a.dry_run)
    else:
        loop(cfg, a.dry_run, only)


if __name__ == "__main__":
    main()
