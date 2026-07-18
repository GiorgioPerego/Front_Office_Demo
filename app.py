# =============================================================================
#  FRONT OFFICE — Fantacalcio Manageriale Pluriennale (multi-utente)
#  ---------------------------------------------------------------------------
#  Hosting:  Hugging Face Spaces (SDK: streamlit)
#  Storage:  JSONBin.io (bin condiviso) — secrets via variabili d'ambiente
#  Login:    per squadra con PIN + ruolo "Direzione" (admin)
#
#  Variabili d'ambiente richieste (Settings → Variables and secrets su HF):
#    JSONBIN_API_KEY   -> X-Master-Key del tuo account JSONBin
#    JSONBIN_BIN_ID    -> id del bin (creane uno con contenuto {} )
#    ADMIN_PIN         -> PIN della Direzione (es. 2468)
#
#  Regole implementate (Regolamento v2 della Lega):
#   - due conti separati per squadra: CASSA e INGAGGI (500+500 il 1° anno)
#   - asta: doppio addebito (prezzo -> ingaggio e cartellino in cassa)
#   - scala conferme +10/+20/+30% (poi sempre +30%) sul valore d'asta V
#   - max 8 conferme (U21 inclusi), portieri MAI confermabili
#   - svincoli: 50% Serie A / 100% estero-ritiro / 100% Astori, su entrambi i conti
#   - rosa fissa 3-8-8-6 + 2 slot U21 (draft, listone che scala: 2026/27 -> nati>=2005)
#   - U21 fuori età: promozione a ingaggio 1 (+1 a conferma) o svincolo; Calafiori (*)
#   - rateizzazione: solo asta estiva, 2-3 anni, rate intere (ultima maggiore),
#     scelta entro 7 giorni irrevocabile, max 6 piani (anche se il giocatore è
#     stato scambiato), estinzione anticipata sempre possibile, rata a chiusura
#   - scambi: chi cede libera l'ingaggio corrente, chi riceve impegna V;
#     conguagli SOLO in cassa; sforo -> annullo + multa 10 a entrambe
#   - acquisti tra club in crediti di cassa (non rateizzabili)
#   - crediti extra 200/200/190/190/180/180/170/170, allocazione libera UNA volta
#   - fasi con lock: mercato aperto/chiuso, finestra svincoli, chiusura stagione
#     con deadline per l'allocazione extra
#   - Excel di backup sincronizzato scaricabile da chiunque, in ogni momento
# =============================================================================

import os
import io
import json
import time
import uuid
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import altair as alt
import streamlit as st

# =============================================================================
# 1) COSTANTI E CONFIG DI BASE
# =============================================================================

TZ = ZoneInfo("Europe/Rome")

JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID = os.environ.get("JSONBIN_BIN_ID", "")
ADMIN_PIN = str(os.environ.get("ADMIN_PIN", "0000"))

JB_BASE = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
JB_HEADERS = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}

ROLE_NAME = {"P": "Portiere", "D": "Difensore", "C": "Centrocampista", "A": "Attaccante"}
ROLE_ORDER = ["P", "D", "C", "A"]
ROLE_EMOJI = {"P": "🧤", "D": "🛡️", "C": "⚙️", "A": "🎯"}

GREEN = "#1C7A45"
DEEP = "#0E4A2B"
BRASS = "#A6781B"
BRICK = "#AE3A2C"

uid = lambda: uuid.uuid4().hex[:9]
now_ms = lambda: int(time.time() * 1000)
today = lambda: dt.datetime.now(TZ).date()


def fmt(n):
    try:
        return f"{int(round(float(n))):,}".replace(",", ".")
    except Exception:
        return str(n)


# =============================================================================
# 2) REGOLE PURE (identiche alla dashboard / regolamento)
# =============================================================================

def mult_k(k: int) -> float:
    """Moltiplicatore ingaggio dopo k conferme: 1.00, 1.10, 1.30, 1.60, 1.90, ..."""
    return 1 + (0.10 if k >= 1 else 0) + (0.20 if k >= 2 else 0) + max(0, k - 2) * 0.30


import math


def wage_at(base: int, k: int) -> int:
    return math.ceil(base * mult_k(k) - 1e-9)


def next_wage(p: dict) -> int:
    if p.get("u21"):
        return 0
    if p.get("promoted"):
        return int(p.get("wage", 1)) + 1
    return wage_at(int(p.get("base", 0)), int(p.get("k", 0)) + 1)


def make_rates(total: int, years: int) -> list:
    """Rate intere, ultima piu' grande: 100/3 -> [33,33,34]."""
    q = total // years
    r = [q] * (years - 1)
    r.append(total - q * (years - 1))
    return r


def season_start(season: str) -> int:
    try:
        return int(str(season).split("/")[0])
    except Exception:
        return 2026


def next_season(season: str) -> str:
    a = season_start(season)
    return f"{a + 1}/{str((a + 2) % 100).zfill(2)}"


def min_birth(season: str) -> int:
    """U21 eleggibili: nati dal (anno_inizio - 21) in poi. 2026/27 -> 2005."""
    return season_start(season) - 21


def u21_ok(p: dict, season: str) -> bool:
    return bool(p.get("u21")) and int(p.get("birthYear") or 0) >= min_birth(season)


def plan_active(pl: dict) -> bool:
    return int(pl.get("paid", 0)) < int(pl.get("years", 0))


def plan_residual(pl: dict) -> int:
    return sum(pl["rates"][int(pl.get("paid", 0)):])


# =============================================================================
# 3) STATO: default, normalizzazione, seed di esempio
# =============================================================================

def base_config() -> dict:
    return {
        "league": "La mia lega",
        "season": "2026/27",
        "cur": "cr",
        "startCassa": 500,
        "startIng": 500,
        "roster": {"P": 3, "D": 8, "C": 8, "A": 6},
        "u21Slots": 2,
        "maxConfirms": 8,
        "refundA": 50,
        "refundEst": 100,
        "maxPlans": 6,
        "rateDays": 7,
        "extraTable": [200, 200, 190, 190, 180, 180, 170, 170],
        "phase": {"mercato_open": True, "svincoli_open": False, "closure_open": False, "extra_deadline": ""},
    }


def empty_state() -> dict:
    return {"config": base_config(), "teams": [], "players": [], "plans": [],
            "trades": [], "closed": [], "extraDone": {}, "log": []}


def normalize(data: dict) -> dict:
    """Rende compatibili bin nuovi/vecchi e i JSON esportati dall'app Claude."""
    if not isinstance(data, dict) or "config" not in data:
        return empty_state()
    d = empty_state()
    d["config"].update(data.get("config") or {})
    d["config"].setdefault("phase", {})
    for k, v in base_config()["phase"].items():
        d["config"]["phase"].setdefault(k, v)
    d["teams"] = data.get("teams") or []
    for t in d["teams"]:
        t.setdefault("pin", "0000")
        t.setdefault("cassa", d["config"]["startCassa"])
        t.setdefault("ing", t.pop("saldoIngaggi", d["config"]["startIng"]) if "saldoIngaggi" in t else t.get("ing", d["config"]["startIng"]))
    d["players"] = data.get("players") or []
    for p in d["players"]:
        p.setdefault("k", 0); p.setdefault("u21", False); p.setdefault("birthYear", None)
        p.setdefault("calafiori", False); p.setdefault("promoted", False)
        p.setdefault("viaTrade", False); p.setdefault("asta", True)
        p.setdefault("createdAt", now_ms())
    d["plans"] = data.get("plans") or []
    d["trades"] = data.get("trades") or []
    ed = data.get("extraDone")
    if isinstance(ed, list):  # formato artifact: lista di teamId
        d["extraDone"] = {tid: {"pos": None, "toIng": None, "toCassa": None, "ts": now_ms()} for tid in ed}
    elif isinstance(ed, dict):
        d["extraDone"] = ed
    d["closed"] = data.get("closed") or []
    d["log"] = data.get("log") or []
    return d


def example_state() -> dict:
    d = empty_state()
    cfg = d["config"]; cfg["league"] = "La Lega dei Bro"
    names = [("Real Tavolino", "Jo", "#1C7A45", "1111"), ("Atletico Divano", "Raoul", "#AE3A2C", "2222"),
             ("Borussia Spritz", "Iso", "#2C6E8E", "3333"), ("Inter Nazionale Bro", "Villa", "#A6781B", "4444")]
    for nm, mg, col, pin in names:
        d["teams"].append({"id": uid(), "name": nm, "manager": mg, "color": col, "pin": pin,
                           "cassa": cfg["startCassa"], "ing": cfg["startIng"]})
    T = {t["manager"]: t for t in d["teams"]}

    def add(t, name, role, base, k=0, u21=False, by=None, plan=None, cala=False):
        p = {"id": uid(), "teamId": t["id"], "name": name, "role": role,
             "base": 0 if u21 else base, "wage": 0 if u21 else wage_at(base, k), "k": 0 if u21 else k,
             "u21": u21, "birthYear": by, "calafiori": cala, "promoted": False,
             "viaTrade": False, "asta": True, "createdAt": now_ms() - 2 * 86400000}
        d["players"].append(p)
        if not u21:
            t["ing"] -= p["wage"]
            if plan:
                d["plans"].append({"id": uid(), "teamId": t["id"], "playerId": p["id"], "playerName": name,
                                   "total": base, "years": plan, "rates": make_rates(base, plan), "paid": 0})
            else:
                t["cassa"] -= base

    add(T["Jo"], "Sommer", "P", 18); add(T["Jo"], "Bastoni", "D", 41, 1); add(T["Jo"], "Dimarco", "D", 33)
    add(T["Jo"], "Barella", "C", 58, 1); add(T["Jo"], "Pulisic", "C", 72); add(T["Jo"], "Lautaro", "A", 100, plan=3)
    add(T["Jo"], "Camarda", "A", 0, u21=True, by=2008)
    add(T["Raoul"], "Di Gregorio", "P", 22); add(T["Raoul"], "Bremer", "D", 47); add(T["Raoul"], "Calafiori", "D", 29)
    add(T["Raoul"], "Koopmeiners", "C", 63, 1); add(T["Raoul"], "Vlahovic", "A", 95, plan=2)
    add(T["Raoul"], "Ndour", "C", 0, u21=True, by=2004, cala=True)  # fuori eta' 2026/27 -> demo promozione
    add(T["Iso"], "Maignan", "P", 26); add(T["Iso"], "Hernandez", "D", 51, 1); add(T["Iso"], "Tonali", "C", 66)
    add(T["Iso"], "Frattesi", "C", 38); add(T["Iso"], "Retegui", "A", 78)
    add(T["Iso"], "Pafundi", "C", 0, u21=True, by=2006)
    add(T["Villa"], "Carnesecchi", "P", 19); add(T["Villa"], "Buongiorno", "D", 43); add(T["Villa"], "McTominay", "C", 55, 1)
    add(T["Villa"], "Yildiz", "C", 48); add(T["Villa"], "Thuram", "A", 84)
    add(T["Villa"], "Esposito", "A", 0, u21=True, by=2005)
    T["Jo"]["cassa"] += 40; T["Iso"]["cassa"] += 25
    d["log"].append({"id": uid(), "ts": now_ms(), "season": cfg["season"], "type": "sistema",
                     "teamId": None, "teamId2": None, "player": "",
                     "dC": 0, "dI": 0, "note": "Lega di esempio creata (PIN squadre: 1111/2222/3333/4444)"})
    return d


