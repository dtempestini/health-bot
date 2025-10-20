import os, json, time, uuid, urllib.parse, boto3

dynamodb_res = boto3.resource("dynamodb")
dynamodb_cli = boto3.client("dynamodb")
sns = boto3.client("sns")

EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "hb_events_dev")
MEAL_EVENTS_ARN = os.environ.get("MEAL_EVENTS_ARN")  # may be None

# Cache table key schema so we always use the correct key names
_key_schema_cache = None
def _get_key_names():
    global _key_schema_cache
    if _key_schema_cache:
        return _key_schema_cache
    desc = dynamodb_cli.describe_table(TableName=EVENTS_TABLE)
    keys = {e["KeyType"]: e["AttributeName"] for e in desc["Table"]["KeySchema"]}
    # HASH must exist; RANGE may or may not
    pk_name = keys["HASH"]
    sk_name = keys.get("RANGE")  # could be None
    _key_schema_cache = (pk_name, sk_name)
    print(f"Key names detected: pk={pk_name}, sk={sk_name}")
    return _key_schema_cache

def _now_parts():
    ts = int(time.time() * 1000)
    dt = time.strftime("%Y-%m-%d", time.gmtime(ts/1000))
    return ts, dt

def _parse_body(event):
    # Direct test: event already a dict with "text"
    if isinstance(event, dict) and "text" in event:
        return {"text": event["text"]}

    body = event.get("body")
    if body is None:
        return {}

    try:
        return json.loads(body)  # JSON body
    except Exception:
        pass

    form = urllib.parse.parse_qs(body or "")
    text = (form.get("Body") or form.get("text") or [""])[0]
    return {"text": text}

def lambda_handler(event, context):
    print("Incoming ->", "sender=", event.get("from") or "", " text=", _parse_body(event).get("text"))
    table = dynamodb_res.Table(EVENTS_TABLE)
    pk_name, sk_name = _get_key_names()

    payload = _parse_body(event)
    raw = (payload.get("text") or "").strip()
    if not raw:
        return {"statusCode": 200, "body": json.dumps({"ok": True, "msg": "empty"})}

    ts, dt = _now_parts()
    is_meal = raw.lower().startswith("meal:")
    pk_value = f"meal#{uuid.uuid4()}" if is_meal else f"event#{uuid.uuid4()}"

    # Build the item using whatever key names the table actually has
    item = {
        pk_name: pk_value,
        "dt": dt,
        "type": "meal" if is_meal else "event",
        "text": raw,
        "source": "sms",
    }
    if sk_name:
        # Use millisecond epoch for sort key; works if type is N or S
        item[sk_name] = ts

    # Write to DynamoDB
    table.put_item(Item=item)

    # Publish to SNS for meals
    if is_meal and MEAL_EVENTS_ARN:
        try:
            sns.publish(TopicArn=MEAL_EVENTS_ARN, Message=json.dumps(item), Subject="NewMealLogged")
            print("Published to SNS", MEAL_EVENTS_ARN)
        except Exception as e:
            print("SNS publish error:", repr(e))

    return {"statusCode": 200, "body": json.dumps({"ok": True, "id": pk_value, "dt": dt})}
