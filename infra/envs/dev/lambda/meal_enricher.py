# file: infra/envs/dev/lambda/meal_enricher.py
import os, json, time, hashlib, logging, re, random
from decimal import Decimal
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, date
import boto3
import requests
from boto3.dynamodb.conditions import Key, Attr
from difflib import get_close_matches

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---- AWS clients / env ----
secrets = boto3.client("secretsmanager")
ddb = boto3.resource("dynamodb")

MEALS_TABLE   = os.environ["MEALS_TABLE"]        # hb_meals_dev
TOTALS_TABLE  = os.environ["TOTALS_TABLE"]       # hb_daily_totals_dev
EVENTS_TABLE  = os.environ["EVENTS_TABLE"]       # hb_events_dev
USER_ID       = os.environ.get("USER_ID", "me")

# Simulation toggle (/test or env)
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# Medication safety limits (env overrideable) – interpreted as **doses per month**
TRIPTAN_LIMIT_DPM = int(os.environ.get("TRIPTAN_LIMIT_DPM", "8"))
DHE_LIMIT_DPM     = int(os.environ.get("DHE_LIMIT_DPM", "6"))
COMBO_LIMIT_DPM   = int(os.environ.get("COMBO_LIMIT_DPM", "8"))     # e.g., Excedrin
PAIN_LIMIT_DPM    = int(os.environ.get("PAIN_LIMIT_DPM", "12"))     # simple analgesics/NSAIDs
NEAR_THRESH_PCT   = float(os.environ.get("NEAR_THRESH_PCT", "0.75"))

FOOD_OVERRIDES_TABLE = os.environ.get("FOOD_OVERRIDES_TABLE", "hb_food_overrides_dev")
over_tbl = ddb.Table(FOOD_OVERRIDES_TABLE)

# NEW tables
MIGRAINES_TABLE = os.environ.get("MIGRAINES_TABLE", "hb_migraines_dev")
MEDS_TABLE      = os.environ.get("MEDS_TABLE", "hb_meds_dev")
FASTING_TABLE   = os.environ.get("FASTING_TABLE", "hb_fasting_dev")
FACTS_TABLE     = os.environ.get("FACTS_TABLE", "hb_migraine_facts_dev")

fast_tbl        = ddb.Table(FASTING_TABLE)
facts_tbl       = ddb.Table(FACTS_TABLE) if FACTS_TABLE else None

NUTRITION_SECRET_NAME = os.environ["NUTRITION_SECRET_NAME"]
TWILIO_SECRET_NAME    = os.environ.get("TWILIO_SECRET_NAME", "hb_twilio_dev")

# Goals (defaults; can be overridden in TF env)
CALORIES_MAX = int(os.environ.get("CALORIES_MAX", "1800"))
PROTEIN_MIN  = int(os.environ.get("PROTEIN_MIN",  "190"))

meals_tbl   = ddb.Table(MEALS_TABLE)
totals_tbl  = ddb.Table(TOTALS_TABLE)
events_tbl  = ddb.Table(EVENTS_TABLE)
migs_tbl    = ddb.Table(MIGRAINES_TABLE)
meds_tbl    = ddb.Table(MEDS_TABLE)

TZ = ZoneInfo("America/New_York")

# ---------- helpers ----------
def _nx_headers():
    sec = secrets.get_secret_value(SecretId=NUTRITION_SECRET_NAME)
    cfg = json.loads(sec["SecretString"])
    return {
        "x-app-id": cfg["app_id"],
        "x-app-key": cfg["app_key"],
        "Content-Type": "application/json",
    }

class NxError(Exception): ...
def _now_ms() -> int: return int(time.time() * 1000)

def _extract_macros_from_food(it: dict) -> tuple[int,int,int,int,str,int,int,str]:
    """Return kcal, protein_g, carbs_g, fat_g, display_name, serving_qty, serving_unit, brand_or_empty."""
    if not it:
        return 0,0,0,0,"item",0,0,""
    kcal = int(round(it.get("nf_calories", 0) or 0))
    pro  = int(round(it.get("nf_protein", 0) or 0))
    carb = int(round(it.get("nf_total_carbohydrate", 0) or 0))
    fat  = int(round(it.get("nf_total_fat", 0) or 0))
    name = it.get("food_name") or it.get("brand_name_item_name") or it.get("food_name_original") or "item"
    qty  = it.get("serving_qty") or 1
    unit = it.get("serving_unit") or ""
    brand = it.get("brand_name") or ""
    return kcal, pro, carb, fat, name, qty, unit, brand

def _nutritionix(query: str):
    """
    Robust Nutritionix call with clear errors and a 'matched items' trace.
    Returns: (total_macros_dict, items_list) or raises NxError on failure.
    """
    headers = _nx_headers()
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    try:
        r = requests.post(url, headers=headers, json={"query": query}, timeout=12)
    except requests.Timeout:
        raise NxError("Nutritionix timed out")
    except Exception as e:
        raise NxError(f"Nutritionix request failed: {e}")

    if r.status_code == 429:
        raise NxError("Nutritionix rate limited (429)")
    if r.status_code >= 500:
        raise NxError(f"Nutritionix server error {r.status_code}")
    if r.status_code >= 400:
        # Keep it short but informative
        body = r.text[:200].replace("\n"," ")
        raise NxError(f"Nutritionix error {r.status_code}: {body}")

    data = r.json()
    foods = data.get("foods", []) or []
    if not foods:
        raise NxError("Nutritionix returned no matches")

    total = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    items_trimmed = []
    for f in foods[:20]:
        kcal, pro, carb, fat, name, qty, unit, brand = _extract_macros_from_food(f)
        total["calories"] += kcal; total["protein"] += pro; total["carbs"] += carb; total["fat"] += fat
        items_trimmed.append({
            "name": name if not brand else f"{brand} {name}",
            "serving_qty": qty,
            "serving_unit": unit,
            "kcal": kcal, "protein_g": pro, "carbs_g": carb, "fat_g": fat
        })
    return total, items_trimmed

