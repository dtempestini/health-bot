# file: infra/envs/dev/lambda/facts_ingest.py
import os, csv, io, json, uuid, logging, boto3
from datetime import datetime
from decimal import Decimal

log = logging.getLogger()
log.setLevel(logging.INFO)

s3   = boto3.client("s3")
ddb  = boto3.resource("dynamodb")
sqs  = boto3.client("sqs")

FACTS_TABLE = os.environ["FACTS_TABLE"]
SQS_URL     = os.environ.get("SQS_URL", "")
USER_ID     = os.environ.get("USER_ID", "me")

facts_tbl = ddb.Table(FACTS_TABLE)

def _as_dec(n):
    try: return Decimal(str(n))
    except: return Decimal("0")

def _put_fact(text: str, tags: list[str], batch_id: str):
    fid = uuid.uuid4().hex[:16]
    item = {
        "pk": USER_ID,
        "sk": f"fact#{fid}",
        "text": text.strip(),
        "tags": tags,
        "dt": datetime.utcnow().date().isoformat(),
        "batch_id": batch_id,
        "schema_version": 1
    }
    facts_tbl.put_item(Item=item)
    return item

def lambda_handler(event, context):
    # S3:ObjectCreated event(s)
    total_rows = 0
    written = 0
    batch_id = uuid.uuid4().hex[:12]
    for rec in event.get("Records", []):
        bkt = rec["s3"]["bucket"]["name"]
        key = rec["s3"]["object"]["key"]
        obj = s3.get_object(Bucket=bkt, Key=key)
        body = obj["Body"].read()
        text = body.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            total_rows += 1
            fact = (row.get("fact") or "").strip()
            if not fact:
                continue
            tags = [t.strip().lower() for t in (row.get("tags") or "").split(",") if t.strip()]
            item = _put_fact(fact, tags, batch_id)
            written += 1
            # optionally enqueue for replay/refresh workflows
            if SQS_URL:
                sqs.send_message(
                    QueueUrl=SQS_URL,
                    MessageBody=json.dumps({"type":"fact.upsert","item":item})
                )
    log.info(f"Ingested {written}/{total_rows} facts (batch {batch_id})")
    return {"ok": True, "total": total_rows, "written": written, "batch_id": batch_id}
