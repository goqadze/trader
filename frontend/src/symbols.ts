// Suggested symbols for the Symbol pickers (backtest, compare, bot, scan). Only a shortlist: any other ticker can
// still be typed. Everything here trades on Alpaca and has Yahoo/Alpaca price history. Share classes are written
// with a dot (BRK.B) like Alpaca; the services turn that into Yahoo's dash (BRK-B) themselves. Crypto is written
// like Yahoo (BTC-USD), whose daily prices the backtests read; Alpaca calls the same pair BTC/USD.

export interface SymbolInfo {
  symbol: string;
  name: string;
  etf?: boolean; // an ETF in a group of mostly companies (overrides the group's `etf`)
}

export interface SymbolGroup {
  label: string;
  short: string; // for the scan form's "+ add group" links
  etf?: boolean; // all of them are ETFs (see isListedEtf)
  note?: string; // what to know before picking them, shown in the symbol picker
  symbols: SymbolInfo[];
}

export const SYMBOL_GROUPS: SymbolGroup[] = [
  {
    // The 20 largest US-listed ETFs by assets on 2026-10-02 (chartrow.com/lists/biggest-etfs). Several track the
    // same index (VOO, IVV and SPY are all the S&P 500), so they trade almost identically. Alpaca doesn't trade
    // futures: SPY and QQQ stand in for the ES and NQ index futures.
    label: "Top 20 ETFs by assets (Oct 2026)",
    short: "Top 20 ETFs",
    etf: true,
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
    // Index futures for the intraday strategies (backtest-service/app/futures.py). This app's market data has no
    // futures: their prices are rebuilt from the ETF on the same index, so MNQ and NQ trade QQQ's moves, at NQ's level.
    label: "Index futures (priced from QQQ / SPY / DIA)",
    short: "Futures",
    symbols: [
      { symbol: "MNQ", name: "Micro Nasdaq-100: $2 a point (from QQQ)" },
      { symbol: "MES", name: "Micro S&P 500: $5 a point (from SPY)" },
      { symbol: "MYM", name: "Micro Dow: $0.50 a point (from DIA)" },
      { symbol: "NQ", name: "E-mini Nasdaq-100: $20 a point" },
      { symbol: "ES", name: "E-mini S&P 500: $50 a point" },
      { symbol: "YM", name: "E-mini Dow: $5 a point" },
    ],
  },
  {
    label: "More index ETFs",
    short: "Dow & small caps",
    etf: true,
    symbols: [
      { symbol: "DIA", name: "Dow Jones 30, like YM / MYM" },
      { symbol: "IWM", name: "Russell 2000 small caps" },
    ],
  },
  {
    // The S&P 500's 11 sectors (SPDR): they all trade since 2018 (XLC) or earlier, so testing past years on them
    // doesn't pick winners with hindsight the way today's largest companies do. A rotation's default universe.
    label: "S&P 500 sector ETFs",
    short: "Sector ETFs",
    etf: true,
    note: "The whole US market split into 11 parts. A good first universe for a rotation: no hindsight in picking them.",
    symbols: [
      { symbol: "XLK", name: "Technology" },
      { symbol: "XLF", name: "Financials" },
      { symbol: "XLE", name: "Energy" },
      { symbol: "XLV", name: "Health care" },
      { symbol: "XLI", name: "Industrials" },
      { symbol: "XLY", name: "Consumer discretionary" },
      { symbol: "XLP", name: "Consumer staples" },
      { symbol: "XLU", name: "Utilities" },
      { symbol: "XLB", name: "Materials" },
      { symbol: "XLRE", name: "Real estate" },
      { symbol: "XLC", name: "Communication services" },
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
    etf: true,
    symbols: [
      { symbol: "GLD", name: "Gold (the ticker GOLD is a company, not gold)" },
      { symbol: "SLV", name: "Silver" },
    ],
  },
  // The next five: the picks of U.S. News & World Report's "best ... to buy" articles of 2026-10-06/07 (space, green,
  // high-dividend ETFs, REITs, money market funds). Checked on 2026-10-09: Yahoo has prices for all of them and Alpaca
  // trades them all (fractions of a share too, except RIET, MMK and JMMF). Left out: the money market mutual funds
  // (NCGXX, VMFXX, SWTXX): Alpaca doesn't trade mutual funds, and their price stays at $1.00.
  {
    label: "High-dividend ETFs (U.S. News picks, Oct 2026)",
    short: "Dividend ETFs",
    etf: true,
    note: "Steadier funds that pay out much of their gain as dividends (yearly yield in brackets; the backtests count dividends). US dividends paid to a non-US investor are taxed at the source.",
    symbols: [
      { symbol: "SPYD", name: "S&P 500 high dividend (4.7%)" },
      { symbol: "VYM", name: "Vanguard high dividend yield (2.3%)" },
      { symbol: "SPHD", name: "S&P 500 high dividend, low volatility (4.6%, monthly)" },
      { symbol: "DURA", name: "VanEck durable high dividend (2.9%)" },
      { symbol: "HDV", name: "iShares core high dividend (3.3%)" },
      { symbol: "DHS", name: "WisdomTree US high dividend (3.4%)" },
      { symbol: "RIET", name: "Hoya high-dividend REITs (10.5%; whole shares only on Alpaca)" },
    ],
  },
  {
    label: "REITs: real estate companies (U.S. News picks, Oct 2026)",
    short: "REITs",
    note: "Companies that own buildings and pay most of their profit as dividends. Picked by Morningstar as below their fair value today.",
    symbols: [
      { symbol: "CCI", name: "Crown Castle: cell towers" },
      { symbol: "INVH", name: "Invitation Homes: houses to rent" },
      { symbol: "SBAC", name: "SBA Communications: cell towers" },
      { symbol: "DOC", name: "Healthpeak: healthcare buildings" },
      { symbol: "AMT", name: "American Tower: cell towers" },
      { symbol: "O", name: "Realty Income: shops, pays monthly" },
      { symbol: "KIM", name: "Kimco: shopping centers" },
      { symbol: "EXR", name: "Extra Space: self-storage" },
      { symbol: "ESS", name: "Essex: West Coast apartments" },
      { symbol: "PSA", name: "Public Storage: self-storage" },
    ],
  },
  {
    label: "Clean energy stocks & ETFs (U.S. News picks, Oct 2026)",
    short: "Clean energy",
    note: "CNRG already holds BE, FSLR and ENLT. Clean energy swings a lot: BE rose over 200% in the past year, FSLR fell over 20%.",
    symbols: [
      { symbol: "BE", name: "Bloom Energy: fuel cells for AI data centers" },
      { symbol: "BEPC", name: "Brookfield Renewable (corporation; BEP is the partnership)" },
      { symbol: "FSLR", name: "First Solar: US solar panels" },
      { symbol: "ETN", name: "Eaton: electrical equipment" },
      { symbol: "ECL", name: "Ecolab: water treatment" },
      { symbol: "ENLT", name: "Enlight Renewable Energy (pricey: about 108x earnings)" },
      { symbol: "CNRG", name: "SPDR Kensho Clean Power ETF", etf: true },
      { symbol: "ICLN", name: "iShares Global Clean Energy ETF", etf: true },
    ],
  },
  {
    label: "Space stocks & ETF (U.S. News picks, Oct 2026)",
    short: "Space",
    note: "High risk: most of these companies don't make a profit yet. UFO is a basket of 68 space companies (over 15% SpaceX) that already holds SPCX, RKLB, ASTS and PL.",
    symbols: [
      { symbol: "SPCX", name: "SpaceX (listed June 2026: under a year of prices)" },
      { symbol: "RKLB", name: "Rocket Lab: launches and spacecraft" },
      { symbol: "PL", name: "Planet Labs: satellite images of Earth" },
      { symbol: "ASTS", name: "AST SpaceMobile: satellites to phones" },
      { symbol: "LUNR", name: "Intuitive Machines: Moon landers" },
      { symbol: "RDW", name: "Redwire: space parts, mostly for defense" },
      { symbol: "UFO", name: "Procure Space ETF: 68 space companies", etf: true },
    ],
  },
  {
    label: "Money market ETFs: cash parking (U.S. News picks, Oct 2026)",
    short: "Money market",
    etf: true,
    note: "They barely move: about 3.7% a year of interest, paid as a slowly rising price. In a rotation one holds a slot when everything else is weaker. All are new: a 12-month momentum needs 13 months of prices, which only MMKT has so far.",
    symbols: [
      { symbol: "MMKT", name: "Texas Capital government money market (since Sep 2024)" },
      { symbol: "JMMF", name: "JPMorgan US Treasury money market (since Dec 2025; whole shares only on Alpaca)" },
      { symbol: "IQMM", name: "ProShares GENIUS money market (since Feb 2026)" },
      { symbol: "MMK", name: "State Street prime money market (since Feb 2026; whole shares only on Alpaca)" },
    ],
  },
  {
    // Coins Alpaca trades against the dollar (checked on its crypto market data, 2026-10-05), largest first. Crypto
    // trades around the clock, 7 days a week, in fractions of a coin, long only (no shorting, no margin), with a
    // 0.15-0.25% fee per trade. Backtests only for now: the daily strategies, decided on the daily close (00:00 UTC).
    label: "Crypto (backtests only for now)",
    short: "Crypto",
    symbols: [
      { symbol: "BTC-USD", name: "Bitcoin" },
      { symbol: "ETH-USD", name: "Ethereum" },
      { symbol: "XRP-USD", name: "XRP" },
      { symbol: "SOL-USD", name: "Solana" },
      { symbol: "DOGE-USD", name: "Dogecoin (very volatile)" },
      { symbol: "ADA-USD", name: "Cardano" },
      { symbol: "LINK-USD", name: "Chainlink" },
      { symbol: "AVAX-USD", name: "Avalanche" },
      { symbol: "BCH-USD", name: "Bitcoin Cash" },
      { symbol: "LTC-USD", name: "Litecoin" },
      { symbol: "DOT-USD", name: "Polkadot" },
      { symbol: "UNI-USD", name: "Uniswap" },
    ],
  },
];

/** ETFs that track the same (or almost the same) index and so trade almost identically. Scanning several of one
 *  family repeats one result and makes one idea look like it worked on several symbols. The first of each is the
 *  one to keep (the most traded). */
export const TWINS: { label: string; symbols: string[] }[] = [
  // A future's prices are its ETF's, rebuilt (see the futures group): the same result again
  { label: "the S&P 500", symbols: ["SPY", "VOO", "IVV", "MES", "ES"] },
  { label: "the Nasdaq-100", symbols: ["QQQ", "QQQM", "MNQ", "NQ"] },
  { label: "the Dow", symbols: ["DIA", "MYM", "YM"] },
  { label: "US large-cap growth", symbols: ["VUG", "IWF"] },
  { label: "US tech", symbols: ["XLK", "VGT"] },
  { label: "US mid caps", symbols: ["IJH", "VO"] },
  { label: "US bonds", symbols: ["BND", "AGG"] },
];

/** The family a symbol belongs to (its twin group's first symbol), or the symbol itself. */
export const familyOf = (symbol: string) => TWINS.find((t) => t.symbols.includes(symbol))?.symbols[0] ?? symbol;

/** Grouped options for a symbol picker, each ticker once (XLK is both a top-20 ETF and a sector): its first group
 *  wins. `render` builds an option's label. */
export function symbolOptions<L>(render: (s: SymbolInfo) => L, groups: SymbolGroup[] = SYMBOL_GROUPS) {
  const seen = new Set<string>();
  return groups.map((g) => ({
    label: g.label,
    options: g.symbols.filter((s) => !seen.has(s.symbol) && seen.add(s.symbol)).map((s) => ({ value: s.symbol, label: render(s), name: s.name })),
  }));
}

/** ETFs: on one of the lists, as an ETF. Anything else (a company, or a typed ticker) may be a hindsight pick in a test
 *  of past years. */
export const isListedEtf = (symbol: string) => SYMBOL_GROUPS.some((g) => g.symbols.some((s) => s.symbol === symbol && (s.etf ?? g.etf ?? false)));

/** Index futures: they run with the intraday strategies only. */
export const FUTURES = SYMBOL_GROUPS.find((g) => g.short === "Futures")!.symbols.map((s) => s.symbol);
export const isFuture = (symbol: string | undefined) => FUTURES.includes((symbol ?? "").toUpperCase());

/** Crypto pairs, written like Yahoo (BTC-USD): any ticker ending in -USD. Backtests run them with the daily
 *  strategies only; live bots can't trade them yet. */
export const isCrypto = (symbol: string | undefined) => /^[A-Z]+-USD$/.test((symbol ?? "").trim().toUpperCase());

/** What a live bot can trade: no futures (on Alpaca "ES" is a utility's stock, not the future) and no crypto yet. */
export const BOT_GROUPS = SYMBOL_GROUPS.filter((g) => g.short !== "Futures" && g.short !== "Crypto");

/** The groups a rotation can hold: daily closes from Yahoo, so no futures (their prices are rebuilt intraday only). */
export const ROTATION_GROUPS = SYMBOL_GROUPS.filter((g) => g.short !== "Futures");

/** Why this symbol can't run with these strategies, or null. Futures run with the intraday strategies only; crypto
 *  with the daily ones only (the intraday ones are built around New York's 9:30 open, and crypto never closes). */
export function symbolClash(symbol: string | undefined, intraday: boolean): string | null {
  if (isFuture(symbol) && !intraday) return "Futures run with the intraday strategies only";
  if (isCrypto(symbol) && intraday) return "Crypto runs with the daily strategies only (the intraday ones trade New York's session)";
  return null;
}
