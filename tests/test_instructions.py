"""Instruction import: discovery, verbatim splitting, index/relevance, ignore-vs-commit, Claude memory sync."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import instructions as ins
from hexiel import prompt as prompt_mod

BIG = ("# Project\n\nIntro line about the project, long enough to matter.\n\n"
       "## Build commands\n\n" + "Run `make build` to compile everything.\n" * 3 +
       "\n## Database migrations\n\n" + "Always run alembic upgrade head before the tests.\n" * 30 +
       "\n## Style\n\n```\n## not a heading, inside a fence\n```\n" + "Use four-space indents.\n" * 40)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    p = tmp_path / "proj"
    p.mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    return p


def test_split_round_trips_and_respects_fences():
    parts = ins.split_sections(BIG)
    assert "".join(b for _, b in parts) == BIG
    assert [t for t, _ in parts] == ["overview", "Build commands", "Database migrations", "Style"]
    assert "".join(b for _, b in ins.chunk(BIG)) == BIG


def test_discover_finds_other_agents_files_and_skips_generated(proj):
    (proj / "CLAUDE.md").write_text(BIG)
    (proj / ".cursorrules").write_text("be terse\n")
    (proj / "AGENTS.md").write_text(ins.INDEX_MARK + "\n# index\n")
    labels = sorted(s.label for s in ins.discover(str(proj)))
    assert labels == ["claude", "cursorrules"]


def test_discover_covers_every_harness(proj):
    """#26: .codex + codex rules, copilot, .agent.md, .cursor rules — layered together."""
    (proj / "AGENTS.md").write_text("# agents rules\n" + BIG)
    codex = proj / ".codex"
    codex.mkdir()
    (codex / "AGENTS.md").write_text("codex nested rules\n")
    codex_rules = proj / ".codex" / "rules"
    codex_rules.mkdir()
    (codex_rules / "git-safety.mdc").write_text("never force push\n")
    (proj / "COPILOT.md").write_text("copilot base rules\n")
    (proj / ".agent.md").write_text("agent harness rules\n")
    cursor = proj / ".cursor" / "rules"
    cursor.mkdir(parents=True)
    (cursor / "style.mdc").write_text("style rules\n")
    labels = sorted(s.label for s in ins.discover(str(proj)))
    assert labels == ["agent", "agents", "codex", "codex-git-safety", "copilot", "cursor-style"]
    by_label = {s.label: s for s in ins.discover(str(proj))}
    assert by_label["agents"].scope == "project"
    assert by_label["codex"].tool == "codex" and by_label["codex-git-safety"].tool == "codex-git-safety"


def test_discover_last_wins_on_same_labels_per_scope(proj):
    """One harness, two copies: the nested canonical one beats the stray root one."""
    (proj / "CLAUDE.md").write_text("root version\n")
    nested = proj / ".claude"
    nested.mkdir()
    (nested / "CLAUDE.md").write_text("nested version (wins)\n")
    srcs = ins.discover(str(proj))
    assert len(srcs) == 1
    assert srcs[0].label == "claude" and srcs[0].path == nested / "CLAUDE.md"
    assert srcs[0].text == "nested version (wins)\n"
    # global scope is scored separately: a global claude memory keeps its own import
    (proj / ".claude" / "CLAUDE.md").unlink()
    (proj / "CLAUDE.md").unlink()
    home_claude = Path(ins.home_config_dir())
    home_claude.mkdir(parents=True, exist_ok=True)
    try:
        (home_claude / "HEXIEL.md").write_text("global rules\n")
        srcs2 = ins.discover(str(proj))
        assert [s.scope for s in srcs2] == ["global"]
        assert srcs2[0].label == "hexiel"
    finally:
        (home_claude / "HEXIEL.md").unlink()


def test_import_ignore_mode_leaves_originals_and_writes_verified_topics(proj):
    (proj / "CLAUDE.md").write_text(BIG)
    srcs = ins.discover(str(proj))
    res = ins.import_sources(str(proj), srcs, "ignore", say=lambda *_: None)
    assert (proj / "CLAUDE.md").read_text() == BIG
    assert res["replaced"] == [] and res["topics"] >= 2
    assert ".hexiel" in (proj / ".gitignore").read_text()
    index = (proj / ".hexiel/instructions/INDEX.md").read_text()
    assert "Database migrations" in index or "claude-database-migrations" in index
    assert ins.pending(str(proj)) == []                      # not offered again


