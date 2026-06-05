from __future__ import annotations

import asyncio
import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import urllib3


DEFAULT_URL = "https://www.realia.es/promociones-obra-nueva/promociones-obra-nueva-madrid/pireo-ii"
DATA_DIR = Path(__file__).parent / "realia_data"
DB_PATH = DATA_DIR / "history.db"
PDF_DIR = DATA_DIR / "pdfs"


logger = logging.getLogger("realia_scrapper")
if not logging.getLogger().handlers:
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def ensure_storage() -> None:
	DATA_DIR.mkdir(parents=True, exist_ok=True)
	PDF_DIR.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
	ensure_storage()
	conn = sqlite3.connect(DB_PATH)
	conn.row_factory = sqlite3.Row
	return conn


def init_db() -> None:
	with get_db() as conn:
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS runs_history (
				execution_id TEXT PRIMARY KEY,
				execution_date TEXT NOT NULL,
				executed_at TEXT NOT NULL,
				url TEXT NOT NULL,
				units_count INTEGER NOT NULL
			)
			"""
		)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS snapshots_history (
				execution_id TEXT NOT NULL,
				execution_date TEXT NOT NULL,
				executed_at TEXT NOT NULL,
				typology TEXT NOT NULL,
				price_from TEXT,
				price_value REAL,
				home TEXT NOT NULL DEFAULT '',
				bedrooms TEXT,
				square_meters TEXT,
				garage_spots TEXT,
				storage_room TEXT,
				plan_pdf_url TEXT,
				plan_pdf_local_path TEXT,
				previous_price_from TEXT,
				price_changed INTEGER NOT NULL DEFAULT 0,
				PRIMARY KEY (execution_id, typology, home)
			)
			"""
		)

		# Migración desde tablas antiguas si existen.
		table_names = {
			row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
		}

		if "runs" in table_names:
			run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
			exec_id_expr = "COALESCE(execution_id, executed_at)" if "execution_id" in run_cols else "executed_at"
			conn.execute(
				f"""
				INSERT OR IGNORE INTO runs_history(execution_id, execution_date, executed_at, url, units_count)
				SELECT {exec_id_expr}, execution_date, executed_at, url, units_count
				FROM runs
				"""
			)

		if "snapshots" in table_names:
			snapshot_cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
			exec_id_expr = "COALESCE(execution_id, executed_at)" if "execution_id" in snapshot_cols else "executed_at"
			home_expr = "COALESCE(home, '')" if "home" in snapshot_cols else "''"
			price_value_expr = "price_value" if "price_value" in snapshot_cols else "NULL"
			prev_price_expr = "previous_price_from" if "previous_price_from" in snapshot_cols else "NULL"
			price_changed_expr = "price_changed" if "price_changed" in snapshot_cols else "0"
			garage_expr = "garage_spots" if "garage_spots" in snapshot_cols else "NULL"
			storage_expr = "storage_room" if "storage_room" in snapshot_cols else "NULL"
			pdf_local_expr = "plan_pdf_local_path" if "plan_pdf_local_path" in snapshot_cols else "NULL"

			conn.execute(
				f"""
				INSERT OR IGNORE INTO snapshots_history(
					execution_id,
					execution_date,
					executed_at,
					typology,
					price_from,
					price_value,
					home,
					bedrooms,
					square_meters,
					garage_spots,
					storage_room,
					plan_pdf_url,
					plan_pdf_local_path,
					previous_price_from,
					price_changed
				)
				SELECT
					{exec_id_expr},
					execution_date,
					executed_at,
					typology,
					price_from,
					{price_value_expr},
					{home_expr},
					bedrooms,
					square_meters,
					{garage_expr},
					{storage_expr},
					plan_pdf_url,
					{pdf_local_expr},
					{prev_price_expr},
					{price_changed_expr}
				FROM snapshots
				WHERE typology IS NOT NULL
				"""
			)

def parse_price_value(price_from: str | None) -> float | None:
	if not price_from:
		return None
	# 435.500€ -> 435500.0
	number = re.sub(r"[^\d,\.]", "", price_from)
	number = number.replace(".", "").replace(",", ".")
	if not number:
		return None
	try:
		return float(number)
	except ValueError:
		return None


