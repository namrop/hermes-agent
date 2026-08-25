"""Behavior contract for the configurable OpenAI-compatible STT timeout.

Ported divergence from primary ``16ecf24006``: the OpenAI STT client
hardcoded ``timeout=30`` — long clips exceed that on upload+transcription
and surface as opaque ``Request timeout`` errors on healthy requests.

Resolution contract under test:

  - ``stt.openai.timeout_seconds`` from config.yaml is honored.
  - Missing section, missing key, or non-positive/garbage values fall back
    to the 120s default — never to zero/negative (which would disable the
    request outright) and never raise.
  - The resolved value reaches the actual ``OpenAI(...)`` construction in
    ``_transcribe_openai`` (which also serves every OpenAI-compatible STT
    endpoint: DeepInfra et al).

Also covers the sibling retained delta from the same primary commit: local
STT command discovery must include the NixOS system profile bin dir so
whisper CLIs installed through NixOS are found on Linux hosts.
"""

import pytest

from tools.transcription_tools import (
    COMMON_LOCAL_BIN_DIRS,
    _get_openai_timeout_seconds,
)


class TestTimeoutResolution:
    def test_explicit_config_value_honored(self):
        cfg = {"openai": {"timeout_seconds": 300}}
        assert _get_openai_timeout_seconds(cfg) == 300

    def test_missing_section_falls_back_to_default(self):
        assert _get_openai_timeout_seconds({}) == 120

    def test_missing_key_falls_back_to_default(self):
        assert _get_openai_timeout_seconds({"openai": {}}) == 120

    def test_none_config_falls_back_to_default(self):
        assert _get_openai_timeout_seconds(None) in (
            _get_openai_timeout_seconds({}),  # same default either way
            120,
        )

    @pytest.mark.parametrize("bad", [0, -5, "abc", None])
    def test_garbage_values_fall_back_to_default(self, bad):
        assert _get_openai_timeout_seconds({"openai": {"timeout_seconds": bad}}) == 120

    def test_float_value_truncates(self):
        # Same coercion semantics as primary's _coerce_positive_int:
        # int() truncation, fallback only on non-positive/non-numeric.
        assert _get_openai_timeout_seconds({"openai": {"timeout_seconds": 3.5}}) == 3

    def test_numeric_string_is_coerced(self):
        assert _get_openai_timeout_seconds({"openai": {"timeout_seconds": "240"}}) == 240

    def test_non_dict_openai_section_is_tolerated(self):
        assert _get_openai_timeout_seconds({"openai": "broken"}) == 120


class _RecorderClient:
    """Stands in for openai.OpenAI; records constructor kwargs."""

    instances = []  # reset per-test via fixture
    audio: object = None

    def __init__(self, **kwargs):
        self.constructor_kwargs = kwargs
        type(self).instances.append(self)

    # ``_transcribe_openai`` finally-block calls client.close() when present.
    def close(self):
        pass


class TestClientConstructionWiring:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        _RecorderClient.instances = []
        real_openai = pytest.importorskip("openai")

        def _factory(**kwargs):
            client = _RecorderClient(**kwargs)
            # Mirror the attribute path the real client exposes:
            # client.audio.transcriptions.create(**kwargs)
            class _Transcriptions:
                @staticmethod
                def create(**_kwargs):
                    return "recorded transcript"

            class _Audio:
                transcriptions = _Transcriptions()

            client.audio = _Audio()
            return client

        monkeypatch.setattr(real_openai, "OpenAI", _factory)
        self.audio_file = tmp_path / "clip.ogg"
        self.audio_file.write_bytes(b"fake-audio-bytes")
        yield

    def _transcribe(self, stt_cfg):
        from tools.transcription_tools import _transcribe_openai

        if stt_cfg is None:
            return _transcribe_openai(
                str(self.audio_file), "whisper-1",
                api_key="test-key", base_url="https://example.invalid/v1",
            )
        import tools.transcription_tools as tt

        original = tt._get_openai_timeout_seconds
        tt._get_openai_timeout_seconds = lambda cfg=None: original(stt_cfg)
        try:
            return _transcribe_openai(
                str(self.audio_file), "whisper-1",
                api_key="test-key", base_url="https://example.invalid/v1",
            )
        finally:
            tt._get_openai_timeout_seconds = original

    def test_configured_timeout_reaches_client_constructor(self):
        result = self._transcribe({"openai": {"timeout_seconds": 300}})
        assert result["success"] is True
        assert result["transcript"] == "recorded transcript"
        assert len(_RecorderClient.instances) == 1
        assert _RecorderClient.instances[0].constructor_kwargs["timeout"] == 300

    def test_default_timeout_reaches_client_constructor(self):
        result = self._transcribe({})
        assert result["success"] is True
        assert _RecorderClient.instances[0].constructor_kwargs["timeout"] == 120


class TestNixosLocalBinDiscovery:
    def test_nixos_system_profile_bin_dir_is_discovered(self):
        # Sol (and any NixOS host) exposes whisper CLIs through
        # /run/current-system/sw/bin — without it, local STT command
        # auto-detection silently fails on NixOS.
        assert "/run/current-system/sw/bin" in COMMON_LOCAL_BIN_DIRS

    def test_macOS_and_linux_conventional_dirs_retained(self):
        assert "/opt/homebrew/bin" in COMMON_LOCAL_BIN_DIRS
        assert "/usr/local/bin" in COMMON_LOCAL_BIN_DIRS
