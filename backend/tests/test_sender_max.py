from app.execution.onchain import sender_max


def test_sender_max_enabled_by_default(monkeypatch):
    monkeypatch.delenv("HELIUS_SENDER_ENABLED", raising=False)
    assert sender_max.enabled() is True


def test_sender_max_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HELIUS_SENDER_ENABLED", "false")
    assert sender_max.enabled() is False
