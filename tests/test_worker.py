"""Issue #1: the Celery app, the task name and the settings the catalog depends on."""

from evilflowers_ocr_worker.settings import Settings
from evilflowers_ocr_worker.worker import CeleryConfig, create_app

CATALOG_OCR_TIME_LIMIT = 4 * 3600  # EVILFLOWERS_OCR_TIME_LIMIT default


def test_task_is_registered_under_the_old_name():
    app = create_app(Settings(_env_file=None))
    app.loader.import_default_modules()
    assert "evilflowers_ocr_worker.ocr" in app.tasks


def test_config_contract():
    settings = Settings(_env_file=None)
    config = CeleryConfig(settings)
    assert config.task_acks_late is True
    assert config.task_reject_on_worker_lost is True
    assert config.worker_send_task_events is True
    assert config.worker_prefetch_multiplier == 1
    assert config.task_default_queue == "evilflowers_ocr_worker"
    assert config.task_routes == {"evilflowers_ocr_worker.*": {"queue": "evilflowers_ocr_worker"}}
    assert settings.task_soft_time_limit < config.task_time_limit < CATALOG_OCR_TIME_LIMIT
    assert not hasattr(config, "task_soft_time_limit"), "the soft limit is the run's own deadline, not Celery's"
    assert config.broker_transport_options["visibility_timeout"] > config.task_time_limit


#: Copying the OCR'd PDF onto the shared volume and writing the outcome file, with room to spare.
WRITE_ALLOWANCE = 300


def test_soft_to_hard_gap_leaves_room_to_write_the_outcome():
    settings = Settings(_env_file=None)
    gap = settings.task_time_limit - settings.task_soft_time_limit
    # Worst case after the deadline: the page in flight runs to TESSERACT_TIMEOUT, then two files are written.
    assert gap >= settings.tesseract_timeout + WRITE_ALLOWANCE


def test_environment_names(monkeypatch):
    monkeypatch.setenv("BROKER", "redis://bridge:6379/7")
    monkeypatch.setenv("STORAGE_PATH", "/srv/data")
    monkeypatch.setenv("CATALOG_API_KEY", "left-over")  # an old deployment's variable is ignored, not refused
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("OCR_ENABLED_ENGINES", '["tesseract"]')
    monkeypatch.setenv("OCR_JOBS", "4")
    settings = Settings(_env_file=None)
    assert settings.redis_url == "redis://bridge:6379/7"
    assert str(settings.storage_path) == "/srv/data"
    assert settings.log_level == "DEBUG"
    assert settings.enabled_engines == ("tesseract",)
    assert settings.ocr_jobs == 4


def test_redis_url_is_accepted_too(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://other:6379/7")
    assert Settings(_env_file=None).redis_url == "redis://other:6379/7"
