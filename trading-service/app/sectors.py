"""Sector badges: which parts of the market a bot trades (Energy, Chips, Defensive ...), so the bot table shows at a
glance what each bot is exposed to, and two bots on the same theme are easy to spot.

A bot's badges come from its symbols (a dip bot's watchlist, a rotation bot's universe, a single-symbol bot's symbol)
until someone sets them by hand on the bot's page (Bot.sectors); then those stay until reset to automatic.

The rule: each symbol has one sector; a sector holding at least a fifth of the bot's symbols becomes a badge, the
three biggest at most. 13 energy stocks: Energy. 8 chip makers and 4 consumer staples: Chips and Defensive. A list
spread so thin that no sector reaches a fifth (the 11 sector ETFs): Diversified.

A symbol's sector: the hand-made list below (themes a trader thinks in: chips apart from the rest of tech, nuclear
apart from utilities), else Yahoo's sector as the daily risk report keeps it (symbol_sectors), else unknown. Unknown
symbols don't count, so a bot whose symbols are all unknown has no badge until one is set by hand."""

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DIP, ROTATION, Bot, SymbolSector

SHARE = 0.2  # a sector needs at least this share of the bot's (known) symbols to be a badge
MOST = 3  # badges at most
DIVERSIFIED = "Diversified"

# Every badge, in the order the dashboard offers them (the bot page's editor; any other name can be typed)
BADGES = ["Chips", "Tech", "Software", "Energy", "Nuclear", "Utilities", "Power & grid", "Metals & mining",
          "Commodities", "Healthcare", "Defensive", "Financials", "Crypto", "Industrials", "Aerospace & defense",
          "Consumer", "Real estate", "Broad market", "Bonds", DIVERSIFIED]

_SECTORS = {
    "Chips": "ADI AMAT AMD ARM ASML AVGO COHR GFS INTC KLAC LITE LRCX MCHP MPWR MRVL MU NVDA NXPI OLED ON QCOM SMH "
             "SOXX TER TSM TXN UMC",
    "Tech": "AAPL AMZN CRWV DELL GLW GOOG GOOGL HPE IBM META MSFT NBIS NFLX QBTS RGTI SMCI STX TSLA UBER VGT WDC XLC "
            "XLK",
    "Software": "ADBE APP CIBR CRM CRWD DDOG FTNT IGV INTU MDB NET NOW OKTA ORCL PANW PLTR QLYS S SHOP SNOW TEAM "
                "TENB WDAY ZS",
    "Energy": "AR BNO COP CVE CVX DBE DVN ENB EOG EQNR EQT ET EXE HAL IMO KMI LNG MPC OIH OKE OXY PSX RRC SLB SU "
              "TRGP TS UGA UNG USO VLO WMB XLE XOM XOP",
    "Nuclear": "BWXT CCJ DNN LEU NNE NXE OKLO SMR UEC URA URNM",
    "Utilities": "AEP CEG D DUK ETR EXC NEE NRG PEG SO TLN VST XEL XLU",
    "Power & grid": "BE ENPH ETN FSLR GEV GNRC HUBB MOD NVT POWL PWR VRT",
    "Metals & mining": "ALB COPX FCX GDX GDXJ GLD GOLD HBM IAU MP NEM NUE REMX SCCO SLV SQM TECK XLB XME",
    "Commodities": "DBA DBC GSG MOS",
    "Healthcare": "ABBV ABT AMGN AZN BMY DHR GILD ISRG JNJ LLY MRK NVO PFE REGN TMO UNH VRTX XBI XLV",
    "Defensive": "CL COST KO MDLZ PEP PG PM STZ TGT WMT XLP",
    "Financials": "APO ARES AXP BAC BCS BLK BNY BRK.B C FBP GS HOOD JPM KRE MA MS SCHW V WFC XLF",
    "Crypto": "BTC-USD CIFR CLSK COIN CRCL ETH-USD IREN MARA MSTR RIOT",
    "Industrials": "CAT DE FIX HON KFY OC UNP XLI",
    "Aerospace & defense": "BA GD GE ITA LHX LMT NOC RKLB RTX",
    "Consumer": "DKS HD LEVI MCD NKE NXST PAG SAH SBUX WGO XLY",
    "Real estate": "AMH AMT CCI EQIX INVH O PLD XLRE",
    "Broad market": "DIA IVV IWM IJH IJR QQQ QQQM SPY VEA VO VOO VTI VTV VUG VWO VXUS",
    "Bonds": "AGG BND TLT",
}
SYMBOLS = {s: sector for sector, symbols in _SECTORS.items() for s in symbols.split()}

# Yahoo's sectors (and the ETF categories, as risk_report.CANON names them) as badges, for symbols not listed above
YAHOO = {
    "Technology": "Tech", "Communication Services": "Tech", "Communications": "Tech", "Energy": "Energy",
    "Equity Energy": "Energy", "Utilities": "Utilities", "Consumer Defensive": "Defensive", "Healthcare": "Healthcare",
    "Health": "Healthcare", "Financial Services": "Financials", "Financial": "Financials", "Industrials": "Industrials",
    "Basic Materials": "Metals & mining", "Equity Precious Metals": "Metals & mining",
    "Natural Resources": "Metals & mining", "Consumer Cyclical": "Consumer", "Real Estate": "Real estate",
    "Commodities Focused": "Commodities", "Commodities Broad Basket": "Commodities", "Large Blend": "Broad market",
    "Large Growth": "Broad market", "Large Value": "Broad market", "Small Blend": "Broad market",
    "Mid-Cap Blend": "Broad market",
}


def yahoo_sectors(session: Session) -> dict[str, str]:
    """symbol -> Yahoo's sector, from the risk report's cache (a few hundred rows: read once per bot list)."""
    return {r.symbol: r.sector for r in session.scalars(select(SymbolSector).where(SymbolSector.sector.is_not(None)))}


def of_symbol(symbol: str, yahoo: dict[str, str]) -> str | None:
    return SYMBOLS.get(symbol) or YAHOO.get(yahoo.get(symbol, ""))


def badges(symbols: list[str], yahoo: dict[str, str]) -> list[str]:
    """The badges of a list of symbols, biggest sector first (see the module's rule)."""
    counts = Counter(s for s in map(lambda x: of_symbol(x, yahoo), symbols) if s)
    known = sum(counts.values())
    if not known:
        return []
    big = [s for s, n in sorted(counts.items(), key=lambda kv: (-kv[1], BADGES.index(kv[0]))) if n / known >= SHARE]
    return big[:MOST] or [DIVERSIFIED]


def bot_symbols(bot: Bot, watched: list[str]) -> list[str]:
    """What a bot trades: its watchlist (dip, given by the caller), its universe (rotation) or its symbol."""
    if bot.strategy == DIP:
        return watched
    if bot.strategy == ROTATION:
        return list(bot.universe or [])
    return [bot.symbol]


def of_bot(bot: Bot, watched: list[str], yahoo: dict[str, str]) -> tuple[list[str], bool, list[str]]:
    """(the bot's badges, whether they're automatic, what its symbols say): set by hand they stay; NULL means its
    symbols decide."""
    computed = badges(bot_symbols(bot, watched), yahoo)
    if bot.sectors is not None:
        return list(bot.sectors), False, computed
    return computed, True, computed


def clean(names: list[str]) -> list[str]:
    """Badges as typed: trimmed, a known badge spelled as listed, no repeats (case-insensitive)."""
    known = {b.lower(): b for b in BADGES}
    out: list[str] = []
    for n in names:
        n = " ".join(n.split())
        n = known.get(n.lower(), n)
        if n and n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out
