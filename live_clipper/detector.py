"""Chat spikes: when a channel's chat suddenly gets much busier than usual.

Every message counts 1, and a message that is a hype reaction (KEKW, kkkk,
"CLIP", a run of question marks...) counts more. Weighted counts go into
one-second bins. A spike is when the recent window's rate is several times
the channel's own baseline, so a 50k-viewer chat and a 500-viewer chat are
judged against themselves, not against one fixed number.

Chat reacts after the moment, so a spike carries `onset`: the first second
in the run-up where the rate climbed past half the trigger level. Clip
windows are anchored on that, not on the moment the trigger fired.

Pure: no clock, no I/O. Callers pass timestamps in.
"""

import re
from collections import deque
from dataclasses import dataclass

# Reactions that mean "something just happened". Matched per word, case
# insensitive. Portuguese and English chat both, since the target is BR and
# international streams.
HYPE_WORDS = frozenset(
    w.lower()
    for w in (
        "KEKW", "LUL", "LULW", "OMEGALUL", "LMAO", "LMFAO", "LOL", "XD",
        "Pog", "PogChamp", "POGGERS", "PogU", "Poggies", "monkaS", "monkaW",
        "WTF", "OMG", "NOOO", "NOO", "YOOO", "HOLY", "CLIP", "CLIPA", "CLIPPED",
        "Kappa", "Jebaited", "D:", "WOW", "INSANE", "CAPTURA", "MDS", "NOSSA",
        "CARALHO", "KRL", "VSF", "SOCORRO", "EITA", "GG", "W", "L",
    )
)
_LAUGH = re.compile(r"^(k{4,}|(ha){3,}h?|(he){3,}|(rs){3,}|ja(ja)+)$", re.IGNORECASE)
_PUNCT_RUN = re.compile(r"[?!]{3,}")
_WORD = re.compile(r"\S+")


def message_weight(text: str, hype_weight: float = 1.0) -> float:
    """1 for any message, plus `hype_weight` if it is a reaction."""
    words = _WORD.findall(text or "")
    hype = _PUNCT_RUN.search(text or "") is not None
    if not hype:
        for w in words[:12]:
            lw = w.lower().strip(".,")
            if lw in HYPE_WORDS or _LAUGH.match(lw):
                hype = True
                break
    return 1.0 + (hype_weight if hype else 0.0)


@dataclass(frozen=True)
class Spike:
    at: float        # when the trigger fired (seconds, caller's clock)
    onset: float     # where the run-up started; anchor clips here
    rate: float      # weighted messages per second in the recent window
    baseline: float  # the channel's usual rate
    score: float     # rate / baseline


class SpikeDetector:
    """Feed messages with `add`, ask with `check` once a second or so."""

    def __init__(
        self,
        *,
        window_s: int = 10,
        baseline_s: int = 300,
        ratio: float = 3.0,
        min_rate: float = 1.0,
        warmup_s: int = 60,
        cooldown_s: int = 90,
        hype_weight: float = 1.0,
    ):
        if window_s <= 0 or baseline_s <= window_s:
            raise ValueError("baseline_s must be longer than window_s")
        self.window_s = window_s
        self.baseline_s = baseline_s
        self.ratio = ratio
        self.min_rate = min_rate
        self.warmup_s = warmup_s
        self.cooldown_s = cooldown_s
        self.hype_weight = hype_weight
        self._bins: deque[list[float]] = deque()  # [second, weight]
        self._first: float | None = None
        self._last_spike: float | None = None

    def add(self, ts: float, text: str) -> None:
        sec = float(int(ts))
        if self._first is None:
            self._first = sec
        w = message_weight(text, self.hype_weight)
        if self._bins and self._bins[-1][0] == sec:
            self._bins[-1][1] += w
        elif self._bins and sec < self._bins[-1][0]:
            # Out of order by a second or two: put it where it belongs.
            for b in reversed(self._bins):
                if b[0] == sec:
                    b[1] += w
                    break
                if b[0] < sec:
                    break
        else:
            self._bins.append([sec, w])
        self._trim(sec)

    def _trim(self, now: float) -> None:
        horizon = now - self.baseline_s - self.window_s
        while self._bins and self._bins[0][0] < horizon:
            self._bins.popleft()

    def _sum(self, start: float, end: float) -> float:
        return sum(w for s, w in self._bins if start <= s < end)

    def check(self, now: float) -> Spike | None:
        now = float(int(now))
        self._trim(now)
        if self._first is None or now - self._first < self.warmup_s:
            return None
        if self._last_spike is not None and now - self._last_spike < self.cooldown_s:
            return None
        recent_start = now - self.window_s + 1
        rate = self._sum(recent_start, now + 1) / self.window_s
        base_start = max(self._first, now - self.baseline_s - self.window_s + 1)
        base_len = recent_start - base_start
        if base_len <= 0:
            return None
        baseline = self._sum(base_start, recent_start) / base_len
        trigger = max(self.min_rate, self.ratio * max(baseline, 0.05))
        if rate < trigger:
            return None
        self._last_spike = now
        return Spike(
            at=now,
            onset=self._onset(now, trigger / 2),
            rate=round(rate, 2),
            baseline=round(baseline, 2),
            score=round(rate / max(baseline, 0.05), 1),
        )

    def _onset(self, now: float, level: float) -> float:
        """First second of the run-up whose 3s average reached `level`."""
        by_sec = dict(self._bins)
        onset = now
        t = now
        limit = now - 3 * self.window_s
        while t > limit:
            avg = sum(by_sec.get(t - i, 0.0) for i in range(3)) / 3
            if avg < level:
                break
            onset = t - 2
            t -= 1
        return onset
