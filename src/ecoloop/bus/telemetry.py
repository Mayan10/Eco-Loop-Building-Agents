"""The ring buffer the EnergyPlus callback writes into and the worker reads from.

This is one of exactly two objects that cross the thread boundary (the other
is :mod:`ecoloop.bus.policy`). Data flows one way through each: the callback
writes telemetry and reads policy; the worker reads telemetry and writes
policy. Nothing else crosses.

The callback-side write must never block. A full buffer drops its oldest
sample rather than waiting for the worker to catch up — blocking the callback
to preserve a sample would stall the EnergyPlus physics solver, which is the
wrong trade for a value the cognitive layer only ever reads in aggregate.
Drops are counted, not silent to the operator: they surface in the run
manifest.
"""

from __future__ import annotations

import threading
from collections import deque

from ecoloop.bus.models import TelemetrySample

__all__ = ["TelemetryBus"]


class TelemetryBus:
    """A fixed-capacity, drop-oldest ring buffer of telemetry samples.

    Args:
        capacity: Maximum samples retained, from ``bus.telemetry_capacity``.
    """

    def __init__(self, capacity: int) -> None:
        """Create an empty bus with the given fixed capacity."""
        self._buffer: deque[TelemetrySample] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._dropped = 0
        self._published = 0

    def put_nowait(self, sample: TelemetrySample) -> None:
        """Publish one sample, dropping the oldest if the buffer is full.

        Called from the EnergyPlus callback on the main thread. Never blocks
        and never raises: a lock acquisition here is uncontended in the
        overwhelmingly common case (the worker thread reads far less often
        than the callback writes) and the critical section is a single
        deque append, well inside the reflex tier's sub-millisecond budget.

        Args:
            sample: The telemetry sample to publish.
        """
        with self._lock:
            if len(self._buffer) == (self._buffer.maxlen or 0):
                self._dropped += 1
            self._buffer.append(sample)
            self._published += 1

    def snapshot(self) -> tuple[TelemetrySample, ...]:
        """Copy every sample currently retained, oldest first.

        Called from the worker thread. Returns a copy rather than a view so
        the caller can iterate it without holding the lock, and so a
        concurrent callback write can never be observed mid-mutation.

        Returns:
            An immutable snapshot of the buffer at the moment of the call.
        """
        with self._lock:
            return tuple(self._buffer)

    def latest(self) -> TelemetrySample | None:
        """The most recently published sample, if any.

        Returns:
            The newest sample, or ``None`` if nothing has been published yet.
        """
        with self._lock:
            return self._buffer[-1] if self._buffer else None

    def window(self, minutes: float) -> tuple[TelemetrySample, ...]:
        """Every sample within a trailing window of the latest one.

        Args:
            minutes: Window length, measured in simulation time back from the
                latest sample's clock — not wall-clock time, since a run can
                execute far faster or slower than real time.

        Returns:
            Matching samples, oldest first. Empty if nothing has been
            published yet.
        """
        samples = self.snapshot()
        if not samples:
            return ()
        cutoff = samples[-1].clock.as_datetime
        return tuple(
            s for s in samples if (cutoff - s.clock.as_datetime).total_seconds() <= minutes * 60.0
        )

    @property
    def dropped_count(self) -> int:
        """Total samples ever dropped for arriving into a full buffer."""
        with self._lock:
            return self._dropped

    @property
    def published_count(self) -> int:
        """Total samples ever published, including ones since dropped."""
        with self._lock:
            return self._published

    def __len__(self) -> int:
        """Number of samples currently retained."""
        with self._lock:
            return len(self._buffer)
