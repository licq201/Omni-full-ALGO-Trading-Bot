"""
auto_trader.py — OMNI ICT Auto-Trading Engine
Compounding risk | Multi-TF ICT entries | Full trade management

⚠️  PAPER MODE IS ON BY DEFAULT
    To enable live trading set OMNI_PAPER_MODE=false in your environment,
    or set "paper_mode": false in config.json.
    Always test on a demo account first!

Kill switch: touch the file at KILL_SWITCH_PATH (default: python/HALT) to
halt trading instantly.  Remove the file to resume.

──────────────────────────────────────────────────────────────────────
RISK MODES  (set via OMNI_RISK_MODE env var or --risk CLI arg)
──────────────────────────────────────────────────────────────────────
  LOW        0.5% risk | 1% max | 2% daily limit | 5% max DD
             Very safe, capital preservation focus
  MODERATE   1.0% risk | 2% max | 3% daily limit | 10% max DD  [default]
             Balanced growth and protection
  HIGH       2.0% risk | 4% max | 5% daily limit | 15% max DD
             Aggressive compounding, higher drawdown tolerance

──────────────────────────────────────────────────────────────────────
FREQUENCY MODES  (set via OMNI_FREQ_MODE env var or --freq CLI arg)
──────────────────────────────────────────────────────────────────────
  CONSERVATIVE  A+/A grades only | conf ≥70 | max 2 open | no zones
                Highest quality only, trades very selectively
  NORMAL        A+/A/B+ live, all paper | conf ≥55 | max 3 open  [default]
                Balanced entry frequency
  AGGRESSIVE    All grades | conf ≥45 | max 5 open | zone entries OK
                Maximum opportunity capture, higher noise tolerance

Run: python auto_trader.py
     python auto_trader.py --risk LOW --freq CONSERVATIVE
     python auto_trader.py --risk HIGH --freq AGGRESSIVE
     OMNI_RISK_MODE=HIGH OMNI_FREQ_MODE=AGGRESSIVE python auto_trader.py
"""

import os
import sys
import json
import time
import re
import math
import logging
import logging.handlers
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── Centralised config (paths, thresholds, paper/live toggle) ─────────────────
from config import cfg
from i18n import msg as _msg

# ── Phase 2f learning loop (optional — gracefully degrade if missing) ────────
try:
    from feature_store import FeatureStore as _FeatureStore
    from parameter_optimizer import ParameterOptimizer as _ParamOpt
    from online_learner import OnlineLearner as _OnlineLearner
    _LEARNING_AVAILABLE = True
except ImportError as _e:
    _FeatureStore = None
    _ParamOpt = None
    _OnlineLearner = None
    _LEARNING_AVAILABLE = False

try:
    from pattern_recognition_model import PatternRecognitionModel as _PatternModel
    _PATTERN_MODEL: "_PatternModel | None" = _PatternModel()
    _PATTERN_AVAILABLE = True
except (ImportError, Exception) as _pe:
    _PatternModel = None          # type: ignore
    _PATTERN_MODEL = None
    _PATTERN_AVAILABLE = False

# Lazy-initialized singletons for the learning loop
_FS_SINGLETON = None
_OPT_SINGLETON = None
_LEARNER_SINGLETON = None


def _get_feature_store():
    global _FS_SINGLETON
    if not _LEARNING_AVAILABLE:
        return None
    if _FS_SINGLETON is None:
        try:
            _FS_SINGLETON = _FeatureStore()
        except Exception as e:
            print(_msg("auto.learning_init_failed", name="FeatureStore", error=e))
            return None
    return _FS_SINGLETON


def _get_optimizer():
    global _OPT_SINGLETON
    if not _LEARNING_AVAILABLE:
        return None
    if _OPT_SINGLETON is None:
        fs = _get_feature_store()
        if fs is None:
            return None
        try:
            _OPT_SINGLETON = _ParamOpt(fs, regime="DEFAULT")
        except Exception as e:
            print(_msg("auto.learning_init_failed", name="ParameterOptimizer", error=e))
            return None
    return _OPT_SINGLETON


def _get_online_learner():
    global _LEARNER_SINGLETON
    if not _LEARNING_AVAILABLE:
        return None
    if _LEARNER_SINGLETON is None:
        fs = _get_feature_store()
        opt = _get_optimizer()
        if fs is None:
            return None
        try:
            _LEARNER_SINGLETON = _OnlineLearner(fs, opt)
        except Exception as e:
            print(_msg("auto.learning_init_failed", name="OnlineLearner", error=e))
            return None
    return _LEARNER_SINGLETON


def _build_feature_payload(setup) -> dict:
    """Extract structured feature dicts from an ICTSetup for feature_store.record_signal.

    Returns four dicts (ict / pivot / structure / candle) keyed on stable names so the
    online_learner and pattern_recognition_model can compute lift across runs.
    """
    conf = getattr(setup, "confluence", None)
    ict_features = {}
    if conf is not None:
        ict_features = {
            "htf_bias_aligned":  bool(getattr(conf, "htf_bias_aligned",  False)),
            "amd_phase_aligned": bool(getattr(conf, "amd_phase_aligned", False)),
            "killzone_active":   bool(getattr(conf, "killzone_active",   False)),
            "liquidity_swept":   bool(getattr(conf, "liquidity_swept",   False)),
            "fvg_in_ote":        bool(getattr(conf, "fvg_in_ote",        False)),
            "mss_confirmed":     bool(getattr(conf, "mss_confirmed",     False)),
            "smt_divergence":    bool(getattr(conf, "smt_divergence",    False)),
            "layers_met":        int(getattr(conf, "layers_met", 0) or 0),
        }
    ict_features["liq_swept"] = bool(getattr(setup, "liq_swept", False))
    ict_features["entry_type"] = str(getattr(setup, "entry_type", ""))
    ict_features["grade"]      = str(getattr(setup, "grade", ""))
    ict_features["tf_bias"]    = str(getattr(setup, "tf_bias", ""))

    structure_features = {
        "rr_ratio":     float(getattr(setup, "rr_ratio", 0) or 0),
        "invalidation": float(getattr(setup, "invalidation", 0) or 0),
    }
    pivot_features: dict = {}
    candle_features: dict = {}
    return {
        "ict_features":       ict_features,
        "pivot_features":     pivot_features,
        "structure_features": structure_features,
        "candle_features":    candle_features,
    }


def _record_signal_open(ticket_key: str, setup, lot_size: float, sl: float,
                        adj_confidence: int, regime: str = "") -> None:
    """Persist an opened-trade row to feature_store. Silent no-op if learning loop unavailable."""
    if not _LEARNING_AVAILABLE:
        return
    fs = _get_feature_store()
    if fs is None:
        return
    try:
        payload = _build_feature_payload(setup)
        fs.record_signal(
            signal_id=str(ticket_key),
            symbol=str(getattr(setup, "symbol", "")),
            timeframe="M15",          # primary entry timeframe in ict_precision
            direction=str(getattr(setup, "direction", "")),
            entry_price=float(getattr(setup, "entry_price", 0) or 0),
            sl_price=float(sl),
            tp_price=float(getattr(setup, "tp3_price", 0)
                           or getattr(setup, "tp2_price", 0)
                           or getattr(setup, "tp1_price", 0) or 0),
            ict_features=payload["ict_features"],
            pivot_features=payload["pivot_features"],
            structure_features=payload["structure_features"],
            candle_features=payload["candle_features"],
            regime=str(regime or ""),
            session=str(getattr(setup, "session", "")),
            decision="TRADED",
            confidence=int(adj_confidence),
            rr_ratio=float(getattr(setup, "rr_ratio", 0) or 0),
        )
    except Exception as e:
        log.debug(f"feature_store record_signal hook error: {e}")


# ── Smart trailing stop (Phase 1) — gated behind rules.json smart_trail.enabled
try:
    from smart_trail_adapter import maybe_smart_trail, is_enabled as _smart_trail_enabled
    _SMART_TRAIL_AVAILABLE = True
except Exception:  # import-safe — absence of the module never breaks the trader
    _SMART_TRAIL_AVAILABLE = False
    def maybe_smart_trail(*_a, **_kw):  # type: ignore
        return None
    def _smart_trail_enabled(_rules):    # type: ignore
        return False

PAPER_MODE       = cfg.PAPER_MODE
OUR_MAGIC        = cfg.MAGIC
SCAN_INTERVAL    = cfg.SCAN_INTERVAL
SESSION_FILTER   = None   # None = all sessions; session-specific thresholds in execute_setup
TRADE_SYMBOLS    = cfg.TRADE_SYMBOLS
JSON_PATH        = cfg.JSON_PATH
CMD_PATH         = cfg.CMD_PATH
RESULT_PATH      = cfg.RESULT_PATH
STATE_PATH       = cfg.STATE_PATH
LOG_PATH         = cfg.LOG_PATH
KILL_SWITCH_PATH = cfg.KILL_SWITCH_PATH
TRADING_DISABLED_PATH = Path(cfg.KILL_SWITCH_PATH).parent / "TRADING_DISABLED"  # Telegram bot creates this to disable trading
SCALP_ENABLED_PATH    = Path(cfg.KILL_SWITCH_PATH).parent / "SCALP_ENABLED"     # Allow accumulation scalps
PYRAMID_ENABLED_PATH  = Path(cfg.KILL_SWITCH_PATH).parent / "PYRAMID_ENABLED"   # Allow winning-trade pyramid scaling
HF_SCALP_PATH         = Path(cfg.KILL_SWITCH_PATH).parent / "HF_SCALP"          # High-frequency scalp mode (lower thresholds)

# Scalp engine — optional import (graceful degrade)
try:
    from scalp_engine import (
        evaluate_accumulation_scalp, evaluate_pyramid_scale,
        detect_opposing_liquidity_swept, ScalpDecision,
        DEFAULT_SCALP_PARAMS,
    )
    _SCALP_AVAILABLE = True
except ImportError:
    _SCALP_AVAILABLE = False
    DEFAULT_SCALP_PARAMS = {}


def _scalp_params() -> dict:
    """Build runtime scalp params, applying HF overrides if HF mode is active."""
    p = dict(DEFAULT_SCALP_PARAMS) if _SCALP_AVAILABLE else {}
    if HF_SCALP_PATH.exists():
        # High-frequency mode: lower bars, more scalps, accept lower RR
        p["scalp_min_conf"]        = 65
        p["scalp_max_per_session"] = 12
        p["scalp_min_rr"]          = 1.2
        p["scalp_tp_pips"]         = 6.0
        p["scalp_sl_pips"]         = 4.0
    return p


# ── Telegram notifications ────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    """Send a Telegram message. Silent no-op if token/chat_id not configured."""
    token   = cfg.TELEGRAM_BOT_TOKEN
    chat_id = cfg.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    try:
        import requests as _req
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as _e:
        log.debug(f"Telegram send error: {_e}")


