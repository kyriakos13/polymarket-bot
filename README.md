# Polymarket mini-bot

Καθαρό, μικρό bot για **copy trading** και **φτηνά limit orders** στο Polymarket.
Μιλάει ΜΟΝΟ με τα επίσημα Polymarket API, το private key μένει τοπικά στο `.env`.

## Εγκατάσταση
```
pip install -r requirements.txt
copy .env.example .env      # και συμπλήρωσε τα στοιχεία σου
```

## Χρήση

**1. Βρες καλούς traders / δες τι κάνει ένα wallet** (δωρεάν, χωρίς key):
```
python bot.py trades 0xWALLET --limit 20
```

**2. Δες token IDs + τιμές μιας αγοράς** (βάλε το slug ή όλο το URL από το site):
```
python bot.py market https://polymarket.com/event/...
```

**3. Φτηνό limit order** (π.χ. 2 cents, 10 shares — ελάχιστο 5 shares):
```
python bot.py buy <token_id> 2 10
```

**4. Copy trading ενός wallet** (αντιγράφει μόνο BUY κάτω από όριο τιμής):
```
python bot.py copy 0xWALLET
```

## Web UI (το εύκολο)
```
python server.py
```
Άνοιξε **http://127.0.0.1:8000** στον browser σου.

Καρτέλες:
- **🏆 Κορυφαίοι** — leaderboard, με ⭐ για προσθήκη στη watchlist.
- **👤 Trader** — αναζήτηση με όνομα ή wallet, χαρτοφυλάκιο + trades (30/100/500/όλα), κουμπί Copy ανά trade.
- **⭐ Watchlist** — οι traders που παρακολουθείς, με βάρος εμπιστοσύνης + ROI/rank/κέρδος.
- **🧠 Έξυπνο Μείγμα** — consensus: αγορές όπου συμφωνούν πολλοί από τη watchlist σου,
  σταθμισμένο με τα βάρη, με φίλτρο κατηγορίας (πολιτική/οικονομία/αθλητικά/γεωπολιτική/tech).
- **📊 Αγορά** & **🔁 Copy Trading** — όπως από CLI, ΣΥΝ **🤖 Auto-copy όλης της watchlist**.

### 🤖 Auto-copy όλης της watchlist («καπάκι στις φθηνές τους»)
Μικρό σταθερό ποσό ανά trade. Μόλις ΟΠΟΙΟΣΔΗΠΟΤΕ από τη watchlist κάνει BUY ≤ max τιμή,
μπαίνει **limit order στο δικό του entry + slippage** — αν η τιμή έφυγε ψηλότερα, το order
περιμένει εκεί, **δεν κυνηγάει** (π.χ. wan123 μπήκε 23¢ → εσύ limit 25¢). Ρυθμίσεις: `$/trade`,
`max τιμή`, `slippage`, `όριο $/μέρα` (δίχτυ ασφαλείας). Σέβεται το `DRY_RUN` — ξεκίνα από εκεί.

### Πώς δουλεύει το «Έξυπνο Μείγμα» (dual-sleeve engine, `engine.py`)
Βάζεις 4-10 traders στη watchlist. Το engine:
- Βαθμολογεί κάθε wallet (**WalletQualityScore 0-100**) από ROI/rank/PnL — αξιόπιστα, θετικά σήματα.
- Χωρίζει τα candidates σε **Sleeve A** (1-5¢ longshot) και **Sleeve B** (35-65¢ value).
- Βγάζει σήματα όπου **ανεξάρτητα ποιοτικά wallets συγκλίνουν**, με:
  - **de-correlation** (τιμωρεί wallets που κρατάνε πάντα τα ίδια = copycat),
  - **whale-conflict** (απορρίπτει αν ισχυρά wallets είναι σε αντίθετες πλευρές),
  - **chased filter** (απορρίπτει αν η τιμή ξέφυγε πάνω από την είσοδό τους),
  - **position significance** (αγνοεί θέσεις < $500).
