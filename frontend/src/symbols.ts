// Suggested symbols for the Symbol pickers (backtest, compare, bot). Only a shortlist: any other
// ticker can still be typed. Everything here trades on Alpaca and has Yahoo/Alpaca price history.

export interface SymbolInfo {
  symbol: string;
  name: string;
}

export interface SymbolGroup {
  label: string;
  symbols: SymbolInfo[];
}

export const SYMBOL_GROUPS: SymbolGroup[] = [
  {
    // Alpaca doesn't trade futures, so these ETFs stand in for the NQ / ES / YM index futures.
    label: "Index ETFs (stand-ins for NQ, ES, YM futures)",
    symbols: [
      { symbol: "QQQ", name: "Nasdaq-100, like NQ / MNQ" },
      { symbol: "SPY", name: "S&P 500, like ES / MES" },
      { symbol: "DIA", name: "Dow Jones 30, like YM / MYM" },
      { symbol: "IWM", name: "Russell 2000 small caps" },
    ],
  },
  {
    label: "Large caps",
    symbols: [
      { symbol: "AAPL", name: "Apple" },
      { symbol: "MSFT", name: "Microsoft" },
      { symbol: "NVDA", name: "Nvidia" },
      { symbol: "AMZN", name: "Amazon" },
      { symbol: "GOOGL", name: "Alphabet (Google)" },
      { symbol: "META", name: "Meta Platforms" },
      { symbol: "TSLA", name: "Tesla (very volatile)" },
      { symbol: "JPM", name: "JPMorgan Chase" },
    ],
  },
  {
    label: "Commodity ETFs",
    symbols: [
      { symbol: "GLD", name: "Gold (the ticker GOLD is a company, not gold)" },
      { symbol: "SLV", name: "Silver" },
    ],
  },
];
