"""Event detection (page 5): non-LLM, always-on time-series anomaly prediction.

Per machine and channel the detector keeps a rolling window of *validated* samples, a robust
baseline (EWMA, frozen while an excursion is open) and a noise estimate. When a deviation is
confirmed it fits three small models to the excursion -- constant, linear trend and first-order
exponential approach (grid over tau, least squares, BIC pick) -- and forecasts the value and a
band at the horizon, e.g. "7 號 128 → 122°C，預測穩定在 122±1°C". On the Spark this is the INT8
time-series model; here it is plain numpy and runs in well under a millisecond per machine.

Bad data (range / jump / stale / redundant-sensor mismatch / missing) is dropped per channel
before it reaches the baseline or the trigger, so it can never wake the commander.
One Anomaly per machine episode until reset(machine_id) or 5 min back to normal.

Plan vs actual (page 6, box 4): after attach_plan(store, sim) every ingest also compares the
active schedule with the plant once per sim minute (plan_check.PlanWatcher) and returns a
SCHEDULE_SLIP anomaly for a late de-mold, a late mold-in or WIP behind its back-propagated T.
"""
from __future__ import annotations

import math
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from warroom.contracts import (
    Anomaly,
    AnomalyKind,
    Channel,
    CompoundStage,
    DataQuality,
    MachineKind,
    Prediction,
    SensorReading,
    Severity,
)
from warroom.detect.plan_check import PlanWatcher
from warroom.detect.validate import JUMP_PER_S, REDUNDANT_TOL_C, check_channel, validate_reading
from warroom.factory import (
    COMPOUND_STOP_C,
    CURATIVE_STOP_C,
    CURE_TEMP_MAX_C,
    CURE_TEMP_MIN_C,
    LINE_LABELS,
    MACHINE_BY_ID,
    presses_on_header,
)
from warroom.sim.physics import COMPOUND_STAGES, THERMAL_TAU_S, approach, implied_press_temp

BASELINE_INIT_N = 10
BASELINE_TAU_S = 300.0
STALE_N = 15                     # identical samples in a row = frozen sensor
ALARM_HOLD_S = 10.0              # deviation must hold this long
PRESS_CONFIRM_S = 39.0           # press: let the excursion show its shape before forecasting
PRESS_DEV_C = 2.5
OTHER_DEV_C = 4.0
TREND_MIN_C_PER_MIN = 0.5
TREND_TSTAT = 6.0
PRESS_TREND_MIN_C_PER_MIN = 0.1
PRESS_TREND_MIN_AGE_S = 60.0
FLOW_LOW_FRAC = 0.7
FLOW_ONLY_HOLD_S = 45.0
PRESSURE_LOW_FRAC = 0.8
PRESSURE_HOLD_S = 20.0
REARM_NORMAL_S = 300.0
RECOVER_MAX_S = 1800.0          # after a repair: at most 30 min to come back before watching again
MAX_FIT_S = 600.0
TAUS = np.geomspace(3.0, 3600.0, 48)


def _t(ts: datetime) -> float:
    return ts.timestamp()


def machine_label(mid: str) -> str:
    d = MACHINE_BY_ID[mid]
    if d.kind is MachineKind.CURING_PRESS:
        return f"{int(d.short_name)} 號"
    if d.line.value in ("A", "B", "C", "D"):
        return f"{d.line.value} 線{d.name}"
    return f"{LINE_LABELS[d.line]}{d.name}"


def default_limits() -> dict[str, float]:
    """Upper temperature limit the forecast is compared with (stage-dependent, page 2/5)."""
    out: dict[str, float] = {}
    for mid, stage in COMPOUND_STAGES.items():
        if stage is CompoundStage.UNVULCANIZED:
            out[mid] = COMPOUND_STOP_C
        elif stage is CompoundStage.CURATIVE_ADDED:
            out[mid] = CURATIVE_STOP_C
    return out


