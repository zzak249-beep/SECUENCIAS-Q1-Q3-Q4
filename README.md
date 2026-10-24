# QT Scanner v6 (BingX / Binance Futures -> Telegram)

Bot que escanea los perpetuos USDT de **BingX** (y opcionalmente Binance) cada 5 minutos con la logica
"QT Chain Map" (cadenas de cuartos, barrido del Q1 del ciclo de 90 min, True Opens)
y envia por Telegram señales **LONG / SHORT listas para abrir a mano**.
**Solo avisa, no opera. No es consejo financiero.**

## Que llega a Telegram

```
🟢 LONG · SOL-USDT · BingX   ⭐⭐⭐⭐⭐☆☆☆ 5/8

📍 Entrada: 148.32  (mercado o limit)
     No entres por encima de 148.55
     Precio ahora: 148.36
🛑 SL: 147.55  (-0.52%)
🎯 TP1: 149.09  (+0.52% · 1.0R)  → cierra 50% y SL a entrada
🏁 TP2: 149.86  (+1.04% · 2.0R)  → cierra el resto

💼 Cuenta 1000 USDT · riesgo 1.0% (10.00 USDT)
     Cantidad: 12 | nocional ~1780 USDT | apalancamiento ≥ 2x

🧩 Cadena: Prof 4 | Daily a Nano | Q2
🧭 Sesgo: precio bajo TO Daily 149.10 y TO Sesión 148.90
🧹 Barrido del minimo del Q1 (147.80) y vela de confirmacion
📊 Volumen vela x1.8 · Tendencia 1H a favor
₿ BTC 1H: alcista
🔨 Ruptura de estructura confirmada (rompe el extremo de las 3 velas previas)
💸 Costes ida y vuelta ≈ 0.27R (comision 0.05% + deslizamiento 0.02% por lado)
🕐 NY AM · vela 5m cerrada 2026-10-23 08:55 NY
[📈 BingX] [📊 TradingView]
```

Despues el bot **responde a ese mismo mensaje** cuando toca TP1 (mueve SL a entrada),
TP2, SL, o si la señal caduca. A las 18:00 NY manda un resumen del dia en R, y cada `HEARTBEAT_HOURS` (8 h) un aviso
"sigo activo" que dice que filtro esta frenando mas señales.
Los resultados son simulados con velas de 5m, **netos de comisiones y deslizamiento** (si SL y TP caen en la misma vela cuenta el SL).

Cómo usarlo a mano: abre la moneda en BingX (el botón te lleva directo), entra a mercado (o limit en la entrada)
**solo si el precio no ha pasado del nivel "No entres..."**, pon el SL y los TP del mensaje, y usa la
cantidad indicada (ajusta el apalancamiento al minimo sugerido en margen aislado).

## Qué mejora la v6

**Estrategia y costes**
- **Costes realistas:** `SLIPPAGE_PCT` (0.02% por lado) se suma a la comision en el filtro `MAX_FEE_R`, en el R de cada
  señal y en todos los resultados simulados. Antes el bot asumia entrar y salir al precio exacto.
- **Ruptura de estructura (`REQUIRE_MSS`)**: exige que la vela de confirmacion cierre mas alla del extremo de las 3 velas
  previas (por debajo del minimo en un SHORT, por encima del maximo en un LONG). Descarta rechazos debiles. Suma 1 punto.
- **Filtro BTC (`BTC_FILTER`)**: no avisa de un SHORT si BTC esta en tendencia 1H alcista, ni de un LONG si esta bajista.
  Las altcoins suelen moverse con BTC; ir en su contra aumenta el riesgo.
- Ambos filtros vienen **desactivados**: no hay datos que digan que mejoran tu configuracion hasta que los pruebes (ver seccion 5).

**Ejecucion manual y riesgo**
- **Precio fresco (`FRESH_CHECK`)**: antes de enviar, el bot lee el precio actual. Si ya se paso del nivel "No entres...", o ya toco
  el SL, no avisa (el escaneo de ~500 monedas tarda y el precio se mueve). El mensaje muestra "Precio ahora".
- **Limite de exposicion (`MAX_OPEN_PER_SIDE` = 4, `MAX_OPEN_TOTAL` = 8)**: no avisa de mas señales mientras haya demasiadas abiertas
  en seguimiento. Evita acabar con 10 shorts correlacionados que son, en la practica, una sola apuesta contra BTC.

**Validacion**
- `--backtest --compare` ahora incluye variantes con MSS y BTC y termina con una **validacion fuera de muestra**: elige la
  variante con mejor resultado en la 1a mitad del periodo y te muestra como le fue en la 2a, que no uso para elegirla.
  Es la comprobacion que separa "funciona" de "tuvo suerte en estos datos".
