import { useState, useEffect, useRef, useCallback } from "react";

const COLORS = {
  bg: "#07090c", card: "#0c0f14", card2: "#11161e", border: "#1c2333",
  gold: "#d4a843", silver: "#a8b8c8", red: "#e84545", green: "#2ecc71",
  blue: "#3b82f6", purple: "#8b5cf6", text: "#dde4ef", muted: "#4a5a72",
  goldDim: "#d4a84322", redDim: "#e8454522", greenDim: "#2ecc7122",
  blueDim: "#3b82f622",
};

const SYMBOLS = ["XAUUSD","EURUSD","GBPUSD","USDJPY","AUDUSD","XAGUSD","USDCAD","GBPJPY","EURJPY","NZDUSD","BTCUSD"];

const TIMEFRAMES = ["M5","M15","H1","H4","D1"];

// ── Realistic price seeds ──
const SEEDS = {
  XAUUSD:{price:2338.50,pip:0.01,digits:2,label:"Gold"},
  EURUSD:{price:1.0847,pip:0.0001,digits:5,label:"EUR/USD"},
  GBPUSD:{price:1.2703,pip:0.0001,digits:5,label:"GBP/USD"},
  USDJPY:{price:153.42,pip:0.01,digits:3,label:"USD/JPY"},
  AUDUSD:{price:0.6412,pip:0.0001,digits:5,label:"AUD/USD"},
  XAGUSD:{price:27.34,pip:0.01,digits:2,label:"Silver"},
  USDCAD:{price:1.3821,pip:0.0001,digits:5,label:"USD/CAD"},
  GBPJPY:{price:194.82,pip:0.01,digits:3,label:"GBP/JPY"},
  EURJPY:{price:166.28,pip:0.01,digits:3,label:"EUR/JPY"},
  NZDUSD:{price:0.5983,pip:0.0001,digits:5,label:"NZD/USD"},
  BTCUSD:{price:76876.59,pip:0.01,digits:2,label:"BTC/USD"},
};

// ── Generate realistic OHLC bars ──
function genBars(seed, count, tfMins) {
  const vol = seed.pip * (tfMins < 60 ? 15 : tfMins < 240 ? 40 : 80);
  let price = seed.price;
  const bars = [];
  for (let i = count; i >= 0; i--) {
    const o = price;
    const move = (Math.random() - 0.49) * vol;
    const c = Math.max(o * 0.98, o + move);
    const h = Math.max(o, c) + Math.abs((Math.random() - 0.3) * vol * 0.5);
    const l = Math.min(o, c) - Math.abs((Math.random() - 0.3) * vol * 0.5);
    bars.push({ o: +o.toFixed(seed.digits), h: +h.toFixed(seed.digits), l: +l.toFixed(seed.digits), c: +c.toFixed(seed.digits), v: Math.floor(Math.random() * 2000 + 500) });
    price = c;
  }
  return bars;
}

// ── ICT Analysis Engine ──
function analyzeICT(bars, sym) {
  const seed = SEEDS[sym];
  const recent = bars.slice(-20);
  const prev = bars.slice(-40, -20);
  const swingHigh = Math.max(...recent.map(b => b.h));
  const swingLow = Math.min(...recent.map(b => b.l));
  const prevHigh = Math.max(...prev.map(b => b.h));
  const prevLow = Math.min(...prev.map(b => b.l));
  const cur = bars[bars.length - 1].c;
  
  // BOS/CHoCH detection
  let structure = "RANGING";
  if (cur > prevHigh) structure = "BOS_BULLISH";
  else if (cur < prevLow) structure = "BOS_BEARISH";
  else if (cur > swingHigh * 0.999) structure = "CHOCH_BULL";
  else if (cur < swingLow * 1.001) structure = "CHOCH_BEAR";

  // Order Block detection
  let obType = "NONE", obHigh = 0, obLow = 0;
  for (let i = bars.length - 5; i >= bars.length - 15; i--) {
    const b = bars[i], bn = bars[i + 1];
    if (!b || !bn) continue;
    if (b.c < b.o && bn.c > bn.o && (bn.c - bn.o) / (bn.h - bn.l) > 0.6) {
      obType = "BULLISH_OB"; obHigh = b.h; obLow = b.l; break;
    }
    if (b.c > b.o && bn.c < bn.o && (bn.o - bn.c) / (bn.h - bn.l) > 0.6) {
      obType = "BEARISH_OB"; obHigh = b.h; obLow = b.l; break;
    }
  }

  // FVG detection
  let fvgType = "NONE", fvgHigh = 0, fvgLow = 0;
  for (let i = bars.length - 3; i >= bars.length - 10; i--) {
    const b0 = bars[i], b2 = bars[i + 2];
    if (!b0 || !b2) continue;
    if (b0.l > b2.h) { fvgType = "BULLISH"; fvgHigh = b0.l; fvgLow = b2.h; break; }
    if (b0.h < b2.l) { fvgType = "BEARISH"; fvgHigh = b2.l; fvgLow = b0.h; break; }
  }

  // Trend (MA cross)
  const ma20 = recent.reduce((s, b) => s + b.c, 0) / 20;
  const ma5 = bars.slice(-5).reduce((s, b) => s + b.c, 0) / 5;
  const trend = ma5 > ma20 ? "BULLISH" : ma5 < ma20 ? "BEARISH" : "NEUTRAL";

  // RSI
  let gains = 0, losses = 0;
  for (let i = bars.length - 14; i < bars.length; i++) {
    const d = bars[i].c - bars[i - 1].c;
    if (d > 0) gains += d; else losses -= d;
  }
  const rs = gains / (losses || 0.001);
  const rsi = +(100 - 100 / (1 + rs)).toFixed(1);

  // Asia range
  const asiaHigh = Math.max(...bars.slice(-8, -4).map(b => b.h));
  const asiaLow = Math.min(...bars.slice(-8, -4).map(b => b.l));

  // Liquidity levels
  const liqHighs = [swingHigh, prevHigh, asiaHigh].filter(v => v > cur);
  const liqLows = [swingLow, prevLow, asiaLow].filter(v => v < cur);

  // Score
  let bullScore = 30, bearScore = 30;
  if (trend === "BULLISH") bullScore += 20;
  if (trend === "BEARISH") bearScore += 20;
  if (fvgType === "BULLISH") bullScore += 25;
  if (fvgType === "BEARISH") bearScore += 25;
  if (obType === "BULLISH_OB") bullScore += 25;
  if (obType === "BEARISH_OB") bearScore += 25;
  if (rsi < 35) bullScore += 15;
  if (rsi > 65) bearScore += 15;
  if (structure === "BOS_BULLISH" || structure === "CHOCH_BULL") bullScore += 15;
  if (structure === "BOS_BEARISH" || structure === "CHOCH_BEAR") bearScore += 15;

  const direction = bullScore > bearScore ? "BUY" : bearScore > bullScore ? "SELL" : "NEUTRAL";
  const score = Math.min(direction === "BUY" ? bullScore : bearScore, 100);

  const atr = bars.slice(-14).reduce((s, b) => s + (b.h - b.l), 0) / 14;
  const sl = direction === "BUY" ? +(cur - atr * 1.5).toFixed(seed.digits) : +(cur + atr * 1.5).toFixed(seed.digits);
  const tp = direction === "BUY" ? +(cur + atr * 3).toFixed(seed.digits) : +(cur - atr * 3).toFixed(seed.digits);
  const rr = atr > 0 ? +(Math.abs(tp - cur) / Math.abs(sl - cur)).toFixed(2) : 0;

  const reasons = [];
  if (obType !== "NONE") reasons.push(`${obType === "BULLISH_OB" ? "Bullish" : "Bearish"} OB at ${obLow.toFixed(seed.digits)}–${obHigh.toFixed(seed.digits)}`);
  if (fvgType !== "NONE") reasons.push(`${fvgType} FVG: ${fvgLow.toFixed(seed.digits)}–${fvgHigh.toFixed(seed.digits)}`);
  if (structure !== "RANGING") reasons.push(`${structure.replace("_"," ")}`);
  if (rsi < 35) reasons.push(`RSI oversold (${rsi})`);
  if (rsi > 65) reasons.push(`RSI overbought (${rsi})`);
  reasons.push(`D1 bias: ${trend}`);

  return { direction, score, sl, tp, rr, rsi, trend, structure, obType, obHigh, obLow, fvgType, fvgHigh, fvgLow, asiaHigh, asiaLow, swingHigh, swingLow, liqHighs, liqLows, reasons, atr, cur };
}

