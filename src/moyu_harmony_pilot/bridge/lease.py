"""Background lease renewal with cancellation visible to action checkpoints and deadlines."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from moyu_harmony_pilot.bridge.client import JsonTransport
from moyu_harmony_pilot.bridge.config import BridgeConfig
from moyu_harmony_pilot.bridge.protocol import BridgeError, Job, parse_time, utc_now


class LeaseKeeper:
    def __init__(
        self,
        job: Job,
        config: BridgeConfig,
        client: JsonTransport,
        stop: threading.Event,
        heartbeat: Callable[[], None],
    ) -> None:
        self.job = job
        self.config = config
        self.client = client
        self.stop = stop
        self.heartbeat = heartbeat
        self.stage = "preparing"
        self.expires_at = job.lease_expires_at
        self.lost = threading.Event()
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="harmony-lease", daemon=True)

    def cancelled(self) -> bool:
        return (
            self.stop.is_set()
            or self.finished.is_set()
            or self.lost.is_set()
            or utc_now() >= min(self.expires_at, self.job.expires_at)
        )

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    def renew(self) -> None:
        response = self.client.post(
            f"/bridge/v1/jobs/{self.job.job_id}/lease",
            {
                "bridgeId": self.config.bridge_id,
                "leaseToken": self.job.lease_token,
                "extendSeconds": 60,
                "stage": self.stage,
            },
        )
        expires = parse_time(response.get("leaseExpiresAt"))
        if (
            self.cancelled()
            or response.get("ok") is not True
            or type(response.get("cancelRequested")) is not bool
            or response["cancelRequested"]
            or expires <= utc_now()
        ):
            self.lost.set()
            return
        self.expires_at = expires

    def _loop(self) -> None:
        while not self.cancelled():
            remaining = (self.expires_at - utc_now()).total_seconds()
            if self.finished.wait(
                max(0.01, min(self.config.lease_seconds, remaining / 3))
            ) or self.cancelled():
                return
            try:
                self.renew()
            except BridgeError as exc:
                if not exc.retryable:
                    self.lost.set()
            except Exception:
                self.lost.set()
            else:
                if not self.cancelled():
                    try:
                        self.heartbeat()
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "Bridge heartbeat failed during lease maintenance."
                        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.finished.set()
        self.thread.join(timeout=2 * self.config.request_timeout + 1)
