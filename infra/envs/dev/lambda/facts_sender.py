# file: infra/envs/dev/lambda/facts_sender.py
import os, json, random, logging, boto3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

ddb      = boto3.resource("dynamodb")
secrets  = boto3.client("secretsmanager")
sqs      = boto3.client("sqs")
http     = boto3.client("lambda")  # not used; placeholder if needed later

FACTS_TABLE        = os.environ["FACTS_TABLE"]
SQS_URL            = os.environ.get("SQS_URL", "")
TWILIO_SECRET_NAME = os.environ.get("TWILIO_SECRET_NAME", "hb_twilio_dev")
USER_ID            = os.environ.get("USER_ID", "me")
DEFAULT_DAILY_HOUR = int(os.environ.get("DEFAULT_DAILY_HOUR", "9"))
TZ_NAME            = os.environ.get("TZ_NAME", "America/New_York")

tbl = ddb.Table(FACTS_TABLE)
TZ  = ZoneInfo(TZ_NAME)

def _send_wa(to_number: str, body: str):
    sec = secrets.get_secret_value(SecretId=TWILIO_SECRET_NAME)["SecretString"]
    cfg = json.loads(sec)
    account_sid = cfg["account_sid"]; auth_token = cfg["auth_token"]
    from_number = cfg.get("from_wa", "whatsapp:+14155238886")
    import requests
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {"From": from_number, "To": to_number, "Body": body}
    r = requests.post(url, data=data, auth=(account_sid, auth_token), timeout=10)
    if r.status_code >= 400:
        log.error(f"Twilio send failed {r.status_code}: {r.text}")

def _get_config_item():
    # pk=USER_ID, sk='config#facts'
    r = tbl.get_item(Key={"pk": USER_ID, "sk": "config#facts"})
    return r.get("Item") or {}

def _set_config_item(**kwargs):
    item = _get_config_item()
    item.update({"pk": USER_ID, "sk": "config#facts"})
    item.update(kwargs)
    tbl.put_item(Item=item)
    return item

def _pick_random_fact(tag: str | None = None) -> dict | None:
    # simple scan (facts count expected small). If large, add a GSI.
    resp = tbl.scan(
        FilterExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("fact#")
    ) if False else tbl.query(
        KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("fact#")
    )
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = tbl.query(
            KeyConditionExpression=Key("pk").eq(USER_ID) & Key("sk").begins_with("fact#"),
            ExclusiveStartKey=resp["LastEvaluatedKey"]
        )
        items.extend(resp.get("Items", []))
    if tag:
        items = [it for it in items if tag.lower() in [t.lower() for t in it.get("tags", [])]]
    if not items:
        return None
    return random.choice(items)

def _hourly_tick():
    cfg = _get_config_item()
    enabled = bool(cfg.get("daily_enabled", False))
    send_hour = int(cfg.get("daily_hour", DEFAULT_DAILY_HOUR))
    to_number = cfg.get("to_number")  # e.g. "whatsapp:+1...."
    last_dt = (cfg.get("last_sent_dt") or "")
    now = datetime.now(TZ)
    today = now.date().isoformat()

    if not enabled or not to_number:
        return {"skipped": True, "reason": "disabled or no to_number"}
    if last_dt == today:
        return {"skipped": True, "reason": "already sent today"}
    if now.hour != send_hour:
        return {"skipped": True, "reason": f"hour {now.hour} != {send_hour}"}

    fact = _pick_random_fact(tag=None)
    if not fact:
        return {"skipped": True, "reason": "no facts available"}

    _send_wa(to_number, f"🧠 Migraine fact:\n{fact['text']}")
    _set_config_item(last_sent_dt=today)
    return {"sent": True, "id": fact["sk"]}

def _consume_queue(event):
    sent = 0
    for rec in event.get("Records", []):
        try:
            body = json.loads(rec["body"])
            if body.get("type") == "fact.upsert":
                # simple ack path — nothing else to do
                sent += 1
        except Exception as e:
            log.exception(e)
    return {"ok": True, "seen": sent}

def lambda_handler(event, context):
    # Distinguish sources: EventBridge (no Records) vs SQS (Records) vs direct test
    if "Records" in event and event["Records"] and "eventSource" in event["Records"][0] and "sqs" in event["Records"][0]["eventSource"]:
        return _consume_queue(event)
    # hourly tick
    return _hourly_tick()
