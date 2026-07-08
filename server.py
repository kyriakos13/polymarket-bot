"""
Τοπικό web UI για το Polymarket bot.

Τρέξε:  python server.py   και άνοιξε http://127.0.0.1:8000 στον browser.

Ο server (Python) κάνει ΟΛΕΣ τις κλήσεις προς το Polymarket μέσω του DoH resolver.
Ο browser σου μιλάει ΜΟΝΟ με το 127.0.0.1 — τίποτα δεν βγαίνει έξω από τον browser.
Δένει επίτηδες μόνο σε localhost (δεν είναι προσβάσιμο από το δίκτυο).
"""

import json
import datetime
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

import bot  # επαναχρησιμοποιεί resolver + fetch_* + place_buy + get_client


def gamma_get(path, params, timeout=12, retries=2):
    """GET προς Gamma με retry σε παροδικά σφάλματα/rate-limit (429/5xx/network)."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(f"{bot.GAMMA_API}{path}", params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"Gamma {r.status_code}")
        except Exception as e:
            last = e
        time.sleep(0.4 * (attempt + 1))
    raise last if last else RuntimeError("Gamma failed")
import engine  # dual-sleeve alpha copilot

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
WATCHLIST_FILE = os.path.join(HERE, "watchlist.json")
HOST, PORT = "127.0.0.1", 8000

# Οι δικές μας κατηγορίες -> επίσημα Polymarket tag ids (για server-side φιλτράρισμα)
CATEGORY_TAGS = {
    "Πολιτική": [2, 144],        # Politics, Elections
    "Οικονομία": [100328, 107],  # Economy, Business
    "Κρύπτο": [21],              # Crypto
    "Γεωπολιτική": [100265, 101970],  # Geopolitics, World
    "Tech": [1401],             # Tech
    "Αθλητικά": [1],            # Sports
    "Κουλτούρα": [596],         # Culture
}

_wl_lock = threading.Lock()


def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return []
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_watchlist(wl):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)


import re


def market_event_url(m):
    """Το σωστό link προς polymarket.com χρειάζεται το SLUG ΤΟΥ EVENT, όχι της αγοράς —
    διαφέρουν (π.χ. market slug 'will-norway-win-...-893' vs event slug 'world-cup-winner').
    Χωρίς αυτό τα links γυρνάνε 404 (Next.js __next_error__)."""
    events = m.get("events") or []
    slug = events[0].get("slug") if events else m.get("slug", "")
    return f"https://polymarket.com/event/{slug}"


def _clean_wallet(raw):
    m = re.search(r"0x[a-fA-F0-9]{40}", raw or "")
    return m.group(0).lower() if m else (raw or "").strip().lower()


def watchlist_add(wallet, label, weight):
    wallet = _clean_wallet(wallet)
    with _wl_lock:
        wl = load_watchlist()
        wl = [m for m in wl if m["wallet"] != wallet]  # χωρίς διπλά
        wl.append({"wallet": wallet, "label": label or wallet[:8], "weight": float(weight or 1)})
        save_watchlist(wl)
        return wl


def watchlist_remove(wallet):
    wallet = wallet.strip().lower()
    with _wl_lock:
        wl = [m for m in load_watchlist() if m["wallet"] != wallet]
        save_watchlist(wl)
        return wl


class CopyManager:
    """Παρακολουθεί ένα wallet σε background thread και αντιγράφει τα BUY του."""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.wallet = None
        self.shares = 5.0
        self.max_price = 0.10
        self.log = []          # πρόσφατα events για το UI
        self.mirrored = 0
        self.client = None

    def _add(self, msg):
        with self.lock:
            self.log.insert(0, {"t": time.strftime("%H:%M:%S"), "msg": msg})
            self.log = self.log[:100]

    def status(self):
        with self.lock:
            return {
                "running": self.thread is not None and self.thread.is_alive(),
                "wallet": self.wallet,
                "shares": self.shares,
                "max_cents": round(self.max_price * 100),
                "mirrored": self.mirrored,
                "dry_run": bot.DRY_RUN,
                "log": list(self.log),
            }

    def start(self, wallet, shares, max_cents):
        if self.thread and self.thread.is_alive():
            return False, "Τρέχει ήδη — σταμάτησέ το πρώτα."
        self.wallet = wallet.strip()
        self.shares = float(shares)
        self.max_price = float(max_cents) / 100.0
        self.mirrored = 0
        self.log = []
        self.stop_flag.clear()
        self.client = None if bot.DRY_RUN else bot.get_client()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        mode = "DRY_RUN (χωρίς πραγματικά orders)" if bot.DRY_RUN else "ΖΩΝΤΑΝΑ"
        self._add(f"Ξεκίνησε — αντιγράφω BUY ≤ {max_cents}c με {shares} shares. Mode: {mode}")
        return True, "ok"

    def stop(self):
        self.stop_flag.set()
        self._add("Διακόπηκε από τον χρήστη.")
        return True

    def _run(self):
        try:
            seen = {bot.trade_key(t) for t in bot.fetch_trades(self.wallet, limit=100)}
        except Exception as e:
            self._add(f"Σφάλμα αρχικοποίησης: {e}")
            return
        poll = 5
        while not self.stop_flag.is_set():
            try:
                trades = bot.fetch_trades(self.wallet, limit=50)
            except Exception as e:
                self._add(f"Σφάλμα δικτύου, ξαναπροσπαθώ: {e}")
                self.stop_flag.wait(poll)
                continue
            for t in reversed(trades):
                k = bot.trade_key(t)
                if k in seen:
                    continue
                seen.add(k)
                side = t.get("side")
                price = float(t.get("price", 0))
                title = t.get("title", "")
                outcome = t.get("outcome", "")
                if side == "BUY" and 0 < price <= self.max_price:
                    try:
                        bot.place_buy(self.client, t["asset"], price, self.shares,
                                      source="copy-1wallet", title=title, outcome=outcome)
                        self.mirrored += 1
                        tag = "[DRY] " if bot.DRY_RUN else "[ORDER] "
                        self._add(f"{tag}Αντιγράφω BUY {self.shares} @ {price*100:.1f}c — {outcome} | {title}")
                    except Exception as e:
                        self._add(f"Σφάλμα order: {e}")
                elif side == "BUY":
                    self._add(f"Προσπερνώ BUY @ {price*100:.1f}c (> όριο) — {outcome}")
                else:
                    self._add(f"(target SELL @ {price*100:.1f}c — {outcome}) δεν αντιγράφεται")
            self.stop_flag.wait(poll)


copy_mgr = CopyManager()


class AutoCopyManager:
    """
    Auto-copy ΟΛΗΣ της watchlist: όταν οποιοσδήποτε από τη λίστα κάνει BUY,
    μπαίνουμε "καπάκι" με μικρό σταθερό ποσό — ΜΟΝΟ αν η τιμή είναι ακόμα κοντά
    στην είσοδό του (limit στο entry+slippage, ποτέ chase πάνω από αυτό).
    Ημερήσιο όριο δαπάνης ως δίχτυ ασφαλείας.
    """

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.cfg = {"usd": 2.0, "max_cents": 30.0, "slip_cents": 2.0, "daily_cap": 20.0}
        self.spent = 0.0
        self.spent_day = None
        self.mirrored = 0
        self.log = []
        self.client = None
        self.wallets = []

    def _add(self, msg):
        with self.lock:
            self.log.insert(0, {"t": time.strftime("%H:%M:%S"), "msg": msg})
            self.log = self.log[:150]

    def status(self):
        with self.lock:
            return {
                "running": self.thread is not None and self.thread.is_alive(),
                "cfg": dict(self.cfg), "mirrored": self.mirrored,
                "spent_today": round(self.spent, 2), "dry_run": bot.DRY_RUN,
                "n_wallets": len(self.wallets), "log": list(self.log),
            }

    def start(self, cfg):
        if self.thread and self.thread.is_alive():
            return False, "Τρέχει ήδη."
        wl = load_watchlist()
        if not wl:
            return False, "Άδεια watchlist."
        self.cfg.update({k: float(cfg[k]) for k in self.cfg if k in cfg})
        self.wallets = wl
        self.mirrored = 0
        self.spent = 0.0
        self.spent_day = time.strftime("%Y-%m-%d")
        self.log = []
        self.stop_flag.clear()
        self.client = None if bot.DRY_RUN else bot.get_client()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        mode = "DRY_RUN" if bot.DRY_RUN else "ΖΩΝΤΑΝΑ"
        c = self.cfg
        self._add(f"Auto-copy {len(wl)} wallets [{mode}] — ${c['usd']}/trade, "
                  f"max {c['max_cents']:.0f}c, slip +{c['slip_cents']:.0f}c, όριο ${c['daily_cap']}/μέρα")
        return True, "ok"

    def stop(self):
        self.stop_flag.set()
        self._add("Διακόπηκε.")

    def _run(self):
        seen = {}
        for m in self.wallets:
            try:
                seen[m["wallet"]] = {bot.trade_key(t) for t in bot.fetch_trades(m["wallet"], limit=60)}
            except Exception:
                seen[m["wallet"]] = set()
        poll = int(os.getenv("POLL_SECONDS", "5"))
        while not self.stop_flag.is_set():
            today = time.strftime("%Y-%m-%d")
            if today != self.spent_day:
                self.spent_day, self.spent = today, 0.0
                self._add("Νέα μέρα — μηδενισμός ημερήσιου ορίου.")
            for m in self.wallets:
                if self.stop_flag.is_set():
                    return
                w, label = m["wallet"], m.get("label") or m["wallet"][:8]
                try:
                    trades = bot.fetch_trades(w, limit=30)
                except Exception:
                    continue
                for t in reversed(trades):
                    k = bot.trade_key(t)
                    if k in seen[w]:
                        continue
                    seen[w].add(k)
                    if t.get("side") != "BUY":
                        continue
                    price = float(t.get("price", 0))
                    cfg = self.cfg
                    if not (0 < price <= cfg["max_cents"] / 100.0):
                        self._add(f"⏭ {label}: BUY @ {price*100:.1f}c > όριο {cfg['max_cents']:.0f}c — {t.get('outcome')} | {(t.get('title') or '')[:40]}")
                        continue
                    limit_price = round(min(price + cfg["slip_cents"] / 100.0, 0.99), 3)
                    shares = max(bot.MIN_SHARES, round(cfg["usd"] / limit_price))
                    cost = shares * limit_price
                    if self.spent + cost > cfg["daily_cap"]:
                        self._add(f"🛑 Ημερήσιο όριο ${cfg['daily_cap']} — προσπερνώ {label}: {t.get('outcome')} @ {price*100:.1f}c")
                        continue
                    try:
                        bot.place_buy(self.client, t["asset"], limit_price, shares,
                                      source="autocopy", title=t.get("title", ""),
                                      outcome=t.get("outcome", ""))
                        self.spent += cost
                        self.mirrored += 1
                        tag = "[DRY]" if bot.DRY_RUN else "[ORDER]"
                        self._add(f"✅ {tag} καπάκι στον {label}: {shares} shares @ {limit_price*100:.1f}c "
                                  f"(αυτός μπήκε {price*100:.1f}c, ~${cost:.2f}) — {t.get('outcome')} | {(t.get('title') or '')[:40]}")
                    except Exception as e:
                        self._add(f"❌ σφάλμα order ({label}): {e}")
                time.sleep(0.15)  # ευγενικό rate προς το API
            self.stop_flag.wait(poll)


autocopy_mgr = AutoCopyManager()


class EngineRunner:
    """Τρέχει το engine (auto-discover ή watchlist) σε background thread ώστε το UI
    να μη περιμένει 30-40s σε ένα HTTP request (που κρεμάει -> 'failed to fetch').
    Το UI κάνει poll στο /api/engine/status."""

    def __init__(self):
        self.thread = None
        self.lock = threading.Lock()
        self.state = {"running": False, "done": False, "result": None, "error": None,
                      "started": 0, "mode": None}

    def status(self):
        with self.lock:
            s = dict(self.state)
        if s["running"]:
            s["elapsed"] = int(time.time() - s["started"])
        return s

    def start(self, mode, n, min_score):
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "done": False, "result": None, "error": None,
                          "started": time.time(), "mode": mode}
        self.thread = threading.Thread(target=self._run, args=(mode, n, min_score), daemon=True)
        self.thread.start()
        return True

    def _run(self, mode, n, min_score):
        try:
            if mode == "auto":
                res = engine.run_auto(n=n, min_score=min_score)
            else:
                res = engine.run(load_watchlist(), min_score=min_score)
            with self.lock:
                self.state.update({"running": False, "done": True, "result": res})
        except Exception as e:
            with self.lock:
                self.state.update({"running": False, "done": True, "error": str(e)})


engine_runner = EngineRunner()


class KeywordRadar:
    """Polls το Polymarket για αγορές που ταιριάζουν με keywords· χτυπάει alarm σε νέο match.
    Πηγές: public-search ανά keyword + σάρωση των νεότερων αγορών (substring)."""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.keywords = []
        self.poll = 20
        self.matches = {}     # key -> record
        self.seen = set()
        self.new_serial = 0   # αυξάνεται σε κάθε νέο match (το UI το συγκρίνει για alarm)
        self.first_pass = True

    def status(self):
        with self.lock:
            ms = sorted(self.matches.values(), key=lambda x: x["first_seen"], reverse=True)
            return {
                "running": self.thread is not None and self.thread.is_alive(),
                "keywords": list(self.keywords), "poll": self.poll,
                "new_serial": self.new_serial, "count": len(ms),
                "matches": ms[:80],
            }

    def start(self, keywords, poll):
        if self.thread and self.thread.is_alive():
            return False, "Τρέχει ήδη."
        kws = [k.strip().lower() for k in keywords if k.strip()]
        if not kws:
            return False, "Δώσε τουλάχιστον ένα keyword."
        self.keywords = kws
        self.poll = max(5, int(poll))
        self.matches = {}
        self.seen = set()
        self.new_serial = 0
        self.first_pass = True
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True, "ok"

    def stop(self):
        self.stop_flag.set()

    def _add(self, key, rec):
        """Κρατάμε ΜΟΝΟ ανοιχτές (παίξιμες) αγορές. Alarm μόνο για γνήσια νέο ανοιχτό match."""
        if rec["closed"]:
            return
        if key in self.matches:
            return
        is_new = not self.first_pass
        rec["is_new"] = is_new
        rec["first_seen"] = int(time.time())
        with self.lock:
            self.matches[key] = rec
            self.seen.add(key)
            if is_new:
                self.new_serial += 1

    def _run(self):
        while not self.stop_flag.is_set():
            try:
                still_open = set()
                # 1) public-search ανά keyword
                for kw in self.keywords:
                    r = requests.get(f"{bot.GAMMA_API}/public-search",
                                     params={"q": kw, "limit_per_type": 20}, timeout=15)
                    for e in r.json().get("events", []):
                        key = f"e{e.get('id')}"
                        closed = bool(e.get("closed"))
                        if not closed:
                            still_open.add(key)
                        self._add(key, {
                            "key": key, "title": e.get("title", ""), "slug": e.get("slug", ""),
                            "keyword": kw, "closed": closed, "created": e.get("createdAt"),
                            "url": f"https://polymarket.com/event/{e.get('slug','')}",
                        })
                # 2) σάρωση νεότερων αγορών (πιάνει ό,τι μόλις δημιουργήθηκε — ήδη μόνο ανοιχτές)
                r = requests.get(f"{bot.GAMMA_API}/markets",
                                 params={"active": "true", "closed": "false", "limit": 200,
                                         "order": "createdAt", "ascending": "false"}, timeout=15)
                for m in r.json():
                    q = (m.get("question") or "").lower()
                    hit = next((k for k in self.keywords if k in q), None)
                    if hit:
                        key = f"m{m.get('id')}"
                        still_open.add(key)
                        self._add(key, {
                            "key": key, "title": m.get("question", ""), "slug": m.get("slug", ""),
                            "keyword": hit, "closed": False, "created": m.get("createdAt"),
                            "url": market_event_url(m),
                        })
                # αφαίρεσε ό,τι έκλεισε στο μεταξύ (δεν παίζεται πια)
                with self.lock:
                    for key in [k for k in self.matches if k.startswith("e") and k not in still_open]:
                        del self.matches[key]
            except Exception:
                pass
            self.first_pass = False
            self.stop_flag.wait(self.poll)


radar = KeywordRadar()


class NewMarketsFeed:
    """Poll συνεχώς τις ΝΕΟΤΕΡΕΣ αγορές του Polymarket (χωρίς keyword) — 'ό,τι μόλις βγήκε'.
    Κάθε αγορά κατηγοριοποιείται (bot.categorize) ώστε το UI να φιλτράρει με checkboxes."""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.poll = 15
        self.items = {}  # market id -> record
        self.first_pass = True

    def status(self):
        with self.lock:
            rows = sorted(self.items.values(), key=lambda x: x["created"] or "", reverse=True)
            return {"running": self.thread is not None and self.thread.is_alive(),
                    "poll": self.poll, "count": len(rows), "items": rows[:300]}

    def start(self, poll):
        if self.thread and self.thread.is_alive():
            return False, "Τρέχει ήδη."
        self.poll = max(5, int(poll))
        self.items = {}
        self.first_pass = True
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True, "ok"

    def stop(self):
        self.stop_flag.set()

    def _run(self):
        while not self.stop_flag.is_set():
            try:
                r = requests.get(f"{bot.GAMMA_API}/markets",
                                 params={"active": "true", "closed": "false", "limit": 200,
                                         "order": "createdAt", "ascending": "false"}, timeout=15)
                for m in r.json():
                    key = str(m.get("id"))
                    if key in self.items:
                        continue
                    q = m.get("question", "")
                    with self.lock:
                        self.items[key] = {
                            "key": key, "title": q, "slug": m.get("slug", ""),
                            "category": bot.categorize(q), "created": m.get("createdAt"),
                            "url": market_event_url(m),
                        }
                # κράτα μόνο τις πιο πρόσφατες ~600 (μνήμη)
                if len(self.items) > 600:
                    with self.lock:
                        keep = sorted(self.items.values(), key=lambda x: x["created"] or "", reverse=True)[:500]
                        self.items = {r["key"]: r for r in keep}
            except Exception:
                pass
            self.first_pass = False
            self.stop_flag.wait(self.poll)


newmarkets = NewMarketsFeed()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # χωρίς θόρυβο στην κονσόλα

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        # ΠΟΤΕ cache — ώστε ο browser να παίρνει πάντα τον τελευταίο κώδικα (όχι Ctrl+F5)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, e):
        self._send(500, {"error": str(e)})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/" or u.path == "/index.html":
                with open(INDEX, "rb") as f:
                    return self._send(200, f.read(), "text/html")
            if u.path == "/api/config":
                return self._send(200, {
                    "dry_run": bot.DRY_RUN,
                    "min_shares": bot.MIN_SHARES,
                    "has_key": bool(os.getenv("PRIVATE_KEY", "").strip()),
                })
            if u.path == "/api/account":
                return self._send(200, bot.account_check())
            if u.path == "/api/journal":
                data = bot.journal_load()
                return self._send(200, {"entries": data[::-1][:200], "stats": bot.journal_stats(data)})
            if u.path == "/api/leaderboard":
                rows = bot.fetch_leaderboard(q.get("period", ["month"])[0], q.get("by", ["PNL"])[0], 25)
                return self._send(200, rows)
            if u.path == "/api/positions":
                return self._send(200, bot.fetch_positions(q["wallet"][0]))
            if u.path == "/api/trades":
                wallet = q["wallet"][0]
                if q.get("all", ["0"])[0] == "1":
                    return self._send(200, bot.fetch_all_trades(wallet))
                limit = int(q.get("limit", ["30"])[0])
                offset = int(q.get("offset", ["0"])[0])
                return self._send(200, bot.fetch_trades(wallet, limit, offset))
            if u.path == "/api/search":
                return self._send(200, bot.search_profiles(q["q"][0]))
            if u.path == "/api/market":
                return self._send(200, bot.fetch_market(q["slug"][0]))
            if u.path == "/api/watchlist":
                return self._send(200, load_watchlist())
            if u.path == "/api/watchlist/stats":
                out = []
                for m in load_watchlist():
                    try:
                        st = bot.fetch_user_stats(m["wallet"])
                    except Exception:
                        st = {"wallet": m["wallet"], "userName": None, "rank": None,
                              "pnl": 0, "vol": 0, "roi": None}
                    st.update({"label": m["label"], "weight": m["weight"]})
                    out.append(st)
                return self._send(200, out)
            if u.path == "/api/signals":
                members = load_watchlist()
                if not members:
                    return self._send(200, {"signals": [], "note": "empty_watchlist"})
                source = q.get("source", ["positions"])[0]
                cat = q.get("category", ["Όλες"])[0]
                min_score = float(q.get("min_score", ["2"])[0])
                sigs = bot.build_signals(members, source=source)
                if cat != "Όλες":
                    sigs = [s for s in sigs if s["category"] == cat]
                sigs = [s for s in sigs if s["score"] >= min_score]
                return self._send(200, {"signals": sigs[:60], "total_members": len(members)})
            if u.path == "/api/engine/start":
                mode = q.get("mode", ["watchlist"])[0]
                min_score = float(q.get("min_score", ["65"])[0])
                n = int(q.get("n", ["12"])[0])
                ok = engine_runner.start(mode, n, min_score)
                return self._send(200, {"ok": ok, "msg": "" if ok else "τρέχει ήδη"})
            if u.path == "/api/engine/status":
                return self._send(200, engine_runner.status())
            if u.path == "/api/resolve":
                tokens = [t for t in q.get("tokens", [""])[0].split(",") if t]
                return self._send(200, bot.resolve_tokens(tokens))
            if u.path == "/api/copy/status":
                return self._send(200, copy_mgr.status())
            if u.path == "/api/autocopy/status":
                return self._send(200, autocopy_mgr.status())
            if u.path == "/api/radar/status":
                return self._send(200, radar.status())
            if u.path == "/api/newmarkets/status":
                return self._send(200, newmarkets.status())
            if u.path == "/api/allmarkets":
                since_h = q.get("since_hours", [""])[0]
                limit = min(int(q.get("limit", ["50"])[0]), 100)
                offset = int(q.get("offset", ["0"])[0])
                ascending = q.get("ascending", ["false"])[0]
                cats = [c for c in q.get("cats", [""])[0].split(",") if c]
                # server-side φιλτράρισμα με ΕΠΙΣΗΜΑ tags (όχι client-side στη σελίδα)
                params = [("active", "true"), ("closed", "false"), ("limit", str(limit)),
                          ("offset", str(offset)), ("order", "createdAt"), ("ascending", ascending)]
                tag_ids = []
                for c in cats:
                    tag_ids += CATEGORY_TAGS.get(c, [])
                for tid in tag_ids:
                    params.append(("tag_id", str(tid)))
                if len(tag_ids) > 1:
                    params.append(("related_tags", "true"))
                if since_h:
                    since = (datetime.datetime.now(datetime.timezone.utc)
                             - datetime.timedelta(hours=float(since_h))).strftime("%Y-%m-%dT%H:%M:%SZ")
                    params.append(("start_date_min", since))
                rows = gamma_get("/markets", params)
                out = [{
                    "title": m.get("question", ""), "category": bot.categorize(m.get("question", "")),
                    "created": m.get("createdAt"), "url": market_event_url(m),
                } for m in rows]
                return self._send(200, {"items": out, "count": len(out), "offset": offset, "limit": limit})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._err(e)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/buy":
                cents = float(body["cents"])
                shares = float(body["shares"])
                price = cents / 100.0
                if not (0 < price < 1):
                    return self._send(400, {"error": "τιμή 1-99 cents"})
                if shares < bot.MIN_SHARES:
                    return self._send(400, {"error": f"ελάχιστο {bot.MIN_SHARES} shares"})
                client = None if bot.DRY_RUN else bot.get_client()
                resp = bot.place_buy(client, body["token_id"], price, shares,
                                     source=body.get("source", "manual"),
                                     title=body.get("title", ""),
                                     outcome=body.get("outcome", ""))
                return self._send(200, {"dry_run": bot.DRY_RUN, "resp": str(resp),
                                        "cost": round(price * shares, 2)})
            if u.path == "/api/journal/refresh":
                data, changed = bot.journal_refresh()
                return self._send(200, {"changed": changed,
                                        "entries": data[::-1][:200],
                                        "stats": bot.journal_stats(data)})
            if u.path == "/api/copy/start":
                ok, msg = copy_mgr.start(body["wallet"], body.get("shares", 5), body.get("max_cents", 10))
                return self._send(200 if ok else 400, {"ok": ok, "msg": msg})
            if u.path == "/api/copy/stop":
                copy_mgr.stop()
                return self._send(200, {"ok": True})
            if u.path == "/api/autocopy/start":
                ok, msg = autocopy_mgr.start(body)
                return self._send(200 if ok else 400, {"ok": ok, "msg": msg})
            if u.path == "/api/autocopy/stop":
                autocopy_mgr.stop()
                return self._send(200, {"ok": True})
            if u.path == "/api/radar/start":
                ok, msg = radar.start(body.get("keywords", []), body.get("poll", 20))
                return self._send(200 if ok else 400, {"ok": ok, "msg": msg})
            if u.path == "/api/radar/stop":
                radar.stop()
                return self._send(200, {"ok": True})
            if u.path == "/api/newmarkets/start":
                ok, msg = newmarkets.start(body.get("poll", 15))
                return self._send(200 if ok else 400, {"ok": ok, "msg": msg})
            if u.path == "/api/newmarkets/stop":
                newmarkets.stop()
                return self._send(200, {"ok": True})
            if u.path == "/api/watchlist/add":
                wl = watchlist_add(body["wallet"], body.get("label", ""), body.get("weight", 1))
                return self._send(200, wl)
            if u.path == "/api/watchlist/remove":
                wl = watchlist_remove(body["wallet"])
                return self._send(200, wl)
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._err(e)


if __name__ == "__main__":
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"\n❌ Δεν μπόρεσα να δεσμεύσω το port {PORT}: {e}")
        print("   Πιθανότατα τρέχει ήδη άλλος server. Κλείσε τον (Ctrl+C στο άλλο παράθυρο)")
        print("   ή σκότωσε όλα τα python: στο PowerShell -> taskkill /F /IM python.exe")
        raise SystemExit(1)
    srv.daemon_threads = True
    print(f"Polymarket UI: http://{HOST}:{PORT}   (DRY_RUN={bot.DRY_RUN})")
    print("Ctrl+C για διακοπή.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nΤερματισμός.")
        srv.shutdown()
