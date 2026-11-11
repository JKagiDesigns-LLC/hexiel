"""What the paid model providers cost this billing cycle, and how close that is to the limit you set.

Every request any hexiel process makes is metered where it is sent (llm.py): its tokens go into a small ledger
shared by every process on the machine (spend.json, one bucket per day, per account, per model). An account is the
bill a model is paid from — "anthropic", "openai", "ollama" (Ollama Cloud), or the host of another paid endpoint;
models on your own machines are free and not metered.

The figure shown for an account comes from the provider when it says what you spent, and is estimated from the
ledger otherwise:
  ollama     ollama.com/api/usage with your Ollama API key (OLLAMA_API_KEY) — the dollars Ollama charged — and
             ollama.com/api/balance: the plan's monthly allowance, the dates of the billing period, what is left of
             the allowance and what is left of the credits you bought on top. The sidebar follows all of it: the
             period is the plan's own (not a guessed day of the month), the budget is the allowance plus any
             credits you added, and "left" is the provider's own remainder.
  anthropic  the Admin API cost report, with an Admin key (ANTHROPIC_ADMIN_KEY; organisations only)
  openai     the organisation costs API, with an Admin key (OPENAI_ADMIN_KEY)
  estimate   tokens × price: built-in Claude prices, the per-token rate Ollama last reported, or [spend.prices]

Besides the cycle figure, every account shows what today cost and the pace of the last week (dollars a day), so a
runaway afternoon is visible before the month is gone. Ollama Cloud's paid plans have no daily cap — only the
monthly credits — so "today" is informational, not a second limit.

Configured in config.toml:

    [spend.ollama]
    limit = 60          # dollars a cycle; the sidebar warns at warn_at (default 80 %) and at 100 %
    cycle_day = 23      # the day of the month the plan renews (1 = calendar months, the default)
    detect_limits = false  # optional: pin the limit and cycle_day above instead of following the plan
                        # (the plan's allowance, period and remaining credits are read on the same
                        # periodic refresh as the spend figure — Ollama Cloud: /api/balance — so an
                        # upgrade or a credit top-up shows up without touching this file)
    [spend.anthropic]
    limit = 25
    [spend.prices]      # dollars per million tokens, for models hexiel has no price for
    "gpt-5.1" = { input = 1.25, output = 10, cached = 0.125 }
"""

from __future__ import annotations

import contextlib
import datetime as dt
import ipaddress
import json
import os
import threading
import time
from urllib.parse import urlparse

from .config import home_data_dir

OLLAMA_URL = "https://ollama.com"
ANTHROPIC_URL = "https://api.anthropic.com"
OPENAI_URL = "https://api.openai.com"
KEY_ENV = {"ollama": "OLLAMA_API_KEY", "anthropic": "ANTHROPIC_ADMIN_KEY", "openai": "OPENAI_ADMIN_KEY"}
KEEP_DAYS = 70                     # ledger days kept: enough for the longest cycle plus the one before
REFRESH_MINUTES = 5                # how often a provider is asked (all processes share the answer)
SNAPSHOT_SECONDS = 15              # the sidebar redraws every second; the figures are recomputed this often

# dollars per million tokens: (input, output, cache read); a cache write costs 1.25 × input (5-minute cache)
CLAUDE_PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25), "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50), "claude-opus-4-7": (5.0, 25.0, 0.50), "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20), "claude-sonnet-5": (2.0, 10.0, 0.20), "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1.0, 5.0, 0.10),
}

ADJUST = "__adjust__"              # a ledger entry that is not a model: a correction made with /spend set (set_spent)

_thread_lock = threading.Lock()
_refreshing: set = set()
_snap: dict = {"at": 0.0, "key": None, "rows": []}


# ---------------------------------------------------------------- which bill a model is paid from

def _local_host(host: str) -> bool:
    if host in ("localhost", "") or host.endswith(".local") or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def account_for(mc) -> str:
    """The account a model profile is billed to, or "" when it is free (a model on your own machines)."""
    billing = str(getattr(mc, "billing", "") or "").strip().lower()
    if billing:
        return "" if billing in ("none", "free", "local") else billing
    model = str(getattr(mc, "model", "")).lower()
    host = (urlparse(getattr(mc, "base_url", "") or "").hostname or "").lower()
    if getattr(mc, "provider", "") == "anthropic":
        return "anthropic"
    if host.endswith("ollama.com") or model.endswith(":cloud") or model.endswith("-cloud"):
        return "ollama"                     # a local Ollama runs ":cloud" models on Ollama's servers, on your plan
    if host.endswith("openai.com"):
        return "openai"
    if _local_host(host):
        return ""
    return host                            # another paid endpoint (OpenRouter, Together, …): priced from [spend.prices]


