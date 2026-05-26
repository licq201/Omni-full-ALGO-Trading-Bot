"""
orchestrator.py — Per-cycle glue for OMNI-ICT (Phase 3 / Phase 4).

Responsibility
--------------
Once per cycle (driven externally — cron, LaunchAgent, or a while-loop in
auto_trader.py), for each symbol in rules.watchlist:

    1. Load HTF bars (default H1) and LTF bars (default M5).
    2. Run smc_engine.analyze() on each to get SMCSnapshots.
    3. Ask dual_tf_selector.select_trade() for a TradeSelection.
    4. If a position is open for this symbol, ask scaling_engine.evaluate()
       for an ADD/REDUCE/HOLD/CLOSE recommendation.
    5. Wrap into a unified Signal envelope and write signals.json +
       omni_pine_overlay.pine atomically.

The orchestrator is deliberately thin. It owns no detection logic — it only
composes pure functions from the engines and writes to disk.

Design notes
------------
 * Bar fetching is pluggable via a `BarFetcher` Protocol so this module can be
   driven by either the live MT5 connector or an offline fixture for tests and
   paper-forward simulation.
 * If rules.dual_tf.enabled is False, the orchestrator still runs but will
   emit only NEUTRAL/non-actionable signals — useful for sanity-checking the
   pipeline against live data without executing trades.
 * All file paths come from rules.signals (with inline fallback) so no code
   changes are needed to relocate the outputs.
 * No network or MT5 calls live here; keep this module pure-enough to unit test.

CLI
---
    python orchestrator.py --dry-run            # run one cycle on fixtures
    python orchestrator.py --symbols EURUSD     # limit to one symbol
    python orchestrator.py --loop 60            # run forever, one cycle / 60s
"""

from __future__ import annotations

import argparse
import concurrent.futures as _cf
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

# Local imports — pure modules, safe to import at module load.
from smc_engine import Bar, analyze, _make_fixture
from dual_tf_selector import select_trade, TradeSelection
from scaling_engine import evaluate as scale_evaluate, PositionCtx, ScaleAction
from signal_writers import (
    Signal, build_signal, build_signals_payload,
    write_signals_json, prune_signals,
)
from i18n import msg as _msg
from pine_codegen import write_pine
from amd_engine import detect_amd, AMDPhase, pip_size_for

log = logging.getLogger("orchestrator")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
DEFAULT_RULES_PATH = HERE / "rules.json"
DEFAULT_SIGNALS_DIR = PROJECT_ROOT / "shared"
DEFAULT_PINE_PATH = PROJECT_ROOT / "pine" / "omni_pine_overlay.pine"


# ──────────────────────────────────────────────────────────────────────────────
# Pluggable bar fetcher
# ──────────────────────────────────────────────────────────────────────────────

class BarFetcher(Protocol):
    """Fetch recent bars for a given symbol + timeframe."""
    def fetch(self, symbol: str, timeframe: str, n: int) -> list[Bar]: ...


def _fetch_with_timeout(fetcher: BarFetcher, symbol: str, tf: str, n: int,
                        timeout_s: float = 30.0) -> list[Bar]:
    """Fetch bars with a hard timeout; raises TimeoutError if the fetcher hangs."""
    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fetcher.fetch, symbol, tf, n)
        return fut.result(timeout=timeout_s)


_Y2000_TS = 946684800.0  # Unix timestamp for 2000-01-01; bars before this are fixtures


def _bars_are_fresh(bars: list[Bar], max_age_s: int = 3600) -> bool:
    """Return False if the newest bar is older than max_age_s seconds.
    Bars with timestamps near the Unix epoch (e.g. fixture data) are always considered fresh."""
    if not bars:
        return False
    newest = max((b.time for b in bars if b.time), default=0.0)
    if newest < _Y2000_TS:
        return True  # fixture / synthetic data — skip staleness check
    return (time.time() - newest) < max_age_s


class FixtureBarFetcher:
    """Fake fetcher used by --dry-run and tests."""
    def fetch(self, symbol: str, timeframe: str, n: int) -> list[Bar]:
        bars = _make_fixture()
        return bars[-n:] if n and n < len(bars) else bars


