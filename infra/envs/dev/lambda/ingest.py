import os, json, time, datetime, urllib.parse, boto3

dynamodb = boto3.resource("dynamodb")
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "hb_events_dev")

def _now():
    t = datetime.datetime.utcnow()
    return t.strftime("%Y-%m-%d"), int(t.timestamp())

def _twiml(msg: str):
    # Twilio expects XML (TwiML)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": f"<Response><Message>{msg}</Message></Response>",
    }

def _json_ok(payload):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }

def _parse_incoming(event):
    """
    Supports:
      - Twilio webhook: content-type 'application/x-www-form-urlencoded'
        -> fields: Body, From
      - Your curl/JSON tests: {"text": "...", "from": "..."}
    Returns: (text, sender)
    """
    body = event.get("body") or ""
    ctype = (event.get("headers", {}).get("content-type")
             or event.get("headers", {}).get("Content-Type", ""))

    # Twilio (form-encoded)
    if "application/x-www-form-urlencoded" in ctype:
        form = urllib.parse.parse_qs(body)
        text = (form.get("Body", [""])[0] or "").strip()
        sender = (form.get("From", [""])[0] or "").strip()
        return text, sender

    # JSON
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        data = {}
    return (data.get("text", "") or "").strip(), (data.get("from", "") or "").strip()

def lambda_handler(event, context):
    text, sender = _parse_incoming(event)
    print(f"Incoming -> sender={sender!r} text={text!r}")

    if not text:
        # If this came from Twilio, answer in TwiML; otherwise JSON
        is_twilio = "application/x-www-form-urlencoded" in (
            event.get("headers", {}).get("content-type", "").lower()
            + event.get("headers", {}).get("Content-Type", "").lower()
        )
        msg = "Empty message. Send 'meal: ...' or 'dev: ...'."
        return _twiml(msg) if is_twilio else _json_ok({"ok": False, "msg": msg})

    # Mode routing
    lower = text.lower().lstrip()
    is_dev = lower.startswith("dev:")
    is_meal = lower.startswith("meal:")

    # Strip mode prefix
    clean = text.split(":", 1)[1].strip() if (is_dev or is_meal) else text

    # Build base event
    dt, ts = _now()
    item = {
        "pk": sender or "self",
        "dt": dt,                 # for GSI dt queries
        "ts": ts,                 # sort/ordering
        "type": "meal" if is_meal else "text",
        "text": clean,
        "raw": text,
    }

    # DEV mode: do NOT persist, echo back
    if is_dev:
        return _twiml(f"[DEV] {clean} — received. Not logged.")

    # MEAL mode: persist
    if is_meal:
        table = dynamodb.Table(EVENTS_TABLE)
        table.put_item(Item=item)

        # 🔔 Publish event to SNS so the meal_enricher Lambda is triggered
        sns_arn = os.environ.get("MEAL_EVENTS_ARN")
        if sns_arn:
            sns = boto3.client("sns")
            sns.publish(
                TopicArn=sns_arn,
                Message=json.dumps(item),
                Subject="NewMealLogged"
            )

        return _twiml(f"✔ Logged meal: {clean}")

    # Fallback
    return _twiml("Send 'meal: ...' to log, or 'dev: ...' to test.")