def get_previous_price(
	conn: sqlite3.Connection,
	executed_at: str,
	typology: str,
	home: str,
) -> str | None:
	row = conn.execute(
		"""
		SELECT price_from
		FROM snapshots_history
		WHERE typology = ?
		  AND home = ?
		  AND executed_at < ?
		ORDER BY executed_at DESC
		LIMIT 1
		""",
		(typology, home, executed_at),
	).fetchone()
	return row["price_from"] if row else None


def build_execution_id(executed_at: str) -> str:
	"""Genera un id estable por ejecución evitando caracteres problemáticos en rutas."""
	return re.sub(r"[^0-9T]", "", executed_at.replace("+", ""))


def safe_file_name(value: str) -> str:
	return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "file"


def download_pdf(
	pdf_url: str,
	execution_id: str,
	typology: str,
	home: str,
	verify: bool | str,
	timeout: int = 60,
) -> str | None:
	if not pdf_url:
		return None

	typology_dir = PDF_DIR / execution_id
	typology_dir.mkdir(parents=True, exist_ok=True)

	url_path = Path(urlparse(pdf_url).path)
	extension = url_path.suffix if url_path.suffix else ".pdf"
	file_name = f"{safe_file_name(typology)}_{safe_file_name(home or 'home')}{extension}"
	target_path = typology_dir / file_name

	headers = {
		"User-Agent": (
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
			"AppleWebKit/537.36 (KHTML, like Gecko) "
			"Chrome/125.0.0.0 Safari/537.36"
		)
	}
	if verify is False:
		urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

	response = requests.get(pdf_url, headers=headers, timeout=timeout, verify=verify)
	response.raise_for_status()
	target_path.write_bytes(response.content)
	return str(target_path)


def fetch_html(
	url: str,
	timeout: int = 30,
	verify: bool | str = True,
) -> str:
	headers = {
		"User-Agent": (
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
			"AppleWebKit/537.36 (KHTML, like Gecko) "
			"Chrome/125.0.0.0 Safari/537.36"
		)
	}
	if verify is False:
		urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
	response = requests.get(url, headers=headers, timeout=timeout, verify=verify)
	response.raise_for_status()
	return response.text


def extract_prices_section(html: str) -> str:
	match = re.search(
		r"<section[^>]*id=[\"']prices[\"'][^>]*>[\s\S]*?</section>",
		html,
		flags=re.IGNORECASE,
	)
	if not match:
		raise ValueError("No se encontró section#prices en la página")
	return match.group(0)


def html_to_lines(section_html: str) -> list[str]:
	clean = re.sub(r"<script[\s\S]*?</script>", "", section_html, flags=re.IGNORECASE)
	clean = re.sub(r"<style[\s\S]*?</style>", "", clean, flags=re.IGNORECASE)
	clean = re.sub(r"<br\s*/?>", "\n", clean, flags=re.IGNORECASE)
	clean = re.sub(r"</(p|div|li|h1|h2|h3|h4|h5|h6|section|article|span|a)>", "\n", clean, flags=re.IGNORECASE)
	clean = re.sub(r"<[^>]+>", "", clean)
	clean = unescape(clean)

	lines: list[str] = []
	for raw in clean.splitlines():
		line = re.sub(r"\s+", " ", raw).strip()
		if line:
			lines.append(line)
	return lines


def extract_plan_links(section_html: str) -> list[str]:
	links = re.findall(
		r"<a[^>]+href=[\"']([^\"']+\.pdf)[\"'][^>]*>",
		section_html,
		flags=re.IGNORECASE,
	)
	# Mantener orden y quitar duplicados.
	seen = set()
	ordered = []
	for link in links:
		if link not in seen:
			seen.add(link)
			ordered.append(link)
	return ordered


def find_after(lines: list[str], label: str) -> str | None:
	for idx, line in enumerate(lines):
		if line.lower() == label.lower() and idx + 1 < len(lines):
			return lines[idx + 1]
	return None


