"""
name_directory.py

Small, always-available company-name -> ticker lookup, used only to power
the "Did you mean" suggestion chips on a search that failed to resolve as a
ticker (_dispatch_search in app.py).

There is no live source of company names anywhere else in this codebase to
build this from: scanner_engine.py's universe fetchers (fetch_asx200,
fetch_sp500, ...) return a ['Ticker', 'Sector'] frame only, never a company
name column, and compounder_data.json only covers the tiny hand-curated
research universe (currently 3 tickers). Rather than add a scraper that can
silently break, this is a hardcoded starter directory of the ~120 most
commonly searched-for ASX and US names - a small dictionary that always
works beats a scraper that breaks.

This module never affects a query that already resolves to a real ticker -
_dispatch_search only calls suggest() after deep_dive_engine.analyze() has
already returned an error for that input.
"""

import difflib

# name (lowercase, human-readable) -> ticker. Deliberately just the most
# commonly searched large/well-known names on each market - this is a
# suggestion aid, not a full listed-company directory.
NAME_TO_TICKER = {
    # --- ASX ---
    "commonwealth bank": "CBA.AX", "commonwealth bank of australia": "CBA.AX",
    "national australia bank": "NAB.AX", "nab": "NAB.AX",
    "westpac": "WBC.AX", "anz": "ANZ.AX", "australia and new zealand banking": "ANZ.AX",
    "bank of queensland": "BOQ.AX", "bendigo bank": "BEN.AX", "bendigo and adelaide bank": "BEN.AX",
    "bhp": "BHP.AX", "bhp group": "BHP.AX", "rio tinto": "RIO.AX",
    "fortescue": "FMG.AX", "fortescue metals": "FMG.AX",
    "mineral resources": "MIN.AX", "south32": "S32.AX", "iluka": "ILU.AX", "iluka resources": "ILU.AX",
    "igo": "IGO.AX",
    "northern star": "NST.AX", "northern star resources": "NST.AX",
    "evolution mining": "EVN.AX", "gold road resources": "GOR.AX",
    "woodside": "WDS.AX", "woodside energy": "WDS.AX", "santos": "STO.AX",
    "origin energy": "ORG.AX", "viva energy": "VEA.AX",
    "csl limited": "CSL.AX",
    "cochlear": "COH.AX", "resmed": "RMD.AX", "sonic healthcare": "SHL.AX",
    "pro medicus": "PME.AX", "ramsay health care": "RHC.AX", "ramsay healthcare": "RHC.AX",
    "macquarie": "MQG.AX", "macquarie group": "MQG.AX", "asx limited": "ASX.AX",
    "suncorp": "SUN.AX", "qbe": "QBE.AX", "qbe insurance": "QBE.AX",
    "magellan": "MFG.AX", "magellan financial": "MFG.AX", "challenger": "CHC.AX",
    "hmc capital": "HMC.AX", "ing": "ING.AX",
    "medibank": "MPL.AX", "medibank private": "MPL.AX",
    "netwealth": "NWL.AX", "pinnacle investment management": "PNI.AX",
    "washington h soul pattinson": "SOL.AX", "soul patts": "SOL.AX",
    "zip co": "ZIP.AX",
    "goodman group": "GMG.AX", "scentre group": "SCG.AX",
    "gpt group": "GPT.AX", "dexus": "DXS.AX", "vicinity centres": "VCX.AX",
    "lendlease": "LLC.AX", "stockland": "SGP.AX",
    "woolworths": "WOW.AX", "coles": "COL.AX", "wesfarmers": "WES.AX",
    "jb hi-fi": "JBH.AX", "jb hifi": "JBH.AX", "harvey norman": "HVN.AX",
    "temple and webster": "TPW.AX",
    "arb corporation": "ARB.AX", "domino's pizza": "DMP.AX", "dominos pizza": "DMP.AX",
    "metcash": "MTS.AX", "nine entertainment": "NEC.AX", "tabcorp": "TAH.AX",
    "xero": "XRO.AX", "seek": "SEK.AX", "carsales": "CAR.AX", "carsales.com": "CAR.AX",
    "rea group": "REA.AX", "wisetech": "WTC.AX", "wisetech global": "WTC.AX",
    "nuix": "NXL.AX", "iress": "IRE.AX",
    "brambles": "BXB.AX", "transurban": "TCL.AX", "qube holdings": "QUB.AX",
    "orica": "ORI.AX", "computershare": "CPU.AX", "seven group holdings": "SVW.AX",
    "telstra": "TLS.AX", "tpg telecom": "TPG.AX",
    "apa group": "APA.AX", "ampol": "ALD.AX",
    "flight centre": "FLT.AX", "qantas": "QAN.AX",
    "james hardie": "JHX.AX", "boral": "BLD.AX", "csr limited": "CSR.AX",
    "amcor": "AMC.AX", "bluescope steel": "BSL.AX", "bluescope": "BSL.AX",
    "liontown resources": "LTR.AX", "lynas": "LYC.AX", "lynas rare earths": "LYC.AX",
    "paladin energy": "PDN.AX", "sandfire resources": "SFR.AX", "yancoal": "YAL.AX",
    "insurance australia group": "IAG.AX",
    "auckland international airport": "AIA.AX", "iph limited": "IPH.AX",
    "aub group": "AUB.AX",
    "objective corporation": "OCL.AX",

    # --- USA ---
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "alphabet": "GOOGL", "google": "GOOGL", "meta platforms": "META", "facebook": "META",
    "tesla": "TSLA", "jpmorgan chase": "JPM", "jp morgan": "JPM",
    "berkshire hathaway": "BRK-B", "berkshire": "BRK-B",
    "unitedhealth group": "UNH", "unitedhealth": "UNH",
    "exxon mobil": "XOM", "exxon": "XOM", "chevron": "CVX",
    "johnson and johnson": "JNJ",
    "visa": "V", "mastercard": "MA", "walmart": "WMT",
    "procter and gamble": "PG", "coca-cola": "KO", "coca cola": "KO", "pepsico": "PEP",
    "netflix": "NFLX", "walt disney": "DIS", "disney": "DIS",
    "adobe": "ADBE", "salesforce": "CRM", "oracle": "ORCL", "intel": "INTC",
    "advanced micro devices": "AMD", "qualcomm": "QCOM",
    "broadcom": "AVGO", "cisco": "CSCO", "ibm": "IBM",
    "costco": "COST", "home depot": "HD", "mcdonald's": "MCD", "mcdonalds": "MCD",
    "nike": "NKE", "starbucks": "SBUX", "boeing": "BA",
    "goldman sachs": "GS", "morgan stanley": "MS", "bank of america": "BAC",
    "wells fargo": "WFC", "american express": "AXP",
    "eli lilly": "LLY", "pfizer": "PFE", "merck": "MRK", "abbvie": "ABBV",
    "verizon": "VZ", "at&t": "T",
    "paypal": "PYPL", "uber": "UBER", "airbnb": "ABNB",
    "palantir": "PLTR", "shopify": "SHOP", "block inc": "SQ", "square": "SQ",
    "spotify": "SPOT",
}


