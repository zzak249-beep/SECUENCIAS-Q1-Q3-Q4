"""Bot scanner de BingX (futuros perpetuos USDT) -> señales por Telegram.

Solo envía señales. NO ejecuta órdenes: tú operas manualmente.
"""
import logging
import sys
import time

import ccxt
import pandas as pd

from config import CFG
from notifier import Notifier, format_digest, format_signal
from scanner import analyze

logging.basicConfig(
    level=getattr(logging, CFG.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot")


def make_exchange():
    return ccxt.bingx(
        {
            "enableRateLimit": True,
            "rateLimit": CFG.rate_limit_ms,
            "timeout": 20000,
            "options": {"defaultType": "swap"},
        }
    )


def _retryable(e):
    if isinstance(e, ccxt.NetworkError):  # incluye RateLimitExceeded, timeouts, caídas
        return True
    msg = str(e).lower()
    return "100410" in msg or "rate limit" in msg or "too many" in msg


def call(ex, fn, *args, **kwargs):
    """Llama a la API con reintentos y pausa adaptativa (BingX devuelve 100410 si vamos muy rápido)."""
    delay = 2.0
    for attempt in range(CFG.max_retries + 1):
        try:
            res = fn(*args, **kwargs)
            ex.rateLimit = max(CFG.rate_limit_ms, ex.rateLimit * 0.98)  # relaja poco a poco
            return res
        except ccxt.BaseError as e:
            if not _retryable(e) or attempt == CFG.max_retries:
                raise
            ex.rateLimit = min(ex.rateLimit * 1.5, 3000)  # frena el ritmo
            log.debug("Reintento %d en %.0fs (rateLimit=%dms): %s", attempt + 1, delay, ex.rateLimit, str(e)[:80])
            time.sleep(delay)
            delay = min(delay * 2, 30)


def fetch_df(ex, symbol, timeframe, limit):
    """Devuelve DataFrame de velas CERRADAS (descarta la vela en formación)."""
    raw = call(ex, ex.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    if not raw or len(raw) < 10:
        return None
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    tf_ms = int(ex.parse_timeframe(timeframe) * 1000)
    now_ms = int(time.time() * 1000)
    if df["ts"].iloc[-1] + tf_ms > now_ms:  # la última vela aún no cerró
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def get_universe(ex):
    """Top N perpetuos USDT por volumen 24h, filtrando stablecoins y activos tradicionales."""
    call(ex, ex.load_markets)
    tickers = call(ex, ex.fetch_tickers)
    rows = []
    for sym, t in tickers.items():
        m = ex.markets.get(sym)
        if not m or not m.get("swap") or m.get("quote") != "USDT":
            continue
        if m.get("active") is False:
            continue
        base = str(m.get("base", "")).upper()
        if base in CFG.exclude_bases:
            continue
        if any(base.startswith(p) for p in CFG.exclude_prefixes):
            continue
        vol = t.get("quoteVolume")
        if not vol:
            vol = (t.get("baseVolume") or 0) * (t.get("last") or 0)
        if CFG.min_volume_usd > 0 and (not vol or vol < CFG.min_volume_usd):
            continue
        rows.append((sym, float(vol)))
    rows.sort(key=lambda r: r[1], reverse=True)
    if CFG.top_n > 0:
        rows = rows[: CFG.top_n]
    return [s for s, _ in rows]


def scan_once(ex, last_signal):
    symbols = get_universe(ex)
    log.info("Universo: %d símbolos (TF %s, HTF %s)", len(symbols), CFG.timeframe, CFG.htf)
    t0 = time.time()
    signals, watch = [], []
    now = time.time()

    for sym in symbols:
        try:
            df = fetch_df(ex, sym, CFG.timeframe, CFG.candles)
            htf_fetcher = lambda s=sym: fetch_df(ex, s, CFG.htf, CFG.candles)
            sig, w = analyze(sym, df, CFG, htf_fetcher)
        except ccxt.BaseError as e:
            log.warning("%s: error de exchange: %s", sym, str(e)[:120])
            continue
        except Exception:
            log.exception("%s: error inesperado", sym)
            continue

        if w and w["in_squeeze"]:
            watch.append(w)
        if sig:
            key = (sym, sig["side"])
            if now - last_signal.get(key, 0) < CFG.cooldown_hours * 3600:
                continue  # en cooldown: ya avisamos hace poco
            last_signal[key] = now
            signals.append(sig)

    log.info("Escaneo completado en %.0fs", time.time() - t0)
    signals.sort(key=lambda s: s["score"], reverse=True)
    watch.sort(key=lambda w: w["depth_pct"])
    return signals, watch


def main():
    notifier = Notifier(CFG.telegram_token, CFG.telegram_chat_id)
    if not notifier.enabled:
        log.warning("Telegram sin configurar: las señales se imprimirán en consola.")

    ex = make_exchange()
    last_signal = {}
    last_digest = time.time()  # primer digest tras DIGEST_HOURS

    if CFG.send_startup:
        notifier.send(
            "🤖 <b>Scanner BingX iniciado</b>\n"
            f"TF {CFG.timeframe} (HTF {CFG.htf}) · "
            f"{'todas las monedas' if CFG.top_n <= 0 else 'top ' + str(CFG.top_n)}\n"
            f"Score mínimo {CFG.min_score:g} · escaneo cada {CFG.scan_interval_min} min\n"
            "<i>Solo señales, sin ejecución automática.</i>"
        )

    while True:
        started = time.time()
        try:
            signals, watch = scan_once(ex, last_signal)
            log.info("Escaneo listo: %d señales, %d en squeeze", len(signals), len(watch))
            for s in signals:
                notifier.send(format_signal(s, CFG))
                time.sleep(1)

            if CFG.digest_hours > 0 and time.time() - last_digest >= CFG.digest_hours * 3600:
                if watch:
                    notifier.send(format_digest(watch[: CFG.digest_top], CFG))
                last_digest = time.time()
        except ccxt.BaseError as e:
            log.error("Error de exchange en el ciclo: %s", str(e)[:200])
        except Exception:
            log.exception("Error inesperado en el ciclo")

        elapsed = time.time() - started
        time.sleep(max(10, CFG.scan_interval_min * 60 - elapsed))


if __name__ == "__main__":
    main()