def test_import_commit_mode_stubs_originals_and_makes_agents_md_the_index(proj):
    (proj / "CLAUDE.md").write_text(BIG)
    (proj / ".cursorrules").write_text("be terse and kind to small models\n" * 5)
    ins.import_sources(str(proj), ins.discover(str(proj)), "commit", say=lambda *_: None)
    assert ins.STUB_MARK in (proj / "CLAUDE.md").read_text()
    assert ins.STUB_MARK in (proj / ".cursorrules").read_text()
    assert ins.INDEX_MARK in (proj / "AGENTS.md").read_text()
    assert (proj / ".hexiel/imported/CLAUDE.md").read_text() == BIG       # backup
    assert ins.restore(str(proj)) and (proj / "CLAUDE.md").read_text() == BIG
    gi = (proj / ".gitignore").read_text()
    assert ".hexiel/imported" in gi and ".hexiel\n" not in gi


def test_prompt_uses_the_index_not_the_whole_file(proj):
    (proj / "CLAUDE.md").write_text(BIG)
    before = prompt_mod._project_docs(str(proj))
    ins.import_sources(str(proj), ins.discover(str(proj)), "ignore", say=lambda *_: None)
    after = prompt_mod._project_docs(str(proj))
    assert "alembic upgrade head" in before and "alembic upgrade head" not in after
    assert len(after) < len(before)


def test_relevant_attaches_matching_topic_once_within_budget(proj):
    (proj / "CLAUDE.md").write_text(BIG)
    ins.import_sources(str(proj), ins.discover(str(proj)), "ignore", say=lambda *_: None)
    sent: set[str] = set()
    got = ins.relevant(str(proj), "fix the database migrations", 32768, sent)
    assert "alembic upgrade head" in got and len(got) <= 2600 + 200
    assert ins.relevant(str(proj), "fix the database migrations", 32768, sent) == ""
    assert ins.relevant(str(proj), "tell a joke", 32768) == ""


def test_offer_declined_is_remembered_until_the_file_changes(proj):
    (proj / "CLAUDE.md").write_text(BIG)
    said = []
    assert ins.offer_import(str(proj), ask=lambda q: "n", say=said.append) is False
    assert ins.offer_import(str(proj), ask=lambda q: pytest.fail("asked twice"), say=said.append) is False
    (proj / "CLAUDE.md").write_text(BIG + "\n## New\nmore\n")
    answers = iter(["y", "i"])
    assert ins.offer_import(str(proj), ask=lambda q: next(answers), say=said.append) is True
    assert (proj / ".hexiel/instructions/INDEX.md").exists()


def test_claude_memories_are_copied_both_ways_with_bridge_notes(proj):
    cm = ins.claude_memory_dir(str(proj))
    cm.mkdir(parents=True)
    (cm / "user-role.md").write_text("---\nname: user-role\ndescription: who the user is\n"
                                     "metadata:\n  type: user\n---\n\nSenior dev.\n")
    (cm / "MEMORY.md").write_text("- [User role](user-role.md) — who the user is\n")
    ins.run_import(str(proj), [], "ignore", say=lambda *_: None)
    hx = proj / ".hexiel/memory"
    assert (hx / "user-role.md").read_text() == (cm / "user-role.md").read_text()
    assert "claude-memory-sync" in (hx / "MEMORY.md").read_text()
    assert "hexiel-memory-sync" in (cm / "MEMORY.md").read_text()
    assert str(hx) in (cm / "hexiel-memory-sync.md").read_text()
    (hx / "new-fact.md").write_text("---\nname: new-fact\ndescription: a hexiel fact\n---\n\nx\n")
    c = ins.sync_memories(str(proj))
    assert c["to_claude"] == 1 and (cm / "new-fact.md").exists()
    from hexiel.memory import Memory
    assert Memory.from_file(hx / "user-role.md", "project").type == "user"


def test_doctor_reports_whether_hexiel_is_on_path(monkeypatch, capsys, tmp_path):
    from hexiel import doctor
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/x/bin/hexiel" if name == "hexiel" else None)
    assert doctor.run_checks(str(tmp_path))["path"] is True
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.run_checks(str(tmp_path))["path"] is False
    assert "pipx ensurepath" in capsys.readouterr().out
