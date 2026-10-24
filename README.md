# QT Scanner v6 (BingX Futures -> Telegram)

Bot que escanea **todos los perpetuos USDT de BingX** (cripto; los perpetuos TradFi de acciones/materias primas se
excluyen por defecto) cada 5 minutos con la logica
"QT Chain Map" (cadenas de cuartos, barrido del Q1 del ciclo de 90 min, True Opens)
y envia por Telegram señales **LONG / SHORT listas para abrir a mano**.
**Solo avisa, no opera. No es consejo financiero.**

## Que llega a Telegram

```
🟢 LONG · SOL-USDT   ⭐⭐⭐⭐⭐☆☆ 5/7

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
📊 Volumen vela x1.8 · Tendencia 1H a favor
💸 Costes ida y vuelta ≈ 0.28R (comision 0.05% + deslizamiento 0.02% por lado)
💧 Liquidez: spread 0.020% · impacto de 1780 USDT 0.015% ≈ 0.06R ida y vuelta
🕐 NY AM · vela 5m cerrada 2026-10-23 08:55 NY
[📈 BingX Futures] [📊 TradingView]
```

Despues el bot **responde a ese mismo mensaje** cuando toca TP1 (mueve SL a entrada),
TP2, SL, o si la señal caduca. A las 18:00 NY manda un resumen del dia en R, y cada `HEARTBEAT_HOURS` (8 h) un aviso "sigo activo"
que dice que filtro esta frenando mas señales.
Los resultados son simulados con velas de 5m, **netos de comisiones** (si SL y TP caen en la misma vela cuenta el SL).

Cómo usarlo a mano: abre la moneda en BingX, entra a mercado (o limit en la entrada) **solo
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
3. **Region:** si en los logs sale error 451/403 del exchange, cambia la region del
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

### Preset "equilibrado" (recomendado si llegan pocas señales)
La cadena deja de ser obligatoria (pasa a sumar puntos) y el filtro de calidad lo hace la puntuacion:
```
REQUIRE_CHAIN=false
SESSIONS=1,2,3
MIN_SCORE=4
```
**Por que:** con `REQUIRE_CHAIN=true`, `LOWEST_LEVEL=90 Min` y `NEED_LEVEL=Daily` solo hay ventanas validas
**martes y miercoles** (el ciclo semanal solo cubre lun-jue y la cadena de 3 niveles exige que Weekly
coincida). Lunes, jueves, viernes y fin de semana: cero señales posibles. Se midio con el propio codigo
(`quarters` + `detect_chain` sobre 8 semanas): 6% de las ventanas con tu configuracion, 0% de lunes y jueves a domingo.
Con `REQUIRE_CHAIN=false` se emiten señales todos los dias. Valida el preset con `--backtest --compare`
(seccion 5) antes de fiarte de el.

### Preset "mas señales"
```
REQUIRE_CHAIN=false
BIAS_MODE=none
SESSIONS=0,1,2,3
MIN_SCORE=3
MIN_QUOTE_VOLUME=10000000
```
Con `BIAS_MODE=none` desaparece el filtro de True Opens, asi que usa `MIN_SCORE` para
quedarte con las mejores.