- El backtest funciona con BingX y con Binance (`--exchange`), y descarga BTC como referencia del filtro BTC.

## Qué monedas escanea (BingX)
- Lee la lista de contratos de BingX (`/openApi/swap/v2/quote/contracts`) y analiza los perpetuos **USDT en
  estado activo**, ordenados por volumen 24h. La lista se refresca cada 30 minutos, asi que las monedas nuevas entran solas.
- `MIN_QUOTE_VOLUME` (por defecto **1.000.000 USDT** en BingX) descarta monedas ilíquidas, donde una mecha
  puede dar señales falsas y el slippage te come el SL. Pon `MIN_QUOTE_VOLUME=0` para analizar **todas**.
- BingX tambien lista perpetuos de acciones, materias primas y forex (nombres que empiezan por `NC` y
  contienen `2USD`). Se excluyen por defecto; activalos con `INCLUDE_NON_CRYPTO=true`.
- Velas de 5m desde `/openApi/swap/v3/quote/klines`; si esa ruta falla, el bot prueba la v2 y recuerda la que funciona.
- Las peticiones se reparten a `BINGX_RPS` por segundo (8 por defecto): ~500 monedas tardan ~1 minuto por
  escaneo, dentro del ciclo de 5 minutos. Si ves errores de limite, baja `BINGX_RPS`.
- `EXCHANGE=both` escanea BingX y Binance; si salta la misma moneda en los dos, solo avisa una vez.

