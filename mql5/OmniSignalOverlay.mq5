//+------------------------------------------------------------------+
//|                                          OmniSignalOverlay.mq5 |
//|                                        OMNI ICT autonomous suite |
//|  读取 orchestrator 生成的 signals.json，并在图表上绘制入场、止损、止盈。 |
//|  Python 端采用临时文件 + 重命名写入，避免读取到半写入文件。              |
//+------------------------------------------------------------------+
#property copyright "OMNI ICT"
#property version   "1.00"
#property indicator_chart_window
#property indicator_plots 0
#property strict

//--- inputs ---------------------------------------------------------
input string   InpSignalsFile   = "omni\\signals.json";         // 信号文件，相对于 MQL5/Files 或 Common/Files
input int      InpPollSeconds   = 3;                            // 轮询间隔（秒）
input color    InpBullColor     = clrTeal;                      // 多头入场线颜色
input color    InpBearColor     = clrRed;                       // 空头入场线颜色
input color    InpSLColor       = clrOrangeRed;                 // 止损线颜色
input color    InpTPColor       = clrLimeGreen;                 // 止盈线颜色
input int      InpLineWidth     = 2;                            // 线宽
input int      InpLineLookback  = 80;                           // 向左延伸的K线数量
input bool     InpShowLabels    = true;                         // 显示入场/止损/止盈标签
input string   InpObjPrefix     = "OmniSig_";                   // 图表对象前缀
input bool     InpSymbolFilter  = true;                         // 仅显示当前品种信号
input bool     InpTimeframeFilter = false;                      // 仅显示当前周期信号

//--- internal state -------------------------------------------------
datetime g_last_poll   = 0;
long     g_last_mtime  = 0;   // reserved (MQL5 has no portable mtime; we re-read each cycle)
string   g_last_hash   = "";  // cheap change-detection via content length + first/last char

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(MathMax(1, InpPollSeconds));
   // First render on load.
   PollAndRender();
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Deinit                                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   ClearAllObjects();
}

//+------------------------------------------------------------------+
//| Per-tick (cheap): just update rightmost line anchor if needed    |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   return(rates_total);
}

//+------------------------------------------------------------------+
//| Timer: re-read signals.json and redraw                           |
//+------------------------------------------------------------------+
void OnTimer()
{
   PollAndRender();
}

//+------------------------------------------------------------------+
//| Read signals.json from MQL5/Files                                |
//+------------------------------------------------------------------+
bool ReadSignalsFile(string &out_contents)
{
   out_contents = "";
   int flags = FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_COMMON;
   int h = FileOpen(InpSignalsFile, flags);
   if(h == INVALID_HANDLE)
   {
      // 如果 Common/Files 未找到，则尝试普通 MQL5/Files 路径
      h = FileOpen(InpSignalsFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
      if(h == INVALID_HANDLE)
         return(false);
   }
   while(!FileIsEnding(h))
   {
      out_contents += FileReadString(h);
      out_contents += "\n";
   }
   FileClose(h);
   return(StringLen(out_contents) > 0);
}

//+------------------------------------------------------------------+
//| Cheap change-detection fingerprint                               |
//+------------------------------------------------------------------+
string Fingerprint(const string &s)
{
   int n = StringLen(s);
   if(n == 0) return("empty");
   string head = StringSubstr(s, 0, MathMin(32, n));
   string tail = StringSubstr(s, MathMax(0, n - 32), 32);
   return(IntegerToString(n) + "|" + head + "|" + tail);
}

//+------------------------------------------------------------------+
//| Poll → read → parse → render                                     |
//+------------------------------------------------------------------+
void PollAndRender()
{
   string body;
   if(!ReadSignalsFile(body))
      return;

   string fp = Fingerprint(body);
   if(fp == g_last_hash)
      return; // no change
   g_last_hash = fp;

   ClearAllObjects();
   ParseAndDraw(body);
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Delete all objects with our prefix                               |
//+------------------------------------------------------------------+
void ClearAllObjects()
{
   int total = ObjectsTotal(0, -1, -1);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, -1, -1);
      if(StringFind(name, InpObjPrefix) == 0)
         ObjectDelete(0, name);
   }
}

