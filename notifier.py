"""Envío de mensajes a Telegram y formato de señales."""
import html
import logging
import time

import requests

log = logging.getLogger("notifier")


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


def fmt_qty(q: float) -> str:
    if q >= 100:
        return f"{q:,.1f}"
    if q >= 1:
        return f"{q:.3f}"
    return f"{q:.6f}"


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            print("\n[TELEGRAM NO CONFIGURADO - mensaje en consola]\n" + text + "\n")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=15)
                if r.status_code == 200:
                    return True
                if r.status_code == 429:  # límite de Telegram
                    wait = r.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(min(int(wait), 30))
                    continue
                log.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
                return False
            except requests.RequestException as e:
                log.warning("Telegram error (intento %d): %s", attempt + 1, e)
                time.sleep(2)
        return False


def format_signal(s: dict, cfg) -> str:
    emoji = "🟢" if s["side"] == "LONG" else "🔴"
    sym = html.escape(s["symbol"].split(":")[0])
    lines = [
        f"{emoji} <b>{s['side']} · {sym}</b> ({s['timeframe']})",
        f"⭐ Score: <b>{s['score']:.0f}/100</b>",
        "",
        f"📍 Entrada ref: <code>{fmt_price(s['entry'])}</code>",
        f"🛑 Stop: <code>{fmt_price(s['sl'])}</code>",
        f"🎯 TP1 ({cfg.tp1_r:g}R): <code>{fmt_price(s['tp1'])}</code>",
        f"🎯 TP2 ({cfg.tp2_r:g}R): <code>{fmt_price(s['tp2'])}</code>",
        "",
        f"💼 Tamaño sugerido: <code>{fmt_qty(s['qty'])}</code> monedas "
        f"(~{s['notional']:,.0f} USDT, {s['leverage']:.1f}x)",
        f"⚠️ Riesgo: ~{s['risk_usd']:.2f} USDT ({cfg.risk_pct:g}% de {cfg.account_usd:,.0f})"
        + (" · limitado por apalancamiento máx." if s["capped"] else ""),
        f"⏱ Cierra a las {cfg.max_hold_hours}h si no toca SL/TP",
        "",
    ]
    lines += [f"• {html.escape(r)}" for r in s["reasons"]]
    lines += [
        "",
        "<i>Señal informativa, no consejo financiero. Verifica el precio actual antes de "
        "entrar y ejecuta manualmente con stop.</i>",
    ]
    return "\n".join(lines)


def format_digest(watch_list: list, cfg) -> str:
    lines = [f"👀 <b>Watchlist: monedas en squeeze ({cfg.timeframe})</b>", ""]
    for w in watch_list:
        sym = html.escape(w["symbol"].split(":")[0])
        lines.append(f"• {sym} — percentil ancho BB: {w['depth_pct']:.0f}")
    lines += ["", "<i>Compresión de volatilidad: posible movimiento fuerte, sin dirección definida.</i>"]
    return "\n".join(lines)
