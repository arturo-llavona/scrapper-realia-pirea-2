# script-realia

Paquete listo para desplegar el scraper de Realia con web + histórico + Telegram.

## Incluye

- `scrapper.py`
- `requirements.txt`
- `.env.example`
- `realia.service` (systemd para Linux)
- `.gitignore`

## 1) Ejecución local (Windows)

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# O por variables de entorno de sesión
$env:TELEGRAM_BOT_TOKEN="TU_TOKEN"
$env:TELEGRAM_CHAT_ID="TU_CHAT_ID"

python .\scrapper.py --serve --insecure --interval-minutes 30 --host 127.0.0.1 --port 8010 --out .\realia_data\last_run.json
```

## 2) Ejecución local (Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="TU_TOKEN"
export TELEGRAM_CHAT_ID="TU_CHAT_ID"

python scrapper.py --serve --insecure --interval-minutes 30 --host 0.0.0.0 --port 8010 --out ./realia_data/last_run.json
```

## 3) Despliegue en servidor Linux con systemd

1. Copia esta carpeta a `/opt/realia`.
2. Crea `/opt/realia/.env` a partir de `.env.example`.
3. Crea venv e instala dependencias.
4. Copia `realia.service` a `/etc/systemd/system/realia.service`.
5. Activa servicio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable realia
sudo systemctl start realia
sudo systemctl status realia
```

Logs:

```bash
sudo journalctl -u realia -f
```

## Notas de seguridad

- No subas `.env` al repositorio.
- Si has compartido el token del bot, regénéralo en BotFather.
