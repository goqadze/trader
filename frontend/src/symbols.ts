// Suggested symbols for the Symbol pickers (backtest, compare, bot, scan). Only a shortlist: any other ticker can
// still be typed. Everything here trades on Alpaca and has Yahoo/Alpaca price history. Share classes are written
// with a dot (BRK.B) like Alpaca; the services turn that into Yahoo's dash (BRK-B) themselves.

export interface SymbolInfo {
  symbol: string;
  name: string;
}

export interface SymbolGroup {
  label: string;
  short: string; // for the scan form's "+ add group" links
  symbols: SymbolInfo[];
}

export const SYMBOL_GROUPS: SymbolGroup[] = [
  {
    // The 20 largest US-listed ETFs by assets on 2026-10-02 (chartrow.com/lists/biggest-etfs). Several track the
    // same index (VOO, IVV and SPY are all the S&P 500), so they trade almost identically. Alpaca doesn't trade
    // futures: SPY and QQQ stand in for the ES and NQ index futures.
    label: "Top 20 ETFs by assets (Oct 2026)",
    short: "Top 20 ETFs",
    symbols: [
      { symbol: "VOO", name: "S&P 500 (Vanguard; same index as SPY)" },
      { symbol: "IVV", name: "S&P 500 (iShares; same index as SPY)" },
      { symbol: "SPY", name: "S&P 500, like ES / MES" },
      { symbol: "VTI", name: "Total US stock market" },
      { symbol: "QQQ", name: "Nasdaq-100, like NQ / MNQ" },
      { symbol: "VEA", name: "Developed markets outside the US" },
      { symbol: "VUG", name: "US large-cap growth" },
      { symbol: "VTV", name: "US large-cap value" },
      { symbol: "VGT", name: "US information technology" },
      { symbol: "VWO", name: "Emerging markets" },
      { symbol: "BND", name: "Total US bond market (moves little)" },
      { symbol: "AGG", name: "US aggregate bonds (moves little)" },
      { symbol: "IWF", name: "Russell 1000 growth" },
      { symbol: "VXUS", name: "Total international stocks" },
      { symbol: "IJH", name: "S&P 400 mid caps" },
      { symbol: "XLK", name: "Technology sector (S&P 500 tech)" },
      { symbol: "IJR", name: "S&P 600 small caps" },
      { symbol: "VIG", name: "Dividend growers" },
      { symbol: "VO", name: "US mid caps" },
      { symbol: "QQQM", name: "Nasdaq-100 (cheaper twin of QQQ)" },
    ],
  },
  {
    label: "More index ETFs",
    short: "Dow & small caps",
    symbols: [
      { symbol: "DIA", name: "Dow Jones 30, like YM / MYM" },
      { symbol: "IWM", name: "Russell 2000 small caps" },
    ],
  },
  {
    // The 20 largest US companies by market value on 2026-10-02 (finhacker.cz top 20 S&P 500 by market cap)
    label: "Top 20 US companies by market value (Oct 2026)",
    short: "Top 20 companies",
    symbols: [
      { symbol: "NVDA", name: "Nvidia" },
      { symbol: "AAPL", name: "Apple" },
      { symbol: "GOOGL", name: "Alphabet (Google)" },
      { symbol: "MSFT", name: "Microsoft" },
      { symbol: "AMZN", name: "Amazon" },
      { symbol: "META", name: "Meta Platforms" },
      { symbol: "AVGO", name: "Broadcom" },
      { symbol: "TSLA", name: "Tesla (very volatile)" },
      { symbol: "MU", name: "Micron Technology" },
      { symbol: "BRK.B", name: "Berkshire Hathaway (class B)" },
      { symbol: "LLY", name: "Eli Lilly" },
      { symbol: "AMD", name: "Advanced Micro Devices" },
      { symbol: "JPM", name: "JPMorgan Chase" },
      { symbol: "WMT", name: "Walmart" },
      { symbol: "XOM", name: "Exxon Mobil" },
      { symbol: "V", name: "Visa" },
      { symbol: "JNJ", name: "Johnson & Johnson" },
      { symbol: "INTC", name: "Intel" },
      { symbol: "MA", name: "Mastercard" },
      { symbol: "ABBV", name: "AbbVie" },
    ],
  },
  {
    label: "Commodity ETFs",
    short: "Gold & silver",
    symbols: [
      { symbol: "GLD", name: "Gold (the ticker GOLD is a company, not gold)" },
      { symbol: "SLV", name: "Silver" },
    ],
  },
];

/** ETFs that track the same (or almost the same) index and so trade almost identically. Scanning several of one
 *  family repeats one result and makes one idea look like it worked on several symbols. The first of each is the
 *  one to keep (the most traded). */
export const TWINS: { label: string; symbols: string[] }[] = [
  { label: "the S&P 500", symbols: ["SPY", "VOO", "IVV"] },
  { label: "the Nasdaq-100", symbols: ["QQQ", "QQQM"] },
  { label: "US large-cap growth", symbols: ["VUG", "IWF"] },
  { label: "US tech", symbols: ["XLK", "VGT"] },
  { label: "US mid caps", symbols: ["IJH", "VO"] },
  { label: "US bonds", symbols: ["BND", "AGG"] },
];

/** The family a symbol belongs to (its twin group's first symbol), or the symbol itself. */
export const familyOf = (symbol: string) => TWINS.find((t) => t.symbols.includes(symbol))?.symbols[0] ?? symbol;