- Conviction-based sizing με caps ανά sleeve/event.

**🔎 Auto-discover:** αντί να βάζεις εσύ wallets, το κουμπί «Auto-discover» βρίσκει μόνο του
πορτοφόλια με **κατευθυντικό edge** — κριτήρια ευθυγραμμισμένα με το engine: ROI ≥ 1% (κόβει
τους hedgers/market-makers με ROI ~0.3% που δεν αντιγράφονται), **συνέπεια** (παρών σε μήνα ΚΑΙ
all-time leaderboard, όχι one-hit streak), όγκος ≥ $50k. Μετά τρέχει το ίδιο dual-sleeve engine
πάνω τους. Κάθε εύρημα έχει ⭐ για να το κρατήσεις στη watchlist.

**ΤΙ ΔΕΝ ΚΑΝΕΙ (και γιατί):** δεν βγάζει «EV +X%». Δοκίμασα — το realized edge των κορυφαίων
είναι **biased αρνητικά** γιατί είναι hedgers (τα κερδισμένα σκέλη μένουν ανοιχτά). Άρα αξιόπιστη
«πραγματική πιθανότητα» ΔΕΝ υπολογίζεται από δημόσια δεδομένα. Το engine δείχνει **σύγκλιση
ποιότητας**, όχι επικυρωμένο edge. Λείπουν επίσης (flagged, όχι εφευρημένα): catalyst, βάθος
ρευστότητας, closing-line value, hype. **Το επόμενο πραγματικό βήμα είναι backtesting** — μέχρι
τότε, κάθε «edge» είναι υπόθεση.

### 📒 Πείραμα (calibration journal)
Κάθε πόνταρισμα (DRY ή αληθινό, από όποιο flow) καταγράφεται στο `trades_log.json`.
Στην καρτέλα «📒 Πείραμα» πατάς «Ανανέωση» → λύνει τις κλειστές αγορές μέσω Gamma και
δείχνει **hit% vs μέση τιμή εισόδου (edge)** και PnL, **ανά flow** (χειροκίνητα/engine/auto-copy).
Αυτή είναι η μέθοδος «calibration first»: με $50 ο στόχος των πρώτων 25-50 pontαρισμάτων ΔΕΝ
είναι κέρδος — είναι να μετρήσεις αν έχεις edge, πριν μεγαλώσεις το ποσό.

## 📉 Backtest (`backtest.py`)
`python backtest.py` — τρέχει πάνω στις κλειστές θέσεις της watchlist (πηγή αλήθειας:
το πραγματικό `realizedPnl` του Polymarket) και δείχνει hit%/ROI ανά wallet & sleeve.
Αποτέλεσμα στα δοκιμαστικά wallets: **ROI ~0%** (κανένα αποδεδειγμένο edge σε επίπεδο
watchlist) — γι' αυτό υπάρχει το Πείραμα: να το επιβεβαιώσεις/διαψεύσεις με δικά σου δεδομένα.

## Ασφάλεια — διάβασέ το
- **Ξεκίνα ΠΑΝΤΑ με `DRY_RUN=true`** (προεπιλογή): το bot τυπώνει τι θα έκανε αλλά ΔΕΝ
  βάζει πραγματικά orders. Βάλε `DRY_RUN=false` μόνο όταν είσαι σίγουρος.
- Χρησιμοποίησε **λίγα λεφτά** στην αρχή. Το copy trading αντιγράφει και λάθος κινήσεις.
- Το `.env` (με το key σου) **δεν το ανεβάζεις/μοιράζεσαι ποτέ**. Είναι ήδη στο `.gitignore`.

## Ρυθμίσεις copy trading (στο .env)
- `COPY_SHARES` — πόσα shares ανά αντιγραμμένο trade (default 5)
- `MAX_COPY_PRICE_CENTS` — αγνόησε BUY πάνω από αυτή την τιμή (default 10c)
- `POLL_SECONDS` — κάθε πόσο ελέγχει για νέα trades (default 5)
