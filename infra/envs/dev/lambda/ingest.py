# ingest.py
import os, json, time, uuid, urllib.parse
import boto3
from decimal import Decimal


# ---- AWS clients/resources ----
dynamodb_res = boto3.resource("dynamodb")
dynamodb_cli = boto3.client("dynamodb")
sns = boto3.client("sns")

# ---- Environment ----
EVENTS_TABLE        = os.environ.get("EVENTS_TABLE", "hb_events_dev")
MEAL_EVENTS_ARN     = os.environ.get("MEAL_EVENTS_ARN")  # SNS topic for meals
USER_ID             = os.environ.get("USER_ID", "me")

PK_ENV = os.environ.get("PK_NAME")
SK_ENV = os.environ.get("SK_NAME")

def _get_key_schema():
    """
    Always use env-provided names when given; otherwise default to pk/sk.
    We assume 'S' (string) types for both keys to match your table.
    This avoids DescribeTable and any IAM dependency.
    """
    pk = PK_ENV or "pk"
    sk = SK_ENV or "sk"  # if your table had no sort key, set this to None
    return (pk, "S", sk, "S")

def to_decimal(x):
    if isinstance(x, float):
        # str() preserves value textually so Decimal is exact enough for DDB
        return Decimal(str(x))
    if isinstance(x, dict):
        return {k: to_decimal(v) for k, v in x.items()}
    if isinstance(x, list):
        return [to_decimal(v) for v in x]
    return x

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
    table.put_item(Item=to_decimal(item))

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
