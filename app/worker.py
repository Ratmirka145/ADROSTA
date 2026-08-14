from __future__ import annotations

import argparse
import logging
import time

from app.config import ConfigError, get_settings
from app.container import ApplicationContext
from app.observability import configure_logging


logger = logging.getLogger("adrosta.outbox")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="Доставка сохранённых заказов в настроенный webhook.",
    )
    parser.add_argument("--once", action="store_true", help="Обработать одну пачку")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = get_settings()
        configure_logging(settings.log_level)
        context = ApplicationContext.build(settings)
        processor = context.outbox_processor()
    except (ConfigError, RuntimeError):
        logger.error("worker_configuration_invalid")
        return 2

    try:
        while True:
            summary = processor.process_once()
            if summary.claimed:
                logger.info(
                    "outbox_batch claimed=%s delivered=%s retry=%s failed=%s",
                    summary.claimed,
                    summary.delivered,
                    summary.retry_scheduled,
                    summary.failed,
                )
            if args.once:
                return 0
            time.sleep(settings.outbox_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        context.close()


if __name__ == "__main__":
    raise SystemExit(main())
