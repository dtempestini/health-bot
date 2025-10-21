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

def _send_sms(to_number, body):
    tw = _get_twilio()
    if not tw or not to_number:
        return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{tw['sid']}/Messages.json"
    auth = (tw["sid"], tw["token"])
    data = {
        "From": tw["from"],
        "To": to_number,
        "Body": body
    }
    r = requests.post(url, auth=auth, data=data, timeout=10)
    if r.status_code not in (200, 201):
        # Don't crash the Lambda—just log
        print(f"Twilio send failed {r.status_code}: {r.text}")

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
            if sender.startswith("whatsapp:"):
                from_number = "whatsapp:" + from_number.lstrip("+")
                sender = "whatsapp:" + sender.lstrip("+")

            _send_sms(sender, body)

        print(f"Enrichment stored OK {meal_pk} {ts}")

    return {"ok": True}