// ── Candlestick Chart Component ──
function CandleChart({ bars, analysis, sym, height = 200 }) {
  const canvasRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !bars.length) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.offsetWidth;
    const H = height;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const disp = bars.slice(-60);
    const allH = disp.map(b => b.h), allL = disp.map(b => b.l);
    const maxP = Math.max(...allH), minP = Math.min(...allL);
    const range = maxP - minP || 0.001;
    const pad = { l: 8, r: 45, t: 12, b: 20 };
    const cW = W - pad.l - pad.r, cH = H - pad.t - pad.b;
    const bW = Math.max(2, (cW / disp.length) - 1);
    const toY = p => pad.t + cH - ((p - minP) / range) * cH;
    const toX = i => pad.l + i * (cW / disp.length) + bW / 2;

    // Grid
    ctx.strokeStyle = COLORS.border;
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + (cH / 4) * i;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      const price = maxP - (range / 4) * i;
      ctx.fillStyle = COLORS.muted;
      ctx.font = "9px monospace";
      ctx.fillText(price.toFixed(SEEDS[sym].digits > 3 ? 4 : 1), W - pad.r + 2, y + 3);
    }

    // Key levels
    const drawLevel = (price, color, label) => {
      if (price < minP || price > maxP) return;
      const y = toY(price);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.font = "8px monospace";
      ctx.fillText(label, pad.l + 2, y - 2);
    };

    if (analysis.obType !== "NONE") {
      const c = analysis.obType === "BULLISH_OB" ? COLORS.green : COLORS.red;
      const oy1 = toY(analysis.obHigh), oy2 = toY(analysis.obLow);
      ctx.fillStyle = c + "25";
      ctx.fillRect(pad.l, oy1, cW, oy2 - oy1);
      drawLevel(analysis.obHigh, c, "OB H");
      drawLevel(analysis.obLow, c, "OB L");
    }
    if (analysis.fvgType !== "NONE") {
      const c = analysis.fvgType === "BULLISH" ? COLORS.blue : COLORS.purple;
      const fy1 = toY(analysis.fvgHigh), fy2 = toY(analysis.fvgLow);
      ctx.fillStyle = c + "20";
      ctx.fillRect(pad.l, fy1, cW, fy2 - fy1);
    }
    drawLevel(analysis.swingHigh, COLORS.muted, "S.H");
    drawLevel(analysis.swingLow, COLORS.muted, "S.L");
    drawLevel(analysis.asiaHigh, COLORS.blue, "Asia H");
    drawLevel(analysis.asiaLow, COLORS.blue, "Asia L");

    // MA line
    const ma = [];
    for (let i = 19; i < disp.length; i++) {
      ma.push({ i, v: disp.slice(i - 19, i + 1).reduce((s, b) => s + b.c, 0) / 20 });
    }
    ctx.strokeStyle = COLORS.gold + "cc";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ma.forEach((p, j) => {
      const x = toX(p.i); const y = toY(p.v);
      if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Candles
    disp.forEach((b, i) => {
      const bull = b.c >= b.o;
      const color = bull ? COLORS.green : COLORS.red;
      const x = toX(i);
      const oY = toY(b.o), cY = toY(b.c), hY = toY(b.h), lY = toY(b.l);
      ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, hY); ctx.lineTo(x, lY); ctx.stroke();
      const bodyH = Math.abs(cY - oY) || 1;
      ctx.fillStyle = bull ? COLORS.green + "dd" : COLORS.red + "dd";
      ctx.fillRect(x - bW / 2, Math.min(oY, cY), bW, bodyH);
      ctx.strokeStyle = color;
      ctx.lineWidth = 0.5;
      ctx.strokeRect(x - bW / 2, Math.min(oY, cY), bW, bodyH);
    });

    // Entry/SL/TP lines
    if (analysis.direction !== "NEUTRAL") {
      drawLevel(analysis.cur, COLORS.gold, "Entry");
      drawLevel(analysis.sl, COLORS.red, "SL");
      drawLevel(analysis.tp, COLORS.green, "TP");
    }
  }, [bars, analysis, sym, height]);

  return <canvas ref={canvasRef} style={{ width: "100%", height, display: "block", background: COLORS.bg }} />;
}

