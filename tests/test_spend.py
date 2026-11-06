"""Spend tracking: which bill each model is paid from, the shared ledger, the providers' own figures, the sidebar."""

import datetime as dt
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import spend
from hexiel.config import ModelConfig, Settings


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path))
    for env in spend.KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    spend._snap.update(at=0.0, key=None, rows=[])
    return tmp_path


def mc(provider="openai", model="m", base_url="", billing=""):
    return ModelConfig(name="p", provider=provider, model=model, base_url=base_url, billing=billing)


def test_each_model_is_billed_to_the_right_account():
    assert spend.account_for(mc("anthropic", "claude-sonnet-5-5")) == "anthropic"
    assert spend.account_for(mc(model="glm-5.3-flash:cloud", base_url="http://localhost:11434/v1")) == "ollama"
    assert spend.account_for(mc(model="gpt-oss:120b", base_url="https://ollama.com/v1")) == "ollama"
    assert spend.account_for(mc(model="gpt-5.1", base_url="https://api.openai.com/v1")) == "openai"
    assert spend.account_for(mc(model="qwen3.6:35b", base_url="http://192.168.3.10:11434/v1")) == ""   # your own box
    assert spend.account_for(mc(model="qwen2.5:3b", base_url="http://localhost:11434/v1")) == ""
    assert spend.account_for(mc(model="x", base_url="https://openrouter.ai/api/v1")) == "openrouter.ai"
    assert spend.account_for(mc(model="x", base_url="https://openrouter.ai/api/v1", billing="none")) == ""
    assert spend.account_for(mc(model="x", base_url="http://10.0.0.5/v1", billing="Work")) == "work"


def test_the_cycle_starts_on_the_plan_day():
    assert spend.cycle_start(23, dt.date(2026, 10, 30)) == dt.date(2026, 10, 23)
    assert spend.cycle_start(23, dt.date(2026, 10, 10)) == dt.date(2026, 9, 23)
    assert spend.cycle_start(1, dt.date(2026, 1, 5)) == dt.date(2026, 1, 1)
    assert spend.cycle_start(31, dt.date(2026, 3, 5)) == dt.date(2026, 2, 28)    # a short month ends the day early
    assert spend.cycle_start(30, dt.date(2026, 1, 2)) == dt.date(2025, 12, 30)


def test_claude_requests_are_estimated_from_their_tokens():
    today = dt.date.today()
    # 1M fresh input, 1M cache read, 1M output on Sonnet 5.5: $2 + $0.20 + $10
    spend.record("anthropic", "claude-sonnet-5-5",
                 {"prompt_tokens": 2_000_000, "cached_tokens": 1_000_000, "completion_tokens": 1_000_000})
    s = Settings(spend={"anthropic": {"limit": 20}})
    [row] = spend.totals(s, today)
    assert row["source"] == "estimate" and row["requests"] == 1
    assert row["usd"] == pytest.approx(12.20) and row["pct"] == 61


def test_openai_style_usage_is_read_too_and_unknown_prices_are_flagged():
    spend.record("openai", "gpt-5.1", {"prompt_tokens": 1000, "completion_tokens": 50,
                                       "prompt_tokens_details": {"cached_tokens": 400}})
    t = spend._read("spend.json")["days"][dt.date.today().isoformat()]["openai"]["gpt-5.1"]
    assert (t["input"], t["cached"], t["output"]) == (1000, 400, 50)
    [row] = spend.totals(Settings())
    assert row["unpriced"] == 1 and row["usd"] == 0
    priced = Settings(spend={"prices": {"gpt-5.1": {"input": 1.0, "output": 10.0, "cached": 0.1}}})
    [row] = spend.totals(priced)
    assert row["usd"] == pytest.approx((600 * 1.0 + 400 * 0.1 + 50 * 10.0) / 1e6) and row["unpriced"] == 0


def test_free_models_and_old_cycles_do_not_count():
    spend.record("", "qwen", {"prompt_tokens": 10, "completion_tokens": 10})
    spend.record("anthropic", "claude-haiku-5-5", {"prompt_tokens": 10, "completion_tokens": 10},
                 when=dt.date.today() - dt.timedelta(days=45))
    assert spend.totals(Settings()) == []


