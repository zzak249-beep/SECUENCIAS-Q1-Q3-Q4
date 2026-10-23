#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QT Chain Scanner  -  escanea todos los perpetuos USDT de Binance Futures en 5m
y envia las entradas por Telegram. NO opera: solo avisa.

Replica la logica del Pine Script "QT Chain Map + Entradas v3":
  - 7 ciclos (Yearly, Monthly, Weekly, Daily, 90 Min, Micro, Nano) en hora de Nueva York
  - Cadenas (3+ ciclos adyacentes en el mismo cuarto, mas puente Q4->Q1)
  - Barrido del Q1 del ciclo de 90 min + vela de confirmacion fuerte
  - Sesgo por True Opens (Daily y Session)
  - SL / TP1 (parcial) / TP2

NO incluye (frente al Pine): filtro SMT con 2o activo, EMA 1H, True Open Weekly,
limites diarios de operaciones, ventanas de noticias.

Instalacion:
    pip install requests tzdata
Variables de entorno:
    TELEGRAM_TOKEN    token que te da @BotFather
    TELEGRAM_CHAT_ID  tu chat id (o el de un canal/grupo)
Uso:
    python qt_scanner_bot.py --selftest            # prueba offline de la logica
    python qt_scanner_bot.py --test-telegram       # manda un mensaje de prueba
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
from dataclasses import dataclass
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
log = logging.getLogger("qt")


# ============================================================================
#  CONFIGURACION (equivale a los inputs del Pine)
# ============================================================================
@dataclass
class Config:
    base_url: str = "https://fapi.binance.com"
    interval: str = "5m"
    klines_limit: int = 400            # ~33 h de velas de 5m (weight 2)
    min_quote_volume: float = 20_000_000.0   # volumen 24h minimo en USDT
    max_symbols: int = 0               # 0 = sin limite
    workers: int = 8
    # cadena
    lowest_level: str = "90 Min"       # "90 Min" | "Micro" | "Nano"
    min_depth: int = 3
    need_level: str = "Daily"          # "Ninguno" o un nivel
    chain_kind: str = "Ambas"          # "Ambas" | "Normal" | "Puente"
    require_chain: bool = True
    # sesion: indices del Q diario permitidos (0 Asia, 1 Londres, 2 NY AM, 3 NY PM)
    sessions: tuple = (1, 2)
    # sesgo
    bias_mode: str = "true_opens"      # "none" | "true_opens"
    to_logic: str = "manipulacion"     # "manipulacion" | "tendencia"
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
    min_risk_ticks: int = 4
    allow_long: bool = True
    allow_short: bool = True
    max_signals_per_scan: int = 10


def apply_env(cfg):
    """Permite configurar el bot desde las Variables de Railway sin tocar el codigo."""
    e = os.environ.get

    def as_bool(name, cur):
        v = e(name)
        return cur if v is None else v.strip().lower() in ("1", "true", "yes", "si", "sí")

    cfg.base_url = e("BINANCE_BASE_URL", cfg.base_url).rstrip("/")
    cfg.min_quote_volume = float(e("MIN_QUOTE_VOLUME", cfg.min_quote_volume))
    cfg.max_symbols = int(e("MAX_SYMBOLS", cfg.max_symbols))
    cfg.lowest_level = e("LOWEST_LEVEL", cfg.lowest_level)
    cfg.min_depth = int(e("MIN_DEPTH", cfg.min_depth))
    cfg.need_level = e("NEED_LEVEL", cfg.need_level)
    cfg.require_chain = as_bool("REQUIRE_CHAIN", cfg.require_chain)
    cfg.bias_mode = e("BIAS_MODE", cfg.bias_mode)
    cfg.to_logic = e("TO_LOGIC", cfg.to_logic)
    cfg.rr = float(e("RR", cfg.rr))
    cfg.body_pct = float(e("BODY_PCT", cfg.body_pct))
    cfg.allow_long = as_bool("ALLOW_LONG", cfg.allow_long)
    cfg.allow_short = as_bool("ALLOW_SHORT", cfg.allow_short)
    cfg.max_signals_per_scan = int(e("MAX_SIGNALS_PER_SCAN", cfg.max_signals_per_scan))
    ses = e("SESSIONS")
    if ses:
        cfg.sessions = tuple(int(x) for x in ses.split(",") if x.strip() != "")
    return cfg


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
    """Devuelve (bull_ok, bear_ok)."""
    if cfg.bias_mode == "none":
        return True, True
    levels = []
    if cfg.use_to_daily and qi["qD"] >= 1:
        levels.append(find_true_open(bars, mins_arr, idx, "daily"))
    if cfg.use_to_session and qi["q90"] >= 1:
        levels.append(find_true_open(bars, mins_arr, idx, "session"))
    used = [lv for lv in levels if lv is not None]
    if not used:
        return False, False
    c = bars[idx]["c"]
    all_above = all(c > lv for lv in used)
    all_below = all(c < lv for lv in used)
    if cfg.to_logic == "manipulacion":
        return all_below, all_above      # LONG bajo TO, SHORT sobre TO
    return all_above, all_below          # tendencia


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
        bull_bias, bear_bias = bias_for(bars, mins_arr, i, qi, cfg)

        short_sig = (cfg.allow_short and bear_bias and not done_s and seq_ok and session_ok and q1_ok
                     and sweep_ok_hi and strong_bear and b["c"] < q1_hi)
        long_sig = (cfg.allow_long and bull_bias and not done_l and seq_ok and session_ok and q1_ok
                    and sweep_ok_lo and strong_bull and b["c"] > q1_lo)

        if short_sig:
            done_s = True
            if i == last:
                sig = _build_signal(symbol, "SHORT", b, c_hi + cfg.sl_buf_atr * a, tick, cfg, chain, qi, cycle_start_ms)
                if sig:
                    signals.append(sig)
        if long_sig:
            done_l = True
            if i == last:
                sig = _build_signal(symbol, "LONG", b, c_lo - cfg.sl_buf_atr * a, tick, cfg, chain, qi, cycle_start_ms)
                if sig:
                    signals.append(sig)
    return signals


