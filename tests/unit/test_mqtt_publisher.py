"""Verify the MQTT publisher's connection lifecycle, LWT, command-topic dispatch,
and reconnect behaviour."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiomqtt import MqttCodeError, MqttError
from carriers_sync.discovery import MqttMessage
from carriers_sync.mqtt_publisher import (
    MqttConfig,
    MqttPublisher,
    ReconnectPolicy,
    RefreshCommand,
)

CFG = MqttConfig(host="h", port=1883, username=None, password=None)
FAST = ReconnectPolicy(backoffs=(0.0,), publish_attempts=3, disconnect_grace=1.0)


async def _stay_connected():
    """Message stream of a healthy connection: nothing arrives, never ends."""
    await asyncio.Event().wait()
    yield None


async def _drop_when(event):
    """Message stream that dies the way aiomqtt's does when the broker goes away."""
    await event.wait()
    raise MqttError("Disconnected during message iteration")
    yield None


def _make_client(messages=None):
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    instance.publish = AsyncMock()
    instance.subscribe = AsyncMock()
    instance.messages = messages if messages is not None else _stay_connected()
    return instance


@pytest.fixture
def fake_client(monkeypatch):
    """Replace aiomqtt.Client with a fake whose methods are AsyncMocks."""
    instance = _make_client()
    factory = MagicMock(return_value=instance)
    monkeypatch.setattr("carriers_sync.mqtt_publisher.Client", factory)
    return instance, factory


async def test_publishes_message(fake_client):
    instance, _ = fake_client
    pub = MqttPublisher(CFG)
    async with pub:
        await pub.publish_many(
            [
                MqttMessage(topic="t/1", payload={"a": 1}, retain=True),
                MqttMessage(topic="t/2", payload="raw", retain=False),
            ]
        )
    calls = instance.publish.await_args_list
    publish_topics = [c.args[0] for c in calls]
    assert "carriers_sync/availability" in publish_topics  # connect-time online
    assert "t/1" in publish_topics
    assert "t/2" in publish_topics


async def test_lwt_set_on_connect(fake_client):
    _, factory = fake_client
    pub = MqttPublisher(CFG)
    async with pub:
        pass
    will = factory.call_args.kwargs["will"]
    assert will.topic == "carriers_sync/availability"
    assert will.payload == "offline"
    assert will.retain is True


async def test_subscribes_to_command_topics(fake_client):
    """Subscriptions are wildcard-based to cover all provider/account combos
    (carriers_sync/<provider>/<account>/refresh/cmd) plus the singleton
    refresh-all topic."""
    instance, _ = fake_client
    pub = MqttPublisher(CFG)
    async with pub:
        await pub.subscribe_commands(account_ids=["03333333", "03222222"])
    instance.subscribe.assert_any_await("carriers_sync/refresh_all/cmd")
    instance.subscribe.assert_any_await("carriers_sync/+/+/refresh/cmd")


async def test_command_iterator_yields_refresh_objects(fake_client):
    instance, _ = fake_client

    msg1 = MagicMock()
    msg1.topic = MagicMock()
    msg1.topic.value = "carriers_sync/refresh_all/cmd"
    msg2 = MagicMock()
    msg2.topic = MagicMock()
    msg2.topic.value = "carriers_sync/alfa_lb/03333333/refresh/cmd"
    msg3 = MagicMock()
    msg3.topic = MagicMock()
    msg3.topic.value = "carriers_sync/touch_lb/familyacct/refresh/cmd"

    async def _gen():
        yield msg1
        yield msg2
        yield msg3
        await asyncio.Event().wait()

    instance.messages = _gen()

    pub = MqttPublisher(CFG)
    async with pub:
        commands = []
        async for cmd in pub.commands():
            commands.append(cmd)
            if len(commands) == 3:
                break
    assert commands[0] == RefreshCommand(account_id=None)
    assert commands[1] == RefreshCommand(account_id="03333333")
    assert commands[2] == RefreshCommand(account_id="familyacct")


async def test_reconnects_and_resubscribes_after_broker_restart(monkeypatch):
    """Mosquitto restarting (e.g. for an update) drops the connection. The next
    publish must go out on a fresh connection that re-announced itself and
    resubscribed, instead of killing the App as it did before 0.6.4."""
    dropped = asyncio.Event()
    first = _make_client(messages=_drop_when(dropped))

    async def _publish_on_first(topic, *args, **kwargs):
        if dropped.is_set():
            raise MqttCodeError(4, "Could not publish message")

    first.publish.side_effect = _publish_on_first
    second = _make_client()
    factory = MagicMock(side_effect=[first, second])
    monkeypatch.setattr("carriers_sync.mqtt_publisher.Client", factory)

    pub = MqttPublisher(CFG, reconnect_policy=FAST)
    async with pub:
        await pub.subscribe_commands(account_ids=[])
        dropped.set()
        await pub.publish_many([MqttMessage(topic="t/1", payload="x", retain=True)])

    assert factory.call_count == 2
    assert second.publish.await_args_list[0] == call(
        "carriers_sync/availability", "online", qos=1, retain=True
    )
    second.subscribe.assert_any_await("carriers_sync/refresh_all/cmd")
    second.subscribe.assert_any_await("carriers_sync/+/+/refresh/cmd")
    second.publish.assert_any_await("t/1", "x", qos=1, retain=True)


async def test_waits_for_broker_that_is_down_at_startup(monkeypatch):
    refused = _make_client()
    refused.__aenter__ = AsyncMock(side_effect=MqttError("[Errno 111] Connection refused"))
    good = _make_client()
    factory = MagicMock(side_effect=[refused, good])
    monkeypatch.setattr("carriers_sync.mqtt_publisher.Client", factory)

    pub = MqttPublisher(CFG, reconnect_policy=FAST)
    async with pub:
        await pub.publish_many([MqttMessage(topic="t/1", payload="x", retain=True)])

    assert factory.call_count == 2
    good.publish.assert_any_await("t/1", "x", qos=1, retain=True)


async def test_publish_gives_up_after_policy_attempts(fake_client):
    """A publish that keeps failing on a connection that never drops raises
    after publish_attempts, rather than retrying forever."""
    instance, _ = fake_client

    async def _reject_t1(topic, *args, **kwargs):
        if topic == "t/1":
            raise MqttCodeError(7, "Could not publish message")

    instance.publish.side_effect = _reject_t1
    policy = ReconnectPolicy(backoffs=(0.0,), publish_attempts=2, disconnect_grace=0.0)
    pub = MqttPublisher(CFG, reconnect_policy=policy)
    async with pub:
        with pytest.raises(MqttCodeError):
            await pub.publish_many([MqttMessage(topic="t/1", payload="x", retain=True)])
    t1_attempts = [c for c in instance.publish.await_args_list if c.args[0] == "t/1"]
    assert len(t1_attempts) == 2


async def test_exit_does_not_raise_when_broker_is_gone(fake_client):
    """The 0.6.3 crash ended in __aexit__ failing to publish "offline" on a
    dead connection. Shutting down must not raise for that."""
    instance, _ = fake_client

    async def _reject_offline(topic, payload, *args, **kwargs):
        if payload == "offline":
            raise MqttCodeError(4, "Could not publish message")

    instance.publish.side_effect = _reject_offline
    pub = MqttPublisher(CFG)
    async with pub:
        pass
    instance.publish.assert_any_await("carriers_sync/availability", "offline", qos=1, retain=True)
