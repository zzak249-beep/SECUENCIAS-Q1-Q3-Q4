# BingX Scanner Bot → Señales por Telegram

Escanea los perpetuos USDT de BingX más líquidos, detecta **rupturas tras compresión de volatilidad (squeeze de Bollinger)** confirmadas con un **filtro wavelet de Haar causal** (sin repintado) y te envía por Telegram la señal con entrada, stop, TPs y tamaño sugerido. **No ejecuta órdenes**: tú operas manualmente.

> Herramienta informativa. No es asesoría financiera ni garantiza ganancias. Haz paper trading antes de usar dinero real.

## Qué hace

1. Cada `SCAN_INTERVAL_MIN` minutos descarga **todos** los perpetuos USDT (o el top N si pones `TOP_N`) (usa ccxt, API pública, **no necesita claves de BingX**).
2. Solo analiza **velas cerradas** (nunca la vela en formación).
3. Busca: squeeze reciente + ruptura de banda + volumen por encima de la media + pendiente wavelet en la dirección de la ruptura.
4. Puntúa de 0 a 100 (profundidad del squeeze, volumen, fuerza de la pendiente, acuerdo con la TF superior, calidad de la ruptura).
5. Envía las que superan `MIN_SCORE`, con *cooldown* por moneda para no repetir.
6. Cada `DIGEST_HOURS` horas envía una *watchlist* de monedas que están en squeeze ahora mismo.

## Estructura

```
main.py          bucle principal (BingX → análisis → Telegram)
scanner.py       lógica de señal y puntuación
indicators.py    wavelet Haar causal, Bollinger, ATR
notifier.py      Telegram y formato de mensajes
config.py        variables de entorno
requirements.txt · Procfile · railway.json · .env.example · .gitignore
```

## 1) Crear el bot de Telegram

1. En Telegram abre **@BotFather** → `/newbot` → copia el **token**.
2. Abre tu bot nuevo y envíale cualquier mensaje (por ejemplo `hola`).
3. En el navegador abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca `"chat":{"id":123456789` → ese número es tu **TELEGRAM_CHAT_ID**.

## 2) Probar en local (opcional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # rellena TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
python indicators.py        # autotest del wavelet
python main.py
```

Sin Telegram configurado, las señales se imprimen en consola.

## 3) Subir a GitHub

```bash
git init
git add .
git commit -m "BingX scanner bot"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/bingx-scanner-bot.git
git push -u origin main
```

`.env` está en `.gitignore`: **nunca subas tu token**.

## 4) Desplegar en Railway

1. railway.app → **New Project → Deploy from GitHub repo** → elige el repositorio.
2. En **Variables** añade como mínimo `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (el resto es opcional, ver `.env.example`).
3. Railway detecta Python y arranca con `python main.py` (definido en `railway.json`). Es un *worker*, no necesita puerto ni dominio.
4. Revisa **Deployments → Logs**: debes ver `Universo: N símbolos` y recibir el mensaje "Scanner BingX iniciado" en Telegram.

Si los logs muestran errores 403/451 al consultar BingX, cambia la región del servicio en Railway (algunas regiones están restringidas).

## Ajustes útiles (variables)

| Variable | Qué hace |
|---|---|
| `MIN_SCORE` | Sube (70+) para menos señales y más selectivas |
| `TIMEFRAME` / `HTF_TIMEFRAME` | Por defecto 1h y 4h |
| `REQUIRE_HTF=true` | Exige que la TF superior apunte en la misma dirección |
| `TOP_N` | `0` = todas las monedas (por defecto); un número = solo el top N por volumen |
| `MIN_VOLUME_USD` | Volumen 24h mínimo (por defecto 300000); `0` = sin filtro |
| `RATE_LIMIT_MS`, `MAX_RETRIES` | Ritmo de peticiones y reintentos (error 100410 de BingX) |
| `ACCOUNT_USD`, `RISK_PCT`, `MAX_LEVERAGE` | Para calcular el tamaño sugerido (solo informativo) |
| `MAX_EXTENSION_ATR` | Descarta rupturas que ya se alejaron demasiado de la banda |
| `EXCLUDE_PREFIXES` | Prefijos de activos tradicionales de BingX a ignorar; edítalo si ves símbolos raros |

## Cómo ejecutar una señal manualmente

1. Comprueba que el precio actual sigue cerca de la "Entrada ref". Si ya se alejó mucho, **no persigas**.
2. Abre el perpetuo en BingX, apalancamiento bajo (el sugerido), orden limit/market.
3. Coloca **siempre** el stop y el TP al abrir.
4. Cierra manualmente si pasan las horas máximas indicadas.

## Limitaciones honestas

- Tras un squeeze el ATR suele ser pequeño, así que los stops quedan ajustados y el tope de apalancamiento limita el tamaño; el riesgo real puede ser menor al configurado.
- Es una estrategia técnica sin garantía: haz backtest (el `.pine` de TradingView sirve) y paper trading antes de arriesgar capital.
- El estado (cooldowns) vive en memoria: si Railway reinicia el servicio, pueden repetirse señales recientes.
- Haar no es invariante a traslaciones: la línea denoised puede "temblar" ligeramente entre barras.
