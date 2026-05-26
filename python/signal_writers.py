"""
signal_writers.py — Phase 3 signal emission for OMNI-ICT.

Takes the output of the Phase-2 engines (dual_tf_selector.TradeSelection,
scaling_engine.ScaleAction, and the smart_trail proposals) and writes them to
disk in the shapes consumed by:

    · the MT5 overlay (`OmniSignalOverlay.mq5`) — via `signals.json`
    · the TradingView Pine overlay — via `pine_codegen.py`
    · the web dashboard — directly via the FastAPI server

The writer is PURE on inputs → strings/dicts. File I/O is isolated in the
`write_signals_json` / `write_pine_script` functions so the core logic stays
testable.

Shapes
------
    signals.json           — consumed by MT5 overlay (file-IPC, 3s poll)
    omni_pine_overlay.pine — human-readable Pine v5 script, refreshed each cycle

Config in rules.json → `signals`:
    {
      "enabled":             true,
      "output_dir":          "<omni-ict>/shared",   # where signals.json lives
      "emit_pine":           true,
      "pine_output_path":    "<omni-ict>/pine/omni_pine_overlay.pine",
      "max_signals_kept":    20
    }

Run `python signal_writers.py` for self-test.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timezone, timedelta
from typing import Optional

from dual_tf_selector import TradeSelection
from scaling_engine import ScaleAction, PositionCtx
from i18n import msg as _msg

log = logging.getLogger(__name__)

SIGNALS_VERSION = "1.0"


# ──────────────────────────────────────────────────────────────────────────────
# Signal envelope
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """
    Unified envelope emitted per symbol/cycle. Shared shape for MT5 + Pine.
    """
    id:            str
    ts:            str          # ISO-8601 UTC
    symbol:        str
    timeframe:    str
    direction:     str          # BULL / BEAR / NEUTRAL
    entry_type:    str          # ob_mitigation / fvg_fill / sweep_choch / none
    entry_price:   Optional[float]
    sl:            Optional[float]
    tp:            Optional[float]
    confidence:    float
    reasons:       list[str] = field(default_factory=list)
    scale_action:  str = "HOLD"
    scale_mult:    float = 1.0
    htf_bias:      Optional[str] = None
    source:        str = "omni-ict"
    expires_at:    str = ""       # ISO UTC; empty = no expiry override
    re_emit_count: int = 0        # incremented when same (symbol, direction, entry_type) re-emitted
    ema20:         float = 0.0     # EMA 20 on entry timeframe
    ema200:        float = 0.0     # EMA 200 on entry timeframe
    ema800:        float = 0.0     # EMA 800 on entry timeframe
    tf_emas:       dict = field(default_factory=dict)  # {TF: {ema20, ema200, ema800}} for ALL TFs
    # Free-form bag for context that downstream consumers (auto_trader,
    # dashboard, telegram) may want — e.g. {"regime": {...}, "pivot": {...}}.
    metadata:      dict = field(default_factory=dict)


def build_signal(symbol: str, timeframe: str,
                 sel: TradeSelection,
                 scale: Optional[ScaleAction] = None,
                 ts: Optional[datetime] = None) -> Signal:
    ts = ts or datetime.now(timezone.utc)
    iso = ts.isoformat()
    # Microsecond-precision ID prevents collisions when multiple symbols trigger same second
    sid = f"{symbol}-{timeframe}-{int(ts.timestamp() * 1e6)}-{sel.direction}"
    expires_iso = (ts + timedelta(hours=6)).isoformat()
    try:
        from ict_diagnostics import build_scorecard
        metadata = {
            "ict_scorecard": build_scorecard(
                direction=sel.direction,
                entry_type=sel.entry_type,
                confidence=sel.confidence,
                reasons=list(sel.reasons),
            )
        }
    except Exception:
        metadata = {}
    return Signal(
        id=sid, ts=iso, symbol=symbol, timeframe=timeframe,
        direction=sel.direction, entry_type=sel.entry_type,
        entry_price=sel.entry_price, sl=sel.sl, tp=sel.tp,
        confidence=round(sel.confidence, 3),
        reasons=list(sel.reasons),
        scale_action=(scale.action if scale else "HOLD"),
        scale_mult=(scale.size_multiplier if scale else 1.0),
        htf_bias=(sel.htf_bias.direction if sel.htf_bias else None),
        expires_at=expires_iso,
        ema20=sel.ema20, ema200=sel.ema200, ema800=sel.ema800,
        tf_emas=sel.tf_emas,
        metadata=metadata,
    )


# ──────────────────────────────────────────────────────────────────────────────
# signals.json (MT5-facing)
# ──────────────────────────────────────────────────────────────────────────────

def build_signals_payload(signals: list[Signal],
                          trail_proposals: Optional[dict] = None) -> dict:
    """
    Build the full payload the MT5 overlay reads. Kept flat and stable.
    """
    return {
        "version":         SIGNALS_VERSION,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "signals":         [asdict(s) for s in signals],
        "trail_proposals": trail_proposals or {},
    }


def write_signals_json(payload: dict, out_path: str, mirror_path: str | None = None) -> None:
    """Atomic write: temp file + rename so MT5 never reads a half-written file."""
    targets = [out_path]
    if mirror_path:
        targets.append(mirror_path)

    for target in targets:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, target)


# ──────────────────────────────────────────────────────────────────────────────
# Pruning
# ──────────────────────────────────────────────────────────────────────────────

def prune_signals(signals: list[Signal], max_kept: int = 20) -> list[Signal]:
    """
    Deduplicate by (symbol, direction, entry_type) keeping the most recent,
    incrementing re_emit_count for repeated signals, then cap at max_kept.
    """
    seen: dict[tuple, Signal] = {}
    for s in sorted(signals, key=lambda x: x.ts):
        key = (s.symbol, s.direction, s.entry_type)
        if key in seen:
            s = replace(s, re_emit_count=seen[key].re_emit_count + 1)
        seen[key] = s
    return sorted(seen.values(), key=lambda s: s.ts, reverse=True)[:max_kept]


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    from smc_engine import _make_fixture
    from dual_tf_selector import select_trade

    sel = select_trade(_make_fixture(), _make_fixture())
    sig = build_signal("EURUSD", "M5", sel)
    assert sig.symbol == "EURUSD"
    assert sig.direction in ("BULL", "BEAR", "NEUTRAL")

    payload = build_signals_payload([sig])
    assert payload["version"] == SIGNALS_VERSION
    assert len(payload["signals"]) == 1

    # Dry-run atomic write to a temp file
    out = "/tmp/omni_signals_test.json"
    write_signals_json(payload, out)
    assert os.path.exists(out)
    restored = json.load(open(out))
    assert restored["version"] == SIGNALS_VERSION
    os.remove(out)

    print(_msg("signals.write_ok", path=out))
    print(f"     id={sig.id} dir={sig.direction} conf={sig.confidence}")


if __name__ == "__main__":
    _self_test()
