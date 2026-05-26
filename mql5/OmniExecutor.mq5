//+------------------------------------------------------------------+
//|  OmniExecutor.mq5 — 执行 auto_trader.py 写入的交易命令           |
//|                                                                  |
//|  命令文件：omni_cmd.txt    （Python 写入，EA 读取）              |
//|  结果文件：omni_result.txt （EA 写入，Python 读取）              |
//|                                                                  |
//|  命令格式：                                                      |
//|    OPEN|SYMBOL|ORDER_TYPE|PRICE|SL|TP|VOLUME|COMMENT            |
//|    CLOSE|TICKET|||||VOLUME|                                      |
//|    MODIFY|TICKET|||SL|TP||                                       |
//|                                                                  |
//|  ORDER_TYPE 可选值：BUY BUY_LIMIT BUY_STOP SELL SELL_LIMIT       |
//|                     SELL_STOP                                    |
//|                                                                  |
//|  挂到任意图表即可，不会影响 OmniExport.mq5。                     |
//+------------------------------------------------------------------+
#property copyright "OMNI Trading Dashboard"
#property version   "1.00"
#property strict

input int    PollMilliseconds = 500;      // 命令轮询间隔（毫秒）
input ulong  MagicNumber      = 20250411; // 必须与 auto_trader.py 的 magic 一致
input int    SlippagePips     = 500;      // 最大允许滑点（点）

string CMD_FILE    = "omni_cmd.txt";
string RESULT_FILE = "omni_result.txt";

int OnInit()
  {
   EventSetMillisecondTimer(PollMilliseconds);
   Print("OmniExecutor 已就绪 | Magic=", MagicNumber, " | 轮询=", PollMilliseconds, "ms");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int r) { EventKillTimer(); }
void OnTick() {}

void OnTimer()
  {
   // 检查命令文件；使用 FILE_COMMON 以匹配 Python 写入路径
   if(!FileIsExist(CMD_FILE, FILE_COMMON)) return;

   int fh = FileOpen(CMD_FILE, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) return;
   string cmd = FileReadString(fh);
   FileClose(fh);

   // 执行前删除命令文件，避免崩溃/重试时重复执行
   FileDelete(CMD_FILE, FILE_COMMON);

   string trimmed = cmd;
   StringTrimLeft(trimmed);
   StringTrimRight(trimmed);
   if(StringLen(trimmed) == 0) return;

   string result = ProcessCommand(cmd);
   WriteResult(result);
  }

//+------------------------------------------------------------------+
//| Route command to appropriate handler                             |
//+------------------------------------------------------------------+
string ProcessCommand(string cmd)
  {
   string parts[];
   int n = StringSplit(cmd, '|', parts);
   if(n < 1) return "ERROR|空命令";

   string action = parts[0];

   if(action == "OPEN"   && n >= 8) return CmdOpen(parts);
   if(action == "CLOSE"  && n >= 2) return CmdClose(parts);
   if(action == "MODIFY" && n >= 6) return CmdModify(parts);

   return "ERROR|未知命令: " + cmd;
  }

//+------------------------------------------------------------------+
//| OPEN|SYMBOL|TYPE|PRICE|SL|TP|VOLUME|COMMENT                     |
//+------------------------------------------------------------------+
string CmdOpen(string &p[])
  {
   string symbol  = p[1];
   string otype   = p[2];
   double price   = StringToDouble(p[3]);
   double sl      = StringToDouble(p[4]);
   double tp      = StringToDouble(p[5]);
   double volume  = StringToDouble(p[6]);
   string comment = p[7];

   if(!SymbolSelect(symbol, true))
      return "ERROR|未找到品种: " + symbol;
   if(volume <= 0)
      return "ERROR|手数无效: " + DoubleToString(volume, 2);

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   ZeroMemory(req);
   ZeroMemory(res);

   req.symbol   = symbol;
   req.volume   = volume;
   req.sl       = sl;
   req.tp       = tp;
   req.comment  = comment;
   req.magic    = MagicNumber;
   req.deviation = SlippagePips;

   // Determine action and order type
   if(otype == "BUY")
     {
      req.action = TRADE_ACTION_DEAL;
      req.type   = ORDER_TYPE_BUY;
      req.price  = SymbolInfoDouble(symbol, SYMBOL_ASK);
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "SELL")
     {
      req.action = TRADE_ACTION_DEAL;
      req.type   = ORDER_TYPE_SELL;
      req.price  = SymbolInfoDouble(symbol, SYMBOL_BID);
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "BUY_LIMIT")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_BUY_LIMIT;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "BUY_STOP")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_BUY_STOP;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "SELL_LIMIT")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_SELL_LIMIT;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "SELL_STOP")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_SELL_STOP;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else
      return "ERROR|未知订单类型: " + otype;

   if(!OrderSend(req, res))
     {
      int err = GetLastError();
      return "ERROR|下单失败 code=" + IntegerToString(err)
             + " retcode=" + IntegerToString(res.retcode)
             + " " + res.comment;
     }

   if(res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED)
      return "OK|" + IntegerToString(res.order);

   return "ERROR|retcode=" + IntegerToString(res.retcode) + " " + res.comment;
  }

