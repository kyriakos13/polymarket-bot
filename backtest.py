"""
Backtest: πάνω στις ΛΥΜΕΝΕΣ/ΚΛΕΙΣΤΕΣ θέσεις κάθε wallet, τι $ROI είχαν πραγματικά
στις μπάντες A (1-5¢) και B (35-65¢);

Πηγή αλήθειας: `realizedPnl` / `totalBought` από το Polymarket API — το ΙΔΙΟ το Polymarket
έχει ήδη υπολογίσει το πραγματικό χρηματικό αποτέλεσμα κάθε θέσης, ό,τι κι αν έγινε
(κράτησε ως τη λήξη, ή πούλησε νωρίτερα με κέρδος/ζημιά).

ΓΙΑΤΙ ΟΧΙ curPrice: δοκιμάσαμε πρώτα να συγκρίνουμε curPrice (τιμή τη στιγμή του query)
με avgPrice ως "win/loss". ΑΠΟΔΕΙΧΤΗΚΕ ΛΑΘΟΣ — βρήκαμε θέση όπου avgPrice=0.68,
curPrice=0.98 (φαινομενικά "κέρδισε"), αλλά realizedPnl=-76.85 (ΠΡΑΓΜΑΤΙΚΑ ΕΧΑΣΕ, γιατί
ο trader πούλησε νωρίς με ζημιά πριν η αγορά κινηθεί αλλού). Το curPrice σε κλειστή θέση
ΔΕΝ αντανακλά αξιόπιστα την έκβαση που έζησε ο trader.

ΤΙΜΙΟΤΗΤΑ / ΟΡΙΑ:
- $-weighted ROI (όχι per-bet), γιατί το realizedPnl είναι $ ποσό, όχι per-share outcome.
- Δεν ξεχωρίζει "κράτησε ως τη λήξη" από "πούλησε νωρίς" — μετράει ό,τι ΠΡΑΓΜΑΤΙΚΑ συνέβη.
- Hedgers (πολλές δίπλευρες θέσεις) επισημαίνονται — το ROI τους μπορεί να είναι artefact
  arbitrage/market-making, όχι κατευθυντικό στοίχημα που αντιγράφεται νοηματικά.
- Καλύπτει μόνο τις τελευταίες ~4000 κλειστές θέσεις ανά wallet (API παγίνωση).

Τρέξε:  python backtest.py [--pages 8]
"""

import argparse
import json
import os
import sys

import requests

import resolver
resolver.install()
import bot

HERE = os.path.dirname(os.path.abspath(__file__))


def closed_positions(wallet, pages=8):
    out, off = [], 0
    for _ in range(pages):
        try:
            r = requests.get(f"{bot.DATA_API}/positions",
                             params={"user": wallet, "limit": 500, "offset": off, "closed": "true"},
                             timeout=25)
            if r.status_code != 200:
                break
            page = r.json()
        except Exception:
            break
        out += page
        if len(page) < 500:
            break
        off += 500
    return out


def band(price):
    if 0.01 <= price <= 0.05:
        return "A"
    if 0.35 <= price <= 0.65:
        return "B"
    return None


def score(positions):
    """$-weighted ROI πάνω σε πραγματικό realizedPnl / totalBought (cost basis)."""
    n = len(positions)
    if not n:
        return None
    cost = sum(float(p.get("totalBought") or 0) for p in positions)
    pnl = sum(float(p.get("realizedPnl") or 0) for p in positions)
    wins = sum(1 for p in positions if float(p.get("realizedPnl") or 0) > 0)
    return {
        "n": n,
        "win_positions_pct": round(100 * wins / n, 1),
        "cost_usd": round(cost),
        "pnl_usd": round(pnl),
        "roi": round(100 * pnl / cost, 1) if cost > 100 else None,
    }


def hedger_flag(positions):
    sides = {}
    for p in positions:
        sides.setdefault(p.get("conditionId"), set()).add(p.get("outcome"))
    both = sum(1 for s in sides.values() if len(s) >= 2)
    return both, len(sides)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=8)
    args = ap.parse_args()

    wl = json.load(open(os.path.join(HERE, "watchlist.json"), encoding="utf-8"))
    if not wl:
        sys.exit("Άδεια watchlist.")

    print(f"Διαβάζω κλειστές θέσεις από {len(wl)} wallets (πηγή αλήθειας: realizedPnl)…\n")
    print(f"{'WALLET':16}{'θέσεις':>8}{'win%':>7}{'κόστος$':>12}{'PnL$':>12}{'ROI%':>8}  flag")
    print("-" * 82)

    pools = {"A": [], "B": []}
    directional_pools = {"A": [], "B": []}
    for m in wl:
        pos = closed_positions(m["wallet"], args.pages)
        by_band = {"A": [], "B": []}
        for p in pos:
            b = band(float(p.get("avgPrice") or 0))
            if b and float(p.get("totalBought") or 0) > 0:
                by_band[b].append(p)
        allpos = by_band["A"] + by_band["B"]
        st = score(allpos)
        both, total_mk = hedger_flag(pos)
        is_hedger = total_mk and both / total_mk > 0.15
        flag = f"⚠ hedger ({both}/{total_mk} δίπλευρες)" if is_hedger else "directional"
        label = m["label"][:15]
        if st:
            print(f"{label:16}{st['n']:>8}{st['win_positions_pct']:>7}{st['cost_usd']:>12,}"
                  f"{st['pnl_usd']:>12,}{(str(st['roi'])+'%') if st['roi'] is not None else '—':>8}  {flag}")
        else:
            print(f"{label:16}{'— (καμία κλειστή in-band)':>50}  {flag}")
        for b in ("A", "B"):
            pools[b] += by_band[b]
            if not is_hedger:
                directional_pools[b] += by_band[b]

    print("-" * 82)
    for name, pl in [("ΟΛΟΙ · Sleeve A", pools["A"]), ("ΟΛΟΙ · Sleeve B", pools["B"])]:
        st = score(pl)
        if st:
            print(f"{name:16}{st['n']:>8}{st['win_positions_pct']:>7}{st['cost_usd']:>12,}"
                  f"{st['pnl_usd']:>12,}{(str(st['roi'])+'%') if st['roi'] is not None else '—':>8}")
    print("-" * 82)
    for name, pl in [("DIRECTIONAL A", directional_pools["A"]), ("DIRECTIONAL B", directional_pools["B"])]:
        st = score(pl)
        if st:
            print(f"{name:16}{st['n']:>8}{st['win_positions_pct']:>7}{st['cost_usd']:>12,}"
                  f"{st['pnl_usd']:>12,}{(str(st['roi'])+'%') if st['roi'] is not None else '—':>8}")
    print("=" * 82)
    print("ROI% = Σ(realizedPnl) / Σ(totalBought) στις in-band θέσεις — πραγματικό $ αποτέλεσμα.")
    print("win% = ποσοστό ΘΕΣΕΩΝ (όχι $) με θετικό realizedPnl.")
    print("DIRECTIONAL = μόνο non-hedger wallets (πιο αξιόπιστο σήμα για copy-trading).")


if __name__ == "__main__":
    main()