def suggest(query, limit=5):
    """
    Best-effort name -> ticker suggestions for a search that failed to
    resolve as a ticker. Returns a list of (display_label, ticker) tuples,
    e.g. [("Resmed - RMD.AX", "RMD.AX")], deduped by ticker, closest matches
    first. Empty list if nothing looks close enough to be useful.

    Matching is deliberately simple and cheap: an exact/substring pass
    against the ~120-name starter directory above (catches "resmed" ->
    "resmed", "commonwealth" -> "commonwealth bank"), then a difflib fuzzy
    pass to catch typos ("aplle", "reamed") the substring pass would miss.
    This is a "did you mean" nudge, not a search engine.
    """
    q = (query or "").strip().lower()
    if not q or len(q) < 3:
        return []

    names = list(NAME_TO_TICKER.keys())

    substring_hits = [n for n in names if q in n or n in q]
    fuzzy_hits = difflib.get_close_matches(q, names, n=limit * 2, cutoff=0.6)

    ordered = []
    for n in substring_hits + fuzzy_hits:
        if n not in ordered:
            ordered.append(n)

    seen_tickers = set()
    out = []
    for n in ordered:
        tk = NAME_TO_TICKER[n]
        if tk in seen_tickers:
            continue
        seen_tickers.add(tk)
        out.append((f"{n.title()} - {tk}", tk))
        if len(out) >= limit:
            break

    return out
