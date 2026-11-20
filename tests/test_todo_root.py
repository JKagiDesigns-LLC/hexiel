from hexiel import todo as todo_mod


def test_creates_root_todo_md(tmp_path):
    todo_mod.action_create({"title": "t", "steps": ["a", "b"]}, str(tmp_path))
    assert (tmp_path / "todo.md").is_file()
    assert not (tmp_path / ".hexiel").exists()


def test_uses_existing_case_insensitive_and_preserves_content(tmp_path):
    f = tmp_path / "TODO.md"
    f.write_text("# Roadmap\n\nSome prose.\n\n## Now\n- [ ] one\n- [x] two\n\n## Notes\nkeep me\n")
    todo_mod.action_update({"step": "one", "state": "~"}, str(tmp_path))
    todo_mod.action_add({"step": "three", "after": "one"}, str(tmp_path))
    todo_mod.action_left_off({"note": "mid"}, str(tmp_path))
    assert not (tmp_path / "todo.md").exists() or (tmp_path / "todo.md").samefile(f)
    text = f.read_text()
    assert "Some prose." in text and "## Notes\nkeep me" in text
    assert "- [~] one\n- [ ] three\n- [x] two" in text
    assert "where we left off: mid" in text


def test_create_on_existing_file_appends_section(tmp_path):
    f = tmp_path / "todo.md"
    f.write_text("# mine\n- [ ] old\n")
    todo_mod.action_create({"title": "New work", "steps": ["x"]}, str(tmp_path))
    text = f.read_text()
    assert text.startswith("# mine\n- [ ] old\n") and "## New work" in text and "- [ ] x" in text
