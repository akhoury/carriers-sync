"""Async cycle scheduler.

Owns:
  - the Playwright browser lifetime (one per cycle)
  - per-account fetch + retry classification
  - reaction to refresh-all + per-account refresh events
  - persistence to state.json after successful fetches
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from carriers_sync.config import AppConfig
from carriers_sync.discovery import (
    MqttMessage,
    build_account_messages,
    build_app_device_messages,
)
from carriers_sync.mqtt_publisher import MqttPublisher
from carriers_sync.providers import get_provider
from carriers_sync.providers.alfa_lb import AlfaLbProvider
from carriers_sync.providers.base import (
    AccountConfig,
    AuthFetchError,
    Provider,
    ProviderResult,
    ProviderUnsupportedError,
    TransientFetchError,
    UnknownFetchError,
)
from carriers_sync.providers.ogero_lb import OgeroLbProvider
from carriers_sync.providers.touch_lb import TouchLbProvider
from carriers_sync.state_store import State, StateStore

logger = logging.getLogger("carriers_sync.scheduler")

_PROVIDER_DISPLAY: dict[str, str] = {
    AlfaLbProvider.id: AlfaLbProvider.display_name,
    TouchLbProvider.id: TouchLbProvider.display_name,
    OgeroLbProvider.id: OgeroLbProvider.display_name,
}


@dataclass(frozen=True)
class RetryPolicy:
    transient_backoffs: tuple[float, ...] = (30.0, 60.0, 120.0)


_DEFAULT_RETRY_POLICY = RetryPolicy()


def classify_outcome(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, TransientFetchError):
        return "transient"
    # ProviderUnsupportedError subclasses AuthFetchError — check it first.
    if isinstance(exc, ProviderUnsupportedError):
        return "unsupported"
    if isinstance(exc, AuthFetchError):
        return "auth"
    if isinstance(exc, UnknownFetchError):
        return "unknown"
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    return "unknown"


async def run_one_account(
    provider: Provider,
    account: AccountConfig,
    browser: Any,
    policy: RetryPolicy,
) -> tuple[ProviderResult | None, BaseException | None]:
    """Fetch one account with retry classification.

    Returns (result, None) on success or (None, last_exception) on failure.
    """
    last_exc: BaseException | None = None

    for i in range(len(policy.transient_backoffs)):
        try:
            result = await provider.fetch(account, browser)
            return result, None
        except TransientFetchError as e:
            last_exc = e
            if i < len(policy.transient_backoffs) - 1:
                logger.info(
                    "transient error for %s, retrying after %.1fs: %s",
                    account.username,
                    policy.transient_backoffs[i],
                    e,
                )
                await asyncio.sleep(policy.transient_backoffs[i])
                continue
            logger.warning("transient retries exhausted for %s: %s", account.username, last_exc)
            return None, last_exc
        except AuthFetchError as e:
            logger.warning("auth error for %s, no retry: %s", account.username, e)
            return None, e
        except UnknownFetchError as e:
            last_exc = e
            if i == 0:
                logger.info("unknown error for %s, retrying once: %s", account.username, e)
                continue
            logger.warning("unknown error after retry for %s: %s", account.username, last_exc)
            return None, last_exc
        except Exception as e:  # noqa: BLE001
            logger.exception("unexpected error for %s", account.username)
            return None, e

    return None, last_exc


class Scheduler:
    def __init__(
        self,
        *,
        config: AppConfig,
        publisher: MqttPublisher,
        state_store: StateStore,
        browser_factory: Callable[[], Awaitable[Any]],
        retry_policy: RetryPolicy = _DEFAULT_RETRY_POLICY,
    ) -> None:
        self._config = config
        self._publisher = publisher
        self._state_store = state_store
        self._browser_factory = browser_factory
        self._retry = retry_policy
        self._refresh_all = asyncio.Event()
        self._account_refresh: asyncio.Queue[str] = asyncio.Queue()
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        state = self._state_store.load()
        await self._publish_discovery()
        await self._republish_known_state(state)
        await self._publish_app_status("starting")
        await self._publisher.subscribe_commands(
            account_ids=[a.username for a in self._config.accounts]
        )

        listener = asyncio.create_task(self._listen_commands())
        refresher = asyncio.create_task(self._account_refresh_worker())
        try:
            while not self._stop.is_set():
                try:
                    await self.run_one_cycle()
                    await self._publish_app_status("running")
                except Exception:
                    logger.exception("unhandled error during cycle")
                    await self._publish_app_status("errored")
                await self._await_next_cycle()
        finally:
            listener.cancel()
            refresher.cancel()

    async def stop(self) -> None:
        self._stop.set()
        self._refresh_all.set()

    async def run_one_cycle(self) -> None:
        browser = await self._browser_factory()
        cycle_start = datetime.now(UTC)
        outcomes: dict[str, int] = {}
        try:
            for account in self._config.accounts:
                token = await self._fetch_and_publish(account, browser)
                outcomes[token] = outcomes.get(token, 0) + 1
        finally:
            await browser.close()
        elapsed = (datetime.now(UTC) - cycle_start).total_seconds()
        ok = outcomes.pop("", 0)
        rest = ", ".join(f"{n} {tok}" for tok, n in outcomes.items()) or "0 errors"
        logger.info("Cycle complete in %.1fs: %d ok, %s", elapsed, ok, rest)

    async def _fetch_and_publish(self, account: AccountConfig, browser: Any) -> str:
        """Fetch one account and publish results. Returns the outcome token
        (empty string on success, classification token on failure) for the
        per-cycle summary."""
        provider = get_provider(account.provider)
        result, exc = await run_one_account(provider, account, browser, self._retry)
        now_iso = datetime.now(UTC).isoformat()

        if result is not None:
            display = _PROVIDER_DISPLAY.get(account.provider, account.provider)
            messages = build_account_messages(
                result,
                danger_percent=self._config.danger_percent,
                provider_display=display,
                provider_id=account.provider,
            )
            await self._publisher.publish_many(messages)

            state = self._state_store.load()
            state.last_results[account.username] = result
            self._state_store.save(state)

            # Per-account success line so users can see what was synced.
            n_lines = len(result.lines)
            logger.info(
                "ok %s (%s): %d %s",
                account.username,
                account.label or "?",
                n_lines,
                "line" if n_lines == 1 else "lines",
            )
            return ""

        err_token = classify_outcome(exc)
        await self._publish_error_state(account, err_token, now_iso, exc=exc)
        return err_token

    async def _publish_error_state(
        self,
        account: AccountConfig,
        err_token: str,
        now_iso: str,
        *,
        exc: BaseException | None = None,
    ) -> None:
        pid = account.provider.replace("-", "_")

        # A permanently-unsupported provider (e.g. Touch after its OTP-only
        # migration) resets to zeros instead of freezing the last-known values,
        # and is dropped from persisted state so a restart won't republish it.
        if isinstance(exc, ProviderUnsupportedError):
            await self._publish_reset_state(account, err_token, now_iso, pid=pid)
            return

        prev = self._state_store.load().last_results.get(account.username)
        merged: dict[str, Any] = {}
        if prev is not None:
            merged = _payload_from_result(prev, danger_percent=self._config.danger_percent)
        merged.update(
            {
                "sync_ok": "OFF",
                "last_error": err_token,
                "last_attempted": now_iso,
            }
        )
        await self._publisher.publish_many(
            [
                MqttMessage(
                    topic=f"carriers_sync/{pid}/{account.username}/state",
                    payload=merged,
                    retain=True,
                )
            ]
        )

    async def _publish_reset_state(
        self, account: AccountConfig, err_token: str, now_iso: str, *, pid: str
    ) -> None:
        state = self._state_store.load()
        prev = state.last_results.get(account.username)

        messages: list[MqttMessage] = [
            MqttMessage(
                topic=f"carriers_sync/{pid}/{account.username}/state",
                payload={
                    "consumed_gb": 0.0,
                    "total_consumed_gb": 0.0,
                    "quota_gb": None,
                    "remaining_gb": 0.0,
                    "usage_percent": 0.0,
                    "extra_consumed_gb": 0.0,
                    "danger": "OFF",
                    "sync_ok": "OFF",
                    "last_error": err_token,
                    "last_attempted": now_iso,
                },
                retain=True,
            )
        ]
        if prev is not None:
            for line in prev.lines:
                if line.is_secondary:
                    messages.append(
                        MqttMessage(
                            topic=f"carriers_sync/{pid}/{account.username}/{line.line_id}/state",
                            payload={"consumed_gb": 0.0},
                            retain=True,
                        )
                    )
            del state.last_results[account.username]
            self._state_store.save(state)

        await self._publisher.publish_many(messages)

    async def _publish_discovery(self) -> None:
        msgs = build_app_device_messages()
        for account in self._config.accounts:
            prev = self._state_store.load().last_results.get(account.username)
            if prev is None:
                continue
            display = _PROVIDER_DISPLAY.get(account.provider, account.provider)
            msgs.extend(
                build_account_messages(
                    prev,
                    danger_percent=self._config.danger_percent,
                    provider_display=display,
                    provider_id=account.provider,
                )
            )
        await self._publisher.publish_many(msgs)

    async def _republish_known_state(self, state: State) -> None:
        # Look up provider per account from current config (provider isn't
        # carried in ProviderResult, so we need the config to derive the
        # display name).
        provider_by_username = {a.username: a.provider for a in self._config.accounts}
        for username, result in state.last_results.items():
            provider_id = provider_by_username.get(username)
            if provider_id is None:
                # Account was removed from config; skip republishing stale
                # state for it. Cleanup happens when the cycle re-publishes.
                continue
            display = _PROVIDER_DISPLAY.get(provider_id, provider_id)
            msgs = build_account_messages(
                result,
                danger_percent=self._config.danger_percent,
                provider_display=display,
                provider_id=provider_id,
            )
            await self._publisher.publish_many(msgs)

    async def _publish_app_status(self, status: str) -> None:
        await self._publisher.publish_many(
            [
                MqttMessage(
                    topic="carriers_sync/app/state",
                    payload={"status": status},
                    retain=True,
                )
            ]
        )

    async def _await_next_cycle(self) -> None:
        timeout = self._config.poll_interval_minutes * 60
        try:
            await asyncio.wait_for(self._refresh_all.wait(), timeout=timeout)
        except TimeoutError:
            return
        finally:
            self._refresh_all.clear()

    async def _listen_commands(self) -> None:
        try:
            async for cmd in self._publisher.commands():
                if cmd.account_id is None:
                    self._refresh_all.set()
                else:
                    await self._account_refresh.put(cmd.account_id)
        except asyncio.CancelledError:
            return

    async def _account_refresh_worker(self) -> None:
        in_flight: set[str] = set()
        try:
            while True:
                acct_id = await self._account_refresh.get()
                if acct_id in in_flight:
                    continue
                account = next(
                    (a for a in self._config.accounts if a.username == acct_id),
                    None,
                )
                if account is None:
                    logger.warning("refresh requested for unknown account %s", acct_id)
                    continue
                in_flight.add(acct_id)
                asyncio.create_task(self._refresh_one(account, in_flight))
        except asyncio.CancelledError:
            return

    async def _refresh_one(self, account: AccountConfig, in_flight: set[str]) -> None:
        try:
            browser = await self._browser_factory()
            try:
                await self._fetch_and_publish(account, browser)
            finally:
                await browser.close()
        finally:
            in_flight.discard(account.username)


def _payload_from_result(result: ProviderResult, *, danger_percent: int) -> dict[str, Any]:
    main = next(line for line in result.lines if not line.is_secondary)
    secondaries = [line for line in result.lines if line.is_secondary]
    if main.is_aggregate:
        total = main.consumed_gb
    else:
        total = main.consumed_gb + sum(s.consumed_gb for s in secondaries)
    quota = main.quota_gb or 0.0
    remaining = max(0.0, quota - total) if quota else 0.0
    pct = round((total / quota) * 100, 1) if quota else 0.0
    danger = bool(quota and (total / quota) * 100 >= danger_percent) or main.extra_consumed_gb > 0
    return {
        "consumed_gb": main.consumed_gb,
        "total_consumed_gb": round(total, 3),
        "quota_gb": main.quota_gb,
        "remaining_gb": round(remaining, 3),
        "usage_percent": pct,
        "extra_consumed_gb": main.extra_consumed_gb,
        "danger": "ON" if danger else "OFF",
        "sync_ok": "ON",
        "last_synced": result.fetched_at.isoformat(),
        "last_attempted": result.fetched_at.isoformat(),
        "last_error": "",
    }
