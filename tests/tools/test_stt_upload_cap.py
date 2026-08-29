"""stt.max_upload_mb — configurable remote-upload cap (2026-08-28 lantern incident).

The 25MB constant was a cloud-API prior applied to self-hosted gateways; the
cap must be raisable per-deployment, default unchanged, and the rejection
message must name the config key so an agent can adapt.
"""
import tools.transcription_tools as tt


def _mkfile(tmp_path, mb):
    p = tmp_path / "big.m4a"
    p.write_bytes(b"\0" * (mb * 1024 * 1024))
    return p


def test_default_cap_is_25mb(monkeypatch, tmp_path):
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {})
    err = tt._validate_audio_file_size(_mkfile(tmp_path, 26))
    assert err is not None and "File too large" in err["error"]
    assert "stt.max_upload_mb" in err["error"]


def test_under_default_cap_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {})
    assert tt._validate_audio_file_size(_mkfile(tmp_path, 24)) is None


def test_raised_cap_accepts_oversized(monkeypatch, tmp_path):
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {"max_upload_mb": 200})
    assert tt._validate_audio_file_size(_mkfile(tmp_path, 29)) is None


def test_raised_cap_still_rejects_beyond(monkeypatch, tmp_path):
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {"max_upload_mb": 27})
    err = tt._validate_audio_file_size(_mkfile(tmp_path, 29))
    assert err is not None and "max 27MB" in err["error"]


def test_bogus_config_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {"max_upload_mb": "not-a-number"})
    err = tt._validate_audio_file_size(_mkfile(tmp_path, 26))
    assert err is not None and "max 25MB" in err["error"]