//+------------------------------------------------------------------+
//| Tiny JSON field extractors — enough for our flat signal shape.   |
//| signals.json contains:                                           |
//|   { "version":"1.0", "generated_at":"...",                       |
//|     "signals":[ {...}, {...} ], "trail_proposals":[...] }        |
//| Per signal keys we care about:                                   |
//|   id, ts, symbol, timeframe, direction, entry_type, entry_price, |
//|   sl, tp, confidence, reasons, scale_action, scale_mult,         |
//|   htf_bias, source                                               |
//+------------------------------------------------------------------+

// Find the substring of the "signals" array content (between [ and matching ]).
bool ExtractSignalsArray(const string &body, string &arr)
{
   int key = StringFind(body, "\"signals\"");
   if(key < 0) return(false);
   int lb = StringFind(body, "[", key);
   if(lb < 0) return(false);
   int depth = 0;
   for(int i = lb; i < StringLen(body); i++)
   {
      ushort c = StringGetCharacter(body, i);
      if(c == '[') depth++;
      else if(c == ']')
      {
         depth--;
         if(depth == 0)
         {
            arr = StringSubstr(body, lb + 1, i - lb - 1);
            return(true);
         }
      }
   }
   return(false);
}

// Split a JSON array of objects into top-level object strings.
int SplitObjects(const string &arr, string &out[])
{
   ArrayResize(out, 0);
   int depth = 0, start = -1;
   int n = StringLen(arr);
   bool in_str = false;
   ushort prev = 0;
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(arr, i);
      if(in_str)
      {
         if(c == '"' && prev != '\\') in_str = false;
         prev = c;
         continue;
      }
      if(c == '"') { in_str = true; prev = c; continue; }
      if(c == '{')
      {
         if(depth == 0) start = i;
         depth++;
      }
      else if(c == '}')
      {
         depth--;
         if(depth == 0 && start >= 0)
         {
            int sz = ArraySize(out);
            ArrayResize(out, sz + 1);
            out[sz] = StringSubstr(arr, start, i - start + 1);
            start = -1;
         }
      }
      prev = c;
   }
   return(ArraySize(out));
}

// Get a string field value. Returns "" if missing.
string GetStr(const string &obj, const string &key)
{
   string pat = "\"" + key + "\"";
   int k = StringFind(obj, pat);
   if(k < 0) return("");
   int colon = StringFind(obj, ":", k);
   if(colon < 0) return("");
   // Skip whitespace
   int i = colon + 1;
   while(i < StringLen(obj))
   {
      ushort c = StringGetCharacter(obj, i);
      if(c != ' ' && c != '\t' && c != '\n' && c != '\r') break;
      i++;
   }
   if(i >= StringLen(obj)) return("");
   ushort first = StringGetCharacter(obj, i);
   if(first == '"')
   {
      int end = i + 1;
      ushort prev = 0;
      while(end < StringLen(obj))
      {
         ushort c = StringGetCharacter(obj, end);
         if(c == '"' && prev != '\\') break;
         prev = c;
         end++;
      }
      return(StringSubstr(obj, i + 1, end - i - 1));
   }
   // non-string (number/bool/null) — read until , } ] whitespace
   int end = i;
   while(end < StringLen(obj))
   {
      ushort c = StringGetCharacter(obj, end);
      if(c == ',' || c == '}' || c == ']' || c == '\n' || c == '\r') break;
      end++;
   }
   string raw = StringSubstr(obj, i, end - i);
   StringTrimLeft(raw);
   StringTrimRight(raw);
   return(raw);
}

double GetNum(const string &obj, const string &key, double defv)
{
   string s = GetStr(obj, key);
   if(StringLen(s) == 0 || s == "null") return(defv);
   return(StringToDouble(s));
}

