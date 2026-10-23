#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QT Chain Scanner v4  -  escanea los perpetuos USDT de Binance Futures en 5m y envia
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

NO incluye (frente al Pine): filtro SMT con 2o activo, EMA 1H, True Open Weekly, noticias.

Uso:
    python qt_scanner_bot.py --selftest            # prueba offline de la logica
    python qt_scanner_bot.py --test-telegram       # mensaje de prueba + senal de ejemplo
    python qt_scanner_bot.py --once --dry-run      # un escaneo, solo imprime
    python qt_scanner_bot.py                       # bucle continuo cada 5 min
Aviso: no es consejo financiero. Prueba en demo / papel antes de usar dinero real.
"""
import argparse
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
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
log = logging.getLogger("qt")


# ============================================================================
#  CONFIGURACION (cada campo se puede sobrescribir con una variable de entorno
#  con el mismo nombre en MAYUSCULAS, p. ej. min_score -> MIN_SCORE)
# ============================================================================
@dataclass
class Config:
    base_url: str = "https://fapi.binance.com"   # env: BINANCE_BASE_URL
    interval: str = "5m"                         # fijo (la logica de ciclos es de 5m)
    klines_limit: int = 400                      # fijo (~33 h de velas de 5m)
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


ENV_ALIASES = {"base_url": "BINANCE_BASE_URL"}
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
def analyze(symbol, bars, tick, cfg):
    """bars: velas CERRADAS [{t,o,h,l,c}], la ultima es la que acaba de cerrar."""
    if len(bars) < 40:
        return []
    atr = atr_series(bars)
    qinfos = [quarters(b["t"]) for b in bars]
    mins_arr = [q["mins"] for q in qinfos]
    last = len(bars) - 1
    cycle_start_ms = bars[last]["t"] - qinfos[last]["m90"] * 60_000
    start = last
    while start - 1 >= 0 and bars[start - 1]["t"] >= cycle_start_ms:
        start -= 1

    signals = []
    q1_hi = q1_lo = c_hi = c_lo = None
    sweep_bar_hi = sweep_bar_lo = None
    done_s = done_l = False

    for i in range(start, last + 1):
        b = bars[i]
        qi = qinfos[i]
        if i == start:
            q1_hi, q1_lo, c_hi, c_lo = b["h"], b["l"], b["h"], b["l"]
        else:
            c_hi = max(c_hi, b["h"])
            c_lo = min(c_lo, b["l"])
            if qi["qMi"] == 0:
                q1_hi = max(q1_hi, b["h"])
                q1_lo = min(q1_lo, b["l"])
        in_window = qi["qMi"] in (1, 2)
        if in_window:
            if b["h"] > q1_hi and sweep_bar_hi is None:
                sweep_bar_hi = i
            if b["l"] < q1_lo and sweep_bar_lo is None:
                sweep_bar_lo = i
        a = atr[i]
        if a is None or not in_window:
            continue

        chain = detect_chain(qi["qs"], cfg)
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

        short_sig = (cfg.allow_short and bear_bias and not done_s and seq_ok and session_ok and q1_ok
                     and sweep_ok_hi and strong_bear and b["c"] < q1_hi)
        long_sig = (cfg.allow_long and bull_bias and not done_l and seq_ok and session_ok and q1_ok
                    and sweep_ok_lo and strong_bull and b["c"] > q1_lo)

        extra = {
            "chain_depth": chain["depth"] if chain else 0,
            "bias_levels": bias_levels,
            "body_ratio": (body / rng) if rng > 0 else 0.0,
        }
        if short_sig:
            done_s = True
            if i == last:
                ex = dict(extra, sweep_atr=depth_hi / a, q1_level=q1_hi)
                sig = _build_signal(symbol, "SHORT", b, c_hi + cfg.sl_buf_atr * a, tick, cfg, chain, qi,
                                    cycle_start_ms, ex)
                if sig:
                    signals.append(sig)
        if long_sig:
            done_l = True
            if i == last:
                ex = dict(extra, sweep_atr=depth_lo / a, q1_level=q1_lo)
                sig = _build_signal(symbol, "LONG", b, c_lo - cfg.sl_buf_atr * a, tick, cfg, chain, qi,
                                    cycle_start_ms, ex)
                if sig:
                    signals.append(sig)
    return signals


def score_signal(ex, cfg):
    """Puntuacion 0-5: cadena, cadena profunda, sesgo True Open, barrido limpio, vela fuerte."""
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
    return s


def _build_signal(symbol, side, bar, sl, tick, cfg, chain, qi, cycle_start_ms, ex):
    entry = bar["c"]
    risk = (sl - entry) if side == "SHORT" else (entry - sl)
    if risk <= 0 or risk < cfg.min_risk_ticks * tick:
        return None
    sgn = -1 if side == "SHORT" else 1
    above = (side == "SHORT") == (cfg.to_logic == "manipulacion")
    return {
        "symbol": symbol, "side": side, "entry": entry, "sl": sl,
        "tp1": entry + sgn * risk * cfg.partial_r,
        "tp2": entry + sgn * risk * cfg.rr,
        "max_entry": entry + sgn * risk * cfg.entry_chase_r,   # LONG: tope superior / SHORT: tope inferior
        "risk_pct": risk / entry * 100.0,
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
    stars = "⭐" * s["score"] + "☆" * (5 - s["score"])
    pf = cfg.partial_pct
    lines = [
        "%s <b>%s · %s</b>   %s %d/5" % (icon, s["side"], s["symbol"], stars, s["score"]),
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
        return "🏆 <b>TP2 alcanzado</b> · %s\nCierra el resto. Resultado simulado: %+.2fR." % (head, r)
    if kind == "SL":
        return "🛑 <b>SL tocado</b> · %s\nResultado simulado: %+.2fR." % (head, r)
    if kind == "BE":
        return "⚪ <b>Cerrada en BE tras TP1</b> · %s\nResultado simulado: %+.2fR." % (head, r)
    return ("⌛ <b>Senal caducada</b> · %s\nSin resolver tras %.0f h; deja de seguirse. "
            "Si sigues dentro, gestiona el trade tu." % (head, cfg.track_hours))


def buttons_for(symbol):
    return [
        {"text": "📈 Binance Futures", "url": "https://www.binance.com/en/futures/%s" % symbol},
        {"text": "📊 TradingView", "url": "https://www.tradingview.com/chart/?symbol=BINANCE%%3A%s.P" % symbol},
    ]


# ============================================================================
#  BINANCE FUTURES + TELEGRAM
# ============================================================================
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


def load_symbols(cfg, only=None):
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


def fetch_bars(cfg, symbol):
    rows = http_get(cfg.base_url + "/fapi/v1/klines",
                    {"symbol": symbol, "interval": cfg.interval, "limit": cfg.klines_limit})
    now_ms = int(time.time() * 1000)
    return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
            for r in rows if int(r[6]) < now_ms]          # solo velas cerradas


def scan_all(cfg, symbols, meta):
    found = []
    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        futs = {ex.submit(fetch_bars, cfg, s): s for s in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                m = meta.get(sym, {})
                for s in analyze(sym, f.result(), m.get("tick", 0.0001), cfg):
                    s["step"] = m.get("step", 0.0)
                    found.append(s)
            except Exception as e:  # una moneda con error no debe parar el escaneo
                log.warning("%s: %s", sym, e)
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
            "stats": {}, "totals": new_stats(), "last_summary": ""}
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
    a.update({"sl_now": s["sl"], "last_t": s["entry_t"], "created": now, "status": "open", "msg": ids})
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
        events.append(("EXP", pf * cfg.partial_r if sig["status"] == "tp1" else 0.0))
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
           "<i>Simulado con velas de 5m (SL primero si coinciden). Tu resultado real puede variar.</i>") % (
        d["n"], d["tp2"], d["be"], d["sl"], d["exp"], d["r"], t["n"], t["r"])
    if emit(msg, False) is not None:
        st["last_summary"] = today


# ============================================================================
#  BUCLE PRINCIPAL
# ============================================================================
def run_once(cfg, symbols, meta, dry_run):
    st = load_state()
    if cfg.track_signals and not dry_run:
        track_active(cfg, st)
    sigs = scan_all(cfg, symbols, meta)
    sigs = [s for s in sigs if s["score"] >= cfg.min_score
            and (cfg.max_risk_pct <= 0 or s["risk_pct"] <= cfg.max_risk_pct)]
    sigs.sort(key=lambda s: (-s["score"], s["risk_pct"]))

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
        log.info("SEÑAL %s %s (%d/5)", s["side"], s["symbol"], s["score"])
        ids = emit(format_message(s, cfg), dry_run, buttons=buttons_for(s["symbol"]))
        if ids is None:
            continue
        n_sent += 1
        if dry_run:
            continue
        st["sent"][key] = now
        st["last_symbol"][s["symbol"]] = now
        st["daily"]["n"] += 1
        bump_stats(st, "n")
        if cfg.track_signals:
            st["active"][key] = make_active(s, ids, now)
    if not dry_run:
        maybe_summary(cfg, st)
        save_state(st)
    log.info("escaneo terminado: %d monedas, %d señales nuevas, %d en seguimiento",
             len(symbols), n_sent, len(st["active"]))


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
    return ("🚀 <b>QT Scanner iniciado</b>\n"
            "Monedas: %d | sesiones: %s\n"
            "Sesgo: %s | cadena: %s | puntuacion minima: %d/5\n"
            "RR %.1f | TP1 %.1fR (%.0f%%) | riesgo max SL %.1f%%\n"
            "%s\n"
            "Seguimiento: %s | resumen diario: %s") % (
        n, ", ".join(SESSION_NAMES[i] for i in cfg.sessions), cfg.bias_mode,
        "obligatoria" if cfg.require_chain else "opcional", cfg.min_score,
        cfg.rr, cfg.partial_r, cfg.partial_pct, cfg.max_risk_pct, acct,
        "on" if cfg.track_signals else "off",
        ("%d:00 NY" % cfg.daily_summary_hour) if cfg.daily_summary_hour >= 0 else "off")


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
            run_once(cfg, symbols, meta, dry_run)
        except Exception as e:
            log.error("error en el escaneo: %s", e)
            hint = ""
            if "451" in str(e) or "403" in str(e):
                hint = "\nBinance bloquea la region del servidor: cambia la region en Railway (p. ej. Europa)."
            notify("scan_error", "⚠️ QT Scanner: error en el escaneo: %s%s" % (str(e)[:200], hint), dry_run)
        now = time.time()
        nxt = (int(now // 300) + 1) * 300 + 3                      # 3 s despues del cierre de la vela de 5m
        time.sleep(max(1, nxt - now))


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
            "bias_levels": [("TO Daily", 100300.0), ("TO Sesión", 100200.0)], "bias_rel": "bajo"}


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
        check("puntuacion 0-5 y max_entry bajo la entrada", 0 <= s["score"] <= 5 and s["max_entry"] < s["entry"])
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
                                   "tick": 0.01, "entry_t": 0, "score": 3}, {}, 0)

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
def main():
    ap = argparse.ArgumentParser(description="QT Chain Scanner (Binance Futures -> Telegram)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--once", action="store_true", help="un solo escaneo y salir")
    ap.add_argument("--dry-run", action="store_true", help="no envia a Telegram, solo imprime")
    ap.add_argument("--symbols", default="", help="lista separada por comas, ej. BTCUSDT,ETHUSDT")
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
    only = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    if a.test_telegram:
        send_telegram("✅ QT Scanner conectado. Las señales llegarán a este chat.\nAsí se verá una señal (EJEMPLO, no operar):")
        s = sample_signal()
        send_telegram("🧪 <b>EJEMPLO</b>\n\n" + format_message(s, cfg), buttons=buttons_for(s["symbol"]))
        print("Mensajes de prueba enviados.")
        return
    if a.once:
        symbols, meta = load_symbols(cfg, only)
        run_once(cfg, symbols, meta, a.dry_run)
    else:
        loop(cfg, a.dry_run, only)


if __name__ == "__main__":
    main()
