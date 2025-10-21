# file: infra/envs/dev/lambda/meal_enricher.py
import os, json, time, hashlib, logging, re
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
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# Medication safety limits (env overrideable)
TRIPTAN_LIMIT_DPM = int(os.environ.get("TRIPTAN_LIMIT_DPM", "8"))   # days per month
DHE_LIMIT_DPM     = int(os.environ.get("DHE_LIMIT_DPM", "6"))
COMBO_LIMIT_DPM   = int(os.environ.get("COMBO_LIMIT_DPM", "8"))     # e.g., Excedrin
PAIN_LIMIT_DPM    = int(os.environ.get("PAIN_LIMIT_DPM", "12"))     # simple analgesics/NSAIDs
NEAR_THRESH_PCT   = float(os.environ.get("NEAR_THRESH_PCT", "0.75"))



# NEW tables
MIGRAINES_TABLE = os.environ.get("MIGRAINES_TABLE", "hb_migraines_dev")
MEDS_TABLE      = os.environ.get("MEDS_TABLE", "hb_meds_dev")

NUTRITION_SECRET_NAME = os.environ["NUTRITION_SECRET_NAME"]
TWILIO_SECRET_NAME    = os.environ.get("TWILIO_SECRET_NAME", "hb_twilio_dev")

# Goals
CALORIES_MAX = int(os.environ.get("CALORIES_MAX", "1800"))
PROTEIN_MIN  = int(os.environ.get("PROTEIN_MIN",  "210"))

meals_tbl   = ddb.Table(MEALS_TABLE)
totals_tbl  = ddb.Table(TOTALS_TABLE)
events_tbl  = ddb.Table(EVENTS_TABLE)
migs_tbl    = ddb.Table(MIGRAINES_TABLE)
meds_tbl    = ddb.Table(MEDS_TABLE)

TZ = ZoneInfo("America/New_York")

# ---------- helpers ----------
def _month_bounds_est(ts_ms: int | None = None):
    now = datetime.now(TZ) if ts_ms is None else datetime.fromtimestamp(ts_ms/1000, TZ)
    start = now.replace(day=1).date()
    end = now.date()
    return start, end

def _med_category_key(cat: str) -> str:
    c = cat.lower()
    if c in ("triptan",): return "triptan"
    if c in ("dhe","d.h.e.","d.h.e"): return "dhe"
    if c in ("excedrin",): return "excedrin"
    if c in ("normal pain med","nsaid","ibuprofen","naproxen","acetaminophen","tylenol","advil","aleve"):
        return "normal pain med"
    return c

def _med_limits_for(cat_key: str) -> int | None:
    return {
        "triptan": TRIPTAN_LIMIT_DPM,
        "dhe": DHE_LIMIT_DPM,
        "excedrin": COMBO_LIMIT_DPM,
        "normal pain med": PAIN_LIMIT_DPM,
    }.get(cat_key)

def _count_med_days_this_month(cat_key: str) -> tuple[int, set]:
    """Return (# distinct days used in month, set of 'YYYY-MM-DD') for this category."""
    start, end = _month_bounds_est()
    # Query month by dt GSI; we may need to pull across many days, but dev volume is small.
    resp = meds_tbl.query(
        IndexName="gsi_dt",
        KeyConditionExpression=Key("dt").between(start.isoformat(), end.isoformat()) & Key("sk").begins_with(start.isoformat()[:0])
    )
    # The gsi_dt uses hash=dt, range=sk; we'll need to page across days:
    # Simpler approach: scan-by-day loop
    days = set()
    d = start
    while d <= end:
        r = meds_tbl.query(
            IndexName="gsi_dt",
            KeyConditionExpression=Key("dt").eq(d.isoformat())
        )
        for it in r.get("Items", []):
            if _med_category_key(it.get("category","")) == cat_key:
                days.add(d.isoformat())
        d += timedelta(days=1)
    return len(days), days

def _now_ms() -> int:
    return int(time.time() * 1000)

def _today_est_from_ts(ts_ms: int | None) -> str:
    if ts_ms is None:
        return datetime.now(TZ).date().isoformat()
    return datetime.fromtimestamp(ts_ms/1000, tz=TZ).date().isoformat()

