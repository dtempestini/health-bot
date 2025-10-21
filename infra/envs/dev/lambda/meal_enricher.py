import os, json, time, hashlib, datetime, logging
from decimal import Decimal
import boto3
import requests

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---- AWS clients / env ----
secrets = boto3.client("secretsmanager")
ddb = boto3.resource("dynamodb")

MEALS_TABLE   = os.environ["MEALS_TABLE"]        # hb_meals_dev
TOTALS_TABLE  = os.environ["TOTALS_TABLE"]       # hb_daily_totals_dev
EVENTS_TABLE  = os.environ["EVENTS_TABLE"]       # hb_events_dev
USER_ID       = os.environ.get("USER_ID", "me")

NUTRITION_SECRET_NAME = os.environ["NUTRITION_SECRET_NAME"]
TWILIO_SECRET_NAME    = os.environ.get("TWILIO_SECRET_NAME", "hb_twilio_dev")

# Goals
CALORIES_MAX = int(os.environ.get("CALORIES_MAX", "1800"))
PROTEIN_MIN  = int(os.environ.get("PROTEIN_MIN",  "210"))

meals_tbl  = ddb.Table(MEALS_TABLE)
totals_tbl = ddb.Table(TOTALS_TABLE)
events_tbl = ddb.Table(EVENTS_TABLE)

# ---------- helpers ----------
def _now_ms() -> int:
    return int(time.time() * 1000)

def _today_local() -> str:
    # If you want America/New_York specifically, swap to zoneinfo.
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def _as_decimal(n) -> Decimal:
    try:
        if n is None:
            return Decimal("0")
        d = Decimal(str(n))
        return d if d >= 0 else Decimal("0")
    except Exception:
        return Decimal("0")

def _decimalize_tree(x):
    if isinstance(x, float):
        return _as_decimal(x)
    if isinstance(x, (int, Decimal)):
        return _as_decimal(x)
    if isinstance(x, dict):
        return {k: _decimalize_tree(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_decimalize_tree(v) for v in x]
    return x

def _parse_ts(val):
    try:
        return int(val)
    except Exception:
        return _now_ms()

def _meal_id(user_id: str, dt: str, text: str, ts_ms: int) -> str:
    raw = f"{user_id}|{dt}|{(text or '').strip()}|{ts_ms}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

def _nutritionix(query: str):
    # Secret: {"app_id":"...","app_key":"..."}
    sec = secrets.get_secret_value(SecretId=NUTRITION_SECRET_NAME)
    cfg = json.loads(sec["SecretString"])
    headers = {
        "x-app-id": cfg["app_id"],
        "x-app-key": cfg["app_key"],
        "Content-Type": "application/json",
    }
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    r = requests.post(url, headers=headers, json={"query": query}, timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"Nutritionix HTTP {r.status_code}: {r.text}")
    data = r.json()
    items = data.get("foods", []) or []
    total = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    for f in items:
        total["calories"] += int(round(f.get("nf_calories", 0)))
        total["protein"]  += int(round(f.get("nf_protein", 0)))
        total["carbs"]    += int(round(f.get("nf_total_carbohydrate", 0)))
        total["fat"]      += int(round(f.get("nf_total_fat", 0)))
    return total, items

def _send_sms(to_number: str, body: str) -> None:
    sec = secrets.get_secret_value(SecretId=TWILIO_SECRET_NAME)["SecretString"]
    cfg = json.loads(sec)
    account_sid = cfg["account_sid"]
    auth_token  = cfg["auth_token"]
    from_number = cfg.get("from_wa", "whatsapp:+14155238886")  # sandbox

    if not to_number.startswith("whatsapp:"):
        to_number = "whatsapp:" + to_number.lstrip("+")

    url  = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {"From": from_number, "To": to_number, "Body": body}

    try:
        r = requests.post(url, data=data, auth=(account_sid, auth_token), timeout=10)
        if r.status_code >= 400:
            log.error(f"Twilio send failed {r.status_code}: {r.text}")
        else:
            log.info(f"Twilio send OK: {r.json().get('sid')}")
    except Exception:
        log.exception("Twilio send exception")

def _get_totals_for_day(dt: str) -> dict:
    key = {"pk": f"total#{USER_ID}", "sk": dt}  # matches current design
    r = totals_tbl.get_item(Key=key)
    return r.get("Item") or {"pk": key["pk"], "sk": key["sk"], "calories": 0, "protein": 0, "carbs": 0, "fat": 0}

def _format_summary_lines(totals: dict) -> str:
    cal = int(totals.get("calories", 0))
    pro = int(totals.get("protein", 0))
    carb = int(totals.get("carbs", 0))
    fat  = int(totals.get("fat", 0))
    lines = [f"Today so far: {cal} / {CALORIES_MAX} kcal, {pro} / {PROTEIN_MIN} P, {carb} C, {fat} F."]
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
        f"Today’s total: {cal} / {CALORIES_MAX} kcal, {pro} / {PROTEIN_MIN} P."
    ]
    if cal > CALORIES_MAX:
        lines.append(f"⚠️ Over calorie goal by {cal - CALORIES_MAX} kcal.")
    if pro < PROTEIN_MIN:
        lines.append(f"💪 {PROTEIN_MIN - pro} g protein left today.")
    return "\n".join(lines)

