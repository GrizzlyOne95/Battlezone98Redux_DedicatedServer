"""Opt-in network impairment for the UDP relay (default OFF; private test runs only).

The relay sits on every peer stream of a private loopback session, so it can
stand in for a bad network without a packet-filter driver: random loss,
periodic outages, delay with jitter, reordering, duplication and a per-player
uplink bandwidth cap. It never rewrites a datagram; it only decides whether
each one is sent, when, and how many times.

Spec: comma-separated key=value, every key optional:

    seed=N            RNG seed (default 1). The same spec, seed and traffic
                      give the same decisions.
    loss=P            drop each datagram with probability P percent ("3" or "3%")
    outage=D/T        drop everything for D ms out of every T ms. The first
                      outage starts T-D ms after the impairment is applied.
    delay=MS          add MS milliseconds to every datagram
    jitter=MS         add a uniform +-MS on top of the delay (never below 0).
                      Each stream stays in order unless reorder= picks a datagram.
    reorder=P         hold P percent of datagrams back by reorder_ms so the
                      following ones overtake them
    reorder_ms=MS     how far a reordered datagram is held (default 30)
    dup=P             send P percent of datagrams twice
    rate=KBPS         cap each sending player's uplink at KBPS kilobits/s,
                      shared across all of its peer streams
    queue=MS          longest a datagram may wait for the uplink before it
                      is tail-dropped (default 200)
    peer=A|B          only impair streams from or to these players (user id
                      or name); default every stream
    dir=both|from|to  with peer=, impair only datagrams the peer sends
                      (from), only those it receives (to), or both (default)
    for=S             clear itself S seconds after being applied

    loss=100,peer=Coop1,for=20   blackholes one player for 20 seconds.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

StreamKey = tuple[str, int, str, int]

_PERCENT_KEYS = ("loss", "reorder", "dup")
_MS_KEYS = ("delay", "jitter", "reorder_ms", "queue")


@dataclass(frozen=True)
class ImpairmentSpec:
    seed: int = 1
    loss: float = 0.0          # probability, 0..1
    outage_ms: float = 0.0
    outage_every_ms: float = 0.0
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    reorder: float = 0.0       # probability, 0..1
    reorder_ms: float = 30.0
    dup: float = 0.0           # probability, 0..1
    rate_kbps: float = 0.0
    queue_ms: float = 200.0
    peers: frozenset[str] = field(default_factory=frozenset)
    direction: str = "both"
    duration_s: float = 0.0

    def describe(self) -> str:
        """Canonical spec text; parse_spec(describe()) gives an equal spec."""
        parts = [f"seed={self.seed}"]
        if self.loss:
            parts.append(f"loss={_fmt(self.loss * 100)}")
        if self.outage_every_ms:
            parts.append(f"outage={_fmt(self.outage_ms)}/{_fmt(self.outage_every_ms)}")
        if self.delay_ms:
            parts.append(f"delay={_fmt(self.delay_ms)}")
        if self.jitter_ms:
            parts.append(f"jitter={_fmt(self.jitter_ms)}")
        if self.reorder:
            parts.append(f"reorder={_fmt(self.reorder * 100)}")
            parts.append(f"reorder_ms={_fmt(self.reorder_ms)}")
        if self.dup:
            parts.append(f"dup={_fmt(self.dup * 100)}")
        if self.rate_kbps:
            parts.append(f"rate={_fmt(self.rate_kbps)}")
            parts.append(f"queue={_fmt(self.queue_ms)}")
        if self.peers:
            parts.append("peer=" + "|".join(sorted(self.peers)))
            parts.append(f"dir={self.direction}")
        if self.duration_s:
            parts.append(f"for={_fmt(self.duration_s)}")
        return ",".join(parts)


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _number(key: str, text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"impairment {key}= needs a number, got {text!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"impairment {key}= must be finite")
    if value < 0:
        raise ValueError(f"impairment {key}= must not be negative")
    return value


def parse_spec(text: str) -> ImpairmentSpec:
    """Parse an impairment spec. Raises ValueError on anything it does not understand."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty impairment spec")
    values: dict[str, object] = {}
    seen: set[str] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, raw = chunk.partition("=")
        key, raw = key.strip().lower(), raw.strip()
        if not sep or not raw:
            raise ValueError(f"impairment parameter {chunk!r} must be key=value")
        if key in seen:
            raise ValueError(f"impairment {key}= given twice")
        seen.add(key)
        if key == "seed":
            try:
                values["seed"] = int(raw)
            except ValueError:
                raise ValueError(f"impairment seed= needs an integer, got {raw!r}") from None
        elif key in _PERCENT_KEYS:
            pct = _number(key, raw[:-1] if raw.endswith("%") else raw)
            if pct > 100:
                raise ValueError(f"impairment {key}= is a percentage, 0-100")
            values[key] = pct / 100.0
        elif key in _MS_KEYS:
            values[{"delay": "delay_ms", "jitter": "jitter_ms", "queue": "queue_ms"}.get(key, key)] = _number(key, raw)
        elif key == "outage":
            dur, slash, every = raw.partition("/")
            if not slash:
                raise ValueError("impairment outage= is DURATION_MS/PERIOD_MS")
            d, t = _number(key, dur), _number(key, every)
            if t <= 0 or d <= 0 or d > t:
                raise ValueError("impairment outage= needs 0 < duration <= period")
            values["outage_ms"], values["outage_every_ms"] = d, t
        elif key == "rate":
            rate = _number(key, raw)
            if rate <= 0:
                raise ValueError("impairment rate= must be above 0 kbit/s")
            values["rate_kbps"] = rate
        elif key == "peer":
            peers = frozenset(p.strip() for p in raw.split("|") if p.strip())
            if not peers:
                raise ValueError("impairment peer= names no player")
            values["peers"] = peers
        elif key == "dir":
            if raw not in ("both", "from", "to"):
                raise ValueError("impairment dir= is both, from or to")
            values["direction"] = raw
        elif key == "for":
            duration = _number(key, raw)
            if duration <= 0:
                raise ValueError("impairment for= must be above 0 seconds")
            values["duration_s"] = duration
        else:
            raise ValueError(f"unknown impairment parameter {key!r}")
    if "direction" in values and "peers" not in values:
        raise ValueError("impairment dir= needs peer=")
    return ImpairmentSpec(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Decision:
    """What to do with one datagram: drop it (reason set) or send one copy per delay."""

    drop: str | None = None
    delays: tuple[float, ...] = (0.0,)   # seconds from now, one per copy
    reordered: bool = False


PASS = Decision()


class RelayImpairment:
    """Seeded per-datagram impairment decisions. Not thread-safe; the relay
    calls it from its event loop only."""

    def __init__(self, spec: ImpairmentSpec, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.spec = spec
        self._clock = clock
        self._rng = random.Random(spec.seed)
        self.applied_at = clock()
        self._stream_floor: dict[StreamKey, float] = {}
        self._uplink_free: dict[str, float] = {}
        self.totals = {"observed": 0, "matched": 0, "sent": 0, "duplicated": 0, "reordered": 0,
                       "dropped_loss": 0, "dropped_outage": 0, "dropped_queue": 0}
        self._delay_sum = 0.0
        self._delay_max = 0.0
        self._delayed = 0
        self.streams: dict[str, dict[str, int]] = {}

    def expired(self, now: float | None = None) -> bool:
        if not self.spec.duration_s:
            return False
        now = self._clock() if now is None else now
        return now - self.applied_at >= self.spec.duration_s

    def matches(self, source_labels: Iterable[str], target_labels: Iterable[str]) -> bool:
        peers = self.spec.peers
        if not peers:
            return True
        from_peer = not peers.isdisjoint(source_labels)
        to_peer = not peers.isdisjoint(target_labels)
        if self.spec.direction == "from":
            return from_peer
        if self.spec.direction == "to":
            return to_peer
        return from_peer or to_peer

    def in_outage(self, now: float) -> bool:
        every = self.spec.outage_every_ms
        if not every:
            return False
        # Rounded to the microsecond so float noise never shifts a window edge.
        phase_ms = round((now - self.applied_at) * 1000.0, 3) % every
        return phase_ms >= every - self.spec.outage_ms

    def decide(
        self,
        size: int,
        stream: StreamKey,
        uplink: str,
        source_labels: Iterable[str] = (),
        target_labels: Iterable[str] = (),
        now: float | None = None,
    ) -> Decision:
        """Decide one datagram of `size` bytes on `stream`, sent by `uplink`."""
        now = self._clock() if now is None else now
        self.totals["observed"] += 1
        if self.expired(now) or not self.matches(source_labels, target_labels):
            return PASS
        spec = self.spec
        self.totals["matched"] += 1
        stream_name = f"{stream[0]}:{stream[1]}->{stream[2]}:{stream[3]}"
        counts = self.streams.setdefault(stream_name, {"matched": 0, "sent": 0, "dropped": 0})
        counts["matched"] += 1
        # Draw the same four numbers for every matched datagram so one
        # decision never shifts the random sequence of the next.
        u_loss, u_jitter, u_reorder, u_dup = (self._rng.random() for _ in range(4))

        drop = None
        if self.in_outage(now):
            drop = "outage"
        elif u_loss < spec.loss:
            drop = "loss"
        send_at = now + max(0.0, spec.delay_ms + spec.jitter_ms * (2.0 * u_jitter - 1.0)) / 1000.0
        if drop is None and spec.rate_kbps:
            start = max(now, self._uplink_free.get(uplink, now))
            if (start - now) * 1000.0 > spec.queue_ms:
                drop = "queue"
            else:
                done = start + size * 8.0 / (spec.rate_kbps * 1000.0)
                self._uplink_free[uplink] = done
                send_at += done - now
        if drop is not None:
            self.totals["dropped_" + drop] += 1
            counts["dropped"] += 1
            return Decision(drop=drop, delays=())

        reordered = u_reorder < spec.reorder
        floor = self._stream_floor.get(stream, now)
        if reordered:
            # Held past the stream's ordering floor without raising it, so
            # the datagrams after it go first.
            send_at = max(send_at, floor) + spec.reorder_ms / 1000.0
            self.totals["reordered"] += 1
        else:
            send_at = max(send_at, floor)
            self._stream_floor[stream] = send_at
        delay = send_at - now
        copies = (delay, delay) if u_dup < spec.dup else (delay,)
        if len(copies) == 2:
            self.totals["duplicated"] += 1
        self.totals["sent"] += len(copies)
        counts["sent"] += len(copies)
        if delay > 0:
            self._delayed += 1
            self._delay_sum += delay
            self._delay_max = max(self._delay_max, delay)
        return Decision(delays=copies, reordered=reordered)

    def metrics(self, now: float | None = None) -> dict[str, object]:
        now = self._clock() if now is None else now
        return {
            "active": not self.expired(now),
            "spec": self.spec.describe(),
            "appliedSecondsAgo": round(now - self.applied_at, 3),
            **self.totals,
            "delayedDatagrams": self._delayed,
            "meanAddedDelayMs": round(self._delay_sum / self._delayed * 1000.0, 3) if self._delayed else 0.0,
            "maxAddedDelayMs": round(self._delay_max * 1000.0, 3),
            "streams": self.streams,
        }