class Fake(BaseHTTPRequestHandler):
    seen: list = []
    allowance: float = 0.0         # what /api/balance reports this run: 0 = the endpoint says nothing

    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        Fake.seen.append((url.path, q, {k.lower(): v for k, v in self.headers.items()}))
        if url.path == "/api/usage":
            body = {"range": "30d", "buckets": [
                {"from": "2026-10-22T00:00:00Z", "usage_usd": 50.0},     # last cycle
                {"from": "2026-10-23T00:00:00Z", "usage_usd": 4.75},
                {"from": "2026-10-28T00:00:00Z", "usage_usd": 0.41}],
                "totals": {"groups": [{"model": "glm-5.3-flash", "usage_usd": 2.0,
                                       "input_tokens": 900_000, "output_tokens": 100_000}]}}
        elif url.path == "/api/balance":
            body = ({"balance_usd": 20.0, "allowance_usd": Fake.allowance, "period": "monthly",
                     "plan": "Max"} if Fake.allowance else {})
        elif url.path == "/v1/organizations/cost_report":          # amounts in cents, paginated
            body = ({"data": [{"results": [{"amount": "123.50", "currency": "USD"}]}], "has_more": True,
                     "next_page": "p2"} if "page" not in q else
                    {"data": [{"results": [{"amount": "76.50", "currency": "USD"}]}], "has_more": False})
        elif url.path == "/v1/organization/costs":                  # dollars, paginated
            body = ({"object": "page", "data": [{"object": "bucket", "results": [
                        {"object": "organization.costs.result", "amount": {"value": 1.25, "currency": "usd"}}]}],
                     "has_more": True, "next_page": "page_abc"} if "page" not in q else
                    {"object": "page", "data": [{"object": "bucket", "results": [
                        {"object": "organization.costs.result", "amount": {"value": 0.5, "currency": "usd"}}]}],
                     "has_more": False, "next_page": None})
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def fake(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    for name in ("OLLAMA_URL", "ANTHROPIC_URL", "OPENAI_URL"):
        monkeypatch.setattr(spend, name, base)
    Fake.seen = []
    Fake.allowance = 0.0
    yield Fake
    server.shutdown()


def test_the_providers_own_figures_win_over_estimates(fake, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "olk")
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin01-x")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-y")
    monkeypatch.setattr(spend, "cycle_start", lambda day, today=None: dt.date(2026, 10, 23) if day == 23
                        else dt.date(2026, 10, 1))
    s = Settings(spend={"ollama": {"limit": 60, "cycle_day": 23}, "anthropic": {"limit": 25},
                        "openai": {"limit": 10}})
    spend.record("anthropic", "claude-opus-5-5", {"prompt_tokens": 1000, "completion_tokens": 1000})
    spend.refresh(s, force=True)
    rows = {r["account"]: r for r in spend.totals(s, dt.date(2026, 10, 30))}
    assert rows["ollama"]["usd"] == pytest.approx(5.16) and rows["ollama"]["source"] == "provider"
    assert rows["ollama"]["pct"] == 8
    assert rows["anthropic"]["usd"] == pytest.approx(2.00) and rows["anthropic"]["source"] == "provider"
    assert rows["openai"]["usd"] == pytest.approx(1.75) and rows["openai"]["pct"] == 17
    calls = {path: (q, h) for path, q, h in fake.seen}
    q, h = calls["/v1/organizations/cost_report"]
    assert h["x-api-key"] == "sk-ant-admin01-x" and h["anthropic-version"] == "2023-06-01"
    q, h = calls["/v1/organization/costs"]
    assert h["authorization"] == "Bearer sk-admin-y" and q["bucket_width"] == "1d"
    assert int(q["start_time"]) == int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp())
    assert any(path == "/v1/organization/costs" and qq.get("page") == "page_abc" for path, qq, _ in fake.seen)
    # the rate Ollama charged is kept, so a request is still estimated if ollama.com can't be reached later
    rates = spend._read("spend-remote.json")["ollama"]["rates"]
    assert rates["glm-5.3-flash"] == pytest.approx(2.0)