# =============================================================================
# 4) JSONBIN I/O (load, save, mutate transazionale)
# =============================================================================

def jb_ready() -> bool:
    return bool(JSONBIN_API_KEY and JSONBIN_BIN_ID)


def load_data(fresh: bool = False) -> dict:
    if not fresh and "data" in st.session_state:
        return st.session_state["data"]
    if not jb_ready():
        st.session_state.setdefault("data", empty_state())
        return st.session_state["data"]
    try:
        r = requests.get(JB_BASE + "/latest", headers=JB_HEADERS, timeout=15)
        r.raise_for_status()
        data = normalize(r.json().get("record") or {})
    except Exception as e:
        st.error(f"Errore di lettura dal database (JSONBin): {e}")
        data = st.session_state.get("data", empty_state())
    st.session_state["data"] = data
    return data


def save_data(data: dict) -> bool:
    if not jb_ready():
        st.session_state["data"] = data
        return True
    try:
        r = requests.put(JB_BASE, headers=JB_HEADERS, data=json.dumps(data), timeout=20)
        r.raise_for_status()
        st.session_state["data"] = data
        return True
    except Exception as e:
        st.error(f"Errore di scrittura sul database: {e}")
        return False


def mutate(fn) -> bool:
    """Ricarica lo stato piu' recente, applica fn(data), salva. Riduce i conflitti."""
    data = load_data(fresh=True)
    fn(data)
    return save_data(data)


def push_log(d: dict, **e):
    d["log"].insert(0, {"id": uid(), "ts": now_ms(), "season": d["config"]["season"],
                        "teamId": None, "teamId2": None, "player": "", "dC": 0, "dI": 0,
                        "type": "sistema", "note": "", **e})


# =============================================================================
# 5) DERIVATI DI COMODO
# =============================================================================

def team_by_id(d, tid):
    return next((t for t in d["teams"] if t["id"] == tid), None)


def roster_of(d, tid):
    return [p for p in d["players"] if p["teamId"] == tid and not p.get("u21")]


def u21_of(d, tid):
    return [p for p in d["players"] if p["teamId"] == tid and p.get("u21")]


def monte_of(d, tid):
    return sum(p["wage"] for p in roster_of(d, tid))


def plans_of(d, tid):
    return [pl for pl in d["plans"] if pl["teamId"] == tid and plan_active(pl)]


def rate_due_of(d, tid):
    return sum(pl["rates"][pl["paid"]] for pl in plans_of(d, tid))


def rate_residual_of(d, tid):
    return sum(plan_residual(pl) for pl in plans_of(d, tid))


def picks_of(d, tid):
    season = d["config"]["season"]
    eligible = [p for p in u21_of(d, tid) if u21_ok(p, season)]
    return max(0, d["config"]["u21Slots"] - len(eligible))


def roster_target(cfg):
    return sum(cfg["roster"].values())


def count_role(d, tid, role):
    return len([p for p in roster_of(d, tid) if p["role"] == role])


def can_rateize(d, p):
    cfg = d["config"]
    if p.get("u21"):
        return False, "gli U21 non hanno cartellino"
    if p.get("viaTrade"):
        return False, "i giocatori arrivati via scambio non sono rateizzabili"
    if not p.get("asta"):
        return False, "solo gli acquisti dell'asta estiva sono rateizzabili"
    if any(pl["playerId"] == p["id"] and plan_active(pl) for pl in d["plans"]):
        return False, "gia' rateizzato"
    if len(plans_of(d, p["teamId"])) >= cfg["maxPlans"]:
        return False, f"limite di {cfg['maxPlans']} piani attivi raggiunto"
    if now_ms() - int(p.get("createdAt", 0)) > cfg["rateDays"] * 86400000:
        return False, f"finestra di {cfg['rateDays']} giorni chiusa"
    return True, ""


# =============================================================================
# 6) OPERAZIONI (tutte passano da mutate)
# =============================================================================

def op_add_player(team_id, name, role, price, u21, birth_year, asta, rate_years):
    def fn(d):
        t = team_by_id(d, team_id)
        if not t:
            return
        V = 0 if u21 else int(price)
        p = {"id": uid(), "teamId": team_id, "name": name.strip(), "role": role,
             "base": V, "wage": 0 if u21 else V, "k": 0, "u21": bool(u21),
             "birthYear": int(birth_year) if u21 else None, "calafiori": False,
             "promoted": False, "viaTrade": False, "asta": bool(asta), "createdAt": now_ms()}
        d["players"].append(p)
        if u21:
            push_log(d, type="draft", teamId=team_id, player=p["name"], note=f"Draft U21 (nato {birth_year})")
            return
        t["ing"] -= V
        if asta and rate_years in (2, 3):
            d["plans"].append({"id": uid(), "teamId": team_id, "playerId": p["id"], "playerName": p["name"],
                               "total": V, "years": rate_years, "rates": make_rates(V, rate_years), "paid": 0})
            push_log(d, type="acquisto", teamId=team_id, player=p["name"], dI=-V,
                     note=f"Asta a {fmt(V)} · cartellino rateizzato in {rate_years} anni ({'/'.join(map(str, make_rates(V, rate_years)))})")
        else:
            t["cassa"] -= V
            push_log(d, type="acquisto", teamId=team_id, player=p["name"], dI=-V, dC=-V,
                     note=f"{'Asta' if asta else 'Riparazione (no rate)'} a {fmt(V)}")
    return mutate(fn)


def op_release(player_id, mode):
    """mode: A | estero | astori. U21: rimborso 0 (base=0)."""
    def fn(d):
        p = next((x for x in d["players"] if x["id"] == player_id), None)
        if not p:
            return
        t = team_by_id(d, p["teamId"])
        cfg = d["config"]
        pct = (cfg["refundEst"] if mode in ("estero", "astori") else cfg["refundA"]) / 100
        ref = int(p["base"] * pct)
        t["cassa"] += ref; t["ing"] += ref
        if mode == "astori":
            for pl in d["plans"]:
                if pl["playerId"] == p["id"]:
                    pl["paid"] = pl["years"]
        d["players"] = [x for x in d["players"] if x["id"] != player_id]
        lab = {"A": f"Svincolo Serie A ({cfg['refundA']}%)",
               "estero": f"Svincolo estero/ritiro ({cfg['refundEst']}%)",
               "astori": "Clausola Astori (100% + diritto acquisto extra)"}[mode]
        push_log(d, type="svincolo", teamId=t["id"], player=p["name"], dC=ref, dI=ref, note=lab)
    return mutate(fn)


def op_rateize(player_id, years):
    def fn(d):
        p = next((x for x in d["players"] if x["id"] == player_id), None)
        if not p:
            return
        ok, why = can_rateize(d, p)
        if not ok:
            push_log(d, type="sistema", teamId=p["teamId"], player=p["name"], note=f"Rateizzazione rifiutata: {why}")
            return
        t = team_by_id(d, p["teamId"])
        t["cassa"] += p["base"]  # il cartellino pagato torna in cassa, parte il piano
        d["plans"].append({"id": uid(), "teamId": p["teamId"], "playerId": p["id"], "playerName": p["name"],
                           "total": p["base"], "years": years, "rates": make_rates(p["base"], years), "paid": 0})
        push_log(d, type="rate", teamId=p["teamId"], player=p["name"], dC=p["base"],
                 note=f"Cartellino rateizzato in {years} anni ({'/'.join(map(str, make_rates(p['base'], years)))}) · scelta irrevocabile")
    return mutate(fn)


def op_extinguish(plan_id):
    def fn(d):
        pl = next((x for x in d["plans"] if x["id"] == plan_id), None)
        if not pl or not plan_active(pl):
            return
        t = team_by_id(d, pl["teamId"])
        res = plan_residual(pl)
        t["cassa"] -= res; pl["paid"] = pl["years"]
        push_log(d, type="rate", teamId=t["id"], player=pl["playerName"], dC=-res,
                 note=f"Estinzione anticipata del piano (residuo {fmt(res)})")
    return mutate(fn)


def op_toggle_calafiori(player_id):
    def fn(d):
        p = next((x for x in d["players"] if x["id"] == player_id), None)
        if not p or not p.get("u21"):
            return
        p["calafiori"] = not p.get("calafiori")
        push_log(d, type="draft", teamId=p["teamId"], player=p["name"],
                 note="Clausola Calafiori attivata (prestito estero ✱, 1 anno)" if p["calafiori"] else "Clausola Calafiori rimossa")
    return mutate(fn)


def op_promote(player_id, release_id, release_mode):
    def fn(d):
        p = next((x for x in d["players"] if x["id"] == player_id), None)
        if not p:
            return
        t = team_by_id(d, p["teamId"]); cfg = d["config"]
        if release_id:
            out = next((x for x in d["players"] if x["id"] == release_id), None)
            if out:
                pct = (cfg["refundEst"] if release_mode == "estero" else cfg["refundA"]) / 100
                ref = int(out["base"] * pct)
                t["cassa"] += ref; t["ing"] += ref
                d["players"] = [x for x in d["players"] if x["id"] != release_id]
                push_log(d, type="svincolo", teamId=t["id"], player=out["name"], dC=ref, dI=ref,
                         note=f"Svincolato per far posto a {p['name']}")
        p["u21"] = False; p["promoted"] = True; p["base"] = 1; p["wage"] = 1; p["k"] = 0; p["calafiori"] = False
        t["ing"] -= 1
        push_log(d, type="promozione", teamId=t["id"], player=p["name"], dI=-1,
                 note="Promosso in prima squadra · ingaggio 1 (+1 a ogni conferma)")
    return mutate(fn)


def op_move(team_id, conto, amount, note, mtype):
    def fn(d):
        t = team_by_id(d, team_id)
        if not t:
            return
        if conto == "cassa":
            t["cassa"] += amount
        else:
            t["ing"] += amount
        push_log(d, type=mtype, teamId=team_id, dC=amount if conto == "cassa" else 0,
                 dI=amount if conto != "cassa" else 0, note=note)
    return mutate(fn)


# ---- scambi: proposta -> accettazione -> ratifica direzione ------------------

