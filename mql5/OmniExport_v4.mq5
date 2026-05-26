//+------------------------------------------------------------------+
//| OmniExport v4.2 — Multi-TF Export + Auto-Trade Execution        |
//| Exports: D1/H4/H1/M30/M15/M5/M1 bars, key levels               |
//| Added:   spread/bid/ask per chart symbol, NDOG/NWOG gaps,       |
//|          full gmt_time with date for Python kill-zone detection  |
//+------------------------------------------------------------------+
#property copyright "OMNI ICT Auto-Trader"
#property version   "4.20"
#property strict

input int    UpdateSeconds   = 5;
input string DataFile        = "omni_data.json";
input string CmdFile         = "omni_cmd.txt";
input string ResultFile      = "omni_result.txt";
input int    MagicNumber     = 20250411;
input bool   AutoTradeEnabled = false;   // 必须设为 true 才会启用实盘交易
// Leader-election: when the EA is attached to multiple charts only one
// instance does the export — others stand down and just refresh their
// claim if the leader dies. Set to false to disable (not recommended).
input bool   LeaderElection  = true;
input int    LeaderHeartbeatSec = 15;     // 主导实例心跳间隔；超过 3 倍视为失效

string LeaderFile  = "omni_leader.lock";
bool   _isLeader   = false;
datetime _lastLeaderHeartbeat = 0;

// Priority symbols — full multi-TF bar export
// Kept to 7 core symbols so total file stays under Wine's 4MB write limit
string primarySymbols[] = {
   "XAUUSD","XAGUSD",
   "EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD"
};

// All watchlist symbols — price-only scan (bid/ask/RSI/OB/FVG)
// Trimmed to keep total file under 4MB (Wine FILE_COMMON write limit)
string allSymbols[] = {
   // ── Core pairs ────────────────────────────────────────────────
   "XAUUSD","XAGUSD",
   "EURUSD","GBPUSD","USDJPY","USDCHF","USDCAD","AUDUSD","NZDUSD",
   "EURGBP","EURJPY","GBPJPY","AUDJPY",
   // ── Crypto top 4 ──────────────────────────────────────────────
   "BTCUSD","ETHUSD","XRPUSD","SOLUSD",
   // ── Indices ───────────────────────────────────────────────────
   ".US30Cash",".USTECHCash",".US500Cash",
   // ── Energy ────────────────────────────────────────────────────
   "USOIL","UKOIL"
};

int  OnInit()
  {
   // Subscribe all needed symbols so Market Watch feeds them price data.
   // Without this, SymbolInfoDouble returns 0 for any symbol not already
   // visible in Market Watch and the EA silently skips it.
   int nPrim = ArraySize(primarySymbols);
   int nAll  = ArraySize(allSymbols);
   for(int i = 0; i < nPrim; i++) SymbolSelect(primarySymbols[i], true);
   for(int i = 0; i < nAll;  i++) SymbolSelect(allSymbols[i],     true);

   EventSetTimer(UpdateSeconds);
   if(LeaderElection)
     {
      _isLeader = TryAcquireLeadership();
      if(_isLeader)
        {
         Print("OmniExport：当前图表实例为主导实例（chart=", _Symbol, ",", PeriodToString(_Period), ")");
         ExportData();
        }
      else
        {
         Print("OmniExport：其他图表实例正在主导导出，本图表待命；主导实例失效后才会接管。（chart=",
               _Symbol, ",", PeriodToString(_Period), ")");
        }
     }
   else
     {
      _isLeader = true;  // legacy mode — every instance runs (will collide!)
      ExportData();
     }
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int r)
  {
   EventKillTimer();
   if(_isLeader)
     {
      // Release leadership on graceful exit so a fresh chart can pick up
      FileDelete(LeaderFile, FILE_COMMON);
      Print("OmniExport：已释放主导权");
     }
  }

void OnTimer()
  {
   if(LeaderElection)
     {
      if(_isLeader)
        {
         RefreshLeadership();
        }
      else
        {
         // Try to take over if current leader has gone stale
         if(LeaderIsStale())
           {
            _isLeader = TryAcquireLeadership();
            if(_isLeader) Print("OmniExport：检测到主导实例超时，正在接管（",
                                _Symbol, ",", PeriodToString(_Period), ")");
           }
        }
      if(!_isLeader) return;   // standby instances do nothing else
     }
   ExportData();
   if(AutoTradeEnabled) CheckCommands();
  }

