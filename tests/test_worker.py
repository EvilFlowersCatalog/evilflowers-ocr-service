"""Issues #1 and #6: the Celery app the kit builds, the task name and the settings the catalog depends on."""

from evilflowers_ocr_worker.settings import POD_TERMINATION_GRACE, Settings
from evilflowers_ocr_worker.worker import app

CATALOG_OCR_TIME_LIMIT = 4 * 3600  # EVILFLOWERS_OCR_TIME_LIMIT default
#: Copying the OCR'd PDF onto the shared volume and writing the outcome file, with room to spare.
WRITE_ALLOWANCE = 300
#: The fallback requeue's Redis call (the kit's client: 5 s socket timeout), with room to spare.
REQUEUE_ALLOWANCE = 5


def test_task_is_registered_under_the_old_name():
    app.loader.import_default_modules()
    assert "evilflowers_ocr_worker.ocr" in app.tasks


def test_config_contract():
    settings = Settings(_env_file=None)
    conf = app.conf
    assert conf.worker_send_task_events is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_default_queue == "evilflowers_ocr_worker"
    assert conf.task_routes == {"evilflowers_ocr_worker.*": {"queue": "evilflowers_ocr_worker"}}
    # The kit's shutdown, not Celery's soft shutdown (which redid a finished book on dev).
    assert conf.worker_soft_shutdown_timeout == 0
    assert settings.task_soft_time_limit < settings.task_time_limit < CATALOG_OCR_TIME_LIMIT
    assert conf.broker_transport_options["visibility_timeout"] == 43_200 > settings.task_time_limit


def test_soft_to_hard_gap_leaves_room_to_write_the_outcome():
    settings = Settings(_env_file=None)
    gap = settings.task_time_limit - settings.task_soft_time_limit
    # Worst case after the deadline: the page in flight runs to TESSERACT_TIMEOUT, then two files are written.
    assert gap >= settings.tesseract_timeout + WRITE_ALLOWANCE


def test_shutdown_fits_in_the_pod_grace():
    """Grace + cancel past the pod's terminationGracePeriodSeconds: SIGKILL before the requeue, the book stuck 12 h."""
    settings = Settings(_env_file=None)
    used = settings.worker_soft_shutdown_timeout + settings.worker_cancel_timeout + REQUEUE_ALLOWANCE
    assert used <= POD_TERMINATION_GRACE
    assert settings.max_deliveries == 5


def test_environment_names(monkeypatch):
    monkeypatch.setenv("BROKER", "redis://bridge:6379/7")
    monkeypatch.setenv("OCR_QUEUE", "ocr-q")
    monkeypatch.setenv("STORAGE_PATH", "/srv/data")
    monkeypatch.setenv("CATALOG_API_KEY", "left-over")  # an old deployment's variable is ignored, not refused
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("OCR_ENABLED_ENGINES", '["tesseract"]')
    monkeypatch.setenv("OCR_JOBS", "4")
    monkeypatch.setenv("MAX_DELIVERIES", "3")
    settings = Settings(_env_file=None)
    assert settings.redis_url == "redis://bridge:6379/7"
    assert settings.queue == "ocr-q"
    assert str(settings.storage_path) == "/srv/data"
    assert settings.log_level == "DEBUG"
    assert settings.enabled_engines == ("tesseract",)
    assert settings.ocr_jobs == 4
    assert settings.max_deliveries == 3


def test_redis_url_and_queue_are_accepted_too(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    monkeypatch.delenv("OCR_QUEUE", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://other:6379/7")
    monkeypatch.setenv("QUEUE", "other-q")
    settings = Settings(_env_file=None)
    assert (settings.redis_url, settings.queue) == ("redis://other:6379/7", "other-q")
