import os, json, uuid, datetime, base64, urllib.parse, urllib.request, urllib.error, re
import boto3

RAW_BUCKET   = os.environ["RAW_BUCKET"]
EVENTS_TABLE = os.environ["EVENTS_TABLE"]
USER_ID      = os.environ.get("USER_ID", "me")
NUTRITION_SECRET_NAME = os.environ.get("NUTRITION_SECRET_NAME")

s3  = boto3.client("s3")
ddb = boto3.client("dynamodb")
secrets = boto3.client("secretsmanager")

MACRO_RE = re.compile(
    r'(?P<kcal>\d+)\s*kcal|'
    r'(?P<p>\d+)\s*[pP]\b|'
    r'(?P<c>\d+)\s*[cC]\b|'
    r'(?P<f>\d+)\s*[fF]\b'
)

def _now_iso():
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

def _today():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def _parse_prefix_and_text(t):
    t = (t or "").strip()
    low = t.lower()
    dev = False
    # dev/dry-run prefixes
    for pref in ("dev:", "test:"):
        if low.startswith(pref):
            dev = True
            t = t[len(pref):].strip()
            low = t.lower()
            break
    # type prefixes
    if   low.startswith("meal:"):     return dev, {"type":"meal",    "text":t[5:].strip()}
    elif low.startswith("migraine:"): return dev, {"type":"migraine","text":t[9:].strip()}
    elif low.startswith("fast:"):     return dev, {"type":"fast",    "text":t[5:].strip()}
    elif low.startswith("note:"):     return dev, {"type":"note",    "text":t[5:].strip()}
    else:                             return dev, {"type":"meal",    "text":t}  # default meal

def _record_persistent(text):
    ts = _now_iso(); dt = _today()
    _, parsed = _parse_prefix_and_text(text)
    rec = {
        "id": str(uuid.uuid4()),
        "user_id": USER_ID,
        "ts": ts,
        "dt": dt,
        "type": parsed["type"],
        "text": text,
        "parsed": parsed,
        "source": "api"
    }
    # S3
    key = f"events/dt={dt}/{rec['id']}.json"
    s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=json.dumps(rec).encode("utf-8"))
    # DDB
    ddb.put_item(
        TableName=EVENTS_TABLE,
        Item={
            "pk": {"S": USER_ID},
            "sk": {"S": ts},
            "dt": {"S": dt},
            "type": {"S": parsed["type"]},
            "text": {"S": text},
            "payload": {"S": json.dumps(rec)}
        }
    )
    return rec

def _parse_macros_inline(text):
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

def _get_nutritionix_keys():
    if not NUTRITION_SECRET_NAME:
        raise RuntimeError("NUTRITION_SECRET_NAME not set")
    val = secrets.get_secret_value(SecretId=NUTRITION_SECRET_NAME)
    data = json.loads(val["SecretString"])
    return data["app_id"], data["app_key"]

def _nutritionix_nlp(query_text):
    app_id, app_key = _get_nutritionix_keys()
    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    payload = json.dumps({"query": query_text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-app-id", app_id)
    req.add_header("x-app-key", app_key)
    with urllib.request.urlopen(req, timeout=8) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    foods = out.get("foods", []) or []
    total = {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0}
    for f in foods:
        total["kcal"]    += int(round(f.get("nf_calories") or 0))
        total["protein"] += int(round(f.get("nf_protein") or 0))
        total["carbs"]   += int(round(f.get("nf_total_carbohydrate") or 0))
        total["fat"]     += int(round(f.get("nf_total_fat") or 0))
    return total

def _twiml(msg):
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{msg}</Message></Response>'

def handler(event, context):
    headers = { (k or "").lower(): v for k, v in (event.get("headers") or {}).items() }
    ctype = headers.get("content-type","")

    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try: body_raw = base64.b64decode(body_raw).decode("utf-8")
        except Exception: pass

    # Twilio: application/x-www-form-urlencoded → Body=<text>
    if "application/x-www-form-urlencoded" in ctype:
        form = urllib.parse.parse_qs(body_raw)
        body_text = (form.get("Body",[None])[0] or "").strip()
        if not body_text:
            return {"statusCode": 200, "headers":{"Content-Type":"text/xml"}, "body": _twiml('Send a meal like: meal: chicken salad')}
        dev, parsed = _parse_prefix_and_text(body_text)
        if dev:
            # DRY-RUN: reply with Nutritionix totals but do not persist
            inline = _parse_macros_inline(parsed["text"])
            if inline is None:
                q = parsed["text"]
                if q.lower().startswith("meal:"):
                    q = q[5:].strip()
                totals = _nutritionix_nlp(q)
            else:
                totals = inline
            msg = f"[DEV] {totals['kcal']} kcal (P {totals['protein']} / C {totals['carbs']} / F {totals['fat']})"
            return {"statusCode": 200, "headers":{"Content-Type":"text/xml"}, "body": _twiml(msg)}
        else:
            # persistent path (triggers enricher via DDB stream)
            rec = _record_persistent(body_text)
            return {"statusCode": 200, "headers":{"Content-Type":"text/xml"}, "body": _twiml(f'✔ logged: {rec["parsed"]["type"]}')}
    # JSON or plain text
    try:
        obj = json.loads(body_raw) if body_raw.strip().startswith("{") else {"text": body_raw}
    except Exception:
        obj = {"text": body_raw}
    body_text = (obj.get("text") or "").strip()
    if not body_text:
        return {"statusCode": 400, "headers":{"content-type":"application/json"}, "body": json.dumps({"ok": False, "error":"missing text"})}
    dev, parsed = _parse_prefix_and_text(body_text)
    if dev:
        inline = _parse_macros_inline(parsed["text"])
        if inline is None:
            q = parsed["text"]
            if q.lower().startswith("meal:"):
                q = q[5:].strip()
            totals = _nutritionix_nlp(q)
        else:
            totals = inline
        return {"statusCode": 200, "headers":{"content-type":"application/json"}, "body": json.dumps({"ok": True, "dev": True, "totals": totals})}
    else:
        _record_persistent(body_text)
        return {"statusCode": 200, "headers":{"content-type":"application/json"}, "body": json.dumps({"ok": True})}
