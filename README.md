# QT Scanner v4 (Binance Futures -> Telegram)

Bot que escanea los perpetuos USDT de Binance Futures cada 5 minutos con la logica
"QT Chain Map" (cadenas de cuartos, barrido del Q1 del ciclo de 90 min, True Opens)
y envia por Telegram señales **LONG / SHORT listas para abrir a mano**.
**Solo avisa, no opera. No es consejo financiero.**

## Que llega a Telegram

```
🟢 LONG · SOLUSDT   ⭐⭐⭐⭐☆ 4/5

📍 Entrada: 148.32  (mercado o limit)
     No entres por encima de 148.55
🛑 SL: 147.55  (-0.52%)
🎯 TP1: 149.09  (+0.52% · 1.0R)  → cierra 50% y SL a entrada
🏁 TP2: 149.86  (+1.04% · 2.0R)  → cierra el resto

💼 Cuenta 1000 USDT · riesgo 1.0% (10.00 USDT)
     Cantidad: 12 | nocional ~1780 USDT | apalancamiento ≥ 2x

🧩 Cadena: Prof 4 | Daily a Nano | Q2
🧭 Sesgo: precio bajo TO Daily 149.10 y TO Sesión 148.90
🧹 Barrido del minimo del Q1 (147.80) y vela de confirmacion
🕐 NY AM · vela 5m cerrada 2026-10-23 08:55 NY
[📈 Binance Futures] [📊 TradingView]
```

Despues el bot **responde a ese mismo mensaje** cuando toca TP1 (mueve SL a entrada),
TP2, SL, o si la señal caduca. A las 18:00 NY manda un resumen del dia en R.
Los resultados son simulados con velas de 5m (si SL y TP caen en la misma vela cuenta el SL).

Cómo usarlo a mano: abre la moneda en Binance, entra a mercado (o limit en la entrada) **solo
si el precio no ha pasado del nivel "No entres..."**, pon el SL y los TP del mensaje, y usa la
cantidad indicada (ajusta el apalancamiento al minimo sugerido en margen aislado).

## 1. Subir a GitHub
1. Crea un repositorio **privado** en https://github.com/new (sin README).
2. Renombra `_gitignore` -> `.gitignore` y `_env.example` -> `.env.example`.
3. En esta carpeta:
   ```bash
   git init
   git add .
   git commit -m "QT scanner v4"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/qt-scanner.git
   git push -u origin main
   ```
   Nunca subas tu token de Telegram: `.env` ya esta en `.gitignore`.

## 2. Telegram
1. Habla con @BotFather, `/newbot`, copia el **token**.
2. Escribe un mensaje a tu bot y abre `https://api.telegram.org/bot<TOKEN>/getUpdates`
   para ver tu **chat id** (`"chat":{"id":123456789}`).
3. Para un canal/grupo: añade el bot como administrador y usa el id (empieza por `-100...`).
   Puedes poner varios chats en `TELEGRAM_CHAT_ID` separados por coma.

## 3. Desplegar en Railway
1. https://railway.com -> **New Project -> Deploy from GitHub repo** -> elige el repo.
   Railway detecta el `Dockerfile` y lo construye solo.
2. Pestaña **Variables** -> **Raw Editor** y pega el bloque de abajo (cambia token, chat id y cuenta).
3. **Region:** si en los logs sale error 451/403 de Binance, cambia la region del
   servicio a Europa (Settings -> Region). Los servidores de EE. UU. suelen estar bloqueados.
4. Es un proceso de fondo: no necesita dominio publico ni puerto.
5. Mira **Deployments -> View logs**. Deberias ver "N monedas a escanear" y recibir
   en Telegram el mensaje "QT Scanner iniciado".

### Variables recomendadas (pegar en Raw Editor)
```
TELEGRAM_TOKEN=PON_AQUI_TU_TOKEN
TELEGRAM_CHAT_ID=PON_AQUI_TU_CHAT_ID
ACCOUNT_SIZE=1000
RISK_PER_TRADE_PCT=1
MAX_LEVERAGE=10
SESSIONS=1,2
MIN_SCORE=3
MAX_RISK_PCT=3
SYMBOL_COOLDOWN_MIN=90
MAX_SIGNALS_PER_DAY=0
MIN_QUOTE_VOLUME=20000000
RR=2
PARTIAL_R=1
PARTIAL_PCT=50
ENTRY_CHASE_R=0.3
TRACK_SIGNALS=true
TRACK_HOURS=6
DAILY_SUMMARY_HOUR=18
```

### Preset "mas señales" (si llegan pocas)
Cambia estas variables:
```
REQUIRE_CHAIN=false
BIAS_MODE=none
SESSIONS=0,1,2,3
MIN_SCORE=2
MIN_QUOTE_VOLUME=10000000
```
Con `BIAS_MODE=none` desaparece el filtro de True Opens, asi que usa `MIN_SCORE` para
quedarte con las mejores.

### Preset "calidad" (menos señales, mas selectivas)
```
SESSIONS=2
MIN_SCORE=4
MAX_RISK_PCT=1.5
MAX_SIGNALS_PER_DAY=4
```

### Todas las variables

