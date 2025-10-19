import os, json, uuid, datetime, base64, urllib.parse
import boto3

RAW_BUCKET   = os.environ["RAW_BUCKET"]
EVENTS_TABLE = os.environ["EVENTS_TABLE"]
USER_ID      = os.environ.get("USER_ID", "me")

s3  = boto3.client("s3")
ddb = boto3.client("dynamodb")

def _now_iso():
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

def _today():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def _parse(body_text):
    t = (body_text or "").strip()
    low = t.lower()
    if low.startswith("meal:"):     return {"type":"meal","text":t[5:].strip()}
    if low.startswith("migraine:"): return {"type":"migraine","text":t[9:].strip()}
    if low.startswith("fast:"):     return {"type":"fast","text":t[5:].strip()}
    if low.startswith("note:"):     return {"type":"note","text":t[5:].strip()}
    # default to meal when freeform text via SMS
    return {"type":"meal","text":t}

def _record(text):
    ts = _now_iso(); dt = _today()
    parsed = _parse(text)
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

def handler(event, context):
    headers = { (k or "").lower(): v for k, v in (event.get("headers") or {}).items() }
    ctype = headers.get("content-type","")

    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try: body_raw = base64.b64decode(body_raw).decode("utf-8")
        except Exception: pass

    if "application/x-www-form-urlencoded" in ctype:
        form = urllib.parse.parse_qs(body_raw)
        text = (form.get("Body",[None])[0] or "").strip()
        if text:
            rec = _record(text)
            twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>✔ logged: {rec["parsed"]["type"]}</Message></Response>'
            return {"statusCode": 200, "headers":{"Content-Type":"text/xml"}, "body": twiml}
        return {"statusCode": 200, "headers":{"Content-Type":"text/xml"}, "body": '<?xml version="1.0"?><Response><Message>Send a meal, e.g. "meal: chicken salad"</Message></Response>'}

    try:
        obj = json.loads(body_raw) if body_raw.strip().startswith("{") else {"text": body_raw}
    except Exception:
        obj = {"text": body_raw}
    text = (obj.get("text") or "").strip()

    if text:
      _record(text)
      return {"statusCode": 200, "headers":{"content-type":"application/json"}, "body": json.dumps({"ok": True})}
    else:
      return {"statusCode": 400, "headers":{"content-type":"application/json"}, "body": json.dumps({"ok": False, "error":"missing text"})}
