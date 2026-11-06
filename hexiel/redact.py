"""Keep credentials out of the model's context.

Everything a model reads — user messages, tool output — is scanned for credential-shaped values
before it is stored in the chat and sent to a provider. The chat (and every provider, including
free/zero-retention-tier ones) sees `<redacted>`; only the value is gone, so the surrounding
command or log stays readable and the model keeps working around it.

This is about what the AI sees, not about encryption: secrets still exist in files, command
output and process lists. Redaction rides along with good practice (secret managers,
.env in .gitignore) — it does not replace it.

Deliberately NARROW so redaction never makes code unreadable: e-mails, LAN IPs and long base64
blobs are NOT redacted here (reporter.redact adds those, but for error reports that leave the
machine — a different threat, and those rules would mangle normal code).
"""

from __future__ import annotations

import re

REDACTED = "<redacted>"

KEYWORDS = {"password", "passwd", "pwd", "secret", "token", "key", "auth", "authorization",
            "credential", "credentials", "apikey", "accesskey", "privatekey", "keystore",
            "session", "issuerid", "bearer"}


def _names_a_secret(name: str) -> bool:
    """GITHUB_TOKEN, ASC_KEY_ID, accessToken, DB-PASSWORD-2 … all become a word set that must
    contain one keyword. camelCase words are split; ALL-CAPS names also match a keyword anywhere
    inside them (MYKEY, GITHUB_TOKEN), so 'tokenizer=…' or 'monkey=…' are never false hits."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)          # accessToken → access Token
    words = {w.lower() for w in re.split(r"[_\-.:/\s]+", words)}
    for w in words:
        if w in KEYWORDS:
            return True
        if w.isupper() and any(k in w for k in KEYWORDS):          # MYKEY, ASCKEYID
            return True
    return False


# name = value / name: value, quoted (may contain spaces) or bare (one shell/JSON token).
_NAME_VALUE = re.compile(
    r"(?<![\w])[A-Za-z_][A-Za-z0-9_.\-/]{0,39}(?P<delim>\s*[:=]+\s*)"
    r"(?:\"(?P<dq>[^\"\n]{5,})\"|'(?P<sq>[^'\n]{5,})'|(?P<raw>(?!\$)[^\s\"']{6,}))")

# postgres://user:hunter2@db — the password rides the URL.
_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)[^/\s:@]{1,64}([:.])[^/\s@\"]{4,64}@")

# Passwords on a command line. Long flag names say what they carry (--password, --passphrase,
# --secret, --token, --api-key …), so they match with any tool; the short forms (-p, -k) are
# ambiguous (ports in ssh/redis/psql!), so they only count for tools whose -p/-k IS a password
# (sshpass, mysql family, openssl, docker login), and only for values that contain letters —
# "sshpass -p 22" never exists, "ssh -p 2222" must survive. $-references and ${VAR} stay visible:
# a variable name is not a secret.
_PW_VAL = (r"(?:\"(?P<dq>(?!\$)[^\"\n]{5,})\"|'(?P<sq>(?!\$)[^'\n]{5,})'"
           r"|(?P<raw>(?!\$)(?=[^\s\"'\n]*[A-Za-z])[^\s\"'\n]{4,}))")
_TOKEN_WORD = r"pass(?:word|wd|phrase|in|out)?|secret(?:[_-]?key)?|token|api[_-]?key|access[_-]?token|client[_-]?secret|ssh[_-]?password"

# --password X / --password=x — long flag names say what they carry, so any tool
_PW_LONG_FLAG = re.compile(r"(?i)-{1,2}(?:" + _TOKEN_WORD + r")(?P<eq>=[^\S\n]*|[^\S\n=]+)" + _PW_VAL)
# sshpass -p '…' / mysql -p x / docker login -p x — spaced short flags, password tools only
_PW_SHORT_FLAG = re.compile(
    r"(?:\b(?:sshpass|mysql|mariadb|mysqldump|mysqladmin)\b|\bdocker\s+login\b)"
    r"(?P<mid>(?:[^\S\n]+\S+)*?)[^\S\n]+(?-i:-p)[^\S\n]*" + _PW_VAL)
# openssl -k Secret1 / -K rawkey — its short password/key flags are case distinct
_PW_OPENSSL_K = re.compile(
    r"\bopenssl\b(?P<mid>(?:[^\S\n]+\S+)*?)[^\S\n]+(?-i:-[kK])[^\S\n]*" + _PW_VAL)
# mysql -pSecret / openssl -kRawKey — attached short flags
_PW_SHORT_ATTACHED = re.compile(
    r"\b(?:mysql|mariadb|mysqldump|mysqladmin|openssl)\b(?P<mid>(?:[^\S\n]+\S+)*?)-[pPkK]"
    r"(?P<att>(?![-\$])(?=[^\s\"'\n]*[A-Za-z])[^\s\"'\n]{4,})")
# curl -u admin:SecretOne1 — the password rides after the colon
_PW_CURL = re.compile(
    r"(?i)\bcurl\b(?P<mid>(?:[^\S\n]+\S+)*?)[^\S\n]+-u(?:=[^\S\n]*|[^\S\n]+)"
    r"(?P<user>[^\s:\n\"']{1,64}):(?P<pass>(?!\$)[^\s\"'\n]{4,})")


# Credential-shaped values that are almost always secrets regardless of what names them.
_TOKEN_PATTERNS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),           # JWTs
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "<key>"),                                    # OpenAI-style keys
    (re.compile(r"\b(?:ghp|gho|ghs|github_pat|glpat|xox[a-z]|hxd)[-_][A-Za-z0-9_-]{10,}"), "<token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<key>"),                                                 # AWS access key id
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "<key>"),                                           # Google API key
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 <token>"),                 # auth headers
]
TOKEN_PATTERNS = _TOKEN_PATTERNS     # legacy alias (reporter imports this list)

# -----BEGIN ... PRIVATE KEY----- blocks, any key type.
_PEM = re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY(?: BLOCK)?-----[\s\S]{19,}?-----END [^-]+-----")


# the flag, the tool and the delimiter stay readable: sshpass -p <redacted>, --password=<redacted>
def _pw_sub(match: re.Match) -> str:
    d = match.groupdict()
    val = d.get("dq") or d.get("sq") or d.get("raw") or d.get("att") or d.get("pass") or ""
    whole = match.group(0)
    close = '"' if d.get("dq") is not None else "'" if d.get("sq") is not None else ""
    return whole[: len(whole) - len(val) - len(close)] + REDACTED + close


def _name_sub(match: re.Match) -> str:
    if not _names_a_secret(match.group(0).split(match.group("delim"))[0]):
        return match.group(0)                    # an ordinary value: leave it alone
    end = match.group("dq") or match.group("sq") or match.group("raw")
    whole, value = match.group(0), end
    close = '"' if match.group("dq") is not None else "'" if match.group("sq") is not None else ""
    keep = whole[: (len(whole) - len(value) - len(close))] or whole[:0]
    return keep + REDACTED + close               # name + delimiter stay readable: user=PASS<redacted>


def scrub(text: str) -> str:
    """Hide credential values in text the model is about to see (see the module docstring)."""
    if not text or not isinstance(text, str):
        return text or ""
    text = _PEM.sub("<private key removed>", text)
    for pattern, repl in TOKEN_PATTERNS:
        text = pattern.sub(repl, text)
    text = _URL_CREDS.sub(r"\1<user>\2" + REDACTED + "@", text)
    for pw in (_PW_LONG_FLAG, _PW_SHORT_FLAG, _PW_OPENSSL_K, _PW_SHORT_ATTACHED, _PW_CURL):
        text = pw.sub(_pw_sub, text)
    return _NAME_VALUE.sub(_name_sub, text)
