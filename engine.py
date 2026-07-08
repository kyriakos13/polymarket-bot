"""
Alpha Copilot engine — dual-sleeve meta-copy-trading scoring on top of the watchlist.

ΤΙ ΕΙΝΑΙ / ΤΙ ΔΕΝ ΕΙΝΑΙ (διάβασέ το):
Υλοποιεί τη σκελετική λογική του v4.0 spec ΜΟΝΟ με σήματα που δίνει πραγματικά το
Polymarket API. Δεν εφευρίσκει catalysts, closing-line value, social hype ή ιστορικό
order-book — αυτά επισημαίνονται ως "χρειάζεται χειροκίνητη επιβεβαίωση" (missing).
Η μόνη πηγή "edge" είναι: έμπειρα, ανεξάρτητα wallets αγόρασαν κάτι φθηνότερα από την
τρέχουσα τιμή. Ο αλγόριθμος το σταθμίζει με Bayesian shrinkage και το καπάρει σκληρά.
ΚΑΝΕΝΑ κέρδος δεν εγγυάται. Το EV προκύπτει από την ΥΠΟΘΕΣΗ ότι οι πηγές σου έχουν
διαρκές alpha — αυτό το επικυρώνει μόνο backtest, όχι το score.
"""

import math
import statistics as stats

import bot  # resolver + fetch_positions/fetch_trades/fetch_user_stats/fetch_market/categorize

# ---- σταθερές / κατώφλια (spec §IV, §IX, §XI) ----
SLEEVE_A = (0.01, 0.05)      # asymmetric longshot
SLEEVE_B = (0.35, 0.65)      # pre-discovery value
K_SHRINK = 40               # Bayesian shrinkage strength (σε # δειγμάτων)
NUDGE_CAP_A = 0.08          # μέγιστη μετατόπιση est_prob για sleeve A (price units)
NUDGE_CAP_B = 0.12          # για sleeve B
KELLY_FRACTION = 0.25       # fractional Kelly
MIN_POS_USD = 500           # spec §V.1 position significance
CACHE = {}


def _sig(x, scale=1.0):
    return 1 / (1 + math.exp(-x / scale))


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def band_of(price):
    if SLEEVE_A[0] <= price <= SLEEVE_A[1]:
        return "A"
    if SLEEVE_B[0] <= price <= SLEEVE_B[1]:
        return "B"
    return None


def shrink(values, k=K_SHRINK, prior=0.0):
    """Bayesian-ish shrinkage του μέσου προς το prior, βάρος n/(n+k)."""
    n = len(values)
    if n == 0:
        return prior, 0, 0.0
    m = sum(values) / n
    sd = stats.pstdev(values) if n > 1 else 0.0
    shrunk = prior + (m - prior) * (n / (n + k))
    return shrunk, n, sd


def get_trader_data(wallet):
    """Μαζεύει (και cache-άρει) ό,τι χρειάζεται για ένα wallet."""
    if wallet in CACHE:
        return CACHE[wallet]
    try:
        st = bot.fetch_user_stats(wallet)
    except Exception:
        st = {"wallet": wallet, "userName": None, "rank": None, "pnl": 0, "vol": 0, "roi": None}
    try:
        positions = bot.fetch_positions(wallet)
    except Exception:
        positions = []
    closed = []
    off = 0
    for _ in range(4):  # έως ~2000 κλειστές θέσεις για hit-rate
        try:
            import requests
            r = requests.get(f"{bot.DATA_API}/positions",
                             params={"user": wallet, "limit": 500, "offset": off, "closed": "true"},
                             timeout=20)
            if r.status_code != 200:
                break
            page = r.json()
        except Exception:
            break
        closed.extend(page)
        if len(page) < 500:
            break
        off += 500
    try:
        recent = [t for t in bot.fetch_trades(wallet, limit=60) if t.get("side") == "BUY"]
    except Exception:
        recent = []
    data = {"stats": st, "positions": positions, "closed": closed, "recent": recent}
    CACHE[wallet] = data
    return data


