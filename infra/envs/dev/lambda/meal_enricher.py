import os, json, re, uuid, datetime, urllib.request, urllib.error
import boto3

USER_ID        = os.environ.get("USER_ID", "me")
MEALS_TABLE    = os.environ["MEALS_TABLE"]
TOTALS_TABLE   = os.environ["TOTALS_TABLE"]
CURATED_BUCKET = os.environ["CURATED_BUCKET"]
NOTIFY_TOPIC   = os.environ["NOTIFY_TOPIC"]
NUTRITION_SECRET_NAME = os.environ.get("NUTRITION_SECRET_NAME")

ddb = boto3.client("dynamodb")
s3  = boto3.client("s3")
sns = boto3.client("sns")
secrets = boto3.client("secretsmanager")

MACRO_RE = re.compile(
    r'(?P<kcal>\d+)\s*kcal|'
    r'(?P<p>\d+)\s*[pP]\b|'
    r'(?P<c>\d+)\s*[cC]\b|'
    r'(?P<f>\d+)\s*[fF]\b'
)

def _today():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def _iso_now():
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

def parse_macros_from_text(text):
    kcal = prot = carbs = fat = None
    for m in MACRO_RE.finditer(text or ""):
        d = m.groupdict()
        if d.get("kcal"): kcal = int(d["kcal"])
        if d.get("p"):    prot = int(d["p"])
        if d.get("c"):    carbs = int(d["c"])
        if d.get("f"):    fat = int(d["f"])
    if any(v is not None for v in (kcal, prot, carbs, fat)):
        return {"kcal": kcal or 0, "protein": prot or 0, "carbs": carbs or 0, "fat": fat or 0}
    return None

def get_nutritionix_keys():
    if not NUTRITION_SECRET_NAME:
        raise RuntimeError("NUTRITION_SECRET_NAME not set")
    val = secrets.get_secret_value(SecretId=NUTRITION_SECRET_NAME)
    data = json.loads(val["SecretString"])
    return data["app_id"], data["app_key"]

def nutritionix_nlp(query_text):
    app_id, app_key = get_nutritionix_keys()
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    payload = json.dumps({"query": query_text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-app-id", app_id)
    req.add_header("x-app-key", app_key)

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Nutritionix HTTPError {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"Nutritionix error: {e}")

    foods = out.get("foods", []) or []
    total = {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0}
    items = []
    for f in foods:
        kcal  = int(round(f.get("nf_calories") or 0))
        prot  = int(round(f.get("nf_protein") or 0))
        carbs = int(round(f.get("nf_total_carbohydrate") or 0))
        fat   = int(round(f.get("nf_total_fat") or 0))
        item = {
            "name": f.get("food_name"),
            "serving_qty": f.get("serving_qty"),
            "serving_unit": f.get("serving_unit"),
            "kcal": kcal, "protein": prot, "carbs": carbs, "fat": fat
        }
        items.append(item)
        total["kcal"]    += kcal
        total["protein"] += prot
        total["carbs"]   += carbs
        total["fat"]     += fat

    return total, items

def put_curated_s3(record):
    dt = record["dt"]
    key = f"meals/dt={dt}/{record['id']}.json"
    s3.put_object(Bucket=CURATED_BUCKET, Key=key, Body=json.dumps(record).encode("utf-8"))

def put_meal_row(ts, dt, text, totals, items):
    ddb.put_item(
        TableName=MEALS_TABLE,
        Item={
            "pk": {"S": USER_ID},
            "sk": {"S": ts},
            "dt": {"S": dt},
            "type": {"S": "meal"},
            "text": {"S": text},
            "kcal": {"N": str(totals["kcal"])},
            "protein": {"N": str(totals["protein"])},
            "carbs": {"N": str(totals["carbs"])},
            "fat": {"N": str(totals["fat"])},
            "items": {"S": json.dumps(items)[:35000]}
        }
    )

def add_daily_totals(dt, totals):
    expr = "ADD kcal :k, protein :p, carbs :c, fat :f"
    ddb.update_item(
        TableName=TOTALS_TABLE,
        Key={"pk": {"S": USER_ID}, "sk": {"S": dt}},
        UpdateExpression=expr,
        ExpressionAttributeValues={
            ":k": {"N": str(totals["kcal"])},
            ":p": {"N": str(totals["protein"])},
            ":c": {"N": str(totals["carbs"])},
            ":f": {"N": str(totals["fat"])}
        }
    )

def fetch_daily_totals(dt):
    r = ddb.get_item(TableName=TOTALS_TABLE, Key={"pk": {"S": USER_ID}, "sk": {"S": dt}})
    it = r.get("Item") or {}
    def N(name): return int((it.get(name) or {}).get("N", "0"))
    return {"kcal": N("kcal"), "protein": N("protein"), "carbs": N("carbs"), "fat": N("fat")}

def notify(ts, dt, text, totals, day_totals):
    msg = (f"Meal logged: {totals['kcal']} kcal "
           f"(P {totals['protein']} / C {totals['carbs']} / F {totals['fat']}).\n"
           f"Day {dt}: {day_totals['kcal']} kcal "
           f"(P {day_totals['protein']} / C {day_totals['carbs']} / F {day_totals['fat']}).")
    sns.publish(TopicArn=NOTIFY_TOPIC, Message=msg, Subject="Meal logged")

def handler(event, context):
    for rec in event.get("Records", []):
        if rec.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        new = (rec.get("dynamodb") or {}).get("NewImage") or {}
        typ = (new.get("type") or {}).get("S")
        if typ != "meal":
            continue

        text = (new.get("text") or {}).get("S", "")
        ts   = (new.get("sk")   or {}).get("S") or _iso_now()
        dt   = (new.get("dt")   or {}).get("S") or _today()

        # Prefer explicit macros if the user typed them
        m = parse_macros_from_text(text)
        items = []
        if m is None:
            q = text[5:].strip() if text.lower().startswith("meal:") else text
            totals, items = nutritionix_nlp(q)
        else:
            totals = m

        curated = {
            "id": str(uuid.uuid4()),
            "user_id": USER_ID,
            "ts": ts,
            "dt": dt,
            "type": "meal",
            "text": text,
            "kcal": totals["kcal"],
            "protein": totals["protein"],
            "carbs": totals["carbs"],
            "fat": totals["fat"],
            "items": items
        }

        put_curated_s3(curated)
        put_meal_row(ts, dt, text, totals, items)
        add_daily_totals(dt, totals)
        day_totals = fetch_daily_totals(dt)
        notify(ts, dt, text, totals, day_totals)

    return {"statusCode": 200}
