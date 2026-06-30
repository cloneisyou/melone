import os

import pytest

from melone_service.config import load_config, load_env_file, resolve_app_data_dir
from melone_service.settings import app_settings_path, update_screen_text_settings


def test_resolve_app_data_dir_defaults_to_macos_app_support(tmp_path):
    assert resolve_app_data_dir(env={}, home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "Melone"
    )


def test_load_config_uses_melone_home(monkeypatch, tmp_path):
    data_dir = tmp_path / "melone-dev"
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("MELONE_") and name != "MELONE_HOME":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MELONE_HOME", str(data_dir))

    config = load_config()

    assert config.data_dir == data_dir
    assert config.database_path == data_dir / "melone.sqlite"
    assert config.pid_file_path == data_dir / "melone.pid"
    assert config.lock_file_path == data_dir / "melone.lock"
    assert config.logs_dir == data_dir / "logs"
    assert config.screenshots_dir == data_dir / "screenshots"
    assert config.settings_path == data_dir / "settings.json"
    assert config.logs_dir.is_dir()
    assert config.screenshots_dir.is_dir()
    assert not config.database_path.exists()
    assert config.polling_interval_seconds == 1.0
    assert config.idle_timeout_seconds == 300
    assert config.activity_active_window_seconds == 30
    assert config.screenshot_min_interval_seconds == 10
    assert config.ocr_provider == "apple_vision"
    assert config.ocr_endpoint == "http://127.0.0.1:8000/v1"
    assert config.ocr_model == "lightonai/LightOnOCR-2-1B"
    assert config.ocr_timeout_seconds == 120.0
    assert config.ocr_max_tokens == 4096
    assert config.screen_text_search_enabled is False
    assert config.screenshot_collector_enabled is False
    assert config.screen_search_workers_enabled is False
    assert config.screenshot_collector_development_override is None
    assert config.screen_search_workers_development_override is None
    assert config.screen_search_max_jobs_per_tick == 2
    assert config.screen_search_retry_backoff_seconds == 60
    assert config.screen_search_high_backlog_threshold == 20
    assert config.screen_search_very_high_backlog_threshold == 100
    assert config.screen_text_retain_screenshots is False
    assert config.semantic_search_enabled is False
    assert config.embedding_model == "google/embeddinggemma-300m"
    assert config.embedding_dimension == 256
    assert config.embedding_batch_size == 16
    assert config.semantic_search_candidate_limit == 50


def test_load_config_reads_local_vllm_env(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_OCR_PROVIDER": " local_vllm ",
            "MELONE_OCR_ENDPOINT": "http://localhost:9000/v1/",
            "MELONE_OCR_MODEL": "custom-ocr-model",
            "MELONE_OCR_TIMEOUT_SECONDS": "30.5",
            "MELONE_OCR_MAX_TOKENS": "2048",
        }
    )

    assert config.ocr_provider == "local_vllm"
    assert config.ocr_endpoint == "http://localhost:9000/v1/"
    assert config.ocr_model == "custom-ocr-model"
    assert config.ocr_timeout_seconds == 30.5
    assert config.ocr_max_tokens == 2048


def test_load_config_defaults_context_rank_refresh_interval(tmp_path):
    config = load_config(env={"MELONE_HOME": str(tmp_path)})

    assert config.context_rank_refresh_min_interval_seconds == 30.0


def test_load_config_reads_context_rank_refresh_interval_env(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS": "5.5",
        }
    )

    assert config.context_rank_refresh_min_interval_seconds == 5.5


def test_load_config_defaults_ocr_languages_to_korean_and_english(tmp_path):
    config = load_config(env={"MELONE_HOME": str(tmp_path)})

    assert config.ocr_recognition_languages == ("ko-KR", "en-US")


def test_load_config_reads_custom_ocr_languages(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_OCR_LANGUAGES": " en-US , ja-JP ",
        }
    )

    assert config.ocr_recognition_languages == ("en-US", "ja-JP")


def test_load_config_ocr_languages_auto_clears_list(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_OCR_LANGUAGES": "auto",
        }
    )

    assert config.ocr_recognition_languages == ()


def test_load_config_ocr_languages_separators_only_falls_back_to_default(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_OCR_LANGUAGES": " , ",
        }
    )

    assert config.ocr_recognition_languages == ("ko-KR", "en-US")


def test_load_config_defaults_language_correction_off(tmp_path):
    config = load_config(env={"MELONE_HOME": str(tmp_path)})

    assert config.ocr_language_correction is False


def test_load_config_reads_language_correction_env(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_OCR_LANGUAGE_CORRECTION": "true",
        }
    )

    assert config.ocr_language_correction is True