def _nutritionix_lookup_upc(upc: str) -> dict | None:
    """
    Try UPC → /search/item?upc. If 404/empty, fallback to instant search and hydrate via nix_item_id.
    Returns a single Nutritionix 'food' dict or None.
    """
    headers = _nx_headers()

    # 1) Primary UPC lookup
    try:
        r = requests.get(
            "https://trackapi.nutritionix.com/v2/search/item",
            headers=headers,
            params={"upc": upc},
            timeout=10
        )
        if r.status_code == 200:
            foods = (r.json() or {}).get("foods", []) or []
            if foods: return foods[0]
        elif r.status_code not in (404, 400):
            log.warning(f"UPC lookup HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.exception(f"UPC lookup exception: {e}")

    # 2) Fallback: instant search → hydrate
    try:
        r2 = requests.get(
            "https://trackapi.nutritionix.com/v2/search/instant",
            headers={k:v for k,v in headers.items() if k != "Content-Type"},
            params={"query": upc},
            timeout=10
        )
        if r2.status_code == 200:
            data = r2.json() or {}
            branded = data.get("branded", []) or []
            if branded:
                nix_id = branded[0].get("nix_item_id")
                if nix_id:
                    r3 = requests.get(
                        "https://trackapi.nutritionix.com/v2/search/item",
                        headers=headers,
                        params={"nix_item_id": nix_id},
                        timeout=10
                    )
                    if r3.status_code == 200:
                        foods = (r3.json() or {}).get("foods", []) or []
                        if foods: return foods[0]
    except Exception as e:
        log.exception(f"Instant fallback exception: {e}")

    return None

def _norm_alias(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _today_est_from_ts(ts_ms: int | None) -> str:
    if ts_ms is None:
        return datetime.now(TZ).date().isoformat()
    return datetime.fromtimestamp(ts_ms/1000, tz=TZ).date().isoformat()

def _as_decimal(n) -> Decimal:
    try:
        if n is None: return Decimal("0")
        d = Decimal(str(n));  return d if d >= 0 else Decimal("0")
    except Exception:
        return Decimal("0")

def _as_decimal_signed(n) -> Decimal:
    try:
        return Decimal(str(n))
    except Exception:
        return Decimal("0")

def _decimalize_tree(x):
    if isinstance(x, float):   return _as_decimal(x)
    if isinstance(x, int):     return _as_decimal(x)
    if isinstance(x, Decimal): return x
    if isinstance(x, dict):    return {k: _decimalize_tree(v) for k, v in x.items()}
    if isinstance(x, list):    return [_decimalize_tree(v) for v in x]
    return x

def _parse_ts(val):
    try:    return int(val)
    except: return _now_ms()

def _parse_when_to_ms(tokens: list[str]) -> int:
    """Parse simple time phrases in EST."""
    text = " ".join(tokens).strip().lower()
    if not text or text == "now": return _now_ms()

    base_dt = datetime.now(TZ)
    if "yesterday" in text:
        base_dt = base_dt - timedelta(days=1)
        text = text.replace("yesterday", "").strip()

    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2})\s*(am|pm)?)?$", text)
    if m:
        y,mn,d = map(int, m.group(1).split("-"))
        hh = int(m.group(2) or 0); mm = int(m.group(3) or 0); ap = (m.group(4) or "").lower()
        if ap in ("am","pm"):
            if hh == 12: hh = 0
            if ap == "pm": hh += 12
        t = datetime(y, mn, d, hh, mm, tzinfo=TZ)
        return int(t.timestamp()*1000)

    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2) or 0); ap = (m.group(3) or "").lower()
        if ap in ("am","pm"):
            if hh == 12: hh = 0
            if ap == "pm": hh += 12
        t = datetime(base_dt.year, base_dt.month, base_dt.day, hh, mm, tzinfo=TZ)
        return int(t.timestamp()*1000)

    m = re.match(r"(today)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text)
    if m:
        hh = int(m.group(2)); mm = int(m.group(3) or 0); ap = (m.group(4) or "").lower()
        if ap in ("am","pm"):
            if hh == 12: hh = 0
            if ap == "pm": hh += 12
        t = datetime(base_dt.year, base_dt.month, base_dt.day, hh, mm, tzinfo=TZ)
        return int(t.timestamp()*1000)

    return _now_ms()

def _month_bounds_est(ts_ms: int | None = None):
    now = datetime.now(TZ) if ts_ms is None else datetime.fromtimestamp(ts_ms/1000, TZ)
    start = now.replace(day=1).date()
    end = now.date()
    return start, end

def _format_duration_mins(mins: int) -> str:
    h = mins // 60; m = mins % 60
    return f"{h}h {m:02d}m"

# ----- Food override helpers -----
def _get_override(alias: str):
    try:
        r = over_tbl.get_item(Key={"pk": USER_ID, "sk": _norm_alias(alias)})
        return r.get("Item")
    except Exception:
        return None

def _put_override(alias: str, macros: dict, note: str = ""):
    item = {
        "pk": USER_ID, "sk": _norm_alias(alias),
        "kcal": _as_decimal(macros.get("calories",0)),
        "protein": _as_decimal(macros.get("protein",0)),
        "carbs": _as_decimal(macros.get("carbs",0)),
        "fat": _as_decimal(macros.get("fat",0)),
        "note": note, "updated_ms": _as_decimal(_now_ms())
    }
    over_tbl.put_item(Item=item)
    return item

def _del_override(alias: str):
    over_tbl.delete_item(Key={"pk": USER_ID, "sk": _norm_alias(alias)})

def _match_override_in_text(text: str):
    """Return (alias, qty) if meal text contains a known alias; supports '2x alias' or 'alias x2'."""
    t = _norm_alias(text)
    m = re.search(r"(\d+)\s*x\s+([a-z].+)$", t)
    if m:
        qty = int(m.group(1)); alias = _norm_alias(m.group(2))
        if _get_override(alias): return alias, qty
    m = re.search(r"([a-z].+?)\s*x\s*(\d+)$", t)
    if m:
        alias = _norm_alias(m.group(1)); qty = int(m.group(2))
        if _get_override(alias): return alias, qty
    parts = re.split(r"[,+/]| and ", t)
    for p in parts:
        a = _norm_alias(p)
        if _get_override(a): return a, 1
    return None, 0

def _macros_from_override(alias: str, qty: int = 1):
    it = _get_override(alias)
    if not it: return None
    q = max(1, int(qty))
    return {
        "calories": int(it.get("kcal",0))*q,
        "protein": int(it.get("protein",0))*q,
        "carbs":   int(it.get("carbs",0))*q,
        "fat":     int(it.get("fat",0))*q
    }

# ----- Facts helpers -----
FACTS_DEFAULT_HOUR = int(os.environ.get("FACTS_DEFAULT_HOUR", "9"))

def _facts_available() -> bool:
    return facts_tbl is not None

def _facts_config_key() -> dict:
    return {"pk": USER_ID, "sk": "config#facts"}

def _fact_get_config() -> dict:
    if not _facts_available(): return {}
    try:
        return facts_tbl.get_item(Key=_facts_config_key()).get("Item") or {}
    except Exception:
        return {}

def _fact_set_config(**kwargs) -> dict:
    if not _facts_available(): return {}
    item = _fact_get_config()
    item.update(_facts_config_key())
    item.update(kwargs)
    facts_tbl.put_item(Item=item)
    return item

def _fact_pick_random(tag: str | None = None) -> dict | None:
    if not _facts_available(): return None
    try:
        resp = facts_tbl.query(
            KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("fact#")
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = facts_tbl.query(
                KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("fact#"),
                ExclusiveStartKey=resp["LastEvaluatedKey"]
            )
            items.extend(resp.get("Items", []))
    except Exception as e:
        log.exception("facts query failed")
        return None

    if tag:
        tag_low = tag.lower()
        items = [it for it in items if tag_low in [t.lower() for t in it.get("tags", [])]]
    if not items:
        return None
    return random.choice(items)

def _fact_status_text() -> str:
    if not _facts_available():
        return "Migraine facts feature is not configured yet."
    cfg = _fact_get_config()
    enabled = bool(cfg.get("daily_enabled", False))
    hour = int(cfg.get("daily_hour", FACTS_DEFAULT_HOUR))
    to_number = cfg.get("to_number") or "(unset)"
    last_dt = cfg.get("last_sent_dt") or "never"
    state = "✅ enabled" if enabled else "⏸️ disabled"
    return (
        f"Daily facts are {state}.\n"
        f"Hour: {hour:02d}:00\n"
        f"Send to: {to_number}\n"
        f"Last sent: {last_dt}"
    )

def _normalize_hour(raw: str | int) -> int:
    try:
        hour = int(raw)
    except Exception:
        return FACTS_DEFAULT_HOUR
    return max(0, min(23, hour))

# ----- Meds helpers -----
def _med_category_key(cat: str) -> str:
    c = (cat or "").lower()
    if c in ("triptan",): return "triptan"
    if c in ("dhe","d.h.e.","d.h.e"): return "dhe"
    if c in ("excedrin",): return "excedrin"
    if c in ("normal pain med","nsaid","ibuprofen","naproxen","acetaminophen","tylenol","advil","aleve"):
        return "normal pain med"
    return c or "unknown"

def _med_limits_for(cat_key: str) -> int | None:
    return {
        "triptan": TRIPTAN_LIMIT_DPM,
        "dhe": DHE_LIMIT_DPM,
        "excedrin": COMBO_LIMIT_DPM,
        "normal pain med": PAIN_LIMIT_DPM,
    }.get(cat_key)

def _count_med_doses_this_month(cat_key: str) -> int:
    """Count total doses (items) this month for a given category."""
    start, end = _month_bounds_est()
    total = 0
    d = start
    while d <= end:
        q = meds_tbl.query(
            IndexName="gsi_dt",
            KeyConditionExpression=Key("dt").eq(d.isoformat())
        )
        items = q.get("Items", [])
        total += sum(1 for it in items if _med_category_key(it.get("category","")) == cat_key)
        while "LastEvaluatedKey" in q:
            q = meds_tbl.query(
                IndexName="gsi_dt",
                KeyConditionExpression=Key("dt").eq(d.isoformat()),
                ExclusiveStartKey=q["LastEvaluatedKey"],
            )
            items = q.get("Items", [])
            total += sum(1 for it in items if _med_category_key(it.get("category","")) == cat_key)
        d += timedelta(days=1)
    return total

def _send_sms(to_number: str, body: str) -> None:
    sec = secrets.get_secret_value(SecretId=TWILIO_SECRET_NAME)["SecretString"]
    cfg = json.loads(sec)
    account_sid = cfg["account_sid"]; auth_token  = cfg["auth_token"]
    from_number = cfg.get("from_wa", "whatsapp:+14155238886")  # sandbox
    if not to_number.startswith("whatsapp:"):
        to_number = "whatsapp:" + to_number.lstrip("+")
    url  = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {"From": from_number, "To": to_number, "Body": body}
    r = requests.post(url, data=data, auth=(account_sid, auth_token), timeout=10)
    if r.status_code >= 400:
        log.error(f"Twilio send failed {r.status_code}: {r.text}")

# ----- Daily totals / summaries -----
def _get_totals_for_day(dt: str) -> dict:
    key = {"pk": f"total#{USER_ID}", "sk": dt}
    r = totals_tbl.get_item(Key=key)
    return r.get("Item") or {"pk": key["pk"], "sk": key["sk"], "calories": 0, "protein": 0, "carbs": 0, "fat": 0}

def _format_summary_lines(totals: dict) -> str:
    cal = int(totals.get("calories", 0))
    pro = int(totals.get("protein", 0))
    carb = int(totals.get("carbs", 0))
    fat  = int(totals.get("fat", 0))
    lines = [f"Today so far (EST): {cal} / {CALORIES_MAX} kcal, {pro} / {PROTEIN_MIN} P, {carb} C, {fat} F."]
    if cal > CALORIES_MAX:
        lines.append(f"⚠️ Over calorie goal by {cal - CALORIES_MAX} kcal.")
    if pro < PROTEIN_MIN:
        lines.append(f"💪 {PROTEIN_MIN - pro} g protein left today.")
    return "\n".join(lines)

def _format_matched_lines(items: list[dict], max_items: int = 6) -> str:
    if not items: return "(no item names returned)"
    lines = []
    for it in items[:max_items]:
        nm = it.get("name") or "item"
        qty = it.get("serving_qty") or 1
        unit = it.get("serving_unit") or ""
        kcal = int(it.get("kcal", 0))
        p = it.get("protein_g", 0)
        lines.append(f"- {nm} x{qty} {unit} → {kcal} kcal, {p} g P")
    return "\n".join(lines)

def _format_meal_reply(macros: dict, new_totals: dict, matched_items: list[dict] | None = None, prefix: str = "Meal saved") -> str:
    mcal = int(macros.get("calories", 0))
    mp   = int(macros.get("protein", 0))
    mc   = int(macros.get("carbs", 0))
    mf   = int(macros.get("fat", 0))
    cal = int(new_totals.get("calories", 0))
    pro = int(new_totals.get("protein", 0))
    lines = [
        f"{prefix}",
    ]
    if matched_items:
        lines.append("Matched:")
        lines.append(_format_matched_lines(matched_items))
    lines.extend([
        f"Total this meal → {mcal} kcal ({mp} P / {mc} C / {mf} F).",
        f"Today’s total (EST): {cal} / {CALORIES_MAX} kcal, {pro} / {PROTEIN_MIN} P."
    ])
    if cal > CALORIES_MAX:
        lines.append(f"⚠️ Over calorie goal by {cal - CALORIES_MAX} kcal.")
    if pro < PROTEIN_MIN:
        lines.append(f"💪 {PROTEIN_MIN - pro} g protein left today.")
    return "\n".join(lines)

def _sum_range(start_d: date, end_d: date) -> dict:
    resp = totals_tbl.query(
        KeyConditionExpression=Key("pk").eq(f"total#{USER_ID}") &
                               Key("sk").between(start_d.isoformat(), end_d.isoformat())
    )
    cal = pro = carb = fat = 0
    for it in resp.get("Items", []):
        cal += int(it.get("calories", 0))
        pro += int(it.get("protein", 0))
        carb += int(it.get("carbs", 0))
        fat  += int(it.get("fat", 0))
    days = (end_d - start_d).days + 1
    return {"cal": cal, "pro": pro, "carb": carb, "fat": fat, "days": days,
            "avg_cal": round(cal / days, 1), "avg_pro": round(pro / days, 1)}

# ----- Barcode / food overrides -----
def _parse_macros_arg(s: str) -> dict | None:
    if not s: return None
    out = {"calories":0,"protein":0,"carbs":0,"fat":0}
    for part in re.split(r"[,\s]+", s.strip()):
        if not part or "=" not in part: continue
        k,v = part.split("=",1)
        try: v = int(float(v))
        except: continue
        k = k.lower()
        if k in ("k","kcal","cal","calories"): out["calories"] = v
        elif k in ("p","protein","prot"): out["protein"] = v
        elif k in ("c","carbs","carb"): out["carbs"] = v
        elif k in ("f","fat"): out["fat"] = v
    return out

def _safe_alias(name: str) -> str:
    alias = re.sub(r"[^a-z0-9]+","-", (name or "").lower()).strip("-")
    return alias or "item"

def _handle_barcode(sender: str, args: str):
    """
    /barcode <digits>
    Accepts EAN/UPC lengths (8, 12, 13, 14). Keeps leading zeros.
    """
    upc = "".join(ch for ch in (args or "") if ch.isdigit())
    if not upc.isdigit() or len(upc) not in (8, 12, 13, 14):
        _send_sms(sender, "Usage: /barcode <UPC/EAN digits> (8/12/13/14)."); 
        return

    item = _nutritionix_lookup_upc(upc)
    if not item:
        _send_sms(
            sender, 
            "Barcode not found. You can set it once and reuse:\n"
            f"→ /food set item_{upc} k=<kcal> p=<P> c=<C> f=<F>\n"
            "Then log: meal: item_{upc}"
        )
        return

    kcal, pro, carb, fat, name, qty, unit, brand = _extract_macros_from_food(item)
    alias = _safe_alias(f"{brand} {name}".strip())
    tip = f"/food set {alias} k={kcal} p={pro} c={carb} f={fat}"
    _send_sms(
        sender,
        "Barcode match:\n"
        f"- {(brand + ' ') if brand else ''}{name}\n"
        f"- Serving: {qty} {unit}\n"
        f"- Macros: {kcal} kcal, {pro} g P, {carb} g C, {fat} g F\n"
        f"Cache it for speed:\n{tip}"
    )

def _handle_food(sender: str, args: str):
    if not args:
        _send_sms(sender, "Usage: /food set <alias> k=.. p=.. c=.. f=.. | /food del <alias> | /food list"); return
    parts = args.split()
    sub = parts[0].lower()

    if sub == "list":
        r = over_tbl.query(KeyConditionExpression=Key("pk").eq(USER_ID))
        items = r.get("Items", [])
        if not items: _send_sms(sender, "No custom foods."); return
        lines = ["Custom foods:"]
        for it in items[:20]:
            lines.append(f"• {it['sk']}: {int(it['kcal'])} kcal, P{int(it['protein'])} C{int(it['carbs'])} F{int(it['fat'])}")
        _send_sms(sender, "\n".join(lines)); return

    if sub == "del" and len(parts) >= 2:
        alias = _norm_alias(" ".join(parts[1:]))
        _del_override(alias)
        _send_sms(sender, f"Deleted custom food: {alias}")
        return

    if sub == "set" and len(parts) >= 3:
        macro_start = None
        for idx, token in enumerate(parts[2:], start=2):
            if "=" in token:
                macro_start = idx
                break
        if macro_start is None:
            _send_sms(sender, "Provide macros, e.g. k=190 p=42 c=0 f=1"); return
        alias_tokens = parts[1:macro_start] or [parts[1]]
        alias = _norm_alias(" ".join(alias_tokens))
        macro_str = " ".join(parts[macro_start:])
        macros = _parse_macros_arg(macro_str)
        if not macros or sum(macros.values()) == 0:
            _send_sms(sender, "Provide macros, e.g. k=190 p=42 c=0 f=1"); return
        _put_override(alias, macros)
        _send_sms(sender, f"Saved custom food: {alias} → {macros['calories']} kcal, P{macros['protein']} C{macros['carbs']} F{macros['fat']}")
        return

    _send_sms(sender, "Usage: /food set <alias> k=.. p=.. c=.. f=.. | /food del <alias> | /food list")

# ----- Week / month summaries -----
def _handle_week(sender: str):
    today = datetime.now(TZ).date()
    start = today - timedelta(days=6)
    s = _sum_range(start, today)
    msg = (f"This week (last {s['days']} days): {s['cal']} kcal, {s['pro']} P, {s['carb']} C, {s['fat']} F.\n"
           f"Daily avg: {s['avg_cal']} kcal vs {CALORIES_MAX}, {s['avg_pro']} P vs {PROTEIN_MIN}.")
    _send_sms(sender, msg)

def _handle_month(sender: str):
    today = datetime.now(TZ).date()
    start = today.replace(day=1)
    s = _sum_range(start, today)
    msg = (f"This month (to date): {s['cal']} kcal, {s['pro']} P, {s['carb']} C, {s['fat']} F.\n"
           f"Daily avg: {s['avg_cal']} kcal vs {CALORIES_MAX}, {s['avg_pro']} P vs {PROTEIN_MIN}.")
    _send_sms(sender, msg)

def _handle_fact(sender: str, args: str, simulate: bool = False):
    if not _facts_available():
        _send_sms(sender, "Facts feature not configured yet."); return
    tag = (args or "").strip()
    fact = _fact_pick_random(tag or None)
    if not fact:
        msg = "No facts available." + (f" (tag: {tag})" if tag else "")
        _send_sms(sender, msg)
        return
    body = f"🧠 Migraine fact:\n{fact.get('text','(missing text)')}"
    if simulate or DRY_RUN:
        _send_sms(sender, f"[TEST] Would send fact:\n{fact.get('text','')}")
        return
    _send_sms(sender, body)

def _handle_facts(sender: str, args: str, simulate: bool = False):
    if not _facts_available():
        _send_sms(sender, "Facts feature not configured yet."); return
    tokens = (args or "").strip().split()
    if not tokens:
        _send_sms(sender,
                  "Facts commands:\n"
                  "• /fact [tag] – send a random fact now\n"
                  "• /facts on [hour] – enable daily facts (default 09:00)\n"
                  "• /facts off – disable daily facts\n"
                  "• /facts hour <0-23> – update send hour\n"
                  "• /facts number <whatsapp:+1...> – set delivery number\n"
                  "• /facts status – show configuration")
        return
    sub = tokens[0].lower()
    cfg = _fact_get_config()
    if sub == "status":
        _send_sms(sender, _fact_status_text())
        return
    if sub == "on":
        hour = cfg.get("daily_hour", FACTS_DEFAULT_HOUR)
        if len(tokens) >= 2:
            hour = _normalize_hour(tokens[1])
        if simulate or DRY_RUN:
            _send_sms(sender, f"[TEST] Would enable daily facts at {hour:02d}:00 to {sender or '(unknown)'}")
            return
        _fact_set_config(daily_enabled=True, daily_hour=hour, to_number=sender)
        _send_sms(sender, f"Daily migraine facts enabled at {hour:02d}:00. Sending to {sender}.")
        return
    if sub == "off":
        if simulate or DRY_RUN:
            _send_sms(sender, "[TEST] Would disable daily migraine facts.")
            return
        _fact_set_config(daily_enabled=False)
        _send_sms(sender, "Daily migraine facts disabled.")
        return
    if sub == "hour" and len(tokens) >= 2:
        hour = _normalize_hour(tokens[1])
        if simulate or DRY_RUN:
            _send_sms(sender, f"[TEST] Would set daily facts hour to {hour:02d}:00.")
            return
        _fact_set_config(daily_hour=hour)
        _send_sms(sender, f"Daily migraine facts hour set to {hour:02d}:00.")
        return
    if sub in ("number", "to") and len(tokens) >= 2:
        target = " ".join(tokens[1:]).strip()
        if simulate or DRY_RUN:
            _send_sms(sender, f"[TEST] Would set facts delivery number to {target}.")
            return
        _fact_set_config(to_number=target)
        _send_sms(sender, f"Facts delivery number set to {target}.")
        return

    _send_sms(sender,
              "Usage: /fact [tag] | /facts on [hour] | /facts off | /facts hour <0-23> | /facts number <...> | /facts status")

# ---------- meals core ----------
def _write_enriched_event(meal_pk: str, ts_ms: int, dt: str, text: str, macros: dict, channel: str):
    events_tbl.put_item(Item={
        "pk": meal_pk, "sk": str(ts_ms),
        "type": "meal.enriched", "text": text,
        "nutrition": _decimalize_tree(macros), "dt": dt,
        "uid": USER_ID, "source": channel
    })

def _put_meal_row_idempotent(user_id: str, dt: str, ts_ms: int, sender: str, meal_text: str, macros: dict, channel: str):
    meal_id = _meal_id(user_id, dt, meal_text, ts_ms)
    item = {
        "pk": user_id, "sk": f"{dt}#{meal_id}", "dt": dt, "meal_id": meal_id,
        "raw_text": meal_text, "raw_text_norm": meal_text.strip().lower(),
        "sender": _sender_e164(sender), "channel": channel, "day_tz": "America/New_York",
        "kcal": _as_decimal(macros.get("calories", 0)),
        "protein_g": _as_decimal(macros.get("protein", 0)),
        "carbs_g": _as_decimal(macros.get("carbs", 0)),
        "fat_g": _as_decimal(macros.get("fat", 0)),
        "created_ms": _as_decimal(_now_ms()),
        "schema_version": 1
    }
    try:
        meals_tbl.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        return True, meal_id
    except Exception as e:
        if "ConditionalCheckFailed" in str(e): log.info("Meal exists; skip")
        else: log.exception("meals put failed")
        return False, meal_id

def _update_totals(dt: str, deltas: dict) -> dict:
    key = {"pk": f"total#{USER_ID}", "sk": dt}
    totals_tbl.update_item(
        Key=key,
        UpdateExpression="ADD calories :c, protein :p, carbs :b, fat :f SET last_update_ms = :now",
        ExpressionAttributeValues={
            ":c": _as_decimal(deltas.get("calories", 0)),
            ":p": _as_decimal(deltas.get("protein", 0)),
            ":b": _as_decimal(deltas.get("carbs", 0)),
            ":f": _as_decimal(deltas.get("fat", 0)),
            ":now": _as_decimal(_now_ms()),
        }
    )
    got = totals_tbl.get_item(Key=key)
    return got.get("Item") or {}

def _decrement_totals(dt: str, macros: dict) -> dict:
    key = {"pk": f"total#{USER_ID}", "sk": dt}
    totals_tbl.update_item(
        Key=key,
        UpdateExpression="ADD calories :c, protein :p, carbs :b, fat :f SET last_update_ms = :now",
        ExpressionAttributeValues={
            ":c": _as_decimal_signed(-int(macros.get("calories", 0))),
            ":p": _as_decimal_signed(-int(macros.get("protein", 0))),
            ":b": _as_decimal_signed(-int(macros.get("carbs", 0))),
            ":f": _as_decimal_signed(-int(macros.get("fat", 0))),
            ":now": _as_decimal(_now_ms()),
        }
    )
    got = totals_tbl.get_item(Key=key)
    return got.get("Item") or {}

def _meal_id(user_id: str, dt: str, text: str, ts_ms: int) -> str:
    raw = f"{user_id}|{dt}|{(text or '').strip()}|{ts_ms}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

def _channel(sender: str) -> str:
    return "wa" if str(sender).startswith("whatsapp:") else "sms"

def _sender_e164(sender: str) -> str:
    return sender.replace("whatsapp:", "")

# ---------- migraine tracking ----------
MIG_CATS = ["non-aura", "aura", "tension", "other"]

def _parse_migraine_category(text: str) -> str:
    for c in MIG_CATS:
        if c in text: return c
    return "non-aura"

def _open_episode():
    """Return last open migraine episode or None."""
    resp = migs_tbl.query(
        KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("migraine#"),
        ScanIndexForward=False,
        Limit=25
    )
    for it in resp.get("Items", []):
        if int(it.get("is_open", 0)) == 1:
            return it
    return None

def _start_migraine(sender: str, when_ms: int, cat: str, note: str):
    ep_id = hashlib.sha256(f"{USER_ID}|{when_ms}".encode()).hexdigest()[:16]
    sk = f"migraine#{ep_id}"
    dt = _today_est_from_ts(when_ms)
    item = {
        "pk": USER_ID, "sk": sk, "episode_id": ep_id, "dt": dt,
        "start_ms": _as_decimal(when_ms), "end_ms": _as_decimal(0),
        "category": cat, "notes": note or "", "is_open": _as_decimal(1),
        "schema_version": 1
    }
    migs_tbl.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
    _send_sms(sender, f"Migraine started ({cat}) at {datetime.fromtimestamp(when_ms/1000, TZ).strftime('%-I:%M %p %Z')}.")

def _end_migraine(sender: str, when_ms: int, note: str):
    ep = _open_episode()
    if not ep:
        _send_sms(sender, "No open migraine to end.")
        return

    key = {"pk": ep["pk"], "sk": ep["sk"]}

    existing = (ep.get("notes") or "").strip()
    incoming = (note or "").strip()
    combined = (existing + (" " if existing and incoming else "") + incoming).strip()

    expr = "SET end_ms=:e, is_open=:z"
    eav = {":e": _as_decimal(when_ms), ":z": _as_decimal(0)}

    if combined:
        expr += ", notes=:n"
        eav[":n"] = combined

    migs_tbl.update_item(Key=key, UpdateExpression=expr, ExpressionAttributeValues=eav)

    start_ms = int(ep.get("start_ms", 0))
    dur_min = max(0, int((when_ms - start_ms) / 60000))
    _send_sms(sender, f"Migraine ended. Duration ≈ {dur_min} min.")

# ---------- medication logging ----------
MED_CATS = {
    "triptan": ["sumatriptan","rizatriptan","eletriptan","zolmitriptan","almotriptan","frovatriptan","naratriptan"],
    "DHE": ["dihydroergotamine","migranal","dhe"],
    "excedrin": ["excedrin"],
    "normal pain med": ["ibuprofen","advil","motrin","naproxen","aleve","acetaminophen","tylenol"],
    "ketorolac": ["ketorolac","toradol"],
    "hydroxyzine": ["hydroxyzine","atarax","vistaril"],
    "zofran": ["zofran","ondansetron"],
    "benadryl im": ["diphenhydramine im","benadryl im","diphenhydramine injection"]
}

def _classify_med(text: str) -> tuple[str,str]:
    t = text.lower()
    for cat, names in MED_CATS.items():
        for n in names:
            if n in t:
                return (cat, n)
    flat = {n: cat for cat, names in MED_CATS.items() for n in names}
    tokens = re.findall(r"[a-z]+", t)
    best = None
    for tok in tokens:
        m = get_close_matches(tok, flat.keys(), n=1, cutoff=0.85)
        if m:
            best = m[0]; break
    if best: return (flat[best], best)
    return ("unknown", t.strip())

def _log_med(sender: str, when_ms: int, text_after: str):
    dose_match = re.search(r"(\d+\.?\d*)\s*(mg|mcg|g|ml)", text_after.lower())
    dose = dose_match.group(0) if dose_match else ""

    raw_cat, matched_name = _classify_med(text_after)
    cat_key = _med_category_key(raw_cat)

    dt = _today_est_from_ts(when_ms)
    sk = f"{dt}#{when_ms}"
    item = {
        "pk": USER_ID, "sk": sk, "dt": dt,
        "ts_ms": _as_decimal(when_ms), "text": text_after.strip(),
        "category": cat_key,
        "matched_name": matched_name, "dose": dose,
        "schema_version": 1,
    }
    meds_tbl.put_item(Item=item)

    # Link to open migraine if present
    ep = _open_episode()
    link_msg = ""
    if ep:
        migs_tbl.update_item(
            Key={"pk": ep["pk"], "sk": ep["sk"]},
            UpdateExpression="SET meds = list_append(if_not_exists(meds, :z), :m)",
            ExpressionAttributeValues={":z": [], ":m": [item]},
        )
        link_msg = " (linked to current migraine)"

    # --- 24h interaction safety checks ---
    warnings = []
    window_start_ms = when_ms - 24*60*60*1000
    dates_to_check = {_today_est_from_ts(when_ms), _today_est_from_ts(window_start_ms)}
    recent: list[dict] = []
    for dtx in dates_to_check:
        q = meds_tbl.query(IndexName="gsi_dt", KeyConditionExpression=Key("dt").eq(dtx))
        recent.extend(q.get("Items", []))
        while "LastEvaluatedKey" in q:
            q = meds_tbl.query(
                IndexName="gsi_dt",
                KeyConditionExpression=Key("dt").eq(dtx),
                ExclusiveStartKey=q["LastEvaluatedKey"],
            )
            recent.extend(q.get("Items", []))

    recent_cats = [
        (_med_category_key(it.get("category", "")), int(it.get("ts_ms", 0)))
        for it in recent
        if abs(when_ms - int(it.get("ts_ms", 0))) < 24*60*60*1000
    ]

    if cat_key == "triptan":
        if any(rc in ("dhe", "triptan") and abs(when_ms - ts) < 24*60*60*1000 for rc, ts in recent_cats):
            warnings.append("⚠️ Triptans should NOT be used within 24h of any triptan or DHE.")
    if cat_key == "dhe":
        if any(rc == "triptan" and abs(when_ms - ts) < 24*60*60*1000 for rc, ts in recent_cats):
            warnings.append("⚠️ DHE should NOT be used within 24h of any triptan.")

    # --- monthly limit checks (doses used) ---
    limit = _med_limits_for(cat_key)
    used_doses = None
    if limit:
        used_doses = _count_med_doses_this_month(cat_key)  # before including this dose
        would_be = used_doses + 1
        pct = 0 if limit == 0 else round(would_be / limit, 2)
        if would_be > limit:
            warnings.append(f"⚠️ Over monthly {cat_key} dose limit: {would_be}/{limit}.")
        elif pct >= NEAR_THRESH_PCT:
            warnings.append(f"⚠️ Approaching monthly {cat_key} dose limit: {would_be}/{limit}.")

    summary_note = f"{cat_key.capitalize()} this month: {used_doses if used_doses is not None else 0}/{limit} dose(s)." if limit else ""
    msg_tail = " ".join(warnings).strip()
    _send_sms(
        sender,
        f"Logged med: {cat_key}{(' ' + dose) if dose else ''}{link_msg}. {summary_note}"
        + (f" {msg_tail}" if msg_tail else "")
    )

def _handle_med(sender: str, args: str, simulate: bool = False):
    """
    /med <free text>
    Examples:
      /med sumatriptan 100mg
      /med zofran 4mg at 10pm
      /med ibuprofen 400mg yesterday 8am
    """
    tokens = (args or "").strip().split()
    when_tokens: list[str] = []
    text_tokens = tokens[:]

    if tokens and tokens[-1].lower() in ("am", "pm", "yesterday", "today"):
        when_tokens = tokens[-2:] if len(tokens) >= 2 else tokens[-1:]
        text_tokens = tokens[:-len(when_tokens)]
    elif re.search(r"(\d{1,2}(:\d{2})?\s*(am|pm))", (args or "").lower()):
        m = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))", (args or "").lower())
        if m:
            when_tokens = [m.group(1)]
            text_tokens = re.sub(re.escape(m.group(1)), "", args, flags=re.IGNORECASE).split()

    when_ms = _parse_when_to_ms(when_tokens)
    text_after = " ".join(text_tokens).strip()
    if not text_after:
        _send_sms(sender, "Usage: /med <name/dose/when>")
        return

    if simulate or DRY_RUN:
        tdisp = datetime.fromtimestamp(when_ms/1000, TZ).strftime('%-I:%M %p %Z')
        _send_sms(sender, f"[TEST] Would log med: {text_after} at {tdisp}")
        return

    _log_med(sender, when_ms, text_after)

