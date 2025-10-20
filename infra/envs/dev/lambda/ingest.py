import os, json, time, uuid, urllib.parse, boto3

dynamodb_res = boto3.resource("dynamodb")
dynamodb_cli = boto3.client("dynamodb")
sns = boto3.client("sns")

EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "hb_events_dev")
MEAL_EVENTS_ARN = os.environ.get("MEAL_EVENTS_ARN")

_schema_cache = None

def _get_key_schema():
    """
    Returns: (pk_name, pk_type, sk_name_or_None, sk_type_or_None)
    Types are one of: 'S', 'N', 'B'
    """
    global _schema_cache
    if _schema_cache:
        return _schema_cache

    desc = dynamodb_cli.describe_table(TableName=EVENTS_TABLE)
    # Build name -> type map from AttributeDefinitions
    type_map = {a["AttributeName"]: a["AttributeType"] for a in desc["Table"]["AttributeDefinitions"]}
    # Extract key names from KeySchema
    ks = {e["KeyType"]: e["AttributeName"] for e in desc["Table"]["KeySchema"]}

    pk_name = ks["HASH"]
    pk_type = type_map[pk_name]
    sk_name = ks.get("RANGE")
    sk_type = type_map.get(sk_name) if sk_name else None

    _schema_cache = (pk_name, pk_type, sk_name, sk_type)
    print(f"Key schema: pk=({pk_name},{pk_type}) sk=({sk_name},{sk_type})")
    return _schema_cache

def _now_parts():
    ts = int(time.time() * 1000)
    dt = time.strftime("%Y-%m-%d", time.gmtime(ts/1000))
    return ts, dt

def _parse_body(event):
    if isinstance(event, dict) and "text" in event:
        return {"text": event["text"]}

    body = event.get("body")
    if body is None:
        return {}

    try:
        return json.loads(body)
    except Exception:
        pass

    form = urllib.parse.parse_qs(body or "")
    text = (form.get("Body") or form.get("text") or [""])[0]
    return {"text": text}

def lambda_handler(event, context):
    payload = _parse_body(event)
    raw = (payload.get("text") or "").strip()
    print("Incoming -> sender=", event.get("from") or "", " text=", raw)

    if not raw:
        return {"statusCode": 200, "body": json.dumps({"ok": True, "msg": "empty"})}

    table = dynamodb_res.Table(EVENTS_TABLE)
    pk_name, pk_type, sk_name, sk_type = _get_key_schema()

    ts, dt = _now_parts()
    is_meal = raw.lower().startswith("meal:")
    pk_val  = f"meal#{uuid.uuid4()}" if is_meal else f"event#{uuid.uuid4()}"

    # Coerce to correct Dynamo types for keys
    if pk_type == "N":
        # very unlikely here, but handle generically
        try:
            pk_val = float(pk_val)  # would be odd—keys are strings in our design
        except Exception:
            pass
    else:
        pk_val = str(pk_val)

    item = {
        pk_name: pk_val,
        "dt": dt,
        "type": "meal" if is_meal else "event",
        "text": raw,
        "source": "sms",
    }

    if sk_name:
        sk_val = ts  # default numeric timestamp
        if sk_type == "S":
            sk_val = str(ts)     # your table expects String
        elif sk_type == "N":
            sk_val = ts          # number OK
        item[sk_name] = sk_val

    # Write
    table.put_item(Item=item)

    # Notify enrich pipeline for meals
    if is_meal and MEAL_EVENTS_ARN:
        try:
            sns.publish(TopicArn=MEAL_EVENTS_ARN, Message=json.dumps(item), Subject="NewMealLogged")
            print("Published to SNS", MEAL_EVENTS_ARN)
        except Exception as e:
            print("SNS publish error:", repr(e))

    return {"statusCode": 200, "body": json.dumps({"ok": True, "id": item[pk_name], "dt": dt})}
