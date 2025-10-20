# ingest.py
import os, json, time, uuid, urllib.parse
import boto3

# ---- AWS clients/resources ----
dynamodb_res = boto3.resource("dynamodb")
dynamodb_cli = boto3.client("dynamodb")
sns = boto3.client("sns")

# ---- Environment ----
EVENTS_TABLE        = os.environ.get("EVENTS_TABLE", "hb_events_dev")
MEAL_EVENTS_ARN     = os.environ.get("MEAL_EVENTS_ARN")  # SNS topic for meals
USER_ID             = os.environ.get("USER_ID", "me")

# If these are present we will NOT call DescribeTable (no IAM needed)
PK_ENV = os.environ.get("PK_NAME")  # e.g., "pk"
SK_ENV = os.environ.get("SK_NAME")  # e.g., "sk" (optional)

_schema_cache = None


def _get_key_schema():
    """
    Returns: (pk_name, pk_type, sk_name_or_None, sk_type_or_None)
    Types are one of 'S', 'N', 'B'. We assume 'S' for env-provided keys.
    """
    global _schema_cache
    if _schema_cache:
        return _schema_cache

    if PK_ENV:
        # Use env-provided key names; assume string types (fits your table)
        print(f"Using PK/SK from env: {PK_ENV}/{SK_ENV}")
        _schema_cache = (PK_ENV, "S", SK_ENV, "S" if SK_ENV else None)
        return _schema_cache

    # Fallback: discover from DynamoDB (requires DescribeTable permission)
    desc = dynamodb_cli.describe_table(TableName=EVENTS_TABLE)
    type_map = {a["AttributeName"]: a["AttributeType"]
                for a in desc["Table"]["AttributeDefinitions"]}
    ks = {e["KeyType"]: e["AttributeName"]
          for e in desc["Table"]["KeySchema"]}
    pk_name = ks["HASH"]; pk_type = type_map[pk_name]
    sk_name = ks.get("RANGE"); sk_type = type_map.get(sk_name) if sk_name else None
    _schema_cache = (pk_name, pk_type, sk_name, sk_type)
    print(f"Key schema: pk=({pk_name},{pk_type}) sk=({sk_name},{sk_type})")
    return _schema_cache


def _now_parts():
    ts_ms = int(time.time() * 1000)
    dt = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))
    return ts_ms, dt


def _parse_body(event):
    """
    Accepts:
      - { "text": "..." } JSON
      - raw API GW v2 event with JSON body
      - Twilio/x-www-form-urlencoded (Body=...&From=...)
    Returns: dict with at least {"text": "..."} (or empty text)
    """
    # direct invocation form
    if isinstance(event, dict) and "text" in event:
        return {"text": event["text"]}

    body = event.get("body")
    if body is None:
        return {"text": ""}

    # try JSON
    try:
        j = json.loads(body)
        if isinstance(j, dict) and "text" in j:
            return {"text": j["text"]}
    except Exception:
        pass

    # try form
    form = urllib.parse.parse_qs(body or "")
    text = (form.get("Body") or form.get("text") or [""])[0]
    return {"text": text}


def lambda_handler(event, context):
    payload = _parse_body(event)
    raw = (payload.get("text") or "").strip()

    print("Incoming -> sender=",
          (payload.get("from") or event.get("from") or ""),
          " text=", raw)

    # Gracefully handle empty input
    if not raw:
        return {"statusCode": 200,
                "body": json.dumps({"ok": True, "msg": "empty"})}

    # Determine type
    is_meal = raw.lower().startswith("meal:")
    evt_type = "meal" if is_meal else "event"

    # Keys & timestamps
    ts, dt = _now_parts()
    pk_name, pk_type, sk_name, sk_type = _get_key_schema()

    # Generate a stable-ish pk; you can change this later to your liking
    pk_val = f"{evt_type}#{uuid.uuid4()}"

    # Build item, coercing key types correctly
    item = {
        pk_name: str(pk_val) if pk_type == "S" else pk_val,
        "dt": dt,
        "type": evt_type,
        "text": raw,
        "source": "sms",
        "uid": USER_ID,
    }
    if sk_name:
        # Your table defines sk as String; coerce if needed
        item[sk_name] = str(ts) if (sk_type or "S") == "S" else ts

    # Write to DynamoDB
    table = dynamodb_res.Table(EVENTS_TABLE)
    table.put_item(Item=item)

    # Fan-out meals to SNS (meal enricher)
    if is_meal and MEAL_EVENTS_ARN:
        try:
            sns.publish(
                TopicArn=MEAL_EVENTS_ARN,
                Message=json.dumps(item),
                Subject="NewMealLogged"
            )
            print("Published to SNS", MEAL_EVENTS_ARN)
        except Exception as e:
            print("SNS publish error:", repr(e))

    return {
        "statusCode": 200,
        "body": json.dumps({"ok": True, "id": item[pk_name], "dt": dt})
    }
