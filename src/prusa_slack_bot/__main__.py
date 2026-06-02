"""Bot entrypoint: validate config, wire components, run forever."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from . import logging_setup
from .config import ConfigError, load_config
from .db import Database
from .poller import Poller
from .prusalink import PrusaLinkClient, PrusaLinkUnreachable
from .single_instance import AlreadyRunning, SingleInstanceLock
from .slack_app import BotApp

logger = logging.getLogger("prusa_slack_bot")


async def _validate_connectivity(client: PrusaLinkClient) -> None:
    """Probe the printer once at startup so config errors surface immediately."""

    try:
        await client.get_snapshot()
    except PrusaLinkUnreachable as exc:
        raise SystemExit(
            f"Can't reach PrusaLink: {exc}. Check PRUSALINK_HOST and that the "
            "printer is powered on and reachable from this machine."
        ) from exc


async def run() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        return 2

    logging_setup.configure(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        extra_secrets=[
            config.slack_bot_token,
            config.slack_app_token,
            config.prusalink_api_key or "",
            config.prusalink_password or "",
        ],
    )
    logger.info("prusa-slack-bot starting (poll every %ds)", config.poll_interval_seconds)

    lock = SingleInstanceLock(config.db_path.with_suffix(config.db_path.suffix + ".lock"))
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        logger.error("%s", exc)
        return 3

    db = Database(config.db_path)
    await db.open()

    prusalink = PrusaLinkClient(
        base_url=config.prusalink_base_url,
        api_key=config.prusalink_api_key,
        username=config.prusalink_username,
        password=config.prusalink_password,
    )
    await prusalink.open()
    await _validate_connectivity(prusalink)

    bot_holder: dict[str, BotApp] = {}

    def current_snapshot():  # closure that the bot uses to do stale-action checks
        poller = bot_holder.get("poller")
        return poller.current_snapshot if poller else None  # type: ignore[union-attr]

    bot = BotApp(config=config, db=db, prusalink=prusalink, get_current_snapshot=current_snapshot)
    await bot.initialize()

    poller = Poller(
        config_poll_interval=config.poll_interval_seconds,
        config_offline_after=config.offline_after_failures,
        prusalink=prusalink,
        bot=bot,
        db=db,
    )
    bot_holder["poller"] = poller  # type: ignore[assignment]

    socket_handler = AsyncSocketModeHandler(bot.app, config.slack_app_token)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop():
        logger.info("Shutdown requested.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    poll_task = asyncio.create_task(poller.run(), name="poller")
    socket_task = asyncio.create_task(socket_handler.start_async(), name="socket-mode")

    try:
        done, pending = await asyncio.wait(
            {poll_task, socket_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t.exception() is not None:
                logger.error("Task crashed: %s", t.exception())
    finally:
        poller.stop()
        try:
            await socket_handler.close_async()
        except Exception:
            pass
        for t in (poll_task, socket_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        await prusalink.close()
        await db.close()
        lock.release()

    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