def find_price(lines: list[str]) -> str | None:
	joined = "\n".join(lines)
	match = re.search(r"Desde\s*\n?\s*([\d\.,]+\s*€)", joined, flags=re.IGNORECASE)
	if match:
		return re.sub(r"\s+", "", match.group(1))
	for line in lines:
		if "€" in line:
			candidate = re.search(r"([\d\.,]+\s*€)", line)
			if candidate:
				return re.sub(r"\s+", "", candidate.group(1))
	return None


def extract_units(lines: list[str], section_html: str) -> list[dict]:
	unit_header_regex = re.compile(r"^[A-Z]\d{2,3}$")
	unit_indexes = [i for i, line in enumerate(lines) if unit_header_regex.match(line)]
	if not unit_indexes:
		return []

	plan_links = extract_plan_links(section_html)
	units: list[dict] = []

	for idx, start in enumerate(unit_indexes):
		end = unit_indexes[idx + 1] if idx + 1 < len(unit_indexes) else len(lines)
		block = lines[start:end]
		typology = block[0]

		unit = {
			"typology": typology,
			"price_from": find_price(block),
			"home": find_after(block, "Viviendas"),
			"bedrooms": find_after(block, "Dormitorios"),
			"square_meters": find_after(block, "m²"),
			"garage_spots": find_after(block, "Nº plazas garaje"),
			"storage_room": find_after(block, "Trastero"),
			"plan_pdf": plan_links[idx] if idx < len(plan_links) else None,
		}
		units.append(unit)

	return units


def scrape_prices(url: str, verify: bool | str = True) -> dict:
	html = fetch_html(url, verify=verify)
	prices_section = extract_prices_section(html)
	lines = html_to_lines(prices_section)
	units = extract_units(lines, prices_section)

	return {
		"url": url,
		"scraped_at": datetime.now(timezone.utc).isoformat(),
		"section": "prices",
		"units_count": len(units),
		"units": units,
		"raw_lines_preview": lines[:30],
	}


def persist_snapshot(
	data: dict[str, Any],
	verify: bool | str,
	download_pdfs: bool = True,
) -> dict[str, Any]:
	init_db()
	executed_at = data["scraped_at"]
	execution_date = executed_at.split("T", maxsplit=1)[0]
	execution_id = build_execution_id(executed_at)
	units = data.get("units", [])
	changes: list[dict[str, Any]] = []

	with get_db() as conn:
		conn.execute(
			"""
			INSERT INTO runs_history(execution_id, execution_date, executed_at, url, units_count)
			VALUES(?, ?, ?, ?, ?)
			ON CONFLICT(execution_id)
			DO UPDATE SET
				executed_at = excluded.executed_at,
				execution_date = excluded.execution_date,
				url = excluded.url,
				units_count = excluded.units_count
			""",
			(execution_id, execution_date, executed_at, data["url"], len(units)),
		)

		for unit in units:
			typology = unit.get("typology")
			home = (unit.get("home") or "").strip()
			if not typology:
				continue

			previous_price = get_previous_price(conn, executed_at, typology, home)
			current_price = unit.get("price_from")
			price_changed = int(previous_price is not None and previous_price != current_price)

			local_pdf = None
			if download_pdfs and unit.get("plan_pdf"):
				try:
					local_pdf = download_pdf(
						pdf_url=unit["plan_pdf"],
						execution_id=execution_id,
						typology=typology,
						home=home,
						verify=verify,
					)
				except Exception:
					local_pdf = None

			conn.execute(
				"""
				INSERT INTO snapshots_history(
					execution_id,
					execution_date,
					executed_at,
					typology,
					price_from,
					price_value,
					home,
					bedrooms,
					square_meters,
					garage_spots,
					storage_room,
					plan_pdf_url,
					plan_pdf_local_path,
					previous_price_from,
					price_changed
				)
				VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(execution_id, typology, home)
				DO UPDATE SET
					execution_date = excluded.execution_date,
					executed_at = excluded.executed_at,
					price_from = excluded.price_from,
					price_value = excluded.price_value,
					home = excluded.home,
					bedrooms = excluded.bedrooms,
					square_meters = excluded.square_meters,
					garage_spots = excluded.garage_spots,
					storage_room = excluded.storage_room,
					plan_pdf_url = excluded.plan_pdf_url,
					plan_pdf_local_path = excluded.plan_pdf_local_path,
					previous_price_from = excluded.previous_price_from,
					price_changed = excluded.price_changed
				""",
				(
					execution_id,
					execution_date,
					executed_at,
					typology,
					current_price,
					parse_price_value(current_price),
					home,
					unit.get("bedrooms"),
					unit.get("square_meters"),
					unit.get("garage_spots"),
					unit.get("storage_room"),
					unit.get("plan_pdf"),
					local_pdf,
					previous_price,
					price_changed,
				),
			)

			if price_changed:
				changes.append(
					{
						"execution_id": execution_id,
						"execution_date": execution_date,
						"executed_at": executed_at,
						"typology": typology,
						"home": home,
						"previous_price_from": previous_price,
						"price_from": current_price,
					}
				)

	return {
		"execution_id": execution_id,
		"execution_date": execution_date,
		"executed_at": executed_at,
		"units_count": len(units),
		"changes_count": len(changes),
		"changes": changes,
	}