# ---------- IF (intermittent fasting) ----------
def _fast_goal_key():
    return {"pk": USER_ID, "sk": "goal"}

def _get_fast_goal_hours(default_hours: int = 16) -> int:
    try:
        item = fast_tbl.get_item(Key=_fast_goal_key()).get("Item")
        if item and "hours" in item:
            return int(item["hours"])
    except Exception:
        pass
    return default_hours

def _set_fast_goal_hours(hours: int):
    fast_tbl.put_item(Item={**_fast_goal_key(), "hours": _as_decimal(hours), "updated_ms": _as_decimal(_now_ms())})

def _open_fast():
    """Return the most recent open fast or None."""
    resp = fast_tbl.query(
        KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("fast#"),
        ScanIndexForward=False,
        Limit=25
    )
    for it in resp.get("Items", []):
        if int(it.get("is_open", 0)) == 1:
            return it
    return None

def _start_fast(sender: str, when_ms: int):
    ep_id = hashlib.sha256(f"fast|{USER_ID}|{when_ms}".encode()).hexdigest()[:16]
    sk = f"fast#{ep_id}"
    dt = _today_est_from_ts(when_ms)
    item = {
        "pk": USER_ID, "sk": sk, "episode_id": ep_id, "dt": dt,
        "start_ms": _as_decimal(when_ms), "end_ms": _as_decimal(0),
        "is_open": _as_decimal(1), "schema_version": 1
    }
    fast_tbl.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
    events_tbl.put_item(Item={"pk": USER_ID, "sk": str(_now_ms()), "type": "fast.start", "dt": dt, "start_ms": _as_decimal(when_ms)})
    tdisp = datetime.fromtimestamp(when_ms/1000, TZ).strftime('%-I:%M %p %Z')
    _send_sms(sender, f"Fasting started at {tdisp}.")

