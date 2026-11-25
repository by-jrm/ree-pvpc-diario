#!/usr/bin/env python3
"""Renderiza el PVPC oficial con Pillow y DejaVu Sans (Book y Bold).

Uso: python scripts/pvpc_infografia.py [--fonts DIRECTORIO] [--output PNG].
Requiere Pillow. En Windows puede requerir también tzdata.
En Linux se pueden instalar las fuentes con el paquete fonts-dejavu-core.
"""
import argparse
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("Europe/Madrid")
QUANTUM = Decimal("0.00001")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre")
NAVY = "#102A43"
BACKGROUND = "#F5F7F8"
TEXT = "#52616B"
GREEN = "#1F9D74"
RED = "#D64545"
ORANGE_BAR = "#F2B36D"
RED_BAR = "#D97870"
HEADER_TEXT = "#DCE8EE"
FOOTER = "#E5EAED"
GRID = "#D9E2E8"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(record, key):
    value = record[key]
    require(type(value) in (Decimal, int) and Decimal(value).is_finite(),
            f"Valor numérico inválido: {key}")
    return Decimal(value)


def interval(row):
    return row["desde"], row["hasta"]


def read_report(path):
    report = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    require(report["verificacion"] == "official_verified"
            and report["fuente"] == "REE/eSIOS API oficial"
            and report["indicador_esios"] == 1001 and report["geo_id"] == 8741
            and report["zona_horaria"] == ZONE.key, "El JSON no está verificado por REE/eSIOS")
    day = date.fromisoformat(report["fecha"])
    require(day.isoformat() == report["fecha"]
            and report["fecha_comparacion"] == (day - timedelta(days=1)).isoformat(),
            "Fechas del informe incoherentes")

    midnight = datetime.combine(day, datetime.min.time(), ZONE).astimezone(timezone.utc)
    tomorrow = datetime.combine(day + timedelta(days=1), datetime.min.time(), ZONE)
    count = (tomorrow.astimezone(timezone.utc) - midnight) // timedelta(hours=1)
    labels = [(midnight + timedelta(hours=i)).astimezone(ZONE).strftime("%H:%M")
              for i in range(count + 1)]
    rows = report["precios_horarios"]
    require(len(rows) == report["numero_intervalos"] == count
            and [interval(row) for row in rows] == list(zip(labels, labels[1:])),
            "Faltan horas o están fuera de orden")
    prices = [number(row, "precio_eur_kwh") for row in rows]
    mean = number(report, "precio_medio_eur_kwh")
    require(abs(mean - sum(prices) / count) <= QUANTUM, "Media diaria incoherente")
    for key in ("precio_medio_dia_anterior_eur_kwh", "precio_medio_7_dias_eur_kwh",
                "variacion_porcentual_vs_anterior", "variacion_porcentual_vs_7_dias"):
        number(report, key)
    for key, extreme in (("minimo", min(prices)), ("maximo", max(prices))):
        row = report[key]
        require(number(row, "precio_eur_kwh") == extreme
                and any(interval(hour) == interval(row) and price == extreme
                        for hour, price in zip(rows, prices)), f"{key} incoherente")

    choices = report["mejores_tramos_2h_sin_solapamiento"]
    require(len(choices) == 3, "Se necesitan exactamente tres franjas")
    candidates = [(prices[i] + prices[i + 1]) / 2 for i in range(count - 1)]
    green = set()
    last_mean = None
    for row in choices:
        mean = number(row, "precio_medio_eur_kwh")
        free = [i for i in range(count - 1) if i not in green and i + 1 not in green]
        cheapest = min(candidates[i] for i in free)
        # El productor redondea por separado cada hora y la media del tramo.
        matches = [i for i in free if (labels[i], labels[i + 2]) == interval(row)
                   and abs(candidates[i] - mean) <= QUANTUM
                   and candidates[i] <= cheapest + QUANTUM]
        require(len(matches) == 1 and (last_mean is None or mean >= last_mean),
                "Franja recomendada incoherente o solapada")
        start = matches[0]
        green.update((start, start + 1))
        last_mean = mean
    return day, report, prices, green


def bar_colors(prices, mean, green):
    limit = mean * Decimal("1.10")
    return [GREEN if i in green else ORANGE_BAR if price <= limit else RED_BAR
            for i, price in enumerate(prices)]