// ── AMD Phases Clock ──
function AMDClock({ session, amdPhase }) {
  const h = new Date().getUTCHours();
  const amdColors = { ACCUMULATION: COLORS.blue, MANIPULATION: COLORS.gold, DISTRIBUTION: COLORS.green, LONDON_CLOSE: COLORS.purple };
  const sesColors = { ASIA: COLORS.blue, LONDON: COLORS.gold, NEW_YORK: COLORS.green, NY_CLOSE: COLORS.purple, CLOSED: COLORS.muted };
  const amdC = amdColors[amdPhase] || COLORS.muted;
  const sesC = sesColors[session] || COLORS.muted;

  const segments = [
    { label: "ACCUM", start: 22, end: 7, color: COLORS.blue },
    { label: "MANIP", start: 7, end: 12, color: COLORS.gold },
    { label: "DIST", start: 12, end: 17, color: COLORS.green },
    { label: "CLOSE", start: 17, end: 22, color: COLORS.purple },
  ];

  const hourToAngle = h => ((h % 24) / 24) * 360 - 90;
  const polarToXY = (angle, r) => ({
    x: 50 + r * Math.cos((angle * Math.PI) / 180),
    y: 50 + r * Math.sin((angle * Math.PI) / 180),
  });
  const arcPath = (a1, a2, r1, r2) => {
    const start1 = polarToXY(a1, r1), end1 = polarToXY(a2, r1);
    const start2 = polarToXY(a1, r2), end2 = polarToXY(a2, r2);
    const large = (a2 - a1 + 360) % 360 > 180 ? 1 : 0;
    return `M ${start1.x} ${start1.y} A ${r1} ${r1} 0 ${large} 1 ${end1.x} ${end1.y} L ${end2.x} ${end2.y} A ${r2} ${r2} 0 ${large} 0 ${start2.x} ${start2.y} Z`;
  };

  const handAngle = hourToAngle(h);
  const handEnd = polarToXY(handAngle, 28);

  return (
    <svg viewBox="0 0 100 100" style={{ width: 90, height: 90, flexShrink: 0 }}>
      {segments.map((seg) => {
        const a1 = hourToAngle(seg.start);
        let a2 = hourToAngle(seg.end);
        if (seg.start === 22) a2 = hourToAngle(7 + 24);
        return <path key={seg.label} d={arcPath(a1, a2, 45, 35)} fill={seg.color + "44"} stroke={seg.color + "88"} strokeWidth="0.3" />;
      })}
      <circle cx="50" cy="50" r="33" fill={COLORS.card} stroke={COLORS.border} strokeWidth="0.5" />
      <text x="50" y="47" textAnchor="middle" fill={sesC} fontSize="7" fontFamily="monospace" fontWeight="bold">{session}</text>
      <text x="50" y="56" textAnchor="middle" fill={amdC} fontSize="5.5" fontFamily="monospace">{amdPhase.slice(0,6)}</text>
      <line x1="50" y1="50" x2={handEnd.x} y2={handEnd.y} stroke={COLORS.gold} strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="50" cy="50" r="2" fill={COLORS.gold} />
    </svg>
  );
}

