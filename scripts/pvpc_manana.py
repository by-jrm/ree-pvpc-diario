#!/usr/bin/env python3
"""Obtiene y valida el PVPC 2.0TD peninsular publicado por REE/eSIOS."""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Madrid")
GEO_ID = 8741
INDICATOR_ID = 1001
API = f"https://api.esios.ree.es/indicators/{INDICATOR_ID}"
OUT = Path("data/latest.json")
HISTORY_DAYS = 7
FIVE = Decimal("0.00001")


def local_midnight(day):
    return datetime(day.year, day.month, day.day, tzinfo=ZONE)


def money(value):
    return float(value.quantize(FIVE, rounding=ROUND_HALF_UP))


def fetch_data(token, from_day, to_day):
    start = local_midnight(from_day)
    end = local_midnight(to_day + timedelta(days=1)) - timedelta(seconds=1)
    query = urllib.parse.urlencode({
        "start_date": start.isoformat(timespec="seconds"),
        "end_date": end.isoformat(timespec="seconds"),
        "geo_ids[]": GEO_ID,
    })
    request = urllib.request.Request(
        API + "?" + query,
        headers={"x-api-key": token, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)["indicator"]


def extract_day(records, day):
    result = []
    for item in records:
        if int(item.get("geo_id") or 0) != GEO_ID:
            continue
        dt = datetime.fromisoformat(item["datetime"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("ESIOS ha devuelto una fecha sin zona horaria")
        dt = dt.astimezone(ZONE)
        if dt.date() != day:
            continue
        if dt.minute != 0 or dt.second != 0:
            raise ValueError("Un registro no empieza en una hora en punto")
        price = Decimal(str(item["value"])) / Decimal("1000")
        if not price.is_finite():
            raise ValueError("Se ha recibido un precio no finito")
        result.append((dt, price))

    result.sort(key=lambda row: row[0].astimezone(timezone.utc))
    start = local_midnight(day).astimezone(timezone.utc)
    end = local_midnight(day + timedelta(days=1)).astimezone(timezone.utc)
    expected = int((end - start).total_seconds() // 3600)
    if len(result) != expected:
        raise ValueError(f"{day}: esperados {expected} registros, recibidos {len(result)}")
    for i, (dt, _) in enumerate(result):
        if dt.astimezone(timezone.utc) != start + timedelta(hours=i):
            raise ValueError(f"{day}: registros duplicados o intervalos ausentes")
    return result


def two_hour_choices(prices):
    candidates = []
    for i in range(len(prices) - 1):
        start = prices[i][0]
        next_hour = prices[i + 1][0]
        if next_hour.astimezone(timezone.utc) - start.astimezone(timezone.utc) != timedelta(hours=1):
            continue
        mean = (prices[i][1] + prices[i + 1][1]) / 2
        finish = (next_hour.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(ZONE)
        candidates.append((mean, i, start, finish))
    chosen = []
    occupied = set()
    for mean, i, start, finish in sorted(candidates, key=lambda x: (x[0], x[1])):
        if i in occupied or i + 1 in occupied:
            continue
        chosen.append({
            "desde": start.strftime("%H:%M"),
            "hasta": finish.strftime("%H:%M"),
            "precio_medio_eur_kwh": money(mean),
        })
        occupied.update((i, i + 1))
        if len(chosen) == 3:
            break
    if len(chosen) < 3:
        raise ValueError("No hay tres tramos de dos horas independientes")
    return chosen


def daily_average(prices):
    return sum(price for _, price in prices) / Decimal(len(prices))


def already_published(target):
    try:
        report = json.loads(OUT.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    if not isinstance(report, dict):
        return False

    start = local_midnight(target).astimezone(timezone.utc)
    end = local_midnight(target + timedelta(days=1)).astimezone(timezone.utc)
    expected = int((end - start).total_seconds() // 3600)
    hours = report.get("precios_horarios")
    return (
        report.get("fecha") == target.isoformat()
        and report.get("verificacion") == "official_verified"
        and report.get("indicador_esios") == INDICATOR_ID
        and report.get("geo_id") == GEO_ID
        and report.get("numero_intervalos") == expected
        and isinstance(hours, list)
        and len(hours) == expected
        and all(
            isinstance(row, dict) and finite_number(row.get("precio_eur_kwh"))
            for row in hours
        )
        and finite_number(report.get("precio_medio_7_dias_eur_kwh"))
        and finite_number(report.get("variacion_porcentual_vs_7_dias"))
    )


def finite_number(value):
    return type(value) in (int, float) and isfinite(value)


def main():
    # La ejecución nocturna publica los precios de mañana, hora peninsular.
    now = datetime.now(ZONE)
    target = now.date() + timedelta(days=1)
    if already_published(target):
        print(f"Ya publicado y verificado: {target}. Sin consulta a REE.")
        return

    token = os.environ.get("ESIOS_API_TOKEN")
    if not token:
        raise RuntimeError("Falta el secreto ESIOS_API_TOKEN")

    previous = target - timedelta(days=1)
    first = target - timedelta(days=HISTORY_DAYS)

    indicator = fetch_data(token, first, target)
    if int(indicator.get("id", -1)) != INDICATOR_ID:
        raise ValueError("El indicador recibido no es el 1001")
    units = str(indicator.get("magnitud", ""))
    if "MWh" not in units:
        raise ValueError(f"Unidad inesperada: {units}")

    records = indicator["values"]
    current = extract_day(records, target)
    history = [
        daily_average(extract_day(records, first + timedelta(days=offset)))
        for offset in range(HISTORY_DAYS)
    ]

    avg_prev = history[-1]
    avg_week = sum(history) / Decimal(HISTORY_DAYS)
    avg = daily_average(current)
    if avg_week == 0:
        raise ValueError("No se puede calcular una variación con media de siete días cero")
    if avg_prev == 0:
        raise ValueError("No se puede calcular una variación con media previa cero")
    minimum = min(current, key=lambda x: x[1])
    maximum = max(current, key=lambda x: x[1])
    delta = (avg / avg_prev - 1) * 100
    delta_week = (avg / avg_week - 1) * 100

    def hour_range(item):
        start, price = item
        end = (start.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(ZONE)
        return {
            "desde": start.strftime("%H:%M"),
            "hasta": end.strftime("%H:%M"),
            "precio_eur_kwh": money(price),
        }

    report = {
        "fecha": target.isoformat(),
        "fecha_comparacion": previous.isoformat(),
        "zona": "Península",
        "zona_horaria": "Europe/Madrid",
        "tarifa": "PVPC 2.0TD",
        "indicador_esios": INDICATOR_ID,
        "geo_id": GEO_ID,
        "fuente": "REE/eSIOS API oficial",
        "url_fuente": "https://www.esios.ree.es/es/pvpc",
        "verificacion": "official_verified",
        "validado_en": now.isoformat(timespec="seconds"),
        "numero_intervalos": len(current),
        "unidad": "€/kWh, término de energía sin impuestos",
        "precio_medio_eur_kwh": money(avg),
        "precio_medio_dia_anterior_eur_kwh": money(avg_prev),
        "variacion_porcentual_vs_anterior": float(delta.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "precio_medio_7_dias_eur_kwh": money(avg_week),
        "variacion_porcentual_vs_7_dias": float(
            delta_week.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ),
        "minimo": hour_range(minimum),
        "maximo": hour_range(maximum),
        "mejores_tramos_2h_sin_solapamiento": two_hour_choices(current),
        "precios_horarios": [hour_range(row) for row in current],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    contents = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    with NamedTemporaryFile(dir=OUT.parent, delete=False) as output:
        pending = Path(output.name)
    try:
        pending.write_text(contents, encoding="utf-8")
        pending.replace(OUT)
    finally:
        pending.unlink(missing_ok=True)
    print(
        f"VALIDADO: {target} | {len(current)} intervalos | "
        f"media {report['precio_medio_eur_kwh']} €/kWh | "
        f"media 7 días {report['precio_medio_7_dias_eur_kwh']} €/kWh | "
        f"variación {report['variacion_porcentual_vs_7_dias']:+.2f}% | REE indicador {INDICATOR_ID}"
    )
    print("Archivo publicado:", OUT)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR DE VALIDACIÓN: {error}", file=sys.stderr)
        sys.exit(1)