def _build_signal(symbol, side, bar, sl, tick, cfg, chain, qi, cycle_start_ms):
    entry = bar["c"]
    risk = (sl - entry) if side == "SHORT" else (entry - sl)
    if risk <= 0 or risk < cfg.min_risk_ticks * tick:
        return None
    sgn = -1 if side == "SHORT" else 1
    return {
        "symbol": symbol, "side": side, "entry": entry, "sl": sl,
        "tp1": entry + sgn * risk * cfg.partial_r,
        "tp2": entry + sgn * risk * cfg.rr,
        "risk_pct": risk / entry * 100.0,
        "chain": chain_text(chain),
        "session": SESSION_NAMES[qi["qD"]],
        "time_ny": qi["local"].strftime("%Y-%m-%d %H:%M"),
        "cycle_start_ms": cycle_start_ms,
        "tick": tick,
    }


def fmt_price(x, tick):
    dec = 0 if tick >= 1 else max(0, math.ceil(-math.log10(tick) - 1e-9))
    return ("%." + str(dec) + "f") % x


def format_message(s):
    icon = "🔴" if s["side"] == "SHORT" else "🟢"
    t = s["tick"]
    return (
        "%s <b>%s</b> %s (5m)\n"
        "Entrada: <code>%s</code>\n"
        "SL: <code>%s</code>  (riesgo %.2f%%)\n"
        "TP1 (%.1fR, cerrar parcial + BE): <code>%s</code>\n"
        "TP2: <code>%s</code>\n"
        "Cadena: %s\n"
        "Sesion: %s | vela cerrada %s NY"
    ) % (icon, s["side"], s["symbol"], fmt_price(s["entry"], t), fmt_price(s["sl"], t), s["risk_pct"],
         1.0, fmt_price(s["tp1"], t), fmt_price(s["tp2"], t), s["chain"], s["session"], s["time_ny"])


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
    info = http_get(cfg.base_url + "/fapi/v1/exchangeInfo")
    ticks, syms = {}, []
    for s in info["symbols"]:
        if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    ticks[s["symbol"]] = float(f["tickSize"])
            syms.append(s["symbol"])
    vols = {x["symbol"]: float(x["quoteVolume"]) for x in http_get(cfg.base_url + "/fapi/v1/ticker/24hr")}
    if only:
        syms = [s for s in syms if s in only]
    else:
        syms = [s for s in syms if vols.get(s, 0.0) >= cfg.min_quote_volume]
    syms.sort(key=lambda s: -vols.get(s, 0.0))
    if cfg.max_symbols:
        syms = syms[:cfg.max_symbols]
    return syms, ticks


def fetch_bars(cfg, symbol):
    rows = http_get(cfg.base_url + "/fapi/v1/klines",
                    {"symbol": symbol, "interval": cfg.interval, "limit": cfg.klines_limit})
    now_ms = int(time.time() * 1000)
    bars = [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
            for r in rows if int(r[6]) < now_ms]          # solo velas cerradas
    return bars


def scan_all(cfg, symbols, ticks):
    found = []
    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        futs = {ex.submit(fetch_bars, cfg, s): s for s in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                bars = f.result()
                found.extend(analyze(sym, bars, ticks.get(sym, 0.0001), cfg))
            except Exception as e:  # una moneda con error no debe parar el escaneo
                log.warning("%s: %s", sym, e)
    return found


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Faltan TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID")
    r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=10)
    if not r.ok:
        raise RuntimeError("Telegram %s: %s" % (r.status_code, r.text[:200]))


SENT_FILE = os.environ.get("SENT_FILE", "sent_signals.json")


def load_sent():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    cutoff = time.time() - 2 * 86400
    return {k: v for k, v in d.items() if v > cutoff}


def save_sent(d):
    folder = os.path.dirname(SENT_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)