void OnTick() {}

string PeriodToString(ENUM_TIMEFRAMES tf)
  {
   if(tf==PERIOD_M1)  return "M1";
   if(tf==PERIOD_M5)  return "M5";
   if(tf==PERIOD_M15) return "M15";
   if(tf==PERIOD_M30) return "M30";
   if(tf==PERIOD_H1)  return "H1";
   if(tf==PERIOD_H4)  return "H4";
   if(tf==PERIOD_D1)  return "D1";
   if(tf==PERIOD_W1)  return "W1";
   return EnumToString(tf);
  }

//+------------------------------------------------------------------+
// LEADER ELECTION — file-based, cooperative
//
// Lock file format (single line):  "<chartId>|<unix_ts>"
//   chartId  = symbol#tf (e.g. "EURUSD#M5") to identify which chart owns the lock
//   unix_ts  = TimeGMT() of last heartbeat
//
// A leader writes the file every LeaderHeartbeatSec.
// A standby instance checks the file every OnTimer; if no file exists OR
// the timestamp is older than 3× heartbeat, it tries to claim leadership.
//+------------------------------------------------------------------+
string MyChartId() { return _Symbol + "#" + PeriodToString(_Period); }

bool TryAcquireLeadership()
  {
   // Read existing lock to see if a fresh leader exists
   if(FileIsExist(LeaderFile, FILE_COMMON))
     {
      int rh = FileOpen(LeaderFile, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
      if(rh != INVALID_HANDLE)
        {
         string content = FileReadString(rh);
         FileClose(rh);
         string parts[];
         if(StringSplit(content, '|', parts) >= 2)
           {
            string ownerId  = parts[0];
            datetime stamp  = (datetime)StringToInteger(parts[1]);
            if((TimeGMT() - stamp) < LeaderHeartbeatSec * 3 && ownerId != MyChartId())
              {
               // Leader is alive and it's not us
               return false;
              }
           }
        }
     }
   return WriteLeaderFile();
  }

void RefreshLeadership()
  {
   if((TimeGMT() - _lastLeaderHeartbeat) >= LeaderHeartbeatSec)
      WriteLeaderFile();
  }

bool LeaderIsStale()
  {
   if(!FileIsExist(LeaderFile, FILE_COMMON)) return true;
   int rh = FileOpen(LeaderFile, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(rh == INVALID_HANDLE) return false;  // can't read; assume someone has it
   string content = FileReadString(rh);
   FileClose(rh);
   string parts[];
   if(StringSplit(content, '|', parts) < 2) return true;
   datetime stamp = (datetime)StringToInteger(parts[1]);
   return (TimeGMT() - stamp) >= LeaderHeartbeatSec * 3;
  }

bool WriteLeaderFile()
  {
   int wh = FileOpen(LeaderFile, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(wh == INVALID_HANDLE) return false;
   FileWriteString(wh, MyChartId() + "|" + IntegerToString((long)TimeGMT()));
   FileClose(wh);
   _lastLeaderHeartbeat = TimeGMT();
   return true;
  }

//+------------------------------------------------------------------+
// HELPERS
//+------------------------------------------------------------------+
string Q(string s)   { return "\"" + s + "\""; }
string KV(string k, string v, bool last=false)  { return "\""+k+"\":\""+v+"\""+(last?"":","); }
string KVN(string k, string v, bool last=false) { return "\""+k+"\":"+v+(last?"":","); }

string GetSession()
  {
   MqlDateTime t; TimeToStruct(TimeGMT(),t); int h=t.hour;
   if(h>=22||h<7)  return "ASIA";
   if(h>=7&&h<12)  return "LONDON";
   if(h>=12&&h<17) return "NEW_YORK";
   return "OVERLAP";
  }

string GetAMDPhase()
  {
   MqlDateTime t; TimeToStruct(TimeGMT(),t);
   int tot=t.hour*60+t.min;
   if(t.hour>=22||t.hour<7) return "ACCUMULATION";
   if(tot>=420&&tot<570)     return "MANIPULATION";
   if(tot>=720&&tot<900)     return "DISTRIBUTION";
   if(tot>=900&&tot<1020)    return "LONDON_CLOSE";
   return "REBALANCE";
  }

// Write N bars directly to open file handle — O(n), no growing string
// Writes:  "tfName":[{...},{...},...]
void WriteBarsToFile(int fh, string sym, ENUM_TIMEFRAMES tf, int count, string tfName)
  {
   FileWriteString(fh, "\""+tfName+"\":[");
   MqlRates rates[];
   int copied = CopyRates(sym, tf, 0, count, rates);
   if(copied <= 0) { FileWriteString(fh, "]"); return; }
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   // Write oldest → newest (reverse of CopyRates order)
   for(int i = copied-1; i >= 0; i--)
     {
      // Build one small per-bar string — always O(1), never grows
      string bar = "{\"t\":\"" + TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS) + "\","
                 + "\"o\":" + DoubleToString(rates[i].open,  digits) + ","
                 + "\"h\":" + DoubleToString(rates[i].high,  digits) + ","
                 + "\"l\":" + DoubleToString(rates[i].low,   digits) + ","
                 + "\"c\":" + DoubleToString(rates[i].close, digits) + ","
                 + "\"v\":" + IntegerToString(rates[i].tick_volume)  + "}";
      if(i > 0) bar += ",";
      FileWriteString(fh, bar);
     }
   FileWriteString(fh, "]");
  }

// Get previous day/week high-low
void GetKeyLevels(string sym, int digits,
                  double &pdh, double &pdl, double &pwh, double &pwl,
                  double &pmh, double &pml, double &wdh, double &wdl)
  {
   MqlRates d1rates[], wrates[];
   // Previous Day
   if(CopyRates(sym,PERIOD_D1,1,2,d1rates)>=2)
     { pdh=d1rates[0].high; pdl=d1rates[0].low; wdh=d1rates[1].high; wdl=d1rates[1].low; }
   // Previous Week
   if(CopyRates(sym,PERIOD_W1,1,1,wrates)>=1)
     { pwh=wrates[0].high; pwl=wrates[0].low; }
   // Previous Month
   MqlRates mrates[];
   if(CopyRates(sym,PERIOD_MN1,1,1,mrates)>=1)
     { pmh=mrates[0].high; pml=mrates[0].low; }
  }

//+------------------------------------------------------------------+
// MAIN EXPORT — writes directly to file, never builds a large string
// O(n) per bar instead of O(n²) string concatenation
//+------------------------------------------------------------------+
void ExportData()
  {
   // Write directly to the final file — Wine's FileMove/rename is unreliable
   // on macOS and causes persistent .tmp lock errors (err=5004).
   // Python handles stale reads via its own retry/last-good-cache logic.
   int fh = INVALID_HANDLE;
   for(int attempt = 0; attempt < 3; attempt++)
     {
      fh = FileOpen(DataFile, FILE_WRITE|FILE_REWRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ);
      if(fh != INVALID_HANDLE) break;
      Sleep(50);
     }
   if(fh==INVALID_HANDLE)
     {
      Print("连续 3 次无法打开数据文件（chart=",
            _Symbol, ",", PeriodToString(_Period), ", err=", GetLastError(), ")");
      return;
     }

   string gmtStr = TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS);
   string session = GetSession();
   string amd     = GetAMDPhase();

   // ── Header ───────────────────────────────────────────────────────
   FileWriteString(fh, "{\n");
   FileWriteString(fh, "\"timestamp\":\""+TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS)+"\",\n");
   FileWriteString(fh, "\"session\":\""+session+"\",\n");
   FileWriteString(fh, "\"amd_phase\":\""+amd+"\",\n");
   FileWriteString(fh, "\"gmt_time\":\""+gmtStr+"\",\n");
   FileWriteString(fh, "\"auto_trade_enabled\":"+IntegerToString(AutoTradeEnabled?1:0)+",\n");

   // ── Account ──────────────────────────────────────────────────────
   // AccountInfo* returns empty/0 for ~10-30s after broker connect.
   // We write a "ready" flag so Python knows whether data is valid.
   string  acc_currency = AccountInfoString(ACCOUNT_CURRENCY);
   long    acc_leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);
   double  acc_balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   // Account is ready when currency is populated AND leverage is non-zero.
   // Balance CAN legitimately be 0 on a new/empty account, so don't gate on it.
   bool acc_ready = (StringLen(acc_currency) > 0 && acc_leverage > 0);
   if(!acc_ready)
      Print("OmniExport：账户数据尚未同步（currency='",acc_currency,
            "' leverage=",acc_leverage,"），经纪商响应前暂写入 0 值");
   FileWriteString(fh, "\"account\":{\n");
   FileWriteString(fh, "\"ready\":"    +(acc_ready?"true":"false")                                  +",\n");
   FileWriteString(fh, "\"login\":"    +IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))          +",\n");
   FileWriteString(fh, "\"name\":\""   +AccountInfoString(ACCOUNT_NAME)                             +"\",\n");
   FileWriteString(fh, "\"server\":\"" +AccountInfoString(ACCOUNT_SERVER)                           +"\",\n");
   FileWriteString(fh, "\"currency\":\""+acc_currency                                               +"\",\n");
   FileWriteString(fh, "\"balance\":"  +DoubleToString(acc_balance,2)                               +",\n");
   FileWriteString(fh, "\"equity\":"   +DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2)         +",\n");
   FileWriteString(fh, "\"margin\":"   +DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN),2)         +",\n");
   FileWriteString(fh, "\"free_margin\":"+DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2)  +",\n");
   FileWriteString(fh, "\"margin_level\":"+DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),2)+",\n");
   FileWriteString(fh, "\"profit\":"   +DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT),2)         +",\n");
   FileWriteString(fh, "\"leverage\":" +IntegerToString(acc_leverage)                               +"\n");
   FileWriteString(fh, "},\n");

   // ── Open Positions ────────────────────────────────────────────────
   FileWriteString(fh, "\"positions\":[\n");
   int total = PositionsTotal(); bool firstPos = true;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i); if(ticket==0) continue;
      if(!firstPos) FileWriteString(fh, ",\n"); firstPos = false;
      FileWriteString(fh,
         "{\"ticket\":"    +IntegerToString(ticket)
        +",\"symbol\":\""  +PositionGetString(POSITION_SYMBOL)+"\""
        +",\"type\":\""    +(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?"BUY":"SELL")+"\""
        +",\"volume\":"    +DoubleToString(PositionGetDouble(POSITION_VOLUME),2)
        +",\"open_price\":"+DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),5)
        +",\"current_price\":"+DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),5)
        +",\"sl\":"        +DoubleToString(PositionGetDouble(POSITION_SL),5)
        +",\"tp\":"        +DoubleToString(PositionGetDouble(POSITION_TP),5)
        +",\"profit\":"    +DoubleToString(PositionGetDouble(POSITION_PROFIT),2)
        +",\"swap\":"      +DoubleToString(PositionGetDouble(POSITION_SWAP),2)
        +",\"magic\":"     +IntegerToString(PositionGetInteger(POSITION_MAGIC))
        +",\"time\":\""    +TimeToString((datetime)PositionGetInteger(POSITION_TIME),TIME_DATE|TIME_SECONDS)+"\"}"
      );
     }
   FileWriteString(fh, "\n],\n");

   // ── History ───────────────────────────────────────────────────────
   FileWriteString(fh, "\"history\":[\n");
   HistorySelect(TimeCurrent()-90*86400, TimeCurrent());
   int deals = HistoryDealsTotal(); bool firstDeal = true;
   for(int i = 0; i < deals; i++)
     {
      ulong ticket = HistoryDealGetTicket(i); if(ticket==0) continue;
      long dtype = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(dtype!=DEAL_TYPE_BUY && dtype!=DEAL_TYPE_SELL) continue;
      long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(!firstDeal) FileWriteString(fh, ",\n"); firstDeal = false;
      FileWriteString(fh,
         "{\"ticket\":"   +IntegerToString(ticket)
        +",\"time\":\""   +TimeToString((datetime)HistoryDealGetInteger(ticket,DEAL_TIME),TIME_DATE|TIME_SECONDS)+"\""
        +",\"symbol\":\"" +HistoryDealGetString(ticket,DEAL_SYMBOL)+"\""
        +",\"type\":\""   +(dtype==DEAL_TYPE_BUY?"BUY":"SELL")+"\""
        +",\"entry\":\""  +(entry==DEAL_ENTRY_IN?"IN":(entry==DEAL_ENTRY_OUT?"OUT":"INOUT"))+"\""
        +",\"volume\":"   +DoubleToString(HistoryDealGetDouble(ticket,DEAL_VOLUME),2)
        +",\"price\":"    +DoubleToString(HistoryDealGetDouble(ticket,DEAL_PRICE),5)
        +",\"profit\":"   +DoubleToString(HistoryDealGetDouble(ticket,DEAL_PROFIT),2)
        +",\"swap\":"     +DoubleToString(HistoryDealGetDouble(ticket,DEAL_SWAP),2)
        +",\"commission\":"+DoubleToString(HistoryDealGetDouble(ticket,DEAL_COMMISSION),2)+"}"
      );
     }
   FileWriteString(fh, "\n],\n");

   // ── Multi-TF bars for primary symbols — all written direct to file ─
   // Bar counts: request maximum; CopyRates returns only what MT5 has
   FileWriteString(fh, "\"charts\":{\n");
   int primCount = ArraySize(primarySymbols);
   bool firstSym = true;
   for(int i = 0; i < primCount; i++)
     {
      string sym = primarySymbols[i];
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      if(bid == 0.0) continue;
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double pdh=0,pdl=0,pwh=0,pwl=0,pmh=0,pml=0,wdh=0,wdl=0;
      GetKeyLevels(sym, digits, pdh, pdl, pwh, pwl, pmh, pml, wdh, wdl);

      if(!firstSym) FileWriteString(fh, ",\n"); firstSym = false;
      FileWriteString(fh, "\""+sym+"\":{\n");

      // ── 7 timeframes — deep history for EMA800 accuracy on all TFs ──
      // LIMIT orders = scan speed NOT critical. 5+ trading days per TF:
      // M1=4000 (~5.5d)  M5=1800 (~6d)  M15=600 (~6d)  M30=400 (~8d)
      // H1=300 (~12d)   H4=100 (~16d)   D1=60 (~2mo)
      WriteBarsToFile(fh, sym, PERIOD_D1,   60, "D1");  FileWriteString(fh, ",\n");
      WriteBarsToFile(fh, sym, PERIOD_H4,  100, "H4");  FileWriteString(fh, ",\n");
      WriteBarsToFile(fh, sym, PERIOD_H1,  300, "H1");  FileWriteString(fh, ",\n");
      WriteBarsToFile(fh, sym, PERIOD_M30, 400, "M30"); FileWriteString(fh, ",\n");
      WriteBarsToFile(fh, sym, PERIOD_M15, 600, "M15"); FileWriteString(fh, ",\n");
      WriteBarsToFile(fh, sym, PERIOD_M5,  1800, "M5");  FileWriteString(fh, ",\n");
      WriteBarsToFile(fh, sym, PERIOD_M1,  4000, "M1");  FileWriteString(fh, ",\n");

      // Live bid/ask/spread
      double cask = SymbolInfoDouble(sym, SYMBOL_ASK);
      double cpt  = SymbolInfoDouble(sym, SYMBOL_POINT);
      int    cspd = (cpt>0)?(int)MathRound((cask-bid)/cpt):0;
      FileWriteString(fh, "\"bid\":"    +DoubleToString(bid, digits) +",\n");
      FileWriteString(fh, "\"ask\":"    +DoubleToString(cask,digits) +",\n");
      FileWriteString(fh, "\"spread\":" +IntegerToString(cspd)        +",\n");

      // NDOG / NWOG opening gaps
      MqlRates dRates[2], wRates[2];
      double ndog_open=0,ndog_close=0,nwog_open=0,nwog_close=0;
      if(CopyRates(sym,PERIOD_D1,0,2,dRates)==2)
        { ndog_close=dRates[1].close; ndog_open=dRates[0].open; }
      if(CopyRates(sym,PERIOD_W1,0,2,wRates)==2)
        { nwog_close=wRates[1].close; nwog_open=wRates[0].open; }
      FileWriteString(fh, "\"ndog_close\":"+DoubleToString(ndog_close,digits)+",\n");
      FileWriteString(fh, "\"ndog_open\":" +DoubleToString(ndog_open, digits)+",\n");
      FileWriteString(fh, "\"nwog_close\":"+DoubleToString(nwog_close,digits)+",\n");
      FileWriteString(fh, "\"nwog_open\":" +DoubleToString(nwog_open, digits)+",\n");

      // Symbol info for lot sizing
      FileWriteString(fh, "\"tick_size\":"    +DoubleToString(SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_SIZE),8)    +",\n");
      FileWriteString(fh, "\"tick_value\":"   +DoubleToString(SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE),8)   +",\n");
      FileWriteString(fh, "\"contract_size\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_TRADE_CONTRACT_SIZE),2)+",\n");
      FileWriteString(fh, "\"point\":"        +DoubleToString(SymbolInfoDouble(sym,SYMBOL_POINT),8)              +",\n");
      FileWriteString(fh, "\"min_lot\":"      +DoubleToString(SymbolInfoDouble(sym,SYMBOL_VOLUME_MIN),2)         +",\n");
      FileWriteString(fh, "\"max_lot\":"      +DoubleToString(SymbolInfoDouble(sym,SYMBOL_VOLUME_MAX),2)         +",\n");
      FileWriteString(fh, "\"lot_step\":"     +DoubleToString(SymbolInfoDouble(sym,SYMBOL_VOLUME_STEP),2)        +",\n");

      // Key levels
      FileWriteString(fh, "\"pdh\":"+DoubleToString(pdh,digits)+",\n");
      FileWriteString(fh, "\"pdl\":"+DoubleToString(pdl,digits)+",\n");
      FileWriteString(fh, "\"pwh\":"+DoubleToString(pwh,digits)+",\n");
      FileWriteString(fh, "\"pwl\":"+DoubleToString(pwl,digits)+",\n");
      FileWriteString(fh, "\"pmh\":"+DoubleToString(pmh,digits)+",\n");
      FileWriteString(fh, "\"pml\":"+DoubleToString(pml,digits)+"\n");
      FileWriteString(fh, "}");
     }
   FileWriteString(fh, "\n},\n");

   // ── All symbol prices + ICT signals ──────────────────────────────
   FileWriteString(fh, "\"prices\":[\n");
   int symCount = ArraySize(allSymbols); bool firstPrice = true;
   for(int i = 0; i < symCount; i++)
     {
      string sym   = allSymbols[i];
      double bid   = SymbolInfoDouble(sym, SYMBOL_BID);
      double ask   = SymbolInfoDouble(sym, SYMBOL_ASK);
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      int    digits= (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      if(bid == 0.0) continue;
      int spread = (point>0)?(int)MathRound((ask-bid)/point):0;

      // EMA + RSI from H1 bars — no indicator handles needed
      MqlRates h1bars[];
      int nbars = CopyRates(sym, PERIOD_H1, 0, 210, h1bars);
      double rsi=50.0, ma20=bid, ma50=bid, ma200=bid;
      if(nbars >= 200)
        {
         double k20=2.0/21.0, k50=2.0/51.0, k200=2.0/201.0;
         double e20=h1bars[0].close, e50=h1bars[0].close, e200=h1bars[0].close;
         for(int b=1;b<nbars;b++)
           { e20=h1bars[b].close*k20+e20*(1-k20);
             e50=h1bars[b].close*k50+e50*(1-k50);
             e200=h1bars[b].close*k200+e200*(1-k200); }
         ma20=e20; ma50=e50; ma200=e200;
         double gains=0, losses=0; int rlen=14;
         for(int b=1;b<=rlen&&b<nbars;b++)
           { double d=h1bars[nbars-b].close-h1bars[nbars-b-1].close;
             if(d>0)gains+=d; else losses-=d; }
         double ag=gains/rlen, al=losses/rlen;
         rsi = (al==0)?100.0:100.0-100.0/(1.0+ag/al);
        }

      string trend  = "NEUTRAL";
      if(bid>ma20&&ma20>ma50&&ma50>ma200)      trend="BULLISH";
      else if(bid<ma20&&ma20<ma50&&ma50<ma200) trend="BEARISH";
      string rsiSig = (rsi<30)?"OVERSOLD":(rsi>70)?"OVERBOUGHT":"NEUTRAL";

      double h0=iHigh(sym,PERIOD_H1,0), l0=iLow(sym,PERIOD_H1,0);
      double h2=iHigh(sym,PERIOD_H1,2), l2=iLow(sym,PERIOD_H1,2);
      string fvg="NONE"; double fvgH=0, fvgL=0;
      if(l0>h2){fvg="BULLISH";fvgH=l0;fvgL=h2;}
      else if(h0<l2){fvg="BEARISH";fvgH=l2;fvgL=h0;}

      string obType="NONE"; double obH=0, obL=0;
      for(int b=2;b<10;b++)
        {
         double cb=iClose(sym,PERIOD_H1,b),  ob_=iOpen(sym,PERIOD_H1,b);
         double cb1=iClose(sym,PERIOD_H1,b-1),ob1=iOpen(sym,PERIOD_H1,b-1);
         if(cb<ob_&&cb1>ob1&&(cb1-ob1)>(ob_-cb)*1.5)
           {obType="BULLISH_OB";obH=iHigh(sym,PERIOD_H1,b);obL=iLow(sym,PERIOD_H1,b);break;}
         if(cb>ob_&&cb1<ob1&&(ob1-cb1)>(cb-ob_)*1.5)
           {obType="BEARISH_OB";obH=iHigh(sym,PERIOD_H1,b);obL=iLow(sym,PERIOD_H1,b);break;}
        }

      double swH=0, swL=999999;
      for(int b=1;b<=20;b++)
        { double hh=iHigh(sym,PERIOD_H1,b), ll=iLow(sym,PERIOD_H1,b);
          if(hh>swH)swH=hh; if(ll<swL)swL=ll; }

      double asH=0, asL=999999;
      for(int b=0;b<48;b++)
        { datetime bt=iTime(sym,PERIOD_H1,b); MqlDateTime bd; TimeToStruct(bt,bd);
          if(bd.hour>=22||bd.hour<7)
            { double bh=iHigh(sym,PERIOD_H1,b), bl=iLow(sym,PERIOD_H1,b);
              if(bh>asH)asH=bh; if(bl<asL)asL=bl; } }
      if(asL==999999) asL=0;

      string structSig="RANGING";
      if(bid>swH)                              structSig="BOS_BULLISH";
      else if(bid<swL)                         structSig="BOS_BEARISH";
      else if(trend=="BULLISH"&&rsi<40)        structSig="CHOCH_POTENTIAL_BULL";
      else if(trend=="BEARISH"&&rsi>60)        structSig="CHOCH_POTENTIAL_BEAR";

      if(!firstPrice) FileWriteString(fh, ",\n"); firstPrice = false;
      FileWriteString(fh,
         "{\"symbol\":\""    +sym+"\""
        +",\"bid\":"         +DoubleToString(bid,digits)
        +",\"ask\":"         +DoubleToString(ask,digits)
        +",\"spread\":"      +IntegerToString(spread)
        +",\"rsi\":"         +DoubleToString(rsi,2)
        +",\"ma20\":"        +DoubleToString(ma20,digits)
        +",\"ma50\":"        +DoubleToString(ma50,digits)
        +",\"ma200\":"       +DoubleToString(ma200,digits)
        +",\"trend\":\""     +trend+"\""
        +",\"rsi_signal\":\""+rsiSig+"\""
        +",\"fvg_type\":\""  +fvg+"\""
        +",\"fvg_high\":"    +DoubleToString(fvgH,digits)
        +",\"fvg_low\":"     +DoubleToString(fvgL,digits)
        +",\"ob_type\":\""   +obType+"\""
        +",\"ob_high\":"     +DoubleToString(obH,digits)
        +",\"ob_low\":"      +DoubleToString(obL,digits)
        +",\"swing_high\":"  +DoubleToString(swH,digits)
        +",\"swing_low\":"   +DoubleToString(swL,digits)
        +",\"asia_high\":"   +DoubleToString(asH,digits)
        +",\"asia_low\":"    +DoubleToString(asL,digits)
        +",\"structure\":\""  +structSig+"\"}"
      );
     }
   FileWriteString(fh, "\n]\n}\n");
   FileClose(fh);
   // File already written directly — no rename needed on Wine/macOS.
   Print("OmniExport v4：数据已更新 | ",amd," | ",session);
  }

