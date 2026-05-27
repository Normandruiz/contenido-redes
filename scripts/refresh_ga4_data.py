"""
Consulta GA4 Data API y genera data/ga4-destinos.json.

Output JSON:
  destinos: ranking de destinos con score (vistas+tiempo+engagement)
  evolucion: KPIs globales con delta vs periodo anterior + serie diaria + trending up/down

Uso local:
    python scripts/refresh_ga4_data.py

Variables de entorno:
    GA4_CREDENTIALS_JSON  Contenido JSON del service account (modo GitHub Actions).
    GA4_CREDENTIALS_PATH  Path al JSON del service account (modo local). Default: credentials/noma-viajes-ga4.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.oauth2 import service_account

PROPERTY_ID = "528481749"
PERIOD_DAYS = 90        # ventana para ranking de destinos
MIN_PAGEVIEWS = 3       # filtro: destinos con menos vistas no entran al ranking
KPI_WINDOW_DAYS = 14    # ventana para KPIs y trending (compara estos vs los 14d anteriores)
SPARKLINE_DAYS = 30     # cantidad de dias mostrados en el sparkline
TREND_TOP_N = 5         # destinos en alza
TREND_BOTTOM_N = 3      # destinos cayendo
TREND_MIN_PV = 2        # filtro: destino debe tener al menos N vistas en alguno de los dos periodos

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDS_PATH = ROOT / "credentials" / "noma-viajes-ga4.json"
OUTPUT_PATH = ROOT / "data" / "ga4-destinos.json"

# Matchea:
#   /vuelos/BUE/cartagena            -> 'cartagena'
#   /guias/cartagena                 -> 'cartagena'
#   /vuelos-baratos-a-cartagena      -> 'cartagena'  (URL legacy)
SLUG_REGEX = re.compile(r"^/(?:vuelos/[A-Z]+/|guias/|vuelos-baratos-a-)([a-z0-9-]+)/?$")


def get_credentials() -> service_account.Credentials:
    raw_json = os.environ.get("GA4_CREDENTIALS_JSON")
    if raw_json:
        info = json.loads(raw_json)
        return service_account.Credentials.from_service_account_info(info)

    creds_path = Path(os.environ.get("GA4_CREDENTIALS_PATH", DEFAULT_CREDS_PATH))
    if not creds_path.exists():
        sys.exit(f"ERROR: credenciales no encontradas en {creds_path}")
    return service_account.Credentials.from_service_account_file(str(creds_path))


def get_client() -> BetaAnalyticsDataClient:
    return BetaAnalyticsDataClient(credentials=get_credentials())


# ============================================================
# RANKING DE DESTINOS
# ============================================================

def fetch_destinos_metrics(client, period_days=PERIOD_DAYS):
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=[DateRange(start_date=f"{period_days}daysAgo", end_date="today")],
        dimensions=[Dimension(name="pagePath")],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
        ],
        limit=10000,
    )
    return client.run_report(request)


def extract_slug(path: str) -> str | None:
    match = SLUG_REGEX.match(path)
    return match.group(1) if match else None


def aggregate_by_slug(response) -> dict:
    agg: dict[str, dict] = {}
    for row in response.rows:
        path = row.dimension_values[0].value
        slug = extract_slug(path)
        if not slug:
            continue
        pv = int(row.metric_values[0].value)
        eng_time_total = float(row.metric_values[1].value)
        eng_rate = float(row.metric_values[2].value)
        bucket = agg.setdefault(
            slug,
            {"pageviews": 0, "engagement_time_total": 0.0, "engagement_rate_weighted": 0.0},
        )
        bucket["pageviews"] += pv
        bucket["engagement_time_total"] += eng_time_total
        bucket["engagement_rate_weighted"] += eng_rate * pv
    return agg


def compute_scores(agg: dict) -> list[dict]:
    items = []
    for slug, data in agg.items():
        pv = data["pageviews"]
        if pv < MIN_PAGEVIEWS:
            continue
        avg_eng_time = data["engagement_time_total"] / pv
        avg_eng_rate = data["engagement_rate_weighted"] / pv
        items.append(
            {
                "slug": slug,
                "pageviews": pv,
                "avg_engagement_time_seconds": round(avg_eng_time, 1),
                "engagement_rate": round(avg_eng_rate, 3),
            }
        )

    if not items:
        return []

    max_pv = max(i["pageviews"] for i in items) or 1
    max_et = max(i["avg_engagement_time_seconds"] for i in items) or 1

    for i in items:
        pv_norm = i["pageviews"] / max_pv
        et_norm = i["avg_engagement_time_seconds"] / max_et
        er = min(max(i["engagement_rate"], 0), 1)
        i["score"] = round(pv_norm * 0.4 + et_norm * 0.4 + er * 0.2, 2)

    items.sort(key=lambda x: x["score"], reverse=True)
    return items


# ============================================================
# EVOLUCION GLOBAL
# ============================================================

def fetch_daily_global(client, period_days=60):
    """Serie diaria de KPIs globales."""
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=[DateRange(start_date=f"{period_days}daysAgo", end_date="today")],
        dimensions=[Dimension(name="date")],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="sessions"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
        ],
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
        limit=200,
    )
    return client.run_report(request)


def parse_daily(response) -> list[dict]:
    """Convierte el response en lista ordenada por fecha asc."""
    rows = []
    for r in response.rows:
        date_raw = r.dimension_values[0].value  # YYYYMMDD
        fecha = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        rows.append({
            "fecha": fecha,
            "pageviews": int(r.metric_values[0].value),
            "sessions": int(r.metric_values[1].value),
            "engagement_time_total": float(r.metric_values[2].value),
            "engagement_rate": float(r.metric_values[3].value),
        })
    rows.sort(key=lambda x: x["fecha"])
    return rows


def compute_kpis(serie: list[dict], window_days: int) -> dict:
    """Compara metricas window_days vs window_days anteriores."""
    if len(serie) < window_days * 2:
        # No hay data suficiente, devuelve solo actual sin comparacion
        actual = serie[-window_days:] if serie else []
        return {
            "pageviews": _kpi_block(actual, [], "pageviews", agg="sum"),
            "sessions": _kpi_block(actual, [], "sessions", agg="sum"),
            "avg_engagement_time": _kpi_block_time(actual, []),
            "engagement_rate": _kpi_block(actual, [], "engagement_rate", agg="avg"),
        }
    actual = serie[-window_days:]
    anterior = serie[-(window_days * 2):-window_days]
    return {
        "pageviews": _kpi_block(actual, anterior, "pageviews", agg="sum"),
        "sessions": _kpi_block(actual, anterior, "sessions", agg="sum"),
        "avg_engagement_time": _kpi_block_time(actual, anterior),
        "engagement_rate": _kpi_block(actual, anterior, "engagement_rate", agg="avg"),
    }


def _kpi_block(actual, anterior, field, agg="sum") -> dict:
    a_val = _aggregate([x[field] for x in actual], agg)
    p_val = _aggregate([x[field] for x in anterior], agg)
    return {
        "actual": round(a_val, 3),
        "anterior": round(p_val, 3),
        "delta_pct": _delta_pct(a_val, p_val),
    }


def _kpi_block_time(actual, anterior) -> dict:
    """Avg engagement time = total engagement / total pageviews."""
    def avg_time(rows):
        total_pv = sum(x["pageviews"] for x in rows)
        total_et = sum(x["engagement_time_total"] for x in rows)
        return (total_et / total_pv) if total_pv > 0 else 0
    a_val = avg_time(actual)
    p_val = avg_time(anterior)
    return {
        "actual": round(a_val, 1),
        "anterior": round(p_val, 1),
        "delta_pct": _delta_pct(a_val, p_val),
    }


def _aggregate(values, agg):
    if not values:
        return 0
    if agg == "sum":
        return sum(values)
    if agg == "avg":
        return sum(values) / len(values)
    raise ValueError(f"unknown agg {agg}")


def _delta_pct(actual, anterior):
    if anterior == 0:
        return None if actual == 0 else 100.0
    return round(((actual - anterior) / anterior) * 100, 1)


def build_sparkline_serie(serie: list[dict], days: int) -> dict:
    last = serie[-days:] if len(serie) >= days else serie
    return {
        "fechas": [r["fecha"] for r in last],
        "pageviews": [r["pageviews"] for r in last],
        "sessions": [r["sessions"] for r in last],
    }


# ============================================================
# TRENDING POR DESTINO
# ============================================================

def fetch_destinos_window(client, start_days_ago: int, end_days_ago: int = 0):
    """Pageviews por pagePath en una ventana especifica."""
    end = "today" if end_days_ago == 0 else f"{end_days_ago}daysAgo"
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        date_ranges=[DateRange(start_date=f"{start_days_ago}daysAgo", end_date=end)],
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews")],
        limit=10000,
    )
    return client.run_report(request)


def aggregate_destinos_pv(response) -> dict:
    agg = {}
    for row in response.rows:
        slug = extract_slug(row.dimension_values[0].value)
        if not slug:
            continue
        agg[slug] = agg.get(slug, 0) + int(row.metric_values[0].value)
    return agg


def compute_trends(actual: dict, anterior: dict):
    all_slugs = set(actual.keys()) | set(anterior.keys())
    trends = []
    for slug in all_slugs:
        a = actual.get(slug, 0)
        p = anterior.get(slug, 0)
        if max(a, p) < TREND_MIN_PV:
            continue
        delta_pct = _delta_pct(a, p)
        trends.append({
            "slug": slug,
            "actual": a,
            "anterior": p,
            "delta_abs": a - p,
            "delta_pct": delta_pct if delta_pct is not None else 0,
        })
    return trends


# ============================================================
# MAIN
# ============================================================

def save_json(items: list[dict], evolucion: dict) -> None:
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": PERIOD_DAYS,
        "property_id": PROPERTY_ID,
        "is_mock": False,
        "destinos": items,
        "evolucion": evolucion,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK: {len(items)} destinos + evolucion -> {OUTPUT_PATH}")


def main() -> None:
    client = get_client()

    print(f"Consultando GA4 property {PROPERTY_ID} (ultimos {PERIOD_DAYS} dias)...")
    resp = fetch_destinos_metrics(client, PERIOD_DAYS)
    print(f"  Recibidas {len(resp.rows)} filas crudas")
    agg = aggregate_by_slug(resp)
    print(f"  Agrupadas en {len(agg)} destinos unicos")
    items = compute_scores(agg)
    print(f"  {len(items)} destinos rankeados (>= {MIN_PAGEVIEWS} vistas)")

    print(f"\nCalculando evolucion (ventana {KPI_WINDOW_DAYS}d vs {KPI_WINDOW_DAYS}d)...")
    daily_resp = fetch_daily_global(client, period_days=max(SPARKLINE_DAYS, KPI_WINDOW_DAYS * 2))
    serie = parse_daily(daily_resp)
    print(f"  Serie diaria: {len(serie)} dias")
    kpis = compute_kpis(serie, KPI_WINDOW_DAYS)
    sparkline = build_sparkline_serie(serie, SPARKLINE_DAYS)

    print(f"Calculando trending por destino (ventana {KPI_WINDOW_DAYS}d)...")
    actual_resp = fetch_destinos_window(client, KPI_WINDOW_DAYS, 0)
    anterior_resp = fetch_destinos_window(client, KPI_WINDOW_DAYS * 2, KPI_WINDOW_DAYS)
    actual = aggregate_destinos_pv(actual_resp)
    anterior = aggregate_destinos_pv(anterior_resp)
    trends = compute_trends(actual, anterior)
    trending_up = sorted(trends, key=lambda x: x["delta_pct"], reverse=True)[:TREND_TOP_N]
    trending_down = sorted(trends, key=lambda x: x["delta_pct"])[:TREND_BOTTOM_N]
    print(f"  Trending: up={len(trending_up)} down={len(trending_down)}")

    evolucion = {
        "window_days": KPI_WINDOW_DAYS,
        "sparkline_days": SPARKLINE_DAYS,
        "kpis": kpis,
        "sparkline": sparkline,
        "trending_up": trending_up,
        "trending_down": trending_down,
    }

    save_json(items, evolucion)


if __name__ == "__main__":
    main()