# ---------------------------------------------------------------- the shared ledger

def _path(name: str):
    return home_data_dir() / name


@contextlib.contextmanager
def _file_lock():
    """Every hexiel process writes the same ledger: take turns (blocking, held for a few milliseconds)."""
    path = _path("spend.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, open(path, "a+") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            pass                           # no lock is better than no meter
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass


def _read(name: str) -> dict:
    try:
        data = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(name: str, data: dict) -> None:
    path = _path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass


def _tokens(usage: dict) -> dict:
    """Anthropic and OpenAI-style usage, as input (all of it), cached, cache-write and output tokens."""
    details = usage.get("prompt_tokens_details") or {}
    cached = int(usage.get("cached_tokens") or details.get("cached_tokens") or 0)
    return {"input": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0), "cached": cached,
            "cache_write": int(usage.get("cache_write_tokens") or 0),
            "output": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)}


def record(account: str, model: str, usage: dict | None, when: dt.date | None = None) -> None:
    """Add one finished request to today's ledger. Never raises: a broken meter must not break a reply."""
    if not account or not usage:
        return
    try:
        t = _tokens(usage)
        if not (t["input"] or t["output"]):
            return
        day = (when or dt.date.today()).isoformat()
        with _file_lock():
            data = _read("spend.json")
            days = data.setdefault("days", {})
            row = days.setdefault(day, {}).setdefault(account, {}).setdefault(
                model, {"requests": 0, "input": 0, "cached": 0, "cache_write": 0, "output": 0})
            row["requests"] += 1
            for k, v in t.items():
                row[k] = row.get(k, 0) + v
            cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
            for old in [d for d in days if d < cutoff]:
                del days[old]
            _write("spend.json", data)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- settings

def accounts(settings) -> dict:
    """[spend.NAME] tables: {name: {"limit": dollars or 0, "cycle_day": 1-28.., "key_env": str}}."""
    raw = getattr(settings, "spend", None) or {}
    out = {}
    for name, body in raw.items():
        if name in ("prices", "warn_at") or not isinstance(body, dict):
            continue
        try:
            limit = max(0.0, float(body.get("limit", 0) or 0))
            day = min(31, max(1, int(body.get("cycle_day", 1) or 1)))
        except (TypeError, ValueError):
            limit, day = 0.0, 1
        try:
            stop_at = min(10.0, max(0.0, float(body.get("stop_at", 0) or 0)))
        except (TypeError, ValueError):
            stop_at = 0.0
        out[str(name).lower()] = {"limit": limit, "cycle_day": day,
                                  "key_env": str(body.get("key_env", KEY_ENV.get(str(name).lower(), ""))),
                                  "detect_limits": bool(body.get("detect_limits", True)), "stop_at": stop_at}
    return out


def over_budget(settings, account: str) -> str:
    """The hard stop: with `stop_at = 1.0` (a share of the budget) under [spend.NAME], a request on that
    account is refused once spend reaches it — the reason, or "" when the request may go."""
    conf = accounts(settings).get(account or "")
    if not conf or not conf.get("stop_at"):
        return ""
    row = next((r for r in snapshot(settings) if r["account"] == account), None)
    if not row or not row["limit"]:
        return ""
    share = row["usd"] / row["limit"]
    if share < conf["stop_at"]:
        return ""
    return (f"{account} is over its budget: {money(row['usd'])} of {money(row['limit'])} ({int(100 * share)}%) this cycle, "
            f"stop_at = {conf['stop_at']:g} under [spend.{account}] — raise it, add credits, or wait for "
            + (f"{_day(row['renews'])}" if row.get("renews") else "the next cycle"))


def warn_at(settings) -> float:
    try:
        v = float((getattr(settings, "spend", None) or {}).get("warn_at", 0.8))
    except (TypeError, ValueError):
        v = 0.8
    return min(1.0, max(0.1, v))


