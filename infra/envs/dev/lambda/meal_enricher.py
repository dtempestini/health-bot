# file: infra/envs/dev/lambda/meal_enricher.py
import os, json, boto3, time, math
import urllib.parse
import requests  # already included in your layer/container; if not, vendor in zip
from twilio.rest import Client

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

def _send_sms(to_number: str, body: str):
    # Load Twilio creds/config from Secrets Manager
    sec = secrets.get_secret_value(SecretId=TWILIO_SECRET_NAME)
    cfg = json.loads(sec["SecretString"])

    account_sid = cfg["account_sid"]
    auth_token  = cfg["auth_token"]

    from_number = (
        cfg.get("from")
        or cfg.get("from_number")
        or cfg.get("twilio_from")
    )
    messaging_service_sid = cfg.get("messaging_service_sid")

    # Normalize channel
    to_number = to_number.strip()
    is_whatsapp = to_number.startswith("whatsapp:")

    if is_whatsapp:
        if not from_number:
            raise RuntimeError("Missing 'from' number in secret for WhatsApp sends.")
        from_number = "whatsapp:" + from_number.lstrip("+")
        to_number   = "whatsapp:" + to_number.replace("whatsapp:", "").lstrip("+")
    else:
        if from_number and not from_number.startswith("+"):
            from_number = "+" + from_number
        if not to_number.startswith("+"):
            to_number = "+" + to_number

    # Build payload
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {"To": to_number, "Body": body}
    if is_whatsapp or not messaging_service_sid:
        if not from_number:
            raise RuntimeError("No 'from' number found in secret (keys tried: 'from', 'from_number', 'twilio_from').")
        data["From"] = from_number
    else:
        data["MessagingServiceSid"] = messaging_service_sid

    try:
        resp = requests.post(url, data=data, auth=(account_sid, auth_token), timeout=10)
        if resp.status_code >= 300:
            print(f"Twilio send failed {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Twilio send failed: {e}")



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