## 1. Subir a GitHub
1. Crea un repositorio **privado** en https://github.com/new (sin README).
2. Renombra `_gitignore` -> `.gitignore` y `_env.example` -> `.env.example`.
3. En esta carpeta:
   ```bash
   git init
   git add .
   git commit -m "QT scanner v6"
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
3. **Region:** si en los logs sale error 451/403 del exchange, cambia la region del
   servicio a Europa (Settings -> Region). Los servidores de EE. UU. suelen estar bloqueados.
4. Antes de dejarlo corriendo, comprueba la conexion en local con `python qt_scanner_bot.py --check`.
5. Es un proceso de fondo: no necesita dominio publico ni puerto.
6. Mira **Deployments -> View logs**. Deberias ver "N monedas a escanear" y recibir
   en Telegram el mensaje "QT Scanner iniciado".

### Variables recomendadas (pegar en Raw Editor)
```
EXCHANGE=bingx
TELEGRAM_TOKEN=PON_AQUI_TU_TOKEN
TELEGRAM_CHAT_ID=PON_AQUI_TU_CHAT_ID
ACCOUNT_SIZE=1000
RISK_PER_TRADE_PCT=1
MAX_LEVERAGE=10
REQUIRE_CHAIN=false
SESSIONS=1,2,3
MIN_SCORE=4
MAX_RISK_PCT=3
MAX_FEE_R=0.35
FEE_PCT=0.05
SLIPPAGE_PCT=0.02
SYMBOL_COOLDOWN_MIN=90
MIN_QUOTE_VOLUME=1000000
BINGX_RPS=8
FRESH_CHECK=true
MAX_OPEN_PER_SIDE=4
MAX_OPEN_TOTAL=8
RR=2
PARTIAL_R=1
PARTIAL_PCT=50
ENTRY_CHASE_R=0.3
TRACK_SIGNALS=true
TRACK_HOURS=6
DAILY_SUMMARY_HOUR=18
HEARTBEAT_HOURS=8
```
`FEE_PCT` es la comision por lado de tu cuenta en el exchange: revisa tu tarifa real y ajustala
(entrar con ordenes limit, como maker, cuesta menos).

### Por qué `REQUIRE_CHAIN=false`
Con `REQUIRE_CHAIN=true`, `LOWEST_LEVEL=90 Min` y `NEED_LEVEL=Daily` solo hay cadena en ~8% de las ventanas
de entrada (medido con el propio codigo: `quarters` + `detect_chain` sobre 8 semanas) y hay dias enteros sin ninguna.
Con `LOWEST_LEVEL=Nano` y `NEED_LEVEL=Ninguno` sube a ~19%. Esos numeros dicen **cuantas señales** permite el filtro,
no si son **mejores**: eso lo decide el backtest (seccion 5). Con `REQUIRE_CHAIN=false` la cadena suma puntos
en vez de bloquear, y `MIN_SCORE` hace el filtro de calidad.

### Preset "mas señales"
```
REQUIRE_CHAIN=false
BIAS_MODE=none
SESSIONS=0,1,2,3
MIN_SCORE=3
MIN_QUOTE_VOLUME=0
```
Con `BIAS_MODE=none` desaparece el filtro de True Opens, asi que usa `MIN_SCORE` para quedarte con las mejores.

### Preset "calidad" (menos señales, mas selectivas)
```
SESSIONS=2
MIN_SCORE=5
MAX_RISK_PCT=1.5
MAX_SIGNALS_PER_DAY=4
TREND_FILTER=true
```

### Preset "candidato a validar" (filtros nuevos)
```
REQUIRE_MSS=true
BTC_FILTER=true
MIN_SCORE=4
```
No lo actives por intuicion: ejecuta primero `--backtest --days 30 --top 60 --compare` y mira la validacion
fuera de muestra (seccion 5).

### Todas las variables

| Variable | Defecto | Que hace |
|---|---|---|
| `EXCHANGE` | bingx | `bingx`, `binance` o `both` |
| `TELEGRAM_TOKEN` | – | **Obligatoria.** Token de @BotFather |
| `TELEGRAM_CHAT_ID` | – | **Obligatoria.** Uno o varios ids separados por coma |
| `ACCOUNT_SIZE` | 0 | Tu cuenta en USDT. Con 0 no se muestra cantidad |
| `RISK_PER_TRADE_PCT` | 1 | % de cuenta arriesgado por trade |
| `MAX_LEVERAGE` | 10 | Tope de apalancamiento sugerido |
| `SESSIONS` | 1,2 | 0 Asia, 1 Londres, 2 NY AM, 3 NY PM |
| `MIN_SCORE` | 0 | Puntuacion minima 0-8 |
| `MAX_RISK_PCT` | 3 | Descarta señales con SL a mas de X% (0 = off) |
| `SYMBOL_COOLDOWN_MIN` | 90 | Minutos sin repetir señal en la misma moneda |
| `MAX_SIGNALS_PER_DAY` | 0 | Limite diario (0 = sin limite) |
| `MAX_SIGNALS_PER_SCAN` | 10 | Maximo de señales por escaneo |
| `MIN_QUOTE_VOLUME` | auto | Volumen 24h minimo (USDT). Auto = 1M en BingX, 20M en Binance. `0` = todas |
| `INCLUDE_NON_CRYPTO` | false | BingX: incluir perpetuos de acciones/materias primas/forex |
| `MAX_SYMBOLS` | 0 | Limita nº de monedas (0 = todas las que pasen el filtro) |
| `EXCLUDE_SYMBOLS` | – | Monedas a ignorar, ej. `DOGE-USDT,PEPEUSDT` (vale con o sin guion) |
| `ALLOW_LONG` / `ALLOW_SHORT` | true | Activa/desactiva cada direccion |
| `RR` | 2 | TP2 en multiplos de R |
| `PARTIAL_R` | 1 | TP1 en multiplos de R |
| `PARTIAL_PCT` | 50 | % que se cierra en TP1 |
| `ENTRY_CHASE_R` | 0.3 | Tope para no perseguir el precio (en R) |
| `SL_BUF_ATR` | 0.1 | Colchon del SL en ATR |
| `FEE_PCT` | 0.05 | Comision por lado en % |
| `SLIPPAGE_PCT` | 0.02 | Deslizamiento estimado por lado en % |
| `MAX_FEE_R` | 0.35 | Descarta señales cuyos costes ida y vuelta superen X R (0 = off) |
| `FRESH_CHECK` | true | Comprueba el precio actual antes de avisar |
| `MAX_OPEN_PER_SIDE` | 4 | Maximo de señales abiertas por lado (0 = off) |
| `MAX_OPEN_TOTAL` | 8 | Maximo de señales abiertas en total (0 = off) |
| `REQUIRE_MSS` | false | Exige ruptura de estructura en la vela de confirmacion |
| `BTC_FILTER` | false | No ir en contra de la tendencia 1H de BTC |
| `TREND_FILTER` | false | Exige tendencia 1H a favor (precio vs EMA y pendiente) |
| `TREND_EMA_BARS` | 240 | EMA en velas de 5m (240 ≈ EMA 20 de 1H) |
| `MIN_REL_VOL` | 0 | Volumen de la vela de confirmacion / media de 20 velas (0 = off) |
| `HEARTBEAT_HOURS` | 8 | Aviso "sigo activo" + filtros que mas frenan (0 = off) |
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
| `BINGX_RPS` | 8 | Peticiones por segundo a BingX |
| `SCAN_DELAY_SEC` | 5 | Segundos de espera tras el cierre de la vela de 5m |
| `STATE_FILE` | state.json | Ruta del estado (ver Notas) |
| `BINGX_BASE_URL` | https://open-api.bingx.com | Base de la API de BingX (respaldo automatico: .pro) |
| `BINANCE_BASE_URL` | https://fapi.binance.com | Base de la API de Binance |
| `LOG_LEVEL` | INFO | Nivel de logs |

Si una variable tiene un valor invalido (p. ej. `BIAS_MODE=xyz`) el bot no arranca y
lo explica en los logs.

### Puntuacion de la señal (0-8)
+1 hay cadena abierta · +1 cadena profunda (4+ ciclos) · +1 sesgo True Open confirmado ·
+1 barrido limpio (0.3-1.5 ATR) · +1 vela de confirmacion fuerte (cuerpo ≥ 60%) ·
+1 tendencia 1H a favor · +1 volumen de la vela ≥ 1.5x la media · +1 ruptura de estructura.

### Costes
Con una comision de 0.05% por lado y 0.02% de deslizamiento, entrar y salir a mercado cuesta ≈ 0.14% del nocional.
Con un SL del 0.5% eso son **0.28R por trade** que se comen la ventaja. Por eso `MAX_FEE_R` descarta las señales con SL
muy corto, y todos los resultados simulados (resumen diario y backtest) son netos de costes.
Usar ordenes limit en la entrada (comision maker, normalmente menor) reduce ese coste.

## 4. Pruebas locales
```bash
pip install -r requirements.txt
python qt_scanner_bot.py --selftest          # logica offline (48 comprobaciones)
python qt_scanner_bot.py --check             # conexion real: monedas, precision, velas, precio y tiempo estimado
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...
python qt_scanner_bot.py --test-telegram     # mensaje de prueba + señal de ejemplo
python qt_scanner_bot.py --once --dry-run    # un escaneo, solo imprime (no guarda estado)
```
`--once --dry-run` imprime tambien el **embudo de filtros** del escaneo: cuantas oportunidades hay y
que filtro bloquea mas.

## 5. Validar la estrategia con datos reales (backtest)
Necesita acceso al exchange (en local normalmente funciona; si tu pais lo bloquea, usa una VPN o ejecutalo
desde la consola de Railway con region Europa).
```bash
python qt_scanner_bot.py --backtest --days 30                       # tu configuracion + embudo de filtros
python qt_scanner_bot.py --backtest --days 30 --compare             # ademas, 15 variantes + validacion fuera de muestra
python qt_scanner_bot.py --backtest --days 30 --top 80 --compare    # mas monedas (defecto: 40 mas liquidas)
python qt_scanner_bot.py --backtest --exchange binance --days 30    # el mismo test con datos de Binance
```
Lee tus variables de entorno, asi que prueba exactamente lo que desplegarias. Muestra:
1. **Embudo:** cuantas oportunidades hay y que filtros, por si solos, mas señales bloquean.
2. **Resultados** por variante: n, % acierto, R medio neto de costes, intervalo ±95%, profit factor,
   suma de R y el R medio de la **1a y 2a mitad** del periodo (si cambia de signo, es ruido).
3. **Validacion fuera de muestra:** la variante elegida con la 1a mitad, juzgada en la 2a.
4. **Gestion del trade:** mismas entradas con distintos TP / parcial.
5. Desglose por sesion, lado y puntuacion.

Como leerlo: fiate de una variante solo si tiene **n ≥ 100-200**, R neto positivo en **las dos mitades**, un ±95% que
no incluya el 0 y supera la validacion fuera de muestra. Probar 15 variantes produce falsos positivos por azar.
Si ninguna lo cumple, la conclusion honesta es que **no hay evidencia de ventaja** con esos datos: no actives dinero
real. Confirma la elegida unos dias en papel/demo. Un backtest no garantiza resultados futuros.

## Notas
- La integracion con BingX se ha probado con respuestas simuladas, no contra el servidor real: ejecuta
  `--check` primero. Si BingX cambia el formato de velas o los limites de peticiones, el bot lo dira en los logs.
  El historico de BingX (`--backtest`) usa `startTime`/`endTime`; si no devuelve datos, prueba `--exchange binance`.
- El sistema de archivos de Railway es efimero: `state.json` (duplicados, señales en
  seguimiento y estadisticas) se pierde al redesplegar. Para conservarlo, añade un
  **Volume** con mount path `/data` y pon `STATE_FILE=/data/state.json`.
- Solo se analiza la ultima vela cerrada de 5m, asi que el riesgo de duplicados es minimo.
- Los avisos de TP/SL se comprueban cada 5 minutos con velas cerradas: pueden llegar
  con hasta 5 min de retraso. Pon tus TP/SL como ordenes en el exchange, no dependas del aviso.
- `FRESH_CHECK` y los limites de exposicion solo actuan en vivo; el backtest no los modela.
- No incluye el filtro SMT con 2o activo, True Open Weekly ni ventanas de noticias.
- El backtest entra al cierre de la vela (el deslizamiento se aproxima con `SLIPPAGE_PCT`) y cuenta el SL primero si SL y TP caen en la misma vela.
- Comprueba las señales en papel/demo antes de arriesgar dinero real.