# --------------------------------------------------------------------------- forecasting
@dataclass
class Fit:
    model: str
    params: tuple
    sigma: float
    cov: np.ndarray | None
    t0: float

    def value(self, t: float) -> float:
        x = t - self.t0
        if self.model == "constant":
            return float(self.params[0])
        if self.model == "linear-trend":
            a, b = self.params
            return float(a + b * x)
        a, b, tau = self.params
        return float(a + b * math.exp(-x / tau))

    def slope_per_min(self, t: float) -> float:
        x = t - self.t0
        if self.model == "constant":
            return 0.0
        if self.model == "linear-trend":
            return float(self.params[1] * 60.0)
        _, b, tau = self.params
        return float(-b / tau * math.exp(-x / tau) * 60.0)

    def half_band(self, t: float) -> float:
        x = t - self.t0
        if self.cov is None:
            var = self.sigma ** 2
        else:
            if self.model == "constant":
                row = np.array([1.0])
            elif self.model == "linear-trend":
                row = np.array([1.0, x])
            else:
                row = np.array([1.0, math.exp(-x / self.params[2])])
            var = float(row @ self.cov @ row) + self.sigma ** 2
        return 2.0 * math.sqrt(max(var, 0.0))

    def settles_within(self, horizon_s: float, t_now: float) -> bool:
        if self.model == "constant":
            return True
        if self.model == "exp-approach":
            return self.params[2] * 3.0 <= (t_now - self.t0) + horizon_s
        return False


def fit_series(ts: np.ndarray, vs: np.ndarray) -> Fit:
    """Pick constant / linear / exponential-approach by BIC."""
    n = len(vs)
    t0 = float(ts[0])
    x = ts - t0
    best: tuple[float, Fit] | None = None

    def consider(model: str, X: np.ndarray, extra: tuple, k: int) -> None:
        nonlocal best
        coef, *_ = np.linalg.lstsq(X, vs, rcond=None)
        resid = vs - X @ coef
        sse = float(resid @ resid)
        dof = max(1, n - k)
        sigma = math.sqrt(sse / dof) if sse > 0 else 1e-6
        bic = n * math.log(max(sse / n, 1e-12)) + k * math.log(n)
        try:
            cov = np.linalg.inv(X.T @ X) * sigma ** 2
        except np.linalg.LinAlgError:
            cov = None
        f = Fit(model, tuple(float(c) for c in coef) + extra, sigma, cov, t0)
        if best is None or bic < best[0]:
            best = (bic, f)

    consider("constant", np.ones((n, 1)), (), 1)
    if n >= 4:
        consider("linear-trend", np.column_stack([np.ones(n), x]), (), 2)
    if n >= 6:
        span = float(x[-1]) if n > 1 else 1.0
        for tau in TAUS:
            if tau > 4 * span:
                break
            consider("exp-approach", np.column_stack([np.ones(n), np.exp(-x / tau)]), (float(tau),), 3)
    assert best is not None
    return best[1]


# --------------------------------------------------------------------------- per-machine state
@dataclass
class _Chan:
    buf: deque = field(default_factory=deque)          # (t, v) validated
    baseline: float | None = None
    normal: float | None = None                        # the first baseline: the machine before any fault
    init: list = field(default_factory=list)
    diffs: deque = field(default_factory=lambda: deque(maxlen=60))
    sigma: float = 0.05
    last_valid: tuple[float, float] | None = None
    recent_raw: deque = field(default_factory=lambda: deque(maxlen=3))
    stale_val: float | None = None
    stale_n: int = 0


@dataclass
class _Track:
    mid: str
    chans: dict = field(default_factory=lambda: {c: _Chan() for c in Channel})
    expected_temp: float | None = None       # press redundant model
    last_t: float | None = None
    last_reading: SensorReading | None = None
    onset_t: float | None = None
    onset_sign: int = 0
    calm_since: float | None = None
    alarm_since: float | None = None
    flow_low_since: float | None = None
    pressure_low_since: float | None = None
    emitted: bool = False
    normal_since: float | None = None
    recovering: bool = False                 # repaired: on its way back to normal, nothing to report
    recover_since: float | None = None


