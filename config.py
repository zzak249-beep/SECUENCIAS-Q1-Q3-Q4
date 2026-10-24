"""Configuración centralizada. Todo se controla con variables de entorno."""
import os

try:  # .env solo para uso local; en Railway se usan las Variables del proyecto
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _s(name, default=""):
    return os.getenv(name, str(default)).strip()


def _f(name, default):
    return float(_s(name, default))


def _i(name, default):
    return int(float(_s(name, default)))


def _b(name, default):
    return _s(name, default).lower() in ("1", "true", "yes", "y", "on", "si", "sí")


def _l(name, default=""):
    return [x.strip().upper() for x in _s(name, default).split(",") if x.strip()]


class Config:
    def __init__(self):
        # --- Telegram ---
        self.telegram_token = _s("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = _s("TELEGRAM_CHAT_ID")

        # --- Universo y temporalidades ---
        self.timeframe = _s("TIMEFRAME", "1h")
        self.htf = _s("HTF_TIMEFRAME", "4h")          # temporalidad superior (confluencia)
        self.candles = _i("CANDLES", 300)
        self.top_n = _i("TOP_N", 80)                  # top N por volumen 24h
        self.min_volume_usd = _f("MIN_VOLUME_USD", 5_000_000)
        self.exclude_bases = _l("EXCLUDE_BASES", "USDC,FDUSD,TUSD,BUSD,USDT")
        # Prefijos de activos tradicionales (acciones/commodities/forex) de BingX. Edítalo si ves símbolos raros.
        self.exclude_prefixes = _l("EXCLUDE_PREFIXES", "NCSK,NCCO,NCFX,NCSI")

        # --- Frecuencia ---
        self.scan_interval_min = _i("SCAN_INTERVAL_MIN", 15)
        self.cooldown_hours = _f("COOLDOWN_HOURS", 12)
        self.digest_hours = _f("DIGEST_HOURS", 6)     # 0 = desactivado (lista de monedas en squeeze)
        self.digest_top = _i("DIGEST_TOP", 10)

        # --- Squeeze (Bollinger) ---
        self.bb_len = _i("BB_LEN", 20)
        self.bb_mult = _f("BB_MULT", 2.0)
        self.sqz_lookback = _i("SQZ_LOOKBACK", 100)
        self.sqz_pct = _f("SQZ_PCT", 20)
        self.sqz_mem = _i("SQZ_MEM", 5)

        # --- Wavelet causal (Haar) ---
        self.wavelet_window = _i("WAVELET_WINDOW", 64)  # potencia de 2
        self.wavelet_mult = _f("WAVELET_MULT", 1.0)
        self.slope_len = _i("SLOPE_LEN", 8)
        self.min_slope_z = _f("MIN_SLOPE_Z", 1.0)
        self.require_htf = _b("REQUIRE_HTF", False)    # exigir acuerdo de la TF superior

        # --- Filtros de calidad ---
        self.min_volume_ratio = _f("MIN_VOLUME_RATIO", 1.0)   # vol vela / media 20 velas
        self.max_extension_atr = _f("MAX_EXTENSION_ATR", 2.0) # evita perseguir rupturas ya extendidas
        self.min_score = _f("MIN_SCORE", 60)
        self.allow_long = _b("ALLOW_LONG", True)
        self.allow_short = _b("ALLOW_SHORT", True)

        # --- Riesgo (solo informativo: tú ejecutas manualmente) ---
        self.atr_len = _i("ATR_LEN", 14)
        self.atr_stop = _f("ATR_STOP", 1.5)
        self.tp1_r = _f("TP1_R", 1.5)
        self.tp2_r = _f("TP2_R", 3.0)
        self.account_usd = _f("ACCOUNT_USD", 1000)
        self.risk_pct = _f("RISK_PCT", 1.0)
        self.max_leverage = _f("MAX_LEVERAGE", 3.0)
        self.max_hold_hours = _i("MAX_HOLD_HOURS", 48)

        # --- Otros ---
        self.send_startup = _b("SEND_STARTUP", True)
        self.log_level = _s("LOG_LEVEL", "INFO").upper()

        n = self.wavelet_window
        if n < 8 or (n & (n - 1)) != 0:
            raise ValueError("WAVELET_WINDOW debe ser potencia de 2 (16, 32, 64, 128, 256)")


CFG = Config()