def trade_effects(d, tr):
    """Ritorna (aI,aC,bI,bC, sforo, slot_warn) post-operazione per il trade tr."""
    A = team_by_id(d, tr["fromTeam"]); B = team_by_id(d, tr["toTeam"])
    give = [p for p in d["players"] if p["id"] in tr["give"]]
    get = [p for p in d["players"] if p["id"] in tr["get"]]
    cong = int(tr.get("congu", 0)); dirAB = tr.get("congDir", "AB") == "AB"
    aI = A["ing"] + sum(0 if p["u21"] else p["wage"] for p in give) - sum(0 if p["u21"] else p["base"] for p in get)
    bI = B["ing"] + sum(0 if p["u21"] else p["wage"] for p in get) - sum(0 if p["u21"] else p["base"] for p in give)
    aC = A["cassa"] + (-cong if dirAB else cong)
    bC = B["cassa"] + (cong if dirAB else -cong)
    # slot U21: netto giovani in ingresso per squadra
    slots = d["config"]["u21Slots"]
    b_u21 = len(u21_of(d, B["id"])) + sum(1 for p in give if p["u21"]) - sum(1 for p in get if p["u21"])
    a_u21 = len(u21_of(d, A["id"])) + sum(1 for p in get if p["u21"]) - sum(1 for p in give if p["u21"])
    slot_warn = a_u21 > slots or b_u21 > slots
    return aI, aC, bI, bC, (aI < 0 or bI < 0 or aC < 0 or bC < 0), slot_warn


def op_trade_propose(from_team, to_team, give_ids, get_ids, congu, cong_dir, by_label):
    def fn(d):
        d["trades"].insert(0, {"id": uid(), "ts": now_ms(), "season": d["config"]["season"],
                               "fromTeam": from_team, "toTeam": to_team,
                               "give": give_ids, "get": get_ids,
                               "congu": int(congu or 0), "congDir": cong_dir,
                               "status": "proposta", "by": by_label})
        push_log(d, type="scambio", teamId=from_team, teamId2=to_team,
                 note="Nuova proposta di scambio (in attesa dell'altra squadra)")
    return mutate(fn)


def op_trade_status(trade_id, status):
    def fn(d):
        tr = next((x for x in d["trades"] if x["id"] == trade_id), None)
        if not tr:
            return
        tr["status"] = status
        push_log(d, type="scambio", teamId=tr["fromTeam"], teamId2=tr["toTeam"],
                 note=f"Proposta {status}")
    return mutate(fn)


def op_trade_ratify(trade_id):
    def fn(d):
        tr = next((x for x in d["trades"] if x["id"] == trade_id), None)
        if not tr or tr["status"] != "accettata":
            return
        aI, aC, bI, bC, sforo, slot_warn = trade_effects(d, tr)
        if sforo or slot_warn:
            tr["status"] = "bloccata (sforo)"
            return
        A = team_by_id(d, tr["fromTeam"]); B = team_by_id(d, tr["toTeam"])
        A["ing"], A["cassa"], B["ing"], B["cassa"] = aI, aC, bI, bC
        names = []
        for pid, dest in [(i, B["id"]) for i in tr["give"]] + [(i, A["id"]) for i in tr["get"]]:
            p = next((x for x in d["players"] if x["id"] == pid), None)
            if not p:
                continue
            p["teamId"] = dest
            if not p["u21"]:
                p["wage"] = p["base"]; p["k"] = 0; p["viaTrade"] = True
                p["asta"] = False; p["promoted"] = False
            names.append(p["name"])
        tr["status"] = "ratificata"
        push_log(d, type="scambio", teamId=A["id"], teamId2=B["id"], player=" ⇄ ".join(names),
                 note=(f"Conguaglio {fmt(tr['congu'])} {'→' if tr['congDir'] == 'AB' else '←'} · " if tr.get("congu") else "")
                      + "ratificato: arrivo con ingaggio = V")
    return mutate(fn)


def op_trade_annul_fine(trade_id):
    def fn(d):
        tr = next((x for x in d["trades"] if x["id"] == trade_id), None)
        if not tr:
            return
        tr["status"] = "annullata (sforo, multa 10)"
        for tid in (tr["fromTeam"], tr["toTeam"]):
            t = team_by_id(d, tid)
            if t:
                t["cassa"] -= 10
                push_log(d, type="multa", teamId=tid, dC=-10,
                         note="Scambio ufficializzato con sforo: annullato + multa 10")
    return mutate(fn)


def op_sale(seller_id, buyer_id, player_id, price):
    def fn(d):
        A = team_by_id(d, seller_id); B = team_by_id(d, buyer_id)
        p = next((x for x in d["players"] if x["id"] == player_id), None)
        if not (A and B and p) or p["u21"]:
            return
        aI, bI = A["ing"] + p["wage"], B["ing"] - p["base"]
        aC, bC = A["cassa"] + price, B["cassa"] - price
        if aI < 0 or bI < 0 or aC < 0 or bC < 0:
            for tid in (seller_id, buyer_id):
                t = team_by_id(d, tid); t["cassa"] -= 10
                push_log(d, type="multa", teamId=tid, dC=-10, note="Compravendita con sforo: annullata + multa 10")
            return
        A["ing"], A["cassa"], B["ing"], B["cassa"] = aI, aC, bI, bC
        p["teamId"] = B["id"]; p["wage"] = p["base"]; p["k"] = 0
        p["viaTrade"] = True; p["asta"] = False; p["promoted"] = False
        push_log(d, type="compravendita", teamId=B["id"], teamId2=A["id"], player=p["name"],
                 dC=-price, dI=-p["base"], note=f"Acquisto da {A['name']} per {fmt(price)} di cassa")
    return mutate(fn)


# ---- chiusura stagione --------------------------------------------------------

def op_extra(team_id, pos, to_ing):
    def fn(d):
        cfg = d["config"]; t = team_by_id(d, team_id)
        if not t or team_id in d["extraDone"]:
            return
        amount = cfg["extraTable"][pos - 1] if 0 < pos <= len(cfg["extraTable"]) else 0
        ti = max(0, min(int(to_ing), amount)); tc = amount - ti
        t["ing"] += ti; t["cassa"] += tc
        d["extraDone"][team_id] = {"pos": pos, "toIng": ti, "toCassa": tc, "ts": now_ms()}
        push_log(d, type="extra", teamId=team_id, dC=tc, dI=ti,
                 note=f"Crediti extra {pos}ª posizione: {fmt(amount)} (→ {fmt(ti)} ingaggi · {fmt(tc)} cassa) · definitivo")
    return mutate(fn)


def op_closure(team_id, choices):
    """choices: {player_id: 'conf'|'svA'|'svE'}"""
    def fn(d):
        cfg = d["config"]; t = team_by_id(d, team_id)
        if not t or team_id in d["closed"]:
            return
        conf_cost = ref_c = ref_i = 0
        conf_names, out_names, to_remove = [], [], []
        for p in [x for x in d["players"] if x["teamId"] == team_id]:
            ch = choices.get(p["id"], "svA")
            if ch == "conf":
                if not p["u21"]:
                    w = next_wage(p); conf_cost += w; p["wage"] = w; p["k"] += 1
                conf_names.append(p["name"])
            else:
                pct = (cfg["refundA"] if ch == "svA" else cfg["refundEst"]) / 100
                r = int(p["base"] * pct); ref_c += r; ref_i += r
                to_remove.append(p["id"]); out_names.append(p["name"])
        d["players"] = [x for x in d["players"] if x["id"] not in to_remove]
        rate_due = 0
        for pl in [x for x in d["plans"] if x["teamId"] == team_id and plan_active(x)]:
            rate_due += pl["rates"][pl["paid"]]; pl["paid"] += 1
        t["ing"] = t["ing"] - conf_cost + ref_i
        t["cassa"] = t["cassa"] + ref_c - rate_due
        d["closed"].append(team_id)
        push_log(d, type="chiusura", teamId=team_id, dC=ref_c - rate_due, dI=ref_i - conf_cost,
                 note=f"Chiusura {cfg['season']}: {len(conf_names)} conferme ({', '.join(conf_names) or 'nessuna'}) · "
                      f"{len(out_names)} svincoli · rate pagate {fmt(rate_due)}")
    return mutate(fn)


def op_advance():
    def fn(d):
        nxt = next_season(d["config"]["season"])
        d["config"]["season"] = nxt
        d["closed"] = []; d["extraDone"] = {}
        d["config"]["phase"].update({"closure_open": False, "svincoli_open": False, "extra_deadline": ""})
        push_log(d, type="sistema", note=f"Nuova stagione {nxt} · U21 eleggibili: nati dal {min_birth(nxt)} in poi")
    return mutate(fn)


# =============================================================================
# 7) EXPORT EXCEL / JSON
# =============================================================================