def test_the_plan_allowance_is_picked_up_and_followed_when_it_changes(fake, monkeypatch):
    """The limit is not frozen at config.toml: the provider's plan allowance is checked on the same
    periodic refresh as the spend figure, so an upgrade (Pro → Max) shows up without edits."""
    monkeypatch.setenv("OLLAMA_API_KEY", "olk")
    monkeypatch.setattr(spend, "cycle_start", lambda day, today=None: dt.date(2026, 10, 23))
    monkeypatch.setattr(spend, "SNAPSHOT_SECONDS", 0)
    s = Settings(spend={"ollama": {"limit": 200, "cycle_day": 23}})      # config written under the old plan
    Fake.allowance = 60.0
    spend.refresh(s, force=True)                                   # the first balance read says Pro
    [row] = spend.totals(s)
    assert row["limit"] == 60 and row["limit_source"] == "plan" and row["plan_name"] == "Max"
    note = spend.alerts(s)
    assert len(note) == 1 and "$60.00" in note[0] and "(Max)" in note[0]
    assert spend.alerts(s) == []                                   # said once, not every turn
    Fake.allowance = 120.0
    spend.refresh(s, force=True)                                   # the plan upgraded → Max
    [row] = spend.totals(s)
    assert row["limit"] == 120 and row["limit_source"] == "plan"
    note = spend.alerts(s)
    assert len(note) == 1 and "$120.00" in note[0]
    assert spend.alerts(s) == []
    assert "(plan)" in spend.summary_lines(s)[0] and "$120.00 (plan) (4%)" in spend.summary_lines(s)[0]
    # a balance read that says nothing keeps the last known allowance
    Fake.allowance = 0.0
    spend.refresh(s, force=True)
    [row] = spend.totals(s)
    assert row["limit"] == 120 and row["limit_source"] == "plan"


def test_plan_allowances_are_told_apart_from_spend_and_remainder():
    assert spend._plan_allowance({"allowance_usd": 60}) == 60
    assert spend._plan_allowance({"data": {"allowance_cents": 20_000}}) == 200
    assert spend._plan_allowance({"monthly_limit_usd": 80}) == 80
    assert spend._plan_allowance({"monthly_usd": 45.5}) is None          # could be spend: not taken
    assert spend._plan_allowance({"remaining_percent": 45}) is None
    assert spend._plan_allowance({"limit": 5}) is None


def test_detect_limits_false_pins_the_configured_limit(fake, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "olk")
    monkeypatch.setattr(spend, "cycle_start", lambda day, today=None: dt.date(2026, 10, 23))
    s = Settings(spend={"ollama": {"limit": 30, "cycle_day": 23, "detect_limits": False}})
    Fake.allowance = 60.0
    spend.refresh(s, force=True)
    [row] = spend.totals(s)
    assert row["limit"] == 30 and row["limit_source"] == "config"


def test_the_sidebar_marks_a_plan_sourced_limit(fake, monkeypatch):
    from hexiel.tui import spend_rows
    monkeypatch.setattr(spend, "cycle_start", lambda day, today=None: dt.date(2026, 10, 23))
    _write_remote = spend._write
    _write_remote("spend-remote.json",
                  {"ollama": {"at": time.time(), "since": "2026-10-23", "usd": 42.0, "plan_usd": 60.0}})
    monkeypatch.setattr(spend, "SNAPSHOT_SECONDS", 0)
    rows = spend_rows(Settings(spend={"ollama": {"limit": 200}}), 27, {"ollama"})
    assert "$42.00 / $60.00 plan" in "\n".join(rows)


def test_a_provider_that_fails_falls_back_to_the_estimate(fake, monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "k")
    monkeypatch.setattr(spend, "OPENAI_URL", spend.OPENAI_URL + "/nope")
    spend.record("openai", "gpt-5.1", {"prompt_tokens": 10, "completion_tokens": 10})
    s = Settings(spend={"openai": {"limit": 5}})
    spend.refresh(s, force=True)
    [row] = spend.totals(s)
    assert row["source"] == "estimate" and "404" in row["error"]
    assert "couldn't ask the provider" in spend.summary_lines(s)[0]


def test_crossing_the_warning_and_the_limit_is_said_once_each(monkeypatch):
    s = Settings(spend={"anthropic": {"limit": 10}, "warn_at": 0.8})
    monkeypatch.setattr(spend, "SNAPSHOT_SECONDS", 0)
    spend.record("anthropic", "claude-sonnet-5-5", {"prompt_tokens": 4_000_000, "completion_tokens": 0})   # $8
    first = spend.alerts(s)
    assert len(first) == 1 and "80%" in first[0]
    assert spend.alerts(s) == []
    spend.record("anthropic", "claude-sonnet-5-5", {"prompt_tokens": 1_000_000, "completion_tokens": 0})   # $10
    assert "has used its $10.00" in spend.alerts(s)[0]
    assert spend.alerts(s) == []


