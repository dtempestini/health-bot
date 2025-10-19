import os, json, uuid, datetime, base64
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

def _parse(body_text: str):
    """
    Minimal parser; prefixes are optional.
    Examples:
      meal: chicken salad 550kcal 40p 35c 22f
      migraine: start 6/10 aura=yes
      migraine: end
      fast: start 12:05
      fast: end 20:00
      note: slept poorly
    """
    if not body_text:
        return {"type": "note", "text": ""}

    t = body_text.strip()
    lowered = t.lower()
    if lowered.startswith("meal:"):
        return {"type":"meal","text":t[5:].strip()}
    if lowered.startswith("migraine:"):
        return {"type":"migraine","text":t[9:].strip()}
    if lowered.startswith("fast:"):
        return {"type":"fast","text":t[5:].strip()}
    if lowered.startswith("note:"):
        return {"type":"note","text":t[5:].strip()}
    return {"type":"note","text":t}

def handler(event, context):
    # HTTP API v2.0; accept either JSON body {"text": "..."} or raw text.
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body_raw = base64.b64decode(body_raw).decode("utf-8")
        except Exception:
            pass

    try:
        obj = json.loads(body_raw) if body_raw.strip().startswith("{") else {"text": body_raw}
    except Exception:
        obj = {"text": body_raw}

    text = (obj.get("text") or "").strip()
    ts   = _now_iso()
    dt   = _today()
    parsed = _parse(text)

    record = {
        "id": str(uuid.uuid4()),
        "user_id": USER_ID,
        "ts": ts,
        "dt": dt,
        "type": parsed["type"],
        "text": text,
        "parsed": parsed,
        "source": "api"
    }

    # Write to S3 (partitioned by date)
    key = f"events/dt={dt}/{record['id']}.json"
    s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=json.dumps(record).encode("utf-8"))

    # Write to DynamoDB
    ddb.put_item(
        TableName=EVENTS_TABLE,
        Item={
            "pk": {"S": USER_ID},
            "sk": {"S": ts},
            "dt": {"S": dt},
            "type": {"S": parsed["type"]},
            "payload": {"S": json.dumps(record)}
        }
    )

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"ok": True, "id": record["id"], "dt": dt})
    }
