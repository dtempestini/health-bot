# Health Bot (Dev Stack)

Automation for logging meals, tracking migraines/medications/fasting, and sending daily stats over WhatsApp.  
This repository contains both the Terraform that provisions the AWS infrastructure and the Python Lambda
code that powers the texting workflows.

---

## High-Level Architecture
- **Twilio Webhooks → AWS Lambda (`ingest.py`)**  
  Incoming WhatsApp/SMS messages are classified and stored in `hb_events_dev`, then published to an SNS topic.

- **Meal Enrichment Pipeline (`meal_enricher.py`)**  
  SNS triggers the meal enricher Lambda. It fetches nutrition data (Nutritionix), applies custom food overrides,
  updates DynamoDB tables for meals/totals/migraines/medications/fasting, and replies to the user via Twilio.

- **Stats API (`stats_api.py`)**  
  Lightweight HTTPS API (API Gateway + Lambda) that aggregates daily/weekly/monthly macros, medications, and
  migraines for dashboards or ad‑hoc queries.

- **Migraine Facts (in progress)**  
  CSV files dropped into S3 are ingested into `hb_migraine_facts_dev`. An hourly Lambda picks a fact and sends it
  over WhatsApp when the daily schedule is met. On-demand requests and user commands still need to be wired in.

- **Terraform**  
  Live in `infra/envs/dev`. Resources include DynamoDB tables, S3 buckets, SNS/SQS, Lambda packaging, EventBridge
  schedulers, and CodeBuild/CodePipeline configuration for plan/apply stages.

---

## Repository Layout

```
infra/envs/dev/
├── *.tf                  # Terraform modules for ingest, enrichment, analytics, stats, facts, CICD
├── buildspecs/           # CodeBuild specs for terraform plan/apply (runs pytest + packaging)
└── lambda/               # Lambda sources zipped automatically by the buildspecs
      ingest.py
      meal_enricher.py
      stats_api.py
      facts_ingest.py
      facts_sender.py
tests/                    # Pytest suite covering key meal enrichment helpers
requirements-dev.txt      # Local + pipeline dev dependencies (pytest, etc.)
```

---

## Primary Workflows

### Meal Logging & Nutrition
1. User texts `meal: grilled chicken` (or commands like `/summary`, `/med ...`).
2. `ingest.py` persists the raw event and fans out via SNS.
3. `meal_enricher.py`:
   - Matches custom foods or queries Nutritionix.
   - Writes enriched rows to `hb_meals_dev` and updates `hb_daily_totals_dev`.
   - Keeps track of migraine episodes, medication usage (with safety limits), fasting sessions, and sends Twilio replies.
4. Daily/weekly/monthly stats are available via commands or `stats_api`.

### Migraine & Medication Tracking
- `/migraine start/end` maintains episodes in `hb_migraines_dev`.
- `/med ...` logs medication doses into `hb_meds_dev`, checks for 24‑hour conflicts, and warns as monthly limits approach.
- `/meds`, `/meds today`, `/meds month` summarize medication usage.

### Intermittent Fasting
- `/fast start/end/status/goal` commands manage fasting sessions stored in `hb_fasting_dev` (provisioned in Terraform).

### Migraine Facts (CSV Dropbox → WhatsApp)
Current implementation includes:
1. **S3 Ingest (`facts_ingest.py`)**
   - Triggered on CSV uploads (prefix `migraine/`).
   - Expected columns: `fact`, optional `tags`.
   - Inserts rows into DynamoDB and optionally pushes to SQS for downstream processing.
2. **Facts Sender (`facts_sender.py`)**
   - Invoked hourly via EventBridge. When daily conditions are met, picks a random fact and sends it via Twilio,
     tracking the last sent day in DynamoDB. Also consumes optional SQS messages.
3. **User Commands (via `meal_enricher.py`)**
   - `/fact [tag]` sends a random fact immediately (filtered by tag when provided).
   - `/facts on|off|hour|number|status` manages the daily delivery schedule, including target WhatsApp number.

---

## Development & Testing

### Prerequisites
- Python 3.12
- Terraform 1.13.x
- AWS CLI credentials for the target account

### Run Unit Tests
```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```
The CodeBuild specs run the same commands during `plan` and `apply`, failing the pipeline if tests fail.

### Packaging Lambdas
Buildspecs in `infra/envs/dev/buildspecs/` handle packaging automatically (zip Lambdas, build the `requests` Lambda layer).
When iterating locally you can mimic the steps:
```bash
cd infra/envs/dev
zip -j lambda_meal_enricher.zip lambda/meal_enricher.py
zip -j lambda_ingest.zip lambda/ingest.py
zip -j lambda_stats_api.zip lambda/stats_api.py
zip -j lambda_facts_ingest.zip lambda/facts_ingest.py
zip -j lambda_facts_sender.zip lambda/facts_sender.py
```

### Terraform
From `infra/envs/dev`:
```bash
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```
The CodePipeline defined in `cicd.tf` uses the buildspecs to run these steps in AWS with plan/storage artifacts.

---

## Command Reference (User-Facing)
- `meal: <description>` – log a meal (automatic nutrition lookup + overrides)
- `/summary`, `/week`, `/month` – caloric/macros summaries
- `/lookup: <description>` – preview meal impact without saving
- `/barcode <upc>` – Nutritionix barcode lookup with custom food helper
- `/food set|del|list` – manage stored overrides
- `/undo`, `/reset today` – meal corrections
- `/migraine start|end`, `/med`, `/meds`, `/meds today`, `/meds month`
- `/fact [tag]`, `/facts on|off|hour|number|status`
- `/fast start|end|status|goal`
- `/test <command…>` – dry-run any command without persistence

---

## Outstanding TODOs
- Expand test coverage for `facts_ingest.py` / `facts_sender.py` and DynamoDB edge cases.
- Backfill integration tests for fasting and medication alert paths.
- Document production/staging environment differences once additional workspaces are added.

---

## Support
Questions or ideas? Reach out to the repo owner or drop notes in the project chat. Contributions should include unit
tests (`pytest`) and pass Terraform validation before raising a PR.
