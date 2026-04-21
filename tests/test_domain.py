from openwrt_presence.domain import Mac, NodeName, PersonName, Room  # noqa: F401


def test_newtypes_are_str_at_runtime():
    m = Mac("aa:bb:cc:dd:ee:01")
    assert isinstance(m, str)
    assert m == "aa:bb:cc:dd:ee:01"


def test_newtypes_distinct_at_typecheck():
    # Pyright should flag this (we rely on CI gating); runtime is str.
    p = PersonName("alice")
    n = NodeName("ap-garden")
    assert p != n
