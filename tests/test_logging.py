import logging

import pytest

from desk.log_setup import MASK, redact, setup_logging

TG_TOKEN = "123456789:AAH4kPq_ZfakeTokenValueForTesting1234567"


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in saved_handlers:
            root.removeHandler(h)
            h.close()
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)


def test_redacts_known_secrets():
    assert redact("key is hunter2secret!", ["hunter2secret"]) == f"key is {MASK}!"


def test_short_values_not_treated_as_secrets():
    assert redact("abc", ["abc"]) == "abc"      # too short to mask safely


def test_redacts_patterns_without_being_told():
    assert "sk-ant-api03-abcdef123456" not in redact("using sk-ant-api03-abcdef123456 now")
    assert TG_TOKEN not in redact(f"token {TG_TOKEN}")
    assert "SECRETTOKEN" not in redact(f"POST https://api.telegram.org/botSECRETTOKEN/sendMessage")
    assert "abc123def" not in redact("GET https://cryptopanic.com/api/v2/posts/?auth_token=abc123def&x=1")
    assert "deadbeef" * 8 not in redact(str({"key": "MYAPIKEY123", "sign": "deadbeef" * 8}))
    assert "MYAPIKEY123" not in redact(str({"key": "MYAPIKEY123", "sign": "deadbeef" * 8}))


def test_file_log_masks_messages_args_and_tracebacks(tmp_path):
    secret = "Sup3rS3cretValue"
    log = setup_logging(tmp_path, process_name="t", secrets=[secret], console=False)
    log.info("plain %s", secret)
    try:
        raise ValueError(f"failed with {secret}")
    except ValueError:
        log.exception("oops")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert secret not in text
    assert text.count(MASK) >= 2
    assert "ValueError" in text


def test_rotation_settings(tmp_path):
    setup_logging(tmp_path, process_name="r", max_mb=1, backups=3, console=False)
    fh = [h for h in logging.getLogger().handlers if hasattr(h, "maxBytes")][0]
    assert fh.maxBytes == 1024 * 1024 and fh.backupCount == 3


def test_calling_twice_does_not_duplicate_handlers(tmp_path):
    setup_logging(tmp_path, process_name="d", console=True)
    setup_logging(tmp_path, process_name="d", console=True)
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_desk_handler", False)]
    assert len(ours) == 2


def test_log_file_actually_rotates(tmp_path):
    log = setup_logging(tmp_path, process_name="big", max_mb=0.01, backups=2, console=False)  # ~10 KB files
    for i in range(500):
        log.info("line %d %s", i, "x" * 100)
    files = sorted(p.name for p in tmp_path.glob("big.log*"))
    assert files == ["big.log", "big.log.1", "big.log.2"]      # never more than 1 + backups


def test_http_libraries_quietened(tmp_path):
    setup_logging(tmp_path, process_name="q", console=False)
    assert logging.getLogger("httpx").level == logging.WARNING
