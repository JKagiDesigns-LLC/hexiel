"""redact_secrets = "full": credentials in tool output and typed text are replaced with `<redacted>`
before they are stored in the chat — so the model, whatever provider serves it, never sees them."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import redact
from hexiel.config import ModelConfig, Settings
from hexiel.session import Session
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext, build_tools, text_result

MC = ModelConfig(name="t", provider="openai", model="m", base_url="http://x")

# --- the value disappears but the line stays readable

LEAKS = [
    ("export ASC_KEY_ID=474VP8LBXS; ssh macmini", "474VP8LBXS"),
    ("DB_PASSWORD=hunter2-prettily-long", "hunter2-prettily-long"),
    ('"api_key": "sk-ant-oat01-abcdef0123456789"', "sk-ant-oat01-abcdef0123456789"),
    ("password: 'hunter2 the brave'", "hunter2 the brave"),
    ("postgres://admin:secretOne1@db.internal:5432/app", "secretOne1"),
    ("Authorization: Bearer a1b2c3d4e5f6g7h8i9j0", "a1b2c3d4e5f6g7h8i9j0"),
    ("glpat-Hy9WdD2OvsMul9UlTiqsjGM6", "glpat-Hy9WdD2OvsMul9UlTiqsjGM6"),
    ("GitHub-Token: ghp_ABCDEFGHIJKLMNOPQRSTUV", "ghp_ABCDEFGHIJKLMNOPQRSTUV"),
    # passwords on the command line
    ("sshpass -p 'Cabot**23' ssh -o StrictHostKeyChecking=no josh@192.168.2.101 \"hostname\"", "Cabot**23"),
    ("mysql -u admin -pSecretOne1 mydb", "SecretOne1"),
    ("mysqldump -pSwordfish99 db > dump.sql", "Swordfish99"),
    ("docker login -p 'hunter2 the brave' -u josh", "hunter2 the brave"),
    ("openssl enc -aes-256-cbc -k SaltandPepper1 -in f -out g", "SaltandPepper1"),
    ("deploy --password=hunter2-prettily-long staging", "hunter2-prettily-long"),
    ("gpg --passphrase 'sOlveItTwice' -c file", "sOlveItTwice"),
    ("curl -u admin:SecretOne1 https://api.example.com", "SecretOne1"),
    ("kubectl --token abcdefghsecret12 get pods", "abcdefghsecret12"),
]
# every leak must name a secret at least somewhere in the scan
for text, secret in LEAKS:
    out = redact.scrub(text)
    assert secret not in out, (text, out)

PROMPT = "ssh macmini 'export ASC_KEY_ID=474VP8LBXS; node /tmp/final-state.mjs' — the script prints the state"
out = redact.scrub(PROMPT)
assert "node /tmp/final-state.mjs" in out and "export ASC_KEY_ID=<redacted>" in out

# a private key block vanishes whole
pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nbiusdbjs\nbiusdbjs\nbiusdbjs\n-----END OPENSSH PRIVATE KEY-----"
assert pem not in redact.scrub("prefix " + pem) and "<private key removed>" in redact.scrub(pem)

# ordinary code and config lines survive untouched
KEEP = [
    "max_tokens: 4096", "n_tokens += 1", "the tokenizer is loaded from ~/models",
    "monkey=browser", "sort key=x", "export PATH=/opt/homebrew/bin:$PATH",
    'grep -E "^ASC_ISSUER_ID=" ~/.spendera-secrets/ios.env', "color: #ff00ff",
    "auth=basic", "postgres://db.internal:5432/app", "contact bob@example.com",
    # ports and flag-shaped options that are not passwords must survive
    "ssh -p 2222 macmini", "redis-cli -p 6379", "psql -h db -p 5432", "mysql -P 3307",
    "sshpass -e ssh macmini", "openssl rand -base64 32", "docker run -p 8080:80 nginx",
    "--password \"$PW\"",     # a quoted $reference names a variable, not a secret
    "token", "KEY=",
]
for text in KEEP:
    assert redact.scrub(text) == text, (text, redact.scrub(text))

assert redact.scrub("") == "" and redact.scrub(None) == ""


# --- end to end: a tool's output (and a typed message) land redacted in the history

def session(tmp_path):
    tools = [t for t in build_tools(Toolchain()) if t.name == "bash"]
    s = Session(MC, Settings(), str(tmp_path), tools, "sys")
    s.bind_tool_ctx(ToolContext(cwd=str(tmp_path), chain=Toolchain()))
    s.auto_approve = True
    return s


def test_tool_output_is_redacted_before_history(tmp_path):
    s = session(tmp_path)
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text("GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUV\nexport ASC_KEY_ID=474VP8LBXS\n")
    s._execute_tool({"id": "c1", "name": "bash", "arguments": {"command": f"cat {secret_file}"}})
    stored = next(m for m in s.messages if m.get("role") == "tool")
    assert "ghp_" not in stored["content"] and "474VP8LBXS" not in stored["content"]
    assert "GITHUB_TOKEN=" in stored["content"] and "ASC_KEY_ID=" in stored["content"]
    assert stored["content"].count(redact.REDACTED) >= 2  # both values gone, both lines still readable


def test_tool_output_keeps_ordinary_content(tmp_path):
    s = session(tmp_path)
    s._execute_tool({"id": "c1", "name": "bash", "arguments": {"command": "echo 'max_tokens: 4096; hello world'"}})
    stored = next(m for m in s.messages if m.get("role") == "tool")
    assert "max_tokens: 4096" in stored["content"]


def test_user_paste_is_redacted_before_history(tmp_path, monkeypatch):
    s = session(tmp_path)

    class P:  # a stub provider that answers "ok" without a network call
        def stream(self, *a, **k):
            yield ("assistant", {"role": "assistant", "content": "ok"})
            yield ("done", {})

    monkeypatch.setattr("hexiel.session.llm.make_provider", lambda *a, **k: P())
    internal_backup = s.internal
    s.internal = True                       # skip the router/instructions plumbing; the history entry is what matters
    s.plan_mode = False
    try:
        s.run_turn("use this key: DB_PASSWORD=hunter2-prettily-long in app.env")
    finally:
        s.internal = internal_backup
    stored = next(m for m in s.messages if m.get("role") == "user")
    assert "hunter2" not in stored["content"]
    assert "<redacted>" in stored["content"]