def render(day, report, prices, green, font_dir):
    image = Image.new("RGB", (1800, 1350), BACKGROUND)
    draw = ImageDraw.Draw(image)

    @lru_cache
    def font(size, bold):
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        path = str(font_dir / name) if font_dir else name
        return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.BASIC)

    def label(x, y, value, size, color=TEXT, bold=False, width=None, anchor="lt"):
        face = font(size, bold)
        if width is not None:
            while draw.textlength(value, font=face) > width:
                size -= 1
                face = font(size, bold)
        box = draw.textbbox((x, y), value, font=face, anchor=anchor)
        require(0 <= box[0] <= box[2] <= 1800 and 0 <= box[1] <= box[3] <= 1350,
                f"Texto fuera del lienzo: {value}")
        draw.text((x, y), value, font=face, fill=color, anchor=anchor)

    def card(box):
        draw.rounded_rectangle(box, radius=28, fill="white")

    def price(value, digits=5):
        rounded = value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
        return f"{rounded:.{digits}f} €/kWh".replace("-", "−")

    def hours(row):
        return "–".join(interval(row))

    draw.rectangle((0, 0, 1799, 219), fill=NAVY)
    label(100, 82, "Precio electricidad CASA VELILLA", 58, "white", True)
    label(101, 147, f"PVPC 2.0TD | {day.isoformat()}", 30, HEADER_TEXT)
    label(1700, 154, "REE/eSIOS API oficial", 25, HEADER_TEXT, anchor="rt")

    for x, right, title in ((90, 597, "PRECIO MEDIO DIARIO"),
                            (646, 1153, "FRENTE AL DÍA ANTERIOR"),
                            (1202, 1710, "Vs. MEDIA 7 DÍAS")):
        card((x, 270, right, 510))
        label(x + 45, 342, title, 25, bold=True, anchor="ls")
    average = number(report, "precio_medio_eur_kwh")
    label(135, 429, price(average, 4), 62, NAVY, True, width=417)
    for x, suffix, key, caption in (
        (691, "anterior", "precio_medio_dia_anterior_eur_kwh", "Anterior"),
        (1247, "7_dias", "precio_medio_7_dias_eur_kwh", "Media 7 días"),
    ):
        change = number(report, f"variacion_porcentual_vs_{suffix}")
        color = GREEN if change < 0 else RED if change > 0 else NAVY
        label(x, 400, f"{change:+.2f} %".replace("-", "−"), 60, color, True, width=417)
        label(x, 473, f"{caption}: {price(number(report, key))}", 27, width=417)

    card((90, 560, 1160, 980))
    card((1210, 560, 1710, 980))
    label(135, 610, "PRECIO HORARIO", 25, bold=True)
    label(1255, 610, "MEJORES FRANJAS · 2 H", 25, bold=True)

    bottom, top = 901, 671
    left, right = 172, 1110
    low, high = min(prices), max(prices)
    span = high - low
    for y, value in ((top, high), ((top + bottom) // 2, (low + high) / 2), (bottom, low)):
        if span or y == bottom:
            draw.line((left, y, right, y), fill=GRID, width=2)
            label(left - 12, y + 2, f"{value:.5f}".replace("-", "−"), 17, anchor="rt")

    step = (right - left) / len(prices)
    colors = bar_colors(prices, average, green)
    for i, (value, color) in enumerate(zip(prices, colors)):
        height = max(11, round((bottom - top) * (value - low) / span)) if span else 11
        x = left + i * step
        draw.rounded_rectangle((round(x + 3), bottom - height, round(x + step - 3),
                                bottom), radius=5, fill=color)
        hour = report["precios_horarios"][i]["desde"][:2]
        if int(hour) % 3 == 0:
            label(round(x + step / 2), 929, hour, 18, anchor="mt")

    for i, (row, color) in enumerate(zip(report["mejores_tramos_2h_sin_solapamiento"],
                                          (GREEN, "#D9822B", RED))):
        y = 662 + 94 * i
        draw.ellipse((1255, y, 1291, y + 36), fill=color)
        label(1273, y + 9, str(i + 1), 21, "white", True, anchor="mt")
        label(1320, y + 15, hours(row), 30, NAVY, True)
        label(1320, y + 55, price(number(row, "precio_medio_eur_kwh")), 25, width=345)

    for x, right, key, title, color in ((90, 850, "minimo", "MÍNIMO", GREEN),
                                         (900, 1710, "maximo", "MÁXIMO", RED)):
        card((x, 1030, right, 1240))
        row = report[key]
        lines = ((title, 25, TEXT, 57), (price(number(row, "precio_eur_kwh")), 50, color, 3),
                 (hours(row), 31, TEXT, 0))
        heights = [draw.textbbox((0, 0), value, font=font(size, True), anchor="lt")[3]
                   for value, size, _, _ in lines]
        y = (1030 + 1240 - sum(heights) - sum(gap for _, _, _, gap in lines)) // 2
        for (value, size, ink, gap), height in zip(lines, heights):
            label(x + 45, y, value, size, ink, True, width=right - x - 90)
            y += height + gap

    draw.rectangle((0, 1285, 1799, 1349), fill=FOOTER)
    label(100, 1321, "Término de energía sin impuestos", 23)
    label(1700, 1322, f"Datos para {day.day} de {MONTHS[day.month - 1]} de {day.year}",
          22, anchor="rt")
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "latest.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fonts", type=Path, help="Directorio con DejaVuSans.ttf y DejaVuSans-Bold.ttf")
    args = parser.parse_args()
    try:
        day, report, prices, green = read_report(args.input)
        output = args.output or Path(f"precio_electricidad_pvpc_{day.isoformat()}.png")
        render(day, report, prices, green, args.fonts).save(output, format="PNG", compress_level=9)
    except (OSError, TypeError, ValueError, KeyError) as error:
        parser.exit(1, f"No se pudo generar la infografía: {error}\n")
    print(output)


if __name__ == "__main__":
    main()