def run_once(cfg, symbols, ticks, dry_run):
    sent = load_sent()
    sigs = scan_all(cfg, symbols, ticks)
    sigs.sort(key=lambda s: s["risk_pct"])
    n_sent = 0
    for s in sigs:
        key = "%s|%s|%s" % (s["symbol"], s["cycle_start_ms"], s["side"])
        if key in sent:
            continue
        if n_sent >= cfg.max_signals_per_scan:
            break
        msg = format_message(s)
        log.info("SEÑAL %s %s", s["side"], s["symbol"])
        if dry_run:
            print(msg.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""), "\n")
        else:
            try:
                send_telegram(msg)
            except Exception as e:
                log.error("no se pudo enviar a Telegram: %s", e)
                continue
        sent[key] = time.time()
        n_sent += 1
    save_sent(sent)
    log.info("escaneo terminado: %d monedas, %d señales nuevas", len(symbols), n_sent)


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


def loop(cfg, dry_run, only):
    symbols, ticks, last_load = [], {}, 0
    announced = False
    while True:
        try:
            if not symbols or time.time() - last_load > 1800:      # refresca la lista cada 30 min
                symbols, ticks = load_symbols(cfg, only)
                last_load = time.time()
                log.info("%d monedas a escanear", len(symbols))
                if not announced:
                    announced = True
                    notify("start", "🚀 QT Scanner iniciado: %d monedas, sesiones %s, sesgo %s, cadena %s." % (
                        len(symbols), [SESSION_NAMES[i] for i in cfg.sessions], cfg.bias_mode,
                        "obligatoria" if cfg.require_chain else "opcional"), dry_run, every=0)
            run_once(cfg, symbols, ticks, dry_run)
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


def selftest():
    ok = True
    # 1) Ejemplos del texto "Quarter Sequence 102"
    cfg = Config(lowest_level="Nano", need_level="Ninguno")
    q = quarters(_ms(2026, 7, 15, 10, 0))["qs"]
    ch = detect_chain(q, cfg)
    exp1 = ch and ch["depth"] == 5 and ch["start"] == 2 and ch["end"] == 6 and ch["q"] == 2
    print("15 jul 2026 10:00 NY ->", [x + 1 for x in q], chain_text(ch), "OK" if exp1 else "FALLO")
    q = quarters(_ms(2026, 9, 23, 9, 58))["qs"]
    ch = detect_chain(q, cfg)
    exp2 = ch and ch["depth"] == 7 and ch["q"] == 2
    print("23 sep 2026 09:58 NY ->", [x + 1 for x in q], chain_text(ch), "OK" if exp2 else "FALLO")
    q = quarters(_ms(2026, 7, 17, 10, 0))["qs"]          # viernes: Weekly fuera (Q0)
    exp3 = q[2] == -1
    print("viernes 17 jul 2026 -> Weekly", q[2], "OK" if exp3 else "FALLO")
    ok = ok and bool(exp1) and bool(exp2) and bool(exp3)

    # 2) Señal sintetica: SHORT tras barrer el Q1 de un ciclo de 90 min (miercoles 15 jul 2026, NY AM)
    cfg = Config(require_chain=False, bias_mode="none", sessions=(1, 2))
    bars = []
    t = _ms(2026, 7, 15, 3, 0)
    while t < _ms(2026, 7, 15, 7, 30):                     # 03:00-07:25: ruido para el ATR (>= 40 velas)
        bars.append({"t": t, "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0})
        t += 300_000
    for m in (30, 35, 40, 45, 50):                         # Q1 del ciclo 07:30 (rango 99.8-100.4)
        bars.append({"t": _ms(2026, 7, 15, 7, m), "o": 100.0, "h": 100.4, "l": 99.8, "c": 100.1})
    bars.append({"t": _ms(2026, 7, 15, 7, 55),             # barrido por arriba + cierre bajista dentro
                 "o": 100.5, "h": 100.9, "l": 99.95, "c": 100.05})
    sigs = analyze("TESTUSDT", bars, 0.01, cfg)
    exp4 = len(sigs) == 1 and sigs[0]["side"] == "SHORT" and sigs[0]["sl"] > 100.9 and sigs[0]["tp2"] < 100.05
    print("senal sintetica ->", [(s["side"], round(s["entry"], 2), round(s["sl"], 3), round(s["tp2"], 3)) for s in sigs],
          "OK" if exp4 else "FALLO")
    if sigs:
        print(format_message(sigs[0]).replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    # 3) Sin barrido valido no debe haber señal
    bars2 = bars[:-1] + [{"t": _ms(2026, 7, 15, 7, 55), "o": 100.1, "h": 100.3, "l": 99.9, "c": 100.0}]
    exp5 = analyze("TESTUSDT", bars2, 0.01, cfg) == []
    print("sin barrido -> sin senal", "OK" if exp5 else "FALLO")
    ok = ok and bool(exp4) and bool(exp5)
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
    ap.add_argument("--loglevel", default="INFO")
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
    only = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    if a.test_telegram:
        send_telegram("✅ QT Scanner conectado. Las señales llegaran a este chat.")
        print("Mensaje de prueba enviado.")
        return
    if a.once:
        symbols, ticks = load_symbols(cfg, only)
        run_once(cfg, symbols, ticks, a.dry_run)
    else:
        loop(cfg, a.dry_run, only)


if __name__ == "__main__":
    main()
