# Front_Office_Demo
Multi-user economic management system for a multi-year fantasy football league. Segregated budgets, long-term contracts with predefined cost escalation, installment plans with exposure caps, propose–accept–ratify trade workflow with automatic limit checks, and Excel audit exports. Built with Python and Streamlit; JSON storage; PIN-based access.
# Front Office — a miniature economy for a multi-year fantasy football league

Multi-user economic management system for a multi-year fantasy football league: segregated budgets, long-term contracts with predefined cost escalation, installment plans with exposure caps, a propose–accept–ratify trade workflow with automatic limit checks, and Excel audit exports. Built with Python and Streamlit.

> **Live demo:** [ADD YOUR .streamlit.app URL] — to try the participant view, log in as team *Real Tavolino* with PIN `1111`. The demo runs on sample data and can be freely explored.

---

## Why

Classic fantasy football resets every summer. We wanted the opposite: a league where rosters, balances and commitments carry over for years. That turns the rulebook into a system-design problem — the real challenge is an economy that neither blows up (irreversible concentration of the best assets) nor freezes (a paralyzed market), with incentives that hold over a long horizon and abuse prevention that does not rely on discretion.

## Core mechanics

| Mechanic | Rule | Principle |
|---|---|---|
| **Segregated accounts** | Two balances per participant (wages + cash), both debited at auction; no transfers between them | Separation of recurring spending capacity and liquidity |
| **Contract escalation** | Renewal cost grows by +10%, +20%, then +30% of the acquisition value (100 → 110 → 130 → 160 → 190 → 220); max 8 renewals per season; one asset class (goalkeepers) rotates yearly by rule | Future cost structure known ex ante; concentration is discouraged |
| **Installment plans** | Transfer fees payable over 2–3 seasons, integer installments with the last one largest; cap of 6 active plans; if the asset is traded, the debt stays with the originator | Amortization with an exposure cap; leverage is allowed but bounded |
| **Trade governance** | Propose → accept → administrator ratification; the system recomputes all post-trade balances; a breach voids the deal with an automatic fine for both parties | Four-eyes principle and preventive limit checks, with codified (not discretionary) sanctions |
| **End-of-season credits** | Near-uniform prizes across standings, allocated freely between the two accounts, once, within a deadline | Structural competitive rebalancing |
| **Youth draft (U21)** | Separate roster slots filled via draft in reverse standings order, with an age limit that shifts every season | Controlled asset pipeline outside the auction |

The full ruleset (19 pages, Italian) covers windows, fines, release refunds, edge-case clauses and the season-closing procedure.

## Architecture

- **App:** single-file Streamlit application (`app.py`, ~1,500 lines), Italian UI
- **State:** one JSON document on [JSONBin.io](https://jsonbin.io) — the app reloads the latest state before every write to minimize conflicts
- **Access:** per-team PIN login plus an administrator role (league management, phases and deadlines, ratifications, fines, data import/export)
- **Onboarding:** auction results imported from the standard Italian fantasy-league CSV export, with name/role enrichment from the official quotations file
- **Audit & backup:** one-click Excel export (teams, players, installment plans, full operations log) and full JSON export/import
- **Quality:** rule engine covered by end-to-end tests (contract escalation, installments, trades, season closing and rollover)

## Getting started

### 1. Create the storage bin

Create a free bin on JSONBin.io and paste the initial state below as its content. Note the **Bin ID**.

<details>
<summary>Initial state (click to expand)</summary>

```json
{
  "config": {
    "league": "My league",
    "season": "2026/27",
    "cur": "cr",
    "startCassa": 500,
    "startIng": 500,
    "roster": { "P": 3, "D": 8, "C": 8, "A": 6 },
    "u21Slots": 2,
    "maxConfirms": 8,
    "refundA": 50,
    "refundEst": 100,
    "maxPlans": 6,
    "rateDays": 7,
    "extraTable": [200, 200, 190, 190, 180, 180, 170, 170],
    "phase": { "mercato_open": true, "svincoli_open": false, "closure_open": false, "extra_deadline": "" }
  },
  "teams": [],
  "players": [],
  "plans": [],
  "trades": [],
  "closed": [],
  "extraDone": {},
  "log": []
}
```

</details>

### 2. Run locally

```bash
pip install -r requirements.txt
export JSONBIN_API_KEY="your JSONBin master key"
export JSONBIN_BIN_ID="your bin id"
export ADMIN_PIN="choose an admin PIN"
streamlit run app.py
```

### 3. Deploy on Streamlit Community Cloud

1. Fork or push this repository to your GitHub account.
2. On [share.streamlit.io](https://share.streamlit.io), create a **New app** pointing to the repo (`main`, `app.py`).
3. In *Advanced settings → Secrets*, add:

```toml
JSONBIN_API_KEY = "your JSONBin master key"
JSONBIN_BIN_ID = "your bin id"
ADMIN_PIN = "choose an admin PIN"
```

4. Deploy. Log in as **Direzione** with the admin PIN, then either load the sample league (*Dati → Carica lega di esempio*) or import real auction data (*Asta iniziale*).

| Environment variable | Purpose |
|---|---|
| `JSONBIN_API_KEY` | JSONBin master key (server-side only, never exposed to users) |
| `JSONBIN_BIN_ID` | The bin holding the league state |
| `ADMIN_PIN` | PIN for the administrator role |

## A note on method

The project was built with the support of an AI assistant (Claude, by Anthropic), with rules, constraints and testing owned by the author. Domain expertise plus AI for implementation: a setup I expect to become increasingly common for building internal tooling — including in risk management, which is my day job.

## License

MIT — see `LICENSE`. The app manages a private game among friends; no real-money features are included or implied.
