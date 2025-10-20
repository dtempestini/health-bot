# infra/envs/dev/lambda/meal_enricher.py
import os
import json
from decimal import Decimal
import boto3
import requests

# --- Config from environment ---
EVENTS_TABLE = os.environ["EVENTS_TABLE"]                     # e.g., hb_events_dev
SECRET_NAME  = os.environ["NUTRITION_SECRET_NAME"]            # JSON: {"app_id":"...","app_key":"..."}

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(EVENTS_TABLE)
secrets = boto3.client("secretsmanager")

# ---------- Helpers ----------

def to_decimal(x):
    """Recursively convert floats to Decimal so DynamoDB accepts them."""
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
    # If using binary (unlikely here)
    return json.loads(resp["SecretBinary"].decode("utf-8"))

def nutritionix_nlp(query: str) -> dict:
    """Call Nutritionix natural language endpoint and return macro totals + items list."""
    creds = get_secret_json(SECRET_NAME)  # {"app_id":"...","app_key":"..."}
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    headers = {
        "x-app-id":  creds["app_id"],
        "x-app-key": creds["app_key"],
        "Content-Type": "application/json",
    }
    body = {"query": query}
    r = requests.post(url, headers=headers, json=body, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"Nutritionix HTTPError {r.status_code}: {r.text}")

    data = r.json()
    foods = data.get("foods", []) or []

    # totals (floats here; we'll Decimal-ize before writing)
    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    items  = []

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

# ---------- Lambda ----------

def lambda_handler(event, context):
    # Records arrive from SNS; each 'Message' is a JSON string from ingest
    for rec in event.get("Records", []):
        msg_raw = rec.get("Sns", {}).get("Message", "{}")
        try:
            msg = json.loads(msg_raw)
        except Exception:
            print("Could not JSON-parse SNS message:", msg_raw)
            continue

        # Expect shape from ingest: {"pk": "...", "sk" or "ts": "...", "dt": "...", "type": "meal", "text": "...", ...}
        if msg.get("type") != "meal":
            print("Skipping non-meal message:", msg)
            continue

        text = msg.get("text") or ""
        if not text:
            print("No text in meal message; skipping:", msg)
            continue

        pk = msg["pk"]
        sk = msg.get("sk") or msg.get("ts")  # tolerate either key
        dt = msg.get("dt")

        # Call Nutritionix
        nutrition = nutritionix_nlp(text)

        # Build enriched record (write back to the same table)
        enriched_item = {
            "pk": pk,                       # reuse meal pk (e.g., meal#UUID)
            "sk": sk,                       # same sort key timestamp
            "type": "meal.enriched",
            "dt": dt,
            "text": text,
            "source": "enricher",
            "nutrition": nutrition,         # has floats -> convert below
        }

        # Convert floats → Decimal for DynamoDB, then write
        table.put_item(Item=to_decimal(enriched_item))

        print("Enrichment stored OK:", pk, sk)

    return {"ok": True}