def _tg_portfolio(state: "TraderState", data: dict) -> str:
    """Build a compact portfolio status string for Telegram."""
    acc     = get_account(data)
    bal     = acc.get("balance", 0)
    eq      = acc.get("equity", 0)
    fpl     = acc.get("profit", 0)
    open_n  = len(get_open_positions(data))
    wr      = (state.winning_trades / state.total_trades * 100) if state.total_trades else 0
    streak  = f"{state.win_streak}W" if state.win_streak > 0 else f"{state.loss_streak}L"
    day_pct = ((eq - state.day_start_equity) / state.day_start_equity * 100) if state.day_start_equity else 0
    mode    = "PAPER" if PAPER_MODE else "LIVE"
    return (
        f"Balance: <b>${bal:,.2f}</b> | Equity: <b>${eq:,.2f}</b>\n"
        f"Float P&amp;L: <b>${fpl:+,.2f}</b> | Day: <b>{day_pct:+.2f}%</b>\n"
        f"Open: <b>{open_n}</b> | WR: <b>{wr:.0f}%</b> ({state.winning_trades}W/{state.losing_trades}L) | Streak: <b>{streak}</b>\n"
        f"Peak DD: <b>{state.peak_drawdown:.1f}%</b> | Risk: <b>{state.current_risk_pct:.2f}%</b> | Mode: <b>{mode}</b>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TRADING PROFILES — Risk Mode × Frequency Mode
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskProfile:
    name:              str
    base_risk_pct:     float   # Starting risk per trade
    max_risk_pct:      float   # Risk ceiling (win streak scaling)
    min_risk_pct:      float   # Risk floor (loss streak scaling)
    daily_loss_limit:   float   # % of equity lost today before halt
    daily_profit_target: float  # % of equity gained today before halting (lock gains)
    max_dd_from_peak:   float   # % drawdown from peak before halt
    min_sl_pips:       int     # Minimum SL distance in pips
    win_streak_step:   float   # How much risk increases per win streak tier
    loss_streak_step:  float   # How much risk decreases per loss streak tier
    description:       str

@dataclass
class FrequencyProfile:
    name:              str
    min_confidence:    int     # Minimum setup confidence (0-100)
    min_confidence_asia: int   # Asia session threshold (noisier session)
    min_rr:            float   # Minimum risk:reward ratio
    max_open_trades:   int     # Maximum simultaneous positions
    allowed_grades:    list    # Which ICT setup grades to trade
    allow_zone_entries: bool   # Allow HTF zone entries (no LTF trigger yet)
    cooldown_scans:    int     # How many scans before re-trading same symbol
    description:       str


RISK_PROFILES = {
    "LOW": RiskProfile(
        name             = "LOW",
        base_risk_pct    = 0.5,
        max_risk_pct     = 1.0,
        min_risk_pct     = 0.25,
        daily_loss_limit    = 2.0,
        daily_profit_target = 1.5,
        max_dd_from_peak    = 5.0,
        min_sl_pips      = 15,
        win_streak_step  = 0.10,
        loss_streak_step = 0.10,
        description      = "Capital preservation — very conservative sizing",
    ),
    "MODERATE": RiskProfile(
        name             = "MODERATE",
        base_risk_pct    = 1.0,
        max_risk_pct     = 2.0,
        min_risk_pct     = 0.5,
        daily_loss_limit    = 3.0,
        daily_profit_target = 2.0,
        max_dd_from_peak    = 10.0,
        min_sl_pips      = 10,
        win_streak_step  = 0.25,
        loss_streak_step = 0.25,
        description      = "Balanced growth and protection",
    ),
    "HIGH": RiskProfile(
        name             = "HIGH",
        base_risk_pct    = 2.0,
        max_risk_pct     = 4.0,
        min_risk_pct     = 1.0,
        daily_loss_limit    = 5.0,
        daily_profit_target = 3.5,
        max_dd_from_peak    = 15.0,
        min_sl_pips      = 8,
        win_streak_step  = 0.50,
        loss_streak_step = 0.50,
        description      = "Aggressive compounding — higher drawdown tolerance",
    ),
}

FREQUENCY_PROFILES = {
    "CONSERVATIVE": FrequencyProfile(
        name               = "CONSERVATIVE",
        min_confidence     = 70,
        min_confidence_asia= 80,
        min_rr             = 2.5,
        max_open_trades    = 2,
        allowed_grades     = ["A+", "A"],
        allow_zone_entries = False,
        cooldown_scans     = 12,   # ~2 min cooldown per symbol
        description        = "Highest quality only — very selective entry",
    ),
    "NORMAL": FrequencyProfile(
        name               = "NORMAL",
        min_confidence     = 65,   # backtest: <65 setups all had negative expectancy
        min_confidence_asia= 72,
        min_rr             = 2.0,
        max_open_trades    = 3,
        allowed_grades     = ["A+", "A", "B+"],   # live; paper allows all
        allow_zone_entries = False,
        cooldown_scans     = 6,
        description        = "Balanced frequency and quality",
    ),
    "AGGRESSIVE": FrequencyProfile(
        name               = "AGGRESSIVE",
        min_confidence     = 55,   # raised from 45; backtest showed <55 consistently loses
        min_confidence_asia= 65,
        min_rr             = 1.5,
        max_open_trades    = 5,
        allowed_grades     = ["A+", "A", "B+", "B", "C"],
        allow_zone_entries = True,
        cooldown_scans     = 3,
        description        = "Maximum opportunity capture — higher noise tolerance",
    ),
}


def _parse_modes() -> tuple:
    """Read risk/freq mode from CLI args first, then env vars, then default."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--risk", choices=["LOW","MODERATE","HIGH"],    default=None)
    parser.add_argument("--freq", choices=["CONSERVATIVE","NORMAL","AGGRESSIVE"], default=None)
    args, _ = parser.parse_known_args()

    risk_mode = (args.risk
                 or os.getenv("OMNI_RISK_MODE", "").upper()
                 or "MODERATE")
    freq_mode = (args.freq
                 or os.getenv("OMNI_FREQ_MODE", "").upper()
                 or "NORMAL")

    if risk_mode not in RISK_PROFILES:
        print(_msg("auto.unknown_risk_mode", mode=risk_mode))
        risk_mode = "MODERATE"
    if freq_mode not in FREQUENCY_PROFILES:
        print(_msg("auto.unknown_freq_mode", mode=freq_mode))
        freq_mode = "NORMAL"

    return risk_mode, freq_mode


# ── Active profiles (set at startup, used throughout) ─────────────────────────
_RISK_MODE, _FREQ_MODE = _parse_modes()
RISK  = RISK_PROFILES[_RISK_MODE]
FREQ  = FREQUENCY_PROFILES[_FREQ_MODE]

# Expose as module-level names used by legacy code
BASE_RISK_PCT    = RISK.base_risk_pct
MAX_RISK_PCT     = RISK.max_risk_pct
MIN_RISK_PCT     = RISK.min_risk_pct
DAILY_LOSS_LIMIT    = RISK.daily_loss_limit
DAILY_PROFIT_TARGET = RISK.daily_profit_target
MAX_DD_FROM_PEAK    = RISK.max_dd_from_peak
MIN_SL_PIPS      = RISK.min_sl_pips
MIN_CONFIDENCE      = FREQ.min_confidence
MIN_CONFIDENCE_ASIA = FREQ.min_confidence_asia
MIN_RR              = FREQ.min_rr
MAX_OPEN_TRADES     = FREQ.max_open_trades

# ── Logging (with rotation — max 10 MB, keep 5 files) ────────────────────────
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger("OMNI")

def _load_regime() -> str:
    """Load current market regime from ai_engine's state file. Returns 'TRENDING' as default."""
    try:
        ai_path = os.path.join(os.path.dirname(STATE_PATH), "ai_state.json")
        if os.path.exists(ai_path):
            with open(ai_path) as f:
                return json.load(f).get("regime", "TRENDING")
    except Exception:
        pass
    return "TRENDING"


# ── Learned-parameters live reload ────────────────────────────────────────────
# parameter_optimizer.optimize_now() writes learned_parameters.json on every
# accepted promotion. The live trader picks them up via mtime-keyed reload —
# no restart required. Hardcoded FREQUENCY_PROFILE values remain the floor.
_LEARNED_PARAMS_PATH = Path(__file__).parent / "learned_parameters.json"
_LEARNED_PARAMS_CACHE: dict = {"data": {}, "mtime": 0.0}
_LEARNED_PARAMS_LAST_LOG_TS = 0.0


def _load_learned_params(regime: str = "DEFAULT") -> dict:
    """Return the most recent learned-param set for `regime`, or {} if none.

    Cached on file mtime so the scanner loop can call this every iteration cheaply.
    """
    global _LEARNED_PARAMS_LAST_LOG_TS
    try:
        if not _LEARNED_PARAMS_PATH.exists():
            return {}
        m = _LEARNED_PARAMS_PATH.stat().st_mtime
        if m != _LEARNED_PARAMS_CACHE["mtime"]:
            with open(_LEARNED_PARAMS_PATH) as f:
                data = json.load(f)
            _LEARNED_PARAMS_CACHE["data"] = data
            _LEARNED_PARAMS_CACHE["mtime"] = m
            # Surface the reload once per minute at most so we don't spam the log
            now = time.time()
            if now - _LEARNED_PARAMS_LAST_LOG_TS > 60:
                _LEARNED_PARAMS_LAST_LOG_TS = now
                try:
                    log.info(f"[learning] learned_parameters.json reloaded (regime={regime})")
                except Exception:
                    pass
        regimes = (_LEARNED_PARAMS_CACHE["data"] or {}).get("regimes", {})
        return regimes.get(regime) or regimes.get("DEFAULT") or {}
    except Exception:
        return {}


def _live_min_conf(session: str = "") -> int:
    """Effective minimum confidence threshold — learned-param override or FREQ default."""
    params = _load_learned_params(_load_regime())
    sess = (session or "").upper()
    if sess and f"session_min_conf_{sess}" in params:
        v = int(params.get(f"session_min_conf_{sess}", 0))
        if v > 0:
            return max(v, FREQ.min_confidence)  # never go below the FREQ floor
    if sess == "ASIA":
        v = int(params.get("session_min_conf_ASIA", 0))
        if v > 0:
            return max(v, FREQ.min_confidence_asia)
        return FREQ.min_confidence_asia
    v = int(params.get("min_confidence", 0))
    if v > 0:
        return max(v, FREQ.min_confidence)
    return FREQ.min_confidence


def _live_min_rr() -> float:
    """Effective minimum RR — learned-param override or FREQ default."""
    params = _load_learned_params(_load_regime())
    v = float(params.get("min_rr", 0) or 0)
    if v > 0:
        return max(v, FREQ.min_rr)  # never go below the FREQ floor
    return FREQ.min_rr


# ── State ─────────────────────────────────────────────────────────────────────
@dataclass
class TraderState:
    # Compounding
    current_risk_pct:    float = BASE_RISK_PCT
    win_streak:          int   = 0
    loss_streak:         int   = 0
    total_trades:        int   = 0
    winning_trades:      int   = 0
    losing_trades:       int   = 0
    total_profit:        float = 0.0
    peak_equity:         float = 0.0
    peak_drawdown:       float = 0.0     # Max % drawdown seen from peak

    # Daily tracking
    day_start_equity:    float = 0.0
    day_initialized:     bool  = False   # Prevents treating equity==0 as "unset"
    last_reset_day:      str   = ""      # "YYYY-MM-DD" UTC — fires reset exactly once per day
    day_profit:          float = 0.0
    day_trades:          int   = 0
    trading_halted:      bool  = False
    halt_reason:         str   = ""

    # Active setups (pending orders we placed)
    pending_orders:      dict  = field(default_factory=dict)
    active_trades:       dict  = field(default_factory=dict)  # ticket -> trade info + TP levels
    recently_traded:     dict  = field(default_factory=dict)   # {symbol: expiry_ts} — time-based cooldown

    # Statistics
    start_time:          str   = ""
    last_scan:           str   = ""

    # Smart trail telemetry — last proposal per ticket, written for dashboard consumption
    last_trail_proposals: dict = field(default_factory=dict)
    # Smart trail scan context — populated by ict_precision; consumed by smart_trail_adapter
    last_scan_context:    dict = field(default_factory=dict)

    # Re-entry queue: symbol → re-entry spec (queued when trailing SL hits in profit)
    pending_reentries:    dict = field(default_factory=dict)

    # Per-symbol consecutive loss counter — pauses a symbol after 2 back-to-back losses
    sym_loss_streak:      dict = field(default_factory=dict)

    # Per-session consecutive loss counter — penalises a session after 3 back-to-back losses
    session_loss_streak:  dict = field(default_factory=dict)

    # Last N closed trades for telegram notifications (full details w/ PnL)
    recent_closes:        list = field(default_factory=list)


def load_state() -> TraderState:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                d = json.load(f)
            s = TraderState()
            for k, v in d.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            # Migration: clear obsolete halt reasons that no longer have an active code path
            obsolete = ("profit target",)
            if s.trading_halted and any(tag in (s.halt_reason or "").lower() for tag in obsolete):
                log.info(f"Clearing obsolete halt_reason on load: {s.halt_reason!r}")
                s.trading_halted = False
                s.halt_reason = ""
            # Migration: recently_traded was a list; convert to dict with 30 min expiry
            if isinstance(s.recently_traded, list):
                _now = time.time()
                s.recently_traded = {sym: _now + 1800 for sym in s.recently_traded}
            return s
        except Exception:
            pass
    return TraderState()


def save_state(state: TraderState, setups=None):
    try:
        d = asdict(state)
        # Append active profile info so the dashboard can read it
        d["risk_mode"]   = _RISK_MODE
        d["freq_mode"]   = _FREQ_MODE
        d["base_risk"]   = BASE_RISK_PCT
        d["max_risk"]    = MAX_RISK_PCT
        d["min_risk"]    = MIN_RISK_PCT
        d["min_conf"]    = MIN_CONFIDENCE
        d["min_rr"]      = MIN_RR
        d["max_open"]    = MAX_OPEN_TRADES
        d["daily_limit"] = DAILY_LOSS_LIMIT
        d["max_dd"]      = MAX_DD_FROM_PEAK
        if setups is not None:
            # Fresh scan results — overwrite with latest
            d["top_setups"] = [
                {
                    "symbol":      s.symbol,
                    "direction":   s.direction,
                    "entry_type":  s.entry_type,
                    "entry_price": round(s.entry_price, 5),
                    "sl_price":    round(s.sl_price, 5),
                    "tp1_price":   round(s.tp1_price, 5),
                    "tp2_price":   round(s.tp2_price, 5),
                    "tp3_price":   round(s.tp3_price, 5),
                    "confidence":  s.confidence,
                    "rr_ratio":    round(s.rr_ratio, 2),
                    "grade":       s.grade,
                    "session":     s.session,
                    "amd_phase":   s.amd_phase,
                    "tf_bias":     s.tf_bias,
                    "reasons":     s.reasons[:8],
                    "invalidation": round(s.invalidation, 5),
                    "is_blocked":  False,
                    "block_reason": "",
                }
                for s in setups[:15]
            ]
        else:
            # Scan was skipped this tick — preserve the last known setups from disk
            try:
                with open(STATE_PATH) as _f:
                    _prev = json.load(_f)
                d["top_setups"] = _prev.get("top_setups", [])
            except Exception:
                d["top_setups"] = []
        with open(STATE_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        log.error(f"State save error: {e}")


# ── Rules loading (cached, mtime-aware) ──────────────────────────────────────
_RULES_CACHE: dict = {"mtime": 0.0, "data": {}}
_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")

def load_rules() -> dict:
    """Load rules.json with mtime-based cache. Safe on missing/malformed."""
    try:
        mtime = os.path.getmtime(_RULES_PATH)
        if mtime > _RULES_CACHE["mtime"]:
            with open(_RULES_PATH, "r", encoding="utf-8") as f:
                _RULES_CACHE["data"] = json.load(f)
            _RULES_CACHE["mtime"] = mtime
    except Exception as e:
        log.debug(f"rules.json load failed (using cached/empty): {e}")
    return _RULES_CACHE.get("data", {}) or {}


# ── orjson fast loader (10× faster than stdlib on 55 MB file) ─────────────────
try:
    import orjson as _orjson_at
except ImportError:
    _orjson_at = None  # type: ignore

_MT5_AT_CACHE: dict = {"mtime": -1.0, "data": {}}

# ── Data Loading ──────────────────────────────────────────────────────────────
def _parse_mt5_json(raw: bytes) -> dict:
    """Parse MT5 JSON, recovering from Wine's 4MB file-write truncation."""
    text = raw.decode("utf-8", errors="replace").rstrip('\x00')
    text = re.sub(r',\s*([\]}])', r'\1', text)
    # Fast path
    try:
        if _orjson_at is not None:
            return _orjson_at.loads(text)
        return json.loads(text)
    except Exception:
        pass
    # Recovery: truncate at last complete object boundary and close open structures
    last_safe = text.rfind('},')
    if last_safe == -1:
        last_safe = text.rfind('}')
    if last_safe != -1:
        trunk = text[:last_safe + 1]
        obj_d = arr_d = 0
        in_s = esc = False
        for ch in trunk:
            if esc:             esc = False; continue
            if ch == '\\' and in_s: esc = True; continue
            if ch == '"':       in_s = not in_s; continue
            if in_s:            continue
            if ch == '{':       obj_d += 1
            elif ch == '}':     obj_d -= 1
            elif ch == '[':     arr_d += 1
            elif ch == ']':     arr_d -= 1
        closer = ']' * max(arr_d, 0) + '}' * max(obj_d, 0)
        try:
            return json.loads(trunk + closer)
        except Exception:
            pass
    return {}


def load_mt5_data() -> dict:
    """Load MT5 data with mtime-based caching so we never re-parse unchanged file."""
    try:
        mtime = os.path.getmtime(JSON_PATH)
        if mtime == _MT5_AT_CACHE["mtime"]:
            return _MT5_AT_CACHE["data"]
        with open(JSON_PATH, "rb") as f:
            raw = f.read()
        data = _parse_mt5_json(raw)
        if data:
            _MT5_AT_CACHE["mtime"] = mtime
            _MT5_AT_CACHE["data"]  = data
            return data
        raise ValueError("empty parse result")
    except Exception as e:
        log.warning(f"Data load error: {e}")
        return _MT5_AT_CACHE.get("data", {})


def get_account(data: dict) -> dict:
    return data.get("account", {})


def get_open_positions(data: dict) -> list:
    return data.get("positions", [])


# ── Position Sizing (Compounding) ─────────────────────────────────────────────
def calculate_lot_size(equity: float, risk_pct: float, entry: float,
                       sl: float, symbol: str, sym_info: dict) -> float:
    """
    Compound position sizing:
    lot_size = (equity × risk%) / (sl_distance × tick_value / tick_size)
    """
    if equity <= 0 or entry <= 0 or sl <= 0:
        return 0.0

    risk_amount  = equity * (risk_pct / 100)
    sl_distance  = abs(entry - sl)
    tick_size    = sym_info.get("tick_size", 0.01)
    tick_value   = sym_info.get("tick_value", 1.0)
    min_lot      = sym_info.get("min_lot", 0.01)
    max_lot      = sym_info.get("max_lot", 100.0)
    lot_step     = sym_info.get("lot_step", 0.01)

    if tick_size == 0 or tick_value == 0:
        return min_lot

    # Risk per lot = (sl_distance / tick_size) × tick_value
    risk_per_lot = (sl_distance / tick_size) * tick_value
    if risk_per_lot <= 0:
        return min_lot

    lot_size = risk_amount / risk_per_lot

    # Round down to lot step, then clamp to broker max_lot
    lot_size = math.floor(lot_size / lot_step) * lot_step
    lot_size = max(min_lot, min(lot_size, max_lot))

    # Hard per-trade ceiling that scales with equity — prevents runaway sizing
    # while allowing proper compounding as the account grows.
    # Tiers (standard lots):  <$500 → 0.05  |  <$2k → 0.20  |  <$5k → 0.50  |  <$10k → 1.00  |  10k+ → 2% of equity in lots
    if equity < 500:
        lot_ceiling = 0.05
    elif equity < 2_000:
        lot_ceiling = 0.20
    elif equity < 5_000:
        lot_ceiling = 0.50
    elif equity < 10_000:
        lot_ceiling = 1.00
    else:
        lot_ceiling = math.floor((equity * 0.02 / 100) / lot_step) * lot_step  # 2% of equity in lots, capped

    lot_size = min(lot_size, lot_ceiling)
    lot_size = max(min_lot, math.floor(lot_size / lot_step) * lot_step)

    return round(lot_size, 2)


# ── Risk Adjuster (Compounding logic) ─────────────────────────────────────────
def adjust_risk(state: TraderState) -> float:
    """
    Adaptive risk scaling — steps scale with the active risk profile:
      Win streak 3+  → compound up by profile.win_streak_step per tier
      Win streak 5+  → another tier up (capped at profile.max_risk_pct)
      Loss streak 2+ → step down by profile.loss_streak_step
      Loss streak 3+ → step down again (floored at profile.min_risk_pct)
    Risk resets toward BASE on new day (done in reset_daily_if_new_day).
    """
    risk  = state.current_risk_pct
    step  = RISK.win_streak_step
    lstep = RISK.loss_streak_step

    if state.win_streak >= 5:
        new_risk = min(BASE_RISK_PCT + step * 2, MAX_RISK_PCT)
        if new_risk > risk:
            risk = new_risk
            log.info(f"Win streak {state.win_streak} — scaling risk UP to {risk:.2f}% "
                     f"[{_RISK_MODE} mode, +{step*2:.2f}%]")
    elif state.win_streak >= 3:
        new_risk = min(BASE_RISK_PCT + step, MAX_RISK_PCT)
        if new_risk > risk:
            risk = new_risk
            log.info(f"Win streak {state.win_streak} — scaling risk UP to {risk:.2f}% "
                     f"[{_RISK_MODE} mode, +{step:.2f}%]")
    elif state.loss_streak >= 3:
        risk = max(risk - lstep * 2, MIN_RISK_PCT)
        log.info(f"Loss streak {state.loss_streak} — risk REDUCED to {risk:.2f}% "
                 f"[{_RISK_MODE} mode, -{lstep*2:.2f}%]")
    elif state.loss_streak >= 2:
        risk = max(risk - lstep, MIN_RISK_PCT)
        log.info(f"Loss streak {state.loss_streak} — risk reduced to {risk:.2f}% "
                 f"[{_RISK_MODE} mode, -{lstep:.2f}%]")

    state.current_risk_pct = risk
    return risk


# ── Drawdown Recovery Mode ────────────────────────────────────────────────────
def check_recovery_mode(state: "TraderState", equity: float, rules: dict) -> None:
    """
    Automatically lift a drawdown halt once equity recovers or win streak proves stability.
    Gated by rules.recovery_protocol.enabled (default: True).
    """
    rp = rules.get("recovery_protocol", {})
    if not rp.get("enabled", True):
        return
    if not state.trading_halted or "drawdown" not in state.halt_reason.lower():
        return
    min_days = int(rp.get("min_winning_days_to_resume", 3))
    eq_pct   = float(rp.get("equity_recovery_pct", 0.95))
    # Resume after N consecutive winning trades (proxy for winning days)
    if state.win_streak >= min_days:
        state.trading_halted = False
        state.halt_reason = ""
        log.info("Recovery mode: win_streak=%d >= %d — resuming trading with reduced limits",
                 state.win_streak, min_days)
        return
    # Resume if equity recovered to configured % of peak
    if state.peak_equity > 0 and equity >= state.peak_equity * eq_pct:
        state.trading_halted = False
        state.halt_reason = ""
        log.info("Recovery mode: equity %.2f recovered to %.1f%% of peak %.2f — resuming",
                 equity, equity / state.peak_equity * 100, state.peak_equity)


# ── Win-streak compounding (leverage_compounding rules block) ─────────────────
def _compounded_risk(state: "TraderState", rules: dict, base_risk: float) -> float:
    """
    Apply win/loss streak multipliers from rules.leverage_compounding.
    Gated by enabled flag AND cfg.PAPER_MODE — never runs in paper mode.
    """
    lc = rules.get("leverage_compounding", {})
    if not lc.get("enabled", False) or cfg.PAPER_MODE:
        return base_risk
    ws = state.win_streak
    ls = state.loss_streak
    mult = 1.0
    if ws >= 7:   mult = float(lc.get("win_streak_7_mult", 1.75))
    elif ws >= 5: mult = float(lc.get("win_streak_5_mult", 1.50))
    elif ws >= 3: mult = float(lc.get("win_streak_3_mult", 1.20))
    elif ls >= 3: mult = float(lc.get("loss_streak_3_mult", 0.60))
    elif ls >= 2: mult = float(lc.get("loss_streak_2_mult", 0.80))
    max_r = float(lc.get("max_compounded_risk_pct", 3.0))
    return min(base_risk * mult, max_r)


# ── Command Sender ────────────────────────────────────────────────────────────
def send_command(command: str) -> str:
    """Write command to file for MT5 EA to execute."""
    if PAPER_MODE:
        log.info(f"[PAPER] Would execute: {command}")
        return "PAPER|simulated"

    try:
        # Purge any stale result left over from a previous timed-out command
        try:
            os.remove(RESULT_PATH)
        except FileNotFoundError:
            pass
        with open(CMD_PATH, "w") as f:
            f.write(command)
        # Poll for result — use try/except to avoid TOCTOU race
        for _ in range(100):  # 10 second timeout (MetaQuotes demo can be slow)
            time.sleep(0.1)
            try:
                with open(RESULT_PATH) as f:
                    result = f.read().strip()
                os.remove(RESULT_PATH)
                log.info(f"EA Result: {result}")
                return result
            except FileNotFoundError:
                pass  # File not ready yet, keep polling
        return "TIMEOUT|no response from EA"
    except Exception as e:
        return f"ERROR|{e}"


def place_order(symbol: str, direction: str, order_type: str,
                price: float, sl: float, tp: float,
                volume: float, comment: str) -> str:
    """Place a pending or market order."""
    cmd = f"OPEN|{symbol}|{order_type}|{price:.5f}|{sl:.5f}|{tp:.5f}|{volume:.2f}|{comment}"
    return send_command(cmd)


def _try_pyramid_scale(
    state: "TraderState",
    data: dict,
    parent_ticket_str: str,
    parent_trade: dict,
    profit_in_r: float,
    current_price: float,
    pos_type: str,
) -> bool:
    """
    Evaluate and (if accepted) place a pyramid scale-in on a winning trade.

    Conditions:
    - PYRAMID_ENABLED file exists
    - scalp_engine module loaded
    - Parent in profit ≥ pyramid_min_profit_r
    - Pivot bounce detected in same direction
    - Pyramid count below cap
    - Opposing liquidity not yet swept

    Returns: True if a pyramid order was placed.
    """
    if not _SCALP_AVAILABLE or not PYRAMID_ENABLED_PATH.exists():
        return False
    try:
        symbol = parent_trade.get("symbol", "")
        direction = parent_trade.get("direction", "")
        if not symbol or direction not in ("BUY", "SELL"):
            return False

        pyramid_count = int(parent_trade.get("pyramid_count", 0) or 0)
        params = _scalp_params()
        if pyramid_count >= int(params.get("pyramid_max_adds", 3)):
            return False

        # Build pivots from charts (M5 + M15 highs/lows are good "scalp pivots")
        charts = data.get("charts", {})
        sym_chart = charts.get(symbol, {})
        m5_bars = sym_chart.get("M5", [])
        m15_bars = sym_chart.get("M15", [])
        pivots: list = []
        # Use last 20 bars' wick extremes as candidate "pivots" for bounce detection
        for tf, bars in (("M5", m5_bars), ("M15", m15_bars)):
            for b in (bars[-20:] if isinstance(bars, list) else []):
                hi = float(b.get("high", 0) or 0)
                lo = float(b.get("low", 0) or 0)
                if hi:
                    pivots.append({"level": hi, "tf": tf})
                if lo:
                    pivots.append({"level": lo, "tf": tf})

        # Sweep detection: convert M5 dicts into objects with .high/.low for the helper
        class _BarObj:
            __slots__ = ("high", "low", "close")
            def __init__(self, d):
                self.high = float(d.get("high", 0) or 0)
                self.low = float(d.get("low", 0) or 0)
                self.close = float(d.get("close", 0) or 0)
        m5_objs = [_BarObj(b) for b in (m5_bars[-50:] if isinstance(m5_bars, list) else [])]
        sweep_detected = detect_opposing_liquidity_swept(
            {"direction": direction, "entry": float(parent_trade.get("entry", 0))},
            m5_objs,
        )

        decision = evaluate_pyramid_scale(
            parent_trade={
                "ticket": parent_ticket_str,
                "symbol": symbol,
                "direction": direction,
                "entry": float(parent_trade.get("entry", 0)),
                "current_sl": float(parent_trade.get("current_sl", 0)),
                "current_profit_r": profit_in_r,
            },
            current_price=current_price,
            pivots=pivots,
            sweep_detected=sweep_detected,
            params=params,
            current_pyramid_count=pyramid_count,
            current_phase=data.get("amd_phase", "") or "",
            parent_initial_lot=float(parent_trade.get("initial_volume", 0)
                                     or parent_trade.get("volume", 0)),
        )
        if not decision.accept:
            log.debug(f"{symbol} pyramid skip: {decision.reason}")
            return False

        # Place the pyramid: use parent's SL, parent's TP3 as target,
        # decayed lot, market order
        parent_lot = float(parent_trade.get("initial_volume", 0)
                          or parent_trade.get("volume", 0) or 0.10)
        new_lot = max(0.01, round(parent_lot * decision.lot_multiplier, 2))
        sl = float(parent_trade.get("current_sl", 0))
        tp = float(parent_trade.get("tp3", 0)
                  or parent_trade.get("tp2", 0)
                  or parent_trade.get("tp1", 0)
                  or 0)
        order_type = "BUY" if direction == "BUY" else "SELL"
        comment = f"PYRAMID_{parent_ticket_str}_{decision.pyramid_count}"
        result = place_order(symbol, direction, order_type, current_price, sl, tp, new_lot, comment)
        if result.startswith("OK") or result.startswith("PAPER"):
            state.active_trades[parent_ticket_str]["pyramid_count"] = decision.pyramid_count
            log.info(f"PYRAMID #{decision.pyramid_count} placed on parent {parent_ticket_str}: "
                     f"{symbol} {direction} {new_lot} lots @ {current_price:.5g} | {decision.reason}")
            send_telegram(
                f"⬆️ <b>PYRAMID SCALE #{decision.pyramid_count}</b>\n"
                f"<b>{symbol}</b> {direction} +{new_lot} lots\n"
                f"Parent {parent_ticket_str} @ {profit_in_r:+.2f}R | "
                f"Bounce: <b>{decision.metadata.get('bounce_pivot', 0):.5f}</b> "
                f"({decision.metadata.get('bounce_tf','?')})\n"
                f"Total adds: <b>{decision.pyramid_count}/{params.get('pyramid_max_adds',3)}</b>"
            )
            return True
        else:
            log.warning(f"PYRAMID order failed for {parent_ticket_str}: {result}")
            return False
    except Exception as e:
        log.warning(f"_try_pyramid_scale error: {e}")
        return False


def close_position(ticket: int, volume: float = 0) -> str:
    vol_str = f"{volume:.2f}" if volume > 0 else ""
    cmd = f"CLOSE|{ticket}|||||{vol_str}|"
    return send_command(cmd)


def modify_position(ticket: int, sl: float, tp: float) -> str:
    cmd = f"MODIFY|{ticket}|||{sl:.5f}|{tp:.5f}||"
    return send_command(cmd)


# ── Daily Loss / Drawdown Check ───────────────────────────────────────────────
def check_daily_limits(state: TraderState, equity: float) -> bool:
    """
    Returns False if trading should be halted.
    Checks both the daily loss limit and max drawdown from peak equity.
    """
    if state.trading_halted:
        return False

    # Initialize baseline on first run (use bool flag, not equity==0 sentinel)
    if not state.day_initialized:
        state.day_initialized = True
        state.day_start_equity = equity
        state.last_reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return True

    # Daily loss check
    if state.day_start_equity > 0:
        day_loss_pct = ((equity - state.day_start_equity) / state.day_start_equity) * 100
        if day_loss_pct <= -DAILY_LOSS_LIMIT:
            state.trading_halted = True
            state.halt_reason = f"Daily loss limit hit: {day_loss_pct:.2f}%"
            log.warning(f"TRADING HALTED: {state.halt_reason}")
            send_telegram(
                f"🛑 <b>TRADING HALTED</b>\n"
                f"Daily loss limit reached: <b>{day_loss_pct:.2f}%</b>\n"
                f"Balance: <b>${equity:,.2f}</b>\n"
                f"Resumes: next trading day (22:00 UTC)"
            )
            return False

    # Peak drawdown check
    if state.peak_equity > 0:
        drawdown_pct = ((state.peak_equity - equity) / state.peak_equity) * 100
        if drawdown_pct > state.peak_drawdown:
            state.peak_drawdown = drawdown_pct
        if drawdown_pct >= MAX_DD_FROM_PEAK:
            state.trading_halted = True
            state.halt_reason = f"Max drawdown from peak hit: {drawdown_pct:.2f}%"
            log.warning(f"TRADING HALTED: {state.halt_reason}")
            send_telegram(
                f"🚨 <b>DRAWDOWN LIMIT HIT — HALTED</b>\n"
                f"Drawdown from peak: <b>{drawdown_pct:.2f}%</b> (limit: {MAX_DD_FROM_PEAK:.0f}%)\n"
                f"Equity: <b>${equity:,.2f}</b> | Peak was: <b>${state.peak_equity:,.2f}</b>\n"
                f"Auto-resumes when equity recovers to 95% of peak"
            )
            return False

    return True


def reset_daily_if_new_day(state: TraderState, equity: float):
    """
    Reset daily stats exactly once per UTC calendar day.
    Uses last_reset_day (YYYY-MM-DD) so it fires once regardless of
    how many scans run in the first minute, and survives restarts.
    """
    now = datetime.now(timezone.utc)
    # FX trading day starts at 22:00 UTC (NY close); shift day boundary accordingly
    if now.hour >= 22:
        trading_day = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        trading_day = now.strftime("%Y-%m-%d")
    if state.last_reset_day == trading_day:
        return  # Already reset today

    log.info(f"New trading day ({trading_day}) — resetting daily stats")
    # Send daily summary before resetting counters
    if state.day_trades > 0 or state.day_profit != 0:
        _day_pct = ((state.day_profit / state.day_start_equity) * 100) if state.day_start_equity else 0
        send_telegram(
            f"📊 <b>Daily Summary — {state.last_reset_day}</b>\n"
            f"Trades: <b>{state.day_trades}</b> | Won: <b>{state.winning_trades}</b> | Lost: <b>{state.losing_trades}</b>\n"
            f"Day P&amp;L: <b>${state.day_profit:+,.2f}</b> (<b>{_day_pct:+.2f}%</b>)\n"
            f"Total P&amp;L: <b>${state.total_profit:+,.2f}</b> | Balance: <b>${equity:,.2f}</b>"
        )
    state.last_reset_day    = trading_day
    state.day_initialized   = True
    state.day_start_equity  = equity
    state.day_profit        = 0.0
    state.day_trades        = 0
    # Only clear daily-limit halts — preserve DD-from-peak halts across the day reset
    if "Daily" in state.halt_reason or "profit target" in state.halt_reason:
        state.trading_halted = False
        state.halt_reason    = ""
    state.recently_traded   = {}
    state.sym_loss_streak     = {}   # Reset per-symbol loss streaks each day
    state.session_loss_streak = {}   # Reset per-session loss streaks each day
    save_state(state)  # Persist immediately so a crash can't re-trigger the reset


# ── Trade Management ──────────────────────────────────────────────────────────
def manage_open_trades(state: TraderState, data: dict, memory=None):
    """
    Manage existing trades:
    - Detect closes using MT5 history for accurate settled P&L
    - Adjust risk streak once after all closes in a cycle
    - Take partial profits at TP1 (50%) and TP2 (30%), let 20% run to TP3
    - Trail SL to break-even (with 1-pip buffer) at 1R, then to 1R at 2R, tight at 3R
    - Use per-symbol pip size for the modify threshold
    """
    positions       = get_open_positions(data)
    position_tickets = {str(p.get("ticket")): p for p in positions}
    charts           = data.get("charts", {})

    # Load rules once per cycle; smart_trail config lives under rules["smart_trail"]
    rules = load_rules()
    smart_trail_on = _SMART_TRAIL_AVAILABLE and _smart_trail_enabled(rules)

    # History lookup for accurate settled P&L (commission + swap included)
    history_by_ticket = {
        str(h.get("ticket", "")): h
        for h in data.get("history", [])
        if isinstance(h, dict)
    }

    # Detect closed trades (were in active_trades, no longer in open positions)
    # PAPER_ tickets never appear in MT5 positions — skip them here; simulation below handles them
    closed_tickets = [t for t in list(state.active_trades.keys())
                      if t not in position_tickets and not t.startswith("PAPER_")]

    any_closed = False
    for ticket in closed_tickets:
        trade = state.active_trades.pop(ticket, {})

        # Use history for settled P&L; fall back to last floating value if not yet exported
        if ticket in history_by_ticket:
            h = history_by_ticket[ticket]
            profit = h.get("profit", 0) + h.get("swap", 0) + h.get("commission", 0)
        else:
            profit = trade.get("last_profit", 0)

        entry_price   = trade.get("entry", 0)
        sl_price      = trade.get("sl", 0)
        close_price   = history_by_ticket.get(ticket, {}).get("price", 0) if ticket in history_by_ticket else 0
        # R-multiple = profit / (risk per unit × lot size).  Use SL distance for risk.
        risk_distance = abs(entry_price - sl_price) if (entry_price > 0 and sl_price > 0) else 1
        risk_usd_per_r = risk_distance   # approximate; memory records full USD via lot size
        r_multiple    = round(profit / (risk_distance * 10000), 2) if risk_distance > 0 else 0
        # Simpler meaningful R: sign follows profit, magnitude is pips gained / pips risked
        if entry_price > 0 and sl_price > 0 and risk_distance > 0 and close_price > 0:
            pips_gained = abs(close_price - entry_price)
            r_multiple  = round((pips_gained / risk_distance) * (1 if profit >= 0 else -1), 2)

        # Determine exit level
        tp1 = trade.get("tp1", 0)
        tp2 = trade.get("tp2", 0)
        tp3 = trade.get("tp3", 0)
        if profit >= 0 and tp3 > 0 and close_price:
            direction = trade.get("direction", "")
            if direction == "BUY":
                if close_price >= tp3: exit_level = "TP3"
                elif close_price >= tp2: exit_level = "TP2"
                elif close_price >= tp1: exit_level = "TP1"
                else: exit_level = "PARTIAL"
            elif direction == "SELL":
                if close_price <= tp3: exit_level = "TP3"
                elif close_price <= tp2: exit_level = "TP2"
                elif close_price <= tp1: exit_level = "TP1"
                else: exit_level = "PARTIAL"
            else: exit_level = "WIN"
        elif profit < 0:
            exit_level = "SL"
        else:
            exit_level = "MANUAL"

        # Re-entry: if trailing SL was hit while in profit (not a TP target), queue
        # a re-entry at the original entry (= better RR since SL is unchanged).
        if profit > 0 and exit_level not in ("TP1", "TP2", "TP3") and trade.get("entry"):
            sym_reentry = trade.get("symbol", "")
            if sym_reentry and sym_reentry not in state.pending_reentries:
                state.pending_reentries[sym_reentry] = {
                    "symbol":             sym_reentry,
                    "direction":          trade.get("direction", ""),
                    "entry":              trade.get("entry", 0),
                    "sl":                 trade.get("sl", 0),
                    "tp1":                trade.get("tp1", 0),
                    "tp2":                trade.get("tp2", 0),
                    "tp3":                trade.get("tp3", 0),
                    "entry_type":         trade.get("entry_type", "ICT"),
                    "valid_until":        time.time() + 3600,
                    "profit_from_parent": profit,
                }
                log.info(f"{sym_reentry} Re-entry queued: {trade.get('direction')} stopped in profit "
                         f"(+${profit:.2f}) — waiting for pullback to {trade.get('entry', 0):.5f}")

        _closed_symbol = trade.get("symbol", "")
        _closed_session = trade.get("session", "")
        if profit >= 0:
            state.win_streak   += 1
            state.loss_streak   = 0
            state.winning_trades += 1
            state.sym_loss_streak[_closed_symbol] = 0
            if _closed_session:
                state.session_loss_streak[_closed_session] = 0
            log.info(f"Trade {ticket} CLOSED WIN: +{profit:.2f} | R={r_multiple:.2f} | Streak: {state.win_streak}W | Exit: {exit_level}")
            _acc = get_account(data)
            send_telegram(
                f"✅ <b>TRADE CLOSED — WIN</b>\n"
                f"<b>{_closed_symbol}</b> {trade.get('direction','')} | Exit: <b>{exit_level}</b>\n"
                f"Entry: {trade.get('entry',0):.5g} → Close: {close_price:.5g}\n"
                f"P&amp;L: <b>+${profit:.2f}</b> | R: <b>+{r_multiple:.2f}R</b>\n"
                f"Streak: <b>{state.win_streak}W</b> | WR: {(state.winning_trades/max(state.winning_trades+state.losing_trades,1)*100):.0f}% "
                f"({state.winning_trades}W/{state.losing_trades}L)\n"
                f"Balance: <b>${_acc.get('balance',0):,.2f}</b>"
            )
        else:
            state.loss_streak  += 1
            state.win_streak    = 0
            state.losing_trades += 1
            _prev_sym_losses = state.sym_loss_streak.get(_closed_symbol, 0)
            state.sym_loss_streak[_closed_symbol] = _prev_sym_losses + 1
            if _prev_sym_losses + 1 >= 2:
                log.info(f"{_closed_symbol}: {_prev_sym_losses + 1} consecutive losses — pausing symbol today")
            if _closed_session:
                _prev_sess_losses = state.session_loss_streak.get(_closed_session, 0)
                state.session_loss_streak[_closed_session] = _prev_sess_losses + 1
                if _prev_sess_losses + 1 >= 3:
                    log.warning(f"SESSION {_closed_session}: {_prev_sess_losses + 1} consecutive losses — penalising session confidence")
            log.info(f"Trade {ticket} CLOSED LOSS: {profit:.2f} | R={r_multiple:.2f} | Streak: {state.loss_streak}L | Exit: {exit_level}")
            _acc = get_account(data)
            send_telegram(
                f"❌ <b>TRADE CLOSED — LOSS</b>\n"
                f"<b>{_closed_symbol}</b> {trade.get('direction','')} | Exit: <b>{exit_level}</b>\n"
                f"Entry: {trade.get('entry',0):.5g} → Close: {close_price:.5g}\n"
                f"P&amp;L: <b>${profit:.2f}</b> | R: <b>{r_multiple:.2f}R</b>\n"
                f"Streak: <b>{state.loss_streak}L</b> | WR: {(state.winning_trades/max(state.winning_trades+state.losing_trades,1)*100):.0f}% "
                f"({state.winning_trades}W/{state.losing_trades}L)\n"
                f"Balance: <b>${_acc.get('balance',0):,.2f}</b>"
            )

        state.total_trades  += 1
        state.total_profit  += profit
        state.day_profit    += profit
        any_closed = True

        # Persist closure for telegram bot AlertMonitor (rich notification fallback)
        try:
            close_record = {
                "ticket":      str(ticket),
                "symbol":      _closed_symbol,
                "direction":   trade.get("direction", ""),
                "entry":       round(trade.get("entry", 0), 5),
                "close":       round(close_price or 0, 5),
                "profit":      round(profit, 2),
                "r_multiple":  round(r_multiple, 2),
                "exit_level":  exit_level,
                "session":     _closed_session,
                "closed_at":   datetime.now(timezone.utc).isoformat(),
                "outcome":     "WIN" if profit >= 0 else "LOSS",
            }
            state.recent_closes.append(close_record)
            # Keep only the last 20
            state.recent_closes = state.recent_closes[-20:]
        except Exception as e:
            log.warning(f"recent_closes append error: {e}")

        # ── Phase 2f: feed closure into learning loop ─────────────────────
        try:
            if _LEARNING_AVAILABLE:
                fs = _get_feature_store()
                if fs is not None:
                    fs.record_outcome(
                        signal_id=str(ticket),
                        outcome="WIN" if profit >= 0 else "LOSS",
                        r_multiple=float(r_multiple),
                        profit_usd=float(profit),
                        exit_level=exit_level or "",
                    )
                learner = _get_online_learner()
                if learner is not None:
                    triggered = learner.maybe_retrain()
                    if triggered:
                        log.info("OnlineLearner retrain triggered (background)")
        except Exception as _e:
            log.debug(f"learning loop hook error: {_e}")

        # Record outcome in trade memory
        if memory and trade.get("memory_trade_id"):
            try:
                memory.record_close(
                    trade_id=trade["memory_trade_id"],
                    close_price=close_price or entry_price,
                    profit_usd=profit,
                    r_multiple=r_multiple,
                    tp_level_hit=exit_level,
                    close_reason=f"Auto-closed | profit={profit:.2f}",
                )
            except Exception as e:
                log.warning(f"Memory record_close error: {e}")

    # Adjust risk once after all closures (prevents double-adjustment within one scan)
    if any_closed:
        adjust_risk(state)

    _regime = _load_regime()

    # Manage active positions
    for ticket_str, pos in position_tickets.items():
        magic = pos.get("magic", 0)

        # Live mode: only manage OMNI's trades by magic number
        # Paper mode: only manage positions we explicitly opened (tracked in active_trades)
        if not PAPER_MODE and magic != OUR_MAGIC:
            continue
        if PAPER_MODE and ticket_str not in state.active_trades:
            continue

        symbol        = pos.get("symbol", "")
        current_price = pos.get("current_price", 0)
        open_price    = pos.get("open_price", 0)
        current_sl    = pos.get("sl", 0)
        current_tp    = pos.get("tp", 0)
        profit        = pos.get("profit", 0)
        pos_type      = pos.get("type", "")
        volume        = pos.get("volume", 0)

        # Track last floating P&L as fallback for win/loss detection
        if ticket_str not in state.active_trades:
            state.active_trades[ticket_str] = {}
        state.active_trades[ticket_str]["last_profit"] = profit

        if current_price == 0 or open_price == 0 or current_sl == 0:
            continue

        # Per-symbol pip size for thresholds
        sym_info = charts.get(symbol, {})
        pip_size = sym_info.get("point", 0.0001) * 10
        min_lot  = sym_info.get("min_lot", 0.01)

        setup     = state.active_trades[ticket_str]
        tp1       = setup.get("tp1", current_tp)
        tp2       = setup.get("tp2", current_tp)
        tp3       = setup.get("tp3", current_tp)
        tp1_taken = setup.get("tp1_taken", False)
        tp2_taken = setup.get("tp2_taken", False)
        risk      = abs(open_price - current_sl)

        if risk == 0:
            continue

        new_sl = current_sl
        new_tp = current_tp

        # Cancel stale pending limit orders where spread has widened beyond tolerance
        order_type_str = state.active_trades.get(ticket_str, {}).get("order_type", "")
        if "LIMIT" in order_type_str:
            entry_spread = state.active_trades.get(ticket_str, {}).get("entry_spread", 0)
            current_ask  = sym_info.get("ask", 0)
            current_bid  = sym_info.get("bid", 0)
            current_spread = current_ask - current_bid if current_ask > current_bid else 0

            entry_ts   = state.active_trades.get(ticket_str, {}).get("entry_ts", time.time())
            mins_pending = (time.time() - entry_ts) / 60

            if (entry_spread > 0 and current_spread > entry_spread * 1.5 and mins_pending > 1):
                log.warning(f"{symbol} PENDING ORDER spread widened {current_spread:.5f} vs entry {entry_spread:.5f} — cancelling limit")
                if ticket_str.isdigit():
                    close_position(int(ticket_str), 0)  # Cancel pending order
                continue

        if pos_type == "BUY":
            profit_in_r = (current_price - open_price) / risk

            # Pyramid scale-in (winning trade rides on pivot bounces) — only fires when
            # PYRAMID_ENABLED is set, profit ≥ 0.5R, fresh same-direction pivot bounce,
            # and opposing liquidity hasn't been swept yet
            try:
                _try_pyramid_scale(state, data, ticket_str,
                                   state.active_trades.get(ticket_str, {}),
                                   profit_in_r, current_price, pos_type)
            except Exception as _pe:
                log.debug(f"pyramid eval (BUY) error: {_pe}")

            # Partial TP1: close 65% when price hits first target — bank the majority immediately
            if not tp1_taken and tp1 > 0 and current_price >= tp1:
                close_vol = round(volume * 0.50, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp1"
                    close_ok = result.startswith("OK") or result.startswith("PAPER")
                    log.info(f"{symbol} TP1 hit — closed 50% ({close_vol} lots) | {result}")
                    if close_ok:
                        state.active_trades[ticket_str]["tp1_taken"] = True
                        tp1_taken = True
                        # Move SL to break-even immediately after TP1 hit
                        new_sl = max(new_sl, open_price + pip_size)
                        # Advance hard TP on MT5 to TP2 so the runner doesn't auto-close at TP1
                        if tp2 > 0 and tp2 > current_tp:
                            new_tp = tp2
                            log.info(f"{symbol} Advancing MT5 TP to TP2 ({new_tp:.5f})")
                        send_telegram(
                            f"💰 <b>TP1 HIT — {symbol} BUY</b>\n"
                            f"Closed 50% @ {current_price:.5g} | +${close_vol * (current_price - open_price) * 10000:.2f} approx\n"
                            f"Runner active ({volume - close_vol:.2f} lots) → TP2: {tp2:.5g}"
                        )

            # Partial TP2: close 25% of original when price hits second target
            if tp1_taken and not tp2_taken and tp2 > 0 and current_price >= tp2:
                close_vol = round(volume * 0.25, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp2"
                    close_ok = result.startswith("OK") or result.startswith("PAPER")
                    log.info(f"{symbol} TP2 hit — closed 25% ({close_vol} lots) | {result}")
                    if close_ok:
                        state.active_trades[ticket_str]["tp2_taken"] = True
                        tp2_taken = True
                        # Runner: remove fixed TP3 — let smart trail manage it freely
                        new_tp = 0
                        log.info(f"{symbol} TP2 hit — runner released, smart trail managing")
                        send_telegram(
                            f"💰 <b>TP2 HIT — {symbol} BUY</b>\n"
                            f"Closed 25% @ {current_price:.5g}\n"
                            f"Runner released ({volume - close_vol:.2f} lots) — trailing to TP3: {tp3:.5g}"
                        )

            # Emergency exit: SL may not have fired via EA — close runaway losses
            if not tp1_taken and profit_in_r < -1.2:
                try:
                    entry_ts = float(setup.get("entry_ts", 0)) or time.time()
                    minutes_open = (time.time() - entry_ts) / 60
                    if minutes_open > 25:
                        log.warning(f"{symbol} BUY EMERGENCY EXIT — {profit_in_r:.2f}R runaway loss at {minutes_open:.0f}min")
                        if ticket_str.isdigit():
                            close_position(int(ticket_str), volume)
                        continue
                except Exception:
                    pass

            # Early loss cut — if trade is moving against us and has shown no progress,
            # exit before reaching full SL. Saves capital vs waiting for full stop.
            if not tp1_taken and profit_in_r < -0.3:
                entry_time_str = setup.get("entry_time", "")
                try:
                    import time as _time
                    entry_ts = float(setup.get("entry_ts", 0)) or _time.time()
                    minutes_open = (_time.time() - entry_ts) / 60
                    if minutes_open > 20:
                        log.info(f"{symbol} BUY early exit — {profit_in_r:.2f}R adverse after {minutes_open:.0f}min — cutting loss")
                        if ticket_str.isdigit():
                            close_position(int(ticket_str), volume)
                        continue
                except Exception:
                    pass

            # Trail SL to break-even + 1 pip at 0.4R (tightened from 0.5R for faster protection)
            if profit_in_r >= 0.4 and current_sl < open_price:
                new_sl = max(new_sl, open_price + pip_size)
                if new_sl > current_sl:
                    log.info(f"{symbol} BUY 0.5R: Moving SL to break-even ({new_sl:.5f})")

            # Lock 0.5R profit at 1R — tightened from 0.3R to protect gains faster
            if profit_in_r >= 1.0 and current_sl < open_price + risk * 0.5:
                new_sl = max(new_sl, open_price + risk * 0.5)
                if new_sl > current_sl:
                    log.info(f"{symbol} BUY 1R: Locking 0.5R profit in SL ({new_sl:.5f})")

            # Trail SL to lock 1.2R profit at 2R move
            if profit_in_r >= 2.0 and current_sl < open_price + risk * 1.2:
                new_sl = max(new_sl, open_price + risk * 1.2)
                if new_sl > current_sl:
                    log.info(f"{symbol} BUY 2R: Locking 1.2R profit in SL ({new_sl:.5f})")

            # Tight trail at 3R — width varies by AI regime
            if profit_in_r >= 3.0:
                _trail_width = 0.20 if _regime == "VOLATILE" else (0.4 if _regime == "TRENDING" else 0.35)
                tight = current_price - risk * _trail_width
                if tight > new_sl:
                    new_sl = tight
                    log.info(f"{symbol} BUY 3R: Tight trail ({new_sl:.5f}) [regime={_regime}]")

            # Progressive trail beyond 3R: trail 0.3R behind price
            if profit_in_r > 3.0:
                prog = current_price - risk * 0.3
                if prog > new_sl:
                    new_sl = prog

            # Distribution phase exit: trim on H1 exhaustion or M15 CHoCH against position
            if profit_in_r >= 1.0 and not setup.get("dist_exit_1_done"):
                try:
                    import ict_precision as _ict_ref
                    _m15b = _ict_ref._parse_bars(sym_info.get("M15", []))
                    _h1b  = _ict_ref._parse_bars(sym_info.get("H1",  []))
                    _exh  = _ict_ref.detect_push_exhaustion(_h1b) if _h1b else {}
                    if "EXHAUSTION" in str(_exh.get("signal", "")).upper():
                        close_vol = round(volume * 0.25, 2)
                        if close_vol >= min_lot and ticket_str.isdigit():
                            result = close_position(int(ticket_str), close_vol)
                            if result.startswith("OK") or result.startswith("PAPER"):
                                state.active_trades[ticket_str]["dist_exit_1_done"] = True
                                log.info(f"{symbol} BUY dist-exit 25% — H1 exhaustion at {profit_in_r:.1f}R")
                    elif profit_in_r >= 2.0 and not setup.get("dist_exit_2_done"):
                        if _m15b and _ict_ref.detect_mss(_m15b, "SELL", lookback=6):
                            close_vol = round(volume * 0.50, 2)
                            if close_vol >= min_lot and ticket_str.isdigit():
                                result = close_position(int(ticket_str), close_vol)
                                if result.startswith("OK") or result.startswith("PAPER"):
                                    state.active_trades[ticket_str]["dist_exit_2_done"] = True
                                    log.info(f"{symbol} BUY dist-exit 50% — M15 CHoCH SELL at {profit_in_r:.1f}R")
                except Exception:
                    pass

        elif pos_type == "SELL":
            profit_in_r = (open_price - current_price) / risk

            # Pyramid scale-in for SELL trades (mirrored logic)
            try:
                _try_pyramid_scale(state, data, ticket_str,
                                   state.active_trades.get(ticket_str, {}),
                                   profit_in_r, current_price, pos_type)
            except Exception as _pe:
                log.debug(f"pyramid eval (SELL) error: {_pe}")

            # Partial TP1: close 50% — bank half, keep a meaningful runner
            if not tp1_taken and tp1 > 0 and current_price <= tp1:
                close_vol = round(volume * 0.50, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp1"
                    close_ok = result.startswith("OK") or result.startswith("PAPER")
                    log.info(f"{symbol} TP1 hit — closed 50% ({close_vol} lots) | {result}")
                    if close_ok:
                        state.active_trades[ticket_str]["tp1_taken"] = True
                        tp1_taken = True
                        # Move SL to break-even immediately after TP1 hit
                        new_sl = min(new_sl, open_price - pip_size)
                        if tp2 > 0 and tp2 < current_tp:
                            new_tp = tp2
                            log.info(f"{symbol} Advancing MT5 TP to TP2 ({new_tp:.5f})")
                        send_telegram(
                            f"💰 <b>TP1 HIT — {symbol} SELL</b>\n"
                            f"Closed 50% @ {current_price:.5g} | approx +${close_vol * (open_price - current_price) * 10000:.2f}\n"
                            f"Runner active ({volume - close_vol:.2f} lots) → TP2: {tp2:.5g}"
                        )

            # Partial TP2: close 25% of original, leave 25% runner to TP3
            if tp1_taken and not tp2_taken and tp2 > 0 and current_price <= tp2:
                close_vol = round(volume * 0.25, 2)
                if close_vol >= min_lot:
                    if ticket_str.isdigit():
                        result = close_position(int(ticket_str), close_vol)
                    else:
                        result = "PAPER|partial_tp2"
                    close_ok = result.startswith("OK") or result.startswith("PAPER")
                    log.info(f"{symbol} TP2 hit — closed 25% ({close_vol} lots) | {result}")
                    if close_ok:
                        state.active_trades[ticket_str]["tp2_taken"] = True
                        tp2_taken = True
                        # Runner: remove fixed TP3 — let smart trail manage it freely
                        new_tp = 0
                        log.info(f"{symbol} TP2 hit — runner released, smart trail managing")
                        send_telegram(
                            f"💰 <b>TP2 HIT — {symbol} SELL</b>\n"
                            f"Closed 25% @ {current_price:.5g}\n"
                            f"Runner released ({volume - close_vol:.2f} lots) — trailing to TP3: {tp3:.5g}"
                        )

            # Emergency exit: SL may not have fired via EA — close runaway losses
            if not tp1_taken and profit_in_r < -1.2:
                try:
                    entry_ts = float(setup.get("entry_ts", 0)) or time.time()
                    minutes_open = (time.time() - entry_ts) / 60
                    if minutes_open > 25:
                        log.warning(f"{symbol} SELL EMERGENCY EXIT — {profit_in_r:.2f}R runaway loss at {minutes_open:.0f}min")
                        if ticket_str.isdigit():
                            close_position(int(ticket_str), volume)
                        continue
                except Exception:
                    pass

            if not tp1_taken and profit_in_r < -0.3:
                try:
                    import time as _time
                    entry_ts = float(setup.get("entry_ts", 0)) or _time.time()
                    minutes_open = (_time.time() - entry_ts) / 60
                    if minutes_open > 20:
                        log.info(f"{symbol} SELL early exit — {profit_in_r:.2f}R adverse after {minutes_open:.0f}min — cutting loss")
                        if ticket_str.isdigit():
                            close_position(int(ticket_str), volume)
                        continue
                except Exception:
                    pass

            if profit_in_r >= 0.4 and current_sl > open_price:
                new_sl = min(new_sl, open_price - pip_size)
                if new_sl < current_sl:
                    log.info(f"{symbol} SELL 0.4R: Moving SL to break-even ({new_sl:.5f})")

            # Lock 0.5R profit at 1R — tightened from 0.3R
            if profit_in_r >= 1.0 and current_sl > open_price - risk * 0.5:
                new_sl = min(new_sl, open_price - risk * 0.5)
                if new_sl < current_sl:
                    log.info(f"{symbol} SELL 1R: Locking 0.5R profit in SL ({new_sl:.5f})")

            # Lock 1.2R profit at 2R move
            if profit_in_r >= 2.0 and current_sl > open_price - risk * 1.2:
                new_sl = min(new_sl, open_price - risk * 1.2)
                if new_sl < current_sl:
                    log.info(f"{symbol} SELL 2R: Locking 1.2R profit in SL ({new_sl:.5f})")

            # Tight trail at 3R — width varies by AI regime
            if profit_in_r >= 3.0:
                _trail_width = 0.20 if _regime == "VOLATILE" else (0.4 if _regime == "TRENDING" else 0.35)
                tight = current_price + risk * _trail_width
                if tight < new_sl:
                    new_sl = tight
                    log.info(f"{symbol} SELL 3R: Tight trail ({new_sl:.5f}) [regime={_regime}]")

            # Progressive trail beyond 3R
            if profit_in_r > 3.0:
                prog = current_price + risk * 0.3
                if prog < new_sl:
                    new_sl = prog

            # Distribution phase exit: trim on H1 exhaustion or M15 CHoCH against position
            if profit_in_r >= 1.0 and not setup.get("dist_exit_1_done"):
                try:
                    import ict_precision as _ict_ref
                    _m15b = _ict_ref._parse_bars(sym_info.get("M15", []))
                    _h1b  = _ict_ref._parse_bars(sym_info.get("H1",  []))
                    _exh  = _ict_ref.detect_push_exhaustion(_h1b) if _h1b else {}
                    if "EXHAUSTION" in str(_exh.get("signal", "")).upper():
                        close_vol = round(volume * 0.25, 2)
                        if close_vol >= min_lot and ticket_str.isdigit():
                            result = close_position(int(ticket_str), close_vol)
                            if result.startswith("OK") or result.startswith("PAPER"):
                                state.active_trades[ticket_str]["dist_exit_1_done"] = True
                                log.info(f"{symbol} SELL dist-exit 25% — H1 exhaustion at {profit_in_r:.1f}R")
                    elif profit_in_r >= 2.0 and not setup.get("dist_exit_2_done"):
                        if _m15b and _ict_ref.detect_mss(_m15b, "BUY", lookback=6):
                            close_vol = round(volume * 0.50, 2)
                            if close_vol >= min_lot and ticket_str.isdigit():
                                result = close_position(int(ticket_str), close_vol)
                                if result.startswith("OK") or result.startswith("PAPER"):
                                    state.active_trades[ticket_str]["dist_exit_2_done"] = True
                                    log.info(f"{symbol} SELL dist-exit 50% — M15 CHoCH BUY at {profit_in_r:.1f}R")
                except Exception:
                    pass

        # ── Smart trail overlay (Phase 1) — gated behind rules.json smart_trail.enabled
        # Never *loosens* the SL computed above — only tightens in our favour, or fires
        # a should_close signal when the engine detects an opposing CHoCH in profit.
        force_close_by_trail = False
        if smart_trail_on:
            try:
                proposal = maybe_smart_trail(pos, charts, rules, state.last_scan_context)
            except Exception as e:
                log.warning(f"smart_trail error for {symbol}/{ticket_str}: {e}")
                proposal = None

            if proposal is not None:
                # Record for dashboard/telemetry regardless of whether we act on it
                state.last_trail_proposals[ticket_str] = {
                    "symbol":       symbol,
                    "direction":    pos_type,
                    "ts":           datetime.now(timezone.utc).isoformat(),
                    "current_sl":   current_sl,
                    "legacy_sl":    new_sl,
                    "proposed_sl":  proposal.new_sl,
                    "should_close": bool(proposal.should_close),
                    "reason":       proposal.reason,
                    "layers":       list(proposal.layers_fired or []),
                }

                # should_close → fully close the position
                if proposal.should_close and ticket_str.isdigit():
                    close_position(int(ticket_str))
                    log.info(f"{symbol} smart_trail CLOSE: {proposal.reason}")
                    force_close_by_trail = True
                else:
                    # Overlay: take whichever SL is more protective for the side
                    if pos_type == "BUY":
                        overlay_sl = max(new_sl, proposal.new_sl)
                    else:  # SELL
                        overlay_sl = min(new_sl, proposal.new_sl)
                    if overlay_sl != new_sl:
                        log.info(f"{symbol} smart_trail overlay SL {new_sl:.5f}→{overlay_sl:.5f} "
                                 f"({proposal.reason} | {','.join(proposal.layers_fired or [])})")
                        new_sl = overlay_sl

        if force_close_by_trail:
            continue

        # Modify only when SL moves by at least 3 pips (prevents micro-update spam)
        min_move = max(risk * 0.05, pip_size * 3)
        if abs(new_sl - current_sl) > min_move and ticket_str.isdigit():
            result = modify_position(int(ticket_str), new_sl, new_tp)
            log.info(f"Modified {ticket_str}: SL {current_sl:.5f}→{new_sl:.5f} | {result}")


# ── Stale Limit Order Cancellation ───────────────────────────────────────────
def cancel_stale_limits(state: TraderState, data: dict):
    """
    Cancel BUY_LIMIT / SELL_LIMIT orders where price has blown through the entry
    level by more than 10 pips — the setup is no longer valid at that point.

    BUY_LIMIT becomes stale when current price > entry + 10 pips (missed the dip).
    SELL_LIMIT becomes stale when current price < entry - 10 pips (missed the rally).
    """
    prices   = {p["symbol"]: p for p in data.get("prices", [])}
    charts   = data.get("charts", {})
    expired  = []

    for ticket_str, trade in state.active_trades.items():
        order_type = trade.get("order_type", "")
        if order_type not in ("BUY_LIMIT", "SELL_LIMIT"):
            continue

        symbol    = trade.get("symbol", "")
        entry     = trade.get("entry", 0)
        if not symbol or entry == 0:
            continue

        sym_info  = charts.get(symbol, {})
        pip_size  = sym_info.get("point", 0.0001) * 10
        price_now = prices.get(symbol, {}).get("bid", 0)
        if price_now == 0:
            continue

        stale = False
        if order_type == "BUY_LIMIT" and price_now > entry + pip_size * 10:
            stale = True
            log.info(f"{symbol} BUY_LIMIT stale — price {price_now:.5f} blew past entry {entry:.5f} by {(price_now - entry)/pip_size:.1f} pips — cancelling")
        elif order_type == "SELL_LIMIT" and price_now < entry - pip_size * 10:
            stale = True
            log.info(f"{symbol} SELL_LIMIT stale — price {price_now:.5f} blew past entry {entry:.5f} by {(entry - price_now)/pip_size:.1f} pips — cancelling")

        if stale:
            if ticket_str.isdigit():
                result = send_command(f"DELETE|{ticket_str}|||||")
                log.info(f"{symbol} Cancel pending order {ticket_str}: {result}")
            expired.append(ticket_str)

    for t in expired:
        state.active_trades.pop(t, None)


# ── Scale-In Logic ────────────────────────────────────────────────────────────
def check_scale_in(state: TraderState, data: dict, equity: float, memory=None):
    """
    After TP1 is hit and push momentum is confirmed, add to the winning position.

    Rules (from rules.json scale_in_rules):
    1. Trade must be in profit >= 1R (SL already at BE or better)
    2. TP1 must already be taken (50% of position closed)
    3. Push phase confirmed on M15 or H1 (bodies growing in direction)
    4. No opposing BOS on H1
    5. Max 1 scale-in per trade

    Scale-in position: 50% of original lot size
    Scale-in SL: same as current trailing SL (at BE or better)
    Scale-in TP: 1.5× TP3 distance from scale-in entry
    """
    positions      = get_open_positions(data)
    pos_dict       = {str(p.get("ticket")): p for p in positions}
    charts         = data.get("charts", {})

    for ticket_str, trade in state.active_trades.items():
        # Read scaling rules (updated in rules.json)
        _scale_rules = load_rules().get("scaling", {})
        max_adds       = int(_scale_rules.get("max_adds", 3))
        min_profit_r   = float(_scale_rules.get("min_add_profit_r", 0.75))

        # Allow up to max_adds per position; no TP1 requirement (scale during move)
        add_count = trade.get("scale_in_count", 0)
        if add_count >= max_adds:
            continue

        symbol    = trade.get("symbol", "")
        direction = trade.get("direction", "")
        pos       = pos_dict.get(ticket_str)
        if not pos:
            continue

        current_price = pos.get("current_price", 0)
        open_price    = pos.get("open_price", 0)
        current_sl    = pos.get("sl", 0)
        original_vol  = trade.get("volume_original", 0)

        if current_price == 0 or open_price == 0 or current_sl == 0:
            continue

        # Check we're at least 1R in profit
        risk = abs(open_price - current_sl) if current_sl else 0
        if risk == 0:
            continue

        if direction == "BUY":
            profit_r = (current_price - open_price) / risk
        else:
            profit_r = (open_price - current_price) / risk

        if profit_r < min_profit_r:
            continue

        # Check push momentum on M15 or H1
        sym_data = charts.get(symbol, {})
        import ict_precision as ict

        # Parse bars from current chart data
        h1_bars  = ict._parse_bars(sym_data.get("H1",  []))
        m15_bars = ict._parse_bars(sym_data.get("M15", []))

        push_h1  = ict.detect_push_exhaustion(h1_bars)  if h1_bars  else {"phase": "NEUTRAL"}
        push_m15 = ict.detect_push_exhaustion(m15_bars) if m15_bars else {"phase": "NEUTRAL"}

        # Prefer M15 for scale-in decision
        push = push_m15 if push_m15.get("phase") != "NEUTRAL" else push_h1

        push_confirmed = (
            push.get("phase") == "PUSH"
            and (
                (direction == "BUY"  and push.get("direction") == "UP")
                or
                (direction == "SELL" and push.get("direction") == "DOWN")
            )
        )

        # Block scale-in if exhaustion is forming (prevent adding into distribution end)
        exhaustion_h1 = push_h1.get("signal", "").upper()
        if "EXHAUSTION" in exhaustion_h1:
            log.debug(f"{symbol} scale-in blocked — H1 push exhaustion detected")
            continue

        if not push_confirmed:
            log.debug(f"{symbol} scale-in skipped — no push momentum (phase={push.get('phase')})")
            continue

        # Check no opposing BOS on H1
        h4_struct = ict.get_h4_structure(h1_bars) if h1_bars else "RANGING"
        opposing_bos = (
            (direction == "BUY"  and h4_struct == "BOS_BEARISH")
            or
            (direction == "SELL" and h4_struct == "BOS_BULLISH")
        )
        if opposing_bos:
            log.info(f"{symbol} scale-in skipped — opposing BOS detected on H1 ({h4_struct})")
            continue

        # Calculate scale-in parameters
        sym_info_data = sym_data
        scale_vol = round(original_vol * 0.50, 2)
        min_lot   = sym_info_data.get("min_lot", 0.01)
        if scale_vol < min_lot:
            continue

        scale_entry  = current_price
        scale_sl     = current_sl  # Already at BE or better
        tp3_original = trade.get("tp3", 0)

        if tp3_original > 0 and open_price > 0:
            # Scale TP = 1.5× the remaining distance to TP3
            remaining_to_tp3 = abs(tp3_original - current_price)
            if direction == "BUY":
                scale_tp = round(current_price + remaining_to_tp3 * 1.5, 5)
            else:
                scale_tp = round(current_price - remaining_to_tp3 * 1.5, 5)
        else:
            orig_risk = abs(open_price - current_sl) if current_sl else 0
            if direction == "BUY":
                scale_tp = round(current_price + orig_risk * 3, 5)
            else:
                scale_tp = round(current_price - orig_risk * 3, 5)

        log.info(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCALE-IN TRIGGERED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Symbol:     {symbol}  {direction}
Parent:     Ticket {ticket_str} | In profit {profit_r:.1f}R
Push phase: {push.get('signal', 'confirmed')}
Scale lots: {scale_vol} (50% of original {original_vol})
Entry:      {scale_entry:.5g}  (market)
SL:         {scale_sl:.5g}    (already at BE/better)
TP:         {scale_tp:.5g}    (1.5× remaining to TP3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

        order_type = "BUY" if direction == "BUY" else "SELL"
        comment    = f"OMNI_SCALE_{ticket_str}"
        result     = place_order(symbol, direction, order_type,
                                 scale_entry, scale_sl, scale_tp, scale_vol, comment)
        log.info(f"Scale-in order result: {result}")

        if result.startswith("OK") or result.startswith("PAPER"):
            state.active_trades[ticket_str]["scale_in_count"] = add_count + 1
            state.active_trades[ticket_str]["scaled_in"] = True  # backwards compat
            parts = result.split("|")
            if result.startswith("OK") and len(parts) > 1 and parts[1].isdigit():
                scale_ticket = parts[1]
            else:
                scale_ticket = f"SCALE_{symbol}_{int(time.time())}"

            state.active_trades[scale_ticket] = {
                "symbol":          symbol,
                "direction":       direction,
                "entry":           scale_entry,
                "tp1":             scale_tp,
                "tp2":             scale_tp,
                "tp3":             scale_tp,
                "volume_original": scale_vol,
                "tp1_taken":       False,
                "tp2_taken":       False,
                "last_profit":     0.0,
                "is_scale_in":     True,
                "parent_ticket":   ticket_str,
            }

            # Log to trade memory
            if memory:
                try:
                    memory.log_entry(
                        symbol=symbol, direction=direction,
                        entry_type="SCALE_IN",
                        entry_price=scale_entry,
                        sl_price=scale_sl,
                        tp1_price=scale_tp, tp2_price=scale_tp, tp3_price=scale_tp,
                        confidence=75,
                        reasons=[
                            f"Scale-in: {profit_r:.1f}R profit on parent trade",
                            f"Push momentum confirmed: {push.get('signal','')}",
                            f"H1 structure: {h4_struct}",
                        ],
                        session=data.get("session", ""),
                        amd_phase=data.get("amd_phase", ""),
                        lot_size=scale_vol,
                        rr_ratio=0,
                        is_scale_in=True,
                        parent_trade_id=ticket_str,
                    )
                except Exception as e:
                    log.warning(f"Memory scale-in log error: {e}")


# ── Re-entry System ──────────────────────────────────────────────────────────
def check_pending_reentries(state: TraderState, data: dict, equity: float):
    """
    After a trailing SL is hit in profit, re-enter the same setup at the
    original (or better) entry price.  Better entry = same SL → improved RR.

    Queued entries expire after 1 hour if price never pulls back.
    """
    if not state.pending_reentries:
        return

    positions = get_open_positions(data)
    charts    = data.get("charts", {})
    prices    = {p["symbol"]: p for p in data.get("prices", [])}
    now       = time.time()
    expired   = []

    for symbol, reentry in list(state.pending_reentries.items()):
        if now > reentry.get("valid_until", 0):
            expired.append(symbol)
            log.info(f"{symbol} Re-entry expired (1h window passed)")
            continue

        # Skip if symbol already has an open position or we're at trade cap
        if any(p.get("symbol") == symbol for p in positions):
            continue
        if len(positions) >= MAX_OPEN_TRADES:
            break

        direction       = reentry["direction"]
        original_entry  = reentry["entry"]
        sl              = reentry["sl"]
        if original_entry == 0 or sl == 0:
            expired.append(symbol)
            continue

        sym_info = charts.get(symbol, {})
        pip_size = sym_info.get("point", 0.0001) * 10
        price_info = prices.get(symbol, {})
        # Use bid for BUY (we're buying), ask for SELL
        current_price = price_info.get("bid", 0) if direction == "BUY" else price_info.get("ask", 0)
        if current_price == 0:
            continue

        # Only re-enter when price has pulled back to within 5 pips of original entry
        # (for BUY: price must be <= original_entry + 5 pips;
        #  for SELL: price must be >= original_entry - 5 pips)
        tolerance = pip_size * 5
        if direction == "BUY":
            if current_price > original_entry + tolerance:
                continue  # Price hasn't pulled back yet
            entry      = min(current_price, original_entry)
            order_type = "BUY" if current_price <= original_entry else "BUY_LIMIT"
        else:
            if current_price < original_entry - tolerance:
                continue
            entry      = max(current_price, original_entry)
            order_type = "SELL" if current_price >= original_entry else "SELL_LIMIT"

        # Validate SL is still on the correct side of entry
        if direction == "BUY" and sl >= entry:
            expired.append(symbol)
            continue
        if direction == "SELL" and sl <= entry:
            expired.append(symbol)
            continue

        lot_size = calculate_lot_size(
            equity=equity,
            risk_pct=state.current_risk_pct,
            entry=entry, sl=sl,
            symbol=symbol, sym_info=sym_info,
        )
        if lot_size <= 0:
            continue

        tp = reentry.get("tp3") or reentry.get("tp2") or reentry.get("tp1")
        comment = f"OMNI_REENTRY_{reentry.get('entry_type', 'ICT')}"

        log.info(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RE-ENTRY (stopped-in-profit recovery)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Symbol:    {symbol}  {direction}
Original:  entry {original_entry:.5f}  SL {sl:.5f}
Re-entry:  entry {entry:.5f}  SL {sl:.5f}  (improved RR)
Lots:      {lot_size}  |  parent banked: ${reentry.get('profit_from_parent', 0):.2f}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

        result = place_order(symbol, direction, order_type, entry, sl, tp, lot_size, comment)
        log.info(f"Re-entry order result: {result}")

        if result.startswith("OK") or result.startswith("PAPER"):
            parts = result.split("|")
            ticket_key = (
                parts[1]
                if (result.startswith("OK") and len(parts) > 1 and parts[1].isdigit())
                else f"REENTRY_{symbol}_{int(time.time())}"
            )
            state.active_trades[ticket_key] = {
                "symbol":          symbol,
                "direction":       direction,
                "entry":           entry,
                "sl":              sl,
                "tp1":             reentry.get("tp1", 0),
                "tp2":             reentry.get("tp2", 0),
                "tp3":             reentry.get("tp3", 0),
                "volume_original": lot_size,
                "tp1_taken":       False,
                "tp2_taken":       False,
                "scaled_in":       False,
                "last_profit":     0.0,
                "memory_trade_id": None,
                "entry_type":      f"REENTRY_{reentry.get('entry_type', 'ICT')}",
            }
            expired.append(symbol)

    for sym in expired:
        state.pending_reentries.pop(sym, None)


# ── Setup Executor ────────────────────────────────────────────────────────────
def execute_setup(setup, equity: float, state: TraderState, sym_info: dict,
                  memory=None) -> bool:
    """Execute an ICT setup with proper position sizing and TP level storage."""
    symbol    = setup.symbol
    direction = setup.direction
    entry     = setup.entry_price
    sl        = setup.sl_price
    # Use TP3 as the order's hard TP so MT5 keeps the full position open.
    # The bot manages partial exits at TP1 (50%) and TP2 (30%) via close_position;
    # the remaining 20% runner auto-closes when price reaches TP3.
    # Fallback chain: tp3 → tp2 → tp1 (in case scanner only provides tp1)
    tp        = setup.tp3_price or setup.tp2_price or setup.tp1_price
    session   = getattr(setup, "session", "")

    # Session streak gate — after 3 back-to-back losses in this session, require higher confidence
    _sess_losses = state.session_loss_streak.get(session, 0)
    if _sess_losses >= 3:
        log.debug(f"{symbol}: session {session} has {_sess_losses} consecutive losses — raising confidence bar")

    # Session-aware confidence threshold — Asia requires higher bar; streak penalty adds 10 pts per tier above 3
    # _live_min_conf reads learned_parameters.json (regime-aware) and never undercuts the FREQ floor.
    effective_min_conf = _live_min_conf(session)
    effective_min_conf = min(85, effective_min_conf + max(0, (_sess_losses - 2) * 10))
    if setup.confidence < effective_min_conf:
        log.debug(f"{symbol}: confidence {setup.confidence} below {session} threshold {effective_min_conf}")
        return False

    # ACCUMULATION (Asia) phase gate — only sweep-based entries allowed
    # ICT: Asia builds the range; London manipulates it; NY distributes.
    # Non-sweep entries during accumulation are counter to market maker intent.
    # EXCEPTION: if SCALP_ENABLED is set, high-conf setups can run as scalps
    # with reduced risk + tight TP/SL (see scalp_engine).
    _amd_gate = getattr(setup, "amd_phase", "")
    setup.is_scalp = False  # Mark scalps for downstream sizing/logging
    if _amd_gate == "ACCUMULATION" and "SWEEP" not in setup.entry_type:
        if _SCALP_AVAILABLE and SCALP_ENABLED_PATH.exists():
            try:
                # Estimate ATR proxy from setup's SL distance
                _atr_proxy = abs(setup.entry_price - setup.sl_price)
                # Count today's scalps in same session
                _session_now = getattr(setup, "session", "")
                _scalps_today = sum(
                    1 for r in (state.recent_closes or [])
                    if r.get("session") == _session_now and "SCALP" in str(r.get("exit_level",""))
                )
                _decision = evaluate_accumulation_scalp(
                    setup, state, atr=_atr_proxy,
                    params=_scalp_params(),
                    current_phase=_amd_gate,
                    scalps_taken_this_session=_scalps_today,
                )
                if _decision.accept:
                    setup.is_scalp = True
                    setup.scalp_mode = _decision.mode
                    setup.scalp_risk_pct = _decision.risk_pct
                    setup.scalp_tp_pips = _decision.tp_pips
                    setup.scalp_sl_pips = _decision.sl_pips
                    log.info(f"{symbol}: SCALP ACCEPT during {_amd_gate} — {_decision.reason}")
                    # Override SL/TP with scalp-tight values (preserve direction)
                    _pip_size = sym_info.get("point") or (
                        0.01  if ("JPY" in symbol or "XAU" in symbol or "XAG" in symbol)
                        else 0.0001
                    )
                    if direction == "BUY":
                        setup.sl_price = setup.entry_price - (_decision.sl_pips * _pip_size)
                        setup.tp1_price = setup.entry_price + (_decision.tp_pips * _pip_size * 0.5)
                        setup.tp2_price = setup.entry_price + (_decision.tp_pips * _pip_size * 0.8)
                        setup.tp3_price = setup.entry_price + (_decision.tp_pips * _pip_size)
                    else:
                        setup.sl_price = setup.entry_price + (_decision.sl_pips * _pip_size)
                        setup.tp1_price = setup.entry_price - (_decision.tp_pips * _pip_size * 0.5)
                        setup.tp2_price = setup.entry_price - (_decision.tp_pips * _pip_size * 0.8)
                        setup.tp3_price = setup.entry_price - (_decision.tp_pips * _pip_size)
                    sl = setup.sl_price
                    tp = setup.tp3_price
                else:
                    log.debug(f"{symbol}: scalp rejected — {_decision.reason}")
                    return False
            except Exception as _e:
                log.warning(f"{symbol}: scalp eval error: {_e} — falling back to standard block")
                return False
        else:
            log.debug(f"{symbol}: ACCUMULATION phase — blocking {setup.entry_type} "
                      f"(only SWEEP entries allowed; enable /scalp to override)")
            return False

    # D1 bias alignment gate — backtest showed bias-aligned trades: 77.8% WR vs 32% overall.
    # Block counter-bias sweep entries below confidence 72 (only A+ setups override bias).
    _d1_bias = getattr(setup, "tf_bias", "NEUTRAL")
    _dir = getattr(setup, "direction", "")
    _bias_opposed = (
        (_dir == "SELL" and _d1_bias == "BULLISH") or
        (_dir == "BUY"  and _d1_bias == "BEARISH")
    )
    if _bias_opposed and setup.confidence < 72:
        log.info(f"{symbol}: Counter-D1-bias ({_d1_bias}) {_dir} at confidence {setup.confidence} — blocked (need ≥72)")
        return False

    # Friday close block — no new positions after 15:00 UTC Friday (weekend gap risk)
    _now_utc = datetime.now(timezone.utc)
    if _now_utc.weekday() == 4 and _now_utc.hour >= 15:
        log.debug(f"{symbol}: Friday 15:00+ UTC — blocking new entries (weekend gap risk)")
        return False

    # Per-symbol consecutive loss guard — pause after 2 back-to-back losses on same symbol
    _sym_losses = state.sym_loss_streak.get(symbol, 0)
    if _sym_losses >= 2:
        log.debug(f"{symbol}: {_sym_losses} consecutive losses today — symbol paused")
        return False

    # Apply adaptive confidence from trade memory
    _setup_swept = getattr(setup, "liq_swept", False)
    if memory:
        try:
            patterns = [r for r in setup.reasons if "Pattern:" in r]
            pattern_names = [p.replace("Pattern:", "").strip().split()[0] for p in patterns]
            adj = memory.get_confidence_adjustment(
                entry_type=setup.entry_type,
                session=session,
                patterns=pattern_names,
                amd_phase=getattr(setup, "amd_phase", ""),
                sweep_confirmed=_setup_swept,
            )
            adj_confidence = setup.confidence + adj
            if adj != 0:
                log.info(f"{symbol}: Memory adj {adj:+d} → effective confidence {adj_confidence}/100")
            # Apply adjusted threshold check
            if adj_confidence < effective_min_conf - 5:
                log.info(f"{symbol}: Memory-adjusted confidence {adj_confidence} too low, skipping")
                return False
        except Exception as e:
            log.warning(f"Memory confidence adj error: {e}")
            adj_confidence = setup.confidence
    else:
        adj_confidence = setup.confidence

    # Pattern recognition model boost/penalty
    if _PATTERN_AVAILABLE and _PATTERN_MODEL is not None:
        try:
            _pfeat = {
                "confidence": float(setup.confidence),
                "rr_ratio": float(getattr(setup, "rr_ratio", 0) or 0),
                "ict_features.sweep_confirmed": 1.0 if _setup_swept else 0.0,
            }
            _p_win = _PATTERN_MODEL.predict_proba(_pfeat)
            _padj = max(-8, min(8, int(round((_p_win - 0.5) * 16))))
            if abs(_padj) >= 2:
                adj_confidence += _padj
                log.info(f"{symbol}: Pattern model P(WIN)={_p_win:.2f} → adj {_padj:+d} → {adj_confidence}")
        except Exception as _pe:
            log.debug(f"Pattern model error: {_pe}")

    # Symbol-specific confidence override (from rules.json symbol_overrides)
    _rules_sym = load_rules().get("symbol_overrides", {}).get(symbol, {})
    _sym_min_conf = _rules_sym.get("min_confidence", 0)
    if _sym_min_conf > 0 and adj_confidence < _sym_min_conf:
        log.info(f"{symbol}: confidence {adj_confidence} below symbol override {_sym_min_conf} — skipped")
        return False

    # Skip if already traded this symbol recently (time-based TTL — 2 h default)
    _now = time.time()
    if isinstance(state.recently_traded, dict):
        # Expire stale entries in-place
        expired = [s for s, exp in state.recently_traded.items() if _now >= exp]
        for s in expired:
            del state.recently_traded[s]
        if symbol in state.recently_traded:
            _remaining = int(state.recently_traded[symbol] - _now)
            log.debug(f"{symbol} recently traded — cooldown {_remaining}s remaining, skipping")
            return False
    elif symbol in state.recently_traded:  # legacy list fallback
        log.debug(f"{symbol} recently traded, skipping")
        return False

    # Minimum SL distance guard: rejects setups where SL would be eaten by spread
    pip_size    = sym_info.get("point", 0.0001) * 10
    sl_distance = abs(entry - sl)
    if sl_distance < MIN_SL_PIPS * pip_size:
        log.debug(f"{symbol}: SL too tight ({sl_distance:.5g} < {MIN_SL_PIPS} pips), skipping")
        return False

    # ── Spread guard — skip trade if spread is abnormally wide ────────────
    # Spread in the prices array is in points; convert to pips for comparison.
    live_spread_pts = sym_info.get("spread", 0)
    # spread field from OmniExport is in points (integer ticks); 1 pip = 10 points for FX
    point = sym_info.get("point", 0.0001)
    if live_spread_pts > 0:
        if "XAU" in symbol or "GOLD" in symbol:
            max_spread = cfg.MAX_SPREAD_XAUUSD_PIPS
        elif "XAG" in symbol or "SILVER" in symbol:
            max_spread = cfg.MAX_SPREAD_XAUUSD_PIPS  # Silver spreads similar width to gold
        elif any(idx in symbol for idx in ("US30", "NAS", "DAX", "SPX", "GER", "UK100")):
            max_spread = cfg.MAX_SPREAD_INDEX_PIPS
        else:
            max_spread = cfg.MAX_SPREAD_FOREX_PIPS
        spread_in_pips = live_spread_pts * point / pip_size if pip_size > 0 else live_spread_pts
        if spread_in_pips > max_spread:
            log.info(f"{symbol}: spread {spread_in_pips:.1f} pips > max {max_spread} — skipping (wide spread)")
            return False

    # Calculate lot size with compounding
    if not sym_info:
        log.warning(f"{symbol}: sym_info missing from charts — position sizing using minimum lot")
    lot_size = calculate_lot_size(
        equity=equity,
        risk_pct=state.current_risk_pct,
        entry=entry, sl=sl,
        symbol=symbol, sym_info=sym_info
    )

    if lot_size <= 0:
        log.warning(f"{symbol}: lot size calculation failed")
        return False

    # Determine order type (limit vs market)
    current_price = sym_info.get("bid", entry)
    if direction == "BUY":
        order_type = "BUY_LIMIT" if entry < current_price * 0.999 else "BUY"
    else:
        order_type = "SELL_LIMIT" if entry > current_price * 1.001 else "SELL"

    risk_usd = equity * state.current_risk_pct / 100
    comment  = f"OMNI_{setup.entry_type}_{adj_confidence}"

    # ── Full detailed pre-trade log ────────────────────────────────
    log.info(f"""
╔══════════════════════════════════════════════════════════════════╗
║  ICT SETUP EXECUTING
╠══════════════════════════════════════════════════════════════════╣
║  Symbol:     {symbol:<10}  Direction: {direction}
║  Type:       {setup.entry_type}
║  Confidence: {setup.confidence}/100 raw  |  {adj_confidence}/100 memory-adjusted
║  RR Ratio:   {setup.rr_ratio}:1  (min required: {MIN_RR}:1)
╠══════════════════════════════════════════════════════════════════╣
║  ENTRY LEVELS:
║    Entry:  {entry:.5g}  ({order_type})
║    SL:     {sl:.5g}  ({sl_distance / pip_size:.1f} pips from entry)
║    TP1:    {setup.tp1_price:.5g}  (50% — first exit)
║    TP2:    {setup.tp2_price:.5g}  (30% — trail to 1R)
║    TP3:    {setup.tp3_price:.5g}  (20% — runner, let it run)
║    Inval:  {getattr(setup, 'invalidation', 0):.5g}  (setup is void if price hits this)
╠══════════════════════════════════════════════════════════════════╣
║  POSITION:
║    Lots:   {lot_size}  |  Risk: {state.current_risk_pct:.2f}%  =  ${risk_usd:.2f}
║    Equity: ${equity:,.2f}  |  Win streak: {state.win_streak}  Loss streak: {state.loss_streak}
╠══════════════════════════════════════════════════════════════════╣
║  CONTEXT:
║    Session:   {setup.session}  |  AMD Phase: {setup.amd_phase}
║    D1 Bias:   {setup.tf_bias}
╠══════════════════════════════════════════════════════════════════╣
║  CONFLUENCE REASONS ({len(setup.reasons)} factors):
{chr(10).join('║    ' + str(i+1).rjust(2) + '. ' + r for i, r in enumerate(setup.reasons))}
╚══════════════════════════════════════════════════════════════════╝
""")

    result = place_order(symbol, direction, order_type, entry, sl, tp, lot_size, comment)
    log.info(f"Order result: {result}")

    # On EA error/timeout — try to rescue a late-placed order by scanning MT5 positions
    if result.startswith("ERROR") or result.startswith("TIMEOUT"):
        # MetaQuotes demo can respond after our poll window. Check if a new position
        # for this symbol appeared in MT5 within the last 30 seconds.
        _rescued = False
        try:
            _live_pos = {str(p["ticket"]): p for p in get_open_positions(data)}
            _tracked  = set(state.active_trades.keys())
            for _tk, _pos in _live_pos.items():
                if _tk in _tracked:
                    continue
                if _pos.get("symbol") != symbol:
                    continue
                # New untracked position on this symbol — claim it
                log.info(f"{symbol}: rescued late-placed order ticket={_tk} from MT5")
                state.active_trades[_tk] = {
                    "symbol":          symbol,
                    "direction":       direction,
                    "entry":           _pos.get("open_price", entry),
                    "sl":              _pos.get("sl", sl),
                    "current_sl":      _pos.get("sl", sl),
                    "tp1":             setup.tp1_price,
                    "tp2":             setup.tp2_price,
                    "tp3":             setup.tp3_price,
                    "volume_original": lot_size,
                    "tp1_taken":       False,
                    "tp2_taken":       False,
                    "scaled_in":       False,
                    "last_profit":     0.0,
                    "memory_trade_id": None,
                    "entry_type":      setup.entry_type,
                    "order_type":      order_type,
                    "entry_ts":        time.time(),
                    "session":         session,
                    "entry_spread":    0.0,
                }
                _freq_floors = {"AGGRESSIVE": 1800, "NORMAL": 3600, "CONSERVATIVE": 7200}
                if isinstance(state.recently_traded, dict):
                    state.recently_traded[symbol] = time.time() + _freq_floors.get(_FREQ_MODE, 3600)
                _rescued = True
                send_telegram(
                    f"📈 <b>NEW TRADE [LIVE] — {symbol} {direction}</b> (rescued)\n"
                    f"Entry: <b>{_pos.get('open_price', entry):.5g}</b> | SL: <b>{_pos.get('sl', sl):.5g}</b>\n"
                    f"Lots: <b>{lot_size}</b> | Ticket: <code>{_tk}</code>"
                )
                break
        except Exception as _re:
            log.debug(f"Late-order rescue failed: {_re}")
        if _rescued:
            return True
        log.warning(f"{symbol}: order rejected ({result}) — 60 s backoff")
        if isinstance(state.recently_traded, dict):
            state.recently_traded[symbol] = time.time() + 60
        return False

    if result.startswith("OK") or result.startswith("PAPER"):
        parts = result.split("|")
        if result.startswith("OK") and len(parts) > 1 and parts[1].isdigit():
            ticket_key = parts[1]
        else:
            ticket_key = f"PAPER_{symbol}_{int(time.time())}"

        # Store TP levels, metadata, and memory trade_id
        trade_id_mem = None
        if memory:
            try:
                trade_id_mem = memory.log_entry(
                    symbol=symbol,
                    direction=direction,
                    entry_type=setup.entry_type,
                    entry_price=entry,
                    sl_price=sl,
                    tp1_price=setup.tp1_price,
                    tp2_price=setup.tp2_price,
                    tp3_price=setup.tp3_price,
                    confidence=setup.confidence,
                    reasons=setup.reasons,
                    session=setup.session,
                    amd_phase=setup.amd_phase,
                    d1_bias=setup.tf_bias,
                    rr_ratio=setup.rr_ratio,
                    lot_size=lot_size,
                    risk_pct=state.current_risk_pct,
                    risk_usd=risk_usd,
                    invalidation=getattr(setup, "invalidation", 0),
                    adj_confidence=adj_confidence,
                    sweep_confirmed=_setup_swept,
                )
            except Exception as e:
                log.warning(f"Memory log_entry error: {e}")

        _current_spread = live_spread_pts * point if live_spread_pts > 0 else 0.0

        state.active_trades[ticket_key] = {
            "symbol":          symbol,
            "direction":       direction,
            "entry":           entry,
            "sl":              sl,           # ← stored for accurate R-multiple calculation
            "tp1":             setup.tp1_price,
            "tp2":             setup.tp2_price,
            "tp3":             setup.tp3_price,
            "volume_original": lot_size,
            "tp1_taken":       False,
            "tp2_taken":       False,
            "scaled_in":       False,
            "last_profit":     0.0,
            "memory_trade_id": trade_id_mem,
            "entry_type":      setup.entry_type,
            "order_type":      order_type,   # stored so stale-limit checker can identify pending orders
            "entry_ts":        time.time(),  # timestamp for early-exit timer
            "session":         session,      # session at entry — used for session loss streak tracking
            "entry_spread":    _current_spread,  # spread at time of order placement
        }

        # ── Phase 2f: persist opened-trade features for the learning loop ─────
        # record_outcome at close UPDATES this row; without record_signal here it would be a no-op.
        try:
            _record_signal_open(
                ticket_key=ticket_key,
                setup=setup,
                lot_size=lot_size,
                sl=sl,
                adj_confidence=adj_confidence,
                regime=_load_regime(),
            )
        except Exception as _e:
            log.debug(f"record_signal hook error: {_e}")

        # Mark symbol as recently traded — cooldown scales with freq mode
        _freq_floors = {"AGGRESSIVE": 1800, "NORMAL": 3600, "CONSERVATIVE": 7200}
        _cooldown_secs = _freq_floors.get(_FREQ_MODE, 3600)
        if isinstance(state.recently_traded, dict):
            state.recently_traded[symbol] = time.time() + _cooldown_secs
        else:
            state.recently_traded = {symbol: time.time() + _cooldown_secs}

        _mode_tag = "PAPER" if PAPER_MODE else "LIVE"
        send_telegram(
            f"{'📄' if PAPER_MODE else '📈'} <b>NEW TRADE [{_mode_tag}] — {symbol} {direction}</b>\n"
            f"Entry: <b>{entry:.5g}</b> | SL: <b>{sl:.5g}</b>\n"
            f"TP1: {setup.tp1_price:.5g} | TP2: {setup.tp2_price:.5g} | TP3: {setup.tp3_price:.5g}\n"
            f"Lots: <b>{lot_size}</b> | Risk: <b>${risk_usd:.2f}</b> ({state.current_risk_pct:.2f}%)\n"
            f"Grade: <b>{getattr(setup,'grade','?')}</b> | Conf: <b>{setup.confidence}/100</b> | RR: <b>{setup.rr_ratio:.1f}:1</b>\n"
            f"Session: {session} | Type: {setup.entry_type} | Phase: {getattr(setup,'amd_phase','?')}"
        )
        return True

    return False


# ── Print Status ──────────────────────────────────────────────────────────────
def print_status(state: TraderState, data: dict, setups: list):
    account   = get_account(data)
    equity    = account.get("equity", 0)
    balance   = account.get("balance", 0)
    profit    = account.get("profit", 0)
    session   = data.get("session", "—")
    amd       = data.get("amd_phase", "—")
    positions = get_open_positions(data)
    win_rate  = (state.winning_trades / state.total_trades * 100) if state.total_trades > 0 else 0
    dd_pct    = state.peak_drawdown
    in_filter = SESSION_FILTER is None or session in SESSION_FILTER

    mode_str = f"RISK:{_RISK_MODE} | FREQ:{_FREQ_MODE}"
    print(f"""
╔══════════════════════════════════════════════════╗
║  OMNI ICT AUTO-TRADER  {'[PAPER]' if PAPER_MODE else '[LIVE]':>8}  {mode_str:<17}  ║
╠══════════════════════════════════════════════════╣
║  Balance:  ${balance:>10,.2f}    Equity:  ${equity:>10,.2f}  ║
║  P&L:      ${profit:>+10,.2f}    Risk:    {state.current_risk_pct:>8.2f}%  ║
║  Session:  {session:<10}    Phase:   {amd:<16}  ║
╠══════════════════════════════════════════════════╣
║  Trades:   {state.total_trades:<5} W:{state.winning_trades} L:{state.losing_trades} WR:{win_rate:.0f}%               ║
║  Profit:   ${state.total_profit:>+10,.2f}    Streak: {'W'+str(state.win_streak) if state.win_streak>0 else 'L'+str(state.loss_streak):<7}        ║
║  Open:     {len(positions):<3} / {MAX_OPEN_TRADES} max    MaxDD: {dd_pct:.1f}% / {MAX_DD_FROM_PEAK:.0f}%           ║
║  Setups:   {len(setups):<3} detected  Filter: {'ACTIVE' if in_filter else 'SESSION WAIT'}                ║
║  Params:   RR≥{MIN_RR}  Conf≥{MIN_CONFIDENCE}  SL≥{MIN_SL_PIPS}p  DailyLim:{DAILY_LOSS_LIMIT}%        ║
╚══════════════════════════════════════════════════╝
""")

    if state.trading_halted:
        print(f"  TRADING HALTED: {state.halt_reason}")

    if setups:
        print("  Top setups:")
        for s in setups[:3]:
            marker = "BUY" if s.direction == "BUY" else "SELL"
            print(f"  [{marker}] {s.symbol:<10} @ {s.entry_price:.5g}  "
                  f"SL:{s.sl_price:.5g}  Conf:{s.confidence}/100  RR:{s.rr_ratio}")
        print()


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║               OMNI ICT AUTO-TRADER STARTING                  ║
╠══════════════════════════════════════════════════════════════╣
║  Trading:  {'PAPER (simulated — no real money)' if PAPER_MODE else '⚠  LIVE TRADING — REAL MONEY'}
║
║  ── RISK MODE: {_RISK_MODE:<10} ─────────────────────────────────────
║  {RISK.description}
║  Base risk: {BASE_RISK_PCT}%  |  Min: {MIN_RISK_PCT}%  |  Max: {MAX_RISK_PCT}%  (compounding)
║  Daily loss limit: {DAILY_LOSS_LIMIT}%  |  Max drawdown: {MAX_DD_FROM_PEAK}% from peak
║
║  ── FREQUENCY MODE: {_FREQ_MODE:<10} ─────────────────────────────
║  {FREQ.description}
║  Min confidence: {MIN_CONFIDENCE} ({MIN_CONFIDENCE_ASIA} Asia)  |  Min RR: {MIN_RR}:1
║  Max open trades: {MAX_OPEN_TRADES}  |  Zone entries: {'YES' if FREQ.allow_zone_entries else 'NO'}
║  Allowed grades: {', '.join(FREQ.allowed_grades)} {'(+all in paper)' if PAPER_MODE else ''}
║
║  Symbols:  {', '.join(TRADE_SYMBOLS[:5])}{'...' if len(TRADE_SYMBOLS)>5 else ''}
║  Sessions: {', '.join(SESSION_FILTER) if SESSION_FILTER else 'All (London, NY, Asia)'}
╚══════════════════════════════════════════════════════════════╝

  To change modes:
    python auto_trader.py --risk LOW --freq CONSERVATIVE
    python auto_trader.py --risk HIGH --freq AGGRESSIVE
    OMNI_RISK_MODE=MODERATE OMNI_FREQ_MODE=NORMAL python auto_trader.py
""")

    if not PAPER_MODE:
        print(_msg("auto.live_warning"))
        for i in range(10, 0, -1):
            print(f"   Starting in {i}...", end="\r")
            time.sleep(1)
        print()

    # Import ICT precision scanner
    try:
        import ict_precision as ict
        log.info(_msg("auto.module_loaded", name="ICT Precision"))
    except ImportError as e:
        log.error(_msg("auto.module_import_failed", name="ict_precision", error=e))
        log.error("请确认 ict_precision.py 位于同一目录。")
        sys.exit(1)

    # Import dual-TF selector (enabled via rules.json dual_tf.enabled)
    _dual_tf_mod = None
    try:
        import dual_tf_selector as _dual_tf_mod
        log.info(_msg("auto.module_loaded", name="Dual-TF selector"))
    except ImportError as e:
        log.warning(_msg("auto.module_import_failed", name="dual_tf_selector", error=e))

    # Import trade memory / AI learning engine
    try:
        from trade_memory import get_memory
        memory = get_memory()
        log.info("交易记忆已加载：%d 条历史交易。", len(memory.trades))
        if memory.trades:
            log.info("\n" + memory.get_performance_report())
    except ImportError as e:
        log.warning("交易记忆模块不可用：%s，将不使用 AI 记忆继续运行。", e)
        memory = None

    state = load_state()
    if not state.start_time:
        state.start_time = datetime.now().isoformat()

    log.info(_msg("auto.started", paper=PAPER_MODE, risk_mode=_RISK_MODE, freq_mode=_FREQ_MODE))
    log.info(_msg("auto.risk_summary", base_risk=BASE_RISK_PCT, max_risk=MAX_RISK_PCT,
                  confidence=MIN_CONFIDENCE, rr=MIN_RR, max_open=MAX_OPEN_TRADES))
    log.info(_msg("auto.state_loaded", trades=state.total_trades, profit=state.total_profit))
    send_telegram(
        f"{'📄' if PAPER_MODE else '🚀'} <b>OMNI-ICT Bot {'[PAPER]' if PAPER_MODE else '[LIVE]'} Started</b>\n"
        f"Risk: <b>{_RISK_MODE}</b> ({BASE_RISK_PCT}%–{MAX_RISK_PCT}%) | Freq: <b>{_FREQ_MODE}</b>\n"
        f"Conf≥{MIN_CONFIDENCE} | RR≥{MIN_RR} | MaxOpen={MAX_OPEN_TRADES} | DD limit: {MAX_DD_FROM_PEAK:.0f}%\n"
        f"Symbols: {', '.join(TRADE_SYMBOLS[:6])}{'…' if len(TRADE_SYMBOLS) > 6 else ''}\n"
        f"Prior history: {state.total_trades} trades | P&amp;L: ${state.total_profit:+,.2f}"
    )

    # Write active modes into state so dashboard can display them
    state_dict_extra = {
        "risk_mode":  _RISK_MODE,
        "freq_mode":  _FREQ_MODE,
        "base_risk":  BASE_RISK_PCT,
        "max_risk":   MAX_RISK_PCT,
        "min_risk":   MIN_RISK_PCT,
        "min_conf":   MIN_CONFIDENCE,
        "min_rr":     MIN_RR,
        "max_open":   MAX_OPEN_TRADES,
        "daily_limit":DAILY_LOSS_LIMIT,
        "max_dd":     MAX_DD_FROM_PEAK,
    }

    # ── Startup reconciliation: sync active_trades with real MT5 positions ─
    # 1. Purge tracked tickets no longer open in MT5.
    # 2. Register live MT5 positions not yet in active_trades.
    # 3. Back-fill TP ladder for reconciled positions that lack it.
    _startup_data = load_mt5_data()
    if _startup_data:
        _live_positions = _startup_data.get("positions", [])
        _live_tickets   = {str(p.get("ticket")): p for p in _live_positions}

        # 1. Remove stale
        _stale = [t for t in list(state.active_trades.keys())
                  if not t.startswith("PAPER_") and t not in _live_tickets]
        if _stale:
            log.warning(
                f"Reconciliation: removing {len(_stale)} stale trades "
                f"no longer open in MT5: {_stale}"
            )
            for t in _stale:
                state.active_trades.pop(t, None)

        # 2 & 3. Register untracked live positions; back-fill missing TP ladders
        for tid, pos in _live_tickets.items():
            if tid not in state.active_trades:
                state.active_trades[tid] = {}
                log.info(f"Reconciliation: registered untracked MT5 position {tid} ({pos.get('symbol')})")
                # Infer tp1_taken: if live volume is <90% of the typical min lot it was
                # likely partial-closed at TP1 before crash — mark it to skip re-close
                live_vol = float(pos.get("volume", 0) or 0)
                if live_vol > 0 and live_vol < 0.09:
                    state.active_trades[tid]["tp1_taken"] = True
                    log.info(f"Reconciliation: inferred tp1_taken for {tid} (vol={live_vol:.2f} suggests partial close)")

            entry  = float(pos.get("open_price", 0) or 0)
            sl     = float(pos.get("sl", 0) or 0)
            mt5_tp = float(pos.get("tp", 0) or 0)
            trade  = state.active_trades[tid]

            # Back-fill TP ladder if not already set
            if not trade.get("tp1") and entry > 0 and sl > 0 and mt5_tp > 0:
                risk = abs(entry - sl)
                pos_type = pos.get("type", "").upper()
                direction = 1 if pos_type == "BUY" else -1
                # Estimate TP1/TP2 from MT5's TP (treat it as TP3)
                tp3 = mt5_tp
                tp1 = entry + direction * risk * 1.5
                tp2 = entry + direction * risk * 2.5
                trade["tp1"] = round(tp1, 5)
                trade["tp2"] = round(tp2, 5)
                trade["tp3"] = round(tp3, 5)
                log.info(f"Reconciliation: back-filled TP ladder for {tid} "
                         f"TP1={tp1:.5f} TP2={tp2:.5f} TP3={tp3:.5f}")

        _tracked = len(state.active_trades)
        _live    = len(_live_tickets)
        log.info(f"Reconciliation complete: {_tracked} tracked / {_live} live MT5 positions")
        save_state(state)
    else:
        log.warning("Could not load MT5 data at startup — skipping reconciliation")

    scan_count       = 0
    data_fail_count  = 0
    _last_stale_log  = 0.0   # throttle: only log stale once per 60s
    _last_nodata_log = 0.0

    while True:
        try:
            scan_count += 1
            state.last_scan = datetime.now().isoformat()

            # ── Weekend guard — forex closes Fri 22:00 UTC, opens Sun 22:00 UTC ──
            _utc_now = datetime.now(timezone.utc)
            _dow = _utc_now.weekday()  # 0=Mon … 6=Sun
            _is_weekend = (_dow == 5) or (_dow == 6 and _utc_now.hour < 22)
            if _is_weekend:
                if _utc_now.minute == 0:  # log once per hour
                    log.info("外汇周末休市，等待至周日 22:00 UTC 后恢复扫描。")
                time.sleep(60)
                continue

            # ── Kill switch check ──────────────────────────────────────
            if os.path.exists(KILL_SWITCH_PATH):
                if not state.trading_halted:
                    log.warning(_msg("auto.kill_switch"))
                    state.trading_halted = True
                    state.halt_reason = "Manual kill switch (HALT file)"
                    save_state(state)
                time.sleep(30)
                continue

            # ── Telegram close-trade command ───────────────────────────
            _tg_close = os.path.join(os.path.dirname(STATE_PATH), "tg_close_cmd.txt")
            if os.path.exists(_tg_close):
                try:
                    _tg_ticket = open(_tg_close).read().strip()
                    os.remove(_tg_close)
                    if _tg_ticket and _tg_ticket.isdigit():
                        log.info(f"Telegram close command: ticket #{_tg_ticket}")
                        close_position(int(_tg_ticket), 0)
                        send_telegram(f"❌ <b>Telegram close executed:</b> ticket #{_tg_ticket}")
                    elif _tg_ticket in state.active_trades:
                        log.info(f"Telegram close command: paper ticket {_tg_ticket}")
                        state.active_trades.pop(_tg_ticket, None)
                        send_telegram(f"❌ <b>Telegram close executed:</b> {_tg_ticket}")
                except Exception as _tce:
                    log.warning(f"tg_close_cmd error: {_tce}")

            # ── Load MT5 data ──────────────────────────────────────────
            data = load_mt5_data()
            if not data:
                data_fail_count += 1
                _now = time.time()
                if _now - _last_nodata_log >= 60:
                    log.error(_msg("auto.mt5_missing", seconds=data_fail_count * SCAN_INTERVAL))
                    _last_nodata_log = _now
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Staleness guard — abort if data file is too old ────────
            try:
                _mtime = os.path.getmtime(JSON_PATH) if os.path.exists(JSON_PATH) else 0
                data_age = time.time() - _mtime
                if data_age > cfg.MAX_DATA_AGE_SECS:
                    _now = time.time()
                    if _now - _last_stale_log >= 60:
                        log.error(
                            f"MT5 data stale ({data_age:.0f}s old, max {cfg.MAX_DATA_AGE_SECS}s) — "
                            f"is OmniExport EA running? Trading paused."
                        )
                        _last_stale_log = _now
                    time.sleep(SCAN_INTERVAL)
                    continue
            except OSError:
                pass  # File may not exist yet; already handled above

            data_fail_count = 0

            account   = get_account(data)
            equity    = account.get("equity", 0)
            balance   = account.get("balance", 0)
            positions = get_open_positions(data)

            if equity == 0:
                log.warning("账户净值为 0，请检查 MT5 连接和 OmniExport 数据。")
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Update peak equity ─────────────────────────────────────
            if equity > state.peak_equity:
                state.peak_equity = equity

            # ── Daily reset (exactly once per UTC date) ────────────────
            reset_daily_if_new_day(state, equity)

            # ── Auto-recovery from drawdown halt ───────────────────────
            check_recovery_mode(state, equity, load_rules())

            # ── Daily loss + drawdown check ────────────────────────────
            if not check_daily_limits(state, equity):
                log.warning(f"Trading halted: {state.halt_reason}")
                print_status(state, data, [])
                save_state(state)
                time.sleep(60)
                continue

            # ── Trading disabled check ────────────────────────────────
            if TRADING_DISABLED_PATH.exists():
                log.info("Trading disabled via Telegram")
                print_status(state, data, [])
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Manage existing trades ─────────────────────────────────
            manage_open_trades(state, data, memory=memory)

            # ── Cancel stale pending limit orders ──────────────────────
            try:
                cancel_stale_limits(state, data)
            except Exception as e:
                log.error(f"Stale limit check error: {e}", exc_info=True)

            # ── Session filter ─────────────────────────────────────────
            current_session = data.get("session", "")
            session_ok = SESSION_FILTER is None or current_session in SESSION_FILTER

            if not session_ok and scan_count % 6 == 0:
                log.debug(f"Outside trading session ({current_session}) — monitoring only")

            # ── Open trade gate ────────────────────────────────────────
            open_count = len(positions)
            # SESSION_FILTER=None = all sessions allowed; session-specific
            # confidence thresholds are applied inside execute_setup
            can_trade  = (open_count < MAX_OPEN_TRADES
                          and not state.trading_halted
                          and session_ok)

            # ── Scale-in check on winning open trades ──────────────────
            if len(positions) > 0 and not state.trading_halted:
                try:
                    check_scale_in(state, data, equity, memory=memory)
                except Exception as e:
                    log.error(f"Scale-in check error: {e}", exc_info=True)

            # ── Re-entry check: fill any pending stopped-in-profit entries ─
            if not state.trading_halted and state.pending_reentries:
                try:
                    check_pending_reentries(state, data, equity)
                except Exception as e:
                    log.error(f"Re-entry check error: {e}", exc_info=True)

            # ── Scan for ICT setups ────────────────────────────────────
            all_setups = []
            if can_trade or scan_count % 6 == 0:  # Always scan for display purposes
                try:
                    log.info(f"ICT scan #{scan_count} starting...")
                    for _h in log.handlers: _h.flush()
                    all_setups = ict.scan_all_primary_symbols()
                    log.info(f"ICT scan #{scan_count} complete — {len(all_setups)} setups")

                    # ── Merge dual-TF selector results ────────────────────
                    if _dual_tf_mod:
                        try:
                            data = ict._DATA_CACHE.get("data", {})
                            rules_json = load_rules()
                            dual_tf_cfg = rules_json.get("dual_tf", {})
                            if dual_tf_cfg.get("enabled", False):
                                charts = data.get("charts", {})
                                existing_keys = {(s.symbol, s.direction) for s in all_setups}
                                for sym in cfg.TRADE_SYMBOLS:
                                    sym_data = charts.get(sym, {})
                                    if not sym_data:
                                        continue
                                    try:
                                        from ict_precision import _parse_bars
                                        h1_bars  = _parse_bars(sym_data.get("H1",  []))
                                        m15_bars = _parse_bars(sym_data.get("M15", []))
                                        if not h1_bars or not m15_bars:
                                            continue
                                        sel = _dual_tf_mod.select_trade(h1_bars, m15_bars, rules_json)
                                        if sel.is_actionable:
                                            direction = "BUY" if sel.direction == "BULL" else "SELL"
                                            if (sym, direction) not in existing_keys:
                                                risk_dist = abs((sel.entry_price or 0) - (sel.sl or 0))
                                                tp1 = (sel.entry_price or 0) + (risk_dist * 1.5 if direction == "BUY" else -risk_dist * 1.5)
                                                tp2 = (sel.entry_price or 0) + (risk_dist * 2.5 if direction == "BUY" else -risk_dist * 2.5)
                                                tp3 = (sel.entry_price or 0) + (risk_dist * 4.0 if direction == "BUY" else -risk_dist * 4.0)
                                                conf_int = max(40, min(100, int(sel.confidence * 100)))
                                                dual_setup = ict.ICTSetup(
                                                    symbol=sym,
                                                    direction=direction,
                                                    entry_type=f"DUAL_TF_{sel.entry_type.upper()}",
                                                    entry_price=sel.entry_price or 0,
                                                    sl_price=sel.sl or 0,
                                                    tp1_price=tp1,
                                                    tp2_price=tp2,
                                                    tp3_price=tp3,
                                                    confidence=conf_int,
                                                    reasons=sel.reasons,
                                                    rr_ratio=dual_tf_cfg.get("tp_rr", 2.0),
                                                    ema20=sel.ema20,
                                                    ema200=sel.ema200,
                                                    ema800=sel.ema800,
                                                    tf_emas=sel.tf_emas,
                                                )
                                                all_setups.append(dual_setup)
                                                existing_keys.add((sym, direction))
                                    except Exception as _e:
                                        log.debug(f"dual_tf scan {sym}: {_e}")
                        except Exception as e:
                            log.warning(f"dual_tf merge error: {e}")

                    # ── Apply frequency profile filters ───────────────────
                    # Threshold sources (live-reloaded from learned_parameters.json by optimizer):
                    _live_conf_floor = _live_min_conf("")
                    _live_rr_floor   = _live_min_rr()

                    def _entry_conf_threshold(s) -> int:
                        """Zone entries need extra confidence bar unless freq=AGGRESSIVE."""
                        if "ZONE" in s.entry_type and not FREQ.allow_zone_entries:
                            return 999  # effectively excluded
                        # Use session-specific learned threshold when available
                        sess_floor = _live_min_conf(getattr(s, "session", ""))
                        if "ZONE" in s.entry_type:
                            return max(sess_floor + 10, 65)
                        return sess_floor

                    def _grade_ok(s) -> bool:
                        if not s.grade:
                            return True
                        if PAPER_MODE:
                            return True  # paper: always allow all grades for testing
                        return s.grade in FREQ.allowed_grades

                    high_conf = [
                        s for s in all_setups
                        if s.confidence >= _entry_conf_threshold(s)
                        and s.rr_ratio >= _live_rr_floor
                        and _grade_ok(s)
                    ]

                    # Log triggered vs zone counts with grades for awareness
                    triggered = [s for s in high_conf if "ZONE" not in s.entry_type]
                    zones     = [s for s in high_conf if "ZONE" in s.entry_type]
                    if triggered:
                        log.info(f"LTF TRIGGERED setups: {len(triggered)} — executing "
                                 f"[{_RISK_MODE}/{_FREQ_MODE}]")
                        for s in triggered:
                            log.info(f"  [{s.grade or '?'}] {s.symbol} {s.direction} "
                                     f"conf={s.confidence} rr={s.rr_ratio} [{s.entry_type}]")
                    if zones:
                        log.debug(f"HTF ZONE setups watching: {len(zones)} (awaiting LTF trigger)")
                        for s in zones:
                            log.debug(f"  [{s.grade or '?'}] {s.symbol} {s.direction} "
                                      f"conf={s.confidence} [{s.entry_type}]")
                except Exception as e:
                    log.error(f"ICT scan error: {e}")
                    high_conf = []

                if scan_count % 6 == 0:
                    print_status(state, data, all_setups)
                    # Heartbeat + memory report every 30 minutes
                    if scan_count % 180 == 0:
                        if memory:
                            log.info("\n" + memory.get_performance_report())
                        _acc_hb = get_account(data)
                        _open_hb = get_open_positions(data)
                        _sess_hb = data.get("session", "—")
                        _amd_hb  = data.get("amd_phase", "—")
                        _open_summary = ""
                        for _p in _open_hb[:5]:
                            _sym = _p.get("symbol", "?")
                            _dir = _p.get("type", "?")
                            _pnl = _p.get("profit", 0)
                            _open_summary += f"\n  • {_sym} {_dir}  ${_pnl:+.2f}"
                        send_telegram(
                            f"💓 <b>Heartbeat — {'PAPER' if PAPER_MODE else 'LIVE'}</b>\n"
                            + _tg_portfolio(state, data)
                            + f"\nSession: <b>{_sess_hb}</b> | Phase: <b>{_amd_hb}</b>"
                            + (f"\n<b>Open trades ({len(_open_hb)}):</b>{_open_summary}" if _open_hb else "\nNo open trades")
                        )

                # ── Execute high-confidence setups ─────────────────────
                if can_trade:
                    # Build a quick correlation map from open positions
                    # USD pairs: EURUSD/GBPUSD/AUDUSD/NZDUSD BUY = USD short
                    #            USDCAD/USDCHF/USDJPY BUY      = USD long
                    _open_positions_now = get_open_positions(data)
                    _usd_short_count = sum(
                        1 for p in _open_positions_now
                        if p.get("type") == "BUY"  and any(x in p.get("symbol","") for x in ("EURUSD","GBPUSD","AUDUSD","NZDUSD"))
                        or p.get("type") == "SELL" and any(x in p.get("symbol","") for x in ("USDCAD","USDCHF","USDJPY"))
                    )
                    _usd_long_count = sum(
                        1 for p in _open_positions_now
                        if p.get("type") == "SELL" and any(x in p.get("symbol","") for x in ("EURUSD","GBPUSD","AUDUSD","NZDUSD"))
                        or p.get("type") == "BUY"  and any(x in p.get("symbol","") for x in ("USDCAD","USDCHF","USDJPY"))
                    )

                    # Deduplicate: one trade per symbol per direction (highest confidence wins)
                    _seen_sym: dict = {}
                    for _s in high_conf:
                        _key = (_s.symbol, _s.direction)
                        if _key not in _seen_sym or _s.confidence > _seen_sym[_key].confidence:
                            _seen_sym[_key] = _s
                    high_conf = list(_seen_sym.values())

                    for setup in high_conf:
                        if open_count >= MAX_OPEN_TRADES:
                            break
                        _rt = state.recently_traded
                        if isinstance(_rt, dict) and setup.symbol in _rt and time.time() < _rt[setup.symbol]:
                            continue
                        elif isinstance(_rt, list) and setup.symbol in _rt:
                            continue

                        # Correlation exposure guard — max 2 positions in same USD direction
                        _is_usd_short = (setup.direction == "BUY"  and any(x in setup.symbol for x in ("EURUSD","GBPUSD","AUDUSD","NZDUSD"))
                                      or setup.direction == "SELL" and any(x in setup.symbol for x in ("USDCAD","USDCHF","USDJPY")))
                        _is_usd_long  = (setup.direction == "SELL" and any(x in setup.symbol for x in ("EURUSD","GBPUSD","AUDUSD","NZDUSD"))
                                      or setup.direction == "BUY"  and any(x in setup.symbol for x in ("USDCAD","USDCHF","USDJPY")))
                        if _is_usd_short and _usd_short_count >= 2:
                            log.debug(f"{setup.symbol} {setup.direction}: skipped — {_usd_short_count} correlated USD-short positions already open")
                            continue
                        if _is_usd_long and _usd_long_count >= 2:
                            log.debug(f"{setup.symbol} {setup.direction}: skipped — {_usd_long_count} correlated USD-long positions already open")
                            continue

                        charts   = data.get("charts", {})
                        sym_info = charts.get(setup.symbol, {})
                        if not sym_info:
                            continue

                        # Inject current bid price for order type detection
                        prices = {p["symbol"]: p for p in data.get("prices", [])}
                        if setup.symbol in prices:
                            sym_info["bid"] = prices[setup.symbol].get("bid", 0)

                        success = execute_setup(setup, equity, state, sym_info, memory=memory)
                        if success:
                            open_count += 1
                            if _is_usd_short: _usd_short_count += 1
                            if _is_usd_long:  _usd_long_count  += 1
                            log.info(f"Order placed: {setup.symbol} {setup.direction} @ {setup.entry_price:.5g}")

            # ── Persist state ──────────────────────────────────────────
            # Pass setups only when we actually ran a scan (non-empty),
            # so we don't overwrite previous top_setups with [] on ticks
            # where the ICT scan was skipped.
            save_state(state, setups=all_setups if all_setups else None)
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info(_msg("auto.shutdown"))
            save_state(state)
            win_rate = state.winning_trades / state.total_trades * 100 if state.total_trades > 0 else 0
            print(f"\n  Final P&L:    ${state.total_profit:.2f}")
            print(f"  Trades:       {state.total_trades} | WR: {win_rate:.0f}%  "
                  f"(W:{state.winning_trades} / L:{state.losing_trades})")
            print(f"  Peak equity:  ${state.peak_equity:,.2f}")
            print(f"  Max drawdown: {state.peak_drawdown:.2f}%")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