def cycle_start(cycle_day: int, today: dt.date | None = None) -> dt.date:
    """The day the current billing cycle began: this month's cycle day, or last month's if it is still ahead."""
    today = today or dt.date.today()

    def on(year: int, month: int) -> dt.date:
        last = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
        return dt.date(year, month, min(cycle_day, last))
    this = on(today.year, today.month)
    if this <= today:
        return this
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return on(year, month)


def cycle_end(cycle_day: int, since: dt.date) -> dt.date:
    """The day the cycle that began on `since` renews: the cycle day of the following month."""
    return cycle_start(cycle_day, since + dt.timedelta(days=32))


def _date(value) -> dt.date | None:
    """'2026-11-02T20:11:03Z' → date(2026, 11, 2); None when it is not a timestamp."""
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _price(model: str, settings, rates: dict) -> tuple | None:
    """(input, output, cache read, cache write) dollars per million tokens, or None when unknown."""
    prices = (getattr(settings, "spend", None) or {}).get("prices") or {}
    p = prices.get(model)
    if isinstance(p, dict):
        try:
            inp, out = float(p.get("input", 0)), float(p.get("output", 0))
            return inp, out, float(p.get("cached", inp)), float(p.get("cache_write", inp))
        except (TypeError, ValueError):
            return None
    base = next((CLAUDE_PRICES[k] for k in sorted(CLAUDE_PRICES, key=len, reverse=True) if model.startswith(k)), None)
    if base:
        return base[0], base[1], base[2], base[0] * 1.25
    rate = rates.get(_ollama_name(model))   # what Ollama last charged per token for this model
    if rate:
        return rate, rate, rate, rate
    return None


def _ollama_name(model: str) -> str:
    return model.removesuffix(":cloud").removesuffix("-cloud")


# ---------------------------------------------------------------- the providers' own figures

