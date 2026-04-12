"""
BetSport — Plataforma de apuestas deportivas
Flask + SQLite | Render-ready | Pagos en efectivo
"""
from flask import Flask, render_template, request, redirect, url_for, session, flash
from functools import wraps
import sqlite3, secrets, hashlib, os
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "betsport-secret-cambia-esto-2024")
DB = os.path.join(os.path.dirname(__file__), "betsport.db")

HOUSE_CUT    = 0.08  # 8% del pool de apuestas perdedoras va a la casa
FIELD_CUT    = 0.07  # 7% del pool de apuestas perdedoras va a jugadores de cancha ganadores (configurable por evento)
MIN_ODD      = 1.01

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def hp(pw): return hashlib.sha256(pw.encode()).hexdigest()

def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'player',
            balance REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_by TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            home TEXT NOT NULL,
            away TEXT NOT NULL,
            league TEXT NOT NULL,
            entry_fee REAL NOT NULL DEFAULT 0.0,
            house_budget REAL NOT NULL DEFAULT 0.0,
            pool REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'open',
            winner_key TEXT DEFAULT '',
            field_cut_pct REAL NOT NULL DEFAULT 0.07,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            option_key TEXT NOT NULL,
            label TEXT NOT NULL,
            odd REAL NOT NULL,
            total_bet REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            paid_at TEXT NOT NULL,
            UNIQUE(user_id, event_id)
        );
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            option_key TEXT NOT NULL,
            option_label TEXT NOT NULL,
            amount REAL NOT NULL,
            odd_at_bet REAL NOT NULL,
            potential REAL NOT NULL,
            result TEXT NOT NULL DEFAULT 'pending',
            payout REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bet_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            option_key TEXT NOT NULL,
            option_label TEXT NOT NULL,
            amount REAL NOT NULL,
            odd_at_request REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT DEFAULT '',
            resolved_at TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS house_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS field_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            team_key TEXT NOT NULL,
            entry_paid REAL NOT NULL DEFAULT 0.0,
            payout REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );
        """)
        if not db.execute("SELECT id FROM users WHERE role='admin'").fetchone():
            db.execute("""INSERT INTO users (username,full_name,phone,email,password_hash,role,balance,created_at)
                VALUES ('admin','Administrador','000000000','admin@betsport.com',?,'admin',0.0,?)""",
                (hp("admin123"), now()))

def login_required(f):
    @wraps(f)
    def d(*a,**kw):
        if "user_id" not in session: return redirect(url_for("login"))
        return f(*a,**kw)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a,**kw):
        if session.get("role") != "admin":
            flash("Acceso restringido.","error"); return redirect(url_for("dashboard"))
        return f(*a,**kw)
    return d

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        with get_db() as db:
            user = db.execute("SELECT * FROM users WHERE username=? AND password_hash=?", (u, hp(p))).fetchone()
        if user:
            session.update(user_id=user["id"], username=user["username"], role=user["role"])
            return redirect(url_for("admin_panel") if user["role"]=="admin" else url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.","error")
    return render_template("login.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        token_str = request.form["token"].strip()
        username  = request.form["username"].strip()
        full_name = request.form["full_name"].strip()
        phone     = request.form["phone"].strip()
        email     = request.form.get("email","").strip()
        password  = request.form["password"]
        with get_db() as db:
            tok = db.execute("SELECT * FROM tokens WHERE token=? AND used=0", (token_str,)).fetchone()
            if not tok:
                flash("Token invalido o ya usado.","error"); return render_template("register.html")
            if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                flash("Usuario ya existe.","error"); return render_template("register.html")
            db.execute("INSERT INTO users (username,full_name,phone,email,password_hash,role,balance,created_at) VALUES (?,?,?,?,?,'player',0.0,?)",
                (username, full_name, phone, email, hp(password), now()))
            db.execute("UPDATE tokens SET used=1, used_by=? WHERE token=?", (username, token_str))
        flash("Cuenta creada. Inicia sesion.","success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# ── PERFIL JUGADOR ────────────────────────────────────────────────────────────

@app.route("/profile")
@login_required
def profile():
    with get_db() as db:
        user  = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        bets  = db.execute("""SELECT b.*,e.home,e.away,e.sport,e.league,e.status,e.winner_key
            FROM bets b JOIN events e ON b.event_id=e.id WHERE b.user_id=? ORDER BY b.created_at DESC""",
            (session["user_id"],)).fetchall()
        bet_reqs = db.execute("""SELECT br.*,e.home,e.away,e.league,e.status as event_status
            FROM bet_requests br JOIN events e ON br.event_id=e.id
            WHERE br.user_id=? ORDER BY br.created_at DESC""",
            (session["user_id"],)).fetchall()
        stats = {
            "total_bet": sum(b["amount"] for b in bets),
            "total_won": sum(b["payout"] for b in bets if b["result"]=="won"),
            "bets_won":  sum(1 for b in bets if b["result"]=="won"),
            "bets_lost": sum(1 for b in bets if b["result"]=="lost"),
        }
    return render_template("profile.html", user=user, bets=bets, bet_reqs=bet_reqs, stats=stats)

# ── DASHBOARD JUGADOR ─────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    if session["role"] == "admin": return redirect(url_for("admin_panel"))
    with get_db() as db:
        user   = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        events = db.execute("SELECT * FROM events WHERE status IN ('open','closed') ORDER BY created_at DESC").fetchall()
        edata  = []
        for ev in events:
            odds = db.execute("SELECT * FROM event_odds WHERE event_id=?", (ev["id"],)).fetchall()
            has_entry = db.execute("SELECT id FROM entries WHERE user_id=? AND event_id=?",
                (session["user_id"], ev["id"])).fetchone()
            pending_entry = db.execute(
                "SELECT id FROM cash_requests WHERE user_id=? AND type=? AND status='pending'",
                (session["user_id"], f"entry_{ev['id']}")
            ).fetchone()
            # Apuestas pendientes del jugador para este evento
            my_bet_reqs = db.execute(
                "SELECT * FROM bet_requests WHERE user_id=? AND event_id=? ORDER BY created_at DESC",
                (session["user_id"], ev["id"])
            ).fetchall()
            edata.append({
                "event": ev, "odds": odds,
                "has_entry": bool(has_entry),
                "pending_entry": bool(pending_entry),
                "my_bet_reqs": my_bet_reqs,
                "bets_open": ev["status"] == "open"
            })
        my_bets = db.execute("""SELECT b.*,e.home,e.away,e.league,e.sport,e.status,e.winner_key
            FROM bets b JOIN events e ON b.event_id=e.id WHERE b.user_id=? ORDER BY b.created_at DESC LIMIT 10""",
            (session["user_id"],)).fetchall()
    return render_template("dashboard.html", user=user, edata=edata, my_bets=my_bets)

# ── SOLICITAR ENTRADA (apuesta de apostador web) ──────────────────────────────

@app.route("/event/<int:eid>/request_entry", methods=["POST"])
@login_required
def request_entry(eid):
    with get_db() as db:
        ev = db.execute("SELECT * FROM events WHERE id=? AND status IN ('open','closed')", (eid,)).fetchone()
        if not ev: flash("Evento no disponible.","error"); return redirect(url_for("dashboard"))
        if db.execute("SELECT id FROM entries WHERE user_id=? AND event_id=?", (session["user_id"],eid)).fetchone():
            flash("Ya tienes entrada a este evento.","info"); return redirect(url_for("dashboard"))
        if db.execute("SELECT id FROM cash_requests WHERE user_id=? AND type=? AND status='pending'",
            (session["user_id"], f"entry_{eid}")).fetchone():
            flash("Ya enviaste una solicitud. Espera que el admin la confirme.","info"); return redirect(url_for("dashboard"))
        db.execute("INSERT INTO cash_requests (user_id,type,amount,status,note,created_at) VALUES (?,?,?,'pending',?,?)",
            (session["user_id"], f"entry_{eid}", ev["entry_fee"],
             f"Entrada apuesta: {ev['home']} vs {ev['away']}", now()))
    flash(f"Solicitud enviada. Paga ${ev['entry_fee']:,.0f} en efectivo al admin.","info")
    return redirect(url_for("dashboard"))

# ── SOLICITAR APUESTA (nuevo flujo: declara monto, admin aprueba) ─────────────

@app.route("/event/<int:eid>/request_bet", methods=["POST"])
@login_required
def request_bet(eid):
    option_key = request.form.get("option_key","")
    try: amount = float(request.form.get("amount",0))
    except: flash("Monto invalido.","error"); return redirect(url_for("dashboard"))
    if amount <= 0: flash("Monto debe ser > 0.","error"); return redirect(url_for("dashboard"))

    with get_db() as db:
        ev = db.execute("SELECT * FROM events WHERE id=? AND status='open'", (eid,)).fetchone()
        if not ev: flash("Las apuestas estan cerradas para este evento.","error"); return redirect(url_for("dashboard"))
        if not db.execute("SELECT id FROM entries WHERE user_id=? AND event_id=?", (session["user_id"],eid)).fetchone():
            flash("Necesitas tener entrada confirmada para apostar.","error"); return redirect(url_for("dashboard"))
        odd_row = db.execute("SELECT * FROM event_odds WHERE event_id=? AND option_key=?", (eid,option_key)).fetchone()
        if not odd_row or odd_row["odd"] <= MIN_ODD:
            flash("Opcion no disponible.","error"); return redirect(url_for("dashboard"))
        # Solo bloquear si ya tiene una solicitud pendiente para la MISMA opción y monto exacto
        # (evita duplicados accidentales por doble click, pero permite nuevas apuestas)
        existing = db.execute(
            "SELECT id FROM bet_requests WHERE user_id=? AND event_id=? AND option_key=? AND amount=? AND status='pending'",
            (session["user_id"], eid, option_key, amount)).fetchone()
        if existing:
            flash("Ya tienes una solicitud idéntica pendiente para esta opción.","info")
            return redirect(url_for("dashboard"))
        odd_at_request = odd_row["odd"]
        db.execute("""INSERT INTO bet_requests (user_id,event_id,option_key,option_label,amount,odd_at_request,status,created_at)
            VALUES (?,?,?,?,?,?,'pending',?)""",
            (session["user_id"], eid, option_key, odd_row["label"], amount, odd_at_request, now()))
    flash(f"Solicitud enviada: ${amount:,.0f} a '{odd_row['label']}' @ {odd_at_request:.2f}x. Espera confirmacion del admin.","info")
    return redirect(url_for("dashboard"))

# ── RETIRAR SOLICITUD DE APUESTA (antes de que se cierre) ────────────────────

@app.route("/bet_request/<int:brid>/cancel", methods=["POST"])
@login_required
def cancel_bet_request(brid):
    with get_db() as db:
        br = db.execute("SELECT * FROM bet_requests WHERE id=? AND user_id=? AND status='pending'",
            (brid, session["user_id"])).fetchone()
        if not br:
            flash("Solicitud no encontrada o ya procesada.","error")
            return redirect(url_for("dashboard"))
        ev = db.execute("SELECT * FROM events WHERE id=?", (br["event_id"],)).fetchone()
        if ev["status"] != "open":
            flash("No puedes retirar una apuesta con las apuestas cerradas.","error")
            return redirect(url_for("dashboard"))
        db.execute("UPDATE bet_requests SET status='cancelled' WHERE id=?", (brid,))
    flash("Solicitud de apuesta retirada.","success")
    return redirect(url_for("dashboard"))

# ── ADMIN PANEL ────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    with get_db() as db:
        tokens  = db.execute("SELECT * FROM tokens ORDER BY created_at DESC").fetchall()
        players = db.execute("SELECT * FROM users WHERE role='player' ORDER BY created_at DESC").fetchall()
        # Pagos pendientes de entrada
        pending_entries = db.execute("""SELECT cr.*,u.username,u.full_name,u.phone
            FROM cash_requests cr JOIN users u ON cr.user_id=u.id
            WHERE cr.status='pending' ORDER BY cr.created_at""").fetchall()
        # Solicitudes de apuesta pendientes — con cálculo de disponible
        raw_bets = db.execute("""SELECT br.*,u.username,u.full_name,u.phone,
            e.home,e.away,e.league,e.sport
            FROM bet_requests br
            JOIN users u ON br.user_id=u.id
            JOIN events e ON br.event_id=e.id
            WHERE br.status='pending' ORDER BY br.created_at""").fetchall()
        pending_bets = []
        for br in raw_bets:
            ev_br = db.execute("SELECT * FROM events WHERE id=?", (br["event_id"],)).fetchone()
            house_budget_br = ev_br["house_budget"] if ev_br else 0
            field_cut_pct_br = ev_br["field_cut_pct"] if ev_br else FIELD_CUT
            total_cuts_br   = HOUSE_CUT + field_cut_pct_br
            odd_row = db.execute("SELECT odd FROM event_odds WHERE event_id=? AND option_key=?",
                (br["event_id"], br["option_key"])).fetchone()
            current_odd = odd_row["odd"] if odd_row else 1.0

            # Saldo del jugador
            jugador_br = db.execute("SELECT balance FROM users WHERE id=?", (br["user_id"],)).fetchone()
            saldo_ok = jugador_br["balance"] >= br["amount"] if jugador_br else False

            all_odds = {o["option_key"]: o for o in db.execute(
                "SELECT * FROM event_odds WHERE event_id=?", (br["event_id"],)
            ).fetchall()}

            # Pool confirmado por opción, incluyendo esta apuesta nueva
            pool_por_opcion = {}
            for okey in all_odds:
                existing = db.execute(
                    "SELECT COALESCE(SUM(amount),0) as t FROM bets WHERE event_id=? AND option_key=? AND result='pending'",
                    (br["event_id"], okey)
                ).fetchone()["t"]
                pool_por_opcion[okey] = existing + (br["amount"] if okey == br["option_key"] else 0)

            total_pool = sum(pool_por_opcion.values())

            peor_deficit = 0.0
            for okey in all_odds:
                win_pool    = pool_por_opcion[okey]
                losing_pool = total_pool - win_pool
                costo_neto  = round(win_pool - losing_pool * total_cuts_br, 2)
                deficit     = round(costo_neto - house_budget_br, 2)
                if deficit > peor_deficit:
                    peor_deficit = deficit

            # Margen disponible para más apuestas en la opción solicitada
            # = cuánto más puede ganar este lado antes de que la casa quede en rojo
            win_pool_actual = pool_por_opcion[br["option_key"]]
            losing_pool_actual = total_pool - win_pool_actual
            costo_neto_actual = round(win_pool_actual - losing_pool_actual * total_cuts_br, 2)
            disponible = round(house_budget_br - costo_neto_actual, 2)

            aprobable = saldo_ok and peor_deficit <= 0.01
            pending_bets.append({
                **dict(br),
                "disponible": max(0, disponible),
                "ganancia_neta": round(br["amount"] * (current_odd - 1), 2),
                "aprobable": aprobable,
                "current_odd": current_odd,
                "saldo_ok": saldo_ok,
            })
        edata = []
        for ev in db.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall():
            odds  = db.execute("SELECT * FROM event_odds WHERE event_id=?", (ev["id"],)).fetchall()
            count = db.execute("SELECT COUNT(*) as c FROM entries WHERE event_id=?", (ev["id"],)).fetchone()["c"]
            fp_home = db.execute("SELECT * FROM field_players WHERE event_id=? AND team_key='home'", (ev["id"],)).fetchall()
            fp_away = db.execute("SELECT * FROM field_players WHERE event_id=? AND team_key='away'", (ev["id"],)).fetchall()
            total_field_home = sum(p["entry_paid"] for p in fp_home)
            total_field_away = sum(p["entry_paid"] for p in fp_away)
            edata.append({
                "event": ev, "odds": odds, "count": count,
                "fp_home": fp_home, "fp_away": fp_away,
                "total_field_home": total_field_home,
                "total_field_away": total_field_away,
            })
        house_total = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM house_log").fetchone()["t"]
    return render_template("admin.html",
        tokens=tokens, players=players,
        pending_entries=pending_entries, pending_bets=pending_bets,
        edata=edata, house_total=house_total)

# ── ADMIN: TOKENS ──────────────────────────────────────────────────────────────

@app.route("/admin/token/create", methods=["POST"])
@login_required
@admin_required
def create_token():
    note = request.form.get("note","").strip()
    tok  = secrets.token_urlsafe(10)
    with get_db() as db:
        db.execute("INSERT INTO tokens (token,used,note,created_at) VALUES (?,0,?,?)", (tok,note,now()))
    flash(f"Token creado: {tok}","success"); return redirect(url_for("admin_panel"))

@app.route("/admin/token/delete/<int:tid>", methods=["POST"])
@login_required
@admin_required
def delete_token(tid):
    with get_db() as db:
        db.execute("DELETE FROM tokens WHERE id=? AND used=0", (tid,))
    flash("Token eliminado.","success"); return redirect(url_for("admin_panel"))

# ── ADMIN: CREAR EVENTO ────────────────────────────────────────────────────────

@app.route("/admin/event/create", methods=["POST"])
@login_required
@admin_required
def create_event():
    f = request.form
    sport          = f["sport"]
    home           = f["home"].strip()
    away           = f["away"].strip()
    league         = f["league"].strip()
    entry_fee      = float(f.get("entry_fee", 0))
    initial_budget = float(f.get("initial_budget", 0))
    field_cut_pct  = float(f.get("field_cut_pct", FIELD_CUT))

    if sport == "futbol":
        odd_home = float(f.get("odd_home", 2.20))
        odd_draw = float(f.get("odd_draw", 3.20))
        odd_away = float(f.get("odd_away", 2.80))
    else:
        odd_home = float(f.get("odd_home", 1.90))
        odd_away = float(f.get("odd_away", 2.00))

    with get_db() as db:
        cur = db.execute("""INSERT INTO events (sport,home,away,league,entry_fee,house_budget,pool,status,field_cut_pct,created_at)
            VALUES (?,?,?,?,?,?,0,'open',?,?)""", (sport,home,away,league,entry_fee,initial_budget,field_cut_pct,now()))
        eid = cur.lastrowid
        if initial_budget > 0:
            db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
                (eid, initial_budget, "Presupuesto inicial de la casa", now()))

        if sport == "futbol":
            options = [("home","Local",odd_home),("draw","Empate",odd_draw),("away","Visitante",odd_away)]
        else:
            options = [("home","Local",odd_home),("away","Visitante",odd_away)]

        for key, label, odd in options:
            db.execute("INSERT INTO event_odds (event_id,option_key,label,odd,total_bet) VALUES (?,?,?,?,0)",
                (eid, key, label, odd))

    flash(f"Evento '{home} vs {away}' creado.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/event/delete/<int:eid>", methods=["POST"])
@login_required
@admin_required
def delete_event(eid):
    with get_db() as db:
        for t in ["event_odds","entries","bets","bet_requests","field_players"]:
            db.execute(f"DELETE FROM {t} WHERE event_id=?", (eid,))
        db.execute("DELETE FROM events WHERE id=?", (eid,))
    flash("Evento eliminado.","success"); return redirect(url_for("admin_panel"))

@app.route("/admin/event/close/<int:eid>", methods=["POST"])
@login_required
@admin_required
def close_event(eid):
    with get_db() as db:
        db.execute("UPDATE events SET status='closed' WHERE id=?", (eid,))
        # Cancelar solicitudes pendientes de apuesta al cerrar
        db.execute("UPDATE bet_requests SET status='cancelled' WHERE event_id=? AND status='pending'", (eid,))
    flash("Evento cerrado. Las solicitudes pendientes fueron canceladas.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/event/reopen/<int:eid>", methods=["POST"])
@login_required
@admin_required
def reopen_event(eid):
    with get_db() as db:
        db.execute("UPDATE events SET status='open' WHERE id=? AND status='closed'", (eid,))
    flash("Evento reabierto para nuevas apuestas.","success"); return redirect(url_for("admin_panel"))

# ── ADMIN: AJUSTE MANUAL DE MULTIPLICADORES ────────────────────────────────────

@app.route("/admin/event/<int:eid>/odds/adjust", methods=["POST"])
@login_required
@admin_required
def adjust_odds(eid):
    with get_db() as db:
        ev = db.execute("SELECT * FROM events WHERE id=? AND status!='finished'", (eid,)).fetchone()
        if not ev:
            flash("Evento no valido o ya finalizado.","error"); return redirect(url_for("admin_panel"))
        # Ajuste del % jugadores de cancha
        new_pct = request.form.get("field_cut_pct","").strip()
        if new_pct:
            try:
                pct = round(float(new_pct), 3)
                if pct < 0 or pct > 0.5:
                    flash("El % para jugadores de cancha debe estar entre 0 y 50%.","error")
                    return redirect(url_for("admin_panel"))
                db.execute("UPDATE events SET field_cut_pct=? WHERE id=?", (pct, eid))
            except ValueError:
                flash("Porcentaje invalido.","error"); return redirect(url_for("admin_panel"))
        # Ajuste de odds
        odds = db.execute("SELECT * FROM event_odds WHERE event_id=?", (eid,)).fetchall()
        updated = 0
        for o in odds:
            val = request.form.get(f"odd_{o['option_key']}", "").strip()
            if val:
                try:
                    new_odd = round(float(val), 2)
                    if new_odd < MIN_ODD:
                        flash(f"Multiplicador de '{o['label']}' debe ser >= {MIN_ODD}.","error")
                        return redirect(url_for("admin_panel"))
                    db.execute("UPDATE event_odds SET odd=? WHERE id=?", (new_odd, o["id"]))
                    updated += 1
                except ValueError:
                    flash(f"Valor invalido para '{o['label']}'.","error")
                    return redirect(url_for("admin_panel"))
    flash(f"Configuracion actualizada.","success")
    return redirect(url_for("admin_panel"))

# ── ADMIN: JUGADORES DE CANCHA ─────────────────────────────────────────────────

@app.route("/admin/event/<int:eid>/field_player/add", methods=["POST"])
@login_required
@admin_required
def add_field_player(eid):
    name       = request.form.get("name","").strip()
    team_key   = request.form.get("team_key","")
    entry_paid = float(request.form.get("entry_paid", 0))
    if not name or team_key not in ("home","away"):
        flash("Datos invalidos.","error"); return redirect(url_for("admin_panel"))
    with get_db() as db:
        ev = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not ev: flash("Evento no encontrado.","error"); return redirect(url_for("admin_panel"))
        db.execute("INSERT INTO field_players (event_id,name,team_key,entry_paid,payout,created_at) VALUES (?,?,?,?,0,?)",
            (eid, name, team_key, entry_paid, now()))
        # La cuota va al presupuesto de la casa
        db.execute("UPDATE events SET house_budget=house_budget+? WHERE id=?", (entry_paid, eid))
        db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
            (eid, entry_paid, f"Cuota cancha: {name} ({team_key})", now()))
    flash(f"Jugador '{name}' agregado al equipo {'Local' if team_key=='home' else 'Visitante'}.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/field_player/<int:fpid>/delete", methods=["POST"])
@login_required
@admin_required
def delete_field_player(fpid):
    with get_db() as db:
        fp = db.execute("SELECT * FROM field_players WHERE id=?", (fpid,)).fetchone()
        if not fp: flash("Jugador no encontrado.","error"); return redirect(url_for("admin_panel"))
        ev = db.execute("SELECT * FROM events WHERE id=?", (fp["event_id"],)).fetchone()
        if ev["status"] == "finished":
            flash("No se puede eliminar jugador de un evento finalizado.","error"); return redirect(url_for("admin_panel"))
        # Devolver cuota al presupuesto (restar)
        db.execute("UPDATE events SET house_budget=house_budget-? WHERE id=?", (fp["entry_paid"], fp["event_id"]))
        db.execute("DELETE FROM field_players WHERE id=?", (fpid,))
    flash("Jugador eliminado.","success"); return redirect(url_for("admin_panel"))

# ── ADMIN: APROBAR / RECHAZAR SOLICITUD DE APUESTA ───────────────────────────

@app.route("/admin/bet_request/<int:brid>/approve", methods=["POST"])
@login_required
@admin_required
def approve_bet_request(brid):
    with get_db() as db:
        br = db.execute("SELECT * FROM bet_requests WHERE id=? AND status='pending'", (brid,)).fetchone()
        if not br: flash("Solicitud no encontrada.","error"); return redirect(url_for("admin_panel"))
        ev = db.execute("SELECT * FROM events WHERE id=?", (br["event_id"],)).fetchone()
        if ev["status"] not in ("open","closed"):
            flash("El evento ya fue finalizado.","error"); return redirect(url_for("admin_panel"))
        odd_row = db.execute("SELECT * FROM event_odds WHERE event_id=? AND option_key=?",
            (br["event_id"], br["option_key"])).fetchone()
        if not odd_row:
            flash("Opcion de apuesta no encontrada.","error"); return redirect(url_for("admin_panel"))

        amount    = br["amount"]
        old_odd   = odd_row["odd"]
        potential = round(amount * old_odd, 2)
        ganancia_neta = round(potential - amount, 2)

        # ── Verificar saldo suficiente ────────────────────────────────────
        jugador = db.execute("SELECT balance FROM users WHERE id=?", (br["user_id"],)).fetchone()
        if jugador["balance"] < amount:
            flash(
                f"Apuesta rechazada: {br['full_name'] if 'full_name' in br.keys() else 'el jugador'} "
                f"no tiene saldo suficiente (saldo: ${jugador['balance']:,.0f}, apuesta: ${amount:,.0f}).",
                "error"
            )
            db.execute("UPDATE bet_requests SET status='rejected' WHERE id=?", (brid,))
            return redirect(url_for("admin_panel"))

        # ── Validación de solvencia de la casa ───────────────────────────
        # Para cada resultado posible R, calculamos cuánto tendría que pagar
        # la casa en neto (ganancias de ganadores - pérdidas de perdedores).
        # La casa solo necesita cubrir la diferencia cuando el pool perdedor
        # no alcanza. Usamos house_budget como colchón.
        #
        # Fórmula por resultado R:
        #   pago_ganadores(R) = sum(amount_i for bets on R)            ← devolver lo apostado
        #                     + losing_pool(R) * (1 - HOUSE_CUT - field_cut_pct)  ← premio
        #   costo_neto_casa(R) = pago_ganadores(R) - losing_pool(R)
        #                      = win_pool(R) - losing_pool(R) * (HOUSE_CUT + field_cut_pct)
        #   Si costo_neto_casa(R) > house_budget → déficit

        house_budget   = ev["house_budget"]
        field_cut_pct  = ev["field_cut_pct"]
        total_cuts     = HOUSE_CUT + field_cut_pct  # fracción del pool perdedor que NO va a ganadores

        all_odds = {o["option_key"]: o for o in db.execute(
            "SELECT * FROM event_odds WHERE event_id=?", (br["event_id"],)
        ).fetchall()}

        # Pool actual confirmado por opción (incluyendo la apuesta nueva)
        pool_por_opcion = {}
        for okey in all_odds:
            existing = db.execute(
                "SELECT COALESCE(SUM(amount),0) as t FROM bets WHERE event_id=? AND option_key=? AND result='pending'",
                (br["event_id"], okey)
            ).fetchone()["t"]
            pool_por_opcion[okey] = existing + (amount if okey == br["option_key"] else 0)

        total_pool = sum(pool_por_opcion.values())

        peor_deficit = 0.0
        peor_label   = None

        for okey, orow in all_odds.items():
            win_pool    = pool_por_opcion[okey]
            losing_pool = total_pool - win_pool
            # Lo que reciben los ganadores = su apuesta de vuelta + su parte del premio
            # Premio disponible = losing_pool * (1 - total_cuts)
            pago_ganadores = win_pool + round(losing_pool * (1 - total_cuts), 2)
            # Costo neto para la casa = lo que sale de la casa al pagar ganadores
            # menos lo que la casa retiene del pool perdedor
            costo_neto = round(pago_ganadores - losing_pool, 2)
            # = win_pool - losing_pool * total_cuts
            deficit = round(costo_neto - house_budget, 2)

            if deficit > peor_deficit:
                peor_deficit = deficit
                peor_label   = orow["label"]

        if peor_deficit > 0.01:
            flash(
                f"Apuesta de ${amount:,.0f} rechazada: si gana '{peor_label}', "
                f"la casa tendría un déficit de ${peor_deficit:,.0f}. "
                f"Presupuesto disponible: ${house_budget:,.0f}.",
                "error"
            )
            return redirect(url_for("admin_panel"))

        # ── Descontar el monto apostado del balance del jugador ──────────
        db.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount, br["user_id"]))

        # Registrar apuesta confirmada
        db.execute("""INSERT INTO bets (user_id,event_id,option_key,option_label,amount,odd_at_bet,potential,result,payout,created_at)
            VALUES (?,?,?,?,?,?,?,'pending',0.0,?)""",
            (br["user_id"], br["event_id"], br["option_key"], br["option_label"], amount, old_odd, potential, now()))
        db.execute("UPDATE bet_requests SET status='approved' WHERE id=?", (brid,))
        db.execute("UPDATE event_odds SET total_bet=total_bet+? WHERE id=?", (amount, odd_row["id"]))
        # Sumar al pool del evento
        db.execute("UPDATE events SET pool=pool+? WHERE id=?", (amount, br["event_id"]))

        # Auto-ajuste de odds
        factor  = amount / 1000.0
        new_odd = max(MIN_ODD, round(old_odd - (old_odd - 1.0) * factor * 0.18, 2))
        db.execute("UPDATE event_odds SET odd=? WHERE id=?", (new_odd, odd_row["id"]))
        for o in db.execute("SELECT * FROM event_odds WHERE event_id=? AND option_key!=?",
            (br["event_id"], br["option_key"])).fetchall():
            boosted = round(min(9.99, o["odd"] + o["odd"] * 0.06 * factor), 2)
            db.execute("UPDATE event_odds SET odd=? WHERE id=?", (boosted, o["id"]))

    flash(f"Apuesta aprobada: ${amount:,.0f} a {old_odd:.2f}x — potencial ${potential:,.0f}.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/bet_request/<int:brid>/reject", methods=["POST"])
@login_required
@admin_required
def reject_bet_request(brid):
    with get_db() as db:
        db.execute("UPDATE bet_requests SET status='rejected' WHERE id=? AND status='pending'", (brid,))
    flash("Solicitud de apuesta rechazada.","info"); return redirect(url_for("admin_panel"))

# ── ADMIN: APROBAR / RECHAZAR PAGOS DE ENTRADA ───────────────────────────────

@app.route("/admin/cash/approve/<int:rid>", methods=["POST"])
@login_required
@admin_required
def approve_cash(rid):
    with get_db() as db:
        req = db.execute("SELECT * FROM cash_requests WHERE id=?", (rid,)).fetchone()
        if not req: flash("Solicitud no encontrada.","error"); return redirect(url_for("admin_panel"))
        t = req["type"]
        if t.startswith("entry_"):
            eid = int(t.split("_")[1])
            db.execute("INSERT OR IGNORE INTO entries (user_id,event_id,paid_at) VALUES (?,?,?)",
                (req["user_id"], eid, now()))
            db.execute("UPDATE events SET house_budget=house_budget+? WHERE id=?", (req["amount"], eid))
            db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
                (eid, req["amount"], f"Cuota entrada apostador ID {req['user_id']}", now()))
        elif t in ("deposit","manual_adjust"):
            db.execute("UPDATE users SET balance=balance+? WHERE id=?", (req["amount"], req["user_id"]))
        db.execute("UPDATE cash_requests SET status='approved', resolved_at=? WHERE id=?", (now(), rid))
    flash("Entrada confirmada. La cuota fue acreditada al presupuesto de la casa.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/cash/reject/<int:rid>", methods=["POST"])
@login_required
@admin_required
def reject_cash(rid):
    with get_db() as db:
        db.execute("UPDATE cash_requests SET status='rejected', resolved_at=? WHERE id=?", (now(),rid))
    flash("Solicitud rechazada.","info"); return redirect(url_for("admin_panel"))

# ── ADMIN: DECLARAR GANADOR Y PAGAR ──────────────────────────────────────────

@app.route("/admin/event/finish/<int:eid>", methods=["POST"])
@login_required
@admin_required
def finish_event(eid):
    winner_key = request.form["winner_key"]
    # winner_key "home" o "away" para cancha; igual para apuestas
    with get_db() as db:
        ev = db.execute("SELECT * FROM events WHERE id=? AND status!='finished'", (eid,)).fetchone()
        if not ev: flash("Evento no valido.","error"); return redirect(url_for("admin_panel"))

        db.execute("UPDATE events SET status='finished', winner_key=? WHERE id=?", (winner_key, eid))

        # ════════════════════════════════════════════════════════════════
        # POOL APUESTAS — apostadores se pagan entre sí
        # ════════════════════════════════════════════════════════════════
        all_bets     = db.execute("SELECT * FROM bets WHERE event_id=? AND result='pending'", (eid,)).fetchall()
        winning_bets = [b for b in all_bets if b["option_key"] == winner_key]
        losing_bets  = [b for b in all_bets if b["option_key"] != winner_key]
        losing_pool  = sum(b["amount"] for b in losing_bets)   # dinero de apostadores que perdieron
        win_pool_sum = sum(b["amount"] for b in winning_bets)  # dinero de apostadores que ganaron

        # ── Distribución del pool perdedor ────────────────────────────────
        # HOUSE_CUT%      → ganancia de la casa
        # field_cut_pct%  → bono para jugadores de cancha ganadores
        # el resto        → premio a repartir entre apostadores ganadores
        #
        # Los apostadores ganadores reciben: su monto de vuelta + su parte del premio
        # (su monto ya fue descontado al aprobar la apuesta, así que el pago neto
        #  para ellos es: monto + premio_proporcional)
        #
        # Si win_pool_sum > losing_pool * (1 - HOUSE_CUT - field_cut_pct),
        # la casa tiene que poner dinero extra del house_budget para cubrir.
        # ─────────────────────────────────────────────────────────────────
        house_share_bets  = round(losing_pool * HOUSE_CUT, 2)
        field_bonus       = round(losing_pool * ev["field_cut_pct"], 2)
        prize_apostadores = round(losing_pool - house_share_bets - field_bonus, 2)

        # Pago total a ganadores = devolver sus apuestas + repartir el premio
        total_pago_ganadores = win_pool_sum + max(0, prize_apostadores)

        # Si el premio es negativo (win_pool > losing_pool neto), la casa cubre la diferencia
        deficit_casa = max(0, round(win_pool_sum - prize_apostadores - losing_pool, 2)) if prize_apostadores < 0 else 0

        # Registrar ganancia/costo de la casa en apuestas
        ganancia_neta_casa = house_share_bets - deficit_casa
        if ganancia_neta_casa != 0:
            db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
                (eid, ganancia_neta_casa,
                 f"Casa: {int(HOUSE_CUT*100)}% pool perdedor ${house_share_bets:,.0f}" +
                 (f" - deficit cubierto ${deficit_casa:,.0f}" if deficit_casa > 0 else ""),
                 now()))
        elif house_share_bets > 0:
            db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
                (eid, house_share_bets, f"Casa {int(HOUSE_CUT*100)}% del pool de apuestas perdedoras", now()))

        # Si hay déficit, descontar del house_budget
        if deficit_casa > 0:
            db.execute("UPDATE events SET house_budget=house_budget-? WHERE id=?", (deficit_casa, eid))

        # Cada apostador ganador recupera su monto + su parte proporcional del premio
        for b in winning_bets:
            share  = (b["amount"] / win_pool_sum) if win_pool_sum > 0 else 0
            payout = round(b["amount"] + max(0, prize_apostadores) * share, 2)
            db.execute("UPDATE bets SET result='won', payout=? WHERE id=?", (payout, b["id"]))
            db.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, b["user_id"]))
        for b in losing_bets:
            db.execute("UPDATE bets SET result='lost' WHERE id=?", (b["id"],))

        # ════════════════════════════════════════════════════════════════
        # POOL CANCHA — completamente separado de las apuestas
        # Solo si el resultado es "home" o "away" hay un equipo ganador
        # en cancha. En caso de EMPATE ("draw") nadie gana en cancha.
        #
        # Equipo ganador de cancha recibe:
        #   - Sus propias cuotas de entrada (recuperan lo que pagaron)
        #   - El 100% de las cuotas del equipo perdedor
        #   - El field_bonus del pool de apuestas (% extra por ganar)
        #
        # En EMPATE: todas las cuotas de cancha + el field_bonus → casa
        # ════════════════════════════════════════════════════════════════
        fp_home = db.execute("SELECT * FROM field_players WHERE event_id=? AND team_key='home'", (eid,)).fetchall()
        fp_away = db.execute("SELECT * FROM field_players WHERE event_id=? AND team_key='away'", (eid,)).fetchall()

        total_home_entry  = sum(p["entry_paid"] for p in fp_home)
        total_away_entry  = sum(p["entry_paid"] for p in fp_away)
        total_field_entry = total_home_entry + total_away_entry

        if winner_key == "draw":
            # EMPATE: ningún equipo de cancha gana. Todo va a la casa.
            extra = total_field_entry + field_bonus
            if extra > 0:
                db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
                    (eid, extra, "Empate: cuotas cancha + bono van a casa", now()))
        else:
            winner_fp = fp_home if winner_key == "home" else fp_away
            loser_fp  = fp_away if winner_key == "home" else fp_home
            total_winner_entry = sum(p["entry_paid"] for p in winner_fp)
            total_loser_entry  = sum(p["entry_paid"] for p in loser_fp)
            n_winners_fp = len(winner_fp)

            if n_winners_fp > 0:
                # Fondo total para repartir entre jugadores ganadores:
                # cuotas propias + cuotas del equipo perdedor + bono de apuestas
                fondo_cancha = total_winner_entry + total_loser_entry + field_bonus
                per_player   = round(fondo_cancha / n_winners_fp, 2)
                for p in winner_fp:
                    db.execute("UPDATE field_players SET payout=? WHERE id=?", (per_player, p["id"]))
            else:
                # Sin jugadores ganadores de cancha: las cuotas perdedoras y el bono van a la casa
                extra = total_loser_entry + field_bonus
                if extra > 0:
                    db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
                        (eid, extra, "Sin jugadores cancha ganadores — cuotas y bono van a casa", now()))

        # Cancelar solicitudes de apuesta pendientes que no se procesaron
        db.execute("UPDATE bet_requests SET status='cancelled' WHERE event_id=? AND status='pending'", (eid,))

    flash(f"Evento finalizado. {len(winning_bets)} apostadores ganadores pagados.","success")
    return redirect(url_for("admin_panel"))

# ── ADMIN: VER JUGADOR ─────────────────────────────────────────────────────────

@app.route("/admin/player/<int:uid>")
@login_required
@admin_required
def view_player(uid):
    with get_db() as db:
        user    = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        bets    = db.execute("""SELECT b.*,e.home,e.away,e.sport,e.league,e.status,e.winner_key
            FROM bets b JOIN events e ON b.event_id=e.id WHERE b.user_id=? ORDER BY b.created_at DESC""",
            (uid,)).fetchall()
        entries = db.execute("""SELECT en.*,e.home,e.away,e.sport,e.entry_fee
            FROM entries en JOIN events e ON en.event_id=e.id WHERE en.user_id=?""", (uid,)).fetchall()
        reqs    = db.execute("SELECT * FROM cash_requests WHERE user_id=? ORDER BY created_at DESC", (uid,)).fetchall()
        bet_reqs = db.execute("""SELECT br.*,e.home,e.away
            FROM bet_requests br JOIN events e ON br.event_id=e.id
            WHERE br.user_id=? ORDER BY br.created_at DESC""", (uid,)).fetchall()
        entry_event_ids = {e["event_id"] for e in entries}
        all_active = db.execute(
            "SELECT * FROM events WHERE status IN ('open','closed') ORDER BY created_at DESC"
        ).fetchall()
        available_events = [ev for ev in all_active if ev["id"] not in entry_event_ids]

        class Stats:
            pass
        stats = Stats()
        stats.total_bet = sum(b["amount"] for b in bets)
        stats.total_won = sum(b["payout"] for b in bets if b["result"]=="won")
        stats.bets_won  = sum(1 for b in bets if b["result"]=="won")
        stats.bets_lost = sum(1 for b in bets if b["result"]=="lost")

    return render_template("player_profile.html", user=user, bets=bets, entries=entries,
        reqs=reqs, bet_reqs=bet_reqs, stats=stats, available_events=available_events)

@app.route("/admin/player/<int:uid>/adjust", methods=["POST"])
@login_required
@admin_required
def adjust_balance(uid):
    try: amount = float(request.form["amount"])
    except: flash("Monto invalido.","error"); return redirect(url_for("view_player",uid=uid))
    note = request.form.get("note","").strip()
    with get_db() as db:
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
        db.execute("""INSERT INTO cash_requests (user_id,type,amount,status,note,resolved_at,created_at)
            VALUES (?,'manual_adjust',?,'approved',?,?,?)""", (uid,amount,note,now(),now()))
    flash(f"Saldo ajustado ${amount:,.0f}.","success"); return redirect(url_for("view_player",uid=uid))

# ── ADMIN: AJUSTE MANUAL DE PRESUPUESTO DE LA CASA ───────────────────────────

@app.route("/admin/event/<int:eid>/house_budget/adjust", methods=["POST"])
@login_required
@admin_required
def adjust_house_budget(eid):
    try: amount = float(request.form["amount"])
    except: flash("Monto invalido.","error"); return redirect(url_for("admin_panel"))
    note = request.form.get("note", "Ajuste manual").strip()
    with get_db() as db:
        ev = db.execute("SELECT * FROM events WHERE id=? AND status!='finished'", (eid,)).fetchone()
        if not ev:
            flash("Evento no valido o ya finalizado.","error"); return redirect(url_for("admin_panel"))
        db.execute("UPDATE events SET house_budget=MAX(0, house_budget+?) WHERE id=?", (amount, eid))
        db.execute("INSERT INTO house_log (event_id,amount,note,created_at) VALUES (?,?,?,?)",
            (eid, amount, note, now()))
    flash(f"Presupuesto de la casa ajustado ${amount:+,.0f} para el evento.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/event/<int:eid>/entry_fee/update", methods=["POST"])
@login_required
@admin_required
def update_entry_fee(eid):
    try: fee = float(request.form["entry_fee"])
    except: flash("Monto invalido.","error"); return redirect(url_for("admin_panel"))
    if fee < 0: flash("La cuota no puede ser negativa.","error"); return redirect(url_for("admin_panel"))
    with get_db() as db:
        ev = db.execute("SELECT * FROM events WHERE id=? AND status!='finished'", (eid,)).fetchone()
        if not ev:
            flash("Evento no valido o ya finalizado.","error"); return redirect(url_for("admin_panel"))
        db.execute("UPDATE events SET entry_fee=? WHERE id=?", (fee, eid))
    flash(f"Cuota de entrada actualizada a ${fee:,.0f}.","success")
    return redirect(url_for("admin_panel"))

with app.app_context():
    init_db()
    # Migración: agregar odd_at_request a bet_requests si no existe
    with get_db() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(bet_requests)").fetchall()]
        if "odd_at_request" not in cols:
            db.execute("ALTER TABLE bet_requests ADD COLUMN odd_at_request REAL NOT NULL DEFAULT 0.0")

if __name__ == "__main__":
    app.run(debug=True, port=5000)