def _end_fast(sender: str, when_ms: int):
    ep = _open_fast()
    if not ep:
        _send_sms(sender, "No open fast to end."); return
    key = {"pk": ep["pk"], "sk": ep["sk"]}
    fast_tbl.update_item(
        Key=key,
        UpdateExpression="SET end_ms=:e, is_open=:z",
        ExpressionAttributeValues={":e": _as_decimal(when_ms), ":z": _as_decimal(0)}
    )
    start_ms = int(ep.get("start_ms", 0))
    dur_min = max(0, int((when_ms - start_ms)/60000))
    goal = _get_fast_goal_hours()
    status = "✅ Met goal" if dur_min >= goal*60 else f"⏳ Short by {max(0, goal*60 - dur_min)} min"
    events_tbl.put_item(Item={"pk": USER_ID, "sk": str(_now_ms()), "type": "fast.end", "dt": _today_est_from_ts(when_ms),
                              "start_ms": _as_decimal(start_ms), "end_ms": _as_decimal(when_ms), "dur_min": _as_decimal(dur_min)})
    _send_sms(sender, f"Fast ended. Duration ≈ {_format_duration_mins(dur_min)} ({status}).")

def _fast_status(sender: str):
    ep = _open_fast()
    goal = _get_fast_goal_hours()
    if ep:
        start_ms = int(ep.get("start_ms", 0))
        now_ms = _now_ms()
        dur_min = max(0, int((now_ms - start_ms)/60000))
        pct = min(100, int((dur_min/(goal*60)) * 100)) if goal > 0 else 0
        tstart = datetime.fromtimestamp(start_ms/1000, TZ).strftime('%-I:%M %p %Z')
        _send_sms(sender, f"Fasting (open since {tstart}): {_format_duration_mins(dur_min)} elapsed. Goal: {goal}h ({pct}%).")
        return
    # last completed
    resp = fast_tbl.query(
        KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("fast#"),
        ScanIndexForward=False,
        Limit=50
    )
    for it in resp.get("Items", []):
        if int(it.get("is_open", 0)) == 0 and int(it.get("end_ms", 0)) > 0:
            dur_min = max(0, int((int(it["end_ms"]) - int(it["start_ms"])) / 60000))
            tstart = datetime.fromtimestamp(int(it["start_ms"])/1000, TZ).strftime('%Y-%m-%d %-I:%M%p')
            tend   = datetime.fromtimestamp(int(it["end_ms"])  /1000, TZ).strftime('%Y-%m-%d %-I:%M%p')
            met = "✅ Met goal" if dur_min >= goal*60 else "⏳ Missed goal"
            _send_sms(sender, f"Last fast: {tstart} → {tend} ({_format_duration_mins(dur_min)}). {met} (goal {goal}h).")
            return
    _send_sms(sender, "No fasting history yet. Try `/fast start`.")