def test_providers_meter_their_requests():
    from hexiel import llm
    p = llm.make_provider(mc(model="glm-5.3-flash:cloud", base_url="http://localhost:11434/v1"), Settings())
    assert p.billing == "ollama"
    p.meter("glm-5.3-flash:cloud", {"prompt_tokens": 5, "completion_tokens": 3})
    assert spend.totals(Settings())[0]["requests"] == 1
    assert llm.make_provider(mc(model="qwen", base_url="http://192.168.3.10:11434/v1"), Settings()).billing == ""


def test_the_sidebar_shows_each_account_against_its_limit():
    from hexiel.tui import spend_rows
    spend.record("anthropic", "claude-sonnet-5-5", {"prompt_tokens": 9_000_000, "completion_tokens": 0})   # $18
    rows = spend_rows(Settings(spend={"anthropic": {"limit": 20}}), 27, {"anthropic"})
    text = "\n".join(rows)
    assert "SPEND THIS CYCLE" in text and "≈$18.00 / $20.00" in text and " 90%" in text
    assert "≈$18.00" in "\n".join(spend_rows(Settings(), 27, {"anthropic"}))    # no limit set: still shown, no meter


def test_the_sidebar_shows_only_the_bill_of_the_model_at_work():
    from hexiel.session import Session
    from hexiel.tui import spend_rows
    settings = Settings(spend={"ollama": {"limit": 60}, "anthropic": {"limit": 20}})
    spend.record("ollama", "glm-5.3-flash:cloud", {"prompt_tokens": 10, "completion_tokens": 10})
    spend.record("anthropic", "claude-sonnet-5-5", {"prompt_tokens": 10, "completion_tokens": 10})
    local = mc(model="qwen3.6:35b", base_url="http://192.168.3.10:11434/v1")
    cloud = mc(model="glm-5.3-flash:cloud", base_url="http://localhost:11434/v1")
    claude = mc("anthropic", "claude-sonnet-5-5")
    s = Session(local, settings, "/tmp", [], "sys")
    assert s.accounts_in_use() == set() and spend_rows(settings, 27, s.accounts_in_use()) == []    # local: nothing
    s.set_active_model(cloud, "step 1/3")                     # a step runs on Ollama Cloud
    shown = "\n".join(spend_rows(settings, 27, s.accounts_in_use()))
    assert "ollama" in shown and "anthropic" not in shown
    s.active_model = ""                                       # the step finished
    assert s.accounts_in_use() == set()
    s.model_config = claude                                   # the chat itself runs on Claude
    shown = "\n".join(spend_rows(settings, 27, s.accounts_in_use()))
    assert "anthropic" in shown and "ollama" not in shown
    s.set_active_model(cloud, "summarizing")                  # both at once: both bills
    assert s.accounts_in_use() == {"anthropic", "ollama"}


def test_spend_set_corrects_the_estimate_to_the_billing_page_and_requests_add_on_top():
    s = Settings(spend={"anthropic": {"limit": 90}})
    spend.record("anthropic", "claude-sonnet-5-5", {"prompt_tokens": 1_000_000, "completion_tokens": 0})    # $2
    assert "set to $78.31" in spend.command(s, "set anthropic $78.31") and "counted $2.00" in spend.command(s, "set anthropic 78.31")
    [row] = spend.totals(s)
    assert row["usd"] == pytest.approx(78.31) and row["pct"] == 87
    spend.record("anthropic", "claude-sonnet-5-5", {"prompt_tokens": 500_000, "completion_tokens": 0})      # +$1
    [row] = spend.totals(s)
    assert row["usd"] == pytest.approx(79.31)
    assert "anthropic: $79.31 of $90.00 (88%)" in spend.command(s, "")
    # the correction belongs to this cycle only
    nxt = spend.cycle_start(1) + dt.timedelta(days=40)
    assert all(r["usd"] == 0 for r in spend.totals(s, nxt.replace(day=2)))


def test_spend_set_explains_its_mistakes():
    s = Settings()
    assert spend.command(s, "set anthropic").startswith("usage:")
    assert spend.command(s, "set anthropic lots") == "not an amount: lots"
    assert spend.command(s, "") == "no paid model has been used this cycle"
