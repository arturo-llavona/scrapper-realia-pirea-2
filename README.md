# script-realia

Despliegue sin tarjeta usando GitHub Actions + GitHub Pages.

## Qué hace este paquete

1. Ejecuta el scraping cada 30 minutos con GitHub Actions.
2. Guarda histórico y detecta cambios de precio.
3. Publica la web estática en GitHub Pages.
4. Envía notificación Telegram cuando hay cambios.

## Estructura importante

1. `scrapper.py`: lógica de scraping, histórico y Telegram.
2. `.github/workflows/realia-scrape.yml`: workflow automático cada 30 minutos.
3. `docs/index.html`: frontend estático para GitHub Pages.
4. `docs/data/*.json`: datos que actualiza el workflow.

## Paso 1: crear repositorio en GitHub

1. Crea una cuenta en GitHub (sin tarjeta).
2. Crea un repo nuevo, por ejemplo `script-realia`.
3. Sube el contenido de esta carpeta.

## Paso 2: configurar Secrets

En tu repo:

1. Ve a `Settings`.
2. Ve a `Secrets and variables` > `Actions`.
3. Crea estos secretos:
4. `TELEGRAM_BOT_TOKEN`
5. `TELEGRAM_CHAT_ID`

Si no quieres Telegram, puedes dejar los secretos sin configurar y el script seguirá funcionando sin notificar.

## Paso 3: activar GitHub Pages

1. Ve a `Settings` > `Pages`.
2. En Source selecciona `Deploy from a branch`.
3. Elige `main` y carpeta `/docs`.
4. Guarda.

La URL quedará como:

`https://TU_USUARIO.github.io/TU_REPO/`

## Paso 4: lanzar el primer workflow

1. Ve a `Actions`.
2. Abre `Realia Scraper`.
3. Pulsa `Run workflow`.

Esto generará:

1. `docs/data/snapshots.json`
2. `docs/data/changes.json`
3. `docs/data/status.json`
4. `docs/data/pdfs/*` (copias locales para enlaces de descarga)
5. `realia_data/last_run.json`

## Paso 5: validación

1. Comprueba que el workflow termina en verde.
2. Abre la URL de GitHub Pages.
3. Verifica que se ve la tabla y la última ejecución.

## Ejecución local opcional

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python .\scrapper.py --insecure --out .\realia_data\last_run.json --export-static .\docs\data
```

## Notas de seguridad

1. No subas `.env` al repositorio.
2. Si el token de Telegram se compartió, regénéralo en BotFather.