//+------------------------------------------------------------------+
//| Parse signals array and draw objects                             |
//+------------------------------------------------------------------+
void ParseAndDraw(const string &body)
{
   string arr;
   if(!ExtractSignalsArray(body, arr))
   {
      Print("OmniSignalOverlay：未找到 signals 数组。");
      return;
   }
   string objs[];
   int n = SplitObjects(arr, objs);
   int drawn = 0;

   string sym = _Symbol;
   ENUM_TIMEFRAMES cur_tf = (ENUM_TIMEFRAMES)_Period;

   for(int i = 0; i < n; i++)
   {
      string o = objs[i];
      string id        = GetStr(o, "id");
      string o_sym     = GetStr(o, "symbol");
      string o_tf      = GetStr(o, "timeframe");
      string dir       = GetStr(o, "direction");
      string ent_type  = GetStr(o, "entry_type");
      double entry     = GetNum(o, "entry_price", 0.0);
      double sl        = GetNum(o, "sl",         0.0);
      double tp        = GetNum(o, "tp",         0.0);
      double conf      = GetNum(o, "confidence", 0.0);

      if(StringLen(id) == 0 || entry == 0.0 || sl == 0.0) continue;

      if(InpSymbolFilter && StringLen(o_sym) > 0 && o_sym != sym) continue;
      if(InpTimeframeFilter && StringLen(o_tf) > 0 && o_tf != TFToString(cur_tf)) continue;

      color c_entry = (dir == "BULL" ? InpBullColor : InpBearColor);

      datetime t_end   = TimeCurrent();
      datetime t_start = t_end - PeriodSeconds(cur_tf) * InpLineLookback;

      string base = InpObjPrefix + id + "_";

      DrawHLineSeg(base + "E", t_start, t_end, entry, c_entry, "入场 " + dir + " " + DoubleToString(conf, 2));
      DrawHLineSeg(base + "S", t_start, t_end, sl,    InpSLColor, "止损");
      if(tp != 0.0)
         DrawHLineSeg(base + "T", t_start, t_end, tp, InpTPColor, "止盈");

      if(InpShowLabels)
      {
         DrawTextLabel(base + "LE", t_end, entry, dir + " " + ent_type + " " + DoubleToString(conf, 2), c_entry);
         DrawTextLabel(base + "LS", t_end, sl,    "止损",                              InpSLColor);
         if(tp != 0.0)
            DrawTextLabel(base + "LT", t_end, tp, "止盈",                              InpTPColor);
      }
      drawn++;
   }
   Comment(StringFormat("OmniSignalOverlay：已绘制 %d 条信号（共解析 %d 条）@ %s",
                        drawn, n, TimeToString(TimeCurrent(), TIME_SECONDS)));
}

//+------------------------------------------------------------------+
//| Draw a horizontal trendline segment from t_start → t_end         |
//+------------------------------------------------------------------+
void DrawHLineSeg(const string name, datetime t_start, datetime t_end,
                  double price, color col, const string tip)
{
   ObjectDelete(0, name);
   if(!ObjectCreate(0, name, OBJ_TREND, 0, t_start, price, t_end, price))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR,      col);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,      InpLineWidth);
   ObjectSetInteger(0, name, OBJPROP_STYLE,      STYLE_SOLID);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT,  false);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT,   false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
   ObjectSetString (0, name, OBJPROP_TOOLTIP,    tip);
}

//+------------------------------------------------------------------+
//| Draw a right-anchored text label at (time, price)                |
//+------------------------------------------------------------------+
void DrawTextLabel(const string name, datetime t, double price,
                   const string text, color col)
{
   ObjectDelete(0, name);
   if(!ObjectCreate(0, name, OBJ_TEXT, 0, t, price))
      return;
   ObjectSetString (0, name, OBJPROP_TEXT,       text);
   ObjectSetInteger(0, name, OBJPROP_COLOR,      col);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE,   9);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR,     ANCHOR_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
}

//+------------------------------------------------------------------+
//| Map ENUM_TIMEFRAMES → short string (M1/M5/H1/H4/D1…) for filter  |
//+------------------------------------------------------------------+
string TFToString(ENUM_TIMEFRAMES tf)
{
   switch(tf)
   {
      case PERIOD_M1:  return("M1");
      case PERIOD_M5:  return("M5");
      case PERIOD_M15: return("M15");
      case PERIOD_M30: return("M30");
      case PERIOD_H1:  return("H1");
      case PERIOD_H4:  return("H4");
      case PERIOD_D1:  return("D1");
      case PERIOD_W1:  return("W1");
      case PERIOD_MN1: return("MN1");
   }
   return("");
}
//+------------------------------------------------------------------+