//+------------------------------------------------------------------+
// COMMAND EXECUTOR
// Command format (pipe-delimited):
// OPEN|SYMBOL|BUY_LIMIT|PRICE|SL|TP|VOLUME|COMMENT
// OPEN|SYMBOL|SELL_LIMIT|PRICE|SL|TP|VOLUME|COMMENT
// OPEN|SYMBOL|BUY|0|SL|TP|VOLUME|COMMENT  (market order)
// CLOSE|TICKET|SYMBOL||||VOLUME|
// MODIFY|TICKET|SYMBOL||SL|TP||
//+------------------------------------------------------------------+
void CheckCommands()
  {
   if(!FileIsExist(CmdFile,FILE_COMMON)) return;
   int fh=FileOpen(CmdFile,FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh==INVALID_HANDLE) return;
   string cmd="";
   while(!FileIsEnding(fh)) cmd+=FileReadString(fh);
   FileClose(fh);
   FileDelete(CmdFile,FILE_COMMON);
   if(StringLen(cmd)<5) return;
   Print("OmniExport：收到交易命令：",cmd);
   string result=ProcessCommand(cmd);
   // Write result
   int rf=FileOpen(ResultFile,FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(rf!=INVALID_HANDLE){FileWriteString(rf,result);FileClose(rf);}
  }

string ProcessCommand(string cmd)
  {
   string parts[];
   int n=StringSplit(cmd,'|',parts);
   if(n<2) return "ERROR|命令格式错误";
   string action=parts[0];

   if(action=="OPEN" && n>=8)
     {
      string sym   =parts[1];
      string type  =parts[2];
      double price =StringToDouble(parts[3]);
      double sl    =StringToDouble(parts[4]);
      double tp    =StringToDouble(parts[5]);
      double vol   =StringToDouble(parts[6]);
      string comment=parts[7];
      ENUM_ORDER_TYPE ot;
      bool isPending=true;
      if(type=="BUY")        {ot=ORDER_TYPE_BUY;      isPending=false;}
      else if(type=="SELL")  {ot=ORDER_TYPE_SELL;     isPending=false;}
      else if(type=="BUY_LIMIT")  ot=ORDER_TYPE_BUY_LIMIT;
      else if(type=="SELL_LIMIT") ot=ORDER_TYPE_SELL_LIMIT;
      else if(type=="BUY_STOP")   ot=ORDER_TYPE_BUY_STOP;
      else if(type=="SELL_STOP")  ot=ORDER_TYPE_SELL_STOP;
      else return "ERROR|未知订单类型";
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action   =isPending?TRADE_ACTION_PENDING:TRADE_ACTION_DEAL;
      req.symbol   =sym; req.volume=vol; req.type=ot;
      req.price    =isPending?price:SymbolInfoDouble(sym,ot==ORDER_TYPE_BUY?SYMBOL_ASK:SYMBOL_BID);
      req.sl=sl; req.tp=tp; req.deviation=500;  // XAUUSD: 500 pts = $5.00 (was $0.30)
      req.magic    =MagicNumber; req.comment=comment;
      req.type_filling=ORDER_FILLING_IOC;
      if(OrderSend(req,res))
         return "OK|"+IntegerToString(res.order)+"|"+IntegerToString(res.deal)+"|已开仓 "+type+" "+sym+" 手数="+DoubleToString(vol,2)+" 成交价="+DoubleToString(res.price,5);
      else
         return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   if(action=="CLOSE" && n>=3)
     {
      ulong ticket=StringToInteger(parts[1]);
      if(!PositionSelectByTicket(ticket)) return "ERROR|未找到持仓";
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action  =TRADE_ACTION_DEAL;
      req.symbol  =PositionGetString(POSITION_SYMBOL);
      req.volume  =n>=7&&StringLen(parts[6])>0?StringToDouble(parts[6]):PositionGetDouble(POSITION_VOLUME);
      req.type    =PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?ORDER_TYPE_SELL:ORDER_TYPE_BUY;
      req.price   =PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?SymbolInfoDouble(req.symbol,SYMBOL_BID):SymbolInfoDouble(req.symbol,SYMBOL_ASK);
      req.deviation=30; req.magic=MagicNumber; req.comment="OMNI_CLOSE";
      req.type_filling=ORDER_FILLING_IOC;
      if(OrderSend(req,res)) return "OK|已平仓 ticket="+IntegerToString(ticket);
      else return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   if(action=="MODIFY" && n>=6)
     {
      ulong ticket=StringToInteger(parts[1]);
      double newSL=StringToDouble(parts[4]);
      double newTP=StringToDouble(parts[5]);
      if(!PositionSelectByTicket(ticket)) return "ERROR|未找到持仓";
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action=TRADE_ACTION_SLTP; req.symbol=PositionGetString(POSITION_SYMBOL);
      req.sl=newSL; req.tp=newTP; req.position=ticket;
      if(OrderSend(req,res)) return "OK|已修改 ticket="+IntegerToString(ticket)+" SL="+DoubleToString(newSL,5)+" TP="+DoubleToString(newTP,5);
      else return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   if(action=="CANCEL" && n>=2)
     {
      ulong ticket=StringToInteger(parts[1]);
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action=TRADE_ACTION_REMOVE; req.order=ticket;
      if(OrderSend(req,res)) return "OK|已取消订单="+IntegerToString(ticket);
      else return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   return "ERROR|未知动作: "+action;
  }
