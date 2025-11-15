from __future__ import annotations

import json
import tempfile
import unittest
import threading
from pathlib import Path

from lumivox_core.logger import (
    LoggingConfig,
    RotatingFileConfig,
    get_logger,
    shutdown_logging,
    configure_logging,
)


class LoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        shutdown_logging()

    def test_bound_context_and_redaction_are_written_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.log"
            configure_logging(
                LoggingConfig(
                    application="test-app",
                    console=False,
                    file=RotatingFileConfig(path),
                )
            )

            get_logger(request_id="request-1").bind(component="worker").info(
                "work_completed", authorization="Bearer secret", payload={"token": "hidden"}
            )
            shutdown_logging()

            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["application"], "test-app")
            self.assertEqual(event["request_id"], "request-1")
            self.assertEqual(event["component"], "worker")
            self.assertEqual(event["authorization"], "[REDACTED]")
            self.assertEqual(event["payload"]["token"], "[REDACTED]")

    def test_exception_contains_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.log"
            configure_logging(
                LoggingConfig(
                    application="test-app",
                    console=False,
                    file=RotatingFileConfig(path),
                )
            )

            try:
                raise ValueError("expected failure")
            except ValueError:
                get_logger().exception("operation_failed")
            shutdown_logging()

            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["event"], "operation_failed")
            self.assertIn("ValueError: expected failure", event["exception"])

    def test_file_level_can_be_more_verbose_than_default_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.log"
            configure_logging(
                LoggingConfig(
                    application="test-app",
                    console=False,
                    file=RotatingFileConfig(path, level="DEBUG"),
                )
            )

            get_logger().debug("debug_event")
            shutdown_logging()

            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["event"], "debug_event")

    def test_http_header_variants_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.log"
            configure_logging(
                LoggingConfig(
                    application="test-app",
                    console=False,
                    file=RotatingFileConfig(path),
                )
            )

            get_logger().info(
                "httpx_response",
                headers={"Set-Cookie": "session=secret", "X-API-Key": "secret-key"},
            )
            shutdown_logging()

            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["headers"]["Set-Cookie"], "[REDACTED]")
            self.assertEqual(event["headers"]["X-API-Key"], "[REDACTED]")

    def test_file_rotation_keeps_configured_number_of_backups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.log"
            configure_logging(
                LoggingConfig(
                    application="test-app",
                    console=False,
                    file=RotatingFileConfig(path, max_bytes=200, backup_count=2),
                )
            )
            logger = get_logger()
            for index in range(10):
                logger.info("large_event", index=index, payload="x" * 200)
            shutdown_logging()

            self.assertTrue(path.exists())
            self.assertTrue(path.with_name("application.log.1").exists())
            self.assertTrue(path.with_name("application.log.2").exists())
            self.assertFalse(path.with_name("application.log.3").exists())

    def test_configuration_is_process_wide(self) -> None:
        configure_logging(LoggingConfig(application="test-app"))
        with self.assertRaisesRegex(RuntimeError, "already configured"):
            configure_logging(LoggingConfig(application="other-app"))

    def test_threaded_logging_delivers_every_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.log"
            configure_logging(
                LoggingConfig(
                    application="test-app",
                    console=False,
                    queue_size=2,
                    file=RotatingFileConfig(path),
                )
            )

            def write_events(worker: int) -> None:
                logger = get_logger(worker=worker)
                for index in range(25):
                    logger.info("thread_event", index=index)

            threads = [threading.Thread(target=write_events, args=(worker,)) for worker in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            shutdown_logging()

            events = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 100)


if __name__ == "__main__":
    unittest.main()
