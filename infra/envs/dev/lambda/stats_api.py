# file: infra/envs/dev/lambda/stats_api.py
import os, json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import boto3
from boto3.dynamodb.conditions import Key

TZ = ZoneInfo("America/New_York")

USER_ID      = os.environ.get("USER_ID", "me")
MEALS_TABLE  = os.environ["MEALS_TABLE"]
TOTALS_TABLE = os.environ["TOTALS_TABLE"]
MIGS_TABLE   = os.environ["MIGRAINES_TABLE"]
MEDS_TABLE   = os.environ["MEDS_TABLE"]

CALORIES_MAX = int(os.environ.get("CALORIES_MAX", "1800"))
PROTEIN_MIN  = int(os.environ.get("PROTEIN_MIN",  "190"))

ddb        = boto3.resource("dynamodb")
meals_tbl  = ddb.Table(MEALS_TABLE)
totals_tbl = ddb.Table(TOTALS_TABLE)
migs_tbl   = ddb.Table(MIGS_TABLE)
meds_tbl   = ddb.Table(MEDS_TABLE)

def _today() -> date:
    return datetime.now(TZ).date()

def _start_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())

def _start_of_month(d: date) -> date:
    return d.replace(day=1)

def _resp(o, code=200):
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "*",
        },
        "body": json.dumps(o, default=str),
    }

# ----- Dynamo helpers -----
def _get_totals_range(d0: date, d1: date):
    """Return list of totals rows between d0..d1 inclusive."""
    resp = totals_tbl.query(
        KeyConditionExpression=Key("pk").eq(f"total#{USER_ID}") &
                               Key("sk").between(d0.isoformat(), d1.isoformat())
    )
    return resp.get("Items", [])

def _sum_rows(rows):
    cal = pro = carb = fat = 0
    for it in rows:
        cal += int(it.get("calories", 0))
        pro += int(it.get("protein", 0))
        carb += int(it.get("carbs", 0))
        fat += int(it.get("fat", 0))
    return {"cal": cal, "pro": pro, "carb": carb, "fat": fat}

def _get_meals_for_day(d: date):
    pfx = f"{d.isoformat()}#"
    resp = meals_tbl.query(
        KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with(pfx)
    )
    items = []
    for it in resp.get("Items", []):
        items.append({
            "text": it.get("raw_text"),
            "kcal": int(it.get("kcal", 0)),
            "protein_g": int(it.get("protein_g", 0)),
            "carbs_g": int(it.get("carbs_g", 0)),
            "fat_g": int(it.get("fat_g", 0)),
            "created_ms": int(it.get("created_ms", 0)),
        })
    return items

def _loop_days_gsi_dt(tbl, d0: date, d1: date):
    """Yield all items for dt in [d0..d1] using IndexName='gsi_dt' (hash = dt)."""
    cur = d0
    while cur <= d1:
        last = None
        while True:
            kwargs = {
                "IndexName": "gsi_dt",
                "KeyConditionExpression": Key("dt").eq(cur.isoformat()),
            }
            if last:
                kwargs["ExclusiveStartKey"] = last
            q = tbl.query(**kwargs)
            for it in q.get("Items", []):
                yield it
            last = q.get("LastEvaluatedKey")
            if not last:
                break
        cur += timedelta(days=1)

def _get_meds_month(d: date):
    d0, d1 = _start_of_month(d), d
    out = []
    for it in _loop_days_gsi_dt(meds_tbl, d0, d1):
        out.append({
            "dt"     : it.get("dt"),
            "ts_ms"  : int(it.get("ts_ms", 0)),
            "category": it.get("category"),
            "dose"   : it.get("dose",""),
            "text"   : it.get("text",""),
        })
    return out

def _get_migraines_month(d: date):
    d0, d1 = _start_of_month(d), d
    out = []
    for it in _loop_days_gsi_dt(migs_tbl, d0, d1):
        out.append({
            "dt": it.get("dt"),
            "episode_id": it.get("episode_id"),
            "category": it.get("category","non-aura"),
            "start_ms": int(it.get("start_ms", 0)),
            "end_ms"  : int(it.get("end_ms", 0)),
            "is_open" : int(it.get("is_open", 0)),
            "notes"   : it.get("notes",""),
        })
    return out

# ----- Route handlers -----
def _today_stats():
    d = _today()
    rows = _get_totals_range(d, d)
    sums = _sum_rows(rows)
    return {
        "date": d.isoformat(),
        "calories": sums["cal"],
        "protein":  sums["pro"],
        "carbs":    sums["carb"],
        "fat":      sums["fat"],
        "goals": {"calories_max": CALORIES_MAX, "protein_min": PROTEIN_MIN},
        "meals": _get_meals_for_day(d),
    }

def _week_stats():
    d1 = _today()
    d0 = d1 - timedelta(days=6)
    rows = _get_totals_range(d0, d1)
    sums = _sum_rows(rows)
    days = (d1 - d0).days + 1
    return {
        "start": d0.isoformat(), "end": d1.isoformat(),
        "sum":  sums,
        "avg":  {"cal": round(sums["cal"]/days,1), "pro": round(sums["pro"]/days,1),
                 "carb": round(sums["carb"]/days,1), "fat": round(sums["fat"]/days,1)},
        "goals": {"calories_max": CALORIES_MAX, "protein_min": PROTEIN_MIN},
    }

def _month_stats():
    d1 = _today()
    d0 = _start_of_month(d1)
    rows = _get_totals_range(d0, d1)
    sums = _sum_rows(rows)
    days = (d1 - d0).days + 1
    return {
        "start": d0.isoformat(), "end": d1.isoformat(),
        "sum":  sums,
        "avg":  {"cal": round(sums["cal"]/days,1), "pro": round(sums["pro"]/days,1),
                 "carb": round(sums["carb"]/days,1), "fat": round(sums["fat"]/days,1)},
        "goals": {"calories_max": CALORIES_MAX, "protein_min": PROTEIN_MIN},
    }

# ----- Lambda entrypoint -----
def lambda_handler(event, context):
    # Simple router on path
    path = (event.get("rawPath") or event.get("path") or "/").lower()
    qs   = event.get("queryStringParameters") or {}
    if path.endswith("/stats/today"):
        return _resp(_today_stats())
    if path.endswith("/stats/week"):
        return _resp(_week_stats())
    if path.endswith("/stats/month"):
        return _resp(_month_stats())
    if path.endswith("/stats/meals"):
        # optional ?date=YYYY-MM-DD, default today
        try:
            d = date.fromisoformat((qs.get("date") or _today().isoformat()))
        except Exception:
            return _resp({"error":"bad date"}, 400)
        return _resp({"date": d.isoformat(), "meals": _get_meals_for_day(d)})
    if path.endswith("/stats/meds/month"):
        return _resp({"items": _get_meds_month(_today())})
    if path.endswith("/stats/migraines/month"):
        return _resp({"items": _get_migraines_month(_today())})
    if path.endswith("/stats/health"):
        # one call that returns everything useful for a dashboard
        d = _today()
        return _resp({
            "today": _today_stats(),
            "week":  _week_stats(),
            "month": _month_stats(),
            "meds_month": _get_meds_month(d),
            "migraines_month": _get_migraines_month(d),
        })

    # CORS preflight / unknown
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _resp({}, 200)

    return _resp({"message":"ok", "paths":[
        "/stats/today", "/stats/week", "/stats/month",
        "/stats/meals?date=YYYY-MM-DD",
        "/stats/meds/month", "/stats/migraines/month",
        "/stats/health"
    ]})
