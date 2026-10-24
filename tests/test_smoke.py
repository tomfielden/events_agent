import events_agent


def test_import() -> None:
    assert callable(events_agent.main)