def _handle_fast(sender: str, args: str, simulate: bool = False):
    """
    /fast start [when]
    /fast end [when]
    /fast status
    /fast goal <hours>
    """
    parts = (args or "").strip().split()
    if not parts:
        _send_sms(sender, "Use `/fast start [when]`, `/fast end [when]`, `/fast status`, or `/fast goal <hours>`"); return
    sub = parts[0].lower()

    if sub == "goal":
        if len(parts) < 2 or not re.match(r"^\d{1,2}$", parts[1]):
            _send_sms(sender, "Usage: /fast goal <hours>"); return
        hrs = int(parts[1])
        if simulate or DRY_RUN:
            _send_sms(sender, f"[TEST] Would set fasting goal to {hrs}h"); return
        _set_fast_goal_hours(hrs)
        _send_sms(sender, f"Fasting goal set to {hrs}h.")
        return

    if sub == "status":
        _fast_status(sender); return

    if sub in ("start","end"):
        when_tokens = parts[1:]
        when_ms = _parse_when_to_ms(when_tokens)
        if simulate or DRY_RUN:
            tdisp = datetime.fromtimestamp(when_ms/1000, TZ).strftime('%-I:%M %p %Z')
            _send_sms(sender, f"[TEST] Would {sub} fast at {tdisp}.")
            return
        if sub == "start": _start_fast(sender, when_ms)
        else:               _end_fast(sender, when_ms)
        return

    _send_sms(sender, "Use `/fast start [when]`, `/fast end [when]`, `/fast status`, or `/fast goal <hours>`")

