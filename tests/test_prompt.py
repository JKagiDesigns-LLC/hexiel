"""The default system prompt stays short and keeps the engineering rules it exists to carry."""
from hexiel import prompt


def test_base_prompt_is_short():
    assert len(prompt.BASE_PROMPT) // 4 < 900        # ~tokens; it rides on every request


def test_power_of_ten_rules_are_all_present_and_numbered():
    text = prompt.BASE_PROMPT
    for n in range(1, 11):
        assert f"\n{n}. " in text
    assert "Power of 10" in text


def test_dry_and_the_change_workflow_are_stated():
    t = " ".join(prompt.BASE_PROMPT.split())          # wrapping is not the point
    assert "DRY" in t and "one source of truth" in t
    for word in ("Lint", "format", "unit tests", "end-to-end"):
        assert word in t
    assert "Commit gate" in t and "even when the user has already asked" in t
    assert "do not install anything on your own" in t
    assert "choose" in t and "two or three options" in t
    assert "never `git add -A`" in t


def test_the_rules_that_must_survive_the_rewrite_are_still_there():
    t = prompt.BASE_PROMPT
    assert "git push" in t and "todo" in t and "memory" in t and "Defensive" in t and "load_tools" in t


def test_full_prompt_still_assembles(tmp_path):
    full = prompt.build_system_prompt(str(tmp_path), "", "")
    assert full.startswith("You are hexiel") and "# Memory" in full and "# Skills" in full