class MT5BarFetcher:
    """
    Thin adapter over mt5_connector. Imported lazily so that the orchestrator
    can run on machines without the MT5 terminal installed (e.g. CI).
    """
    def __init__(self):
        self._mod = None

    def _lazy(self):
        if self._mod is None:
            import importlib
            try:
                self._mod = importlib.import_module("mt5_connector")
            except Exception as e:  # pragma: no cover
                raise RuntimeError(f"mt5_connector not available: {e}") from e
        return self._mod

    def fetch(self, symbol: str, timeframe: str, n: int) -> list[Bar]:
        mod = self._lazy()
        raw = mod.get_bars(symbol, timeframe, n)
        bars = []
        for r in raw:
            # Prefer the broker-offset-corrected UTC timestamp set by
            # mt5_connector.get_bars(). Fall back to local parsing for
            # backward compat with older callers that don't surface it.
            t = r.get("time_utc")
            if t is None or t == 0:
                raw_t = r.get("time", 0)
                if isinstance(raw_t, str):
                    try:
                        t = datetime.strptime(raw_t, "%Y.%m.%d %H:%M:%S").replace(
                            tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        t = 0.0
                else:
                    t = float(raw_t or 0)
            bars.append(Bar(
                time=float(t),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]),   close=float(r["close"]),
            ))
        return bars


# ──────────────────────────────────────────────────────────────────────────────
# Position state loader (optional)
# ──────────────────────────────────────────────────────────────────────────────

