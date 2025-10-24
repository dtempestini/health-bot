from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


class FakeFactsTable:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.config_item = None

    def query(self, **kwargs):
        return {"Items": list(self.items)}

    def get_item(self, **kwargs):
        if self.config_item:
            return {"Item": dict(self.config_item)}
        return {}

    def put_item(self, Item):
        if Item.get("sk") == "config#facts":
            self.config_item = dict(Item)
        else:
            self.items.append(dict(Item))
        return {}

    def update_item(self, **kwargs):
        return {}


def test_handle_food_set_preserves_multiword_alias(meal_enricher, monkeypatch):
    captured = {}

    def fake_put_override(alias, macros, note=""):
        captured["alias"] = alias
        captured["macros"] = macros

    sent_messages: list[str] = []
    monkeypatch.setattr(meal_enricher, "_put_override", fake_put_override)
    monkeypatch.setattr(meal_enricher, "_send_sms", lambda sender, body: sent_messages.append(body))

    meal_enricher._handle_food("+15555551212", "set canned tuna k=120 p=26 c=0 f=1")

    assert captured["alias"] == "canned tuna"
    assert captured["macros"] == {"calories": 120, "protein": 26, "carbs": 0, "fat": 1}
    assert any("Saved custom food: canned tuna" in msg for msg in sent_messages)


def test_handle_fact_sends_random_fact(meal_enricher, monkeypatch):
    fake_table = FakeFactsTable(
        [{"pk": "unit-test-user", "sk": "fact#1", "text": "Stay hydrated to reduce migraine risk.", "tags": ["hydration"]}]
    )
    monkeypatch.setattr(meal_enricher, "facts_tbl", fake_table)
    monkeypatch.setattr(meal_enricher.random, "choice", lambda seq: seq[0])

    messages: list[str] = []
    monkeypatch.setattr(meal_enricher, "_send_sms", lambda sender, body: messages.append(body))

    meal_enricher._handle_fact("whatsapp:+15551234567", "", simulate=False)

    assert messages and messages[-1].startswith("🧠 Migraine fact:")
    assert "Stay hydrated" in messages[-1]


def test_handle_facts_on_sets_config(meal_enricher, monkeypatch):
    fake_table = FakeFactsTable()
    monkeypatch.setattr(meal_enricher, "facts_tbl", fake_table)

    messages: list[str] = []
    monkeypatch.setattr(meal_enricher, "_send_sms", lambda sender, body: messages.append(body))

    meal_enricher._handle_facts("whatsapp:+15551234567", "on 8", simulate=False)

    cfg = fake_table.config_item
    assert cfg["daily_enabled"] is True
    assert cfg["daily_hour"] == 8
    assert cfg["to_number"] == "whatsapp:+15551234567"
    assert any("enabled" in msg.lower() for msg in messages)


def test_parse_when_to_ms_iso_datetime(meal_enricher):
    target = datetime(2024, 2, 20, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    tokens = ["2024-02-20", "08:30"]
    ts_ms = meal_enricher._parse_when_to_ms(tokens)
    assert ts_ms == int(target.timestamp() * 1000)