def wallet_alpha(data):
    """
    WalletQualityScore (0-100) από ΑΞΙΟΠΙΣΤΑ, θετικά σήματα: all-time ROI, rank, PnL.
    ΔΕΝ χρησιμοποιεί το realized closed-position edge ως οδηγό — είναι biased αρνητικά
    για hedgers/arbitrageurs (τα κερδισμένα σκέλη μένουν ανοιχτά). Το κρατάμε ΜΟΝΟ ως
    πληροφοριακό `calib` με προειδοποίηση, όχι για EV.
    """
    st = data["stats"]
    roi = (st.get("roi") or 0) / 100.0
    pnl = float(st.get("pnl") or 0)
    rank = int(st["rank"]) if st.get("rank") else 10 ** 7
    rank_bonus = 1.0 if rank <= 100 else 0.6 if rank <= 500 else 0.3 if rank <= 3000 else 0.1

    quality = _clamp(
        45 * _sig(roi, 0.02)                       # κερδοφορία (thin αλλά πραγματική)
        + 35 * rank_bonus                          # κατάταξη
        + 20 * _sig(math.log10(max(pnl, 1)) - 4, 1),  # κλίμακα PnL (>$10k αρχίζει να μετράει)
        0, 100)

    # πληροφοριακό μόνο: realized band edge (biased — γι' αυτό ΔΕΝ οδηγεί το EV)
    band_edges = {"A": [], "B": []}
    for p in data["closed"]:
        entry = float(p.get("avgPrice") or 0)
        cur = float(p.get("curPrice") or 0)
        b = band_of(entry)
        if b is None:
            continue
        if cur > 0.9:
            band_edges[b].append(1.0 - entry)
        elif cur < 0.1:
            band_edges[b].append(0.0 - entry)

    out = {"roi": roi, "rank": st.get("rank"), "name": st.get("userName"),
           "pnl": pnl, "quality": round(quality, 1)}
    for b in ("A", "B"):
        calib, n, sd = shrink(band_edges[b], prior=0.0)
        # score ανά μπάντα = ποιότητα, με μικρό tilt αν το calib δεν είναι χάλια
        out[b] = {"score": round(quality, 1), "calib": round(calib, 4), "n": n}
    return out


def jaccard(a, b):
    if not a or not b:
        return 0.0
    ia = len(a & b)
    return ia / len(a | b)


def correlation_penalties(members_data):
    """
    Penalty ανά wallet = μέσο Jaccard overlap των τρεχουσών θέσεων με τα άλλα.
    Υψηλό overlap => πιθανό copycat/correlated => μικρότερη ανεξάρτητη αξία (spec §V.4).
    """
    sets = {}
    for w, d in members_data.items():
        sets[w] = {p.get("conditionId") for p in d["positions"] if p.get("conditionId")}
    pen = {}
    for w in sets:
        others = [jaccard(sets[w], sets[o]) for o in sets if o != w]
        pen[w] = round(sum(others) / len(others), 3) if others else 0.0
    return pen