def query_snapshots(limit: int = 500) -> list[dict[str, Any]]:
	init_db()
	with get_db() as conn:
		rows = conn.execute(
			"""
			SELECT
				execution_id,
				execution_date,
				executed_at,
				typology,
				price_from,
				price_value,
				previous_price_from,
				price_changed,
				home,
				bedrooms,
				square_meters,
				garage_spots,
				storage_room,
				plan_pdf_url,
				plan_pdf_local_path
			FROM snapshots_history
			ORDER BY executed_at DESC, typology ASC, home ASC
			LIMIT ?
			""",
			(limit,),
		).fetchall()

	result: list[dict[str, Any]] = []
	for row in rows:
		entry = dict(row)
		entry["price_changed"] = bool(entry["price_changed"])
		entry["has_pdf"] = bool(entry.get("plan_pdf_local_path"))
		entry["has_pdf_url"] = bool(entry.get("plan_pdf_url"))
		result.append(entry)
	return result


def get_last_run() -> dict[str, Any] | None:
	init_db()
	with get_db() as conn:
		row = conn.execute(
			"""
			SELECT execution_id, execution_date, executed_at, url, units_count
			FROM runs_history
			ORDER BY executed_at DESC
			LIMIT 1
			"""
		).fetchone()
	return dict(row) if row else None


