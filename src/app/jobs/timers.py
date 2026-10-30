"""The timer job process: ``python -m app.jobs.timers [--once]``.

Without ``--once`` it runs :func:`app.services.timers.run_timers` every 60 seconds
until SIGINT or SIGTERM, then exits cleanly (status 0). Each run logs its
:class:`~app.services.timers.TimerReport` as one line. Notifications go to the
:class:`~app.services.notify.LoggingNotifier`.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Sequence

from app.core.clock import SystemClock
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_database_engine, create_database_sessionmaker
from app.services.notify import LoggingNotifier
from app.services.timers import run_timers

INTERVAL_SECONDS = 60.0
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)

logger = get_logger("timers")


async def serve(
    settings: Settings,
    *,
    once: bool,
    interval: float = INTERVAL_SECONDS,
    stop: asyncio.Event | None = None,
) -> int:
    """Run the timers once, or every ``interval`` seconds until ``stop`` or a signal."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    if not once:
        for signum in _STOP_SIGNALS:
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)

    engine = create_database_engine(settings)
    session_factory = create_database_sessionmaker(engine)
    clock = SystemClock()
    notifier = LoggingNotifier()
    try:
        while True:
            report = await run_timers(
                session_factory, clock=clock, notifier=notifier, settings=settings
            )
            logger.info(
                "timer_run expired_holds=%d expired_deposits=%d completed=%d flagged_at_risk=%d",
                report.expired_holds,
                report.expired_deposits,
                report.completed,
                report.flagged_at_risk,
            )
            if once:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
            logger.info("timer_stopped")
            break
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.timers")
    parser.add_argument("--once", action="store_true", help="run the timers once and exit")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.debug)
    return asyncio.run(serve(settings, once=args.once))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
