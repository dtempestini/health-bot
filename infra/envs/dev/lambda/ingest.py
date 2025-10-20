# file: infra/envs/dev/lambda/ingest.py
import os, json, urllib.parse, boto3, time

sns = boto3.client('sns')
ddb = boto3.resource('dynamodb')
TABLE = os.environ["EVENTS_TABLE"]
RAW_BUCKET = os.environ["RAW_BUCKET"]   # not used here but kept for future
USER_ID = os.environ.get("USER_ID", "me")
MEAL_EVENTS_ARN = os.environ["MEAL_EVENTS_ARN"]

def _now_ms():
    return int(time.time() * 1000)

def _parse_body(event):
    # Support Twilio (x-www-form-urlencoded) and JSON test posts
    ct = (event.get("headers") or {}).get("content-type") or (event.get("headers") or {}).get("Content-Type") or ""
    body = event.get("body") or ""
    is_base64 = event.get("isBase64Encoded")
    if is_base64:
        body = urllib.parse.unquote_plus(body)
    if "application/x-www-form-urlencoded" in ct:
        data = urllib.parse.parse_qs(body)
        text = (data.get("Body") or [""])[0].strip()
        sender = (data.get("From") or [""])[0].strip()
        return text, sender, "sms"
    # JSON fallback
    try:
        j = json.loads(body or "{}")
        return j.get("text","").strip(), j.get("sender","").strip(), j.get("source","cli")
    except Exception:
        return "", "", "unknown"

def _twiml(message):
    # Minimal TwiML reply
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{message}</Message>
</Response>"""

def lambda_handler(event, context):
    text, sender, source = _parse_body(event)
    ts = _now_ms()
    dt = time.strftime("%Y-%m-%d", time.gmtime(ts/1000))

    # Write “raw meal” event
    table = ddb.Table(TABLE)
    pk = f"meal#{context.aws_request_id}"
    item = {
        "pk": pk,
        "sk": str(ts),
        "type": "meal",
        "text": text,
        "source": source,
        "uid": USER_ID,
        "dt": dt,
    }
    if sender:
        item["sender"] = sender

    table.put_item(Item=item)

    # Fan-out to enricher
    sns.publish(
        TopicArn=MEAL_EVENTS_ARN,
        Message=json.dumps(item),
        Subject="meal_event"
    )

    # If this was Twilio, reply immediately with TwiML
    headers = {k.lower(): v for k,v in (event.get("headers") or {}).items()}
    ct = headers.get("content-type","")
    if "application/x-www-form-urlencoded" in ct:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/xml"},
            "body": _twiml("Got it! Logging your meal and calculating macros…")
        }

    # JSON response for curl/tests
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True, "id": pk, "dt": dt})
    }