def export_static_data(output_dir: str) -> dict[str, Any]:
	base = Path(output_dir)
	base.mkdir(parents=True, exist_ok=True)
	pdf_export_dir = base / "pdfs"
	pdf_export_dir.mkdir(parents=True, exist_ok=True)

	def resolve_existing_exported_pdf(entry: dict[str, Any]) -> str | None:
		"""Return existing static PDF path for historical rows without local DB path."""
		execution_date = entry.get("execution_date")
		typology = entry.get("typology")
		if not execution_date or not typology:
			return None

		base_name = safe_file_name(str(typology))
		candidates = [f"{base_name}.pdf"]
		plan_pdf_url = entry.get("plan_pdf_url")
		if plan_pdf_url:
			url_extension = Path(urlparse(str(plan_pdf_url)).path).suffix.lower()
			if url_extension and f"{base_name}{url_extension}" not in candidates:
				candidates.insert(0, f"{base_name}{url_extension}")

		for file_name in candidates:
			candidate = pdf_export_dir / str(execution_date) / file_name
			if candidate.exists():
				relative = candidate.relative_to(base)
				return f"data/{relative.as_posix()}"
		return None

	snapshots = query_snapshots(limit=5000)
	exported_pdfs = 0
	static_snapshots: list[dict[str, Any]] = []
	for row in snapshots:
		entry = dict(row)
		local_pdf_path = entry.get("plan_pdf_local_path")
		pdf_data_path = None

		if local_pdf_path:
			source = Path(local_pdf_path)
			if source.exists():
				try:
					relative_pdf_path = source.relative_to(PDF_DIR)
				except ValueError:
					relative_pdf_path = Path(source.name)

				target = pdf_export_dir / relative_pdf_path
				target.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(source, target)
				pdf_data_path = f"data/pdfs/{relative_pdf_path.as_posix()}"
				exported_pdfs += 1

		if not pdf_data_path:
			pdf_data_path = resolve_existing_exported_pdf(entry)

		entry["plan_pdf_data_path"] = pdf_data_path
		entry["has_pdf"] = bool(pdf_data_path)
		static_snapshots.append(entry)

	changes = query_changes(limit=5000)
	last_run = get_last_run()

	status = {
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"records": len(static_snapshots),
		"changes": len(changes),
		"exported_pdfs": exported_pdfs,
		"last_run": last_run,
	}

	(base / "snapshots.json").write_text(json.dumps(static_snapshots, ensure_ascii=False, indent=2), encoding="utf-8")
	(base / "changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
	(base / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

	return status


def query_changes(limit: int = 500) -> list[dict[str, Any]]:
	init_db()
	with get_db() as conn:
		rows = conn.execute(
			"""
			SELECT
				execution_id,
				execution_date,
				executed_at,
				typology,
				home,
				previous_price_from,
				price_from
			FROM snapshots_history
			WHERE price_changed = 1
			ORDER BY executed_at DESC, typology ASC, home ASC
			LIMIT ?
			""",
			(limit,),
		).fetchall()
	return [dict(row) for row in rows]


def get_snapshot(execution_id: str, typology: str, home: str = "") -> dict[str, Any] | None:
	init_db()
	with get_db() as conn:
		row = conn.execute(
			"""
			SELECT *
			FROM snapshots_history
			WHERE execution_id = ? AND typology = ? AND home = ?
			LIMIT 1
			""",
			(execution_id, typology, home),
		).fetchone()
	return dict(row) if row else None


def run_scrape_cycle(
	url: str,
	verify: bool | str,
	download_pdfs: bool,
	output_path: str | None = None,
	telegram_token: str | None = None,
	telegram_chat_id: str | None = None,
	telegram_notify_no_changes: bool = False,
) -> dict[str, Any]:
	data = scrape_prices(url=url, verify=verify)
	persistence_result = persist_snapshot(
		data=data,
		verify=verify,
		download_pdfs=download_pdfs,
	)
	data["history"] = persistence_result

	notify_telegram(
		telegram_token=telegram_token,
		telegram_chat_id=telegram_chat_id,
		url=url,
		history=persistence_result,
		units=data.get("units", []),
		notify_no_changes=telegram_notify_no_changes,
	)

	if output_path:
		output = Path(output_path)
		output.parent.mkdir(parents=True, exist_ok=True)
		with output.open("w", encoding="utf-8") as file:
			json.dump(data, file, ensure_ascii=False, indent=2)

	return data


def send_telegram_message(token: str, chat_id: str, text: str, timeout: int = 20) -> None:
	endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
	payload = {
		"chat_id": chat_id,
		"text": text,
		"disable_web_page_preview": True,
		"parse_mode": "HTML",
	}
	response = requests.post(endpoint, json=payload, timeout=timeout)
	result: dict[str, Any] | None = None
	try:
		result = response.json()
	except Exception:
		result = None

	if response.status_code >= 400:
		description = None
		if isinstance(result, dict):
			description = result.get("description")
		detail = description or response.text
		raise RuntimeError(f"Telegram HTTP {response.status_code}: {detail}")

	if not isinstance(result, dict) or not result.get("ok"):
		raise RuntimeError(f"Telegram API error: {result or response.text}")


def format_execution_time(executed_at: str | None, tz_name: str = "Europe/Madrid") -> str:
	if not executed_at:
		return "-"
	try:
		dt = datetime.fromisoformat(str(executed_at).replace("Z", "+00:00"))
	except ValueError:
		return str(executed_at)

	try:
		localized = dt.astimezone(ZoneInfo(tz_name))
	except Exception:
		localized = dt

	return localized.strftime("%H:%M:%S")


def build_telegram_message(
	url: str,
	history: dict[str, Any],
	units: list[dict[str, Any]] | None,
	notify_no_changes: bool,
) -> str | None:
	def tg(value: Any) -> str:
		return escape(str(value), quote=False)

	changes = history.get("changes", [])
	changes_count = history.get("changes_count", 0)
	execution_date = history.get("execution_date", "-")
	executed_at = history.get("executed_at", "-")
	hour_text = format_execution_time(executed_at)
	units_count = history.get("units_count", 0)
	units = units or []

	# Compatibilidad hacia atrás por si se desea desactivar el mensaje sin cambios.
	if changes_count == 0 and not notify_no_changes and not units:
		return None

	lines = [
		"📊 <b>Resumen Realia - Pireo II</b>",
		f"🗓️ <b>Fecha:</b> {tg(execution_date)}",
		f"🕒 <b>Hora ejecución</b> <i>(Madrid)</i>: {tg(hour_text)}",
		f"🏠 <b>Viviendas leídas:</b> {units_count}",
		f"🔄 <b>Cambios detectados:</b> {changes_count}",
	]

	if units:
		lines.append("")
		lines.append("<b>Detalle leído</b>")
		for unit in units:
			typology = tg(unit.get("typology") or "-")
			home = tg(unit.get("home") or "-")
			price = tg(unit.get("price_from") or "-")
			bedrooms = tg(unit.get("bedrooms") or "-")
			square_meters = tg(unit.get("square_meters") or "-")
			lines.append(
				f"• <b>{typology}</b> | {home} | 💶 <b>{price}</b> | {bedrooms} hab | {square_meters} m²"
			)

	if changes:
		lines.append("")
		lines.append("⚠️ <b>Cambios vs ejecución anterior</b>")
		for item in changes:
			typology = tg(item.get("typology", "-"))
			home = tg(item.get("home") or "-")
			previous_price = tg(item.get("previous_price_from") or "-")
			price = tg(item.get("price_from") or "-")
			lines.append(f"• <b>{typology}</b> | {home}: <code>{previous_price}</code> → <code>{price}</code>")
	else:
		lines.append("")
		lines.append("✅ <i>Sin cambios respecto a la ejecución anterior.</i>")

	lines.append("")
	lines.append(f"🔗 <b>URL:</b> {tg(url)}")

	message = "\n".join(lines)
	max_chars = 3900
	if len(message) > max_chars:
		message = message[: max_chars - 35] + "\n...\n(Mensaje recortado por longitud)"
	return message


def notify_telegram(
	telegram_token: str | None,
	telegram_chat_id: str | None,
	url: str,
	history: dict[str, Any],
	units: list[dict[str, Any]] | None,
	notify_no_changes: bool,
) -> None:
	if not telegram_token or not telegram_chat_id:
		return

	message = build_telegram_message(
		url=url,
		history=history,
		units=units,
		notify_no_changes=notify_no_changes,
	)
	if not message:
		return

	try:
		send_telegram_message(token=telegram_token, chat_id=telegram_chat_id, text=message)
		logger.info("Notificación Telegram enviada")
	except Exception as exc:
		logger.exception("Error enviando Telegram: %s", exc)


def build_web_app(
	url: str,
	verify: bool | str,
	download_pdfs: bool,
	interval_seconds: int,
	output_path: str | None = None,
	telegram_token: str | None = None,
	telegram_chat_id: str | None = None,
	telegram_notify_no_changes: bool = False,
):
	from contextlib import asynccontextmanager
	from fastapi import FastAPI, HTTPException
	from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

	state: dict[str, Any] = {
		"running": False,
		"last_cycle": None,
		"last_error": None,
		"interval_seconds": interval_seconds,
	}

	def _execute_cycle() -> None:
		logger.info("Ejecutando ciclo de scraping en background")
		result = run_scrape_cycle(
			url=url,
			verify=verify,
			download_pdfs=download_pdfs,
			output_path=output_path,
			telegram_token=telegram_token,
			telegram_chat_id=telegram_chat_id,
			telegram_notify_no_changes=telegram_notify_no_changes,
		)
		state["last_cycle"] = result.get("history")
		state["last_error"] = None
		logger.info("Ciclo completado. Cambios detectados: %s", result.get("history", {}).get("changes_count", 0))

	async def _background_loop() -> None:
		state["running"] = True
		loop = asyncio.get_running_loop()
		while True:
			await asyncio.sleep(interval_seconds)
			try:
				await loop.run_in_executor(None, _execute_cycle)
			except Exception as exc:
				state["last_error"] = str(exc)
				logger.exception("Error en ciclo de scraping en background: %s", exc)

	@asynccontextmanager
	async def lifespan(app: FastAPI):
		loop = asyncio.get_running_loop()
		try:
			await loop.run_in_executor(None, _execute_cycle)
		except Exception as exc:
			state["last_error"] = str(exc)
			logger.exception("Error en ciclo inicial de scraping: %s", exc)

		task = asyncio.create_task(_background_loop())
		yield
		task.cancel()
		try:
			await task
		except asyncio.CancelledError:
			pass
		state["running"] = False

	app = FastAPI(title="Histórico Realia", lifespan=lifespan)

	@app.get("/", response_class=HTMLResponse)
	def index() -> str:
		return """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Histórico Realia - Precios</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; margin: 20px; background: #f6f7fb; color: #111827; }
    h1 { margin-bottom: 6px; }
    .muted { color: #6b7280; margin-bottom: 14px; }
    .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
    .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 10px 14px; min-width: 160px; }
    .label { color: #6b7280; font-size: 12px; }
    .value { font-size: 20px; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e5e7eb; }
    th, td { border-bottom: 1px solid #f1f5f9; padding: 8px 10px; font-size: 13px; text-align: left; }
    th { background: #f9fafb; position: sticky; top: 0; }
    tr.changed { background: #fff7ed; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; }
    .badge.changed { background: #ffedd5; color: #9a3412; }
    .actions a { color: #1d4ed8; text-decoration: none; }
    .actions a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Histórico de precios - section#prices</h1>
  <div class="muted">Clave de histórico: fecha de ejecución + tipología</div>
  <div class="cards">
    <div class="card"><div class="label">Registros</div><div class="value" id="total">-</div></div>
    <div class="card"><div class="label">Cambios detectados</div><div class="value" id="changes">-</div></div>
		<div class="card"><div class="label">Última actualización</div><div class="value" id="last-update" style="font-size:14px;">-</div></div>
  </div>
  <table>
    <thead>
      <tr>
        <th>Fecha</th>
        <th>Tipología</th>
        <th>Precio</th>
        <th>Precio anterior</th>
        <th>Dormitorios</th>
        <th>m²</th>
        <th>Vivienda</th>
        <th>Estado</th>
        <th>PDF</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

  <script>
		function formatDateTime(value) {
			if (!value) return '-';
			const date = new Date(value);
			if (Number.isNaN(date.getTime())) return value;
			return date.toLocaleString('es-ES');
		}

    async function loadData() {
			const [resp, statusResp] = await Promise.all([
				fetch('/api/snapshots'),
				fetch('/api/status'),
			]);
			const data = await resp.json();
			const status = await statusResp.json();
      const rows = document.getElementById('rows');
      const total = document.getElementById('total');
      const changes = document.getElementById('changes');
			const lastUpdate = document.getElementById('last-update');

      rows.innerHTML = '';
      let changedCount = 0;
      for (const row of data) {
        if (row.price_changed) changedCount += 1;
        const tr = document.createElement('tr');
        if (row.price_changed) tr.className = 'changed';
				const pdf = row.has_pdf
					? `<a href="/api/pdf/${encodeURIComponent(row.execution_id)}/${encodeURIComponent(row.typology)}?home=${encodeURIComponent(row.home || '')}" target="_blank">Ver PDF</a>`
					: '-';
        const status = row.price_changed
          ? '<span class="badge changed">Cambio</span>'
          : '-';
        tr.innerHTML = `
          <td>${row.execution_date}</td>
          <td>${row.typology || '-'}</td>
          <td>${row.price_from || '-'}</td>
          <td>${row.previous_price_from || '-'}</td>
          <td>${row.bedrooms || '-'}</td>
          <td>${row.square_meters || '-'}</td>
          <td>${row.home || '-'}</td>
          <td>${status}</td>
          <td class="actions">${pdf}</td>
        `;
        rows.appendChild(tr);
      }

      total.textContent = String(data.length);
      changes.textContent = String(changedCount);
			lastUpdate.textContent = formatDateTime(status?.last_cycle?.executed_at);
    }

		setInterval(() => {
			loadData().catch((error) => console.error(error));
		}, 30000);

		loadData().catch((error) => {
      console.error(error);
      alert('Error cargando datos');
    });
  </script>
</body>
</html>
		"""

	@app.get("/api/snapshots")
	def api_snapshots(limit: int = 500) -> JSONResponse:
		return JSONResponse(query_snapshots(limit=limit))

	@app.get("/api/changes")
	def api_changes(limit: int = 500) -> JSONResponse:
		return JSONResponse(query_changes(limit=limit))

	@app.get("/api/status")
	def api_status() -> JSONResponse:
		return JSONResponse(state)

	@app.get("/api/pdf/{execution_id}/{typology}")
	def api_pdf(execution_id: str, typology: str, home: str = ""):
		row = get_snapshot(execution_id=execution_id, typology=typology, home=home)
		if not row:
			raise HTTPException(status_code=404, detail="Registro no encontrado")
		pdf_path = row.get("plan_pdf_local_path")
		if not pdf_path:
			raise HTTPException(status_code=404, detail="PDF no descargado")
		path = Path(pdf_path)
		if not path.exists():
			raise HTTPException(status_code=404, detail="Archivo PDF no existe en disco")
		return FileResponse(
			path=str(path),
			media_type="application/pdf",
			headers={"Content-Disposition": f'inline; filename="{path.name}"'},
		)

	return app


def main() -> int:
	parser = argparse.ArgumentParser(description="Scraper de section#prices en realia.es")
	parser.add_argument("--url", default=DEFAULT_URL, help="URL objetivo")
	parser.add_argument("--out", help="Ruta de salida JSON opcional")
	parser.add_argument("--serve", action="store_true", help="Inicia interfaz web para consultar histórico")
	parser.add_argument("--host", default="127.0.0.1", help="Host del servidor web")
	parser.add_argument("--port", type=int, default=8010, help="Puerto del servidor web")
	parser.add_argument("--interval-minutes", type=int, default=30, help="Minutos entre actualizaciones en background")
	parser.add_argument("--export-static", help="Exporta snapshots/changes/status JSON para web estática")
	parser.add_argument("--skip-pdf", action="store_true", help="No descarga PDFs en local")
	parser.add_argument("--telegram-token", help="Token del bot de Telegram")
	parser.add_argument("--telegram-chat-id", help="Chat ID de Telegram")
	parser.add_argument(
		"--telegram-notify-no-changes",
		action="store_true",
		help="Envía Telegram también cuando no haya cambios",
	)
	parser.add_argument(
		"--insecure",
		action="store_true",
		help="Desactiva verificación TLS (solo para redes corporativas/interceptadas)",
	)
	parser.add_argument(
		"--ca-bundle",
		help="Ruta a certificado corporativo PEM para validar TLS",
	)
	args = parser.parse_args()

	verify: bool | str = True
	if args.ca_bundle:
		verify = args.ca_bundle
	elif args.insecure:
		verify = False

	telegram_token = args.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
	telegram_chat_id = args.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")

	if args.serve:
		try:
			import uvicorn
		except Exception as exc:
			raise RuntimeError("Para --serve necesitas instalar fastapi y uvicorn") from exc
		interval_minutes = max(1, args.interval_minutes)
		app = build_web_app(
			url=args.url,
			verify=verify,
			download_pdfs=not args.skip_pdf,
			interval_seconds=interval_minutes * 60,
			output_path=args.out,
			telegram_token=telegram_token,
			telegram_chat_id=telegram_chat_id,
			telegram_notify_no_changes=args.telegram_notify_no_changes,
		)
		uvicorn.run(app, host=args.host, port=args.port)
		return 0

	data = run_scrape_cycle(
		url=args.url,
		verify=verify,
		download_pdfs=not args.skip_pdf,
		output_path=args.out,
		telegram_token=telegram_token,
		telegram_chat_id=telegram_chat_id,
		telegram_notify_no_changes=args.telegram_notify_no_changes,
	)

	if not args.out:
		print(json.dumps(data, ensure_ascii=False, indent=2))
	else:
		print(f"JSON guardado en: {args.out}")

	if args.export_static:
		status = export_static_data(args.export_static)
		print(f"Export estático generado en: {args.export_static}")
		print(json.dumps(status, ensure_ascii=False, indent=2))

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