# ---------- commands ----------
def _handle_help(sender: str):
    _send_sms(sender,
        "Commands:\n"
        "• meal: <desc>            – log a meal\n"
        "• /lookup: <desc>         – predict totals (no save)\n"
        "• /summary                – today’s totals (EST)\n"
        "• /week                   – last 7 days summary\n"
        "• /month                  – month-to-date summary\n"
        "• /undo                   – remove the last meal today\n"
        "• /reset today            – zero today’s totals\n"
        "• /barcode <digits>       – look up a UPC\n"
        "• /food set <alias> k=.. p=.. c=.. f=.. | /food del <alias> | /food list\n"
        "• /migraine start [when] [category] [note]\n"
        "• /migraine end [when] [note]\n"
        "• /med <name/dose/when>   – log medication (dose-based limits)\n"
        "• /meds                   – doses this month by category\n"
        "• /meds today             – doses today by category\n"
        "• /meds month             – month-to-date doses by category\n"
        "• /fact [tag]             – send a migraine fact now (optional tag)\n"
        "• /facts …                – manage daily facts (on/off/hour/number/status)\n"
        "• /fast start [when]      – start a fast (IF)\n"
        "• /fast end [when]        – end current fast\n"
        "• /fast status            – current or last fast summary\n"
        "• /fast goal <hours>      – set fasting goal (e.g., 16)\n"
        "• /test <command...>      – simulate without saving\n"
    )

