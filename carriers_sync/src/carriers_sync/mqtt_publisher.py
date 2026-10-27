"""Long-lived MQTT connection used to publish discovery + state messages
and to receive button-press commands. Wraps aiomqtt.Client.

A background task owns the connection and reconnects with backoff whenever
the broker goes away (e.g. Mosquitto restarting for an update). Publishing
waits for a live connection and retries a message lost to a disconnect, so
a broker restart delays a sync instead of killing the App.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType

from aiomqtt import Client, MqttError, Will

from carriers_sync.discovery import AVAILABILITY_TOPIC, MqttMessage

logger = logging.getLogger("carriers_sync.mqtt")

# Single wildcard subscription matches all provider/account refresh buttons
# — `carriers_sync/<provider>/<account>/refresh/cmd` — plus refresh-all.
COMMAND_TOPICS = ("carriers_sync/refresh_all/cmd", "carriers_sync/+/+/refresh/cmd")


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str | None
    password: str | None


@dataclass(frozen=True)
class RefreshCommand:
    """A button press received via MQTT. account_id=None means refresh-all."""

    account_id: str | None


@dataclass(frozen=True)
class ReconnectPolicy:
    # Delay before each reconnect attempt; the last one repeats until the
    # broker is back.
    backoffs: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
    # Tries per message before publish_many gives up and raises.
    publish_attempts: int = 5
    # After a failed publish, how long to wait for the connection task to
    # notice the drop before retrying (on the same connection if it didn't).
    disconnect_grace: float = 15.0


_DEFAULT_RECONNECT_POLICY = ReconnectPolicy()


@dataclass
class _Connection:
    client: Client
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class MqttPublisher:
    def __init__(
        self,
        cfg: MqttConfig,
        reconnect_policy: ReconnectPolicy = _DEFAULT_RECONNECT_POLICY,
    ) -> None:
        self._cfg = cfg
        self._policy = reconnect_policy
        self._conn: _Connection | None = None
        self._connected = asyncio.Event()
        self._subscriptions: tuple[str, ...] = ()
        self._commands: asyncio.Queue[RefreshCommand] = asyncio.Queue()
        self._runner: asyncio.Task[None] | None = None

    async def __aenter__(self) -> MqttPublisher:
        self._runner = asyncio.create_task(self._run())
        await self._connected.wait()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._runner is None:
            return
        conn = self._conn
        if conn is not None:
            # Best effort: if the connection is already dead the broker
            # publishes our will ("offline") instead.
            with contextlib.suppress(MqttError):
                await conn.client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        self._runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._runner
        self._runner = None

    async def publish_many(self, messages: list[MqttMessage]) -> None:
        assert self._runner is not None, "use as async context manager"
        for m in messages:
            payload = (
                json.dumps(m.payload, ensure_ascii=False)
                if isinstance(m.payload, dict)
                else str(m.payload)
            )
            await self._publish(m.topic, payload, retain=m.retain)

    async def subscribe_commands(self, *, account_ids: list[str]) -> None:
        # The account_ids parameter is retained for compatibility but no longer
        # needed to enumerate per-account topics (see COMMAND_TOPICS).
        assert self._runner is not None
        del account_ids  # unused; kept for API stability
        # Remembered so the connection task resubscribes after a reconnect:
        # the session isn't persistent, so the broker forgets subscriptions.
        self._subscriptions = COMMAND_TOPICS
        conn = await self._live_connection()
        try:
            for topic in COMMAND_TOPICS:
                await conn.client.subscribe(topic)
        except MqttError as e:
            logger.warning("subscribe failed (%s); will resubscribe on reconnect", e)

    async def commands(self) -> AsyncIterator[RefreshCommand]:
        assert self._runner is not None
        while True:
            yield await self._commands.get()

    async def _publish(self, topic: str, payload: str, *, retain: bool) -> None:
        for attempt in range(1, self._policy.publish_attempts + 1):
            conn = await self._live_connection()
            try:
                await conn.client.publish(topic, payload, qos=1, retain=retain)
                return
            except MqttError as e:
                if attempt == self._policy.publish_attempts:
                    raise
                logger.warning("publish to %s failed (%s), retrying", topic, e)
                # Usually the broker went away. Give the connection task a
                # moment to notice, so the retry goes out on the new connection.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(conn.closed.wait(), self._policy.disconnect_grace)

    async def _live_connection(self) -> _Connection:
        while self._conn is None:
            await self._connected.wait()
        return self._conn

    async def _run(self) -> None:
        """Keep a connection up until cancelled by __aexit__."""
        failures = 0
        while True:
            conn = _Connection(self._new_client())
            try:
                await self._serve(conn)
                error = "connection closed"
            except MqttError as e:
                error = str(e)
            except Exception as e:  # noqa: BLE001
                logger.exception("unexpected error on the MQTT connection")
                error = repr(e)
            finally:
                was_live = self._conn is conn
                self._conn = None
                self._connected.clear()
                conn.closed.set()
            if was_live:
                failures = 0
            delay = self._policy.backoffs[min(failures, len(self._policy.backoffs) - 1)]
            failures += 1
            logger.warning(
                "MQTT %s (%s); reconnecting in %.0fs",
                "connection lost" if was_live else "connect failed",
                error,
                delay,
            )
            await asyncio.sleep(delay)

    async def _serve(self, conn: _Connection) -> None:
        """Connect, announce, resubscribe, then pump inbound commands until the
        connection drops (aiomqtt raises MqttError from the message iterator)."""
        async with conn.client as client:
            await client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
            for topic in self._subscriptions:
                await client.subscribe(topic)
            self._conn = conn
            self._connected.set()
            logger.info("MQTT connected")
            async for msg in client.messages:
                cmd = _parse_command(msg.topic.value)
                if cmd is not None:
                    self._commands.put_nowait(cmd)

    def _new_client(self) -> Client:
        # A fresh Client per connection rather than re-entering one: in aiomqtt
        # 2.5 a client that dropped unexpectedly keeps its old "connected"
        # future, so re-entering it doesn't wait for the broker's CONNACK.
        return Client(
            hostname=self._cfg.host,
            port=self._cfg.port,
            username=self._cfg.username,
            password=self._cfg.password,
            will=Will(
                topic=AVAILABILITY_TOPIC,
                payload="offline",
                qos=1,
                retain=True,
            ),
        )


def _parse_command(topic: str) -> RefreshCommand | None:
    if topic == "carriers_sync/refresh_all/cmd":
        return RefreshCommand(account_id=None)
    # `carriers_sync/<provider>/<account>/refresh/cmd`
    parts = topic.split("/")
    if (
        len(parts) == 5
        and parts[0] == "carriers_sync"
        and parts[3] == "refresh"
        and parts[4] == "cmd"
    ):
        return RefreshCommand(account_id=parts[2])
    logger.warning("ignoring unexpected command topic: %s", topic)
    return None
