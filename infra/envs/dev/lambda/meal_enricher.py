import json
import os
import boto3
import urllib.request
import urllib.error
from decimal import Decimal


# --- AWS setup ---
dynamodb = boto3.resource("dynamodb")
secrets = boto3.client("secretsmanager")

# Environment variables from Terraform
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "hb_events_dev")
SECRET_ARN = os.environ.get("NUTRITION_SECRET_ARN", "hb_nutrition_api_key_dev")

# Global cache to avoid multiple Secrets Manager calls per invocation
_creds_cache = None


# --- Helpers ---
def get_nutritionix_creds():
    """Fetch Nutritionix app_id and app_key once from Secrets Manager."""
    global _creds_cache
    if _creds_cache:
        return _creds_cache
    resp = secrets.get_secret_value(SecretId=SECRET_ARN)
    creds = json.loads(resp["SecretString"])
    _creds_cache = creds
    return creds

def to_decimal(x):
    if isinstance(x, float):
        # str() preserves value textually so Decimal is exact enough for DDB
        return Decimal(str(x))
    if isinstance(x, dict):
        return {k: to_decimal(v) for k, v in x.items()}
    if isinstance(x, list):
        return [to_decimal(v) for v in x]
    return x


def nutritionix_lookup(query: str, timezone: str = "US/Eastern"):
    """Send the meal text to Nutritionix and return parsed results."""
    creds = get_nutritionix_creds()
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    headers = {
        "Content-Type": "application/json",
        "x-app-id": creds["app_id"],
        "x-app-key": creds["app_key"],
    }
    body = json.dumps({"query": query, "timezone": timezone}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def summarize_foods(foods):
    """Summarize totals for calories, protein, carbs, and fat."""
    return {
        "calories": sum(float(f.get("nf_calories") or 0) for f in foods),
        "protein_g": sum(float(f.get("nf_protein") or 0) for f in foods),
        "carbs_g": sum(float(f.get("nf_total_carbohydrate") or 0) for f in foods),
        "fat_g": sum(float(f.get("nf_total_fat") or 0) for f in foods),
    }


def lambda_handler(event, context):
    print("Event received:", json.dumps(event)[:800])
    table = dynamodb.Table(EVENTS_TABLE)
    processed, skipped = 0, 0

    for record in event.get("Records", []):
        try:
            message = json.loads(record["Sns"]["Message"])
        except Exception as e:
            print("Bad SNS message:", e)
            skipped += 1
            continue

        # Get meal text
        text = message.get("text") or message.get("raw") or ""
        if not text:
            print("Skipping: no text found in message")
            skipped += 1
            continue

        # Clean "meal:" prefix if present
        if text.lower().startswith("meal:"):
            text = text.split(":", 1)[1].strip()

        if not text:
            print("Skipping empty query")
            skipped += 1
            continue

        try:
            # Query Nutritionix
            result = nutritionix_lookup(text)
            foods = result.get("foods", [])
            totals = summarize_foods(foods)

            # Update DynamoDB record with nutrition info
            table.update_item(
                Key={"pk": message["pk"], "sk": message["sk"]},
                UpdateExpression="SET nutrition = :n",
                ExpressionAttributeValues={
                    ":n": {
                        "query": text,
                        "totals": totals,
                        "foods": foods[:5],  # first 5 for brevity
                    }
                },
                ReturnValues="UPDATED_NEW",
            )
            print(f"✅ Updated meal {message['pk']} with totals {totals}")
            processed += 1

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"HTTPError {e.code}: {body} for {text!r}")
            skipped += 1
        except Exception as e:
            print("Error:", repr(e))
            skipped += 1

    # Return 200 so SNS doesn't retry
    return {
        "statusCode": 200,
        "body": json.dumps({"ok": True, "processed": processed, "skipped": skipped}),
    }