def _handle_summary(sender: str, dt: str):
    _send_sms(sender, _format_summary_lines(_get_totals_for_day(dt)))

def _handle_lookup(sender: str, dt: str, text_after_lookup: str):
    try:
        macros, items = _nutritionix(text_after_lookup)
    except NxError as e:
        _send_sms(sender, f"Lookup failed — {str(e)}"); return

    today = _get_totals_for_day(dt)
    hypo_cal = int(today.get("calories", 0)) + macros["calories"]
    hypo_pro = int(today.get("protein", 0))  + macros["protein"]

    dk = hypo_cal - CALORIES_MAX
    dp = PROTEIN_MIN - hypo_pro

    msg = [
        "Lookup matched:",
        _format_matched_lines(items),
        f"Adds → +{macros['calories']} kcal, +{macros['protein']} g P (+{macros['carbs']} C / +{macros['fat']} F)",
        f"Totals after: {hypo_cal}/{CALORIES_MAX} kcal, {hypo_pro}/{PROTEIN_MIN} g P",
        f"Δ vs goals: {'over by ' + str(dk) + ' kcal' if dk>0 else str(-dk)+' kcal left'}, "
        f"{'short ' + str(dp) + ' g protein' if dp>0 else 'exceeds protein by ' + str(-dp) + ' g'}"
    ]
    _send_sms(sender, "\n".join(msg))

def _handle_meal(sender: str, dt: str, ts_ms: int, meal_pk: str, text: str, simulate: bool = False):
    channel = _channel(sender)
    alias, qty = _match_override_in_text(text)
    if alias:
        macros = _macros_from_override(alias, qty)
        if not macros:
            # fall back to NX if alias somehow missing
            try:
                macros, items = _nutritionix(text)
            except NxError as e:
                _send_sms(sender, f"Sorry — {str(e)}. Try rephrasing or set a food with `/food set`."); return
        if simulate or DRY_RUN:
            today = _get_totals_for_day(dt)
            hypo_cal = int(today.get("calories", 0)) + macros["calories"]
            hypo_pro = int(today.get("protein", 0))  + macros["protein"]
            _send_sms(sender, f"[TEST] Would save meal (override {alias} x{qty}): {macros['calories']} kcal ({macros['protein']} P / {macros['carbs']} C / {macros['fat']} F).\nTotals would become: {hypo_cal} / {CALORIES_MAX} kcal, {hypo_pro} / {PROTEIN_MIN} P.")
            return
        _write_enriched_event(meal_pk=meal_pk, ts_ms=ts_ms, dt=dt, text=f"[override] {text}", macros=macros, channel=channel)
        created, _ = _put_meal_row_idempotent(USER_ID, dt, ts_ms, sender, text, macros, channel)
        if not created: log.info("Idempotent meal write skipped (override).")
        new_totals = _update_totals(dt, macros)
        if sender: _send_sms(sender, _format_meal_reply(macros, new_totals, prefix="Meal saved (override)"))
        return

    try:
        macros, items = _nutritionix(text)
    except NxError as e:
        _send_sms(sender, f"Sorry — {str(e)}. Try rephrasing or set a custom food with `/food set`."); return

    if simulate or DRY_RUN:
        today = _get_totals_for_day(dt)
        hypo_cal = int(today.get("calories", 0)) + macros["calories"]
        hypo_pro = int(today.get("protein", 0))  + macros["protein"]
        _send_sms(sender, f"[TEST] Would save meal:\nMatched:\n{_format_matched_lines(items)}\n"
                          f"Total → {macros['calories']} kcal ({macros['protein']} P / {macros['carbs']} C / {macros['fat']} F)\n"
                          f"Then totals: {hypo_cal} / {CALORIES_MAX} kcal, {hypo_pro} / {PROTEIN_MIN} P.")
        return

    _write_enriched_event(meal_pk=meal_pk, ts_ms=ts_ms, dt=dt, text=text, macros=macros, channel=channel)
    created, _ = _put_meal_row_idempotent(USER_ID, dt, ts_ms, sender, text, macros, channel)
    if not created: log.info("Idempotent meal write skipped.")
    new_totals = _update_totals(dt, macros)
    if sender: _send_sms(sender, _format_meal_reply(macros, new_totals, matched_items=items))

def _handle_undo(sender: str, dt: str, simulate: bool = False):
    resp = meals_tbl.query(KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with(f"{dt}#"))
    items = resp.get("Items", [])
    if not items:
        _send_sms(sender, "No meals found for today."); return
    latest = max(items, key=lambda it: int(it.get("created_ms", 0)))
    macros = {"calories": int(latest.get("kcal", 0)), "protein": int(latest.get("protein_g", 0)),
              "carbs": int(latest.get("carbs_g", 0)), "fat": int(latest.get("fat_g", 0))}
    if simulate or DRY_RUN:
        _send_sms(sender, f"[TEST] Would undo last meal: -{macros['calories']} kcal, -{macros['protein']} P.")
        return
    _decrement_totals(dt, macros)
    meals_tbl.delete_item(Key={"pk": latest["pk"], "sk": latest["sk"]})
    events_tbl.put_item(Item={"pk": USER_ID, "sk": str(_now_ms()), "type": "meal.undo", "dt": dt,
                              "ref": latest.get("meal_id"), "kcal": _as_decimal(macros["calories"])})
    _send_sms(sender, f"Undid last meal: -{macros['calories']} kcal, -{macros['protein']} P.")