def _get(url: str, headers: dict, params) -> dict:
    import httpx
    resp = httpx.get(url, headers=headers, params=params, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
    return resp.json()


def fetch_ollama(key: str, since: dt.date) -> tuple[float, dict, dict]:
    """Dollars Ollama charged since `since` (its API reports at most 30 days, by day), per-token rates,
    and the dollars of each day of the cycle ({"2026-11-11": 5.22, …} — the sidebar's "today" and pace)."""
    data = _get(f"{OLLAMA_URL}/api/usage", {"authorization": f"Bearer {key}"}, {"range": "30d", "group_by": "model"})
    total, days = 0.0, {}
    for bucket in data.get("buckets") or []:
        day = str(bucket.get("from", ""))[:10]
        if day >= since.isoformat():
            usd = float(bucket.get("usage_usd") or 0)
            total += usd
            days[day] = days.get(day, 0.0) + usd
    rates = {}
    for g in (data.get("totals") or {}).get("groups") or []:
        tokens = (g.get("input_tokens") or 0) + (g.get("output_tokens") or 0)
        if tokens and g.get("model"):
            rates[str(g["model"])] = 1e6 * float(g.get("usage_usd") or 0) / tokens   # blended $ per million tokens
    return total, rates, days


def _plan_allowance(data, _depth: int = 4) -> float | None:
    """The plan's own dollar allowance from an ollama.com balance reply — the figure that changes
    when you switch plans (Pro → Max). Only unambiguous cap-shaped keys are taken: an amount for
    what you already spent or have left must never be mistaken for the budget."""
    if _depth <= 0 or not isinstance(data, dict):
        return None
    for key, value in data.items():
        lower = str(key).lower()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            says_left = any(w in lower for w in ("remaining", "percent", "used", "left", "balance"))
            says_cap = ("allowance" in lower or "quota" in lower
                        or ("limit" in lower and "monthly" in lower))
            if says_cap and not says_left:
                dollars = value / (100.0 if "cents" in lower else 1.0)
                if 1.0 <= dollars <= 1_000_000.0:
                    return dollars
    for value in data.values():
        if isinstance(value, dict):
            found = _plan_allowance(value, _depth - 1)
            if found is not None:
                return found
    return None


BALANCE_KEYS = ("plan_usd", "plan_name", "period_from", "period_until", "included_left", "purchased_left")


def fetch_ollama_balance(key: str) -> dict:
    """What ollama.com/api/balance says about the plan, as the keys it actually answers (BALANCE_KEYS):

        {"included": {"balance_usd": 0, "allowance_usd": 300,
                      "period": {"from": "2026-11-02T20:11:03Z", "until": "2026-12-02T20:11:03Z"}},
         "purchased": {"balance_usd": 1.78}}

    → plan_usd 300 (the monthly allowance), period_from/period_until (the plan's own billing period),
    included_left 0 (what is left of the allowance) and purchased_left 1.78 (credits bought on top).
    A reply of another shape still yields the allowance when a cap-shaped key is present. Raised when
    the reply says nothing usable, so the caller keeps what it had."""
    data = _get(f"{OLLAMA_URL}/api/balance", {"authorization": f"Bearer {key}"}, None)
    if not isinstance(data, dict):
        raise RuntimeError("the balance reply is not an object")
    included = data.get("included") if isinstance(data.get("included"), dict) else {}
    purchased = data.get("purchased") if isinstance(data.get("purchased"), dict) else {}
    out: dict = {}
    allowance = _num(included.get("allowance_usd"))
    if allowance is None or allowance <= 0:
        allowance = _plan_allowance(data)
    if allowance:
        out["plan_usd"] = allowance
    if _num(included.get("balance_usd")) is not None:
        out["included_left"] = max(0.0, _num(included["balance_usd"]))
    if _num(purchased.get("balance_usd")) is not None:
        out["purchased_left"] = max(0.0, _num(purchased["balance_usd"]))
    period = included.get("period") if isinstance(included.get("period"), dict) else {}
    start, end = _date(period.get("from")), _date(period.get("until"))
    if start and end and start < end:
        out["period_from"], out["period_until"] = start.isoformat(), end.isoformat()
    for candidate in ("plan", "tier", "name", "plan_name"):
        value = data.get(candidate) or next(
            (v.get(candidate) for v in data.values() if isinstance(v, dict) and v.get(candidate)), "")
        if isinstance(value, str) and value.strip():
            out["plan_name"] = value.strip()
            break
    if not out:
        raise RuntimeError("no plan allowance in the balance reply")
    return out


def fetch_anthropic(key: str, since: dt.date) -> float:
    """Dollars on the Anthropic organisation's bill since `since` (Admin API cost report; amounts are in cents)."""
    total, page = 0.0, None
    for _ in range(20):
        params = {"starting_at": f"{since.isoformat()}T00:00:00Z", "limit": 31}
        if page:
            params["page"] = page
        data = _get(f"{ANTHROPIC_URL}/v1/organizations/cost_report",
                    {"x-api-key": key, "anthropic-version": "2023-06-01"}, params)
        for bucket in data.get("data") or []:
            for r in bucket.get("results") or []:
                total += float(r.get("amount") or 0) / 100
        page = data.get("next_page")
        if not data.get("has_more") or not page:
            break
    return total


def fetch_openai(key: str, since: dt.date) -> float:
    """Dollars on the OpenAI organisation's bill since `since` (organization costs API, Admin key)."""
    start = int(dt.datetime.combine(since, dt.time(), tzinfo=dt.timezone.utc).timestamp())
    total, page = 0.0, None
    for _ in range(20):
        params = {"start_time": start, "bucket_width": "1d", "limit": 31}
        if page:
            params["page"] = page
        data = _get(f"{OPENAI_URL}/v1/organization/costs", {"authorization": f"Bearer {key}"}, params)
        for bucket in data.get("data") or []:
            for r in bucket.get("results") or []:
                amount = r.get("amount") or {}
                total += float(amount.get("value") or 0) if isinstance(amount, dict) else float(amount or 0)
        page = data.get("next_page")
        if not data.get("has_more") or not page:
            break
    return total


def refresh(settings, force: bool = False) -> None:
    """Ask each provider that can say what you spent (at most every REFRESH_MINUTES across all processes)."""
    accts = accounts(settings)
    for name in ("ollama", "anthropic", "openai"):
        conf = accts.get(name) or {"cycle_day": 1, "key_env": KEY_ENV[name]}
        key = os.environ.get(conf["key_env"] or KEY_ENV[name], "")
        if not key:
            continue
        follow = conf.get("detect_limits", True)
        since = cycle_start(conf["cycle_day"])
        with _thread_lock:
            cached = _read("spend-remote.json").get(name) or {}
            same_cycle = cached.get("since") == since.isoformat() or bool(follow and cached.get("period_from"))
            fresh = same_cycle and time.time() - float(cached.get("at", 0)) < REFRESH_MINUTES * 60
            if (fresh and not force) or name in _refreshing:
                continue
            _refreshing.add(name)
        try:
            entry = {"since": since.isoformat(), "at": time.time()}
            kept = {k: cached[k] for k in BALANCE_KEYS if k in cached}      # the last balance reply, if any
            try:
                if name == "ollama":
                    try:
                        balance = fetch_ollama_balance(key)
                    except Exception:                    # noqa: BLE001 - the balance is best effort
                        balance = kept
                    entry.update(balance)
                    if follow and balance.get("period_from"):
                        since = dt.date.fromisoformat(balance["period_from"])   # the plan's own period
                        entry["since"] = since.isoformat()
                    entry["usd"], entry["rates"], entry["days"] = fetch_ollama(key, since)
                elif name == "anthropic":
                    entry["usd"] = fetch_anthropic(key, since)
                else:
                    entry["usd"] = fetch_openai(key, since)
            except Exception as exc:  # noqa: BLE001 - shown as "estimate", with the reason in /usage
                entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
                if cached.get("rates"):
                    entry["rates"] = cached["rates"]
                for k, v in kept.items():
                    entry.setdefault(k, v)
            with _file_lock():
                data = _read("spend-remote.json")
                data[name] = entry
                _write("spend-remote.json", data)
        finally:
            with _thread_lock:
                _refreshing.discard(name)


def refresh_async(settings) -> None:
    threading.Thread(target=refresh, args=(settings,), daemon=True, name="hexiel-spend").start()


# ---------------------------------------------------------------- what to show

def totals(settings, today: dt.date | None = None) -> list[dict]:
    """One row per account worth showing: configured, or used this cycle. Sorted with the configured ones first."""
    today = today or dt.date.today()
    accts = accounts(settings)
    ledger = _read("spend.json").get("days", {})
    remote = _read("spend-remote.json")
    names = list(accts)
    for day in ledger.values():
        names += [a for a in day if a not in names]
    rows = []
    for name in names:
        conf = accts.get(name) or {"limit": 0.0, "cycle_day": 1, "detect_limits": True}
        reported = remote.get(name) or {}
        follow = conf.get("detect_limits", True)
        # the billing period: the plan's own when the provider says (Ollama Cloud: /api/balance), else the
        # configured day of the month
        period = _date(reported.get("period_from")) if follow else None
        if period and period <= today:
            since, renews = period, _date(reported.get("period_until")) or cycle_end(conf["cycle_day"], period)
        else:
            since = cycle_start(conf["cycle_day"], today)
            renews = cycle_end(conf["cycle_day"], since)
        rates = reported.get("rates") or {}
        requests, usd, unpriced, days = 0, 0.0, 0, {}
        for day, per_account in ledger.items():
            if day < since.isoformat():
                continue
            for model, t in (per_account.get(name) or {}).items():
                if model == ADJUST:
                    usd += float(t.get("usd", 0))
                    continue
                requests += t.get("requests", 0)
                p = _price(model, settings, rates)
                if p is None:
                    unpriced += t.get("requests", 0)
                    continue
                fresh = max(0, t.get("input", 0) - t.get("cached", 0) - t.get("cache_write", 0))
                cost = (fresh * p[0] + t.get("output", 0) * p[1] + t.get("cached", 0) * p[2]
                        + t.get("cache_write", 0) * p[3]) / 1e6
                usd += cost
                days[day] = days.get(day, 0.0) + cost
        source = "estimate"
        if reported.get("since") == since.isoformat() and "usd" in reported:
            usd, source = float(reported["usd"]), "provider"
            if isinstance(reported.get("days"), dict):
                days = {d: float(v) for d, v in reported["days"].items()}
        if name not in accts and not requests and source == "estimate":
            continue                       # neither configured nor used this cycle
        # The budget: what the provider says is left (Ollama: what remains of the allowance plus the credits
        # bought on top) added to what was spent — so a top-up grows the bar instead of overflowing it; else
        # the plan's allowance, which is what moves when you upgrade (Pro → Max); else config.toml's limit.
        # detect_limits = false pins the limit to what you wrote.
        plan_usd = float(reported.get("plan_usd") or 0) if follow else 0.0
        included_left, purchased_left = reported.get("included_left"), reported.get("purchased_left")
        left = None
        if follow and (included_left is not None or purchased_left is not None):
            left = float(included_left or 0) + float(purchased_left or 0)
        limit, limit_source = conf["limit"], "config"
        if left is not None:
            limit, limit_source = usd + left, "plan"
        elif plan_usd:
            limit, limit_source = plan_usd, "plan"
        if left is None and limit:
            left = max(0.0, limit - usd)
        # today, and the pace of the last week (days of the cycle before today), for "at this rate…"
        today_usd = days.get(today.isoformat(), 0.0)
        window = [today - dt.timedelta(days=n) for n in range(1, 8)]
        window = [d for d in window if d >= since]
        per_day = (sum(days.get(d.isoformat(), 0.0) for d in window) / len(window)) if window else today_usd
        rows.append({"account": name, "usd": usd, "limit": limit, "limit_source": limit_source,
                     "plan_usd": plan_usd, "left": left, "purchased_left": float(purchased_left or 0) if follow else 0.0,
                     "since": since, "renews": renews, "source": source, "plan_name": reported.get("plan_name", ""),
                     "today": today_usd, "per_day": per_day,
                     "requests": requests, "unpriced": unpriced if source == "estimate" else 0,
                     "pct": int(100 * usd / limit) if limit else None, "error": reported.get("error", "")})
    rows.sort(key=lambda r: (not r["limit"], r["account"]))
    return rows


def set_spent(settings, account: str, amount: float) -> str:
    """/spend set: correct this cycle's estimate to what the provider's billing page shows (spend from before hexiel
    counted, or from other tools on the same account). Requests after it are added on top; a new cycle starts clean."""
    account = account.strip().lower()
    conf = accounts(settings).get(account) or {"cycle_day": 1}
    since = cycle_start(conf["cycle_day"]).isoformat()
    with _file_lock():
        data = _read("spend.json")
        for day, per_account in data.get("days", {}).items():
            if day >= since:
                (per_account.get(account) or {}).pop(ADJUST, None)
        _write("spend.json", data)
    _snap["at"] = 0.0
    row = next((r for r in totals(settings) if r["account"] == account), None)
    if row and row["source"] == "provider":
        return f"{account}'s figure comes from the provider ({money(row['usd'])}) — nothing to correct"
    counted = row["usd"] if row else 0.0
    with _file_lock():
        data = _read("spend.json")
        entry = data.setdefault("days", {}).setdefault(dt.date.today().isoformat(), {}).setdefault(account, {})
        entry[ADJUST] = {"usd": round(float(amount) - counted, 6)}
        _write("spend.json", data)
    _snap["at"] = 0.0
    return (f"{account}: set to {money(float(amount))} for the cycle that began {_day(dt.date.fromisoformat(since))} "
            f"(hexiel had counted {money(counted)}); new requests are added on top")


def command(settings, arg: str) -> str:
    """/spend [set ACCOUNT AMOUNT]"""
    parts = arg.split()
    if parts and parts[0] == "set":
        if len(parts) != 3:
            return "usage: /spend set ACCOUNT AMOUNT   e.g. /spend set anthropic 78.31 (from the provider's billing page)"
        try:
            amount = float(parts[2].lstrip("$").replace(",", ""))
        except ValueError:
            return f"not an amount: {parts[2]}"
        return set_spent(settings, parts[1], amount)
    _snap["at"] = 0.0
    refresh_async(settings)                 # a plain /spend re-asks the providers that can say
    return "\n".join(summary_lines(settings)) or "no paid model has been used this cycle"


def snapshot(settings) -> list[dict]:
    """totals(), recomputed at most every SNAPSHOT_SECONDS, and the providers asked in the background when due."""
    key = (id(settings), json.dumps(getattr(settings, "spend", None) or {}, sort_keys=True, default=str))
    if time.time() - _snap["at"] < SNAPSHOT_SECONDS and _snap["key"] == key:
        return _snap["rows"]
    refresh_async(settings)
    try:
        rows = totals(settings)
    except Exception:  # noqa: BLE001 - the sidebar must keep drawing
        rows = []
    _snap.update(at=time.time(), key=key, rows=rows)
    return rows


def _day(d: dt.date) -> str:
    return f"{d:%b} {d.day}"            # "Oct 23" (strftime's %-d does not exist on Windows)


def money(usd: float) -> str:
    return f"${usd:,.2f}" if usd < 1000 else f"${usd:,.0f}"


def budget_label(r: dict) -> str:
    """Where the budget figure comes from, for the UIs: 'plan + credits', 'plan' or '' (config.toml)."""
    if r.get("limit_source") != "plan":
        return ""
    return "plan + credits" if r.get("purchased_left") else "plan"


def runway(r: dict) -> str:
    """'runs out ~Nov 14' / 'lasts the cycle' at the last week's pace — '' when there is nothing to say."""
    left, per_day, renews = r.get("left"), r.get("per_day") or 0.0, r.get("renews")
    if left is None or not r.get("limit"):
        return ""
    if left <= 0:
        return "used up"
    if per_day <= 0:
        return ""
    today = dt.date.today()
    out = today + dt.timedelta(days=int(left / per_day))
    if renews and out >= renews:
        return "lasts the cycle"
    return "runs out today" if out == today else f"runs out ~{_day(out)}"


def summary_lines(settings) -> list[str]:
    """For /usage: one line per account."""
    lines = []
    for r in totals(settings):
        label = budget_label(r)
        cap = f" of {money(r['limit'])}{(f' ({label})' if label else '')} ({r['pct']}%)" if r["limit"] else ""
        when = f"since {_day(r['since'])}" + (f", renews {_day(r['renews'])}" if r.get("renews") else "")
        parts = [f"{r['account']}: {money(r['usd'])}{cap} {when}"]
        if r.get("left") is not None and r["limit"]:
            parts.append(f"{money(r['left'])} left" + (f" ({money(r['purchased_left'])} of it bought credits)"
                                                       if r.get("purchased_left") else ""))
        parts.append(f"today {money(r['today'])}, ~{money(r['per_day'])}/day this week")
        if runway(r):
            parts.append(runway(r))
        how = ("reported by the provider" if r["source"] == "provider"
               else "estimated from hexiel's own requests" + (f"; {r['unpriced']} request(s) have no price — add it "
                                                              "under [spend.prices]" if r["unpriced"] else ""))
        why = f" — couldn't ask the provider: {r['error']}" if r["error"] and r["source"] != "provider" else ""
        lines.append(" · ".join(parts) + f", {how}{why}")
    return lines


def alerts(settings) -> list[str]:
    """Messages for an account that just passed warn_at or its limit — each said once per cycle, by one process.
    Also said once per plan allowance: when the provider reports a new one (you changed your plan)."""
    level_at = warn_at(settings)
    rows = [r for r in snapshot(settings) if r["limit"]]
    if not rows:
        return []
    out = []
    with _file_lock():
        said = _read("spend-alerts.json")
        changed = False
        for r in rows:
            allowance = r.get("plan_usd") or (r["limit"] if r["limit_source"] == "plan" else 0)
            if r["limit_source"] == "plan" and allowance:
                tag = f"{r['account']}:plan:{money(allowance)}"
                if not said.get(tag):
                    said[tag] = time.time()
                    changed = True
                    name = f" ({r['plan_name']})" if r.get("plan_name") else ""
                    out.append(f"📈 {r['account']}'s plan{name} allows {money(allowance)} a cycle — the budget "
                               f"line follows it (pin yours with detect_limits = false under [spend.{r['account']}])")
            if r.get("purchased_left"):
                # credits bought on top of the plan: said once per top-up (the remainder only ever goes down
                # between top-ups, so a rise means you added some)
                last = float(said.get(f"{r['account']}:credits") or 0)
                if r["purchased_left"] > last + 0.005:
                    out.append(f"💳 {r['account']}: {money(r['purchased_left'])} of bought credits available on top of "
                               f"the plan — {money(r['left'])} left this cycle")
                if abs(r["purchased_left"] - last) > 0.005:
                    said[f"{r['account']}:credits"], changed = r["purchased_left"], True
            if not r["limit"]:
                continue
            share = r["usd"] / r["limit"]
            level = "limit" if share >= 1 else ("warn" if share >= level_at else "")
            tag = f"{r['account']}:{r['since']}:{level}"
            if not level or said.get(tag):
                continue
            said[tag], changed = time.time(), True
            if level == "limit":
                out.append(f"💸 {r['account']} has used its {money(r['limit'])} for this cycle "
                           f"({money(r['usd'])} since {_day(r['since'])}"
                           + (f"; renews {_day(r['renews'])}" if r.get("renews") else "") + ")")
            else:
                out.append(f"💸 {r['account']} is at {int(100 * share)}% of its {money(r['limit'])} for this cycle "
                           f"({money(r['usd'])} since {_day(r['since'])}, {money(r['left'] or 0)} left)")
        if changed:
            _write("spend-alerts.json", said)
    return out
