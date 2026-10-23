# QT Scanner (Binance Futures -> Telegram)

Bot que escanea los perpetuos USDT de Binance Futures cada 5 minutos con la logica
"QT Chain Map" (cadenas de cuartos, barrido del Q1 del ciclo de 90 min, True Opens)
y envia las entradas por Telegram. **Solo avisa, no opera.** No es consejo financiero.

## 1. Subir a GitHub
1. Crea un repositorio **privado** en https://github.com/new (sin README).
2. En esta carpeta:
   ```bash
   git init
   git add .
   git commit -m "QT scanner"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/qt-scanner.git
   git push -u origin main
   ```
   Nunca subas tu token de Telegram: `.env` ya esta en `.gitignore`.

## 2. Telegram
1. Habla con @BotFather, `/newbot`, copia el **token**.
2. Escribe un mensaje a tu bot y abre `https://api.telegram.org/bot<TOKEN>/getUpdates`
   para ver tu **chat id**.

## 3. Desplegar en Railway
1. https://railway.com -> **New Project -> Deploy from GitHub repo** -> elige el repo.
   Railway detecta el `Dockerfile` y lo construye solo.
2. Pestana **Variables** del servicio: anade `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`
   (y las opcionales de `.env.example` si quieres cambiar filtros).
3. **Region:** si en los logs sale error 451/403 de Binance, cambia la region del
   servicio a Europa (Settings -> Region). Los servidores de EE. UU. suelen estar bloqueados.
4. Es un proceso de fondo: no necesita dominio publico ni puerto.
5. Mira **Deployments -> View logs**. Deberias ver "N monedas a escanear" y recibir
   en Telegram el mensaje "QT Scanner iniciado".

## 4. Pruebas locales
```bash
pip install -r requirements.txt
python qt_scanner_bot.py --selftest          # logica offline
python qt_scanner_bot.py --test-telegram     # mensaje de prueba
python qt_scanner_bot.py --once --dry-run    # un escaneo, solo imprime
```

## Notas
- El sistema de archivos de Railway es efimero: `sent_signals.json` se pierde al
  redesplegar. Solo se analiza la ultima vela cerrada, asi que el riesgo de duplicados
  es minimo. Para persistirlo, monta un Volume y pon `SENT_FILE=/data/sent_signals.json`.
- Si tienes pocas senales, relaja filtros: `REQUIRE_CHAIN=false`, `BIAS_MODE=none`,
  `SESSIONS=0,1,2,3`.
- No incluye el filtro SMT con 2o activo, EMA 1H, True Open Weekly ni limites diarios.
- Comprueba las senales en papel/demo antes de arriesgar dinero real.