def _as_decimal(n) -> Decimal:
    try:
        if n is None: return Decimal("0")
        d = Decimal(str(n))
        return d if d >= 0 else Decimal("0")
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
    """
    Parse simple time phrases in EST:
      - 'now' (default), 'yesterday', 'today 7:30am', '10:15 pm', '2025-10-20 08:00'
    """
    text = " ".join(tokens).strip().lower()
    if not text or text == "now": return _now_ms()

    # detect 'yesterday'
    base_dt = datetime.now(TZ)
    if "yesterday" in text:
        base_dt = base_dt - timedelta(days=1)
        text = text.replace("yesterday", "").strip()

    # ISO date first
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2})\s*(am|pm)?)?$", text)
    if m:
        y,mn,d = map(int, m.group(1).split("-"))
        hh = int(m.group(2) or 0)
        mm = int(m.group(3) or 0)
        ap = (m.group(4) or "").lower()
        if ap in ("am","pm"):
            if hh == 12: hh = 0
            if ap == "pm": hh += 12
        t = datetime(y, mn, d, hh, mm, tzinfo=TZ)
        return int(t.timestamp()*1000)

    # time like '7:30am' or '10 pm'
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2) or 0); ap = (m.group(3) or "").lower()
        if ap in ("am","pm"):
            if hh == 12: hh = 0
            if ap == "pm": hh += 12
        t = datetime(base_dt.year, base_dt.month, base_dt.day, hh, mm, tzinfo=TZ)
        return int(t.timestamp()*1000)

    # 'today 8am'
    m = re.match(r"(today)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text)
    if m:
        hh = int(m.group(2)); mm = int(m.group(3) or 0); ap = (m.group(4) or "").lower()
        if ap in ("am","pm"):
            if hh == 12: hh = 0
            if ap == "pm": hh += 12
        t = datetime(base_dt.year, base_dt.month, base_dt.day, hh, mm, tzinfo=TZ)
        return int(t.timestamp()*1000)

    # fallback: now
    return _now_ms()

def _meal_id(user_id: str, dt: str, text: str, ts_ms: int) -> str:
    raw = f"{user_id}|{dt}|{(text or '').strip()}|{ts_ms}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

def _channel(sender: str) -> str:
    return "wa" if str(sender).startswith("whatsapp:") else "sms"

def _sender_e164(sender: str) -> str:
    return sender.replace("whatsapp:", "")

def _nutritionix(query: str):
    # Secret: {"app_id":"...","app_key":"..."}
    sec = secrets.get_secret_value(SecretId=NUTRITION_SECRET_NAME)
    cfg = json.loads(sec["SecretString"])
    headers = {"x-app-id": cfg["app_id"], "x-app-key": cfg["app_key"], "Content-Type": "application/json"}
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    r = requests.post(url, headers=headers, json={"query": query}, timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"Nutritionix HTTP {r.status_code}: {r.text}")
    data = r.json()
    foods = data.get("foods", []) or []
    total = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    items_trimmed = []
    for f in foods[:20]:
        kcal = int(round(f.get("nf_calories", 0)))
        pro  = int(round(f.get("nf_protein", 0)))
        carb = int(round(f.get("nf_total_carbohydrate", 0)))
        fat  = int(round(f.get("nf_total_fat", 0)))
        total["calories"] += kcal; total["protein"] += pro; total["carbs"] += carb; total["fat"] += fat
        items_trimmed.append({
            "name": f.get("food_name"),
            "serving_qty": f.get("serving_qty"),
            "serving_unit": f.get("serving_unit"),
            "kcal": kcal, "protein_g": pro, "carbs_g": carb, "fat_g": fat
        })
    return total, items_trimmed

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

def _get_totals_for_day(dt: str) -> dict:
    key = {"pk": f"total#{USER_ID}", "sk": dt}
    r = totals_tbl.get_item(Key=key)
    return r.get("Item") or {"pk": key["pk"], "sk": key["sk"], "calories": 0, "protein": 0, "carbs": 0, "fat": 0}

# ---------- summaries ----------
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

def _format_meal_reply(macros: dict, new_totals: dict) -> str:
    mcal = int(macros.get("calories", 0))
    mp   = int(macros.get("protein", 0))
    mc   = int(macros.get("carbs", 0))
    mf   = int(macros.get("fat", 0))
    cal = int(new_totals.get("calories", 0))
    pro = int(new_totals.get("protein", 0))
    lines = [
        f"Meal saved: {mcal} kcal ({mp} P / {mc} C / {mf} F).",
        f"Today’s total (EST): {cal} / {CALORIES_MAX} kcal, {pro} / {PROTEIN_MIN} P."
    ]
    if cal > CALORIES_MAX:
        lines.append(f"⚠️ Over calorie goal by {cal - CALORIES_MAX} kcal.")
    if pro < PROTEIN_MIN:
        lines.append(f"💪 {PROTEIN_MIN - pro} g protein left today.")
    return "\n".join(lines)

def _sum_range(start_d: date, end_d: date) -> dict:
    """Sum totals from hb_daily_totals_dev between start and end inclusive."""
    # DDB Query by PK and SK between
    resp = totals_tbl.query(
        KeyConditionExpression=Key("pk").eq(f"total#{USER_ID}") & Key("sk").between(
            start_d.isoformat(), end_d.isoformat()
        )
    )
    cal = pro = carb = fat = 0
    for it in resp.get("Items", []):
        cal += int(it.get("calories", 0))
        pro += int(it.get("protein", 0))
        carb += int(it.get("carbs", 0))
        fat += int(it.get("fat", 0))
    days = (end_d - start_d).days + 1
    return {"cal": cal, "pro": pro, "carb": carb, "fat": fat, "days": days,
            "avg_cal": round(cal / days, 1), "avg_pro": round(pro / days, 1)}

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

# ---------- migraine tracking ----------
MIG_CATS = ["non-aura", "aura", "tension", "other"]

def _parse_migraine_category(text: str) -> str:
    for c in MIG_CATS:
        if c in text: return c
    return "non-aura"

def _open_episode():
    """Return last open episode or None."""
    # Query GSI for open episodes quickly
    resp = migs_tbl.query(
        IndexName="gsi_open",
        KeyConditionExpression=Key("pk").eq(USER_ID) & Key("is_open").eq(1),
        Limit=1,
        ScanIndexForward=False
    )
    items = resp.get("Items", [])
    return items[0] if items else None

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
    migs_tbl.update_item(
        Key=key,
        UpdateExpression="SET end_ms=:e, is_open=:z, notes = if_not_exists(notes, :n) || :sep || :n2",
        ExpressionAttributeValues={
            ":e": _as_decimal(when_ms), ":z": _as_decimal(0),
            ":n": "", ":sep": " ", ":n2": note or ""
        }
    )
    start_ms = int(ep.get("start_ms", 0))
    dur_min = max(0, int((when_ms - start_ms)/60000))
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
    # direct hit
    for cat, names in MED_CATS.items():
        for n in names:
            if n in t:
                return (cat, n)
    # fuzzy to a flattened name list
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
    # try to extract dose if present
    dose_match = re.search(r"(\d+\.?\d*)\s*(mg|mcg|g|ml)", text_after.lower())
    dose = dose_match.group(0) if dose_match else ""
    cat, matched_name = _classify_med(text_after)
    dt = _today_est_from_ts(when_ms)
    sk = f"{dt}#{when_ms}"
    item = {
        "pk": USER_ID, "sk": sk, "dt": dt,
        "ts_ms": _as_decimal(when_ms), "text": text_after.strip(),
        "category": cat, "matched_name": matched_name, "dose": dose
    }
    meds_tbl.put_item(Item=item)
    # Link to open migraine if present (soft link, by episode_id)
    ep = _open_episode()
    link_msg = ""
    if ep:
        migs_tbl.update_item(
            Key={"pk": ep["pk"], "sk": ep["sk"]},
            UpdateExpression="SET meds = list_append(if_not_exists(meds, :z), :m)",
            ExpressionAttributeValues={":z": [], ":m": [item]}
        )
        link_msg = " (linked to current migraine)"

        # --- 24h interaction safety checks ---
    warnings = []
    # Fetch meds in last 24h (today + yesterday via dt index)
    window_start_ms = when_ms - 24*60*60*1000
    dates_to_check = {_today_est_from_ts(when_ms), _today_est_from_ts(window_start_ms)}
    recent = []
    for dtx in dates_to_check:
        r = meds_tbl.query(IndexName="gsi_dt", KeyConditionExpression=Key("dt").eq(dtx))
        recent.extend(r.get("Items", []))

    # Normalize for checks
    recent_cats = [( _med_category_key(it.get("category","")), int(it.get("ts_ms", 0)) ) for it in recent]
    if cat == "triptan":
        # triptan within 24h of DHE or another triptan -> warn (label)
        if any(rc in ("dhe","triptan") and abs(when_ms - ts) < 24*60*60*1000 for rc, ts in recent_cats):
            warnings.append("⚠️ Triptans should NOT be used within 24h of any triptan or DHE.")
    if cat == "dhe":
        if any(rc in ("triptan",) and abs(when_ms - ts) < 24*60*60*1000 for rc, ts in recent_cats):
            warnings.append("⚠️ DHE should NOT be used within 24h of any triptan.")

    # --- monthly limit checks (days used) ---
    cat_key = _med_category_key(cat)
    limit = _med_limits_for(cat_key)
    if limit:
        used_days, day_set = _count_med_days_this_month(cat_key)
        # If we haven't already counted today's day and this isn't simulate, include it in 'would-be' count
        would_be = used_days if dt in day_set else used_days + 1
        pct = 0 if limit == 0 else round(would_be/limit, 2)
        if would_be > limit:
            warnings.append(f"⚠️ Over monthly {cat_key} limit: {would_be}/{limit} days.")
        elif pct >= NEAR_THRESH_PCT:
            warnings.append(f"⚠️ Approaching monthly {cat_key} limit: {would_be}/{limit} days.")

    # Tailor reply
    summary_note = ""
    if limit:
        summary_note = f"{cat_key.capitalize()} this month: {used_days}/{limit} days."
    msg_tail = f"{' '.join(warnings)}".strip()

    _send_sms(sender, f"Logged med: {cat}{' ' + dose if dose else ''}{link_msg}. {summary_note}" + (f" {msg_tail}" if msg_tail else ""))

# ---------- commands ----------
def _handle_help(sender: str):
    _send_sms(sender,
        "Commands:\n"
        "• meal: <desc>      – log a meal\n"
        "• /summary          – today’s totals (EST)\n"
        "• /week             – last 7 days summary\n"
        "• /month            – month-to-date summary\n"
        "• /lookup: <desc>   – predict totals (no save)\n"
        "• /undo             – remove the last meal today\n"
        "• /reset today      – zero today’s totals\n"
        "• /migraine start [when] [category] [note]\n"
        "• /migraine end [when] [note]\n"
        "• /med <name/dose/when>  – log medication"
    )

def _handle_summary(sender: str, dt: str):
    _send_sms(sender, _format_summary_lines(_get_totals_for_day(dt)))

def _handle_lookup(sender: str, dt: str, text_after_lookup: str):
    macros, _ = _nutritionix(text_after_lookup)
    today = _get_totals_for_day(dt)
    hypo_cal = int(today.get("calories", 0)) + macros["calories"]
    hypo_pro = int(today.get("protein", 0))  + macros["protein"]
    lines = [
        f"If you eat this ({macros['calories']} kcal, {macros['protein']} P / {macros['carbs']} C / {macros['fat']} F):",
        f"➡️ Today would be: {hypo_cal} / {CALORIES_MAX} kcal, {hypo_pro} / {PROTEIN_MIN} P."
    ]
    if hypo_cal > CALORIES_MAX: lines.append(f"⚠️ That would put you over by {hypo_cal - CALORIES_MAX} kcal.")
    if hypo_pro < PROTEIN_MIN:  lines.append(f"💪 You’d still need {PROTEIN_MIN - hypo_pro} g protein today.")
    _send_sms(sender, "\n".join(lines))

def _handle_meal(sender: str, dt: str, ts_ms: int, meal_pk: str, text: str, simulate: bool = False):
    channel = _channel(sender)
    macros, items = _nutritionix(text)

    if simulate or DRY_RUN:
        # show what WOULD happen (no DB writes)
        today = _get_totals_for_day(dt)
        hypo_cal = int(today.get("calories", 0)) + macros["calories"]
        hypo_pro = int(today.get("protein", 0))  + macros["protein"]
        msg = (f"[TEST] Would save meal: {macros['calories']} kcal "
               f"({macros['protein']} P / {macros['carbs']} C / {macros['fat']} F).\n"
               f"Totals would become: {hypo_cal} / {CALORIES_MAX} kcal, {hypo_pro} / {PROTEIN_MIN} P.")
        _send_sms(sender, msg)
        return

    _write_enriched_event(meal_pk=meal_pk, ts_ms=ts_ms, dt=dt, text=text, macros=macros, channel=channel)
    created, meal_id = _put_meal_row_idempotent(USER_ID, dt, ts_ms, sender, text, macros, channel)
    if not created: log.info("Idempotent meal write skipped.")
    new_totals = _update_totals(dt, macros)
    if sender: _send_sms(sender, _format_meal_reply(macros, new_totals))

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
    """
    /migraine start [when] [category] [note...]
    /migraine end [when] [note...]
    """
    parts = args.strip().split()
    if not parts:
        _send_sms(sender, "Use `/migraine start ...` or `/migraine end ...`"); return
    sub = parts[0].lower()

    if sub == "start":
        # try parse optional time (tokens until we hit a known category)
        cat = "non-aura"
        when_tokens = []
        note_tokens = []
        # find category mention
        for i, tok in enumerate(parts[1:], start=1):
            if tok.lower() in MIG_CATS:
                cat = tok.lower()
                when_tokens = parts[1:i]
                note_tokens = parts[i+1:]
                break
        if not when_tokens and not note_tokens and len(parts) > 1:
            # maybe only time or note given
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

def _handle_meds(sender: str):
    start, end = _month_bounds_est()
    cats = ["triptan","dhe","excedrin","normal pain med","other"]
    rows = []
    for ck in cats:
        used, _ = _count_med_days_this_month(ck)
        lim = _med_limits_for(ck)
        rows.append((ck, used, lim))
    lines = ["This month (med days used):"]
    for ck, used, lim in rows:
        if lim:
            lines.append(f"• {ck}: {used} / {lim} days")
        else:
            lines.append(f"• {ck}: {used} days")
    _send_sms(sender, "\n".join(lines))

def _handle_med(sender: str, args: str, simulate: bool = False):
    """
    /med <free text>  (we’ll parse dose & time roughly)
    Examples:
      /med sumatriptan 100mg
      /med zofran 4mg at 10pm
      /med ibuprofen 400mg yesterday 8am
    """
    # pull out a trailing time phrase if present (very simple heuristics)
    tokens = args.strip().split()
    when_tokens = []
    text_tokens = tokens[:]
    # heuristics: if ends with "am"/"pm" or "yesterday"/"today", treat last 1-2 tokens as time
    if tokens and tokens[-1].lower() in ("am","pm","yesterday","today"):
        when_tokens = tokens[-2:] if len(tokens) >= 2 else tokens[-1:]
        text_tokens = tokens[:-len(when_tokens)]
    elif re.match(r".*(\d{1,2}(:\d{2})?\s*(am|pm))$", args.lower()):
        # crude, re-extract the time part
        m = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))", args.lower())
        if m:
            when_tokens = [m.group(1)]
            text_tokens = re.sub(re.escape(m.group(1)), "", args, flags=re.IGNORECASE).split()

    when_ms = _parse_when_to_ms(when_tokens)
    text_after = " ".join(text_tokens).strip()
    if not text_after:
        _send_sms(sender, "Usage: /med <name/dose/when>"); return
    
    if simulate or DRY_RUN:
        _send_sms(sender, f"[TEST] Would log med: {text_after} at {datetime.fromtimestamp(when_ms/1000, TZ).strftime('%-I:%M %p %Z')}")
        return

    _log_med(sender, when_ms, text_after)

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
        elif lower == "/week":
            _handle_week(sender)
        elif lower == "/month":
            _handle_month(sender)
        elif lower.startswith("/lookup:") or lower.startswith("/lookup "):
            q = raw_text.split(":", 1)[1].strip() if ":" in raw_text else raw_text.split(" ", 1)[1].strip()
            _handle_lookup(sender, dt, q) if q else _send_sms(sender, "Usage: /lookup: <meal>")
        elif lower == "/undo":
            _handle_undo(sender, dt, simulate=simulate)             # <<< add simulate
        elif lower == "/reset today":
            _handle_reset_today(sender, dt, simulate=simulate)      # <<< add simulate
        elif lower == "/meds":
            _handle_meds(sender)
        elif lower.startswith("/migraine"):
            _handle_migraine(sender, raw_text.split(" ", 1)[1] if " " in raw_text else "", simulate=simulate)  # <<< add simulate
        elif lower.startswith("/med"):
            _handle_med(sender, raw_text.split(" ", 1)[1] if " " in raw_text else "", simulate=simulate)       # <<< add simulate
        elif lower.startswith("meal:") or lower.startswith("meal "):
            meal_text = raw_text.split(":", 1)[1].strip() if ":" in raw_text else raw_text.split(" ", 1)[1].strip()
            _handle_meal(sender, dt, ts_ms, meal_pk, meal_text, simulate=simulate) if meal_text else _send_sms(sender, "Try: meal: greek yogurt + berries")  # <<< add simulate

        else:
            _send_sms(sender, "Unrecognized. Send `meal: ...`, `/summary`, `/week`, `/month`, `/lookup: ...`, `/undo`, `/reset today`, `/migraine ...`, `/med ...`, or `/help`")
    return {"ok": True}
