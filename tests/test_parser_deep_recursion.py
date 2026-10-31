"""``parser.deep_recursion``: a one-way, thread-safe raise of the recursion limit."""

from __future__ import annotations

import sys
import threading

from arango_cypher.parser import _PARSE_RECURSION_LIMIT, deep_recursion, parse_cypher

_DEEP = "RETURN " + "[" * 60 + "]" * 60 + " AS deep"


def test_limit_is_raised_and_never_lowered():
    before = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(900)
        with deep_recursion():
            assert sys.getrecursionlimit() == _PARSE_RECURSION_LIMIT
        assert sys.getrecursionlimit() == _PARSE_RECURSION_LIMIT  # not restored: a restore races
        sys.setrecursionlimit(_PARSE_RECURSION_LIMIT + 1000)
        with deep_recursion():
            assert sys.getrecursionlimit() == _PARSE_RECURSION_LIMIT + 1000  # a higher limit stays
    finally:
        sys.setrecursionlimit(max(before, _PARSE_RECURSION_LIMIT))


def test_concurrent_deep_parses_all_succeed():
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def parse() -> None:
        try:
            barrier.wait()
            for _ in range(5):
                parse_cypher(_DEEP)
        except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=parse) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sys.getrecursionlimit() >= _PARSE_RECURSION_LIMIT
