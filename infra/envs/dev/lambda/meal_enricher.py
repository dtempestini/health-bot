# infra/envs/dev/lambda/meal_enricher.py
import os
import json
from decimal import Decimal
import boto3
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

EVENTS_TABLE = os.environ["EVENTS_TABLE"]
SECRET_NAME  = os.environ["NUTRITION_SECRET_NAME"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(EVENTS_TABLE)
secrets = boto3.client("secretsmanager")

def to_decimal(x):
    if isinstance(x, float):
        return Decimal(str(x))
    if isinstance(x, dict):
        return {k: to_decimal(v) for k, v in x.items()}
    if isinstance(x, list):
        return [to_decimal(v) for v in x]
    return x

def get_secret_json(name: str) -> dict:
    resp = secrets.get_secret_value(SecretId=name)
    if "SecretString" in resp:
        return json.loads(resp["SecretString"])
    return json.loads(resp["SecretBinary"].decode("utf-8"))

def http_post_json(url: str, headers: dict, body: dict, timeout: int = 10) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {msg}") from e
    except URLError as e:
        raise RuntimeError(f"HTTP error: {e}") from e

def nutritionix_nlp(query: str) -> dict:
    creds = get_secret_json(SECRET_NAME)  # {"app_id":"...","app_key":"..."}
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    headers = {
        "x-app-id":  creds["app_id"],
        "x-app-key": creds["app_key"],
        "Content-Type": "application/json",
    }
    data = http_post_json(url, headers, {"query": query})
    foods = data.get("foods", []) or []

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    items = []
    for f in foods:
        cals = float(f.get("nf_calories") or 0)
        prot = float(f.get("nf_protein") or 0)
        carb = float(f.get("nf_total_carbohydrate") or 0)
        fat  = float(f.get("nf_total_fat") or 0)
        totals["calories"]  += cals
        totals["protein_g"] += prot
        totals["carbs_g"]   += carb
        totals["fat_g"]     += fat
        items.append({
            "name": f.get("food_name"),
            "serving_qty": f.get("serving_qty"),
            "serving_unit": f.get("serving_unit"),
            "calories": cals,
            "protein_g": prot,
            "carbs_g": carb,
            "fat_g": fat,
        })
    return {"totals": totals, "items": items}

def lambda_handler(event, context):
    for rec in event.get("Records", []):
        msg_raw = rec.get("Sns", {}).get("Message", "{}")
        try:
            msg = json.loads(msg_raw)
        except Exception:
            print("Could not parse SNS message:", msg_raw)
            continue

        if msg.get("type") != "meal":
            print("Skipping non-meal:", msg)
            continue

        text = msg.get("text") or ""
        if not text:
            print("Empty text; skip")
            continue

        pk = msg["pk"]
        sk = msg.get("sk") or msg.get("ts")
        dt = msg.get("dt")

        nutrition = nutritionix_nlp(text)

        item = {
            "pk": pk,
            "sk": sk,
            "type": "meal.enriched",
            "dt": dt,
            "text": text,
            "source": "enricher",
            "nutrition": nutrition,
        }
        table.put_item(Item=to_decimal(item))
        print("Enrichment stored OK", pk, sk)

    return {"ok": True}