def build_excel(d: dict) -> bytes:
    cfg = d["config"]
    rows_t = [{"Squadra": t["name"], "Presidente": t.get("manager", ""),
               "Cassa": t["cassa"], "Saldo ingaggi": t["ing"], "Monte rosa": monte_of(d, t["id"]),
               "Rate residue": rate_residual_of(d, t["id"]), "Rata in scadenza": rate_due_of(d, t["id"]),
               "Rosa": f"{len(roster_of(d, t['id']))}/{roster_target(cfg)}",
               "U21": f"{len(u21_of(d, t['id']))}/{cfg['u21Slots']}", "Pick draft": picks_of(d, t["id"])}
              for t in d["teams"]]
    rows_p = []
    for p in d["players"]:
        t = team_by_id(d, p["teamId"])
        pl = next((x for x in d["plans"] if x["playerId"] == p["id"] and plan_active(x)), None)
        rows_p.append({"Squadra": t["name"] if t else "?", "Giocatore": p["name"], "Ruolo": p["role"],
                       "Valore d'asta (V)": "" if p["u21"] else p["base"],
                       "Ingaggio corrente": "" if p["u21"] else p["wage"],
                       "Prossimo ingaggio": "" if p["u21"] or p["role"] == "P" else next_wage(p),
                       "Conferme": "" if p["u21"] else p["k"],
                       "U21": "Si" if p["u21"] else "", "Anno nascita": p.get("birthYear") or "",
                       "Calafiori": "✱" if p.get("calafiori") else "",
                       "Via scambio": "Si" if p.get("viaTrade") else "",
                       "Promosso": "Si" if p.get("promoted") else "",
                       "Rate": f"{pl['paid']}/{pl['years']} (residuo {plan_residual(pl)})" if pl else ""})
    rows_r = [{"Squadra": (team_by_id(d, pl["teamId"]) or {}).get("name", "?"), "Giocatore": pl["playerName"],
               "Totale": pl["total"], "Anni": pl["years"], "Piano": " / ".join(map(str, pl["rates"])),
               "Rate pagate": pl["paid"], "Residuo": plan_residual(pl),
               "Prossima rata": pl["rates"][pl["paid"]] if plan_active(pl) else "",
               "Stato": "Attivo" if plan_active(pl) else "Estinto"} for pl in d["plans"]]
    rows_l = [{"Data": dt.datetime.fromtimestamp(l["ts"] / 1000, TZ).strftime("%d/%m/%Y %H:%M"),
               "Stagione": l.get("season", ""), "Tipo": l.get("type", ""),
               "Squadra": (team_by_id(d, l.get("teamId")) or {}).get("name", ""),
               "Squadra 2": (team_by_id(d, l.get("teamId2")) or {}).get("name", ""),
               "Giocatore": l.get("player", ""), "Δ Cassa": l.get("dC", 0), "Δ Ingaggi": l.get("dI", 0),
               "Note": l.get("note", "")} for l in reversed(d["log"])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        pd.DataFrame(rows_t).to_excel(w, sheet_name="Squadre", index=False)
        pd.DataFrame(rows_p).to_excel(w, sheet_name="Giocatori", index=False)
        pd.DataFrame(rows_r).to_excel(w, sheet_name="Piani rate", index=False)
        pd.DataFrame(rows_l).to_excel(w, sheet_name="Movimenti", index=False)
    return buf.getvalue()


def op_team_selfedit(team_id, name, manager, color, new_pin):
    """Personalizzazione self-service del presidente: nome (unico), presidente, colore, PIN."""
    result = {"ok": False, "msg": ""}

    def fn(d):
        t = team_by_id(d, team_id)
        if not t:
            result["msg"] = "Squadra non trovata."
            return
        nm = (name or "").strip()
        if not nm:
            result["msg"] = "Il nome squadra non puo' essere vuoto."
            return
        if any(x["id"] != team_id and x["name"].strip().lower() == nm.lower() for x in d["teams"]):
            result["msg"] = "Esiste gia' una squadra con questo nome."
            return
        changes = []
        if nm != t["name"]:
            changes.append(f"nome «{t['name']}» → «{nm}»")
        t["name"] = nm
        t["manager"] = (manager or "").strip()
        t["color"] = color or t.get("color", "#1C7A45")
        np = (new_pin or "").strip()
        if np:
            if not (4 <= len(np) <= 8):
                result["msg"] = "Il PIN deve avere tra 4 e 8 caratteri."
                return
            t["pin"] = np
            changes.append("PIN aggiornato")
        if changes:
            push_log(d, type="sistema", teamId=team_id, note="Personalizzazione squadra: " + ", ".join(changes))
        result["ok"] = True
    mutate(fn)
    return result


# =============================================================================
# 7-bis) IMPORT ASTA INIZIALE (file "per fantaleghe" di Leghe Fantacalcio)
# =============================================================================

MANTRA_TO_CLASSIC = {"por": "P", "ds": "D", "dd": "D", "dc": "D", "b": "D",
                     "e": "C", "m": "C", "c": "C", "t": "C", "w": "C", "a": "A", "pc": "A"}


def parse_fantaleghe_csv(text: str):
    """File 'per fantaleghe': sezioni separate da $,$,$ e righe Fantasquadra,IdGiocatore,Prezzo.
    Ritorna (ordine_squadre, {squadra: [(id, prezzo), ...]}) preservando l'ordine del file."""
    teams, order = {}, []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        t, pid = parts[0], parts[1]
        try:
            pr = int(float(parts[2]))
        except Exception:
            continue
        if t not in teams:
            teams[t] = []
            order.append(t)
        teams[t].append((pid, pr))
    return order, teams


def load_quotazioni(data: bytes, filename: str):
    """Listone Quotazioni fantacalcio.it (xlsx o csv) -> {id: (nome, ruolo P/D/C/A)}.
    Trova da solo la riga di intestazione (colonne 'Id' e 'Nome'); ruolo da 'R' o, in
    mancanza, dal primo ruolo Mantra in 'RM'."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xls")):
        df0 = pd.read_excel(io.BytesIO(data), header=None)
    else:
        txt = data.decode("utf-8", errors="replace")
        df0 = pd.read_csv(io.StringIO(txt), header=None, sep=None, engine="python")
    hdr = None
    for i in range(min(8, len(df0))):
        vals = [str(x).strip().lower() for x in df0.iloc[i].tolist()]
        if "id" in vals and "nome" in vals:
            hdr = i
            break
    if hdr is None:
        raise ValueError("intestazione non trovata: servono le colonne 'Id' e 'Nome' (listone Quotazioni)")
    cols = [str(x).strip() for x in df0.iloc[hdr].tolist()]
    df = df0.iloc[hdr + 1:].copy()
    df.columns = cols
    cl = {c.lower(): c for c in df.columns}
    c_id, c_nome = cl["id"], cl["nome"]
    c_r, c_rm = cl.get("r"), cl.get("rm")
    out = {}
    for _, row in df.iterrows():
        try:
            pid = str(int(float(row[c_id])))
        except Exception:
            continue
        nome = str(row[c_nome]).strip()
        ruolo = None
        if c_r is not None:
            rv = str(row[c_r]).strip().upper()
            if rv in ROLE_ORDER:
                ruolo = rv
        if ruolo is None and c_rm is not None:
            first = str(row[c_rm]).strip().split(";")[0].strip().lower()
            ruolo = MANTRA_TO_CLASSIC.get(first)
        if nome and nome.lower() != "nan":
            out[pid] = (nome, ruolo)
    return out


def positional_roles(n: int, roster: dict):
    """Ruoli dedotti dall'ordine del file (3P-8D-8C-6A): per rose incomplete tronca in coda."""
    seq = []
    for r in ROLE_ORDER:
        seq += [r] * roster[r]
    return (seq + ["A"] * n)[:n]


def op_import_asta(assign: dict, rows_by_team: dict, quot: dict, reset: bool):
    """assign: {nome_csv: team_id | '__new__'} · rows_by_team: {nome_csv: [(pid, prezzo)]}
    quot: {pid: (nome, ruolo)} eventualmente vuoto. reset: azzera rose e riporta i conti a 500/500."""
    report = {"teams": 0, "players": 0, "missing": 0, "byteam": []}

    def fn(d):
        cfg = d["config"]
        for csv_name, rows in rows_by_team.items():
            tid = assign.get(csv_name)
            if tid == "__new__":
                t = {"id": uid(), "name": csv_name, "manager": "", "color": "#1C7A45", "pin": "0000",
                     "cassa": cfg["startCassa"], "ing": cfg["startIng"]}
                d["teams"].append(t)
            else:
                t = team_by_id(d, tid)
                if not t:
                    continue
            if reset:
                d["players"] = [p for p in d["players"] if p["teamId"] != t["id"]]
                d["plans"] = [pl for pl in d["plans"] if pl["teamId"] != t["id"]]
                t["ing"] = cfg["startIng"]
                t["cassa"] = cfg["startCassa"]
            pos = positional_roles(len(rows), cfg["roster"])
            spent = 0
            for i, (pid, price) in enumerate(rows):
                nome, ruolo = quot.get(pid, (None, None))
                if nome is None:
                    report["missing"] += 1
                p = {"id": uid(), "teamId": t["id"], "name": nome or f"Id {pid}",
                     "role": ruolo or pos[i], "base": price, "wage": price, "k": 0,
                     "u21": False, "birthYear": None, "calafiori": False, "promoted": False,
                     "viaTrade": False, "asta": True, "createdAt": now_ms(), "fcId": pid}
                d["players"].append(p)
                t["ing"] -= price
                t["cassa"] -= price
                spent += price
                report["players"] += 1
            report["teams"] += 1
            report["byteam"].append((t["name"], len(rows), spent, t["ing"], t["cassa"]))
            push_log(d, type="acquisto", teamId=t["id"], dI=-spent, dC=-spent,
                     note=f"Asta iniziale importata da file fantaleghe: {len(rows)} giocatori, spesa {fmt(spent)}"
                          + (" (rosa azzerata prima dell'import)" if reset else ""))
    mutate(fn)
    return report


# =============================================================================
# 8) UI DI BASE
# =============================================================================

st.set_page_config(page_title="Front Office · Fanta Manageriale", page_icon="⚽", layout="wide")

st.markdown(f"""
<style>
  .stApp {{ background:#EDF1EC; }}
  h1,h2,h3 {{ color:{DEEP}; }}
  .fo-badge {{ display:inline-block; padding:2px 9px; border-radius:14px; font-size:12px;
              font-weight:600; margin-right:6px; }}
  .stButton>button[kind="primary"] {{ background:{GREEN}; border:none; }}
  div[data-testid="stMetricValue"] {{ color:{DEEP}; }}
  .block-container {{ padding-top:2.2rem; }}
</style>
""", unsafe_allow_html=True)


def badge(txt, color=GREEN):
    return f"<span class='fo-badge' style='background:{color}18;color:{color};border:1px solid {color}33'>{txt}</span>"


def player_line(p, cfg):
    tags = []
    if p["role"] == "P":
        tags.append(badge("no conferma", BRICK))
    if p.get("viaTrade"):
        tags.append(badge("via scambio", "#2C6E8E"))
    if p.get("promoted"):
        tags.append(badge("ex U21", GREEN))
    if p.get("calafiori"):
        tags.append(badge("✱ Calafiori", BRASS))
    return f"{ROLE_EMOJI[p['role']]} **{p['name']}** " + " ".join(tags)


def is_admin():
    return st.session_state.get("auth", {}).get("role") == "admin"


def my_team_id():
    return st.session_state.get("auth", {}).get("teamId")


def can_edit(team_id):
    return is_admin() or my_team_id() == team_id


# =============================================================================
# 9) LOGIN
# =============================================================================

def page_login():
    d = load_data()
    st.markdown(f"<h1 style='margin-bottom:0'>⚽ Front Office</h1><p style='color:#5C6D62;margin-top:4px'>"
                f"<b>{d['config']['league']}</b> · stagione {d['config']['season']} · fantacalcio manageriale pluriennale</p>",
                unsafe_allow_html=True)
    if not jb_ready():
        st.warning("Database non configurato: imposta JSONBIN_API_KEY, JSONBIN_BIN_ID e ADMIN_PIN nei "
                   "Settings dello Space (Variables and secrets). Fino ad allora i dati NON vengono salvati.")
    opts = ["— Direzione (admin) —"] + [f"{t['name']} · {t.get('manager', '')}" for t in d["teams"]]
    with st.form("login"):
        who = st.selectbox("Chi sei?", opts)
        pin = st.text_input("PIN", type="password", max_chars=8)
        ok = st.form_submit_button("Entra", type="primary", use_container_width=True)
    if ok:
        if who == opts[0]:
            if pin == ADMIN_PIN:
                st.session_state["auth"] = {"role": "admin", "teamId": None}
                st.rerun()
            else:
                st.error("PIN Direzione errato.")
        else:
            t = d["teams"][opts.index(who) - 1]
            if pin == str(t.get("pin", "")):
                st.session_state["auth"] = {"role": "president", "teamId": t["id"]}
                st.rerun()
            else:
                st.error("PIN errato.")
    if not d["teams"]:
        st.info("Nessuna squadra ancora creata: entra come **Direzione** con l'ADMIN_PIN e inizializza la lega.")


# =============================================================================
# 10) PAGINE
# =============================================================================

def sidebar(d):
    cfg = d["config"]
    with st.sidebar:
        st.markdown(f"### ⚽ {cfg['league']}")
        st.caption(f"Stagione **{cfg['season']}** · U21: nati dal {min_birth(cfg['season'])}")
        auth = st.session_state["auth"]
        if auth["role"] == "admin":
            st.markdown(badge("DIREZIONE", BRICK), unsafe_allow_html=True)
        else:
            t = team_by_id(d, auth["teamId"])
            if t:
                st.markdown(f"**{t['name']}** · {t.get('manager', '')}")
                st.metric("Saldo ingaggi", fmt(t["ing"]))
                st.metric("Cassa", fmt(t["cassa"]))
        st.divider()
        pages = ["🏟️ Cruscotto", "👥 Squadre", "🔁 Mercato & Scambi", "🏁 Chiusura stagione"]
        if is_admin():
            pages.append("🛠️ Direzione")
        page = st.radio("Vai a", pages, label_visibility="collapsed")
        st.divider()
        st.download_button("📊 Scarica Excel (backup)", data=build_excel(d),
                           file_name=f"FrontOffice_{cfg['league'].replace(' ', '_')}_{cfg['season'].replace('/', '-')}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
        c1, c2 = st.columns(2)
        if c1.button("🔄 Aggiorna", use_container_width=True):
            load_data(fresh=True); st.rerun()
        if c2.button("🚪 Esci", use_container_width=True):
            del st.session_state["auth"]; st.rerun()
    return page


def page_cruscotto(d):
    cfg = d["config"]
    st.subheader("Cruscotto della lega")
    ph = cfg["phase"]
    stato = []
    stato.append(badge("mercato APERTO" if ph["mercato_open"] else "mercato chiuso", GREEN if ph["mercato_open"] else "#8A9990"))
    stato.append(badge("finestra svincoli APERTA" if ph["svincoli_open"] else "finestra svincoli chiusa", GREEN if ph["svincoli_open"] else "#8A9990"))
    stato.append(badge("chiusura stagione APERTA" if ph["closure_open"] else "chiusura non aperta", BRASS if ph["closure_open"] else "#8A9990"))
    if ph.get("extra_deadline"):
        stato.append(badge(f"deadline extra: {ph['extra_deadline']}", BRICK))
    st.markdown(" ".join(stato), unsafe_allow_html=True)

    neg = [t for t in d["teams"] if t["ing"] < 0 or t["cassa"] < 0]
    if neg:
        st.error("⚠ **Sforo di budget** (multa 10 e rientro immediato da regolamento): " +
                 ", ".join(t["name"] for t in neg))

    rows = [{"Squadra": t["name"], "Presidente": t.get("manager", ""), "Ingaggi": t["ing"], "Cassa": t["cassa"],
             "Monte rosa": monte_of(d, t["id"]), "Rate residue": rate_residual_of(d, t["id"]),
             "Rosa": f"{len(roster_of(d, t['id']))}/{roster_target(cfg)}",
             "U21": f"{len(u21_of(d, t['id']))}/{cfg['u21Slots']}", "Pick": picks_of(d, t["id"]),
             "Chiusa": "✅" if t["id"] in d["closed"] else ""} for t in d["teams"]]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if d["teams"]:
        ch = pd.DataFrame([{"Squadra": t["name"], "Conto": "Ingaggi", "Crediti": t["ing"]} for t in d["teams"]] +
                          [{"Squadra": t["name"], "Conto": "Cassa", "Crediti": t["cassa"]} for t in d["teams"]])
        chart = alt.Chart(ch).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
            x=alt.X("Squadra:N", sort=None, title=None),
            xOffset="Conto:N",
            y=alt.Y("Crediti:Q", title="crediti"),
            color=alt.Color("Conto:N", scale=alt.Scale(domain=["Ingaggi", "Cassa"], range=[GREEN, BRASS]), legend=alt.Legend(title=None)),
            tooltip=["Squadra", "Conto", "Crediti"]).properties(height=260)
        st.altair_chart(chart, use_container_width=True)
    st.caption("I due conti non si travasano mai (unica eccezione: allocazione dei crediti extra a fine stagione).")


def render_team_block(d, t, editable):
    cfg = d["config"]
    st.markdown(f"#### {t['name']} <span style='color:#8A9990;font-size:14px'>· {t.get('manager', '')}</span>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Saldo ingaggi", fmt(t["ing"]))
    c2.metric("Cassa", fmt(t["cassa"]))
    c3.metric("Monte rosa", fmt(monte_of(d, t["id"])))
    c4.metric("Pick draft", picks_of(d, t["id"]))
    counts = " ".join(f"{ROLE_EMOJI[r]} {count_role(d, t['id'], r)}/{cfg['roster'][r]}" for r in ROLE_ORDER)
    st.caption(f"Rosa: {counts} · U21 {len(u21_of(d, t['id']))}/{cfg['u21Slots']}")

    # ---- prima squadra ----
    ros = sorted(roster_of(d, t["id"]), key=lambda p: (ROLE_ORDER.index(p["role"]), -p["wage"]))
    if not ros:
        st.info("Rosa vuota.")
    for p in ros:
        cols = st.columns([4, 1.4, 1.4, 1.6, 2.6])
        cols[0].markdown(player_line(p, cfg), unsafe_allow_html=True)
        cols[1].markdown(f"V **{fmt(p['base'])}**")
        cols[2].markdown(f"ing **{fmt(p['wage'])}**")
        cols[3].markdown("—" if p["role"] == "P" else f"→ conf. **{fmt(next_wage(p))}** (k{p['k']})")
        with cols[4]:
            if editable:
                pl = next((x for x in d["plans"] if x["playerId"] == p["id"] and plan_active(x)), None)
                ok_rate, why = can_rateize(d, p)
                b1, b2 = st.columns(2)
                if ok_rate:
                    yrs = b1.selectbox("Rate", ["—", "2 anni", "3 anni"], key=f"ry{p['id']}", label_visibility="collapsed")
                    if yrs != "—" and b1.button("Rateizza", key=f"rb{p['id']}"):
                        op_rateize(p["id"], 2 if yrs.startswith("2") else 3); st.rerun()
                elif pl:
                    b1.caption(f"rate {pl['paid']}/{pl['years']}")
                sv_ok = is_admin() or cfg["phase"]["svincoli_open"]
                if sv_ok:
                    mode = b2.selectbox("Svincolo", ["—", "Serie A", "Estero"] + (["Astori"] if is_admin() else []),
                                        key=f"sv{p['id']}", label_visibility="collapsed")
                    if mode != "—" and b2.button("Svincola", key=f"sb{p['id']}"):
                        op_release(p["id"], {"Serie A": "A", "Estero": "estero", "Astori": "astori"}[mode]); st.rerun()
                else:
                    b2.caption("finestra svincoli chiusa (fuori data: multa 30 via Direzione)")

    # ---- U21 ----
    st.markdown("**Settore U21** · eleggibili: nati dal " + str(min_birth(cfg["season"])))
    yn = u21_of(d, t["id"])
    if not yn:
        st.caption(f"Slot liberi: {picks_of(d, t['id'])} pick al prossimo draft (ordine inverso di classifica).")
    for p in yn:
        ok_age = u21_ok(p, cfg["season"])
        cols = st.columns([4, 2, 3])
        cols[0].markdown(player_line(p, cfg) + f" · nato **{p['birthYear']}**", unsafe_allow_html=True)
        cols[1].markdown(badge("OK eta'", GREEN) if ok_age else badge("FUORI ETA': promuovi o svincola", BRICK), unsafe_allow_html=True)
        with cols[2]:
            if editable:
                b1, b2, b3 = st.columns(3)
                if b1.button("✱", key=f"ca{p['id']}", help="Clausola Calafiori (prestito estero, 1 anno)"):
                    op_toggle_calafiori(p["id"]); st.rerun()
                if (is_admin() or cfg["phase"]["svincoli_open"]) and b2.button("Promuovi", key=f"pr{p['id']}", help="In prima squadra a ingaggio 1"):
                    st.session_state["promote_id"] = p["id"]; st.rerun()
                if b3.button("Svincola", key=f"su{p['id']}", help="Nessun rimborso (era a costo zero)"):
                    op_release(p["id"], "A"); st.rerun()

    # ---- promozione guidata ----
    pid = st.session_state.get("promote_id")
    if editable and pid:
        p = next((x for x in d["players"] if x["id"] == pid and x["teamId"] == t["id"]), None)
        if p:
            with st.container(border=True):
                st.markdown(f"**Promuovi {p['name']}** → prima squadra a **ingaggio 1** (+1 a ogni conferma).")
                free = count_role(d, t["id"], p["role"]) < cfg["roster"][p["role"]]
                rel_id, rel_mode = None, "A"
                if not free:
                    same = [x for x in roster_of(d, t["id"]) if x["role"] == p["role"]]
                    sel = st.selectbox("Chi esce per fargli posto?", [f"{x['name']} (V {fmt(x['base'])})" for x in same])
                    rel_id = same[[f"{x['name']} (V {fmt(x['base'])})" for x in same].index(sel)]["id"]
                    rel_mode = "A" if st.radio("Motivo svincolo di chi esce", [f"Serie A ({cfg['refundA']}%)", f"Estero ({cfg['refundEst']}%)"], horizontal=True).startswith("Serie") else "estero"
                cA, cB = st.columns(2)
                if cA.button("Conferma promozione", type="primary"):
                    op_promote(pid, rel_id, rel_mode); st.session_state.pop("promote_id", None); st.rerun()
                if cB.button("Annulla"):
                    st.session_state.pop("promote_id", None); st.rerun()

    # ---- piani rate ----
    pls = plans_of(d, t["id"])
    st.markdown(f"**Piani rate** · {len(pls)}/{cfg['maxPlans']} attivi (restano anche se il giocatore viene scambiato)")
    for pl in pls:
        cols = st.columns([4, 3, 2])
        cols[0].markdown(f"**{pl['playerName']}** · {' / '.join(map(str, pl['rates']))}")
        cols[1].markdown(f"pagate {pl['paid']}/{pl['years']} · prossima **{fmt(pl['rates'][pl['paid']])}** · residuo **{fmt(plan_residual(pl))}**")
        if editable and cols[2].button(f"Estingui ({fmt(plan_residual(pl))})", key=f"ex{pl['id']}"):
            op_extinguish(pl["id"]); st.rerun()
    if not pls:
        st.caption(f"Nessun piano attivo. Si rateizza entro {cfg['rateDays']} giorni dall'acquisto d'asta (2 o 3 anni, irrevocabile).")


def page_squadre(d):
    st.subheader("Squadre")
    if is_admin():
        tabs = st.tabs([t["name"] for t in d["teams"]] or ["—"])
        for tab, t in zip(tabs, d["teams"]):
            with tab:
                render_team_block(d, t, editable=True)
    else:
        mine = team_by_id(d, my_team_id())
        with st.expander("⚙️ Personalizza la tua squadra (nome, colore, PIN)", expanded=False):
            c1, c2 = st.columns(2)
            nm = c1.text_input("Nome squadra", mine["name"], max_chars=30)
            mg = c2.text_input("Presidente", mine.get("manager", ""), max_chars=30)
            c3, c4 = st.columns(2)
            col = c3.color_picker("Colore", mine.get("color", "#1C7A45"))
            np = c4.text_input("Nuovo PIN (lascia vuoto per non cambiarlo)", type="password", max_chars=8)
            if st.button("💾 Salva personalizzazione", type="primary"):
                res = op_team_selfedit(mine["id"], nm, mg, col, np)
                if res["ok"]:
                    st.success("Squadra aggiornata." + (" Ricorda il nuovo PIN per il prossimo accesso!" if np.strip() else ""))
                    st.rerun()
                else:
                    st.error(res["msg"])
        mine = team_by_id(d, my_team_id())
        others = [t for t in d["teams"] if t["id"] != my_team_id()]
        render_team_block(d, mine, editable=True)
        st.divider()
        st.markdown("### Le altre squadre (sola lettura)")
        for t in others:
            with st.expander(f"{t['name']} · ing {fmt(t['ing'])} · cassa {fmt(t['cassa'])}"):
                render_team_block(d, t, editable=False)


def trade_card(d, tr, ctx):
    A = team_by_id(d, tr["fromTeam"]); B = team_by_id(d, tr["toTeam"])
    give = [p for p in d["players"] if p["id"] in tr["give"]]
    get = [p for p in d["players"] if p["id"] in tr["get"]]
    aI, aC, bI, bC, sforo, slot_warn = trade_effects(d, tr)
    with st.container(border=True):
        st.markdown(f"**{A['name']} → {B['name']}** · {badge(tr['status'], BRASS if 'propost' in tr['status'] else GREEN if tr['status'] in ('accettata', 'ratificata') else BRICK)}",
                    unsafe_allow_html=True)
        st.markdown(f"{A['name']} cede: " + (", ".join(f"**{p['name']}**" + (" (U21)" if p["u21"] else f" (V {fmt(p['base'])} · ing {fmt(p['wage'])})") for p in give) or "—"))
        st.markdown(f"{B['name']} cede: " + (", ".join(f"**{p['name']}**" + (" (U21)" if p["u21"] else f" (V {fmt(p['base'])} · ing {fmt(p['wage'])})") for p in get) or "—"))
        if tr.get("congu"):
            st.markdown(f"Conguaglio cassa: **{fmt(tr['congu'])}** {'da ' + A['name'] + ' a ' + B['name'] if tr['congDir'] == 'AB' else 'da ' + B['name'] + ' a ' + A['name']}")
        st.caption(f"Saldi post-operazione → {A['name']}: ing {fmt(aI)} · cassa {fmt(aC)} | {B['name']}: ing {fmt(bI)} · cassa {fmt(bC)}")
        if sforo:
            st.error("SFORO DI BUDGET: se ufficializzato va annullato con multa 10 a entrambe (regolamento).")
        if slot_warn:
            st.error("Slot U21 pieni nella squadra di destinazione.")
        if tr["status"] == "proposta" and ctx == "inbox":
            c1, c2 = st.columns(2)
            if c1.button("✅ Accetta", key=f"ac{tr['id']}"):
                op_trade_status(tr["id"], "accettata"); st.rerun()
            if c2.button("❌ Rifiuta", key=f"rf{tr['id']}"):
                op_trade_status(tr["id"], "rifiutata"); st.rerun()
        if tr["status"] == "proposta" and ctx == "mine":
            if st.button("↩️ Ritira proposta", key=f"wd{tr['id']}"):
                op_trade_status(tr["id"], "ritirata"); st.rerun()
        if is_admin() and tr["status"] == "accettata":
            c1, c2 = st.columns(2)
            if not (sforo or slot_warn) and c1.button("🏛️ Ratifica ed esegui", key=f"ok{tr['id']}", type="primary"):
                op_trade_ratify(tr["id"]); st.rerun()
            if (sforo or slot_warn) and c1.button("⚠️ Annulla + multa 10 a entrambe", key=f"an{tr['id']}"):
                op_trade_annul_fine(tr["id"]); st.rerun()
            if c2.button("Annulla senza multa", key=f"a0{tr['id']}"):
                op_trade_status(tr["id"], "annullata"); st.rerun()


def page_mercato(d):
    cfg = d["config"]
    st.subheader("Mercato & Scambi")
    if not cfg["phase"]["mercato_open"] and not is_admin():
        st.warning("Il mercato e' chiuso: le proposte di scambio sono sospese (riapre nelle finestre previste).")

    # ---- proposta scambio ----
    if (cfg["phase"]["mercato_open"] or is_admin()) and d["teams"]:
        with st.expander("➕ Proponi uno scambio", expanded=False):
            if is_admin():
                fromT = st.selectbox("Squadra proponente", [t["name"] for t in d["teams"]])
                from_id = d["teams"][[t["name"] for t in d["teams"]].index(fromT)]["id"]
            else:
                from_id = my_team_id()
            others = [t for t in d["teams"] if t["id"] != from_id]
            toT = st.selectbox("Con quale squadra?", [t["name"] for t in others]) if others else None
            to_id = others[[t["name"] for t in others].index(toT)]["id"] if others else None
            mineP = [p for p in d["players"] if p["teamId"] == from_id]
            theirP = [p for p in d["players"] if p["teamId"] == to_id] if to_id else []
            lab = lambda p: f"{p['name']}" + (" (U21)" if p["u21"] else f" (V {fmt(p['base'])} · ing {fmt(p['wage'])})")
            give = st.multiselect("Cedo", [lab(p) for p in mineP])
            get = st.multiselect("Ricevo", [lab(p) for p in theirP])
            c1, c2 = st.columns(2)
            congu = c1.number_input("Conguaglio (crediti di cassa)", min_value=0, step=1, value=0)
            cong_dir = c2.radio("Chi paga il conguaglio", ["Io (proponente)", "L'altra squadra"], horizontal=True)
            if st.button("📨 Invia proposta", type="primary", disabled=not (to_id and (give or get))):
                give_ids = [p["id"] for p in mineP if lab(p) in give]
                get_ids = [p["id"] for p in theirP if lab(p) in get]
                op_trade_propose(from_id, to_id, give_ids, get_ids, congu,
                                 "AB" if cong_dir.startswith("Io") else "BA",
                                 "direzione" if is_admin() else "presidente")
                st.success("Proposta inviata: l'altra squadra la trova qui sotto."); st.rerun()

    # ---- liste proposte ----
    trades = [tr for tr in d["trades"] if tr["season"] == cfg["season"]]
    if is_admin():
        pend = [tr for tr in trades if tr["status"] in ("proposta", "accettata")]
        st.markdown(f"### In attesa ({len(pend)})")
        for tr in pend:
            trade_card(d, tr, "admin")
    else:
        inbox = [tr for tr in trades if tr["toTeam"] == my_team_id() and tr["status"] == "proposta"]
        mine = [tr for tr in trades if tr["fromTeam"] == my_team_id() and tr["status"] in ("proposta", "accettata")]
        st.markdown(f"### 📬 Proposte ricevute ({len(inbox)})")
        for tr in inbox:
            trade_card(d, tr, "inbox")
        st.markdown(f"### 📤 Le mie proposte ({len(mine)})")
        for tr in mine:
            trade_card(d, tr, "mine")

    st.markdown("### Storico proposte")
    done = [tr for tr in trades if tr["status"] not in ("proposta", "accettata")][:15]
    for tr in done:
        A = team_by_id(d, tr["fromTeam"]); B = team_by_id(d, tr["toTeam"])
        st.caption(f"{A['name']} ⇄ {B['name']} · {tr['status']}")

    # ---- registro movimenti ----
    st.markdown("### Registro movimenti")
    rows = [{"Quando": dt.datetime.fromtimestamp(l["ts"] / 1000, TZ).strftime("%d/%m %H:%M"),
             "Tipo": l["type"], "Squadra": (team_by_id(d, l.get("teamId")) or {}).get("name", ""),
             "Giocatore": l.get("player", ""), "Δ ing": l.get("dI", 0), "Δ cassa": l.get("dC", 0),
             "Note": l.get("note", "")} for l in d["log"][:80]]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=320)