| Variable | Defecto | Que hace |
|---|---|---|
| `TELEGRAM_TOKEN` | – | **Obligatoria.** Token de @BotFather |
| `TELEGRAM_CHAT_ID` | – | **Obligatoria.** Uno o varios ids separados por coma |
| `ACCOUNT_SIZE` | 0 | Tu cuenta en USDT. Con 0 no se muestra cantidad |
| `RISK_PER_TRADE_PCT` | 1 | % de cuenta arriesgado por trade |
| `MAX_LEVERAGE` | 10 | Tope de apalancamiento sugerido |
| `SESSIONS` | 1,2 | 0 Asia, 1 Londres, 2 NY AM, 3 NY PM |
| `MIN_SCORE` | 0 | Puntuacion minima 0-5 |
| `MAX_RISK_PCT` | 3 | Descarta señales con SL a mas de X% (0 = off) |
| `SYMBOL_COOLDOWN_MIN` | 90 | Minutos sin repetir señal en la misma moneda |
| `MAX_SIGNALS_PER_DAY` | 0 | Limite diario (0 = sin limite) |
| `MAX_SIGNALS_PER_SCAN` | 10 | Maximo de señales por escaneo |
| `MIN_QUOTE_VOLUME` | 20000000 | Volumen 24h minimo (USDT) |
| `MAX_SYMBOLS` | 0 | Limita nº de monedas (0 = todas las que pasen el filtro) |
| `EXCLUDE_SYMBOLS` | – | Monedas a ignorar, ej. `USDCUSDT,BTCDOMUSDT` |
| `ALLOW_LONG` / `ALLOW_SHORT` | true | Activa/desactiva cada direccion |
| `RR` | 2 | TP2 en multiplos de R |
| `PARTIAL_R` | 1 | TP1 en multiplos de R |
| `PARTIAL_PCT` | 50 | % que se cierra en TP1 |
| `ENTRY_CHASE_R` | 0.3 | Tope para no perseguir el precio (en R) |
| `SL_BUF_ATR` | 0.1 | Colchon del SL en ATR |
| `TRACK_SIGNALS` | true | Avisos de TP1/TP2/SL/caducidad |
| `TRACK_HOURS` | 6 | Horas hasta que una señal caduca |
| `DAILY_SUMMARY_HOUR` | 18 | Hora NY del resumen (-1 = off) |
| `REQUIRE_CHAIN` | true | Exigir cadena de cuartos abierta |
| `BIAS_MODE` | true_opens | `none` o `true_opens` |
| `TO_LOGIC` | manipulacion | `manipulacion` (LONG bajo TO, SHORT sobre TO) o `tendencia` |
| `LOWEST_LEVEL` | 90 Min | `90 Min`, `Micro` o `Nano` |
| `NEED_LEVEL` | Daily | `Ninguno` o un nivel que debe estar en la cadena |
| `MIN_DEPTH` | 3 | Profundidad minima de cadena |
| `CHAIN_KIND` | Ambas | `Ambas`, `Normal` o `Puente` |
| `BODY_PCT` | 40 | % minimo de cuerpo de la vela de confirmacion |
| `MAX_BARS_SWEEP` | 6 | Velas maximas desde el barrido |
| `MIN_SWEEP_ATR` / `MAX_SWEEP_ATR` | 0.2 / 3 | Profundidad del barrido en ATR |
| `MIN_Q1_ATR` / `MAX_Q1_ATR` | 0.8 / 10 | Rango valido del Q1 en ATR |
| `MIN_RISK_TICKS` | 4 | Riesgo minimo en ticks |
| `STATE_FILE` | state.json | Ruta del estado (ver Notas) |
| `BINANCE_BASE_URL` | https://fapi.binance.com | Base de la API |
| `LOG_LEVEL` | INFO | Nivel de logs |

Si una variable tiene un valor invalido (p. ej. `BIAS_MODE=xyz`) el bot no arranca y
lo explica en los logs.

### Puntuacion de la señal (0-5)
+1 hay cadena abierta · +1 cadena profunda (4+ ciclos) · +1 sesgo True Open confirmado ·
+1 barrido limpio (0.3-1.5 ATR) · +1 vela de confirmacion fuerte (cuerpo ≥ 60%).

## 4. Pruebas locales
```bash
pip install -r requirements.txt
python qt_scanner_bot.py --selftest          # logica offline (17 comprobaciones)
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...
python qt_scanner_bot.py --test-telegram     # mensaje de prueba + señal de ejemplo
python qt_scanner_bot.py --once --dry-run    # un escaneo, solo imprime (no guarda estado)
```

## Notas
- El sistema de archivos de Railway es efimero: `state.json` (duplicados, señales en
  seguimiento y estadisticas) se pierde al redesplegar. Para conservarlo, añade un
  **Volume** con mount path `/data` y pon `STATE_FILE=/data/state.json`.
- Solo se analiza la ultima vela cerrada de 5m, asi que el riesgo de duplicados es minimo.
- Los avisos de TP/SL se comprueban cada 5 minutos con velas cerradas: pueden llegar
  con hasta 5 min de retraso. Pon tus TP/SL como ordenes en Binance, no dependas del aviso.
- No incluye el filtro SMT con 2o activo, EMA 1H, True Open Weekly ni ventanas de noticias.
- Comprueba las señales en papel/demo antes de arriesgar dinero real.
