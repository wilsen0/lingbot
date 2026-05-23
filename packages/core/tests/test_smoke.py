from linling_core import __version__


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)


def test_core_exports_key_symbols() -> None:
    import linling_core as m

    for name in (
        "Event",
        "Action",
        "Scope",
        "User",
        "Segment",
        "EventBus",
        "text",
        "image",
        "at",
        "reply",
    ):
        assert hasattr(m, name), name
