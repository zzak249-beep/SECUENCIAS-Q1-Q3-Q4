"""Lógica de detección. No hace llamadas de red: recibe DataFrames de velas CERRADAS."""
import numpy as np

from indicators import atr_wilder, bollinger, percent_rank, wavelet_slope_z


def _clip01(v):
    return float(max(0.0, min(1.0, v)))


def _htf_slope(htf_df, cfg):
    if htf_df is None or len(htf_df) < cfg.wavelet_window + cfg.slope_len:
        return None
    logc = np.log(htf_df["close"].astype(float).values)
    z, _ = wavelet_slope_z(logc, cfg.wavelet_window, cfg.slope_len, cfg.wavelet_mult)
    return z


def analyze(symbol, df, cfg, htf_fetcher=None):
    """Analiza un símbolo.

    df: velas cerradas (columnas ts, open, high, low, close, volume), orden ascendente.
    htf_fetcher: función sin argumentos que devuelve el DataFrame de la TF superior
                 (solo se llama si hay una ruptura candidata, para ahorrar llamadas a la API).

    Devuelve (señal | None, info_watch | None).
    """
    need = max(
        cfg.wavelet_window + cfg.slope_len,
        cfg.bb_len + cfg.sqz_lookback + cfg.sqz_mem + 2,
        cfg.atr_len * 3,
    )
    if df is None or len(df) < need:
        return None, None

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    basis, upper, lower = bollinger(close, cfg.bb_len, cfg.bb_mult)
    bw = (upper - lower) / basis
    atr = atr_wilder(high, low, close, cfg.atr_len)

    last = len(df) - 1
    ranks = [percent_rank(bw, last - i, cfg.sqz_lookback) for i in range(cfg.sqz_mem + 1)]
    depth_pct = min(ranks)
    watch = {
        "symbol": symbol,
        "in_squeeze": bool(ranks[0] <= cfg.sqz_pct),
        "depth_pct": float(ranks[0]),
    }

    if depth_pct > cfg.sqz_pct:  # no hubo squeeze reciente
        return None, watch

    c, c1 = close.iloc[-1], close.iloc[-2]
    up, up1 = upper.iloc[-1], upper.iloc[-2]
    lo, lo1 = lower.iloc[-1], lower.iloc[-2]
    a = float(atr.iloc[-1])
    if not np.isfinite(a) or a <= 0:
        return None, watch

    side = None
    if cfg.allow_long and c > up and c1 <= up1:
        side = "LONG"
        extension = (c - up) / a
    elif cfg.allow_short and c < lo and c1 >= lo1:
        side = "SHORT"
        extension = (lo - c) / a
    if side is None:
        return None, watch

    if extension > cfg.max_extension_atr:  # ya muy extendida: entrada tardía
        return None, watch

    vol_ratio = float(volume.iloc[-1] / max(volume.iloc[-21:-1].mean(), 1e-12))
    if vol_ratio < cfg.min_volume_ratio:
        return None, watch

    logc = np.log(close.values)
    slope_z, noise_frac = wavelet_slope_z(logc, cfg.wavelet_window, cfg.slope_len, cfg.wavelet_mult)
    if slope_z is None:
        return None, watch
    if side == "LONG" and slope_z < cfg.min_slope_z:
        return None, watch
    if side == "SHORT" and slope_z > -cfg.min_slope_z:
        return None, watch

    # Confluencia con la temporalidad superior
    htf_z = None
    if htf_fetcher is not None:
        try:
            htf_z = _htf_slope(htf_fetcher(), cfg)
        except Exception:
            htf_z = None
    sign = 1.0 if side == "LONG" else -1.0
    htf_dir = None if htf_z is None else htf_z * sign
    if cfg.require_htf and (htf_dir is None or htf_dir <= 0):
        return None, watch

    # ---------------- Puntuación 0-100 ----------------
    s_sqz = _clip01((cfg.sqz_pct - depth_pct) / max(cfg.sqz_pct, 1e-9)) * 25
    s_vol = _clip01(vol_ratio / 3.0) * 20
    s_slope = _clip01(abs(slope_z) / (3.0 * max(cfg.min_slope_z, 1e-9))) * 20
    if htf_dir is None:
        s_htf = 5.0
    elif htf_dir >= cfg.min_slope_z:
        s_htf = 20.0
    elif htf_dir > 0:
        s_htf = 10.0
    else:
        s_htf = 0.0
    s_brk = _clip01(extension / 1.0) * 15
    score = round(s_sqz + s_vol + s_slope + s_htf + s_brk, 1)
    if score < cfg.min_score:
        return None, watch

    # ---------------- Niveles y tamaño sugerido ----------------
    entry = float(c)
    stop_dist = a * cfg.atr_stop
    if side == "LONG":
        sl = entry - stop_dist
        tp1 = entry + stop_dist * cfg.tp1_r
        tp2 = entry + stop_dist * cfg.tp2_r
    else:
        sl = entry + stop_dist
        tp1 = entry - stop_dist * cfg.tp1_r
        tp2 = entry - stop_dist * cfg.tp2_r

    risk_usd = cfg.account_usd * cfg.risk_pct / 100.0
    qty = risk_usd / stop_dist
    notional = qty * entry
    capped = False
    if notional > cfg.account_usd * cfg.max_leverage:
        notional = cfg.account_usd * cfg.max_leverage
        qty = notional / entry
        risk_usd = qty * stop_dist
        capped = True

    reasons = [
        f"Squeeze reciente (percentil de ancho BB: {depth_pct:.0f})",
        f"Ruptura {'alcista' if side == 'LONG' else 'bajista'} con volumen x{vol_ratio:.1f}",
        f"Pendiente wavelet {slope_z:+.1f}σ (ruido {noise_frac * 100:.0f}%)",
    ]
    if htf_z is not None:
        reasons.append(f"{cfg.htf}: pendiente wavelet {htf_z:+.1f}σ")

    signal = {
        "symbol": symbol,
        "timeframe": cfg.timeframe,
        "side": side,
        "score": score,
        "entry": entry,
        "sl": float(sl),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "atr": a,
        "qty": float(qty),
        "notional": float(notional),
        "leverage": float(notional / cfg.account_usd),
        "risk_usd": float(risk_usd),
        "capped": capped,
        "reasons": reasons,
        "candle_ts": int(df["ts"].iloc[-1]),
    }
    return signal, watch
