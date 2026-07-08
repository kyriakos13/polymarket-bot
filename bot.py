"""
Polymarket mini-bot: copy trading + φτηνά limit orders.

ΑΣΦΑΛΕΙΑ: Ο κώδικας μιλάει ΜΟΝΟ με τα επίσημα Polymarket API:
  - https://clob.polymarket.com       (orders — μόνο όταν αγοράζεις)
  - https://data-api.polymarket.com   (δημόσια trades άλλων wallets)
  - https://gamma-api.polymarket.com  (δημόσια στοιχεία αγορών)
Το PRIVATE_KEY διαβάζεται από το τοπικό .env και δεν φεύγει πουθενά αλλού.

Εντολές:
  python bot.py market <slug ή URL αγοράς>     -> token IDs + τρέχουσες τιμές
  python bot.py trades <wallet>                -> πρόσφατα trades ενός wallet (δωρεάν, χωρίς key)
  python bot.py buy <token_id> <cents> <shares>-> limit order π.χ. buy 123... 2 10  (2 cents, 10 shares)
  python bot.py copy <wallet>                  -> ζωντανό copy trading του wallet
"""

import argparse
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")  # ελληνικά στην κονσόλα των Windows
except Exception:
    pass

import requests
from dotenv import load_dotenv

import resolver
resolver.install()  # αξιόπιστη ανάλυση DNS (DoH) για *.polymarket.com

CLOB_HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
POLYGON_CHAIN_ID = 137
MIN_SHARES = 5  # ελάχιστο μέγεθος limit order στο Polymarket

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() != "false"


def get_client():
    """Φτιάχνει authenticated CLOB client. Χρειάζεται μόνο για buy/copy."""
    from py_clob_client.client import ClobClient

    key = os.getenv("PRIVATE_KEY", "").strip()
    funder = os.getenv("FUNDER_ADDRESS", "").strip()
    sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))
    if not key or not funder:
        raise RuntimeError("Λείπει PRIVATE_KEY ή FUNDER_ADDRESS από το .env (δες .env.example)")
    client = ClobClient(
        CLOB_HOST, key=key, chain_id=POLYGON_CHAIN_ID,
        signature_type=sig_type, funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def journal_load():
    if not os.path.exists(JOURNAL_FILE):
        return []
    with open(JOURNAL_FILE, encoding="utf-8") as f:
        return json.load(f)


def journal_refresh():
    """Λύνει τις ανοιχτές εγγραφές του ημερολογίου μέσω Gamma (closed -> won/lost)."""
    data = journal_load()
    changed = 0
    for e in data:
        if e.get("result") is not None:
            continue
        try:
            r = requests.get(f"{GAMMA_API}/markets",
                             params={"clob_token_ids": e["token"]}, timeout=15)
            ms = r.json()
        except Exception:
            continue
        if not ms or not ms[0].get("closed"):
            continue
        m = ms[0]
        toks = json.loads(m.get("clobTokenIds") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
        if e["token"] in toks:
            v = float(prices[toks.index(e["token"])])
            if v > 0.98:
                e["result"] = 1
                changed += 1
            elif v < 0.02:
                e["result"] = 0
                changed += 1
    if changed:
        with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    return data, changed


def journal_stats(data):
    """Calibration: hit% vs μέση τιμή εισόδου, PnL αν κρατούσες ως τη λήξη, ανά πηγή."""
    def block(rows):
        res = [e for e in rows if e.get("result") is not None]
        n, nr = len(rows), len(res)
        if not nr:
            return {"bets": n, "resolved": 0, "hit": None, "mean_entry_c": None,
                    "edge_pp": None, "pnl": None,
                    "staked": round(sum(e["cost"] for e in rows), 2)}
        wins = sum(e["result"] for e in res)
        mean_entry = sum(e["price"] for e in res) / nr
        pnl = sum(e["shares"] * (e["result"] - e["price"]) for e in res)
        return {
            "bets": n, "resolved": nr, "hit": round(100 * wins / nr, 1),
            "mean_entry_c": round(100 * mean_entry, 1),
            "edge_pp": round(100 * (wins / nr - mean_entry), 1),
            "pnl": round(pnl, 2),
            "staked": round(sum(e["cost"] for e in rows), 2),
        }
    out = {"ΣΥΝΟΛΟ": block(data)}
    for src in sorted({e.get("source", "manual") for e in data}):
        out[src] = block([e for e in data if e.get("source", "manual") == src])
    return out


def account_check():
    """Ασφαλής έλεγχος: επιβεβαιώνει ότι το wallet του .env συνδέεται και δείχνει υπόλοιπο USDC.
    ΔΕΝ κάνει καμία συναλλαγή — μόνο auth + ανάγνωση."""
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    client = get_client()
    addr = client.get_address()
    usdc = None
    try:
        ba = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                   signature_type=int(os.getenv("SIGNATURE_TYPE", "1"))))
        raw = ba.get("balance") if isinstance(ba, dict) else None
        usdc = round(int(raw) / 1_000_000, 2) if raw is not None else None  # USDC = 6 decimals
    except Exception as e:
        usdc = f"σφάλμα ανάγνωσης: {e}"
    return {"address": addr, "funder": os.getenv("FUNDER_ADDRESS"), "usdc_balance": usdc}


JOURNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_log.json")


def log_trade(entry: dict):
    """Καταγράφει κάθε πόνταρισμα (dry ή αληθινό) στο ημερολόγιο calibration."""
    try:
        data = []
        if os.path.exists(JOURNAL_FILE):
            with open(JOURNAL_FILE, encoding="utf-8") as f:
                data = json.load(f)
        data.append(entry)
        with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[journal] αποτυχία εγγραφής: {e}")


def place_buy(client, token_id: str, price: float, shares: float,
              source: str = "manual", title: str = "", outcome: str = ""):
    """Βάζει GTC limit BUY. Σε DRY_RUN απλώς τυπώνει τι θα έκανε.
    Κάθε κλήση καταγράφεται στο ημερολόγιο για μέτρηση calibration."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    cost = price * shares
    label = f"BUY {shares} shares @ {price:.3f} (κόστος ~${cost:.2f}) token={token_id[:16]}..."
    resp = None
    if DRY_RUN:
        print(f"[DRY_RUN] Θα έβαζα order: {label}")
    else:
        order = client.create_order(OrderArgs(price=price, size=shares, side=BUY, token_id=token_id))
        resp = client.post_order(order, OrderType.GTC)
        print(f"[ORDER] {label}\n        απάντηση: {resp}")
    log_trade({
        "ts": int(time.time()), "token": token_id, "price": round(price, 4),
        "shares": shares, "cost": round(cost, 2), "dry": DRY_RUN,
        "source": source, "title": title, "outcome": outcome, "result": None,
    })
    return resp


def fetch_market(slug_or_url: str):
    """Επιστρέφει λίστα αγορών με τα outcomes/τιμές/token_ids (structured)."""
    slug = slug_or_url.rstrip("/").split("/")[-1].split("?")[0]
    r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
    r.raise_for_status()
    markets = r.json()
    if not markets:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=15)
        r.raise_for_status()
        events = r.json()
        markets = events[0].get("markets", []) if events else []
    result = []
    for m in markets:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
        tokens = json.loads(m.get("clobTokenIds", "[]"))
        result.append({
            "question": m.get("question", m.get("title", slug)),
            "outcomes": [
                {"name": o, "price": float(p), "token_id": t}
                for o, p, t in zip(outcomes, prices, tokens)
            ],
        })
    return result


def cmd_market(args):
    markets = fetch_market(args.slug)
    if not markets:
        sys.exit(f"Δεν βρέθηκε αγορά για: {args.slug}")
    for m in markets:
        print(f"\n{m['question']}")
        for o in m["outcomes"]:
            print(f"  {o['name']:<8} τιμή={o['price']*100:.1f}c  token_id={o['token_id']}")


def fetch_trades(wallet: str, limit: int = 50, offset: int = 0):
    r = requests.get(
        f"{DATA_API}/trades",
        params={"user": wallet, "limit": limit, "offset": offset, "takerOnly": "true"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_all_trades(wallet: str, page_size: int = 500, max_pages: int = 10):
    """Όλα τα trades ενός wallet, σελιδοποιώντας (μέχρι page_size*max_pages).
    Το data-api απορρίπτει με 400 πέρα από ένα όριο offset — σταματάμε αζήμια εκεί
    και επιστρέφουμε ό,τι έχει ήδη μαζευτεί, αντί να αποτυγχάνει όλο το αίτημα."""
    all_trades, offset = [], 0
    for _ in range(max_pages):
        try:
            page = fetch_trades(wallet, limit=page_size, offset=offset)
        except requests.HTTPError:
            break
        all_trades.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_trades


def fetch_positions(wallet: str, limit: int = 100):
    """Τρέχον χαρτοφυλάκιο ενός wallet (ανοιχτές θέσεις + P&L)."""
    r = requests.get(
        f"{DATA_API}/positions",
        params={"user": wallet, "limit": limit, "sortBy": "CURRENT", "sortDirection": "DESC"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


CATEGORIES = {
    # Η σειρά έχει σημασία: πιο συγκεκριμένα/σπάνια keywords πρώτα, "Κρύπτο" πριν "Οικονομία"
    # ώστε "Dogecoin Up or Down" να μην πιάνεται λάθος από οικονομικά keywords.
    "Κρύπτο": ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "xrp",
               "dogecoin", "doge", "coinbase", "stablecoin", "hyperliquid", "bnb",
               "cardano", "polkadot", "chainlink", "avalanche"],
    "Πολιτική": ["trump", "biden", "harris", "election", "president", "senate", "congress",
                 "democrat", "republican", "governor", "poll", "vote", "primary", "nominee",
                 "impeach", "cabinet", "supreme court"],
    "Οικονομία": ["fed", "rate cut", "interest rate", "inflation", "gdp", "recession", "cpi",
                  "jobs", "unemployment", "s&p", "nasdaq", "dow jones", "earnings", "ipo"],
    "Γεωπολιτική": ["russia", "ukraine", "china", "israel", "iran", "gaza", "putin", "xi",
                    "nato", "war", "ceasefire", "north korea", "taiwan", "venezuela"],
    "Tech/Elon": ["elon", "musk", "tesla", "spacex", "openai", "chatgpt", "gpt", "ai",
                  "apple", "nvidia", "google", "twitter"],
    "Αθλητικά": ["vs", "win on", "premier league", "nba", "nfl", "mlb", "world cup",
                 "champions league", "match", "o/u", "to advance", "wimbledon", "ufc", "f1",
                 "halftime", "exact score", "moneyline", "spread", "point spread", "over/under",
                 "leading", "series", "innings", "quarter", "fc", "united", "fixture",
                 "first half", "second half"],
}

_CAT_PATTERNS = {
    cat: re.compile(r"\b(" + "|".join(re.escape(k) for k in kws) + r")\b", re.I)
    for cat, kws in CATEGORIES.items()
}


def categorize(title: str) -> str:
    t = title or ""
    for cat, pattern in _CAT_PATTERNS.items():
        if pattern.search(t):
            return cat
    return "Άλλα"


def fetch_user_stats(wallet: str):
    """Στατιστικά ποιότητας ενός trader: rank, PnL, τζίρος, ROI (all-time)."""
    r = requests.get(
        f"{DATA_API}/v1/leaderboard",
        params={"timePeriod": "all", "orderBy": "PNL", "limit": 1, "offset": 0,
                "category": "overall", "user": wallet},
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return {"wallet": wallet, "userName": None, "rank": None, "pnl": 0, "vol": 0, "roi": None}
    u = rows[0]
    vol = float(u.get("vol") or 0)
    pnl = float(u.get("pnl") or 0)
    return {
        "wallet": wallet,
        "userName": u.get("userName") or u.get("pseudonym"),
        "rank": u.get("rank"),
        "pnl": pnl,
        "vol": vol,
        "roi": round(100 * pnl / vol, 2) if vol else None,
    }


def build_signals(members, source="positions", limit_each=40):
    """
    Συνδυάζει τις κινήσεις πολλών traders σε σταθμισμένο consensus.
    members: λίστα {wallet, label, weight}
    source: 'positions' (τι κρατάνε τώρα) ή 'recent' (πρόσφατες αγορές)
    Επιστρέφει λίστα σημάτων ταξινομημένη κατά score (σύνολο βαρών όσων συμφωνούν).
    """
    signals = {}  # token_id -> aggregate
    for m in members:
        w = m["wallet"]
        weight = float(m.get("weight", 1) or 1)
        label = m.get("label") or w[:8]
        try:
            if source == "recent":
                items = [t for t in fetch_trades(w, limit=limit_each) if t.get("side") == "BUY"]
            else:
                items = fetch_positions(w)[:limit_each]
        except Exception:
            continue
        for it in items:
            tok = it.get("asset")
            if not tok:
                continue
            title = it.get("title", "")
            price = float(it.get("price") or it.get("curPrice") or it.get("avgPrice") or 0)
            s = signals.setdefault(tok, {
                "token_id": tok, "title": title, "outcome": it.get("outcome"),
                "slug": it.get("slug"), "category": categorize(title),
                "score": 0.0, "members": [], "prices": [],
            })
            if any(mm["label"] == label for mm in s["members"]):
                continue  # ο ίδιος trader μετράει μία φορά ανά αγορά
            s["score"] += weight
            s["members"].append({"label": label, "weight": weight, "price": price})
            if price > 0:
                s["prices"].append(price)
    out = []
    for s in signals.values():
        s["n"] = len(s["members"])
        s["avg_price"] = round(sum(s["prices"]) / len(s["prices"]), 3) if s["prices"] else None
        del s["prices"]
        out.append(s)
    out.sort(key=lambda x: (x["score"], x["n"]), reverse=True)
    return out


def search_profiles(query: str, limit: int = 10):
    """Αναζήτηση traders με βάση όνομα/pseudonym -> λίστα {name, pseudonym, proxyWallet, profileImage}."""
    r = requests.get(
        f"{GAMMA_API}/public-search",
        params={"q": query, "search_profiles": "true", "limit_per_type": limit},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("profiles", [])


def fetch_leaderboard(period: str = "month", by: str = "PNL", limit: int = 25):
    """Top traders. period: day|week|month|all, by: PNL|VOL."""
    r = requests.get(
        f"{DATA_API}/v1/leaderboard",
        params={"timePeriod": period, "orderBy": by, "limit": limit,
                "offset": 0, "category": "overall"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def trade_key(t):
    return f"{t.get('transactionHash', '')}-{t.get('asset', '')}-{t.get('side', '')}"


def print_trade(t, prefix=""):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(t.get("timestamp", 0))))
    print(
        f"{prefix}{ts}  {t.get('side'):<4} {float(t.get('size', 0)):>8.1f} shares "
        f"@ {float(t.get('price', 0))*100:.1f}c  [{t.get('outcome')}] {t.get('title')}"
    )


def cmd_trades(args):
    trades = fetch_trades(args.wallet, limit=args.limit)
    if not trades:
        print("Κανένα trade για αυτό το wallet.")
        return
    for t in trades:
        print_trade(t)


def cmd_buy(args):
    price = args.cents / 100.0
    if not (0 < price < 1):
        sys.exit("Η τιμή πρέπει να είναι 1-99 cents")
    if args.shares < MIN_SHARES:
        sys.exit(f"Ελάχιστο μέγεθος: {MIN_SHARES} shares (έδωσες {args.shares})")
    client = get_client() if not DRY_RUN else None
    place_buy(client, args.token_id, price, args.shares)


def cmd_copy(args):
    copy_shares = float(os.getenv("COPY_SHARES", "5"))
    max_price = float(os.getenv("MAX_COPY_PRICE_CENTS", "10")) / 100.0
    poll = int(os.getenv("POLL_SECONDS", "5"))

    client = get_client() if not DRY_RUN else None
    seen = {trade_key(t) for t in fetch_trades(args.wallet, limit=100)}
    print(
        f"Παρακολουθώ {args.wallet}\n"
        f"  αντιγράφω BUY <= {max_price*100:.0f}c με {copy_shares} shares το καθένα, "
        f"έλεγχος κάθε {poll}s, DRY_RUN={DRY_RUN}\n"
        f"  (τα SELL του target τυπώνονται μόνο — πούλα χειροκίνητα αν θες)\n"
        f"Ctrl+C για διακοπή.\n"
    )
    while True:
        try:
            trades = fetch_trades(args.wallet, limit=50)
        except requests.RequestException as e:
            print(f"[σφάλμα δικτύου, ξαναπροσπαθώ] {e}")
            time.sleep(poll)
            continue
        for t in reversed(trades):  # παλιότερα πρώτα
            k = trade_key(t)
            if k in seen:
                continue
            seen.add(k)
            print_trade(t, prefix="[ΝΕΟ] ")
            price = float(t.get("price", 0))
            if t.get("side") == "BUY" and 0 < price <= max_price:
                try:
                    place_buy(client, t["asset"], price, copy_shares)
                except Exception as e:
                    print(f"[σφάλμα order] {e}")
            elif t.get("side") == "BUY":
                print(f"       (το προσπερνάω: τιμή {price*100:.1f}c > όριο {max_price*100:.0f}c)")
        time.sleep(poll)


def main():
    p = argparse.ArgumentParser(description="Polymarket mini-bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("market", help="Δείξε token IDs + τιμές μιας αγοράς")
    pm.add_argument("slug", help="slug ή URL της αγοράς από το polymarket.com")
    pm.set_defaults(func=cmd_market)

    pt = sub.add_parser("trades", help="Πρόσφατα trades ενός wallet")
    pt.add_argument("wallet")
    pt.add_argument("--limit", type=int, default=30)
    pt.set_defaults(func=cmd_trades)

    pb = sub.add_parser("buy", help="Limit BUY order")
    pb.add_argument("token_id")
    pb.add_argument("cents", type=float, help="τιμή σε cents, π.χ. 2")
    pb.add_argument("shares", type=float, help="πόσα shares, ελάχιστο 5")
    pb.set_defaults(func=cmd_buy)

    pc = sub.add_parser("copy", help="Copy trading ενός wallet")
    pc.add_argument("wallet")
    pc.set_defaults(func=cmd_copy)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