// ── Mini Sparkline ──
function Sparkline({ bars, color }) {
  if (!bars || bars.length < 2) return null;
  const prices = bars.slice(-20).map(b => b.c);
  const min = Math.min(...prices), max = Math.max(...prices);
  const range = max - min || 0.001;
  const pts = prices.map((p, i) => `${(i / (prices.length - 1)) * 100},${100 - ((p - min) / range) * 100}`).join(" ");
  return (
    <svg viewBox="0 0 100 100" style={{ width: 60, height: 28 }} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

// ── RSI Gauge ──
function RSIGauge({ value }) {
  const angle = ((value / 100) * 180) - 90;
  const c = value > 70 ? COLORS.red : value < 30 ? COLORS.green : COLORS.gold;
  const rad = (angle * Math.PI) / 180;
  const x = 50 + 30 * Math.cos(rad), y = 50 + 30 * Math.sin(rad);
  return (
    <svg viewBox="0 0 100 60" style={{ width: 70, height: 42 }}>
      <path d="M 10 50 A 40 40 0 0 1 90 50" fill="none" stroke={COLORS.border} strokeWidth="8" strokeLinecap="round" />
      <path d={`M 10 50 A 40 40 0 0 1 ${x} ${y}`} fill="none" stroke={c} strokeWidth="8" strokeLinecap="round" />
      <line x1="50" y1="50" x2={x} y2={y} stroke={c} strokeWidth="2" />
      <circle cx={x} cy={y} r="3" fill={c} />
      <text x="50" y="40" textAnchor="middle" fill={c} fontSize="11" fontFamily="monospace" fontWeight="bold">{value}</text>
    </svg>
  );
}

// ── Main App ──
export default function OmniICTDashboard() {
  const [activeSym, setActiveSym] = useState("XAUUSD");
  const [activeTF, setActiveTF] = useState("H1");
  const [page, setPage] = useState("markets");
  const [barsMap, setBarsMap] = useState({});
  const [analysisMap, setAnalysisMap] = useState({});
  const [tick, setTick] = useState(0);
  const [account] = useState({ balance: 10000, equity: 10187.50, profit: 187.50, leverage: 100 });
  const [tradeLog, setTradeLog] = useState([]);
  const [alerts, setAlerts] = useState([]);

  // Init bars
  useEffect(() => {
    const tfMins = { M5: 5, M15: 15, H1: 60, H4: 240, D1: 1440 };
    const map = {};
    SYMBOLS.forEach(sym => {
      map[sym] = {};
      TIMEFRAMES.forEach(tf => {
        map[sym][tf] = genBars(SEEDS[sym], 200, tfMins[tf]);
      });
    });
    setBarsMap(map);
  }, []);

  // Tick prices + regen analysis
  useEffect(() => {
    if (!Object.keys(barsMap).length) return;
    const interval = setInterval(() => {
      setBarsMap(prev => {
        const next = { ...prev };
        SYMBOLS.forEach(sym => {
          const seed = SEEDS[sym];
          next[sym] = { ...prev[sym] };
          TIMEFRAMES.forEach(tf => {
            const arr = [...prev[sym][tf]];
            const last = arr[arr.length - 1];
            const vol = seed.pip * (tf === "M5" ? 5 : tf === "M15" ? 12 : tf === "H1" ? 25 : 50);
            const move = (Math.random() - 0.49) * vol;
            const c = +(Math.max(last.c * 0.99, last.c + move)).toFixed(seed.digits);
            const h = +(Math.max(last.h, c) + Math.abs((Math.random() - 0.5) * vol * 0.2)).toFixed(seed.digits);
            const l = +(Math.min(last.l, c) - Math.abs((Math.random() - 0.5) * vol * 0.2)).toFixed(seed.digits);
            arr[arr.length - 1] = { ...last, c, h: Math.max(h, c), l: Math.min(l, c) };
            next[sym][tf] = arr;
          });
        });
        return next;
      });
      setTick(t => t + 1);
    }, 2000);
    return () => clearInterval(interval);
  }, [barsMap]);

  // Update analysis
  useEffect(() => {
    if (!Object.keys(barsMap).length) return;
    const map = {};
    SYMBOLS.forEach(sym => {
      if (barsMap[sym]?.[activeTF]) {
        map[sym] = analyzeICT(barsMap[sym][activeTF], sym);
      }
    });
    setAnalysisMap(map);
  }, [barsMap, activeTF, tick]);

  // Auto alerts
  useEffect(() => {
    if (!Object.keys(analysisMap).length) return;
    SYMBOLS.forEach(sym => {
      const a = analysisMap[sym];
      if (!a) return;
      if (a.score >= 75 && a.direction !== "NEUTRAL") {
        const msg = `${sym} ${a.direction} signal — Score ${a.score}/100 | RR ${a.rr}`;
        setAlerts(prev => {
          if (prev.length > 0 && prev[0].msg === msg) return prev;
          return [{ msg, sym, dir: a.direction, ts: new Date().toLocaleTimeString(), id: Date.now() }, ...prev.slice(0, 4)];
        });
      }
    });
  }, [analysisMap]);

  const analysis = analysisMap[activeSym];
  const bars = barsMap[activeSym]?.[activeTF] || [];
  const curPrice = bars.length ? bars[bars.length - 1].c : SEEDS[activeSym].price;
  const seed = SEEDS[activeSym];

  const h = new Date().getUTCHours();
  const session = h >= 22 || h < 7 ? "ASIA" : h < 12 ? "LONDON" : h < 17 ? "NEW_YORK" : "NY_CLOSE";
  const amdPhase = h >= 22 || h < 7 ? "ACCUMULATION" : h < 12 ? "MANIPULATION" : "DISTRIBUTION";

  const addPaperTrade = useCallback(() => {
    if (!analysis || analysis.direction === "NEUTRAL") return;
    const t = {
      id: Date.now(), sym: activeSym, dir: analysis.direction,
      entry: curPrice, sl: analysis.sl, tp: analysis.tp,
      score: analysis.score, rr: analysis.rr, tf: activeTF,
      time: new Date().toLocaleTimeString(), status: "OPEN",
      pnl: 0,
    };
    setTradeLog(prev => [t, ...prev.slice(0, 9)]);
  }, [analysis, activeSym, curPrice, activeTF]);

  const sessCol = { ASIA: COLORS.blue, LONDON: COLORS.gold, NEW_YORK: COLORS.green, NY_CLOSE: COLORS.purple };
  const dirCol = d => d === "BUY" ? COLORS.green : d === "SELL" ? COLORS.red : COLORS.muted;
  const scoreCol = s => s >= 75 ? COLORS.gold : s >= 55 ? COLORS.blue : COLORS.muted;

  const cs = s => ({ cursor: "pointer", fontFamily: "monospace", fontSize: 11, display: "flex", alignItems: "center", gap: 8, padding: "9px 14px", borderRadius: 8, margin: "2px 0", color: page === s ? COLORS.gold : COLORS.muted, background: page === s ? COLORS.gold + "15" : "transparent", border: `1px solid ${page === s ? COLORS.gold + "44" : "transparent"}` });

  const navPages = [
    { id: "markets", icon: "◈", label: "Markets" },
    { id: "chart", icon: "▦", label: "Chart" },
    { id: "scanner", icon: "⌖", label: "Scanner" },
    { id: "positions", icon: "≡", label: "Positions" },
    { id: "ai", icon: "⬡", label: "AI Engine" },
  ];

  return (
    <div style={{ display: "flex", background: COLORS.bg, minHeight: "100vh", fontFamily: "'Sora','Segoe UI',sans-serif", color: COLORS.text }}>
      {/* ── Sidebar ── */}
      <div style={{ width: 190, minWidth: 190, background: COLORS.card, borderRight: `1px solid ${COLORS.border}`, display: "flex", flexDirection: "column", position: "sticky", top: 0, height: "100vh", overflowY: "auto" }}>
        <div style={{ padding: "18px 16px 14px", borderBottom: `1px solid ${COLORS.border}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: COLORS.gold, fontSize: 20 }}>◈</span>
            <div>
              <div style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 14, color: COLORS.text, letterSpacing: "0.2em" }}>OMNI</div>
              <div style={{ fontSize: 9, color: COLORS.gold, letterSpacing: "0.3em" }}>ICT AI PRO</div>
            </div>
          </div>
        </div>

        {/* Account mini */}
        <div style={{ margin: "10px 10px 6px", background: COLORS.card2, borderRadius: 8, padding: "10px 12px", border: `1px solid ${COLORS.border}` }}>
          <div style={{ fontSize: 10, color: COLORS.muted, marginBottom: 3 }}>PAPER ACCOUNT</div>
          <div style={{ fontFamily: "monospace", fontSize: 14, fontWeight: 700, color: COLORS.gold }}>${account.balance.toLocaleString()}</div>
          <div style={{ fontFamily: "monospace", fontSize: 11, color: account.profit >= 0 ? COLORS.green : COLORS.red, marginTop: 2 }}>{account.profit >= 0 ? "+" : ""}${account.profit.toFixed(2)}</div>
        </div>

        {/* Session */}
        <div style={{ margin: "0 10px 8px", padding: "8px 12px", borderRadius: 8, background: (sessCol[session] || COLORS.muted) + "12", border: `1px solid ${(sessCol[session] || COLORS.muted)}33` }}>
          <div style={{ fontSize: 9, color: COLORS.muted, marginBottom: 2 }}>SESSION</div>
          <div style={{ fontFamily: "monospace", fontSize: 11, fontWeight: 700, color: sessCol[session] || COLORS.muted }}>{session}</div>
          <div style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted, marginTop: 1 }}>{amdPhase}</div>
        </div>

        {/* Nav */}
        <div style={{ padding: "0 8px", flex: 1 }}>
          <div style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted, letterSpacing: "0.15em", padding: "6px 8px 3px" }}>NAVIGATION</div>
          {navPages.map(p => (
            <div key={p.id} style={cs(p.id)} onClick={() => setPage(p.id)}>
              <span style={{ fontSize: 14 }}>{p.icon}</span>
              <span>{p.label}</span>
            </div>
          ))}
        </div>

        {/* Clock */}
        <div style={{ padding: "10px 16px", borderTop: `1px solid ${COLORS.border}`, textAlign: "center" }}>
          <AMDClock session={session} amdPhase={amdPhase} />
          <div style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted, marginTop: 4 }}>{new Date().toUTCString().slice(17, 25)} UTC</div>
        </div>
      </div>

      {/* ── Main content ── */}
      <div style={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
        {/* Alerts bar */}
        {alerts.length > 0 && (
          <div style={{ background: COLORS.gold + "15", borderBottom: `1px solid ${COLORS.gold}33`, padding: "8px 20px", display: "flex", gap: 16, overflowX: "auto" }}>
            {alerts.slice(0, 3).map(a => (
              <div key={a.id} style={{ display: "flex", alignItems: "center", gap: 8, whiteSpace: "nowrap" }}>
                <span style={{ color: dirCol(a.dir), fontFamily: "monospace", fontSize: 10, fontWeight: 700 }}>{a.dir}</span>
                <span style={{ color: COLORS.gold, fontFamily: "monospace", fontSize: 10 }}>{a.sym}</span>
                <span style={{ color: COLORS.muted, fontSize: 9 }}>{a.ts}</span>
              </div>
            ))}
          </div>
        )}

        <div style={{ padding: "18px 22px" }}>

          {/* ══ MARKETS PAGE ══ */}
          {page === "markets" && (
            <div>
              <div style={{ marginBottom: 16 }}>
                <h2 style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 18, color: COLORS.gold, margin: 0 }}>Markets Overview</h2>
                <p style={{ fontSize: 12, color: COLORS.muted, margin: "4px 0 0" }}>ICT Smart Money Analysis — {SYMBOLS.length} instruments</p>
              </div>

              {/* Top metrics */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(130px,1fr))", gap: 10, marginBottom: 16 }}>
                {[
                  { label: "Balance", val: `$${account.balance.toLocaleString()}`, c: COLORS.gold },
                  { label: "Equity", val: `$${account.equity.toFixed(0)}`, c: COLORS.gold },
                  { label: "Open P&L", val: `+$${account.profit.toFixed(2)}`, c: COLORS.green },
                  { label: "Session", val: session, c: sessCol[session] },
                  { label: "AMD Phase", val: amdPhase.slice(0,6), c: COLORS.purple },
                  { label: "Leverage", val: `1:${account.leverage}`, c: COLORS.muted },
                ].map(m => (
                  <div key={m.label} style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderTop: `2px solid ${m.c}44`, borderRadius: 8, padding: "10px 12px" }}>
                    <div style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted, textTransform: "uppercase", letterSpacing: "0.1em" }}>{m.label}</div>
                    <div style={{ fontFamily: "monospace", fontSize: 14, fontWeight: 700, color: m.c, marginTop: 3 }}>{m.val}</div>
                  </div>
                ))}
              </div>

              {/* Symbol grid */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))", gap: 10 }}>
                {SYMBOLS.map(sym => {
                  const a = analysisMap[sym];
                  const b = barsMap[sym]?.[activeTF] || [];
                  const price = b.length ? b[b.length - 1].c : SEEDS[sym].price;
                  const prev = b.length > 1 ? b[b.length - 2].c : price;
                  const chg = ((price - prev) / prev * 100);
                  const dc = dirCol(a?.direction);
                  const sc = scoreCol(a?.score || 0);

                  return (
                    <div key={sym} onClick={() => { setActiveSym(sym); setPage("chart"); }}
                      style={{ background: COLORS.card, border: `1px solid ${sym === activeSym ? COLORS.gold + "66" : COLORS.border}`, borderRadius: 10, padding: 14, cursor: "pointer", transition: "all 0.2s" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
                        <div>
                          <div style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 13, color: COLORS.text }}>{sym}</div>
                          <div style={{ fontSize: 10, color: COLORS.muted }}>{SEEDS[sym].label}</div>
                        </div>
                        <div style={{ textAlign: "right" }}>
                          <div style={{ fontFamily: "monospace", fontSize: 14, fontWeight: 700, color: COLORS.text }}>{price.toFixed(seed.digits > 3 ? 5 : 2)}</div>
                          <div style={{ fontFamily: "monospace", fontSize: 10, color: chg >= 0 ? COLORS.green : COLORS.red }}>{chg >= 0 ? "+" : ""}{chg.toFixed(3)}%</div>
                        </div>
                      </div>

                      <div style={{ marginBottom: 8 }}>
                        <Sparkline bars={b} color={a?.trend === "BULLISH" ? COLORS.green : COLORS.red} />
                      </div>

                      {a && (
                        <>
                          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                            <span style={{ fontFamily: "monospace", fontSize: 11, fontWeight: 700, color: dc }}>{a.direction}</span>
                            <span style={{ fontFamily: "monospace", fontSize: 10, color: sc }}>{a.score}/100</span>
                          </div>
                          <div style={{ height: 3, background: COLORS.border, borderRadius: 2, marginBottom: 8 }}>
                            <div style={{ height: "100%", width: `${a.score}%`, background: `linear-gradient(90deg,${dc},${COLORS.gold})`, borderRadius: 2 }} />
                          </div>
                          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                            {a.obType !== "NONE" && <span style={{ fontSize: 9, fontFamily: "monospace", background: (a.obType === "BULLISH_OB" ? COLORS.green : COLORS.red) + "22", color: a.obType === "BULLISH_OB" ? COLORS.green : COLORS.red, padding: "2px 6px", borderRadius: 4 }}>OB</span>}
                            {a.fvgType !== "NONE" && <span style={{ fontSize: 9, fontFamily: "monospace", background: COLORS.blue + "22", color: COLORS.blue, padding: "2px 6px", borderRadius: 4 }}>FVG</span>}
                            <span style={{ fontSize: 9, fontFamily: "monospace", background: COLORS.card2, color: COLORS.muted, padding: "2px 6px", borderRadius: 4 }}>{a.structure.replace("_"," ").slice(0,12)}</span>
                          </div>
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* ══ CHART PAGE ══ */}
          {page === "chart" && (
            <div>
              {/* Controls */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12, marginBottom: 14 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <select value={activeSym} onChange={e => setActiveSym(e.target.value)}
                    style={{ background: COLORS.card2, color: COLORS.text, border: `1px solid ${COLORS.border}`, borderRadius: 6, padding: "6px 10px", fontFamily: "monospace", fontSize: 12, cursor: "pointer" }}>
                    {SYMBOLS.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                  {TIMEFRAMES.map(tf => (
                    <button key={tf} onClick={() => setActiveTF(tf)}
                      style={{ background: activeTF === tf ? COLORS.gold + "22" : COLORS.card2, color: activeTF === tf ? COLORS.gold : COLORS.muted, border: `1px solid ${activeTF === tf ? COLORS.gold + "44" : COLORS.border}`, borderRadius: 6, padding: "6px 12px", fontFamily: "monospace", fontSize: 11, cursor: "pointer" }}>
                      {tf}
                    </button>
                  ))}
                </div>
                {analysis && (
                  <button onClick={addPaperTrade}
                    style={{ background: dirCol(analysis.direction) + "22", color: dirCol(analysis.direction), border: `1px solid ${dirCol(analysis.direction)}44`, borderRadius: 6, padding: "7px 16px", fontFamily: "monospace", fontSize: 11, fontWeight: 700, cursor: "pointer" }}>
                    + {analysis.direction} {activeSym}
                  </button>
                )}
              </div>

              {/* Price header */}
              {analysis && (
                <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 12, padding: "12px 16px", background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}` }}>
                  <div>
                    <div style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 22, color: COLORS.text }}>{curPrice.toFixed(seed.digits)}</div>
                    <div style={{ fontSize: 11, color: COLORS.muted }}>{SEEDS[activeSym].label}</div>
                  </div>
                  <div style={{ height: 40, width: 1, background: COLORS.border }} />
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: "0 20px" }}>
                    {[
                      { l: "Signal", v: analysis.direction, c: dirCol(analysis.direction) },
                      { l: "Score", v: `${analysis.score}/100`, c: scoreCol(analysis.score) },
                      { l: "RSI", v: analysis.rsi, c: analysis.rsi > 70 ? COLORS.red : analysis.rsi < 30 ? COLORS.green : COLORS.muted },
                      { l: "RR Ratio", v: `${analysis.rr}:1`, c: analysis.rr >= 2 ? COLORS.green : COLORS.gold },
                      { l: "Structure", v: analysis.structure.replace("_"," ").slice(0,10), c: COLORS.blue },
                    ].map(m => (
                      <div key={m.l}>
                        <div style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted, textTransform: "uppercase" }}>{m.l}</div>
                        <div style={{ fontFamily: "monospace", fontSize: 12, fontWeight: 700, color: m.c, marginTop: 2 }}>{m.v}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Main chart */}
              <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, padding: 12, marginBottom: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <span style={{ fontFamily: "monospace", fontSize: 11, color: COLORS.muted }}>{activeSym} / {activeTF} — ICT Analysis</span>
                  <div style={{ display: "flex", gap: 12, fontSize: 9, fontFamily: "monospace" }}>
                    {[
                      { c: COLORS.gold, l: "MA20" }, { c: COLORS.green + "aa", l: "OB Bull" },
                      { c: COLORS.red + "aa", l: "OB Bear" }, { c: COLORS.blue + "aa", l: "FVG" },
                    ].map(({ c, l }) => (
                      <span key={l} style={{ display: "flex", alignItems: "center", gap: 4, color: COLORS.muted }}>
                        <span style={{ width: 10, height: 2, background: c, display: "inline-block" }} />{l}
                      </span>
                    ))}
                  </div>
                </div>
                {bars.length > 0 && analysis && <CandleChart bars={bars} analysis={analysis} sym={activeSym} height={320} />}
              </div>

              {/* Analysis panels */}
              {analysis && (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
                  {/* Entry plan */}
                  <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, padding: 14 }}>
                    <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.gold, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 10 }}>Trade Setup</div>
                    {[
                      { l: "Entry", v: curPrice.toFixed(seed.digits), c: COLORS.gold },
                      { l: "Stop Loss", v: analysis.sl.toFixed(seed.digits), c: COLORS.red },
                      { l: "Take Profit", v: analysis.tp.toFixed(seed.digits), c: COLORS.green },
                      { l: "RR Ratio", v: `${analysis.rr}:1`, c: analysis.rr >= 2 ? COLORS.green : COLORS.gold },
                      { l: "ATR", v: analysis.atr.toFixed(seed.digits), c: COLORS.muted },
                      { l: "Session", v: session, c: sessCol[session] },
                    ].map(m => (
                      <div key={m.l} style={{ display: "flex", justifyContent: "space-between", padding: "5px 0", borderBottom: `1px solid ${COLORS.border}` }}>
                        <span style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.muted }}>{m.l}</span>
                        <span style={{ fontFamily: "monospace", fontSize: 10, fontWeight: 700, color: m.c }}>{m.v}</span>
                      </div>
                    ))}
                  </div>

                  {/* ICT Levels */}
                  <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, padding: 14 }}>
                    <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.gold, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 10 }}>ICT Levels</div>
                    <div style={{ fontSize: 9, color: COLORS.blue, marginBottom: 4, fontFamily: "monospace" }}>ASIA RANGE</div>
                    <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.text, marginBottom: 8 }}>H: {analysis.asiaHigh.toFixed(seed.digits)} / L: {analysis.asiaLow.toFixed(seed.digits)}</div>
                    <div style={{ fontSize: 9, color: COLORS.muted, marginBottom: 4, fontFamily: "monospace" }}>SWING LEVELS</div>
                    <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.text, marginBottom: 8 }}>H: {analysis.swingHigh.toFixed(seed.digits)} / L: {analysis.swingLow.toFixed(seed.digits)}</div>
                    {analysis.obType !== "NONE" && (
                      <>
                        <div style={{ fontSize: 9, color: analysis.obType === "BULLISH_OB" ? COLORS.green : COLORS.red, marginBottom: 4, fontFamily: "monospace" }}>ORDER BLOCK</div>
                        <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.text, marginBottom: 8 }}>{analysis.obLow.toFixed(seed.digits)} – {analysis.obHigh.toFixed(seed.digits)}</div>
                      </>
                    )}
                    {analysis.fvgType !== "NONE" && (
                      <>
                        <div style={{ fontSize: 9, color: COLORS.blue, marginBottom: 4, fontFamily: "monospace" }}>FAIR VALUE GAP ({analysis.fvgType})</div>
                        <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.text }}>{analysis.fvgLow.toFixed(seed.digits)} – {analysis.fvgHigh.toFixed(seed.digits)}</div>
                      </>
                    )}
                    <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 10 }}>
                      <span style={{ fontSize: 9, color: COLORS.muted, fontFamily: "monospace" }}>RSI</span>
                      <RSIGauge value={analysis.rsi} />
                    </div>
                  </div>

                  {/* Reasons */}
                  <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, padding: 14 }}>
                    <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.gold, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 10 }}>Signal Reasons</div>
                    {analysis.reasons.map((r, i) => (
                      <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "4px 0", borderBottom: `1px solid ${COLORS.border}` }}>
                        <span style={{ color: dirCol(analysis.direction), fontSize: 8, marginTop: 3 }}>▸</span>
                        <span style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.muted, lineHeight: 1.4 }}>{r}</span>
                      </div>
                    ))}
                    <div style={{ marginTop: 12, padding: "8px 10px", background: dirCol(analysis.direction) + "12", borderRadius: 6, border: `1px solid ${dirCol(analysis.direction)}33` }}>
                      <span style={{ fontFamily: "monospace", fontSize: 10, fontWeight: 700, color: dirCol(analysis.direction) }}>
                        {analysis.direction} Signal — {analysis.score >= 75 ? "High Confidence" : analysis.score >= 55 ? "Medium Confidence" : "Low Confidence"}
                      </span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ══ SCANNER PAGE ══ */}
          {page === "scanner" && (
            <div>
              <div style={{ marginBottom: 14 }}>
                <h2 style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 18, color: COLORS.text, margin: 0 }}>ICT Scanner</h2>
                <p style={{ fontSize: 12, color: COLORS.muted, margin: "4px 0 0" }}>Multi-symbol confluence detector — {activeTF} timeframe</p>
              </div>

              {/* Top setups */}
              <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, padding: 14, marginBottom: 12 }}>
                <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.gold, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 10 }}>Top Setups</div>
                {Object.entries(analysisMap).sort((a, b) => b[1].score - a[1].score).slice(0, 5).map(([sym, a]) => {
                  const dc = dirCol(a.direction);
                  return (
                    <div key={sym} onClick={() => { setActiveSym(sym); setPage("chart"); }}
                      style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 0", borderBottom: `1px solid ${COLORS.border}`, cursor: "pointer" }}>
                      <div style={{ minWidth: 80 }}>
                        <span style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 13, color: COLORS.text }}>{sym}</span>
                        <span style={{ fontFamily: "monospace", fontSize: 11, color: dc, fontWeight: 700, marginLeft: 8 }}>{a.direction}</span>
                      </div>
                      <div style={{ flex: 1 }}>
                        <div style={{ height: 4, background: COLORS.border, borderRadius: 2 }}>
                          <div style={{ height: "100%", width: `${a.score}%`, background: `linear-gradient(90deg,${dc},${COLORS.gold})`, borderRadius: 2, transition: "width 0.5s" }} />
                        </div>
                      </div>
                      <span style={{ fontFamily: "monospace", fontSize: 11, color: COLORS.gold, minWidth: 50 }}>{a.score}/100</span>
                      <span style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.muted, minWidth: 40 }}>RR {a.rr}</span>
                      <div style={{ display: "flex", gap: 5 }}>
                        {a.obType !== "NONE" && <span style={{ fontSize: 9, fontFamily: "monospace", background: COLORS.green + "22", color: COLORS.green, padding: "2px 5px", borderRadius: 3 }}>OB</span>}
                        {a.fvgType !== "NONE" && <span style={{ fontSize: 9, fontFamily: "monospace", background: COLORS.blue + "22", color: COLORS.blue, padding: "2px 5px", borderRadius: 3 }}>FVG</span>}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Full table */}
              <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, overflow: "hidden" }}>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ background: COLORS.card2 }}>
                        {["Symbol","Price","Signal","Score","RSI","Trend","Structure","OB","FVG","SL","TP"].map(h => (
                          <th key={h} style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.gold, textTransform: "uppercase", letterSpacing: "0.08em", border: `1px solid ${COLORS.border}`, padding: "8px 10px", textAlign: "left" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(analysisMap).sort((a, b) => b[1].score - a[1].score).map(([sym, a]) => {
                        const b = barsMap[sym]?.[activeTF] || [];
                        const price = b.length ? b[b.length - 1].c : SEEDS[sym].price;
                        const sd = SEEDS[sym];
                        return (
                          <tr key={sym} onClick={() => { setActiveSym(sym); setPage("chart"); }}
                            style={{ cursor: "pointer", borderBottom: `1px solid ${COLORS.border}` }}
                            onMouseEnter={e => e.currentTarget.style.background = COLORS.card2}
                            onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
                            {[
                              { v: sym, c: COLORS.text, w: 700 },
                              { v: price.toFixed(sd.digits > 3 ? 5 : 2), c: COLORS.text },
                              { v: a.direction, c: dirCol(a.direction), w: 700 },
                              { v: `${a.score}/100`, c: scoreCol(a.score) },
                              { v: a.rsi, c: a.rsi > 70 ? COLORS.red : a.rsi < 30 ? COLORS.green : COLORS.muted },
                              { v: a.trend, c: a.trend === "BULLISH" ? COLORS.green : a.trend === "BEARISH" ? COLORS.red : COLORS.muted },
                              { v: a.structure.replace("_"," ").slice(0,10), c: COLORS.blue },
                              { v: a.obType === "NONE" ? "—" : a.obType.replace("_OB","").slice(0,4), c: a.obType.includes("BULL") ? COLORS.green : a.obType.includes("BEAR") ? COLORS.red : COLORS.muted },
                              { v: a.fvgType === "NONE" ? "—" : a.fvgType.slice(0,4), c: a.fvgType !== "NONE" ? COLORS.blue : COLORS.muted },
                              { v: a.sl.toFixed(sd.digits > 3 ? 5 : 2), c: COLORS.red },
                              { v: a.tp.toFixed(sd.digits > 3 ? 5 : 2), c: COLORS.green },
                            ].map((cell, i) => (
                              <td key={i} style={{ fontFamily: "monospace", fontSize: 10, color: cell.c, fontWeight: cell.w, border: `1px solid ${COLORS.border}`, padding: "7px 10px" }}>{cell.v}</td>
                            ))}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* ══ POSITIONS PAGE ══ */}
          {page === "positions" && (
            <div>
              <div style={{ marginBottom: 14 }}>
                <h2 style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 18, color: COLORS.text, margin: 0 }}>Paper Positions</h2>
                <p style={{ fontSize: 12, color: COLORS.muted, margin: "4px 0 0" }}>All paper trades — click chart to add entries</p>
              </div>

              {tradeLog.length === 0 ? (
                <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, padding: 40, textAlign: "center" }}>
                  <div style={{ fontSize: 32, color: COLORS.gold + "44", marginBottom: 12 }}>◈</div>
                  <div style={{ fontFamily: "monospace", fontSize: 13, color: COLORS.muted }}>No trades yet</div>
                  <div style={{ fontSize: 11, color: COLORS.muted + "88", marginTop: 6 }}>Go to Chart and click "+ BUY/SELL" to log a paper trade</div>
                </div>
              ) : (
                <div>
                  {/* Summary */}
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 10, marginBottom: 14 }}>
                    {[
                      { l: "Total Trades", v: tradeLog.length, c: COLORS.text },
                      { l: "Open", v: tradeLog.filter(t => t.status === "OPEN").length, c: COLORS.gold },
                      { l: "Avg Score", v: (tradeLog.reduce((s, t) => s + t.score, 0) / tradeLog.length).toFixed(0), c: COLORS.blue },
                      { l: "Avg RR", v: (tradeLog.reduce((s, t) => s + t.rr, 0) / tradeLog.length).toFixed(2), c: COLORS.green },
                    ].map(m => (
                      <div key={m.l} style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: "10px 14px" }}>
                        <div style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted }}>{m.l}</div>
                        <div style={{ fontFamily: "monospace", fontSize: 18, fontWeight: 700, color: m.c, marginTop: 4 }}>{m.v}</div>
                      </div>
                    ))}
                  </div>

                  {tradeLog.map(t => (
                    <div key={t.id} style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 10, padding: 14, marginBottom: 10 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
                        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                          <div>
                            <span style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 14, color: COLORS.text }}>{t.sym}</span>
                            <span style={{ fontFamily: "monospace", fontSize: 12, fontWeight: 700, color: dirCol(t.dir), marginLeft: 10 }}>{t.dir}</span>
                            <span style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted, marginLeft: 8 }}>{t.tf}</span>
                          </div>
                        </div>
                        <div style={{ display: "flex", gap: 16 }}>
                          {[
                            { l: "Entry", v: t.entry.toFixed(SEEDS[t.sym]?.digits || 5), c: COLORS.gold },
                            { l: "SL", v: t.sl.toFixed(SEEDS[t.sym]?.digits || 5), c: COLORS.red },
                            { l: "TP", v: t.tp.toFixed(SEEDS[t.sym]?.digits || 5), c: COLORS.green },
                            { l: "Score", v: `${t.score}/100`, c: COLORS.blue },
                            { l: "RR", v: `${t.rr}:1`, c: t.rr >= 2 ? COLORS.green : COLORS.gold },
                          ].map(m => (
                            <div key={m.l} style={{ textAlign: "center" }}>
                              <div style={{ fontFamily: "monospace", fontSize: 8, color: COLORS.muted }}>{m.l}</div>
                              <div style={{ fontFamily: "monospace", fontSize: 11, fontWeight: 700, color: m.c }}>{m.v}</div>
                            </div>
                          ))}
                        </div>
                        <div style={{ fontSize: 9, color: COLORS.muted, fontFamily: "monospace" }}>{t.time}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ══ AI ENGINE PAGE ══ */}
          {page === "ai" && (
            <div>
              <div style={{ marginBottom: 14 }}>
                <h2 style={{ fontFamily: "monospace", fontWeight: 700, fontSize: 18, color: COLORS.purple, margin: 0 }}>
                  <span style={{ marginRight: 8 }}>⬡</span>AI Engine
                </h2>
                <p style={{ fontSize: 12, color: COLORS.muted, margin: "4px 0 0" }}>Market regime analysis — ICT Smart Money integration</p>
              </div>

              {/* Regime */}
              <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.purple}44`, padding: 16, marginBottom: 12 }}>
                <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.purple, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 12 }}>Market Regime</div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))", gap: 10 }}>
                  {[
                    { l: "Phase", v: "RANGING", c: COLORS.blue },
                    { l: "AMD Stage", v: amdPhase, c: COLORS.gold },
                    { l: "Session", v: session, c: sessCol[session] },
                    { l: "Bias", v: "NEUTRAL", c: COLORS.muted },
                    { l: "Kill Zone", v: "INACTIVE", c: COLORS.muted },
                    { l: "SMT Div", v: "FORMING", c: COLORS.gold },
                  ].map(m => (
                    <div key={m.l} style={{ background: COLORS.card2, border: `1px solid ${COLORS.border}`, borderTop: `2px solid ${m.c}44`, borderRadius: 6, padding: "8px 12px" }}>
                      <div style={{ fontFamily: "monospace", fontSize: 9, color: COLORS.muted, textTransform: "uppercase", letterSpacing: "0.08em" }}>{m.l}</div>
                      <div style={{ fontFamily: "monospace", fontSize: 12, fontWeight: 700, color: m.c, marginTop: 3 }}>{m.v}</div>
                    </div>
                  ))}
                </div>
              </div>

              {/* AI Briefing */}
              <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, borderLeft: `2px solid ${COLORS.purple}`, padding: 16, marginBottom: 12 }}>
                <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.purple, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 10 }}>AI Briefing</div>
                <pre style={{ fontFamily: "monospace", fontSize: 11, color: COLORS.text, lineHeight: 1.7, whiteSpace: "pre-wrap", margin: 0 }}>
{`[${session}] ${amdPhase} phase | RANGING market | NEUTRAL bias.

Current conditions: Asia session with normal volatility. No active kill zones.
SMT divergence detected between XAUUSD and XAGUSD spreads.

Recommended strategy:
• Priority setups: FVG fills and equal H/L sweeps
• Base risk: 1.5% per trade (reduced for Asia session)
• Min RR: 2.0:1 (ranging market demands higher threshold)
• Max open trades: 3
• Skip: MANIPULATION phase trades until London open

Key levels to watch at London open (07:00 UTC):
• XAUUSD Asia high/low sweep probability: HIGH
• EURUSD equal lows cluster near 1.0820 — potential long trigger
• GBPUSD bearish FVG at 1.2740 — short entry on retest

ICT Concepts active:
• Silver Bullet window: 10:00–11:00 UTC (London)
• NY Midnight Open: 00:00 UTC (watch for gap fills)
• NY 8:30 Open: high-probability continuation setups`}
                </pre>
              </div>

              {/* Signal evaluation */}
              <div style={{ background: COLORS.card, borderRadius: 10, border: `1px solid ${COLORS.border}`, padding: 16 }}>
                <div style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.gold, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 10 }}>Signal Evaluation ({SYMBOLS.length} symbols)</div>
                {SYMBOLS.map(sym => {
                  const a = analysisMap[sym];
                  if (!a) return null;
                  const verdict = a.score >= 70 && a.direction !== "NEUTRAL" ? (a.direction === "BUY" ? "BUY" : "SELL") : "NEUTRAL";
                  const vC = verdict === "BUY" ? COLORS.green : verdict === "SELL" ? COLORS.red : COLORS.muted;
                  return (
                    <div key={sym} style={{ display: "flex", alignItems: "center", gap: 12, padding: "7px 0", borderBottom: `1px solid ${COLORS.border}` }}>
                      <span style={{ fontFamily: "monospace", fontSize: 12, color: dirCol(a.direction), fontWeight: 700, minWidth: 90 }}>{sym} {a.direction}</span>
                      <span style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.muted, minWidth: 60 }}>ICT_SCAN</span>
                      <span style={{ fontFamily: "monospace", fontSize: 11, color: vC, fontWeight: 700, minWidth: 80 }}>{verdict}</span>
                      <span style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.purple, minWidth: 40 }}>{a.score}%</span>
                      <span style={{ fontFamily: "monospace", fontSize: 10, color: COLORS.muted, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.reasons[0]}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

        </div>
      </div>
    </div>
  );
}