class StreamDetector:
    """Implements contracts.Detector."""

    def __init__(self, clock=None, window_s: int = 1800,
                 fault_start_lookup: Callable[[str], datetime | None] | None = None,
                 limits: dict[str, float] | None = None):
        self.clock = clock
        self.window_s = window_s
        self.fault_start_lookup = fault_start_lookup     # e.g. Factory.fault_started_at (latency metric)
        self.limits = default_limits() if limits is None else dict(limits)
        self._tracks: dict[str, _Track] = {}
        self.quality_counts: Counter = Counter()          # (machine_id, DataQuality) -> n
        self.last_quality: dict[str, DataQuality] = {}
        self.bad_data_log: deque = deque(maxlen=500)      # (ts, machine_id, channel, quality)
        self.anomalies: list[Anomaly] = []
        self.plan_watcher = PlanWatcher()
        self._plan_store = None
        self._plan_sim = None

    # ------------------------------------------------------------------ plan vs actual (page 6)
    def attach_plan(self, store=None, sim=None) -> None:
        """Enable the once-per-minute plan-vs-actual check: store.current().schedule is the plan,
        sim.machines() / sim.semi_finished() the actual."""
        self._plan_store, self._plan_sim = store, sim

    def check_plan(self, now: datetime | None = None, *, force: bool = False) -> list[Anomaly]:
        """Compare plan with actual if a sim minute has passed (or force). Returns new slips."""
        if self._plan_sim is None:
            return []
        if now is None:
            now = self.clock.now() if self.clock is not None else self._plan_sim.now()
        if not force and not self.plan_watcher.due(now):
            return []
        schedule = None
        if self._plan_store is not None:
            try:
                cur = self._plan_store.current()
                schedule = getattr(cur, "schedule", cur)
            except Exception:  # noqa: BLE001 - no active schedule yet: check the plant-side slips only
                schedule = None
        semi = None
        try:
            semi = self._plan_sim.semi_finished()
        except Exception:  # noqa: BLE001
            semi = None
        return self.plan_watcher.check(now, schedule, self._plan_sim.machines(), semi)

    def _track(self, mid: str) -> _Track:
        tr = self._tracks.get(mid)
        if tr is None:
            tr = self._tracks[mid] = _Track(mid)
        return tr

    # ------------------------------------------------------------------ validation
    def _check(self, tr: _Track, r: SensorReading, neighbours: list[SensorReading] | None
               ) -> tuple[DataQuality, dict[Channel, DataQuality], float | None]:
        """Pure: quality of the reading, per-channel quality, and the press model's next expected temp."""
        t = _t(r.ts)
        per: dict[Channel, DataQuality] = {}
        for ch in Channel:
            v = getattr(r, ch.value)
            if v is None:
                continue
            c = tr.chans[ch]
            prev = c.last_valid
            q = check_channel(ch, v, prev[1] if prev else None, (t - prev[0]) if prev else 1.0)
            if q is DataQuality.JUMP and len(c.recent_raw) == 2:
                # three consistent samples at the new level = a real change, accept it
                vals = [x for _, x in c.recent_raw] + [v]
                if max(vals) - min(vals) <= JUMP_PER_S[ch] * 2 and all(
                        check_channel(ch, x, prev[1], 1.0) is DataQuality.JUMP for x in vals):
                    q = DataQuality.OK
            if q is DataQuality.OK and c.stale_val is not None and v == c.stale_val and c.stale_n + 1 >= STALE_N:
                q = DataQuality.STALE
            per[ch] = q
        if r.temp_c is None:
            per[Channel.TEMP] = DataQuality.MISSING
        expected = None
        d = MACHINE_BY_ID.get(r.machine_id)
        if d is not None and d.kind is MachineKind.CURING_PRESS and per.get(Channel.STEAM) is DataQuality.OK:
            dt = (t - tr.last_t) if tr.last_t is not None else 0.0
            if tr.expected_temp is not None and 0 < dt <= 5.0:
                expected = approach(tr.expected_temp, implied_press_temp(r.steam_kgf_cm2), dt, THERMAL_TAU_S)
                if per.get(Channel.TEMP) is DataQuality.OK and abs(r.temp_c - expected) > REDUNDANT_TOL_C:
                    per[Channel.TEMP] = DataQuality.NEIGHBOR_MISMATCH
        if neighbours and per.get(Channel.TEMP) is DataQuality.OK and validate_reading(r.model_copy(update={"pressure_kgf_cm2": None, "water_flow_lpm": None,
                                                     "steam_kgf_cm2": None}),
                                None, neighbours) is DataQuality.NEIGHBOR_MISMATCH:
            per[Channel.TEMP] = DataQuality.NEIGHBOR_MISMATCH
        overall = per.get(Channel.TEMP, DataQuality.MISSING)
        if overall is DataQuality.OK:
            overall = next((q for q in per.values() if q is not DataQuality.OK), DataQuality.OK)
        return overall, per, expected

    def validate(self, reading: SensorReading) -> DataQuality:
        tr = self._tracks.get(reading.machine_id) or _Track(reading.machine_id)
        return self._check(tr, reading, None)[0]

    def _commit(self, tr: _Track, r: SensorReading, per: dict[Channel, DataQuality], expected: float | None) -> None:
        t = _t(r.ts)
        for ch, q in per.items():
            v = getattr(r, ch.value)
            c = tr.chans[ch]
            if v is not None:
                c.recent_raw.append((t, v))
                if c.stale_val == v:
                    c.stale_n += 1
                else:
                    c.stale_val, c.stale_n = v, 1
            if q is not DataQuality.OK:
                if q is not DataQuality.MISSING or ch is Channel.TEMP:
                    self.quality_counts[(tr.mid, q)] += 1
                    self.bad_data_log.append((r.ts, tr.mid, ch, q))
                continue
            if c.last_valid is not None:
                c.diffs.append(abs(v - c.last_valid[1]) / max(1.0, t - c.last_valid[0]))
                if len(c.diffs) >= 10:
                    c.sigma = max(0.02, float(np.median(c.diffs)) * 1.4826 / math.sqrt(2))
            c.last_valid = (t, v)
            c.buf.append((t, v))
            while c.buf and c.buf[0][0] < t - self.window_s:
                c.buf.popleft()
        # press redundant model: follow the steam even when the temp sample was bad
        d = MACHINE_BY_ID.get(tr.mid)
        if d is not None and d.kind is MachineKind.CURING_PRESS:
            if expected is not None:
                tr.expected_temp = expected
            elif per.get(Channel.STEAM) is DataQuality.OK and per.get(Channel.TEMP) is DataQuality.OK:
                tr.expected_temp = r.temp_c          # (re)initialise after start or a gap
            elif per.get(Channel.STEAM) is not DataQuality.OK:
                tr.expected_temp = None
        tr.last_t = t
        tr.last_reading = r
        self.last_quality[tr.mid] = per.get(Channel.TEMP, DataQuality.MISSING) if any(
            q is not DataQuality.OK for q in per.values()) else DataQuality.OK

    # ------------------------------------------------------------------ baselines and triggers
    def _update_baseline(self, tr: _Track, ch: Channel, t: float, v: float) -> None:
        c = tr.chans[ch]
        if c.baseline is None:
            c.init.append(v)
            if len(c.init) >= BASELINE_INIT_N:
                c.baseline = c.normal = float(np.median(c.init))
            return
        frozen = (ch is Channel.TEMP and tr.onset_t is not None) or tr.recovering
        frozen = frozen or (ch is Channel.WATER_FLOW and tr.flow_low_since is not None)
        frozen = frozen or (ch is Channel.PRESSURE and tr.pressure_low_since is not None)
        if tr.emitted:
            frozen = True
        if not frozen:
            prev_t = c.buf[-2][0] if len(c.buf) >= 2 else t - 1.0
            alpha = min(1.0, max(0.0, t - prev_t) / BASELINE_TAU_S)
            c.baseline += alpha * (v - c.baseline)

    def _window(self, tr: _Track, ch: Channel, t_now: float) -> tuple[np.ndarray, np.ndarray]:
        c = tr.chans[ch]
        if ch is Channel.TEMP and tr.onset_t is not None:
            # skip the first seconds of the excursion: steam lag + thermal lag make its start
            # S-shaped, and a single exponential fitted over it underestimates the asymptote
            skip = min(8.0, 0.25 * (t_now - tr.onset_t))
            start = max(tr.onset_t + skip, t_now - MAX_FIT_S)
        else:
            start = t_now - 120.0
        pts = [(a, b) for a, b in c.buf if a >= start]
        if len(pts) < 3:
            pts = list(c.buf)[-3:]
        arr = np.array(pts, dtype=float).reshape(-1, 2)
        return arr[:, 0], arr[:, 1]

    def _forecast(self, tr: _Track, ch: Channel, horizon_s: float) -> tuple[Prediction, Fit] | None:
        c = tr.chans[ch]
        if not c.buf:
            return None
        t_now, v_now = c.buf[-1]
        ts, vs = self._window(tr, ch, t_now)
        fit = fit_series(ts, vs) if len(vs) >= 2 else Fit("constant", (float(v_now),), c.sigma, None, t_now)
        pred = fit.value(t_now + horizon_s)
        half = max(1.0, fit.half_band(t_now + horizon_s))
        p = Prediction(machine_id=tr.mid, channel=ch, now_value=round(float(v_now), 2),
                       predicted_value=round(pred, 2), band_lo=round(pred - half, 2), band_hi=round(pred + half, 2),
                       horizon_s=horizon_s, trend_per_min=round(fit.slope_per_min(t_now), 3), model=fit.model)
        return p, fit

    def predict(self, machine_id: str, channel: Channel = Channel.TEMP, horizon_s: float = 1800) -> Prediction:
        tr = self._tracks.get(machine_id)
        res = self._forecast(tr, Channel(channel), horizon_s) if tr is not None else None
        if res is None:
            raise KeyError(f"no validated {channel} data for {machine_id}")
        return res[0]

    def _crossing_s(self, fit: Fit, t_now: float, limit: float, rising: bool, horizon_s: float) -> float | None:
        for k in range(0, int(horizon_s) + 1, 10):
            v = fit.value(t_now + k)
            if (rising and v >= limit) or (not rising and v < limit):
                return float(k)
        return None

    def _significant_trend(self, tr: _Track, t: float, sign: int, min_c_per_min: float) -> bool:
        """Linear slope over the excursion window: right sign, big enough, t-stat >= TREND_TSTAT."""
        ts_, vs_ = self._window(tr, Channel.TEMP, t)
        if len(vs_) < 10:
            return False
        x = ts_ - ts_[0]
        A = np.column_stack([np.ones_like(x), x])
        coef, *_ = np.linalg.lstsq(A, vs_, rcond=None)
        resid = vs_ - A @ coef
        s2 = float(resid @ resid) / max(1, len(vs_) - 2)
        sxx = float(((x - x.mean()) ** 2).sum())
        se = math.sqrt(s2 / sxx) if sxx > 0 and s2 > 0 else 1e-9
        slope = float(coef[1]) * sign
        return slope * 60.0 >= min_c_per_min and slope / se >= TREND_TSTAT

    def _evaluate(self, tr: _Track, t: float, ts: datetime) -> Anomaly | None:
        d = MACHINE_BY_ID.get(tr.mid)
        if d is None:
            return None
        is_press = d.kind is MachineKind.CURING_PRESS
        temp = tr.chans[Channel.TEMP]
        flow = tr.chans[Channel.WATER_FLOW]
        pres = tr.chans[Channel.PRESSURE]
        # --- flow / pressure state
        if flow.baseline is not None and flow.baseline >= 5.0 and flow.last_valid and flow.last_valid[0] == t:
            low = flow.last_valid[1] < FLOW_LOW_FRAC * flow.baseline
            tr.flow_low_since = (tr.flow_low_since or t) if low else None
        if pres.baseline is not None and pres.baseline >= 1.0 and pres.last_valid and pres.last_valid[0] == t:
            low = pres.last_valid[1] < PRESSURE_LOW_FRAC * pres.baseline
            tr.pressure_low_since = (tr.pressure_low_since or t) if low else None
        if temp.baseline is None or not temp.last_valid or temp.last_valid[0] != t:
            return None
        v = temp.last_valid[1]
        dev = v - temp.baseline
        if tr.recovering:
            # repaired: the way back to normal is not an anomaly; watch again once it is back (or after 30 min)
            tr.recover_since = tr.recover_since or t
            if abs(dev) < max(1.0, 5.0 * temp.sigma) or t - tr.recover_since >= RECOVER_MAX_S:
                tr.recovering, tr.recover_since = False, None
            return None
        onset_thr = max(1.0, 5.0 * temp.sigma)
        dev_thr = PRESS_DEV_C if is_press else OTHER_DEV_C
        # --- onset / alarm bookkeeping
        if tr.onset_t is None:
            if abs(dev) >= onset_thr:
                tr.onset_t, tr.onset_sign, tr.calm_since = t, (1 if dev > 0 else -1), None
        else:
            if abs(dev) < onset_thr / 2:
                tr.calm_since = tr.calm_since or t
                if t - tr.calm_since >= 30.0 and not tr.emitted:
                    tr.onset_t, tr.onset_sign, tr.alarm_since = None, 0, None
            else:
                tr.calm_since = None
        beyond = is_press and (v < CURE_TEMP_MIN_C or v > CURE_TEMP_MAX_C)
        in_alarm = tr.onset_t is not None and (dev * tr.onset_sign >= dev_thr or beyond)
        tr.alarm_since = (tr.alarm_since or t) if in_alarm else None
        # --- re-arm after a long calm period
        if tr.emitted:
            if abs(dev) < onset_thr and tr.flow_low_since is None and tr.pressure_low_since is None:
                tr.normal_since = tr.normal_since or t
                if t - tr.normal_since >= REARM_NORMAL_S:
                    tr.emitted, tr.onset_t, tr.alarm_since, tr.normal_since = False, None, None, None
            else:
                tr.normal_since = None
            return None
        held = tr.alarm_since is not None and t - tr.alarm_since >= ALARM_HOLD_S
        limit = self.limits.get(tr.mid)
        if is_press:
            # a platen is steam heated: a real temperature change is explained by the steam;
            # one the steam does not explain is a sensor going bad, so wait for validation to flag it
            if tr.expected_temp is not None and abs(v - tr.expected_temp) > REDUNDANT_TOL_C / 2:
                return None
            if held and (t - tr.onset_t >= PRESS_CONFIRM_S or beyond):
                kind = AnomalyKind.TEMP_DROP if dev < 0 else AnomalyKind.OVERHEAT
                return self._make(tr, ts, t, kind, Channel.TEMP, v, temp.baseline, rule={
                    "rule": "press_deviation_held", "label": "硫化機溫度偏離基準並持續",
                    "checks": [f"偏離 {dev:+.1f}°C（門檻 ±{dev_thr}°C）", f"持續 {t - tr.alarm_since:.0f} 秒（門檻 {ALARM_HOLD_S:.0f} 秒）",
                               f"確認 {t - tr.onset_t:.0f} 秒（門檻 {PRESS_CONFIRM_S:.0f} 秒）" if not beyond else
                               f"已超出 {CURE_TEMP_MIN_C:.0f}–{CURE_TEMP_MAX_C:.0f}°C"]})
            # slow drift (e.g. a leak): a significant trend heading out of 120–130 within 30 min
            if tr.onset_t is not None and t - tr.onset_t >= PRESS_TREND_MIN_AGE_S and \
                    self._significant_trend(tr, t, tr.onset_sign, PRESS_TREND_MIN_C_PER_MIN):
                res = self._forecast(tr, Channel.TEMP, 1800.0)
                if res is not None:
                    falling = tr.onset_sign < 0
                    lim = CURE_TEMP_MIN_C if falling else CURE_TEMP_MAX_C
                    cross = self._crossing_s(res[1], t, lim, not falling, 1800.0)
                    if cross is not None:
                        kind = AnomalyKind.TEMP_DROP if falling else AnomalyKind.OVERHEAT
                        return self._make(tr, ts, t, kind, Channel.TEMP, v, temp.baseline, rule={
                            "rule": "press_slow_drift", "label": "硫化機緩慢漂移，預測 30 分內出界",
                            "checks": [f"斜率 {res[1].slope_per_min(t):+.2f}°C/分（門檻 {PRESS_TREND_MIN_C_PER_MIN}°C/分）",
                                       f"預測約 {cross / 60:.0f} 分後越過 {lim:.0f}°C"]})
            return None
        # compound / other machines
        rising_trend = tr.onset_t is not None and tr.onset_sign > 0 and t - tr.onset_t >= 5.0 and \
            self._significant_trend(tr, t, 1, TREND_MIN_C_PER_MIN)
        if rising_trend and (tr.flow_low_since is not None or limit is not None):
            res = self._forecast(tr, Channel.TEMP, 1800.0)
            crosses = limit is not None and res is not None and \
                self._crossing_s(res[1], t, limit, True, 1800.0) is not None
            if tr.flow_low_since is not None or crosses:
                return self._make(tr, ts, t, AnomalyKind.OVERHEAT, Channel.TEMP, v, temp.baseline, rule={
                    "rule": "compound_rising", "label": "膠溫持續上升（冷卻水偏低或預測達上限）",
                    "checks": [f"上升趨勢 ≥ {TREND_MIN_C_PER_MIN}°C/分",
                               "冷卻水流低於基準 70%" if tr.flow_low_since is not None else f"預測 30 分內達 {limit:.0f}°C 上限"]})
        if held:
            kind = AnomalyKind.OVERHEAT if dev > 0 else AnomalyKind.TEMP_DROP
            return self._make(tr, ts, t, kind, Channel.TEMP, v, temp.baseline, rule={
                "rule": "deviation_held", "label": "溫度偏離基準並持續",
                "checks": [f"偏離 {dev:+.1f}°C（門檻 ±{dev_thr}°C）", f"持續 {t - tr.alarm_since:.0f} 秒（門檻 {ALARM_HOLD_S:.0f} 秒）"]})
        if tr.flow_low_since is not None and t - tr.flow_low_since >= FLOW_ONLY_HOLD_S:
            return self._make(tr, ts, t, AnomalyKind.WATER_FLOW_LOW, Channel.WATER_FLOW, flow.last_valid[1],
                              flow.baseline, rule={"rule": "flow_low", "label": "冷卻水流偏低並持續",
                                                   "checks": [f"低於基準 {FLOW_LOW_FRAC:.0%}", f"持續 ≥ {FLOW_ONLY_HOLD_S:.0f} 秒"]})
        if tr.pressure_low_since is not None and t - tr.pressure_low_since >= PRESSURE_HOLD_S:
            return self._make(tr, ts, t, AnomalyKind.PRESSURE_LOW, Channel.PRESSURE, pres.last_valid[1],
                              pres.baseline, rule={"rule": "pressure_low", "label": "壓力偏低並持續",
                                                   "checks": [f"低於基準 {PRESSURE_LOW_FRAC:.0%}", f"持續 ≥ {PRESSURE_HOLD_S:.0f} 秒"]})
        return None

    def _make(self, tr: _Track, ts: datetime, t: float, kind: AnomalyKind, ch: Channel, value: float,
              baseline: float, rule: dict | None = None) -> Anomaly:
        d = MACHINE_BY_ID[tr.mid]
        is_press = d.kind is MachineKind.CURING_PRESS
        horizon = 1800.0
        res = self._forecast(tr, ch, horizon)
        pred, fit = res if res else (None, None)
        label = machine_label(tr.mid)
        severity = Severity.WARN
        limit = self.limits.get(tr.mid)
        flow = tr.chans[Channel.WATER_FLOW]
        if is_press and ch is Channel.TEMP and pred is not None:
            half = max(1, round((pred.band_hi - pred.band_lo) / 2))
            arrow = f"{label} {baseline:.0f} → {value:.0f}°C"
            if fit.settles_within(horizon, t):
                msg = f"{arrow}，預測穩定在 {pred.predicted_value:.0f}±{half}°C"
            else:
                msg = f"{arrow}，持續{'下降' if pred.trend_per_min < 0 else '上升'} " \
                      f"{abs(pred.trend_per_min):.2f}°C/分，預測 30 分後 {pred.predicted_value:.1f}°C"
            if pred.band_lo < CURE_TEMP_MIN_C or pred.band_hi > CURE_TEMP_MAX_C or \
                    not CURE_TEMP_MIN_C <= value <= CURE_TEMP_MAX_C:
                severity = Severity.CRITICAL
        elif kind is AnomalyKind.OVERHEAT and pred is not None:
            msg = f"{label} 膠溫 {value:.0f}°C 持續上升（{pred.trend_per_min:+.1f}°C/分）"
            if tr.flow_low_since is not None and flow.last_valid:
                msg += f"，水流偏低 {flow.last_valid[1]:.0f} L/min（基準 {flow.baseline:.0f}）"
            cross = self._crossing_s(fit, t, limit, True, horizon) if limit is not None else None
            if cross is not None:
                msg += f"；預測約 {max(1, round(cross / 60))} 分後達 {limit:.0f}°C 上限"
                severity = Severity.CRITICAL
            else:
                msg += f"；預測 30 分後 {pred.predicted_value:.0f}°C"
        elif kind is AnomalyKind.WATER_FLOW_LOW:
            msg = f"{label} 冷卻水流偏低 {value:.0f} L/min（基準 {baseline:.0f}）"
        elif kind is AnomalyKind.PRESSURE_LOW:
            msg = f"{label} 壓力偏低 {value:.1f}（基準 {baseline:.1f} kgf/cm²）"
        else:
            msg = f"{label} {ch.value} {baseline:.1f} → {value:.1f}"
        related = [p for p in presses_on_header(d.steam_header) if p != tr.mid] if d.steam_header else []
        latency = None
        if self.fault_start_lookup is not None:
            fs = self.fault_start_lookup(tr.mid)
            if fs is not None:
                latency = round((ts - fs).total_seconds(), 1)
        tr.emitted = True
        tr.normal_since = None
        # issue #54: the pre-classification the dashboard shows before any LLM runs
        chan = tr.chans[ch]
        validation = {"range": "通過", "jump": "通過", "stale": "通過",
                      "quality": self.last_quality.get(tr.mid, DataQuality.OK).value}
        if is_press and tr.expected_temp is not None and ch is Channel.TEMP:
            validation["steam_model"] = f"蒸汽推算 {tr.expected_temp:.1f}°C，差 {abs(value - tr.expected_temp):.1f}°C（容許 {REDUNDANT_TOL_C}°C）"
        if related:
            validation["neighbours"] = f"同蒸汽管線 {len(related)} 台比對"
        preclass = {**(rule or {"rule": kind.value, "label": kind.value, "checks": []}),
                    "kind": kind.value, "channel": ch.value, "value": round(value, 2), "baseline": round(baseline, 2),
                    "sigma": round(chan.sigma, 3), "severity": severity.value, "validation": validation,
                    "forecast": None if pred is None else {"model": fit.model, "predicted": pred.predicted_value,
                                                           "band": [pred.band_lo, pred.band_hi], "horizon_min": round(horizon / 60),
                                                           "trend_per_min": pred.trend_per_min},
                    "limit": limit}
        return Anomaly(id=f"AN-{ts:%Y%m%d-%H%M%S}-{tr.mid}", ts=ts, machine_id=tr.mid, kind=kind, channel=ch,
                       value=round(value, 2), baseline=round(baseline, 2), prediction=pred, severity=severity,
                       message=msg, related_machines=related, detection_latency_s=latency, preclass=preclass)

    # ------------------------------------------------------------------ public ingest
    def ingest(self, readings: list[SensorReading]) -> list[Anomaly]:
        """Validate, update and return NEW anomalies only (debounced per machine episode)."""
        by_header: dict[str, list[SensorReading]] = {}
        for r in readings:
            d = MACHINE_BY_ID.get(r.machine_id)
            if d is not None and d.steam_header:
                by_header.setdefault(d.steam_header, []).append(r)
        out: list[Anomaly] = []
        for r in readings:
            if r.machine_id not in MACHINE_BY_ID:
                continue
            tr = self._track(r.machine_id)
            d = MACHINE_BY_ID[r.machine_id]
            neigh = [n for n in by_header.get(d.steam_header, []) if n.machine_id != r.machine_id] \
                if d.steam_header else None
            _, per, expected = self._check(tr, r, neigh)
            self._commit(tr, r, per, expected)
            t = _t(r.ts)
            for ch, q in per.items():
                if q is DataQuality.OK:
                    self._update_baseline(tr, ch, t, getattr(r, ch.value))
            a = self._evaluate(tr, t, r.ts)
            if a is not None:
                out.append(a)
        out = self._merge_headers(out)
        if self._plan_sim is not None and readings:
            out += self.check_plan(max(r.ts for r in readings))
        self.anomalies.extend(out)
        return out

    def _merge_headers(self, found: list[Anomaly]) -> list[Anomaly]:
        """Two or more presses on one steam header deviating together = one MULTI_MACHINE event."""
        result: list[Anomaly] = []
        used: set[str] = set()
        for a in found:
            if a.id in used:
                continue
            d = MACHINE_BY_ID[a.machine_id]
            if d.kind is not MachineKind.CURING_PRESS or not d.steam_header:
                result.append(a)
                continue
            peers = [p for p in presses_on_header(d.steam_header) if p != a.machine_id]
            group = [a.machine_id]
            for p in peers:
                tr = self._tracks.get(p)
                if tr is None:
                    continue
                other = next((x for x in found if x.machine_id == p), None)
                if other is not None or (tr.alarm_since is not None and not tr.emitted):
                    group.append(p)
                    tr.emitted = True
                    if other is not None:
                        used.add(other.id)
            if len(group) >= 2:
                a = a.model_copy(update={
                    "kind": AnomalyKind.MULTI_MACHINE, "related_machines": group[1:],
                    "severity": Severity.CRITICAL,
                    "message": f"{d.steam_header} 蒸汽管線 {'、'.join(machine_label(g) for g in group)} 同時偏離："
                               + a.message})
            result.append(a)
        return result

    def reset(self, machine_id: str | None = None, *, repaired: bool = False) -> None:
        """Close the episode. With a machine id the baseline restarts from the last 30 s of
        valid data (the new normal); with None every track starts fresh. repaired=True (the fault was
        fixed): the baseline goes back to the machine's normal, so the way back is not a new 驟降 / 驟升
        (Kyle 10-21: 回報處置完成 → 「A 線大開煉機溫度驟降」)."""
        if machine_id is None:
            self._tracks.clear()
            self.plan_watcher.reset()
            return
        tr = self._tracks.get(machine_id)
        if tr is None:
            return
        for c in tr.chans.values():
            if repaired and c.normal is not None:
                c.baseline = c.normal
            elif c.buf:
                t_now = c.buf[-1][0]
                recent = [v for t, v in c.buf if t >= t_now - 30.0]
                c.baseline = float(np.median(recent)) if recent else c.baseline
        tr.onset_t, tr.onset_sign, tr.alarm_since, tr.calm_since = None, 0, None, None
        tr.flow_low_since = tr.pressure_low_since = None
        tr.emitted, tr.normal_since = False, None
        tr.recovering, tr.recover_since = repaired, None