def closure_form(d, t):
    """Form conferme/svincoli per una squadra (presidente o admin)."""
    cfg = d["config"]
    st.markdown(f"#### Chiusura {t['name']} · {cfg['season']}")
    st.caption(f"Max **{cfg['maxConfirms']} conferme** (U21 inclusi) · portieri mai confermabili · "
               f"i non confermati sono svincolati con rimborso su entrambi i conti · le rate in scadenza si pagano ora.")
    ps = [p for p in d["players"] if p["teamId"] == t["id"]]
    ps = sorted(ps, key=lambda p: (ROLE_ORDER.index(p["role"]), -(0 if p["u21"] else p["wage"])))
    choices = {}
    for p in ps:
        fuori = p["u21"] and not u21_ok(p, cfg["season"])
        cols = st.columns([4, 3])
        info = (f"U21 · nato {p['birthYear']}" + (" · FUORI ETA'" if fuori else "")) if p["u21"] \
            else f"V {fmt(p['base'])} · ing {fmt(p['wage'])} → conferma a {fmt(next_wage(p))}"
        cols[0].markdown(player_line(p, cfg) + f"<br><span style='color:#8A9990;font-size:12px'>{info}</span>", unsafe_allow_html=True)
        opts = []
        if p["role"] != "P" and not fuori:
            opts.append("Conferma" + ("" if p["u21"] else f" ({fmt(next_wage(p))})"))
        opts += [f"Svincola Serie A ({cfg['refundA']}%)", f"Svincola Estero ({cfg['refundEst']}%)"]
        default = 0 if (p["u21"] and not fuori) else len(opts) - 2 if p["role"] != "P" and not fuori else 0
        sel = cols[1].selectbox(" ", opts, index=min(default, len(opts) - 1), key=f"cl{t['id']}{p['id']}",
                                label_visibility="collapsed")
        choices[p["id"]] = "conf" if sel.startswith("Conferma") else ("svA" if "Serie A" in sel else "svE")
        if fuori:
            cols[1].caption("Fuori eta': per tenerlo, promuovilo (ing. 1) da 'Squadre' prima di chiudere.")
    n_conf = sum(1 for v in choices.values() if v == "conf")
    conf_cost = sum(next_wage(p) for p in ps if choices[p["id"]] == "conf" and not p["u21"])
    ref = sum(int(p["base"] * ((cfg["refundA"] if choices[p["id"]] == "svA" else cfg["refundEst"]) / 100))
              for p in ps if choices[p["id"]] in ("svA", "svE"))
    rate_due = rate_due_of(d, t["id"])
    new_ing = t["ing"] - conf_cost + ref
    new_cassa = t["cassa"] + ref - rate_due
    ok_conf = n_conf <= cfg["maxConfirms"]
    st.markdown(f"Conferme: **{n_conf}/{cfg['maxConfirms']}** (costo {fmt(conf_cost)}) · rimborsi **+{fmt(ref)}** su entrambi i conti · "
                f"rate **−{fmt(rate_due)}** → saldi finali: ingaggi **{fmt(new_ing)}** · cassa **{fmt(new_cassa)}**")
    if not ok_conf:
        st.error(f"Troppe conferme: massimo {cfg['maxConfirms']}.")
    if new_ing < 0 or new_cassa < 0:
        st.error("Sforo nei saldi finali: rivedi le conferme.")
    sure = st.checkbox("Confermo: la chiusura e' definitiva e non modificabile", key=f"ok{t['id']}")
    if st.button("🏁 Applica chiusura", type="primary", disabled=not (ok_conf and sure), key=f"go{t['id']}"):
        op_closure(t["id"], choices)
        st.success("Chiusura applicata."); st.rerun()


