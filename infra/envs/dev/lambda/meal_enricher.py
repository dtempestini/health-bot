# file: infra/envs/dev/lambda/meal_enricher.py
import os, json, boto3, time, math
import urllib.parse
import requests  # already included in your layer/container; if not, vendor in zip

secrets = boto3.client("secretsmanager")
ddb = boto3.resource("dynamodb")
sns = boto3.client("sns")

MEALS_TABLE  = os.environ["MEALS_TABLE"]
TOTALS_TABLE = os.environ["TOTALS_TABLE"]
EVENTS_TABLE = os.environ["EVENTS_TABLE"]
TWILIO_SECRET_NAME = os.environ.get("TWILIO_SECRET_NAME")
USER_ID = os.environ.get("USER_ID", "me")

# ---------- helpers ----------

def _decimalize(x):
    # Convert floats to Decimal-compatible (integers) for DynamoDB
    if isinstance(x, float):
        return int(round(x))
    if isinstance(x, dict):
        return {k: _decimalize(v) for k,v in x.items()}
    if isinstance(x, list):
        return [_decimalize(v) for v in x]
    return x

def _nutritionix(query):
    # Expect Nutritionix secret same as before: {"app_id":"...","app_key":"..."}
    sec = secrets.get_secret_value(SecretId=os.environ["NUTRITION_SECRET_NAME"])
    cfg = json.loads(sec["SecretString"])
    app_id = cfg["app_id"]; app_key = cfg["app_key"]
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    headers = {
        "x-app-id": app_id,
        "x-app-key": app_key,
        "Content-Type": "application/json"
    }
    r = requests.post(url, headers=headers, json={"query": query})
    if r.status_code != 200:
        raise RuntimeError(f"Nutritionix HTTPError {r.status_code}: {r.text}")
    data = r.json()
    items = data.get("foods", [])
    # Summarize macros (ints)
    total = {"calories":0,"protein":0,"carbs":0,"fat":0}
    for f in items:
        total["calories"] += int(round(f.get("nf_calories",0)))
        total["protein"]  += int(round(f.get("nf_protein",0)))
        total["carbs"]    += int(round(f.get("nf_total_carbohydrate",0)))
        total["fat"]      += int(round(f.get("nf_total_fat",0)))
    return total, items

def _get_twilio():
    if not TWILIO_SECRET_NAME:
        return None
    sec = secrets.get_secret_value(SecretId=TWILIO_SECRET_NAME)
    cfg = json.loads(sec["SecretString"])
    return {
        "sid": cfg["account_sid"],
        "token": cfg["auth_token"],
        "from": cfg["from_number"]
    }

import json, os, requests
import boto3

secrets = boto3.client("secretsmanager")
TWILIO_SECRET_NAME = os.environ.get("TWILIO_SECRET_NAME", "hb_twilio_dev")

def _send_sms(to_number: str, body: str) -> None:
    """
    Send a reply via Twilio. Works for plain SMS (+1...) and WhatsApp (whatsapp:+1...).
    Secrets JSON may be either:
      {"account_sid":"AC...","auth_token":"...","from":"+1XXXXXXXXXX"}
    or (recommended, supports both channels explicitly):
      {"account_sid":"AC...","auth_token":"...","from_sms":"+1XXXXXXXXXX","from_wa":"whatsapp:+14155238886"}
    If `from_wa` is omitted, the WhatsApp sandbox number is used by default.
    """

    # 1) load creds
    sec = secrets.get_secret_value(SecretId=TWILIO_SECRET_NAME)["SecretString"]
    cfg = json.loads(sec)

    account_sid = cfg["account_sid"]
    auth_token  = cfg["auth_token"]

    # allow either "from" or "from_sms"
    from_sms = cfg.get("from_sms") or cfg.get("from")
    # default WA From is sandbox number; override with from_wa if you set one
    from_wa  = cfg.get("from_wa", "whatsapp:+14155238886")

    if not from_sms:
        print("Twilio send skipped: missing from/from_sms in Secrets.")
        return

    # 2) decide channel from the *to_number*
    n = (to_number or "").strip()

    if n.startswith("whatsapp:"):
        # WhatsApp: make sure both ends carry the WA prefix
        from_number = from_wa
        # normalize recipient to 'whatsapp:+<digits>'
        n = "whatsapp:" + n.replace("whatsapp:", "").lstrip("+")
    else:
        # SMS: both To and From must be plain E.164 like +1...
        from_number = from_sms
        # strip accidental 'whatsapp:' if present
        n = n.replace("whatsapp:", "")

    # 3) fire Twilio REST (no SDK required)
    url  = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {"From": from_number, "To": n, "Body": body}

    try:
        r = requests.post(url, data=data, auth=(account_sid, auth_token), timeout=10)
        if r.status_code >= 400:
            # surface Twilio error JSON (e.g., 21212, 21910, etc.)
            print(f"Twilio send failed {r.status_code}: {r.text}")
        else:
            # optionally log the SID
            sid = r.json().get("sid")
            print(f"Twilio send OK sid={sid}")
    except Exception as e:
        print(f"Twilio send exception: {e}")


# ---------- handler ----------

def lambda_handler(event, context):
    # SNS event -> records
    for rec in event.get("Records", []):
        msg = json.loads(rec["Sns"]["Message"])
        text = msg.get("text","")
        dt   = msg.get("dt")
        ts   = int(msg.get("sk"))
        sender = msg.get("sender","")  # phone (Twilio)
        meal_pk = msg.get("pk")

        macros, items = _nutritionix(text)

        # 1) store enriched meal as another event row in hb_events_dev (same table pattern you used)
        events = ddb.Table(EVENTS_TABLE)
        enriched = {
            "pk": meal_pk,
            "sk": f"{ts+1}",                 # keep order
            "type": "meal.enriched",
            "text": text,
            "nutrition": _decimalize(macros),
            "dt": dt,
            "uid": USER_ID,
            "source": "sms"
        }
        events.put_item(Item=enriched)

        # 2) upsert daily totals (simple add)
        totals = ddb.Table(TOTALS_TABLE)
        key = {"pk": f"total#{USER_ID}", "sk": dt}
        totals.update_item(
            Key=key,
            UpdateExpression="ADD calories :c, protein :p, carbs :b, fat :f",
            ExpressionAttributeValues={
                ":c": _decimalize(macros["calories"]),
                ":p": _decimalize(macros["protein"]),
                ":b": _decimalize(macros["carbs"]),
                ":f": _decimalize(macros["fat"]),
            }
        )

        # 3) optional SMS reply with macros
        if sender:
            body = f"Logged: {text}\nCalories {macros['calories']}, P {macros['protein']}g, C {macros['carbs']}g, F {macros['fat']}g."
            _send_sms(sender, body)

        print(f"Enrichment stored OK {meal_pk} {ts}")

    return {"ok": True}
