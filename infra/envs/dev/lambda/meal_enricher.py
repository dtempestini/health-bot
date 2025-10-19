import json, os, boto3, urllib.request, urllib.error

SECRETS_ARN = os.environ.get("NUTRITION_SECRET_ARN")
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "hb_events_dev")

secrets = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(EVENTS_TABLE)

_creds_cache = None
def _get_nutritionix_creds():
    global _creds_cache
    if _creds_cache is not None:
        return _creds_cache
    sid = SECRETS_ARN or "hb_nutrition_api_key_dev"
    sec = secrets.get_secret_value(SecretId=sid)["SecretString"]
    _creds_cache = json.loads(sec)  # {"app_id":"...","app_key":"..."}
    return _creds_cache

def _nutritionix_nlp(query: str, timezone: str = "US/Eastern"):
    creds = _get_nutritionix_creds()
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    payload = json.dumps({"query": query.strip(), "timezone": timezone}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-app-id", creds["app_id"])
    req.add_header("x-app-key", creds["app_key"])
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())

def _pairs_from_sns(event):
    pairs = []
    for rec in event.get("Records", []):
        try:
            msg = json.loads(rec["Sns"]["Message"])
        except Exception as e:
            print("Bad SNS record:", e)
            continue
        q = (msg.get("text") or msg.get("raw") or "").strip()
        if q.lower().startswith("meal:"):
            q = q.split(":", 1)[1].strip()
        if not q:
            print("Skipping: empty query in SNS message:", msg)
            continue
        pairs.append((msg, q))
    return pairs

def lambda_handler(event, context):
    print("SNS event:", json.dumps(event)[:1000])
    pairs = _pairs_from_sns(event)
    processed, skipped = 0, 0

    for item, query in pairs:
        try:
            data = _nutritionix_nlp(query)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"Nutritionix HTTPError {e.code}: {body} for query={query!r}")
            skipped += 1
            continue
        except Exception as e:
            print("Nutritionix error:", repr(e), "for query:", query)
            skipped += 1
            continue

        foods = data.get("foods", [])
        totals = {
            "cal": sum(float(f.get("nf_calories") or 0) for f in foods),
            "protein_g": sum(float(f.get("nf_protein") or 0) for f in foods),
            "carbs_g": sum(float(f.get("nf_total_carbohydrate") or 0) for f in foods),
            "fat_g": sum(float(f.get("nf_total_fat") or 0) for f in foods),
        }

        try:
            table.update_item(
                Key={"pk": item["pk"], "ts": item["ts"]},
                UpdateExpression="SET nutrition = :n",
                ExpressionAttributeValues={
                    ":n": {
                        "query": query,
                        "totals": totals,
                        "foods": foods[:5],
                    }
                },
                ReturnValues="UPDATED_NEW"
            )
            processed += 1
        except Exception as e:
            print("DynamoDB update error:", repr(e), "item:", item)

    # Always return 200 so SNS does NOT retry
    return {"statusCode": 200, "body": json.dumps({"ok": True, "processed": processed, "skipped": skipped})}