def _load_open_positions(path: Optional[Path]) -> dict[str, PositionCtx]:
    """
    Load open positions keyed by symbol. Returns {} when file missing.
    Format (JSON): [{"symbol":"EURUSD","direction":"BULL","entry":1.10,"sl":1.099,"current":1.102,"size":0.1,"add_count":0}, …]
    """
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("could not parse open positions %s: %s", path, e)
        return {}
    out: dict[str, PositionCtx] = {}
    for r in raw or []:
        try:
            initial_sl = float(r.get("initial_sl", r.get("sl", r["entry"])))
            out[r["symbol"]] = PositionCtx(
                symbol=r["symbol"],
                direction=r["direction"],
                entry_price=float(r["entry"]),
                current_price=float(r.get("current", r["entry"])),
                initial_sl=initial_sl,
                current_sl=float(r.get("current_sl", r.get("sl", initial_sl))),
                volume=float(r.get("volume", r.get("size", 0.01))),
                add_count=int(r.get("add_count", 0)),
            )
        except Exception as e:  # skip malformed rows
            log.warning("skipping bad position row %s: %s", r, e)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Cycle result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CycleResult:
    ts:        str
    symbols:   list[str]
    signals:   list[Signal]
    written:   list[str]
    errors:    list[str]

    def as_dict(self) -> dict:
        return {
            "ts":      self.ts,
            "symbols": self.symbols,
            "signals": [s.id for s in self.signals],
            "written": self.written,
            "errors":  self.errors,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Core: run one cycle
# ──────────────────────────────────────────────────────────────────────────────

def run_cycle(
    rules: dict,
    *,
    fetcher: BarFetcher,
    symbols: Optional[list[str]] = None,
    open_positions: Optional[dict[str, PositionCtx]] = None,
    signals_path: Optional[Path] = None,
    pine_path: Optional[Path] = None,
    max_kept: int = 20,
    n_htf: int = 200,
    n_ltf: int = 300,
) -> CycleResult:
    """
    Execute one end-to-end cycle and emit signals.json + pine overlay.

    Parameters
    ----------
    rules : dict      — parsed rules.json
    fetcher : BarFetcher — source of bars (MT5 / fixtures / mock)
    symbols : optional subset of rules.watchlist
    open_positions : dict keyed by symbol, optional
    signals_path : override output location for signals.json
    pine_path    : override output location for .pine file
    """
    open_positions = open_positions or {}
    watchlist = symbols or rules.get("watchlist", [])
    dt_cfg = rules.get("dual_tf", {}) or {}
    htf_tf  = dt_cfg.get("htf_timeframe",   "H1")
    ltf_tf  = dt_cfg.get("ltf_timeframe",   "M5")
    # Macro timeframe for D1/H4 cascade confirmation (skipped on error/fixture)
    macro_tf = dt_cfg.get("macro_timeframe", "H4")

    ts_now = datetime.now(timezone.utc)
    ts_iso = ts_now.isoformat()

    produced: list[Signal] = []
    errors:   list[str] = []
    written:  list[str] = []

    # Optional asset rotation — drop out-of-session symbols before scanning.
    try:
        from asset_rotation_manager import AssetRotationManager
        _arm = AssetRotationManager(symbols=watchlist)
        _filtered = [s for s in watchlist if not _arm.should_skip_asset(s)]
        if _filtered:
            watchlist = _filtered
    except Exception:
        pass

    # Optional regime detector — runs once per HTF series, attached to signal
    # metadata so consumers (auto_trader, dashboard) can adapt.
    try:
        from regime_detector import detect_regime
        _have_regime = True
    except Exception:
        _have_regime = False
        detect_regime = None  # type: ignore

    for symbol in watchlist:
        try:
            htf_bars   = _fetch_with_timeout(fetcher, symbol, htf_tf,   n_htf)
            ltf_bars   = _fetch_with_timeout(fetcher, symbol, ltf_tf,   n_ltf)
            # Fetch macro bars for H4/D1 cascade; silently skip on failure
            macro_bars = None
            try:
                macro_bars = _fetch_with_timeout(fetcher, symbol, macro_tf, 100)
            except Exception:
                pass

            # Freshness check — skip if bars haven't been updated recently
            if not _bars_are_fresh(htf_bars):
                # Diagnostic: when staleness fires, log enough info to
                # pinpoint why. (Cheap — only on the failure path.)
                if htf_bars:
                    times = [b.time for b in htf_bars if b.time]
                    if times:
                        newest = max(times)
                        oldest = min(times)
                        age_min = (time.time() - newest) / 60.0
                        log.warning(
                            "stale-debug %s: bars=%d, newest=%.0f (age=%+.1fmin), "
                            "oldest=%.0f, now=%.0f",
                            symbol, len(htf_bars), newest, age_min,
                            oldest, time.time(),
                        )
                    else:
                        log.warning("stale-debug %s: bars=%d but ALL b.time are 0/falsy",
                                    symbol, len(htf_bars))
                else:
                    log.warning("stale-debug %s: htf_bars is EMPTY", symbol)
                errors.append(f"{symbol}: stale HTF bars (oldest > 1h)")
                log.warning("stale HTF bars for %s — skipping cycle", symbol)
                continue

            # select_trade runs analyze() internally on the raw bars, so pass
            # bars in directly. We also compute snapshots for the scaling engine.
            htf_snap = analyze(htf_bars)
            ltf_snap = analyze(ltf_bars)

            sel: TradeSelection = select_trade(htf_bars, ltf_bars,
                                               rules=dt_cfg or None,
                                               macro_bars=macro_bars)

            # ── Compute EMA20/200/800 on ALL available timeframes ────────
            # LIMIT orders = scan speed is NOT critical. Fetch max history
            # MT5 EA now exports: M5=1800 M15=600 M30=400 H1=300 H4=100 D1=60
            tf_emas = {}
            for tf_name, tf_n in [
                ("M5",  1800),   # ~6 days of M5 → EMA800 valid
                ("M15",  600),   # ~6 days of M15 → EMA800 valid
                ("M30",  400),   # ~8 days of M30 → EMA800 valid
                ("H1",   300),   # ~12 days of H1 → EMA800 valid
                ("H4",   100),   # ~16 days of H4
                ("D1",    60),   # ~2 months of D1
                ("W1",    20),   # ~5 months of W1 (may be 0 for some symbols)
            ]:
                try:
                    tf_bars = _fetch_with_timeout(fetcher, symbol, tf_name, tf_n)
                    if tf_bars and len(tf_bars) >= 20:
                        from ict_precision import _calc_ema
                        closes = [b.close for b in tf_bars]
                        emas_tf = {"ema20": round(_calc_ema(closes, 20)[-1], 5)}
                        if len(tf_bars) >= 200:
                            emas_tf["ema200"] = round(_calc_ema(closes, 200)[-1], 5)
                        if len(tf_bars) >= 800:
                            emas_tf["ema800"] = round(_calc_ema(closes, 800)[-1], 5)
                        tf_emas[tf_name] = emas_tf
                except Exception:
                    pass  # TF not available — skip silently
            sel.tf_emas = tf_emas

            scale_act: Optional[ScaleAction] = None
            pos = open_positions.get(symbol)
            if pos is not None:
                scale_act = scale_evaluate(pos, htf_snap, ltf_snap,
                                           rules=rules.get("scaling"))

            sig = build_signal(symbol, ltf_tf, sel, scale=scale_act, ts=ts_now)

            # AMD scan — runs on M15 bars; result merged into signal metadata.
            try:
                amd_cfg = rules.get("amd", {})
                if amd_cfg.get("enabled", True):
                    m15_bars = _fetch_with_timeout(fetcher, symbol, "M15", 200)
                    if _bars_are_fresh(m15_bars):
                        ps = pip_size_for(symbol)
                        amd = detect_amd(
                            m15_bars,
                            pip_size=ps,
                            asian_start_h=amd_cfg.get("asian_start_h", 0),
                            asian_end_h=amd_cfg.get("asian_end_h", 7),
                            min_confidence=amd_cfg.get("min_confidence", 0.50),
                        )
                        if amd is not None:
                            amd_meta = {
                                "phase":      amd.phase.value,
                                "direction":  amd.direction,
                                "confidence": round(amd.confidence, 3),
                                "reasons":    amd.reasons,
                            }
                            if amd.asian_range:
                                amd_meta["asian_high"] = amd.asian_range.high
                                amd_meta["asian_low"]  = amd.asian_range.low
                            if amd.phase == AMDPhase.DISTRIBUTION and amd.entry:
                                amd_meta.update({
                                    "entry":  amd.entry,
                                    "sl":     amd.sl,
                                    "tp1":    amd.tp1,
                                    "tp2":    amd.tp2,
                                    "tp3":    amd.tp3,
                                    "rr_tp1": amd.rr_tp1,
                                    "rr_tp2": amd.rr_tp2,
                                    "rr_tp3": amd.rr_tp3,
                                })
                                # Elevate signal confidence when AMD aligns with
                                # the dual-TF direction.
                                if (amd.direction == "BULL" and sel.direction == "BULL") or \
                                   (amd.direction == "BEAR" and sel.direction == "BEAR"):
                                    bonus = min(0.15, amd.confidence * 0.20)
                                    if hasattr(sig, "confidence"):
                                        sig = sig.__class__(
                                            **{**sig.__dict__,
                                               "confidence": min(1.0, sig.confidence + bonus)}
                                        )
                                    amd_meta["dual_tf_aligned"] = True
                                    amd_meta["confidence_bonus"] = round(bonus, 3)
                                else:
                                    amd_meta["dual_tf_aligned"] = False

                            # HTF liquidity pools (always surfaced)
                            if amd.htf_liquidity:
                                amd_meta["htf_liquidity"] = [
                                    {"price": lv.price, "kind": lv.kind,
                                     "distance": round(lv.distance, 6)}
                                    for lv in amd.htf_liquidity
                                ]

                            # Continuation (Stage 2) — re-accumulation → re-manip → D2
                            if amd.continuation is not None:
                                c = amd.continuation
                                amd_meta["continuation"] = {
                                    "direction":   c.direction,
                                    "confidence":  round(c.confidence, 3),
                                    "re_accum_high": c.re_accum.high,
                                    "re_accum_low":  c.re_accum.low,
                                    "re_accum_bars": c.re_accum.bar_count,
                                    "re_manip_extreme": c.re_manip.extreme,
                                    "entry":  c.entry,
                                    "sl":     c.sl,
                                    "tp1":    c.tp1,
                                    "tp2":    c.tp2,
                                    "tp3":    c.tp3,
                                    "rr_tp1": c.rr_tp1,
                                    "rr_tp2": c.rr_tp2,
                                    "rr_tp3": c.rr_tp3,
                                    "targets": [
                                        {"price": t.price, "kind": t.kind}
                                        for t in c.targets
                                    ],
                                    "reasons": c.reasons,
                                }

                            if hasattr(sig, "metadata") and isinstance(sig.metadata, dict):
                                sig.metadata["amd"] = amd_meta
                            log.info(
                                "AMD %s: phase=%s dir=%s conf=%.2f%s",
                                symbol, amd.phase.value, amd.direction, amd.confidence,
                                f" +continuation@{amd.continuation.confidence:.2f}"
                                if amd.continuation else "",
                            )
            except Exception as _amd_err:
                log.debug("amd scan skipped for %s: %s", symbol, _amd_err)

            # Attach regime metadata if detector is available
            if _have_regime and detect_regime:
                try:
                    bars_as_dict = [
                        {"high": b.high, "low": b.low, "close": b.close}
                        for b in htf_bars
                    ]
                    rr = detect_regime(bars_as_dict)
                    if hasattr(sig, "metadata") and isinstance(sig.metadata, dict):
                        sig.metadata.setdefault("regime", {}).update({
                            "label":      rr.regime,
                            "confidence": round(rr.confidence, 3),
                            "reason":     rr.reason,
                        })
                except Exception as _e:
                    log.debug("regime detection skipped for %s: %s", symbol, _e)

            # ── AMD phase veto — only trade during MANIPULATION or DISTRIBUTION ──
            # ACCUMULATION = ranging chop — NO trades. This is the single biggest
            # filter for trade quality.
            amd_phase_value = amd_meta.get("phase", "") if amd_meta else ""
            if amd_cfg.get("veto_accumulation", True) and amd_phase_value == "ACCUMULATION":
                log.info(_msg("orchestrator.amd_veto", symbol=symbol))
                continue  # Skip this symbol entirely

            produced.append(sig)
        except Exception as e:
            errors.append(f"{symbol}: {type(e).__name__}: {e}")
            log.exception("cycle error for %s", symbol)

    sig_cfg = rules.get("signals", {})

    # Prune to bound file size — rules.signals.max_kept overrides caller default
    effective_max = max_kept if max_kept != 20 else int(sig_cfg.get("max_kept", 20))
    kept = prune_signals(produced, max_kept=effective_max)

    # Write outputs — rules.signals paths override module-level defaults
    s_path = (Path(signals_path) if signals_path else
              PROJECT_ROOT / sig_cfg.get("output_dir", "shared") / "signals.json")
    p_path = (Path(pine_path) if pine_path else
              PROJECT_ROOT / sig_cfg.get("pine_path", "pine/omni_pine_overlay.pine"))
    mirror_path = sig_cfg.get("mirror_mt5_common_path", "")

    try:
        payload = build_signals_payload(kept)
        write_signals_json(payload, str(s_path), mirror_path=mirror_path or None)
        written.append(str(s_path))
        if mirror_path:
            written.append(str(mirror_path))
    except Exception as e:
        errors.append(f"write_signals_json: {e}")
        log.exception(_msg("orchestrator.signal_write_failed", error=e))

    try:
        write_pine(kept, str(p_path))
        written.append(str(p_path))
    except Exception as e:
        errors.append(f"write_pine: {e}")
        log.exception(_msg("orchestrator.pine_write_failed", error=e))

    return CycleResult(
        ts=ts_iso,
        symbols=list(watchlist),
        signals=kept,
        written=written,
        errors=errors,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _load_rules(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_fetcher(dry_run: bool) -> BarFetcher:
    if dry_run:
        return FixtureBarFetcher()
    try:
        return MT5BarFetcher()
    except Exception as e:
        log.warning(_msg("orchestrator.mt5_fallback", error=e))
        return FixtureBarFetcher()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="OMNI-ICT orchestrator cycle runner")
    p.add_argument("--dry-run", action="store_true", help="use fixtures instead of MT5")
    p.add_argument("--symbols", nargs="+", help="subset of watchlist to run")
    p.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                   help="run forever, sleep N seconds between cycles (0 = single run)")
    p.add_argument("--rules", default=str(DEFAULT_RULES_PATH))
    p.add_argument("--positions", default="", help="optional JSON file with open positions")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    rules = _load_rules(Path(args.rules))
    fetcher = _build_fetcher(args.dry_run)
    pos_path = Path(args.positions) if args.positions else None

    def _once() -> CycleResult:
        positions = _load_open_positions(pos_path)
        res = run_cycle(rules, fetcher=fetcher, symbols=args.symbols,
                        open_positions=positions)
        print(json.dumps(res.as_dict(), indent=2))
        return res

    if args.loop <= 0:
        res = _once()
        return 0 if not res.errors else 1

    # Loop mode
    while True:
        try:
            _once()
        except KeyboardInterrupt:
            log.info(_msg("orchestrator.stopped"))
            return 0
        except Exception:
            log.exception("cycle failed; continuing")
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
