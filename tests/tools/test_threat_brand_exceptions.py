"""Contract tests: config-subtractable C2 brand list in threat_patterns.

``security.threat_scan_brand_exceptions`` subtracts individual brands from
the ``known_c2_framework`` context pattern. Contract:

* no exceptions          -> exact upstream behavior (all brands block)
* one brand excepted     -> that brand passes; every other brand still blocks
* all brands excepted    -> the pattern is dropped, not compiled-to-nothing
* unknown/malformed cfg  -> fail-open to no exceptions (scanner never widens)
* other pattern ids      -> unaffected (c2_explicit etc. still fire)
"""

import pytest

import tools.threat_patterns as tp


@pytest.fixture
def recompiled():
    """Snapshot/restore the compiled pattern cache around a test.

    Yields a function that clears the cache and recompiles, so a test can
    monkeypatch ``_brand_exceptions`` and observe the effect.
    """
    saved = tp._COMPILED

    def recompile():
        tp._COMPILED = {}
        tp._compile()

    yield recompile
    tp._COMPILED = saved


class TestDefaultBehavior:
    def test_all_brands_block_by_default(self, monkeypatch, recompiled):
        monkeypatch.setattr(tp, "_brand_exceptions", frozenset)
        recompiled()
        for text in (
            "deploy a Cobalt Strike beacon",
            "sliver implant",
            "havoc framework",
            "the mythic rendering of the field",
            "metasploit module",
            "brainworm node",
        ):
            assert "known_c2_framework" in tp.scan_for_threats(text, scope="context"), text

    def test_context_pattern_absent_from_all_scope(self, monkeypatch, recompiled):
        monkeypatch.setattr(tp, "_brand_exceptions", frozenset)
        recompiled()
        assert "known_c2_framework" not in tp.scan_for_threats("mythic", scope="all")


class TestExceptions:
    def test_excepted_brand_passes_others_still_block(self, monkeypatch, recompiled):
        monkeypatch.setattr(tp, "_brand_exceptions", lambda: frozenset({"mythic"}))
        recompiled()
        assert tp.scan_for_threats("the mythic transmission lane", scope="context") == []
        assert "known_c2_framework" in tp.scan_for_threats("sliver implant", scope="context")
        assert "known_c2_framework" in tp.scan_for_threats("metasploit", scope="context")

    def test_multiword_brand_exception_by_display_name(self, monkeypatch, recompiled):
        monkeypatch.setattr(
            tp, "_brand_exceptions", lambda: frozenset({"cobalt strike"})
        )
        recompiled()
        assert tp.scan_for_threats("cobalt strike payload", scope="context") == []
        assert "known_c2_framework" in tp.scan_for_threats("mythic", scope="context")

    def test_all_brands_excepted_drops_pattern(self, monkeypatch, recompiled):
        monkeypatch.setattr(
            tp,
            "_brand_exceptions",
            lambda: frozenset(name for name, _ in tp._C2_BRANDS),
        )
        assert tp._c2_brand_pattern() is None
        recompiled()
        assert tp.scan_for_threats("mythic sliver havoc", scope="context") == []

    def test_unknown_exception_names_are_ignored(self, monkeypatch, recompiled):
        monkeypatch.setattr(
            tp, "_brand_exceptions", lambda: frozenset({"nonexistent-brand"})
        )
        recompiled()
        assert "known_c2_framework" in tp.scan_for_threats("mythic", scope="context")

    def test_other_c2_patterns_unaffected_by_exceptions(self, monkeypatch, recompiled):
        monkeypatch.setattr(
            tp,
            "_brand_exceptions",
            lambda: frozenset(name for name, _ in tp._C2_BRANDS),
        )
        recompiled()
        assert "c2_explicit" in tp.scan_for_threats("c2 server rotation", scope="context")
        assert "c2_explicit_long" in tp.scan_for_threats("command and control", scope="context")


class TestConfigReader:
    def test_reader_fails_open_to_empty(self, monkeypatch):
        def boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr("hermes_cli.config.read_raw_config", boom)
        assert tp._brand_exceptions() == frozenset()

    def test_reader_ignores_non_list_value(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"security": {"threat_scan_brand_exceptions": "mythic"}},
        )
        assert tp._brand_exceptions() == frozenset()

    def test_reader_normalizes_case_and_whitespace(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"security": {"threat_scan_brand_exceptions": ["  Mythic ", ""]}},
        )
        assert tp._brand_exceptions() == frozenset({"mythic"})