### Preset "calidad" (menos señales, mas selectivas)
```
SESSIONS=2
MIN_SCORE=5
MAX_RISK_PCT=1.5
MAX_SIGNALS_PER_DAY=4
TREND_FILTER=true
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
| `MIN_SCORE` | 0 | Puntuacion minima 0-7 |
| `MAX_RISK_PCT` | 3 | Descarta señales con SL a mas de X% (0 = off) |
| `SYMBOL_COOLDOWN_MIN` | 90 | Minutos sin repetir señal en la misma moneda |
| `MAX_SIGNALS_PER_DAY` | 0 | Limite diario (0 = sin limite) |
| `MAX_SIGNALS_PER_SCAN` | 10 | Maximo de señales por escaneo |
| `MIN_QUOTE_VOLUME` | 20000000 | Volumen 24h minimo (USDT). **`0` = analizar todas las monedas** (el filtro de liquidez real por señal te protege, ver abajo) |
| `MAX_SYMBOLS` | 0 | Limita nº de monedas (0 = todas las que pasen el filtro) |
| `EXCLUDE_SYMBOLS` | – | Monedas a ignorar, ej. `USDCUSDT,BTCDOMUSDT` |
| `ALLOW_LONG` / `ALLOW_SHORT` | true | Activa/desactiva cada direccion |
| `RR` | 2 | TP2 en multiplos de R |
| `PARTIAL_R` | 1 | TP1 en multiplos de R |
| `PARTIAL_PCT` | 50 | % que se cierra en TP1 |
| `ENTRY_CHASE_R` | 0.3 | Tope para no perseguir el precio (en R) |
| `SL_BUF_ATR` | 0.1 | Colchon del SL en ATR |
| `TREND_FILTER` | false | Exige tendencia 1H a favor (precio vs EMA y pendiente) |
| `TREND_EMA_BARS` | 240 | EMA en velas de 5m (240 ≈ EMA 20 de 1H) |
| `MIN_REL_VOL` | 0 | Volumen de la vela de confirmacion / media de 20 velas (0 = off) |
| `FEE_PCT` | 0.05 | Comision taker por lado en % (tarifa base de perpetuos BingX y Binance; maker 0.02) |
| `SLIP_PCT` | 0.02 | Deslizamiento estimado por lado en % (se suma a la comision en backtest, autoajuste y filtro de costes) |
| `MAX_FEE_R` | 0.35 | Descarta señales cuyos costes (comision + deslizamiento, ida y vuelta) superen X R (0 = off) |
| `MAX_SLIP_R` | 0.20 | Señal en vivo: descarta si el impacto **real** en el libro de ordenes (ida y vuelta, con el tamaño de tu orden) supera X R (0 = off) |
| `MAX_OPEN_PER_SIDE` | 5 | Maximo de señales abiertas en el mismo lado (las altcoins se mueven juntas; 0 = sin limite) |
| `BTC_FILTER` | false | No operar contra la tendencia 1H de BTC (LONG si BTC no es bajista, SHORT si no es alcista) |
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
| `STATE_FILE` | state.json | Ruta del estado (ver Notas) |
| `EXCHANGE` | bingx | `bingx` o `binance` |
| `BASE_URL` | segun exchange | Base de la API (BingX: https://open-api.bingx.com) |
| `INCLUDE_TRADFI` | false | BingX: incluir perpetuos de acciones, materias primas, indices y forex |
| `BINGX_API_KEY` / `BINGX_API_SECRET` | – | Opcionales. Los datos de mercado suelen ir sin clave; si BingX pide firma, usa una clave de **solo lectura** |
| `AUTOTUNE` | true | Autoajuste periodico (ver seccion 6) |
| `AUTOTUNE_MODE` | apply | `apply` aplica el cambio · `suggest` solo lo propone por Telegram |
| `AUTOTUNE_HOURS` | 24 | Cada cuantas horas se reajusta |
| `AUTOTUNE_DAYS` | 21 | Dias de historico (2/3 entrenamiento, 1/3 validacion) |
| `AUTOTUNE_TOP` | 0 | Monedas usadas para ajustar (**0 = todas**). Si limitas: 1/3 las mas liquidas + muestra aleatoria rotativa del resto |
| `AUTOTUNE_MAX_MINUTES` | 25 | Presupuesto de tiempo del autoajuste (orden aleatorio, asi un corte no sesga hacia las liquidas) |
| `AUTOTUNE_MIN_TRADES` | 40 | Operaciones simuladas minimas para fiarse de una variante |
| `AUTOTUNE_Z` | 2.2 | Cota inferior = R medio − Z × error estandar (mas alto que antes porque hay mas variantes) |
| `AUTOTUNE_MIN_GAIN_R` | 0.10 | Mejora minima (en R) sobre la actual para cambiar |
| `LOG_LEVEL` | INFO | Nivel de logs |

Si una variable tiene un valor invalido (p. ej. `BIAS_MODE=xyz`) el bot no arranca y
lo explica en los logs.

### Puntuacion de la señal (0-7)
+1 hay cadena abierta · +1 cadena profunda (4+ ciclos) · +1 sesgo True Open confirmado ·
+1 barrido limpio (0.3-1.5 ATR) · +1 vela de confirmacion fuerte (cuerpo ≥ 60%) ·
+1 tendencia 1H a favor · +1 volumen de la vela ≥ 1.5x la media.

### Costes y liquidez (importante si analizas todas las monedas)
1. **Comisiones + deslizamiento estimado:** con 0.05% taker + 0.02% de deslizamiento por lado, entrar y salir a mercado
   cuesta ≈ 0.14% del nocional: con un SL del 0.5% son **0.28R por trade**. `MAX_FEE_R` descarta las señales con SL
   muy corto y todos los resultados simulados (resumen diario, backtest, autoajuste) son netos de estos costes.
2. **Liquidez real en cada señal:** justo antes de enviarla, el bot lee el **libro de ordenes** de esa moneda y calcula
   a que precio medio se ejecutaria una orden **del tamaño que te sugiere** (tu `ACCOUNT_SIZE` y riesgo; 1000 USDT si no lo
   indicas). Si el impacto ida y vuelta supera `MAX_SLIP_R` (0.20R) o el libro no alcanza para llenarla, la señal se
   descarta. Asi puedes analizar *todas* las monedas sin que te lleguen señales sobre monedas donde no podrias
   entrar/salir a un precio razonable. Si el libro no esta disponible, la señal llega marcada "liquidez no verificada".
3. **Concentracion:** las altcoins se mueven casi todas juntas con BTC. `MAX_OPEN_PER_SIDE` limita cuantas señales
   abiertas puedes tener en el mismo lado (con `TRACK_SIGNALS=true`); asi un barrido de BTC no te cuesta 10 SL a la vez.
4. **Filtro BTC** (`BTC_FILTER`, opcional): el autoajuste lo prueba como una variante mas y lo adopta solo si valida.
Usar ordenes limit en la entrada (comision maker 0.02%) reduce el coste.

## 3b. Primera vez: verifica la conexion con BingX
```bash
python qt_scanner_bot.py --check-exchange
```
Comprueba contratos, filtro de volumen, formato de velas, retraso de la ultima vela, historico y **cuanto tardaria
un escaneo de todas las monedas** (debe ser < 240 s). Si algo sale con `!!`, arreglalo antes de desplegar.

## 4. Pruebas locales
```bash
pip install -r requirements.txt
python qt_scanner_bot.py --selftest          # logica offline (79 comprobaciones)
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
python qt_scanner_bot.py --backtest --days 30                 # tu configuracion + embudo de filtros
python qt_scanner_bot.py --backtest --days 30 --compare       # ademas, 10 variantes de la estrategia
python qt_scanner_bot.py --backtest --days 30 --top 80        # mas monedas (defecto: 40 mas liquidas)
```
Lee tus variables de entorno, asi que prueba exactamente lo que desplegarias. Muestra:
1. **Embudo:** cuantas oportunidades hay y que filtros, por si solos, mas señales bloquean.
2. **Resultados** por variante: n, % acierto, R medio neto de comisiones, intervalo ±95%, profit factor,
   suma de R y el R medio de la **1a y 2a mitad** del periodo (si cambia de signo, es ruido).
3. **Gestion del trade:** mismas entradas con distintos TP / parcial.
4. Desglose por sesion, lado y puntuacion.

### Descarga mas rapida del backtest
Usa `--top 40` (defecto) para empezar; con `--top 0` baja todas las monedas y tarda varios minutos.

Como leerlo: fiate de una variante solo si tiene **n ≥ 100-200**, R neto positivo en **las dos mitades** y
un ±95% que no incluya el 0. Probar muchas variantes produce falsos positivos por azar. Confirma la elegida
unos dias en papel/demo. Un backtest no garantiza resultados futuros.

## 6. Autoajuste: "que mejore constantemente"
Cada `AUTOTUNE_HOURS` (24 h) el bot, **en segundo plano**, descarga `AUTOTUNE_DAYS` dias de **todas las monedas**
(`AUTOTUNE_TOP=0`; con el coste medido, ~500 monedas son ≈ 7-10 min de CPU al dia) y simula una rejilla **fija de 216
variantes** (cadena obligatoria u opcional × tendencia 1H × filtro BTC × sesiones × score minimo 3/4/5 × 3 formas de
gestionar el trade), con comisiones y deslizamiento. Despues:

1. Solo es candidata una variante con **≥ 40 operaciones** y R neto **positivo tanto en el tramo de entrenamiento
   (primeros 2/3) como en el de validacion (ultimo 1/3)**, y con la cota inferior (R medio − 2 × error estandar) > 0.
2. Sustituye a la actual solo si mejora esa cota en **≥ 0.10R** (histeresis: evita bailar entre variantes casi iguales).
3. Si **ninguna** cumple, **no inventa nada**: mantiene la configuracion y las señales llevan la marca
   "⚠️ Experimental: sin ventaja validada". No significa que vaya a perder; significa que no hay evidencia de que gane.
4. Cada cambio (y cada vez que cambia el estado de "ventaja validada") llega por Telegram con las cifras antes/despues.

Los parametros adoptados se guardan en `state.json` → **necesitas el Volume** (`STATE_FILE=/data/state.json`, ver Notas)
o se pierden al redesplegar y el bot vuelve a tu configuracion de variables hasta el siguiente ajuste.

Limites que debes conocer:
- Probar 216 variantes genera falsos positivos por azar; las reglas de arriba los reducen, **no los eliminan**.
- **Mira la linea "Por liquidez"** del aviso: muestra el R medio de la configuracion en monedas liquidas, medias y de
  "cola". Si la cola sale claramente negativa, sube `MIN_QUOTE_VOLUME`: las monedas ilíquidas suelen tener mechas y
  barridos espurios que la estrategia confunde con barridos de liquidez reales.
- Reajustar sobre las ultimas 3 semanas persigue el mercado reciente; un cambio de regimen puede invalidar la variante elegida.
- Un autoajuste no crea una ventaja que no existe: sirve para **detectar cuando la hay y cuando no**.
- Si prefieres decidir tu, pon `AUTOTUNE_MODE=suggest` (propone por Telegram, no cambia nada) o `AUTOTUNE=false`.

Para verlo a mano: `python qt_scanner_bot.py --autotune` imprime el ranking de variantes, cuales cumplen las reglas y
la decision, sin aplicar nada.

## Notas
- El sistema de archivos de Railway es efimero: `state.json` (duplicados, señales en
  seguimiento y estadisticas) se pierde al redesplegar. Para conservarlo, añade un
  **Volume** con mount path `/data` y pon `STATE_FILE=/data/state.json`.
- Solo se analiza la ultima vela cerrada de 5m, asi que el riesgo de duplicados es minimo.
- Los avisos de TP/SL se comprueban cada 5 minutos con velas cerradas: pueden llegar
  con hasta 5 min de retraso. Pon tus TP/SL como ordenes en el exchange, no dependas del aviso.
- No incluye el filtro SMT con 2o activo, True Open Weekly ni ventanas de noticias.
- El backtest entra al cierre de la vela (sin deslizamiento) y cuenta el SL primero si SL y TP caen en la misma vela.
- Cada escaneo hace 1 peticion por moneda (499 velas). Con ~500 monedas son ~500 peticiones por escaneo, a ~30/s: unos 20-30 s.
- Si el exchange aun no publico la ultima vela cerrada de una moneda, esa moneda se omite en ese escaneo (no se opera con datos viejos).
- **Monedas con poco volumen:** el backtest usa un deslizamiento fijo (`SLIP_PCT`); el coste real solo se mide en vivo con el libro de ordenes, que el backtest no puede reproducir. `MIN_QUOTE_VOLUME=0` = todas.
- El limite `MAX_OPEN_PER_SIDE` y el filtro de liquidez no estan en el backtest (solo actuan en vivo).
- Comprueba las señales en papel/demo antes de arriesgar dinero real.
