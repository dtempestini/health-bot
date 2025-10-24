from __future__ import annotations

import sys
import uuid
from importlib import util
from pathlib import Path

import boto3
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


class _StubSecretsClient:
    def __init__(self):
        self._data: dict[str, str] = {}

    def add_secret(self, secret_id: str, secret_string: str) -> None:
        self._data[secret_id] = secret_string

    def get_secret_value(self, SecretId: str):
        return {"SecretString": self._data.get(SecretId, "{}")}


class _StubDynamoTable:
    def put_item(self, **kwargs):
        return {}

    def update_item(self, **kwargs):
        return {}

    def get_item(self, **kwargs):
        return {}

    def query(self, **kwargs):
        return {"Items": []}

    def delete_item(self, **kwargs):
        return {}


class _StubDynamoResource:
    def Table(self, name: str):
        return _StubDynamoTable()


@pytest.fixture
def meal_enricher(monkeypatch):
    """Load the meal_enricher module with AWS clients stubbed."""

    env = {
        "MEALS_TABLE": "meals_test",
        "TOTALS_TABLE": "totals_test",
        "EVENTS_TABLE": "events_test",
        "MIGRAINES_TABLE": "migraines_test",
        "MEDS_TABLE": "meds_test",
        "FASTING_TABLE": "fasting_test",
        "FACTS_TABLE": "facts_test",
        "FOOD_OVERRIDES_TABLE": "overrides_test",
        "NUTRITION_SECRET_NAME": "nutrition_secret_test",
        "TWILIO_SECRET_NAME": "twilio_secret_test",
        "USER_ID": "unit-test-user",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    secrets_stub = _StubSecretsClient()
    monkeypatch.setattr(boto3, "client", lambda service: secrets_stub if service == "secretsmanager" else object())
    monkeypatch.setattr(boto3, "resource", lambda service: _StubDynamoResource() if service == "dynamodb" else object())

    module_name = f"meal_enricher_{uuid.uuid4().hex}"
    module_path = REPO_ROOT / "infra/envs/dev/lambda/meal_enricher.py"
    spec = util.spec_from_file_location(module_name, module_path)
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    yield module

    sys.modules.pop(module_name, None)
