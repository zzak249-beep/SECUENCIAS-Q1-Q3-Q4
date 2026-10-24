"""Indicadores: wavelet de Haar causal, Bollinger, ATR de Wilder."""
import numpy as np
import pandas as pd

SQRT2 = np.sqrt(2.0)


def haar_denoise(x, mult=1.0):
    """Denoising por wavelet de Haar (umbral universal de Donoho, soft thresholding).

    x: array con longitud potencia de 2, del más antiguo al más reciente.
    Es CAUSAL: solo usa la ventana recibida y devuelve únicamente el último punto.

    Devuelve (valor_denoised_final, sigma_ruido, fraccion_de_ruido 0..1).
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 4 or (n & (n - 1)) != 0:
        raise ValueError("La longitud debe ser potencia de 2")

    c = x.copy()
    m = n
    while m > 1:  # transformada directa (layout Mallat in-place)
        h = m // 2
        even = c[0:m:2].copy()
        odd = c[1:m:2].copy()
        c[:h] = (even + odd) / SQRT2
        c[h:m] = (even - odd) / SQRT2
        m = h

    sigma = float(np.median(np.abs(c[n // 2:])) / 0.6745)  # MAD del nivel más fino
    lam = mult * sigma * np.sqrt(2.0 * np.log(n))
    d = c[1:]
    c[1:] = np.sign(d) * np.maximum(np.abs(d) - lam, 0.0)

    m = 2
    while m <= n:  # transformada inversa
        h = m // 2
        a = c[:h].copy()
        dd = c[h:m].copy()
        c[0:m:2] = (a + dd) / SQRT2
        c[1:m:2] = (a - dd) / SQRT2
        m *= 2

    var_raw = float(np.var(x))
    nf = 1.0 - float(np.var(c)) / var_raw if var_raw > 0 else 0.0
    return float(c[-1]), sigma, float(min(max(nf, 0.0), 1.0))


def wavelet_slope_z(log_close, window, slope_len, mult):
    """Pendiente del precio denoised en unidades de sigma de ruido.

    Devuelve (slope_z, noise_fraction) o (None, None) si no hay datos suficientes.
    """
    if len(log_close) < window + slope_len:
        return None, None
    den_now, sigma, nf = haar_denoise(log_close[-window:], mult)
    den_prev, _, _ = haar_denoise(log_close[-window - slope_len:-slope_len], mult)
    if sigma <= 0:
        return 0.0, nf
    return (den_now - den_prev) / sigma, nf


def bollinger(close: pd.Series, length: int, mult: float):
    basis = close.rolling(length).mean()
    dev = mult * close.rolling(length).std(ddof=0)  # igual que ta.stdev de TradingView
    return basis, basis + dev, basis - dev


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean()


def percent_rank(series: pd.Series, pos: int, lookback: int) -> float:
    """Percentil (0-100) del valor en `pos` frente a las `lookback` barras anteriores."""
    window = series.iloc[max(0, pos - lookback):pos]
    if len(window) == 0:
        return 100.0
    return float((window <= series.iloc[pos]).mean() * 100.0)


if __name__ == "__main__":
    # Autotest: con umbral 0 la reconstrucción debe ser exacta (transformada ortonormal).
    rng = np.random.default_rng(1)
    x = np.cumsum(rng.normal(size=64))
    den, sigma, nf = haar_denoise(x, mult=0.0)
    assert abs(den - x[-1]) < 1e-9, "Reconstrucción no exacta"
    print("OK: reconstrucción exacta | sigma=%.4f nf=%.3f" % (sigma, nf))
