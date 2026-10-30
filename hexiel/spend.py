"""What the paid model providers cost this billing cycle, and how close that is to the limit you set.

Every request any hexiel process makes is metered where it is sent (llm.py): its tokens go into a small ledger
shared by every process on the machine (spend.json, one bucket per day, per account, per model). An account is the
bill a model is paid from — "anthropic", "openai", "ollama" (Ollama Cloud), or the host of another paid endpoint;
models on your own machines are free and not metered.

The figure shown for an account comes from the provider when it says what you spent, and is estimated from the
ledger otherwise:
  ollama     ollama.com/api/usage with your Ollama API key (OLLAMA_API_KEY) — the dollars Ollama charged
  anthropic  the Admin API cost report, with an Admin key (ANTHROPIC_ADMIN_KEY; organisations only)
  openai     the organisation costs API, with an Admin key (OPENAI_ADMIN_KEY)
  estimate   tokens × price: built-in Claude prices, the per-token rate Ollama last reported, or [spend.prices]

Configured in config.toml:

    [spend.ollama]
    limit = 60          # dollars a cycle; the sidebar warns at warn_at (default 80 %) and at 100 %
    cycle_day = 23      # the day of the month the plan renews (1 = calendar months, the default)
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
        out[str(name).lower()] = {"limit": limit, "cycle_day": day,
                                  "key_env": str(body.get("key_env", KEY_ENV.get(str(name).lower(), "")))}
    return out


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


def fetch_ollama(key: str, since: dt.date) -> tuple[float, dict]:
    """Dollars Ollama charged since `since` (its API reports at most 30 days, by day), and per-token rates."""
    data = _get(f"{OLLAMA_URL}/api/usage", {"authorization": f"Bearer {key}"}, {"range": "30d", "group_by": "model"})
    total = 0.0
    for bucket in data.get("buckets") or []:
        if str(bucket.get("from", ""))[:10] >= since.isoformat():
            total += float(bucket.get("usage_usd") or 0)
    rates = {}
    for g in (data.get("totals") or {}).get("groups") or []:
        tokens = (g.get("input_tokens") or 0) + (g.get("output_tokens") or 0)
        if tokens and g.get("model"):
            rates[str(g["model"])] = 1e6 * float(g.get("usage_usd") or 0) / tokens   # blended $ per million tokens
    return total, rates


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
        since = cycle_start(conf["cycle_day"])
        with _thread_lock:
            cached = _read("spend-remote.json").get(name) or {}
            fresh = (cached.get("since") == since.isoformat()
                     and time.time() - float(cached.get("at", 0)) < REFRESH_MINUTES * 60)
            if (fresh and not force) or name in _refreshing:
                continue
            _refreshing.add(name)
        try:
            entry = {"since": since.isoformat(), "at": time.time()}
            try:
                if name == "ollama":
                    entry["usd"], entry["rates"] = fetch_ollama(key, since)
                elif name == "anthropic":
                    entry["usd"] = fetch_anthropic(key, since)
                else:
                    entry["usd"] = fetch_openai(key, since)
            except Exception as exc:  # noqa: BLE001 - shown as "estimate", with the reason in /usage
                entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
                if cached.get("rates"):
                    entry["rates"] = cached["rates"]
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
        conf = accts.get(name) or {"limit": 0.0, "cycle_day": 1}
        since = cycle_start(conf["cycle_day"], today)
        rates = (remote.get(name) or {}).get("rates") or {}
        requests, usd, unpriced = 0, 0.0, 0
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
                usd += (fresh * p[0] + t.get("output", 0) * p[1] + t.get("cached", 0) * p[2]
                        + t.get("cache_write", 0) * p[3]) / 1e6
        reported = remote.get(name) or {}
        source = "estimate"
        if reported.get("since") == since.isoformat() and "usd" in reported:
            usd, source = float(reported["usd"]), "provider"
        if name not in accts and not requests and source == "estimate":
            continue                       # neither configured nor used this cycle
        limit = conf["limit"]
        rows.append({"account": name, "usd": usd, "limit": limit, "since": since, "source": source,
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


def summary_lines(settings) -> list[str]:
    """For /usage: one line per account."""
    lines = []
    for r in totals(settings):
        cap = f" of {money(r['limit'])} ({r['pct']}%)" if r["limit"] else ""
        how = ("reported by the provider" if r["source"] == "provider"
               else "estimated from hexiel's own requests" + (f"; {r['unpriced']} request(s) have no price — add it "
                                                              "under [spend.prices]" if r["unpriced"] else ""))
        why = f" — couldn't ask the provider: {r['error']}" if r["error"] and r["source"] != "provider" else ""
        lines.append(f"{r['account']}: {money(r['usd'])}{cap} since {_day(r['since'])}, {how}{why}")
    return lines


def alerts(settings) -> list[str]:
    """Messages for an account that just passed warn_at or its limit — each said once per cycle, by one process."""
    level_at = warn_at(settings)
    rows = [r for r in snapshot(settings) if r["limit"]]
    if not rows:
        return []
    out = []
    with _file_lock():
        said = _read("spend-alerts.json")
        changed = False
        for r in rows:
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
                           f"({money(r['usd'])} since {_day(r['since'])})")
            else:
                out.append(f"💸 {r['account']} is at {int(100 * share)}% of its {money(r['limit'])} for this cycle "
                           f"({money(r['usd'])} since {_day(r['since'])})")
        if changed:
            _write("spend-alerts.json", said)
    return out