def build_candidates(watchlist):
    """
    Γυρίζει scored candidates ανά sleeve από τις ΤΡΕΧΟΥΣΕΣ θέσεις της watchlist.
    Κάθε candidate: token, market, outcome, price, supporters, est_prob, edge, sizing, flags.
    """
    members_data = {}
    alphas = {}
    trust = {}
    for m in watchlist:
        w = m["wallet"]
        d = get_trader_data(w)
        members_data[w] = d
        alphas[w] = wallet_alpha(d)
        trust[w] = float(m.get("weight", 1) or 1)
    corr = correlation_penalties(members_data)

    # aggregate ανά (conditionId, outcome)
    agg = {}
    for w, d in members_data.items():
        for p in d["positions"]:
            price = float(p.get("curPrice") or 0)
            b = band_of(price)
            if b is None:
                continue
            val = float(p.get("currentValue") or 0)
            if val < MIN_POS_USD:
                continue  # spec §V.1 — αγνόησε dust
            key = (p.get("conditionId"), p.get("outcome"))
            a = agg.setdefault(key, {
                "token": p.get("asset"), "title": p.get("title"), "outcome": p.get("outcome"),
                "slug": p.get("slug"), "category": bot.categorize(p.get("title", "")),
                "price": price, "band": b, "supporters": [],
            })
            a["supporters"].append({
                "wallet": w, "name": alphas[w].get("name") or w[:8],
                "trust": trust[w], "alpha": alphas[w][b]["score"],
                "n": alphas[w][b]["n"], "entry": float(p.get("avgPrice") or price),
                "usd": round(val), "corr_pen": corr[w],
            })

    # whale-conflict map (spec §V.5): consensus ανά (conditionId, outcome)
    cond_sides = {}
    for (cond, outcome), a in agg.items():
        c = sum(s["trust"] * (s["alpha"] / 100.0) * (1 - s["corr_pen"]) for s in a["supporters"])
        cond_sides.setdefault(cond, {})[outcome] = c

    candidates = []
    for (cond, outcome), a in agg.items():
        sup = a["supporters"]
        p = a["price"]
        entries = [s["entry"] for s in sup]
        avg_entry = sum(entries) / len(entries)
        indep_n = len(sup)
        avg_quality = sum(s["alpha"] for s in sup) / indep_n
        avg_corr = sum(s["corr_pen"] for s in sup) / indep_n
        max_quality = max(s["alpha"] for s in sup)
        total_usd = sum(s["usd"] for s in sup)

        # de-correlated consensus: άθροισμα ποιότητας, μειωμένο για correlated wallets
        consensus = sum(s["trust"] * (s["alpha"] / 100.0) * (1 - s["corr_pen"]) for s in sup)

        # "chased?" — πληρώνεις πάνω από όσο μπήκαν οι supporters (spec §V.3)
        max_over = 0.015 if a["band"] == "A" else 0.03
        chased = p > avg_entry + max_over

        # ---- ΤΙΜΙΟ EV: ΔΕΝ εφευρίσκουμε πιθανότητα. implied = market price. ----
        # consensus_lean: ΜΟΝΟ ενδεικτικό, μη-επικυρωμένο, upward-only και μικρό.
        lean = _clamp(0.03 * math.log1p(consensus), 0, NUDGE_CAP_A if a["band"] == "A" else NUDGE_CAP_B)
        est_prob_display = round(p + (lean if not chased else 0), 3)

        # whale conflict: ισχυρή αντίθετη πλευρά στην ίδια αγορά (spec §V.5)
        opp_consensus = max((c for o, c in cond_sides.get(cond, {}).items() if o != outcome),
                            default=0.0)

        # ---- Convergence gates (spec §V.4: Independent Cluster) ----
        rejected = None
        if opp_consensus >= 0.6 * consensus:
            rejected = f"whale conflict: αντίθετη πλευρά consensus {opp_consensus:.2f}"
        elif indep_n < 2 and max_quality < 78:
            rejected = "solo & όχι Tier-A (χρειάζεται 2+ ή 1 πολύ ισχυρό)"
        elif chased:
            rejected = f"chased: τιμή {p*100:.1f}c > entry {avg_entry*100:.1f}c +{max_over*100:.0f}c"
        elif total_usd < MIN_POS_USD:
            rejected = "θέσεις κάτω από όριο σημαντικότητας"

        # conviction score (ΜΟΝΟ αληθινά σήματα — όχι EV)
        score = _clamp(
            32 * _sig(consensus - 0.5, 0.6)          # ανεξάρτητη σταθμισμένη ποιότητα
            + 26 * (avg_quality / 100)               # πόσο καλοί οι supporters
            + 20 * _sig(indep_n - 1, 1.0)            # πόσοι ανεξάρτητοι συμφωνούν
            + 12 * (1 - min(avg_corr, 1))            # de-correlation bonus
            + 10 * (0 if chased else _sig(avg_entry - p, 0.03)),  # μπήκες όσο/φθηνότερα απ' αυτούς
            0, 100)

        # conviction-based sizing (Kelly ΑΝΕΝΕΡΓΟ: δεν υπάρχει επικυρωμένο edge)
        if score >= 78:
            size = 1.2 if a["band"] == "A" else 2.0
        elif score >= 65:
            size = 0.6 if a["band"] == "A" else 1.0
        else:
            size = 0.3 if a["band"] == "A" else 0.4
        if indep_n < 2:
            size = min(size, 0.25)  # solo cap

        candidates.append({
            "sleeve": a["band"], "token": a["token"], "market": a["title"],
            "outcome": a["outcome"], "category": a["category"], "slug": a["slug"],
            "price": round(p, 3), "implied": round(p, 3),
            "est_prob": est_prob_display, "consensus": round(consensus, 2),
            "indep_n": indep_n, "avg_quality": round(avg_quality, 1),
            "chased": chased, "total_usd": total_usd,
            "supporters": [{"name": s["name"], "quality": s["alpha"], "entry": round(s["entry"], 3),
                            "usd": s["usd"], "corr": s["corr_pen"]} for s in sup],
            "avg_entry": round(avg_entry, 3),
            "size_pct": size, "score": round(score, 1),
            "rejected": rejected,
            "ev_status": "ΜΗ ΕΠΙΚΥΡΩΜΕΝΟ — χρειάζεται catalyst/backtest",
            "missing": ["catalyst", "liquidity-depth", "closing-line-value", "hype"],
        })
    return candidates