# ---------- core ops ----------
def _write_enriched_event(meal_pk: str, ts_ms: int, dt: str, text: str, macros: dict):
    item = {
        "pk": meal_pk,
        "sk": str(ts_ms),
        "type": "meal.enriched",
        "text": text,
        "nutrition": _decimalize_tree(macros),
        "dt": dt,
        "uid": USER_ID,
        "source": "sms"
    }
    events_tbl.put_item(Item=item)

def _put_meal_row_idempotent(user_id: str, dt: str, ts_ms: int, sender: str, meal_text: str, macros: dict):
    meal_id = _meal_id(user_id, dt, meal_text, ts_ms)
    pk = user_id
    sk = f"{dt}#{meal_id}"
    item = {
        "pk": pk,
        "sk": sk,
        "dt": dt,
        "meal_id": meal_id,
        "raw_text": meal_text,
        "sender": sender,
        "kcal": _as_decimal(macros.get("calories", 0)),
        "protein_g": _as_decimal(macros.get("protein", 0)),
        "carbs_g": _as_decimal(macros.get("carbs", 0)),
        "fat_g": _as_decimal(macros.get("fat", 0)),
        "created_ms": _as_decimal(_now_ms()),
    }
    try:
        meals_tbl.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)"
        )
        return True
    except Exception as e:
        if "ConditionalCheckFailed" in str(e):
            log.info("Meal already exists; skipping idempotent write.")
        else:
            log.exception("meals put_item failed")
        return False

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

# ---------- commands ----------
def _handle_summary(sender: str, dt: str):
    totals = _get_totals_for_day(dt)
    _send_sms(sender, _format_summary_lines(totals))

def _handle_lookup(sender: str, dt: str, text_after_lookup: str):
    macros, _ = _nutritionix(text_after_lookup)
    today = _get_totals_for_day(dt)
    hypo = {
        "calories": int(today.get("calories", 0)) + macros["calories"],
        "protein":  int(today.get("protein", 0))  + macros["protein"],
        "carbs":    int(today.get("carbs", 0))    + macros["carbs"],
        "fat":      int(today.get("fat", 0))      + macros["fat"],
    }
    lines = [
        f"If you eat this ({macros['calories']} kcal, {macros['protein']} P / {macros['carbs']} C / {macros['fat']} F):",
        f"➡️ Today would be: {hypo['calories']} / {CALORIES_MAX} kcal, {hypo['protein']} / {PROTEIN_MIN} P."
    ]
    if hypo["calories"] > CALORIES_MAX:
        lines.append(f"⚠️ That would put you over by {hypo['calories'] - CALORIES_MAX} kcal.")
    if hypo["protein"] < PROTEIN_MIN:
        lines.append(f"💪 You’d still need {PROTEIN_MIN - hypo['protein']} g protein today.")
    _send_sms(sender, "\n".join(lines))

def _handle_meal(sender: str, dt: str, ts_ms: int, meal_pk: str, text: str):
    macros, _items = _nutritionix(text)

    _write_enriched_event(meal_pk=meal_pk, ts_ms=ts_ms, dt=dt, text=text, macros=macros)

    created = _put_meal_row_idempotent(USER_ID, dt, ts_ms, sender, text, macros)
    if not created:
        log.info("Idempotent meal write skipped (exists).")

    new_totals = _update_totals(dt, macros)

    if sender:
        _send_sms(sender, _format_meal_reply(macros, new_totals))

# ---------- handler ----------
def lambda_handler(event, context):
    for rec in event.get("Records", []):
        msg = json.loads(rec["Sns"]["Message"])
        raw_text = (msg.get("text") or msg.get("body") or "").strip()
        dt = msg.get("dt") or _today_local()
        ts_ms = _parse_ts(msg.get("sk"))
        sender = msg.get("sender") or msg.get("from") or ""
        meal_pk = msg.get("pk") or USER_ID

        lower = raw_text.lower()
        if lower.startswith("/summary"):
            _handle_summary(sender, dt)
        elif lower.startswith("/lookup:") or lower.startswith("/lookup "):
            q = raw_text.split(":", 1)[1].strip() if ":" in raw_text else raw_text.split(" ", 1)[1].strip()
            _handle_lookup(sender, dt, q) if q else _send_sms(sender, "Usage: /lookup: <meal>")
        elif lower.startswith("meal:") or lower.startswith("meal "):
            meal_text = raw_text.split(":", 1)[1].strip() if ":" in raw_text else raw_text.split(" ", 1)[1].strip()
            if meal_text:
                _handle_meal(sender, dt, ts_ms, meal_pk, meal_text)
            else:
                _send_sms(sender, "Try: meal: greek yogurt + berries")
        else:
            _send_sms(sender, "Unrecognized. Send `meal: ...`, `/summary`, or `/lookup: ...`")

    return {"ok": True}
