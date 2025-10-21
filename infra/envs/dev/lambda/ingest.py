# file: infra/envs/dev/lambda/ingest.py
import os, json, urllib.parse, boto3, time, base64, re

sns = boto3.client('sns')
ddb = boto3.resource('dynamodb')

TABLE = os.environ["EVENTS_TABLE"]
RAW_BUCKET = os.environ.get("RAW_BUCKET", "")  # reserved for future
USER_ID = os.environ.get("USER_ID", "me")
MEAL_EVENTS_ARN = os.environ["MEAL_EVENTS_ARN"]

COMMAND_PREFIXES = ("/summary", "/week", "/month", "/lookup", "/undo", "/reset today", "/migraine", "/med", "/help")

def _now_ms() -> int:
    return int(time.time() * 1000)

def _parse_body(event):
    """
    Support Twilio (x-www-form-urlencoded) and JSON test posts.
    Returns (text, sender, transport)
    """
    headers = event.get("headers") or {}
    # header keys can vary in case
    ct = headers.get("content-type") or headers.get("Content-Type") or ""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8", "ignore")
        except Exception:
            pass

    if "application/x-www-form-urlencoded" in ct:
        data = urllib.parse.parse_qs(body)
        text = (data.get("Body") or [""])[0].strip()
        sender = (data.get("From") or [""])[0].strip()
        to = (data.get("To") or [""])[0].strip()
        transport = "whatsapp" if sender.startswith("whatsapp:") or to.startswith("whatsapp:") else "sms"
        return text, sender, transport, "twilio"
    # JSON fallback (manual tests)
    try:
        j = json.loads(body or "{}")
        return j.get("text","").strip(), j.get("sender","").strip(), j.get("source","cli"), "json"
    except Exception:
        return "", "", "unknown", "unknown"

def _classify(text: str):
    """
    Light classification for analytics / debugging.
    Returns kind, verb, args
    """
    t = (text or "").strip()
    low = t.lower()

    # meal
    if low.startswith("meal:") or low.startswith("meal "):
        verb = "meal"
        args = t.split(":", 1)[1].strip() if ":" in t else t.split(" ", 1)[1].strip()
        return "meal", verb, args

    # commands
    for p in COMMAND_PREFIXES:
        if low.startswith(p):
            if p.startswith("/lookup"):
                # keep full tail for enricher
                arg = t.split(":", 1)[1].strip() if ":" in t else (t.split(" ", 1)[1].strip() if " " in t else "")
                return "command", "lookup", arg
            if p == "/reset today":
                return "command", "reset_today", ""
            if p == "/migraine":
                arg = t.split(" ", 1)[1].strip() if " " in t else ""
                return "command", "migraine", arg
            if p == "/med":
                arg = t.split(" ", 1)[1].strip() if " " in t else ""
                return "command", "med", arg
            return "command", p.lstrip("/"), ""
    # unknown/free text (still forward; enricher will reply with help)
    return "unknown", "", t

def _twiml_empty():
    # Empty TwiML to acknowledge webhook without sending a message from this Lambda
    return """<?xml version="1.0" encoding="UTF-8"?><Response/>"""

def lambda_handler(event, context):
    text, sender, transport, source_kind = _parse_body(event)
    ts = _now_ms()
    dt = time.strftime("%Y-%m-%d", time.gmtime(ts/1000))

    kind, verb, args = _classify(text)

    # Write raw event to hb_events_dev
    table = ddb.Table(TABLE)
    pk = f"evt#{context.aws_request_id}"
    item = {
        "pk": pk,
        "sk": str(ts),
        "type": kind,           # "meal" | "command" | "unknown"
        "verb": verb,           # e.g., "meal", "summary", "lookup", "migraine", ...
        "text": text,
        "args": args,
        "source": transport,    # "whatsapp" | "sms" | "cli"
        "uid": USER_ID,
        "dt": dt,
    }
    if sender:
        item["sender"] = sender

    table.put_item(Item=item)

    # Fan-out to enricher (single topic for all types)
    sns.publish(
        TopicArn=MEAL_EVENTS_ARN,
        Message=json.dumps(item),
        Subject="meal_or_command_event"
    )

    # If Twilio webhook, just 200 OK with empty TwiML so we don't send a duplicate user-visible message here.
    headers = { (k or "").lower(): v for k,v in (event.get("headers") or {}).items() }
    ct = headers.get("content-type","")
    if "application/x-www-form-urlencoded" in ct:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/xml"},
            "body": _twiml_empty()
        }

    # JSON response for curl/tests
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True, "id": pk, "dt": dt, "kind": kind})
    }