def _handle_reset_today(sender: str, dt: str, simulate: bool = False):
    if simulate or DRY_RUN:
        _send_sms(sender, "[TEST] Would reset today’s totals to 0 (meals remain).")
        return
    key = {"pk": f"total#{USER_ID}", "sk": dt}
    totals_tbl.update_item(Key=key,
        UpdateExpression="SET calories=:z, protein=:z, carbs=:z, fat=:z, last_update_ms=:now",
        ExpressionAttributeValues={":z": _as_decimal(0), ":now": _as_decimal(_now_ms())})
    events_tbl.put_item(Item={"pk": USER_ID, "sk": str(_now_ms()), "type": "totals.reset", "dt": dt})
    _send_sms(sender, "Today’s totals have been reset to 0. (Meals remain.)")

def _handle_migraine(sender: str, args: str, simulate: bool = False):
    parts = args.strip().split()
    if not parts:
        _send_sms(sender, "Use `/migraine start ...` or `/migraine end ...`"); return
    sub = parts[0].lower()

    if sub == "start":
        cat = "non-aura"; when_tokens = []; note_tokens = []
        for i, tok in enumerate(parts[1:], start=1):
            if tok.lower() in MIG_CATS:
                cat = tok.lower(); when_tokens = parts[1:i]; note_tokens = parts[i+1:]; break
        if not when_tokens and not note_tokens and len(parts) > 1:
            when_tokens = parts[1:]
        when_ms = _parse_when_to_ms(when_tokens)
        note = " ".join(note_tokens) if note_tokens else ""
        if simulate or DRY_RUN:
            tdisp = datetime.fromtimestamp(when_ms/1000, TZ).strftime('%-I:%M %p %Z')
            _send_sms(sender, f"[TEST] Would start migraine ({cat}) at {tdisp}. Note: {note or '(none)'}")
            return
        _start_migraine(sender, when_ms, cat, note)

    elif sub == "end":
        when_tokens = parts[1:]
        when_ms = _parse_when_to_ms(when_tokens)
        note = " ".join([t for t in when_tokens if t not in ("now","today","yesterday")])
        if simulate or DRY_RUN:
            tdisp = datetime.fromtimestamp(when_ms/1000, TZ).strftime('%-I:%M %p %Z')
            _send_sms(sender, f"[TEST] Would end migraine at {tdisp}. Note: {note or '(none)'}")
            return
        _end_migraine(sender, when_ms, note)
    else:
        _send_sms(sender, "Use `/migraine start ...` or `/migraine end ...`")

def _meds_summary(days: int):
    end = datetime.now(TZ).date()
    start = end - timedelta(days=days-1)
    agg = {}  # category -> count
    total = 0
    for i in range(days):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        resp = meds_tbl.query(IndexName="gsi_dt", KeyConditionExpression=Key("dt").eq(day), ScanIndexForward=False)
        for it in resp.get("Items", []):
            cat = (_med_category_key(it.get("category") or "unknown")).lower()
            agg[cat] = agg.get(cat, 0) + 1
            total += 1
    lines = [f"- {k}: {v} dose(s)" for k, v in sorted(agg.items(), key=lambda x: (-x[1], x[0]))]
    return f"Doses last {days}d: {total}\n" + ("\n".join(lines) if lines else "No doses.")

def _handle_meds(sender: str):
    # keep existing default summary = MTD
    _handle_meds_month(sender)

def _handle_meds_today(sender: str):
    _send_sms(sender, _meds_summary(1))

def _handle_meds_month(sender: str):
    today = datetime.now(TZ).date()
    _send_sms(sender, _meds_summary(today.day))

# ---------- handler ----------
def lambda_handler(event, context):
    for rec in event.get("Records", []):
        msg = json.loads(rec["Sns"]["Message"])
        raw_text = (msg.get("text") or msg.get("body") or "").strip()
        sender = msg.get("sender") or msg.get("from") or ""
        meal_pk = msg.get("pk") or USER_ID

        simulate = False
        if raw_text.lower().startswith("/test "):
            simulate = True
            raw_text = raw_text[6:].strip()

        ts_ms = _parse_ts(msg.get("sk"))
        dt = _today_est_from_ts(ts_ms)  # EST day boundary

        lower = raw_text.lower()
        if lower in ("/help", "help"):
            _handle_help(sender)
        elif lower.startswith("/summary"):
            _handle_summary(sender, dt)
        elif lower.startswith("/barcode"):
            _handle_barcode(sender, raw_text.split(" ", 1)[1] if " " in raw_text else "")
        elif lower.startswith("/food"):
            _handle_food(sender, raw_text.split(" ", 1)[1] if " " in raw_text else "")
        elif lower == "/week":
            _handle_week(sender)
        elif lower == "/month":
            _handle_month(sender)
        elif lower.startswith("/lookup:") or lower.startswith("/lookup "):
            q = raw_text.split(":", 1)[1].strip() if ":" in raw_text else raw_text.split(" ", 1)[1].strip()
            _handle_lookup(sender, dt, q) if q else _send_sms(sender, "Usage: /lookup: <meal>")
        elif lower == "/undo":
            _handle_undo(sender, dt, simulate=simulate)
        elif lower == "/reset today":
            _handle_reset_today(sender, dt, simulate=simulate)
        elif lower == "/meds":
            _handle_meds(sender)
        elif lower == "/meds today":
            _handle_meds_today(sender)
        elif lower == "/meds month":
            _handle_meds_month(sender)
        elif lower.startswith("/migraine"):
            _handle_migraine(sender, raw_text.split(" ", 1)[1] if " " in raw_text else "", simulate=simulate)
        elif lower.startswith("/med"):
            _handle_med(sender, raw_text.split(" ", 1)[1] if " " in raw_text else "", simulate=simulate)
        elif lower.startswith("/facts"):
            tail = raw_text.split(" ", 1)[1] if " " in raw_text else ""
            _handle_facts(sender, tail, simulate=simulate)
        elif lower.startswith("/fact"):
            if ":" in raw_text:
                tail = raw_text.split(":", 1)[1].strip()
            else:
                tail = raw_text.split(" ", 1)[1].strip() if " " in raw_text else ""
            _handle_fact(sender, tail, simulate=simulate)
        elif lower.startswith("/fast"):
            _handle_fast(sender, raw_text.split(" ", 1)[1] if " " in raw_text else "", simulate=simulate)
        elif lower.startswith("meal:") or lower.startswith("meal "):
            meal_text = raw_text.split(":", 1)[1].strip() if ":" in raw_text else raw_text.split(" ", 1)[1].strip()
            _handle_meal(sender, dt, ts_ms, meal_pk, meal_text, simulate=simulate) if meal_text else _send_sms(sender, "Try: meal: greek yogurt + berries")
        else:
            _send_sms(sender,
                "Unrecognized. Send `meal: ...`, `/lookup: ...`, `/summary`, `/week`, `/month`, `/undo`, `/reset today`, "
                "`/barcode ...`, `/food ...`, `/migraine ...`, `/med ...`, `/meds`, `/meds today`, `/meds month`, `/fact`, `/facts ...`, "
                "`/fast ...`, or `/help`")
    return {"ok": True}