def test_load_config_accepts_legacy_vlm_env_names(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_VLM_PROVIDER": "local_vllm",
            "MELONE_VLM_ENDPOINT": "http://localhost:9001/v1",
            "MELONE_VLM_MODEL": "legacy-model",
            "MELONE_VLM_TIMEOUT_SECONDS": "45",
            "MELONE_VLM_MAX_TOKENS": "1024",
        }
    )

    assert config.ocr_provider == "local_vllm"
    assert config.ocr_endpoint == "http://localhost:9001/v1"
    assert config.ocr_model == "legacy-model"
    assert config.ocr_timeout_seconds == 45.0
    assert config.ocr_max_tokens == 1024


def test_load_config_reads_screen_search_worker_env(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_SCREENSHOT_COLLECTOR_ENABLED": "yes",
            "MELONE_SCREEN_SEARCH_WORKERS_ENABLED": "true",
            "MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK": "5",
            "MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS": "12",
            "MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD": "7",
            "MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD": "11",
            "MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS": "enabled",
        }
    )

    assert config.screenshot_collector_enabled is True
    assert config.screen_search_workers_enabled is True
    assert config.screenshot_collector_development_override is True
    assert config.screen_search_workers_development_override is True
    assert config.screenshot_collector_development_override_enabled is True
    assert config.screen_search_workers_development_override_enabled is True
    assert config.screen_search_max_jobs_per_tick == 5
    assert config.screen_search_retry_backoff_seconds == 12
    assert config.screen_search_high_backlog_threshold == 7
    assert config.screen_search_very_high_backlog_threshold == 11
    assert config.screen_text_retain_screenshots is True


def test_load_config_reads_semantic_search_env(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_SEMANTIC_SEARCH_ENABLED": "true",
            "MELONE_EMBEDDING_MODEL": " custom-embedding-model ",
            "MELONE_EMBEDDING_DIMENSION": "512",
            "MELONE_EMBEDDING_BATCH_SIZE": "8",
            "MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT": "25",
        }
    )

    assert config.semantic_search_enabled is True
    assert config.embedding_model == "custom-embedding-model"
    assert config.embedding_dimension == 512
    assert config.embedding_batch_size == 8
    assert config.semantic_search_candidate_limit == 25


@pytest.mark.parametrize("dimension", ["128", "256", "512", "768"])
def test_load_config_accepts_supported_embedding_dimensions(tmp_path, dimension):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_EMBEDDING_DIMENSION": dimension,
        }
    )

    assert config.embedding_dimension == int(dimension)


def test_load_config_rejects_unsupported_embedding_dimension(tmp_path):
    with pytest.raises(
        ValueError,
        match=(
            "MELONE_EMBEDDING_DIMENSION must be one of "
            "128, 256, 512, or 768"
        ),
    ):
        load_config(
            env={
                "MELONE_HOME": str(tmp_path),
                "MELONE_EMBEDDING_DIMENSION": "384",
            }
        )


def test_load_config_reads_screen_text_product_setting(tmp_path):
    update_screen_text_settings(app_settings_path(tmp_path), enabled=True)

    config = load_config(env={"MELONE_HOME": str(tmp_path)})

    assert config.screen_text_search_enabled is True
    assert config.screenshot_collector_enabled is True
    assert config.screen_search_workers_enabled is True
    assert config.screenshot_collector_development_override is None
    assert config.screen_search_workers_development_override is None


def test_env_false_overrides_enabled_screen_text_product_setting(tmp_path):
    update_screen_text_settings(app_settings_path(tmp_path), enabled=True)

    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_SCREENSHOT_COLLECTOR_ENABLED": "false",
            "MELONE_SCREEN_SEARCH_WORKERS_ENABLED": "false",
        }
    )

    assert config.screen_text_search_enabled is True
    assert config.screenshot_collector_enabled is False
    assert config.screen_search_workers_enabled is False
    assert config.screenshot_collector_development_override is False
    assert config.screen_search_workers_development_override is False


def test_load_env_file_reads_dotenv_without_overriding_existing_env(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("MELONE_HOME=/tmp/from-dotenv\n", encoding="utf-8")

    previous_value = os.environ.pop("MELONE_HOME", None)
    try:
        assert load_env_file(dotenv_path) is True
        assert os.environ["MELONE_HOME"] == "/tmp/from-dotenv"

        os.environ["MELONE_HOME"] = "/tmp/from-shell"
        assert load_env_file(dotenv_path) is True
        assert os.environ["MELONE_HOME"] == "/tmp/from-shell"
    finally:
        if previous_value is None:
            os.environ.pop("MELONE_HOME", None)
        else:
            os.environ["MELONE_HOME"] = previous_value