def discover_wallets(n=10, pool=60, min_vol=50_000, min_roi_pct=1.0):
    """
    Auto-discovery ποιοτικών πορτοφολιών για copy-signals.
    Κριτήρια ΕΥΘΥΓΡΑΜΜΙΣΜΕΝΑ με το εύρημα του engine:
      - ROI ≥ min_roi_pct: κόβει τους pure hedgers/market-makers (ROI ~0.3%) των οποίων
        οι θέσεις ΔΕΝ μεταφράζονται σε αντιγράψιμο κατευθυντικό σήμα.
      - Συνέπεια: παρών και στο μηνιαίο ΚΑΙ στο all-time leaderboard (όχι one-hit streak).
      - Όγκος ≥ min_vol: πραγματική δραστηριότητα, όχι τυχερός με 3 στοιχήματα.
    Επιστρέφει top-n με DiscoveryScore, έτοιμα ως watchlist entries.
    """
    seen = {}
    for period in ("month", "all"):
        try:
            rows = bot.fetch_leaderboard(period, "PNL", pool)
        except Exception:
            continue
        for r in rows:
            w = (r.get("proxyWallet") or "").lower()
            if not w:
                continue
            e = seen.setdefault(w, {"wallet": w, "name": r.get("userName") or w[:8],
                                    "windows": [], "best_rank": 10 ** 7, "pnl": 0.0, "vol": 0.0})
            e["windows"].append(period)
            e["best_rank"] = min(e["best_rank"], int(r.get("rank") or 10 ** 7))
            # κρατάμε τα all-time μεγέθη αν υπάρχουν, αλλιώς του μήνα
            if period == "all" or e["vol"] == 0:
                e["pnl"] = float(r.get("pnl") or 0)
                e["vol"] = float(r.get("vol") or 0)

    out = []
    for e in seen.values():
        if e["vol"] < min_vol or e["pnl"] <= 0:
            continue
        roi = 100 * e["pnl"] / e["vol"]
        if roi < min_roi_pct:
            continue  # hedger/MM profile — όχι αντιγράψιμο σήμα
        consistency = 1.0 if len(set(e["windows"])) == 2 else 0.45
        rank_bonus = 1.0 if e["best_rank"] <= 50 else 0.7 if e["best_rank"] <= 200 else 0.4
        e["roi"] = round(roi, 2)
        e["consistency"] = consistency
        e["discovery_score"] = round(_clamp(
            50 * _sig(roi / 100, 0.03) + 30 * consistency + 20 * rank_bonus, 0, 100), 1)
        out.append(e)
    out.sort(key=lambda x: x["discovery_score"], reverse=True)
    return out[:n]


def run_auto(n=10, min_score=65):
    """Auto-discovery + engine σε ένα βήμα."""
    found = discover_wallets(n=n)
    if not found:
        return {"note": "no_wallets_found", "approved": [], "watch": [], "blacklist": [],
                "discovered": []}
    wl = [{"wallet": f["wallet"], "label": f["name"], "weight": 1.0} for f in found]
    res = run(wl, min_score=min_score)
    res["discovered"] = [{k: f[k] for k in ("wallet", "name", "best_rank", "roi", "pnl",
                                            "vol", "consistency", "discovery_score")}
                         for f in found]
    return res


def run(watchlist, min_score=55):
    """Επιστρέφει blended portfolio + απορρίψεις, δομημένα για το UI."""
    CACHE.clear()
    if not watchlist:
        return {"note": "empty_watchlist", "approved": [], "watch": [], "blacklist": []}
    cands = build_candidates(watchlist)
    approved, watch, blacklist = [], [], []
    for c in cands:
        if c["rejected"]:
            blacklist.append({**c, "reason": c["rejected"]})
        elif c["score"] >= min_score:
            approved.append(c)
        else:
            watch.append(c)
    approved.sort(key=lambda x: x["score"], reverse=True)
    watch.sort(key=lambda x: x["score"], reverse=True)

    # book-level cluster caps (spec §XI): max 3% ανά event, cap ανά sleeve
    event_used, a_total, b_total, final = {}, 0.0, 0.0, []
    for c in approved:
        ev = c["slug"] or c["market"]
        cap_sleeve = 5.0 if c["sleeve"] == "A" else 10.0
        cur_total = a_total if c["sleeve"] == "A" else b_total
        alloc = c["size_pct"]
        if event_used.get(ev, 0) + alloc > 3.0:
            alloc = max(0, 3.0 - event_used.get(ev, 0))
        if cur_total + alloc > cap_sleeve:
            alloc = max(0, cap_sleeve - cur_total)
        c = {**c, "alloc_pct": round(alloc, 2)}
        event_used[ev] = event_used.get(ev, 0) + alloc
        if c["sleeve"] == "A":
            a_total += alloc
        else:
            b_total += alloc
        final.append(c)

    final = [c for c in final if c["alloc_pct"] > 0][:10]
    return {
        "approved": final,
        "watch": watch[:15],
        "blacklist": blacklist[:20],
        "summary": {
            "n_members": len(watchlist),
            "n_candidates": len(cands),
            "n_approved": len(final),
            "sleeveA_exposure": round(a_total, 2),
            "sleeveB_exposure": round(b_total, 2),
            "standby": len(final) < 2,
        },
    }
