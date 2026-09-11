"""Quota-exhaustion cooldowns, Brave fallback routing, and the search audit row.

Keeper ruling 2026-09-10 (Luis): "we can fallback to my brave API search tool
if z.ai is exhausted". On the night of the ruling Z.AI answered every reader
and search call with MCP error -429 / code 1310, and the stateless one-shot
keyless rescue re-tried Z.AI on every call for the rest of the composer run.

Covers:
- exhaustion parse against the VERBATIM gateway-log error text, plus the
  negative cases that must NOT earn a cooldown (a transient MCP -429 with no
  reset stamp, Brave's 1-qps HTTP 429, an ordinary upstream 500)
- cooldown routing: the first 1310 parks Z.AI and Brave serves; the SECOND
  call never touches Z.AI again (the whole point — the old rescue was stateless)
- extract during a Z.AI cooldown: Brave is search-only, so with no keyed
  extract backend the dead call is skipped and the keyless ring serves
- the search_event.v1 audit row: exact shape, and the privacy rule that the
  query text and the URLs never appear in it
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import tools.web_backend_fallback as fb
import tools.web_tools as web_tools
from agent.web_search_provider import WebSearchProvider

# Verbatim from journalctl -u hermes-primary.service, 2026-09-10 21:24:25.
ZAI_SEARCH_ERROR = (
    "Z.AI web search failed: Z.AI MCP tool 'web_search_prime' failed: "
    'MCP error -429: {"error":{"code":"1310","message":"Weekly/Monthly Limit '
    'Exhausted. Your limit will reset at 2026-09-24 03:09:48"}}'
)
ZAI_EXTRACT_ERROR = (
    "Z.AI web reader failed: Z.AI MCP tool 'webReader' failed: "
    'MCP error -429: {"error":{"code":"1310","message":"Weekly/Monthly Limit '
    'Exhausted. Your limit will reset at 2026-09-24 03:09:48"}}'
)


class _FakeZai(WebSearchProvider):
    """Z.AI double: always answers with the real exhaustion error."""

    def __init__(self):
        self.search_calls = 0
        self.extract_calls = 0

    @property
    def name(self):
        return "zai"

    @property
    def display_name(self):
        return "Z.AI"

    def supports_search(self):
        return True

    def supports_extract(self):
        return True

    def is_available(self):
        return True

    def search(self, query, limit=5):
        self.search_calls += 1
        return {"success": False, "error": ZAI_SEARCH_ERROR}

    def extract(self, urls, **kwargs):
        self.extract_calls += 1
        return [
            {"url": u, "title": "", "content": "", "error": ZAI_EXTRACT_ERROR}
            for u in urls
        ]


class _FakeBrave(WebSearchProvider):
    """Brave double: search-only, always succeeds."""

    def __init__(self):
        self.search_calls = 0

    @property
    def name(self):
        return "brave-free"

    @property
    def display_name(self):
        return "Brave Search (Free)"

    def supports_search(self):
        return True

    def supports_extract(self):
        return False

    def is_available(self):
        return True

    def search(self, query, limit=5):
        self.search_calls += 1
        return {
            "success": True,
            "data": {"web": [{"title": "t", "url": "https://brave.example", "description": "d", "position": 1}]},
        }


@pytest.fixture
def backends(monkeypatch):
    """Register the zai + brave doubles and clear the cooldown table."""
    from agent import web_search_registry

    fb._reset_for_tests()
    zai, brave = _FakeZai(), _FakeBrave()
    previous = {
        name: web_search_registry.snapshot_registration(name)
        for name in ("zai", "brave-free")
    }
    web_search_registry.register_provider(zai)
    web_search_registry.register_provider(brave)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(
        web_tools, "_load_web_config",
        lambda: {"backend": "zai", "search_backend": "zai", "extract_backend": "zai"},
    )
    try:
        yield zai, brave
    finally:
        web_search_registry.restore_registration("zai", zai, previous["zai"])
        web_search_registry.restore_registration(
            "brave-free", brave, previous["brave-free"]
        )
        fb._reset_for_tests()


class TestExhaustionParse:
    def test_real_search_error_is_exhaustion(self):
        signal = fb.parse_exhaustion(ZAI_SEARCH_ERROR)
        assert signal is not None
        assert signal.code == "1310"
        # 03:09:48 Asia/Shanghai — the stamp carries no offset.
        assert signal.reset_at == datetime(
            2026, 9, 24, 3, 9, 48, tzinfo=timezone(timedelta(hours=8))
        )

    def test_real_extract_error_is_exhaustion(self):
        signal = fb.parse_exhaustion(ZAI_EXTRACT_ERROR)
        assert signal is not None and signal.code == "1310"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            None,
            "HTTP 500 upstream exploded",
            "Brave Search returned HTTP 429",  # 1 qps free tier, self-heals
            "MCP error -429: too many requests",  # transient, names no reset
        ],
    )
    def test_transient_failures_do_not_earn_a_cooldown(self, text):
        assert fb.parse_exhaustion(text) is None

    def test_unparseable_reset_falls_back_to_default_cooldown(self, backends):
        signal = fb.parse_exhaustion(
            'error {"code":"1310"} Limit Exhausted, no reset given'
        )
        assert signal is not None and signal.reset_at is None
        until = fb.mark_exhausted("zai", signal)
        assert until > datetime.now(timezone.utc)
        assert fb.is_exhausted("zai")


class TestCooldownRouting:
    def test_first_1310_parks_zai_and_brave_serves(self, backends):
        zai, brave = backends
        payload = json.loads(web_tools.web_search_tool("lee county news", limit=3))
        assert payload["success"] is True
        assert payload["data"]["web"][0]["url"] == "https://brave.example"
        assert zai.search_calls == 1 and brave.search_calls == 1
        assert fb.is_exhausted("zai")

    def test_second_call_never_touches_the_exhausted_backend(self, backends):
        zai, brave = backends
        web_tools.web_search_tool("first", limit=3)
        web_tools.web_search_tool("second", limit=3)
        assert zai.search_calls == 1, "exhausted backend must not be called again"
        assert brave.search_calls == 2

    def test_cooldown_persists_across_a_restart(self, backends):
        web_tools.web_search_tool("first", limit=3)
        assert fb._state_path().exists()
        fb._reset_for_tests()  # simulate a fresh gateway process
        assert fb.is_exhausted("zai"), "cooldown must survive a restart"

    def test_cooldown_lapses_when_the_reset_time_passes(self, backends):
        signal = fb.ExhaustionSignal(
            code="1310",
            reset_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            message="",
        )
        fb.mark_exhausted("zai", signal)
        assert fb.is_exhausted("zai")
        fb._cooldowns["zai"]["until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert not fb.is_exhausted("zai")

    def test_brave_is_not_offered_for_extract(self, backends):
        assert fb.pick_fallback_provider("search", exclude="zai") is not None
        assert fb.pick_fallback_provider("extract", exclude="zai") is None

    @pytest.mark.asyncio
    async def test_extract_during_cooldown_skips_zai_for_the_keyless_ring(
        self, backends, monkeypatch
    ):
        zai, _ = backends
        fb.mark_exhausted("zai", fb.parse_exhaustion(ZAI_EXTRACT_ERROR))
        rescued = []

        def _fake_rescue(provider_name, urls, results, **kwargs):
            rescued.append(provider_name)
            return [
                {"url": u, "title": "ok", "content": "body", "error": ""} for u in urls
            ]

        monkeypatch.setattr(web_tools, "_rescue_extract", _fake_rescue)
        results, served, fallback_from, code = await web_tools._dispatch_extract(
            zai, ["https://example.com/a"], "markdown"
        )
        assert zai.extract_calls == 0, "exhausted backend must not be called"
        assert served == "keyless" and fallback_from == "zai"
        assert rescued == ["zai"] and results[0]["content"] == "body"


class TestSearchAuditRow:
    EXPECTED_KEYS = {
        "schema", "observed_at", "harness", "session_id", "job_id", "tool",
        "backend", "query_sha256", "url_count", "result_count", "status",
        "error_code", "latency_ms", "fallback_from",
    }

    def _rows(self):
        path = fb._search_audit_path()
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_row_shape_and_fallback_attribution(self, backends):
        web_tools.web_search_tool("lee county news", limit=3)
        rows = self._rows()
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == self.EXPECTED_KEYS
        assert row["schema"] == "search_event.v1"
        assert row["harness"] == "hermes"
        assert row["tool"] == "web_search"
        # brave-free registers hyphenated; the ledger vocabulary says "brave".
        assert row["backend"] == "brave"
        assert row["fallback_from"] == "zai"
        assert row["status"] == "ok"
        assert row["error_code"] == "1310"
        assert row["url_count"] == 0 and row["result_count"] == 1
        assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0
        # ISO-8601 WITH an offset.
        assert datetime.fromisoformat(row["observed_at"]).tzinfo is not None

    def test_query_is_hashed_never_recorded(self, backends):
        query = "luis ramirez lee county debt"
        web_tools.web_search_tool(query, limit=3)
        raw = fb._search_audit_path().read_text(encoding="utf-8")
        assert query not in raw, "the query text must never reach the ledger"
        assert self._rows()[0]["query_sha256"] == fb.query_sha256(query)

    @pytest.mark.asyncio
    async def test_extract_row_counts_urls_and_hides_them(self, backends, monkeypatch):
        zai, _ = backends
        monkeypatch.setattr(
            web_tools, "_rescue_extract",
            lambda name, urls, results, **kw: [
                {"url": u, "title": "", "content": "body", "error": ""} for u in urls
            ],
        )
        urls = ["https://secret.example/a", "https://secret.example/b"]
        _, served, fallback_from, code = await web_tools._dispatch_extract(
            zai, urls, "markdown"
        )
        fb.write_search_audit(
            tool="web_extract", backend=served, status=fb.audit_status(True, code),
            latency_ms=12.7, query=None, url_count=len(urls), result_count=2,
            error_code=code, fallback_from=fallback_from,
        )
        row = self._rows()[-1]
        assert set(row) == self.EXPECTED_KEYS
        assert row["tool"] == "web_extract" and row["backend"] == "keyless"
        assert row["query_sha256"] is None
        assert row["url_count"] == 2 and row["result_count"] == 2
        assert "secret.example" not in fb._search_audit_path().read_text(encoding="utf-8")

    def test_audit_write_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            fb, "_search_audit_path",
            lambda: (_ for _ in ()).throw(RuntimeError("no home")),
        )
        fb.write_search_audit(
            tool="web_search", backend="zai", status="error", latency_ms=1
        )  # must not raise