def page_chiusura(d):
    cfg = d["config"]; ph = cfg["phase"]
    st.subheader("Chiusura stagione")
    st.markdown("Ordine: **1)** crediti extra (allocazione libera, una volta sola) → **2)** conferme/svincoli + rate → "
                "**3)** la Direzione avvia la nuova stagione → draft U21 → asta.")
    if not ph["closure_open"] and not is_admin():
        st.info("La fase di chiusura non e' ancora aperta dalla Direzione.")
        return

    # ---- 1) crediti extra ----
    st.markdown("### 1 · Crediti extra di classifica")
    st.caption("Tabella 1ª→8ª: " + " · ".join(map(str, cfg["extraTable"])) +
               (f" · deadline: {ph['extra_deadline']}" if ph.get("extra_deadline") else ""))
    deadline_ok = True
    if ph.get("extra_deadline"):
        try:
            deadline_ok = today() <= dt.date.fromisoformat(ph["extra_deadline"])
        except Exception:
            deadline_ok = True
    targets = d["teams"] if is_admin() else [team_by_id(d, my_team_id())]
    for t in targets:
        done = d["extraDone"].get(t["id"])
        with st.container(border=True):
            if done:
                st.markdown(f"**{t['name']}** · {badge('assegnati: definitivo', GREEN)} "
                            f"{done.get('pos', '?')}ª → {fmt(done.get('toIng', 0))} ingaggi · {fmt(done.get('toCassa', 0))} cassa",
                            unsafe_allow_html=True)
            elif not deadline_ok and not is_admin():
                st.markdown(f"**{t['name']}** · {badge('deadline superata: contatta la Direzione', BRICK)}", unsafe_allow_html=True)
            else:
                st.markdown(f"**{t['name']}** · scegli posizione e allocazione (scelta **definitiva**)")
                c1, c2, c3 = st.columns([2, 2, 2])
                pos = c1.selectbox("Posizione finale", list(range(1, len(cfg["extraTable"]) + 1)),
                                   format_func=lambda i: f"{i}ª → {cfg['extraTable'][i - 1]}", key=f"pos{t['id']}")
                amount = cfg["extraTable"][pos - 1]
                to_ing = c2.number_input("→ a ingaggi", 0, amount, amount, key=f"ti{t['id']}")
                c3.metric("→ a cassa", fmt(amount - to_ing))
                if st.button(f"Assegna {fmt(amount)} a {t['name']}", key=f"ex{t['id']}", type="primary"):
                    op_extra(t["id"], pos, to_ing); st.rerun()

    # ---- 2) conferme ----
    st.markdown("### 2 · Conferme, svincoli e rate")
    if is_admin():
        for t in d["teams"]:
            with st.expander(f"{t['name']} " + ("✅ chiusa" if t["id"] in d["closed"] else "· da chiudere"),
                             expanded=False):
                if t["id"] in d["closed"]:
                    st.success("Squadra gia' chiusa per questa stagione.")
                else:
                    closure_form(d, t)
    else:
        t = team_by_id(d, my_team_id())
        if t["id"] in d["closed"]:
            st.success("La tua squadra e' chiusa per questa stagione: rose e saldi aggiornati. "
                       "Si riparte con draft U21 e asta.")
        else:
            closure_form(d, t)

    # ---- 3) avanza ----
    if is_admin():
        st.markdown("### 3 · Nuova stagione")
        left = [t["name"] for t in d["teams"] if t["id"] not in d["closed"]]
        if left:
            st.caption("Mancano: " + ", ".join(left))
        if st.button(f"▶️ Avvia stagione {next_season(cfg['season'])}", type="primary",
                     disabled=bool(left)):
            op_advance(); st.rerun()