//+------------------------------------------------------------------+
//| CLOSE|TICKET|||||VOLUME|                                         |
//+------------------------------------------------------------------+
string CmdClose(string &p[])
  {
   ulong  ticket = (ulong)StringToInteger(p[1]);
   double vol    = (ArraySize(p) >= 7 && StringLen(p[6]) > 0)
                   ? StringToDouble(p[6]) : 0;

   if(!PositionSelectByTicket(ticket))
      return "ERROR|未找到持仓: " + IntegerToString(ticket);

   string symbol  = PositionGetString(POSITION_SYMBOL);
   double pos_vol = PositionGetDouble(POSITION_VOLUME);
   long   pos_type = PositionGetInteger(POSITION_TYPE);

   double close_vol = (vol > 0 && vol < pos_vol) ? vol : pos_vol;

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   ZeroMemory(req);
   ZeroMemory(res);

   req.action   = TRADE_ACTION_DEAL;
   req.position = ticket;
   req.symbol   = symbol;
   req.volume   = close_vol;
   req.type     = (pos_type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.price    = (pos_type == POSITION_TYPE_BUY)
                  ? SymbolInfoDouble(symbol, SYMBOL_BID)
                  : SymbolInfoDouble(symbol, SYMBOL_ASK);
   req.deviation = SlippagePips;
   req.magic    = MagicNumber;
   req.comment  = "OMNI_CLOSE";
   req.type_filling = SelectFilling(symbol);

   if(!OrderSend(req, res))
     {
      int err = GetLastError();
      return "ERROR|平仓失败 code=" + IntegerToString(err)
             + " retcode=" + IntegerToString(res.retcode)
             + " " + res.comment;
     }

   if(res.retcode == TRADE_RETCODE_DONE)
      return "OK|" + IntegerToString(ticket);

   return "ERROR|retcode=" + IntegerToString(res.retcode) + " " + res.comment;
  }

//+------------------------------------------------------------------+
//| MODIFY|TICKET|||SL|TP||                                          |
//+------------------------------------------------------------------+
string CmdModify(string &p[])
  {
   ulong  ticket = (ulong)StringToInteger(p[1]);
   double sl     = (ArraySize(p) >= 5 && StringLen(p[4]) > 0) ? StringToDouble(p[4]) : 0;
   double tp     = (ArraySize(p) >= 6 && StringLen(p[5]) > 0) ? StringToDouble(p[5]) : 0;

   // 先尝试持仓，再尝试挂单
   bool is_position = PositionSelectByTicket(ticket);
   bool is_order    = !is_position && OrderSelect(ticket);

   if(!is_position && !is_order)
      return "ERROR|未找到 ticket: " + IntegerToString(ticket);

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   ZeroMemory(req);
   ZeroMemory(res);

   if(is_position)
     {
      req.action   = TRADE_ACTION_SLTP;
      req.position = ticket;
      req.symbol   = PositionGetString(POSITION_SYMBOL);
      req.sl       = sl;
      req.tp       = tp;
     }
   else
     {
      req.action = TRADE_ACTION_MODIFY;
      req.order  = ticket;
      req.price  = OrderGetDouble(ORDER_PRICE_OPEN);
      req.sl     = sl;
      req.tp     = tp;
     }

   if(!OrderSend(req, res))
     {
      int err = GetLastError();
      return "ERROR|修改失败 code=" + IntegerToString(err)
             + " retcode=" + IntegerToString(res.retcode)
             + " " + res.comment;
     }

   if(res.retcode == TRADE_RETCODE_DONE)
      return "OK|" + IntegerToString(ticket);

   return "ERROR|retcode=" + IntegerToString(res.retcode) + " " + res.comment;
  }

//+------------------------------------------------------------------+
//| 写入结果文件，供 Python 读取                                     |
//+------------------------------------------------------------------+
void WriteResult(string result)
  {
   int fh = FileOpen(RESULT_FILE, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE)
     {
      Print("OmniExecutor：无法写入结果文件");
      return;
     }
   FileWriteString(fh, result);
   FileClose(fh);
   Print("OmniExecutor 执行结果：", result);
  }

//+------------------------------------------------------------------+
//| 为品种选择合适的成交模式                                         |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING SelectFilling(string symbol)
  {
   // SYMBOL_FILLING_MODE bitmask: bit0=FOK(1), bit1=IOC(2).
   // When both bits are 0 the broker uses RETURN mode (MetaQuotes demo default).
   long filling = (long)SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
  }
//+------------------------------------------------------------------+
