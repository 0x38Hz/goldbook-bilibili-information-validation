import pytest

from goldbook.config import Settings


def test_defaults_are_local_and_minimax_concurrency_is_three(tmp_path):
    settings = Settings.from_env({"GOLDBOOK_DATA_DIR": str(tmp_path)})

    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8765
    assert settings.lookback_days == 183
    assert settings.minimax_model == "MiniMax-M3"
    assert settings.minimax_max_concurrency == 3
    assert settings.whisper_model == "small"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.test"])
def test_rejects_non_loopback_binding(tmp_path, host):
    with pytest.raises(ValueError, match="local-only"):
        Settings.from_env(
            {
                "GOLDBOOK_DATA_DIR": str(tmp_path),
                "WEB_HOST": host,
            }
        )


def test_accepts_minimax_concurrency_of_twenty(tmp_path):
    settings = Settings.from_env(
        {
            "GOLDBOOK_DATA_DIR": str(tmp_path),
            "MINIMAX_MAX_CONCURRENCY": "20",
        }
    )

    assert settings.minimax_max_concurrency == 20


def test_rejects_minimax_concurrency_above_twenty(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 20"):
        Settings.from_env(
            {
                "GOLDBOOK_DATA_DIR": str(tmp_path),
                "MINIMAX_MAX_CONCURRENCY": "21",
            }
        )


def test_rejects_minimax_concurrency_below_one(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 20"):
        Settings.from_env(
            {
                "GOLDBOOK_DATA_DIR": str(tmp_path),
                "MINIMAX_MAX_CONCURRENCY": "0",
            }
        )