def page_direzione(d):
    cfg = d["config"]
    st.subheader("Direzione")
    t_fasi, t_asta, t_sq, t_mercato, t_multe, t_dati = st.tabs(["⏱️ Fasi & deadline", "🎪 Asta iniziale", "👥 Squadre & PIN", "🛒 Operazioni dirette", "⚖️ Multe & movimenti", "💾 Dati"])

    # ---- fasi ----
    with t_fasi:
        ph = cfg["phase"]
        c1, c2, c3 = st.columns(3)
        mo = c1.toggle("Mercato aperto (proposte di scambio)", value=ph["mercato_open"])
        so = c2.toggle("Finestra svincoli aperta", value=ph["svincoli_open"])
        co = c3.toggle("Chiusura stagione aperta", value=ph["closure_open"])
        dl = st.text_input("Deadline allocazione extra (YYYY-MM-DD, vuoto = nessuna)", value=ph.get("extra_deadline", ""))
        if st.button("Salva fasi", type="primary"):
            def fn(dd):
                dd["config"]["phase"].update({"mercato_open": mo, "svincoli_open": so, "closure_open": co,
                                              "extra_deadline": dl.strip()})
                push_log(dd, type="sistema", note=f"Fasi aggiornate: mercato={'on' if mo else 'off'}, "
                                                  f"svincoli={'on' if so else 'off'}, chiusura={'on' if co else 'off'}"
                                                  + (f", deadline extra {dl}" if dl.strip() else ""))
            mutate(fn); st.success("Fasi salvate."); st.rerun()
        st.caption("Consiglio: finestra svincoli = prima settimana di agosto; fuori finestra lo svincolo lo fa la "
                   "Direzione applicando la multa 30 (preset in Multe).")

    # ---- asta iniziale (import da Leghe Fantacalcio) ----
    with t_asta:
        st.markdown("**Importa l'esito dell'asta** dal file *per fantaleghe* di Leghe Fantacalcio "
                    "(righe `Fantasquadra, Id giocatore, Prezzo`). Per avere **nomi e ruoli** carica anche "
                    "il listone **Quotazioni** di fantacalcio.it (xlsx o csv, colonne `Id`, `Nome`, `R`); "
                    "senza listone i ruoli vengono dedotti dall'ordine 3P-8D-8C-6A e i nomi restano `Id NNNN`.")
        up_rose = st.file_uploader("1 · File per fantaleghe (obbligatorio)", type=["csv", "txt"], key="asta_csv")
        up_quot = st.file_uploader("2 · Listone Quotazioni (consigliato)", type=["xlsx", "xls", "csv"], key="asta_quot")
        if up_rose is not None:
            try:
                order, rows_by_team = parse_fantaleghe_csv(up_rose.getvalue().decode("utf-8", errors="replace"))
            except Exception as e:
                st.error(f"File rose non leggibile: {e}")
                order, rows_by_team = [], {}
            quot = {}
            if up_quot is not None:
                try:
                    quot = load_quotazioni(up_quot.getvalue(), up_quot.name)
                    st.success(f"Listone caricato: {fmt(len(quot))} giocatori con nome e ruolo.")
                except Exception as e:
                    st.error(f"Listone non leggibile ({e}): procedo con ruoli posizionali e nomi `Id NNNN`.")
            if order:
                st.markdown("**Anteprima e abbinamento squadre**")
                prev = [{"Fantasquadra (file)": nm, "Giocatori": len(rows_by_team[nm]),
                         "Spesa": sum(p for _, p in rows_by_team[nm]),
                         "Con nome dal listone": sum(1 for pid, _ in rows_by_team[nm] if pid in quot)}
                        for nm in order]
                st.dataframe(pd.DataFrame(prev), hide_index=True, use_container_width=True)
                exp = roster_target(cfg)
                if any(len(rows_by_team[nm]) != exp for nm in order):
                    st.warning(f"Alcune rose non hanno {exp} giocatori: l'import procede comunque, "
                               "completa poi da 'Operazioni dirette'.")
                assign = {}
                app_names = ["➕ Crea nuova squadra"] + [t["name"] for t in d["teams"]]
                for nm in order:
                    match = next((t["name"] for t in d["teams"] if t["name"].strip().lower() == nm.strip().lower()), None)
                    idx = app_names.index(match) if match else 0
                    sel = st.selectbox(f"«{nm}» →", app_names, index=idx, key=f"map_{nm}")
                    assign[nm] = "__new__" if sel == app_names[0] else next(t["id"] for t in d["teams"] if t["name"] == sel)
                chosen = [v for v in assign.values() if v != "__new__"]
                if len(chosen) != len(set(chosen)):
                    st.error("Due fantasquadre puntano alla stessa squadra dell'app: correggi gli abbinamenti.")
                reset = st.checkbox(f"Azzera le rose delle squadre coinvolte e riparti da {fmt(cfg['startIng'])} ingaggi + "
                                    f"{fmt(cfg['startCassa'])} cassa (consigliato per la prima importazione)", value=True)
                sure = st.checkbox("Confermo: importa l'asta (operazione registrata nel log)")
                if st.button("🎪 Importa asta iniziale", type="primary",
                             disabled=not sure or len(chosen) != len(set(chosen))):
                    rep = op_import_asta(assign, rows_by_team, quot, reset)
                    st.success(f"Importate {rep['teams']} squadre e {rep['players']} giocatori."
                               + (f" {rep['missing']} id non trovati nel listone (nomi `Id NNNN`, ruolo posizionale)."
                                  if rep["missing"] else ""))
                    for nm, n, sp, ing, cas in rep["byteam"]:
                        icona = "⚠️" if (ing < 0 or cas < 0) else "✅"
                        st.markdown(f"{icona} **{nm}** · {n} giocatori · spesa {fmt(sp)} → ingaggi {fmt(ing)} · cassa {fmt(cas)}")
                    st.info("Da adesso decorrono i 7 giorni per le rateizzazioni: ogni presidente le sceglie "
                            "dalla propria pagina Squadre. Le squadre nuove hanno PIN 0000: cambialo in 'Squadre & PIN'.")

    # ---- squadre & pin ----
    with t_sq:
        with st.form("newteam"):
            st.markdown("**Nuova squadra**")
            c1, c2, c3, c4 = st.columns(4)
            nm = c1.text_input("Nome")
            mg = c2.text_input("Presidente")
            pin = c3.text_input("PIN", max_chars=8)
            col = c4.color_picker("Colore", "#1C7A45")
            if st.form_submit_button("Crea", type="primary") and nm.strip():
                def fn(dd):
                    dd["teams"].append({"id": uid(), "name": nm.strip(), "manager": mg.strip(),
                                        "color": col, "pin": pin or "0000",
                                        "cassa": dd["config"]["startCassa"], "ing": dd["config"]["startIng"]})
                    push_log(dd, type="sistema", note=f"Creata squadra {nm.strip()}")
                mutate(fn); st.rerun()
        for t in d["teams"]:
            with st.expander(f"{t['name']} · {t.get('manager', '')}"):
                c1, c2, c3, c4, c5 = st.columns(5)
                nm = c1.text_input("Nome", t["name"], key=f"n{t['id']}")
                mg = c2.text_input("Presidente", t.get("manager", ""), key=f"m{t['id']}")
                pin = c3.text_input("PIN", str(t.get("pin", "")), key=f"p{t['id']}")
                ing = c4.number_input("Saldo ingaggi", value=int(t["ing"]), step=1, key=f"i{t['id']}")
                cas = c5.number_input("Cassa", value=int(t["cassa"]), step=1, key=f"c{t['id']}")
                b1, b2 = st.columns(2)
                if b1.button("Salva", key=f"sv{t['id']}", type="primary"):
                    def fn(dd, tid=t["id"]):
                        x = team_by_id(dd, tid)
                        x.update({"name": nm, "manager": mg, "pin": pin, "ing": int(ing), "cassa": int(cas)})
                    mutate(fn); st.rerun()
                if b2.button("Elimina squadra", key=f"del{t['id']}"):
                    def fn(dd, tid=t["id"]):
                        dd["teams"] = [x for x in dd["teams"] if x["id"] != tid]
                        dd["players"] = [x for x in dd["players"] if x["teamId"] != tid]
                        dd["plans"] = [x for x in dd["plans"] if x["teamId"] != tid]
                    mutate(fn); st.rerun()

    # ---- operazioni dirette ----
    with t_mercato:
        st.markdown("**Acquisto (asta / riparazione)**")
        with st.form("buy"):
            c1, c2, c3, c4 = st.columns(4)
            tn = c1.selectbox("Squadra", [t["name"] for t in d["teams"]] or ["—"])
            nm = c2.text_input("Giocatore")
            rl = c3.selectbox("Ruolo", ROLE_ORDER, format_func=lambda r: ROLE_NAME[r])
            pr = c4.number_input("Prezzo (V)", 0, 999, 1)
            c5, c6 = st.columns(2)
            sess = c5.radio("Sessione", ["Asta estiva", "Riparazione (no rate)"], horizontal=True)
            rate = c6.radio("Rateizza cartellino", ["No", "2 anni", "3 anni"], horizontal=True)
            if st.form_submit_button("Registra acquisto", type="primary") and d["teams"] and nm.strip():
                tid = d["teams"][[t["name"] for t in d["teams"]].index(tn)]["id"]
                asta = sess.startswith("Asta")
                ry = 0 if rate == "No" or not asta else int(rate[0])
                op_add_player(tid, nm, rl, pr, False, None, asta, ry); st.rerun()
        st.markdown("**Draft U21**")
        with st.form("draft"):
            c1, c2, c3, c4 = st.columns(4)
            tn = c1.selectbox("Squadra ", [t["name"] for t in d["teams"]] or ["—"])
            nm = c2.text_input("Giovane")
            rl = c3.selectbox("Ruolo ", ROLE_ORDER, format_func=lambda r: ROLE_NAME[r])
            by = c4.number_input("Anno di nascita", 1990, 2015, min_birth(cfg["season"]))
            if st.form_submit_button("Drafta", type="primary") and d["teams"] and nm.strip():
                tid = d["teams"][[t["name"] for t in d["teams"]].index(tn)]["id"]
                if by < min_birth(cfg["season"]):
                    st.error(f"Nato prima del {min_birth(cfg['season'])}: non eleggibile U21 per {cfg['season']}.")
                elif len(u21_of(d, tid)) >= cfg["u21Slots"]:
                    st.error("Slot U21 pieni.")
                else:
                    op_add_player(tid, nm, rl, 0, True, by, False, 0); st.rerun()
        st.markdown("**Acquisto tra club (crediti di cassa)**")
        with st.form("sale"):
            c1, c2, c3, c4 = st.columns(4)
            sn = c1.selectbox("Venditore", [t["name"] for t in d["teams"]] or ["—"])
            sid = d["teams"][[t["name"] for t in d["teams"]].index(sn)]["id"] if d["teams"] else None
            plist = [p for p in d["players"] if p["teamId"] == sid and not p["u21"]] if sid else []
            pn = c2.selectbox("Giocatore", [f"{p['name']} (V {fmt(p['base'])})" for p in plist] or ["—"])
            bn = c3.selectbox("Compratore", [t["name"] for t in d["teams"] if t["id"] != sid] or ["—"])
            pz = c4.number_input("Prezzo cassa", 0, 999, 0)
            if st.form_submit_button("Esegui (controllo sforo automatico)", type="primary") and plist:
                pid = plist[[f"{p['name']} (V {fmt(p['base'])})" for p in plist].index(pn)]["id"]
                bid = [t for t in d["teams"] if t["name"] == bn][0]["id"]
                op_sale(sid, bid, pid, int(pz)); st.rerun()

    # ---- multe ----
    with t_multe:
        st.markdown("**Multe rapide (dalla cassa)** · le multe a ridosso della chiusura si detraggono di fatto dai crediti extra")
        presets = [("Svincolo fuori finestra", 30), ("Sforo di budget", 10), ("Formazione non inviata", 5),
                   ("Slot scoperto", 2), ("Condotta antisportiva", 30), ("Ritardo quota", 10)]
        cols = st.columns(3)
        tn = cols[0].selectbox("Squadra", [t["name"] for t in d["teams"]] or ["—"], key="finet")
        pr = cols[1].selectbox("Preset", [f"{l} (−{v})" for l, v in presets])
        if cols[2].button("Applica multa", type="primary") and d["teams"]:
            tid = d["teams"][[t["name"] for t in d["teams"]].index(tn)]["id"]
            l, v = presets[[f"{l} (−{v})" for l, v in presets].index(pr)]
            op_move(tid, "cassa", -v, l, "multa"); st.rerun()
        st.markdown("**Movimento manuale (correzioni admin)**")
        with st.form("mov"):
            c1, c2, c3, c4 = st.columns(4)
            tn = c1.selectbox("Squadra ", [t["name"] for t in d["teams"]] or ["—"])
            conto = c2.radio("Conto", ["cassa", "ingaggi"], horizontal=True)
            amt = c3.number_input("Importo (usa il segno)", -999, 999, 0)
            note = c4.text_input("Causale")
            if st.form_submit_button("Registra") and d["teams"] and amt != 0:
                tid = d["teams"][[t["name"] for t in d["teams"]].index(tn)]["id"]
                op_move(tid, conto, int(amt), note or "Movimento manuale", "movimento"); st.rerun()
        st.caption("Nota: i travasi tra conti sono vietati dal regolamento (eccetto allocazione extra) — "
                   "usa i movimenti solo per multe e correzioni, motivandole.")

    # ---- dati ----
    with t_dati:
        c1, c2, c3 = st.columns(3)
        c1.download_button("⬇️ Esporta JSON", data=json.dumps(d, indent=2),
                           file_name="frontoffice_backup.json", mime="application/json", use_container_width=True)
        up = c2.file_uploader("Importa JSON (anche dall'app Claude)", type=["json"])
        if up is not None and c2.button("Importa e sostituisci", type="primary"):
            try:
                nd = normalize(json.loads(up.getvalue().decode("utf-8")))
                save_data(nd); st.success("Importato."); st.rerun()
            except Exception as e:
                st.error(f"File non valido: {e}")
        if c3.button("🧪 Carica lega di esempio", use_container_width=True):
            save_data(example_state()); st.rerun()
        st.divider()
        st.markdown("**Zona pericolosa**")
        sure = st.checkbox("Confermo di voler azzerare TUTTO (scarica prima Excel/JSON)")
        if st.button("🗑️ Azzera lega", disabled=not sure):
            save_data(empty_state()); st.rerun()
        st.divider()
        st.markdown("**Parametri** (i valori delle regole fisse si cambiano qui, con voto di lega)")
        with st.form("cfg"):
            c1, c2, c3 = st.columns(3)
            lg = c1.text_input("Nome lega", cfg["league"])
            ss = c2.text_input("Stagione", cfg["season"])
            ex = c3.text_input("Extra 1ª→8ª", ",".join(map(str, cfg["extraTable"])))
            if st.form_submit_button("Salva parametri", type="primary"):
                def fn(dd):
                    dd["config"]["league"] = lg
                    dd["config"]["season"] = ss
                    try:
                        dd["config"]["extraTable"] = [int(x) for x in ex.split(",")]
                    except Exception:
                        pass
                mutate(fn); st.rerun()


# =============================================================================
# 11) MAIN
# =============================================================================

def main():
    if "auth" not in st.session_state:
        page_login()
        return
    d = load_data()
    page = sidebar(d)
    if page.startswith("🏟️"):
        page_cruscotto(d)
    elif page.startswith("👥"):
        page_squadre(d)
    elif page.startswith("🔁"):
        page_mercato(d)
    elif page.startswith("🏁"):
        page_chiusura(d)
    elif page.startswith("🛠️"):
        page_direzione(d)


main()
