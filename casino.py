"""
APUSM Casino — Blackjack, Póker (Texas Hold'em) y Ruleta
Módulo separado que se registra en app.py mediante register_casino(app, socketio)
"""
import random, json
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import join_room, leave_room, emit as sio_emit

# ── CONSTANTES CASINO ─────────────────────────────────────────────────────────
# Ventaja de la casa: ligera pero consistente
BJ_HOUSE_EDGE   = 0.52   # Blackjack: dealer gana en empate de bust y usa regla S17
ROULETTE_ZEROS  = 2      # Ruleta: doble 0 (americana) → ventaja ~5.26%
POKER_RAKE_PCT  = 0.05   # Póker: 5% del bote va a la casa
POKER_RAKE_CAP  = 500    # Máximo rake por mano de póker
CASINO_MIN_BET  = 100
CASINO_MAX_BET  = 50000

casino = Blueprint("casino", __name__)

# ── HELPERS ───────────────────────────────────────────────────────────────────
def now_s(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def round50(x): return int(x // 50) * 50

# ── BARAJA ────────────────────────────────────────────────────────────────────
SUITS  = ["♠","♥","♦","♣"]
RANKS  = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
VALUES = {"A":11,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":10,"Q":10,"K":10}

def new_deck(n=6):
    """Zapato de n mazos barajados."""
    d = [{"rank":r,"suit":s} for r in RANKS for s in SUITS] * n
    random.shuffle(d)
    return d

def hand_value(hand):
    """Calcula valor de mano de blackjack."""
    v, aces = 0, 0
    for c in hand:
        v += VALUES[c["rank"]]
        if c["rank"] == "A": aces += 1
    while v > 21 and aces:
        v -= 10; aces -= 1
    return v

def card_str(c): return f"{c['rank']}{c['suit']}"

# ── MANOS DE PÓKER ────────────────────────────────────────────────────────────
HAND_RANKS = ["High Card","One Pair","Two Pair","Three of a Kind",
              "Straight","Flush","Full House","Four of a Kind","Straight Flush","Royal Flush"]

def rank_val(r):
    order = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":11,"Q":12,"K":13,"A":14}
    return order[r]

def eval_poker_hand(cards):
    """Evalúa la mejor mano de 5 de entre las cards dadas (5-7 cartas)."""
    from itertools import combinations
    best = None
    for combo in combinations(cards, 5):
        score = _score5(list(combo))
        if best is None or score > best:
            best = score
    return best

def _score5(hand):
    ranks  = sorted([rank_val(c["rank"]) for c in hand], reverse=True)
    suits  = [c["suit"] for c in hand]
    flush  = len(set(suits)) == 1
    straight = (ranks[0]-ranks[4]==4 and len(set(ranks))==5) or ranks==[14,5,4,3,2]
    counts = {}
    for r in ranks: counts[r] = counts.get(r,0)+1
    cv = sorted(counts.values(), reverse=True)
    ck = sorted(counts.keys(), key=lambda x: (counts[x], x), reverse=True)
    if flush and straight:
        cat = 9 if ranks[0]==14 and ranks[1]==13 else 8
    elif cv==[4,1]: cat=7
    elif cv==[3,2]: cat=6
    elif flush:     cat=5
    elif straight:  cat=4
    elif cv[0]==3:  cat=3
    elif cv==[2,2,1]: cat=2
    elif cv[0]==2:  cat=1
    else:           cat=0
    return (cat, ck)

# ── ESTADO EN MEMORIA (rooms) ─────────────────────────────────────────────────
# Cada sala es un dict con toda la info del juego en curso
_rooms: dict = {}

def get_room(rid): return _rooms.get(rid)
def set_room(rid, data): _rooms[rid] = data

def new_rid(game_type):
    import uuid
    rid = f"{game_type}_{uuid.uuid4().hex[:8]}"
    return rid

def open_rooms(game_type):
    return [r for r,d in _rooms.items()
            if d["game"]==game_type and d["status"]=="waiting"
            and len(d["players"]) < d["max_players"]]

# ═══════════════════════════════════════════════════════════════════════════════
# BLACKJACK
# ═══════════════════════════════════════════════════════════════════════════════

def bj_new_room():
    rid = new_rid("blackjack")
    set_room(rid, {
        "game": "blackjack", "status": "waiting",
        "max_players": 6, "players": {},
        "deck": new_deck(6), "dealer_hand": [],
        "round": 0, "turn_order": [], "current_turn": None
    })
    return rid

def bj_deal_room(rid):
    r = get_room(rid)
    if len(r["deck"]) < 30: r["deck"] = new_deck(6)
    r["dealer_hand"] = [r["deck"].pop(), r["deck"].pop()]
    r["round"] += 1
    r["status"] = "playing"
    r["turn_order"] = list(r["players"].keys())
    r["current_turn"] = r["turn_order"][0] if r["turn_order"] else None
    for uid, p in r["players"].items():
        p["hand"]     = [r["deck"].pop(), r["deck"].pop()]
        p["hand2"]    = []      # para split
        p["status"]   = "playing"   # playing/stand/bust/done
        p["doubled"]  = False
        p["split"]    = False
        p["bet2"]     = 0
    set_room(rid, r)

def bj_resolve_room(conn, rid, fetchone_fn, execute_fn, house_log_fn):
    """Resuelve todos los jugadores vs dealer. Retorna dict uid→result."""
    r      = get_room(rid)
    dealer = r["dealer_hand"]
    # Dealer juega: debe llegar a 17+ (S17)
    while hand_value(dealer) < 17:
        dealer.append(r["deck"].pop())
    dv     = hand_value(dealer)
    dealer_bj = len(dealer)==2 and dv==21

    results = {}
    for uid, p in r["players"].items():
        for hand_key in (["hand"] + (["hand2"] if p["split"] else [])):
            hand = p[hand_key]
            bet  = p["bet"] if hand_key=="hand" else p["bet2"]
            pv   = hand_value(hand)
            player_bj = len(hand)==2 and pv==21

            if pv > 21:
                result, payout = "bust", 0
            elif player_bj and not dealer_bj:
                bruto  = round(bet * 2.5, 2)   # Blackjack paga 3:2
                payout = round50(bruto)
                result = "blackjack"
            elif dealer_bj and not player_bj:
                result, payout = "dealer_bj", 0
            elif pv > dv or dv > 21:
                bruto  = bet * 2
                payout = round50(bruto)
                result = "win"
            elif pv == dv:
                payout = bet   # push: devuelve apuesta exacta (sin redondeo)
                result = "push"
            else:
                result, payout = "lose", 0

            redondeo = 0
            if result in ("win","blackjack"):
                redondeo = round(bet*2 - payout, 2) if result=="win" else round(bet*2.5 - payout, 2)

            # Actualizar saldo
            user = fetchone_fn(conn, "SELECT balance FROM users WHERE id=?", (uid,))
            if user:
                execute_fn(conn, "UPDATE users SET balance=balance+? WHERE id=?", (payout, uid))

            # Registrar en casino_log
            execute_fn(conn, """INSERT INTO casino_log
                (user_id,game,room_id,bet,payout,result,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (uid,"blackjack",rid,bet,payout,result,now_s()))

            if redondeo > 0:
                house_log_fn(conn, "blackjack", rid, redondeo, "redondeo",
                    f"Redondeo BJ uid={uid}")

            results[f"{uid}_{hand_key}"] = {
                "result": result, "payout": payout,
                "hand_value": pv, "dealer_value": dv
            }

    # Ganancia casa = suma de apuestas - suma de pagos
    total_bet = sum(p["bet"] + p.get("bet2",0) for p in r["players"].values())
    total_paid= sum(v["payout"] for v in results.values())
    house_gain = round(total_bet - total_paid, 2)
    if house_gain > 0:
        house_log_fn(conn, "blackjack", rid, house_gain, "profit",
            f"Ganancia casa BJ sala {rid}")
    elif house_gain < 0:
        house_log_fn(conn, "blackjack", rid, house_gain, "expense",
            f"Casa pagó extra BJ sala {rid}")

    r["status"]      = "finished"
    r["results"]     = results
    r["dealer_hand"] = dealer
    set_room(rid, r)
    return results

# ═══════════════════════════════════════════════════════════════════════════════
# RULETA AMERICANA (0 y 00 → ventaja 5.26%)
# ═══════════════════════════════════════════════════════════════════════════════

ROULETTE_REDS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

def roulette_spin():
    """Gira la ruleta. 0-36 + 37=00"""
    return random.randint(0, 37)

def roulette_number_label(n):
    return "00" if n==37 else str(n)

def roulette_resolve_bet(bet_type, bet_value, winning_number):
    """Retorna multiplicador bruto o 0 si pierde."""
    n = winning_number
    # 0 y 00 solo ganan en straight-up
    if n in (0, 37):
        return 36 if bet_type=="straight" and str(bet_value) in ("0","00","37") else 0
    label = str(n)
    is_red   = n in ROULETTE_REDS
    is_even  = n % 2 == 0
    dozen    = (n-1)//12 + 1
    column   = (n-1)%3 + 1
    low      = 1 <= n <= 18
    row      = (n-1)//3 + 1    # 1-12

    payouts = {
        "straight":  (35, str(n)==str(bet_value)),   # 35:1
        "split":     (17, str(n) in str(bet_value).split(",")),  # 17:1
        "street":    (11, row==int(bet_value)),       # 11:1
        "corner":    (8,  str(n) in str(bet_value).split(",")),  # 8:1
        "line":      (5,  row in [int(x) for x in str(bet_value).split(",")]),  # 5:1
        "dozen":     (2,  dozen==int(bet_value)),     # 2:1
        "column":    (2,  column==int(bet_value)),    # 2:1
        "red":       (1,  is_red),                    # 1:1
        "black":     (1,  not is_red),                # 1:1
        "even":      (1,  is_even),                   # 1:1
        "odd":       (1,  not is_even),               # 1:1
        "low":       (1,  low),                       # 1:1
        "high":      (1,  not low),                   # 1:1
    }
    if bet_type not in payouts: return 0
    mult, win = payouts[bet_type]
    return mult if win else 0

# ═══════════════════════════════════════════════════════════════════════════════
# TEXAS HOLD'EM POKER
# ═══════════════════════════════════════════════════════════════════════════════

def poker_new_room():
    rid = new_rid("poker")
    set_room(rid, {
        "game": "poker", "status": "waiting",
        "max_players": 8, "players": {},
        "deck": [], "community": [],
        "pot": 0, "side_pots": [],
        "round": 0, "phase": "waiting",   # waiting/preflop/flop/turn/river/showdown
        "dealer_pos": 0, "turn_order": [], "current_turn": None,
        "small_blind": 100, "big_blind": 200,
        "min_bet": 200, "last_raise": 200,
    })
    return rid

def poker_start_hand(rid):
    r = get_room(rid)
    if len(r["players"]) < 2: return False
    r["deck"]      = new_deck(1)
    r["community"] = []
    r["pot"]       = 0
    r["round"]    += 1
    r["phase"]     = "preflop"
    r["status"]    = "playing"

    uids = list(r["players"].keys())
    dp   = r["dealer_pos"] % len(uids)
    r["dealer_pos"] = dp
    sb_i = (dp+1) % len(uids)
    bb_i = (dp+2) % len(uids)

    for uid, p in r["players"].items():
        p["hand"]       = [r["deck"].pop(), r["deck"].pop()]
        p["bet_total"]  = 0
        p["bet_round"]  = 0
        p["status"]     = "active"   # active/folded/allin/out
        p["action"]     = None

    # Blinds
    sb_uid = uids[sb_i]; bb_uid = uids[bb_i]
    for uid, blind in [(sb_uid, r["small_blind"]), (bb_uid, r["big_blind"])]:
        actual = min(blind, r["players"][uid]["chips"])
        r["players"][uid]["chips"]    -= actual
        r["players"][uid]["bet_round"] = actual
        r["players"][uid]["bet_total"] = actual
        r["pot"] += actual

    r["turn_order"]   = uids
    first_act         = (bb_i+1) % len(uids)
    r["current_turn"] = uids[first_act]
    r["min_bet"]      = r["big_blind"]
    r["last_raise"]   = r["big_blind"]
    set_room(rid, r)
    return True

# ── RUTAS CASINO ──────────────────────────────────────────────────────────────

@casino.route("/casino")
def casino_lobby():
    if "user_id" not in session: return redirect(url_for("login"))
    from app import _casino_enabled
    if not _casino_enabled():
        flash("El casino no está disponible en este momento.", "info")
        return redirect(url_for("dashboard"))
    bj_rooms  = [(rid,d) for rid,d in _rooms.items() if d["game"]=="blackjack" and d["status"]!="finished"]
    pk_rooms  = [(rid,d) for rid,d in _rooms.items() if d["game"]=="poker"     and d["status"]!="finished"]
    return render_template("casino_lobby.html",
        bj_rooms=bj_rooms, pk_rooms=pk_rooms,
        balance=session.get("balance",0))

@casino.route("/casino/blackjack/join", methods=["POST"])
def bj_join():
    if "user_id" not in session: return redirect(url_for("login"))
    uid = str(session["user_id"])
    # Buscar si el jugador ya está en una sala activa
    for rid, d in _rooms.items():
        if d["game"]=="blackjack" and uid in d.get("players",{}) and d["status"] != "finished":
            return redirect(url_for("casino.bj_room", rid=rid))
    # Limpiar salas terminadas
    finished = [r for r,d in _rooms.items() if d["game"]=="blackjack" and d["status"]=="finished"]
    for r in finished: _rooms.pop(r, None)
    rooms = open_rooms("blackjack")
    rid   = rooms[0] if rooms else bj_new_room()
    r     = get_room(rid)
    if uid not in r["players"]:
        r["players"][uid] = {"name": session.get("username","?"), "bet":0, "hand":[], "hand2":[], "status":"waiting", "split":False, "doubled":False, "bet2":0}
        set_room(rid, r)
    return redirect(url_for("casino.bj_room", rid=rid))

@casino.route("/casino/blackjack/<rid>")
def bj_room(rid):
    if "user_id" not in session: return redirect(url_for("login"))
    r = get_room(rid)
    if not r: flash("Sala no encontrada.","error"); return redirect(url_for("casino.casino_lobby"))
    return render_template("casino_bj.html", rid=rid, room=r,
        uid=str(session["user_id"]), balance=session.get("balance",0))

@casino.route("/casino/roulette")
def roulette_page():
    if "user_id" not in session: return redirect(url_for("login"))
    return render_template("casino_roulette.html", balance=session.get("balance",0))

@casino.route("/casino/poker/join", methods=["POST"])
def poker_join():
    if "user_id" not in session: return redirect(url_for("login"))
    rooms = open_rooms("poker")
    rid   = rooms[0] if rooms else poker_new_room()
    r     = get_room(rid)
    uid   = str(session["user_id"])
    chips = float(request.form.get("buy_in", 2000))
    chips = max(CASINO_MIN_BET*2, min(chips, CASINO_MAX_BET))
    if uid not in r["players"]:
        r["players"][uid] = {"name":session.get("username","?"), "chips":chips,
            "hand":[], "bet_total":0, "bet_round":0, "status":"waiting", "action":None}
        set_room(rid, r)
    return redirect(url_for("casino.poker_room", rid=rid))

@casino.route("/casino/poker/<rid>")
def poker_room(rid):
    if "user_id" not in session: return redirect(url_for("login"))
    r = get_room(rid)
    if not r: flash("Sala no encontrada.","error"); return redirect(url_for("casino.casino_lobby"))
    return render_template("casino_poker.html", rid=rid, room=r,
        uid=str(session["user_id"]), balance=session.get("balance",0))

# ── API JSON ───────────────────────────────────────────────────────────────────

@casino.route("/api/casino/bj/bet", methods=["POST"])
def api_bj_bet():
    if "user_id" not in session: return jsonify({"ok":False,"msg":"Sin sesión"})
    data  = request.get_json()
    rid   = data.get("rid"); uid = str(session["user_id"])
    bet   = int(data.get("bet",0))
    r     = get_room(rid)
    if not r or uid not in r["players"]:
        return jsonify({"ok":False,"msg":"Sala inválida"})
    if bet < CASINO_MIN_BET or bet > CASINO_MAX_BET:
        return jsonify({"ok":False,"msg":f"Apuesta entre ${CASINO_MIN_BET} y ${CASINO_MAX_BET}"})
    # Verificar saldo
    from app import fetchone as fo, execute as ex, get_db
    conn = get_db()
    user = fo(conn, "SELECT balance FROM users WHERE id=?", (session["user_id"],))
    conn.close()
    if not user or user["balance"] < bet:
        return jsonify({"ok":False,"msg":"Saldo insuficiente"})
    r["players"][uid]["bet"] = bet
    # Descontar apuesta inmediatamente
    conn = get_db()
    ex(conn, "UPDATE users SET balance=balance-? WHERE id=?", (bet, session["user_id"]))
    conn.commit(); conn.close()
    # Iniciar ronda inmediatamente (no esperar a otros jugadores)
    if r["status"] == "waiting":
        bj_deal_room(rid)
    set_room(rid, r)
    _emit_casino_room(rid, None)
    return jsonify({"ok":True,"room":_sanitize_room(rid, uid)})

@casino.route("/api/casino/bj/action", methods=["POST"])
def api_bj_action():
    if "user_id" not in session: return jsonify({"ok":False})
    data   = request.get_json()
    rid    = data.get("rid"); uid = str(session["user_id"])
    action = data.get("action")   # hit/stand/double/split
    r      = get_room(rid)
    if not r or r["current_turn"] != uid:
        return jsonify({"ok":False,"msg":"No es tu turno"})
    p = r["players"][uid]

    if action == "hit":
        p["hand"].append(r["deck"].pop())
        if hand_value(p["hand"]) >= 21:
            p["status"] = "bust" if hand_value(p["hand"])>21 else "stand"
            _bj_advance_turn(rid)
    elif action == "stand":
        p["status"] = "stand"
        _bj_advance_turn(rid)
    elif action == "double":
        if len(p["hand"])==2:
            from app import execute as ex, get_db
            conn = get_db()
            ex(conn,"UPDATE users SET balance=balance-? WHERE id=?", (p["bet"], session["user_id"]))
            conn.commit(); conn.close()
            p["bet"] *= 2; p["doubled"] = True
            p["hand"].append(r["deck"].pop())
            p["status"] = "stand"
            _bj_advance_turn(rid)
    elif action == "split":
        if len(p["hand"])==2 and p["hand"][0]["rank"]==p["hand"][1]["rank"] and not p["split"]:
            from app import execute as ex, get_db
            conn = get_db()
            ex(conn,"UPDATE users SET balance=balance-? WHERE id=?", (p["bet"], session["user_id"]))
            conn.commit(); conn.close()
            p["bet2"]  = p["bet"]; p["split"] = True
            p["hand2"] = [p["hand"].pop(), r["deck"].pop()]
            p["hand"].append(r["deck"].pop())

    set_room(rid, r)
    # Resolver si todos terminaron
    if _bj_all_done(rid):
        from app import fetchone as fo, execute as ex, get_db, now as now_app
        conn = get_db()
        bj_resolve_room(conn, rid, fo, ex, _casino_house_log)
        conn.commit(); conn.close()
    # Emitir estado actualizado a todos en la sala
    _emit_casino_room(rid, None)
    return jsonify({"ok":True,"room":_sanitize_room(rid, uid)})

def _bj_advance_turn(rid):
    r   = get_room(rid)
    idx = r["turn_order"].index(r["current_turn"])
    nxt = idx + 1
    while nxt < len(r["turn_order"]):
        nuid = r["turn_order"][nxt]
        if r["players"][nuid]["status"] == "playing":
            r["current_turn"] = nuid; break
        nxt += 1
    else:
        r["current_turn"] = None
    set_room(rid, r)

def _bj_all_done(rid):
    r = get_room(rid)
    return all(p["status"] != "playing" for p in r["players"].values())

@casino.route("/api/casino/roulette/spin", methods=["POST"])
def api_roulette_spin():
    if "user_id" not in session: return jsonify({"ok":False})
    data = request.get_json()
    bets = data.get("bets", [])   # [{type, value, amount}]
    if not bets: return jsonify({"ok":False,"msg":"Sin apuestas"})

    total_bet = sum(int(b["amount"]) for b in bets)
    if total_bet < CASINO_MIN_BET or total_bet > CASINO_MAX_BET:
        return jsonify({"ok":False,"msg":f"Apuesta entre ${CASINO_MIN_BET} y ${CASINO_MAX_BET}"})

    from app import fetchone as fo, execute as ex, get_db
    conn = get_db()
    user = fo(conn, "SELECT balance FROM users WHERE id=?", (session["user_id"],))
    if not user or user["balance"] < total_bet:
        conn.close(); return jsonify({"ok":False,"msg":"Saldo insuficiente"})

    ex(conn,"UPDATE users SET balance=balance-? WHERE id=?",(total_bet, session["user_id"]))

    winning = roulette_spin()
    wlabel  = roulette_number_label(winning)
    total_payout = 0
    bet_results  = []

    for b in bets:
        amt  = int(b["amount"])
        mult = roulette_resolve_bet(b["type"], b["value"], winning)
        if mult > 0:
            bruto  = amt + amt * mult   # apuesta devuelta + ganancia
            payout = round50(bruto)
            redond = bruto - payout
            total_payout += payout
            if redond > 0:
                _casino_house_log(conn,"roulette","single",redond,"redondeo",f"Redondeo ruleta uid={session['user_id']}")
            bet_results.append({"type":b["type"],"value":b["value"],"amount":amt,"win":True,"payout":payout,"mult":mult,"gain":round50(amt*mult)})
        else:
            bet_results.append({"type":b["type"],"value":b["value"],"amount":amt,"win":False,"payout":0,"mult":0,"gain":0})

    if total_payout > 0:
        ex(conn,"UPDATE users SET balance=balance+? WHERE id=?",(total_payout, session["user_id"]))

    house_gain = total_bet - total_payout
    ex(conn,"""INSERT INTO casino_log (user_id,game,room_id,bet,payout,result,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (session["user_id"],"roulette","single",total_bet,total_payout,wlabel,now_s()))

    if house_gain > 0:
        _casino_house_log(conn,"roulette","single",house_gain,"profit",f"Ruleta uid={session['user_id']} número={wlabel}")
    elif house_gain < 0:
        _casino_house_log(conn,"roulette","single",house_gain,"expense",f"Ruleta pagó extra uid={session['user_id']}")

    conn.commit(); conn.close()

    new_bal = fo(get_db(), "SELECT balance FROM users WHERE id=?",(session["user_id"],))
    session["balance"] = new_bal["balance"] if new_bal else 0

    return jsonify({"ok":True,"winning_number":winning,"winning_label":wlabel,
        "bets":bet_results,"total_bet":total_bet,"total_payout":total_payout,
        "net": total_payout - total_bet, "new_balance": session.get("balance",0)})

@casino.route("/api/casino/poker/action", methods=["POST"])
def api_poker_action():
    if "user_id" not in session: return jsonify({"ok":False})
    data   = request.get_json()
    rid    = data.get("rid"); uid = str(session["user_id"])
    action = data.get("action")   # fold/check/call/raise/allin
    amount = int(data.get("amount", 0))
    r      = get_room(rid)
    if not r or r["current_turn"] != uid:
        return jsonify({"ok":False,"msg":"No es tu turno"})
    p = r["players"][uid]

    if action == "fold":
        p["status"] = "folded"; p["action"] = "fold"
    elif action in ("check","call","raise","allin"):
        to_call = r["min_bet"] - p["bet_round"]
        if action == "check":
            if to_call > 0: return jsonify({"ok":False,"msg":"No puedes hacer check"})
            p["action"] = "check"
        elif action == "call":
            actual = min(to_call, p["chips"])
            p["chips"] -= actual; p["bet_round"] += actual; p["bet_total"] += actual; r["pot"] += actual
            p["action"] = "call"
            if p["chips"] == 0: p["status"] = "allin"
        elif action == "raise":
            if amount < r["last_raise"]*2: return jsonify({"ok":False,"msg":"Raise mínimo insuficiente"})
            total_add = min(to_call + amount, p["chips"])
            p["chips"] -= total_add; p["bet_round"] += total_add; p["bet_total"] += total_add; r["pot"] += total_add
            r["min_bet"] = p["bet_round"]; r["last_raise"] = amount
            p["action"] = "raise"
        elif action == "allin":
            r["pot"] += p["chips"]; p["bet_round"] += p["chips"]; p["bet_total"] += p["chips"]; p["chips"] = 0
            p["status"] = "allin"; p["action"] = "allin"

    # Avanzar turno
    _poker_advance_turn(rid)

    # Verificar si la fase terminó
    r = get_room(rid)
    if _poker_phase_done(rid):
        _poker_next_phase(rid)

    set_room(rid, r)
    _emit_casino_room(rid, None)
    return jsonify({"ok":True,"room":_sanitize_room(rid, uid)})

def _poker_advance_turn(rid):
    r    = get_room(rid)
    uids = r["turn_order"]
    idx  = uids.index(r["current_turn"])
    for i in range(1, len(uids)+1):
        nuid = uids[(idx+i)%len(uids)]
        if r["players"][nuid]["status"] == "active":
            r["current_turn"] = nuid; break
    else:
        r["current_turn"] = None
    set_room(rid, r)

def _poker_phase_done(rid):
    r = get_room(rid)
    active = [p for p in r["players"].values() if p["status"]=="active"]
    if len(active) <= 1: return True
    # Todos igualaron la apuesta máxima o actuaron
    max_bet = max(p["bet_round"] for p in r["players"].values() if p["status"]!="folded")
    return all(p["bet_round"]==max_bet or p["status"] in ("folded","allin") for p in r["players"].values())

def _poker_next_phase(rid):
    r = get_room(rid)
    # Resetear apuestas de ronda
    for p in r["players"].values(): p["bet_round"] = 0
    r["min_bet"] = r["big_blind"]; r["last_raise"] = r["big_blind"]

    phase_order = ["preflop","flop","turn","river","showdown"]
    idx = phase_order.index(r["phase"])
    r["phase"] = phase_order[min(idx+1, len(phase_order)-1)]

    if r["phase"] == "flop":
        r["community"] += [r["deck"].pop() for _ in range(3)]
    elif r["phase"] == "turn":
        r["community"].append(r["deck"].pop())
    elif r["phase"] == "river":
        r["community"].append(r["deck"].pop())
    elif r["phase"] == "showdown":
        _poker_showdown(rid)
        return

    # Primer jugador activo después del dealer
    uids   = r["turn_order"]
    dp     = r["dealer_pos"]
    for i in range(1, len(uids)+1):
        nuid = uids[(dp+i)%len(uids)]
        if r["players"][nuid]["status"] == "active":
            r["current_turn"] = nuid; break
    set_room(rid, r)

def _poker_showdown(rid):
    from app import fetchone as fo, execute as ex, get_db
    r       = get_room(rid)
    pot     = r["pot"]
    rake    = min(round(pot * POKER_RAKE_PCT, 2), POKER_RAKE_CAP)
    rake    = round50(rake)
    net_pot = pot - rake

    conn = get_db()

    # Evaluar manos
    active = {uid:p for uid,p in r["players"].items() if p["status"] in ("active","allin")}
    if not active:
        conn.close(); return

    scores = {}
    for uid, p in active.items():
        all_cards = p["hand"] + r["community"]
        scores[uid] = eval_poker_hand(all_cards)

    winner_uid = max(scores, key=lambda u: scores[u])
    payout_bruto = net_pot
    payout       = round50(payout_bruto)
    redondo      = payout_bruto - payout

    ex(conn,"UPDATE users SET balance=balance+? WHERE id=?",(payout, int(winner_uid)))
    ex(conn,"""INSERT INTO casino_log (user_id,game,room_id,bet,payout,result,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (int(winner_uid),"poker",rid,r["players"][winner_uid]["bet_total"],payout,"won",now_s()))

    for uid in active:
        if uid != winner_uid:
            ex(conn,"""INSERT INTO casino_log (user_id,game,room_id,bet,payout,result,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (int(uid),"poker",rid,r["players"][uid]["bet_total"],0,"lost",now_s()))

    if rake > 0:
        _casino_house_log(conn,"poker",rid,rake,"profit",f"Rake {POKER_RAKE_PCT*100:.0f}% sala {rid}")
    if redondo > 0:
        _casino_house_log(conn,"poker",rid,redondo,"redondeo",f"Redondeo pot póker {rid}")

    conn.commit(); conn.close()

    r["status"]  = "finished"
    r["winner"]  = winner_uid
    r["scores"]  = {uid: scores[uid][0] for uid in scores}  # solo categoría
    r["payout"]  = payout
    r["rake"]    = rake
    set_room(rid, r)

# ── HELPERS INTERNOS ──────────────────────────────────────────────────────────

def _casino_house_log(conn, game, room_id, amount, log_type, note):
    from app import execute as ex
    ex(conn, """INSERT INTO casino_house_log (game,room_id,amount,type,note,created_at)
        VALUES (?,?,?,?,?,?)""", (game, str(room_id), amount, log_type, note, now_s()))

def _emit_casino_room(rid, my_uid):
    """Emite el estado de la sala a todos los conectados via Socket.IO."""
    try:
        from flask_socketio import emit as _emit
        r = _sanitize_room(rid, my_uid or "")
        _sio_instance.emit("casino_state", r, room=rid)
    except Exception:
        pass  # Si no hay socket activo, no pasa nada

_sio_instance = None  # Se asigna en register_casino

def _sanitize_room(rid, my_uid):
    """Devuelve el estado de la sala ocultando las cartas de otros jugadores en póker."""
    r = get_room(rid)
    if not r: return {}
    safe = dict(r)
    if r["game"] == "poker":
        safe["players"] = {}
        for uid, p in r["players"].items():
            pd = dict(p)
            if uid != my_uid and r["phase"] not in ("showdown","finished"):
                pd["hand"] = [{"rank":"?","suit":"?"}] * len(p["hand"])
            safe["players"][uid] = pd
    return safe

# ── TABLAS CASINO ──────────────────────────────────────────────────────────────

CASINO_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS casino_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    game TEXT NOT NULL,
    room_id TEXT NOT NULL,
    bet REAL NOT NULL DEFAULT 0,
    payout REAL NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS casino_house_log (
    id SERIAL PRIMARY KEY,
    game TEXT NOT NULL,
    room_id TEXT NOT NULL,
    amount REAL NOT NULL,
    type TEXT NOT NULL DEFAULT 'profit',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

def init_casino_tables(conn):
    cur = conn.cursor()
    for stmt in CASINO_TABLES_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt: cur.execute(stmt)
    conn.commit()

def register_casino(app, socketio_instance):
    """Llama esto en app.py para registrar el módulo casino."""
    global _sio_instance
    _sio_instance = socketio_instance
    app.register_blueprint(casino)

    # Socket events para tiempo real
    @socketio_instance.on("casino_join_room")
    def on_casino_join(data):
        join_room(data.get("rid"))
        # Enviar estado actual al recién conectado
        rid = data.get("rid"); uid = data.get("uid","")
        r   = _sanitize_room(rid, uid)
        sio_emit("casino_state", r, room=rid)

    @socketio_instance.on("casino_leave_room")
    def on_casino_leave(data):
        leave_room(data.get("rid"))

    @socketio_instance.on("casino_state_request")
    def on_state_req(data):
        rid = data.get("rid"); uid = data.get("uid","")
        r   = _sanitize_room(rid, uid)
        sio_emit("casino_state", r, room=rid)
