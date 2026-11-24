from hexiel.session import _announces_action


def test_announce_only_replies_are_detected():
    assert _announces_action("Let me check the state.\n\nEmitting.")
    assert _announces_action("Let me issue it.")
    assert not _announces_action("Done. The build uploaded.")
    assert not _announces_action("Let me know if you want more. " * 40)
    assert not _announces_action("")
