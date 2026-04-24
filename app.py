"""
APUSM — Plataforma de apuestas deportivas
Flask + PostgreSQL | Render-ready | Pagos en efectivo
"""
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_socketio import SocketIO, emit
from functools import wraps
import psycopg2, psycopg2.extras, secrets, hashlib, os
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "apusm-secret-cambia-esto-2024")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

HOUSE_CUT = 0.08
FIELD_CUT = 0.07
MIN_ODD   = 1.01

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def hp(pw): return hashlib.sha256(pw.encode()).hexdigest()
def round50(x):
    return int(x // 50) * 50

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def q(conn, sql, params=()):
    """Ejecuta una query y retorna el cursor."""
    # Convierte ? → %s (SQLite → PostgreSQL)
    sql_pg = sql.replace("?", "%s")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql_pg, params)
    return cur

def fetchone(conn, sql, params=()):
    cur = q(conn, sql, params)
    return cur.fetchone()

def fetchall(conn, sql, params=()):
    cur = q(conn, sql, params)
    return cur.fetchall()

def execute(conn, sql, params=()):
    q(conn, sql, params)

def lastrowid(conn, sql, params=()):
    """INSERT ... RETURNING id"""
    sql_pg = sql.replace("?", "%s")
    # Agrega RETURNING id si no lo tiene
    if "RETURNING" not in sql_pg.upper():
        sql_pg = sql_pg.rstrip(";") + " RETURNING id"
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql_pg, params)
    row = cur.fetchone()
    return row["id"] if row else None

def init_db():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
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
            id SERIAL PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_by TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
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
            odds_mode TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_odds (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL,
            option_key TEXT NOT NULL,
            label TEXT NOT NULL,
            odd REAL NOT NULL,
            total_bet REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE IF NOT EXISTS entries (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            paid_at TEXT NOT NULL,
            UNIQUE(user_id, event_id)
        );
        CREATE TABLE IF NOT EXISTS bets (
            id SERIAL PRIMARY KEY,
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
            id SERIAL PRIMARY KEY,
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
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT DEFAULT '',
            resolved_at TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS house_log (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL DEFAULT 'income',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS field_players (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            team_key TEXT NOT NULL,
            entry_paid REAL NOT NULL DEFAULT 0.0,
            payout REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );
        """)
        # Admin por defecto
        cur.execute("SELECT id FROM users WHERE role='admin'")
        if not cur.fetchone():
            cur.execute("""INSERT INTO users (username,full_name,phone,email,password_hash,role,balance,created_at)
                VALUES (%s,%s,%s,%s,%s,'admin',0.0,%s)""",
                ('ElNegroA','Administrador','000000000','admin@apusm.com',hp("MatiasGOPE1324.m@"), now()))
        conn.commit()
    finally:
        conn.close()

def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if "user_id" not in session: return redirect(url_for("login"))
        return f(*a, **kw)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if session.get("role") != "admin":
            flash("Acceso restringido.", "error")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return d

def recalc_auto_odds(conn, eid):
    ev   = fetchone(conn, "SELECT * FROM events WHERE id=?", (eid,))
    odds = fetchall(conn, "SELECT * FROM event_odds WHERE event_id=?", (eid,))
    total_pool = sum(o["total_bet"] for o in odds)
    cuts = HOUSE_CUT + ev["field_cut_pct"]
    for o in odds:
        if o["total_bet"] > 0 and total_pool > 0:
            losing  = total_pool - o["total_bet"]
            new_odd = round(1 + losing * (1 - cuts) / o["total_bet"], 2)
            new_odd = max(MIN_ODD, min(99.0, new_odd))
        else:
            new_odd = o["odd"]
        execute(conn, "UPDATE event_odds SET odd=? WHERE id=?", (new_odd, o["id"]))

# ── TIEMPO REAL ───────────────────────────────────────────────────────────────

def emit_update(event_type, data=None):
    """Emite un evento a todos los clientes conectados."""
    socketio.emit("update", {"type": event_type, "data": data or {}})

@app.route("/healthz")
def healthz():
    return "ok", 200

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route("/terms")
def terms():
    from datetime import date
    return render_template("terms.html", now_date=date.today().strftime("%d/%m/%Y"))

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        conn = get_db()
        try:
            user = fetchone(conn, "SELECT * FROM users WHERE username=? AND password_hash=?", (u, hp(p)))
        finally:
            conn.close()
        if user:
            session.update(user_id=user["id"], username=user["username"], role=user["role"])
            return redirect(url_for("admin_panel") if user["role"] == "admin" else url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        token_str = request.form["token"].strip()
        username  = request.form["username"].strip()
        full_name = request.form["full_name"].strip()
        phone     = request.form["phone"].strip()
        email     = request.form.get("email", "").strip()
        password  = request.form["password"]
        conn = get_db()
        try:
            tok = fetchone(conn, "SELECT * FROM tokens WHERE token=? AND used=0", (token_str,))
            if not tok:
                flash("Token invalido o ya usado.", "error"); return render_template("register.html")
            if fetchone(conn, "SELECT id FROM users WHERE username=?", (username,)):
                flash("Usuario ya existe.", "error"); return render_template("register.html")
            execute(conn, """INSERT INTO users (username,full_name,phone,email,password_hash,role,balance,created_at)
                VALUES (?,?,?,?,?,'player',0.0,?)""", (username, full_name, phone, email, hp(password), now()))
            execute(conn, "UPDATE tokens SET used=1, used_by=? WHERE token=?", (username, token_str))
            conn.commit()
        finally:
            conn.close()
        flash("Cuenta creada. Inicia sesion.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# ── PERFIL JUGADOR ────────────────────────────────────────────────────────────

@app.route("/profile")
@login_required
def profile():
    conn = get_db()
    try:
        user = fetchone(conn, "SELECT * FROM users WHERE id=?", (session["user_id"],))
        bets = fetchall(conn, """SELECT b.*,e.home,e.away,e.sport,e.league,e.status,e.winner_key
            FROM bets b JOIN events e ON b.event_id=e.id
            WHERE b.user_id=? ORDER BY b.created_at DESC""", (session["user_id"],))
        bet_reqs = fetchall(conn, """SELECT br.*,e.home,e.away,e.league,e.status as event_status
            FROM bet_requests br JOIN events e ON br.event_id=e.id
            WHERE br.user_id=? ORDER BY br.created_at DESC""", (session["user_id"],))
        stats = {
            "total_bet": sum(b["amount"] for b in bets),
            "total_won": sum(b["payout"] for b in bets if b["result"] == "won"),
            "bets_won":  sum(1 for b in bets if b["result"] == "won"),
            "bets_lost": sum(1 for b in bets if b["result"] == "lost"),
        }
    finally:
        conn.close()
    return render_template("profile.html", user=user, bets=bets, bet_reqs=bet_reqs, stats=stats)

# ── DASHBOARD JUGADOR ─────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    if session["role"] == "admin": return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        user   = fetchone(conn, "SELECT * FROM users WHERE id=?", (session["user_id"],))
        events = fetchall(conn, "SELECT * FROM events WHERE status IN ('open','closed') ORDER BY created_at DESC")
        edata  = []
        for ev in events:
            odds = fetchall(conn, "SELECT * FROM event_odds WHERE event_id=?", (ev["id"],))
            has_entry    = fetchone(conn, "SELECT id FROM entries WHERE user_id=? AND event_id=?", (session["user_id"], ev["id"]))
            pending_entry= fetchone(conn, "SELECT id FROM cash_requests WHERE user_id=? AND type=? AND status='pending'", (session["user_id"], f"entry_{ev['id']}"))
            my_bet_reqs  = fetchall(conn, "SELECT * FROM bet_requests WHERE user_id=? AND event_id=? ORDER BY created_at DESC", (session["user_id"], ev["id"]))
            edata.append({
                "event": ev, "odds": odds,
                "has_entry": bool(has_entry),
                "pending_entry": bool(pending_entry),
                "my_bet_reqs": my_bet_reqs,
                "bets_open": ev["status"] == "open"
            })
        my_bets = fetchall(conn, """SELECT b.*,e.home,e.away,e.league,e.sport,e.status,e.winner_key
            FROM bets b JOIN events e ON b.event_id=e.id
            WHERE b.user_id=? ORDER BY b.created_at DESC LIMIT 20""", (session["user_id"],))
    finally:
        conn.close()
    return render_template("dashboard.html", user=user, edata=edata, my_bets=my_bets)

# ── SOLICITAR ENTRADA ─────────────────────────────────────────────────────────

@app.route("/event/<int:eid>/request_entry", methods=["POST"])
@login_required
def request_entry(eid):
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=? AND status IN ('open','closed')", (eid,))
        if not ev: flash("Evento no disponible.", "error"); return redirect(url_for("dashboard"))
        if fetchone(conn, "SELECT id FROM entries WHERE user_id=? AND event_id=?", (session["user_id"], eid)):
            flash("Ya tienes entrada a este evento.", "info"); return redirect(url_for("dashboard"))
        if fetchone(conn, "SELECT id FROM cash_requests WHERE user_id=? AND type=? AND status='pending'", (session["user_id"], f"entry_{eid}")):
            flash("Ya enviaste una solicitud. Espera confirmacion.", "info"); return redirect(url_for("dashboard"))
        execute(conn, "INSERT INTO cash_requests (user_id,type,amount,status,note,created_at) VALUES (?,?,?,'pending',?,?)",
            (session["user_id"], f"entry_{eid}", ev["entry_fee"], f"Entrada: {ev['home']} vs {ev['away']}", now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"Solicitud enviada. Paga ${ev['entry_fee']:,.0f} en efectivo al admin.", "info")
    emit_update("new_entry_request")
    return redirect(url_for("dashboard"))

# ── SOLICITAR APUESTA ─────────────────────────────────────────────────────────

@app.route("/event/<int:eid>/request_bet", methods=["POST"])
@login_required
def request_bet(eid):
    option_key = request.form.get("option_key", "")
    try: amount = float(request.form.get("amount", 0))
    except: flash("Monto invalido.", "error"); return redirect(url_for("dashboard"))
    if amount <= 0: flash("Monto debe ser > 0.", "error"); return redirect(url_for("dashboard"))
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=? AND status='open'", (eid,))
        if not ev: flash("Las apuestas estan cerradas.", "error"); return redirect(url_for("dashboard"))
        if not fetchone(conn, "SELECT id FROM entries WHERE user_id=? AND event_id=?", (session["user_id"], eid)):
            flash("Necesitas entrada confirmada para apostar.", "error"); return redirect(url_for("dashboard"))
        odd_row = fetchone(conn, "SELECT * FROM event_odds WHERE event_id=? AND option_key=?", (eid, option_key))
        if not odd_row or odd_row["odd"] <= MIN_ODD:
            flash("Opcion no disponible.", "error"); return redirect(url_for("dashboard"))
        if fetchone(conn, "SELECT id FROM bet_requests WHERE user_id=? AND event_id=? AND option_key=? AND amount=? AND status='pending'",
                (session["user_id"], eid, option_key, amount)):
            flash("Ya tienes una solicitud identica pendiente.", "info"); return redirect(url_for("dashboard"))
        execute(conn, """INSERT INTO bet_requests (user_id,event_id,option_key,option_label,amount,odd_at_request,status,created_at)
            VALUES (?,?,?,?,?,?,'pending',?)""",
            (session["user_id"], eid, option_key, odd_row["label"], amount, odd_row["odd"], now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"Solicitud enviada: ${amount:,.0f} a '{odd_row['label']}' @ {odd_row['odd']:.2f}x.", "info")
    emit_update("new_bet_request")
    return redirect(url_for("dashboard"))

# ── CANCELAR SOLICITUD DE APUESTA ─────────────────────────────────────────────

@app.route("/bet_request/<int:brid>/cancel", methods=["POST"])
@login_required
def cancel_bet_request(brid):
    conn = get_db()
    try:
        br = fetchone(conn, "SELECT * FROM bet_requests WHERE id=? AND user_id=? AND status='pending'", (brid, session["user_id"]))
        if not br: flash("Solicitud no encontrada.", "error"); return redirect(url_for("dashboard"))
        ev = fetchone(conn, "SELECT * FROM events WHERE id=?", (br["event_id"],))
        if ev["status"] != "open": flash("No puedes cancelar con apuestas cerradas.", "error"); return redirect(url_for("dashboard"))
        execute(conn, "UPDATE bet_requests SET status='cancelled' WHERE id=?", (brid,))
        conn.commit()
    finally:
        conn.close()
    flash("Solicitud cancelada.", "success")
    return redirect(url_for("dashboard"))

# ── ADMIN PANEL ────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    conn = get_db()
    try:
        tokens   = fetchall(conn, "SELECT * FROM tokens ORDER BY created_at DESC")
        players  = fetchall(conn, "SELECT * FROM users WHERE role='player' ORDER BY created_at DESC")
        all_users= fetchall(conn, "SELECT * FROM users ORDER BY role DESC, created_at DESC")
        pending_entries = fetchall(conn, """SELECT cr.*,u.username,u.full_name,u.phone
            FROM cash_requests cr JOIN users u ON cr.user_id=u.id
            WHERE cr.status='pending' ORDER BY cr.created_at""")
        raw_bets = fetchall(conn, """SELECT br.*,u.username,u.full_name,u.phone,
            e.home,e.away,e.league,e.sport
            FROM bet_requests br JOIN users u ON br.user_id=u.id
            JOIN events e ON br.event_id=e.id
            WHERE br.status='pending' ORDER BY br.event_id, br.created_at""")
        pending_bets = []
        for br in raw_bets:
            odd_row = fetchone(conn, "SELECT odd FROM event_odds WHERE event_id=? AND option_key=?", (br["event_id"], br["option_key"]))
            current_odd = odd_row["odd"] if odd_row else 1.0
            locked_odd  = br["odd_at_request"] if br["odd_at_request"] > 1.0 else current_odd
            pending_bets.append({**dict(br), "current_odd": current_odd, "locked_odd": locked_odd,
                "potential": round(br["amount"] * locked_odd, 2), "aprobable": True})
        edata = []
        for ev in fetchall(conn, "SELECT * FROM events ORDER BY created_at DESC"):
            odds    = fetchall(conn, "SELECT * FROM event_odds WHERE event_id=?", (ev["id"],))
            count   = fetchone(conn, "SELECT COUNT(*) as c FROM entries WHERE event_id=?", (ev["id"],))["c"]
            fp_home = fetchall(conn, "SELECT * FROM field_players WHERE event_id=? AND team_key='home'", (ev["id"],))
            fp_away = fetchall(conn, "SELECT * FROM field_players WHERE event_id=? AND team_key='away'", (ev["id"],))
            pcount  = fetchone(conn, "SELECT COUNT(*) as c FROM bet_requests WHERE event_id=? AND status='pending'", (ev["id"],))["c"]
            edata.append({"event": ev, "odds": odds, "count": count,
                "fp_home": fp_home, "fp_away": fp_away,
                "total_field_home": sum(p["entry_paid"] for p in fp_home),
                "total_field_away": sum(p["entry_paid"] for p in fp_away),
                "pending_bets_count": pcount})
        house_total = fetchone(conn, "SELECT COALESCE(SUM(amount),0) as t FROM house_log WHERE type='profit'")["t"]
    finally:
        conn.close()
    return render_template("admin.html", tokens=tokens, players=players, all_users=all_users,
        pending_entries=pending_entries, pending_bets=pending_bets, edata=edata, house_total=house_total)

# ── TOKENS ─────────────────────────────────────────────────────────────────────

@app.route("/admin/token/create", methods=["POST"])
@login_required
@admin_required
def create_token():
    note = request.form.get("note", "").strip()
    tok  = secrets.token_urlsafe(10)
    conn = get_db()
    try:
        execute(conn, "INSERT INTO tokens (token,used,note,created_at) VALUES (?,0,?,?)", (tok, note, now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"Token creado: {tok}", "success"); return redirect(url_for("admin_panel"))

@app.route("/admin/token/delete/<int:tid>", methods=["POST"])
@login_required
@admin_required
def delete_token(tid):
    conn = get_db()
    try:
        execute(conn, "DELETE FROM tokens WHERE id=? AND used=0", (tid,))
        conn.commit()
    finally:
        conn.close()
    flash("Token eliminado.", "success"); return redirect(url_for("admin_panel"))

# ── CREAR USUARIO DIRECTO ─────────────────────────────────────────────────────

@app.route("/admin/user/create", methods=["POST"])
@login_required
@admin_required
def create_user():
    username  = request.form.get("username","").strip()
    full_name = request.form.get("full_name","").strip()
    phone     = request.form.get("phone","").strip()
    email     = request.form.get("email","").strip()
    password  = request.form.get("password","")
    role      = request.form.get("role","player")
    if role not in ("player","admin"): role = "player"
    if not username or not full_name or not phone or not password:
        flash("Completa todos los campos obligatorios.","error"); return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        if fetchone(conn, "SELECT id FROM users WHERE username=?", (username,)):
            flash(f"El usuario '{username}' ya existe.","error"); return redirect(url_for("admin_panel"))
        execute(conn, """INSERT INTO users (username,full_name,phone,email,password_hash,role,balance,created_at)
            VALUES (?,?,?,?,?,?,0.0,?)""", (username, full_name, phone, email, hp(password), role, now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"Usuario '{username}' creado.","success"); return redirect(url_for("admin_panel"))

@app.route("/admin/user/delete/<int:uid>", methods=["POST"])
@login_required
@admin_required
def delete_user(uid):
    if uid == session["user_id"]: flash("No puedes eliminarte a ti mismo.","error"); return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        execute(conn, "DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    flash("Usuario eliminado.","success"); return redirect(url_for("admin_panel"))

@app.route("/admin/user/toggle_role/<int:uid>", methods=["POST"])
@login_required
@admin_required
def toggle_role(uid):
    if uid == session["user_id"]: flash("No puedes cambiar tu propio rol.","error"); return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        user = fetchone(conn, "SELECT role FROM users WHERE id=?", (uid,))
        if not user: flash("Usuario no encontrado.","error"); return redirect(url_for("admin_panel"))
        new_role = "player" if user["role"] == "admin" else "admin"
        execute(conn, "UPDATE users SET role=? WHERE id=?", (new_role, uid))
        conn.commit()
    finally:
        conn.close()
    flash(f"Rol actualizado a {'Admin' if new_role=='admin' else 'Jugador'}.","success"); return redirect(url_for("admin_panel"))

# ── CREAR EVENTO ───────────────────────────────────────────────────────────────

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
    odds_mode      = f.get("odds_mode", "manual")
    try:
        pct_raw = float(f.get("field_cut_pct", FIELD_CUT * 100))
        field_cut_pct = round(pct_raw / 100.0, 4) if pct_raw > 1 else pct_raw
        field_cut_pct = max(0.0, min(0.50, field_cut_pct))
    except:
        field_cut_pct = FIELD_CUT
    if sport == "futbol":
        odd_home = float(f.get("odd_home", 2.20))
        odd_draw = float(f.get("odd_draw", 3.20))
        odd_away = float(f.get("odd_away", 2.80))
    else:
        odd_home = float(f.get("odd_home", 1.90))
        odd_away = float(f.get("odd_away", 2.00))
        odd_draw = None
    if odds_mode == "auto":
        odd_home = 2.00; odd_draw = 3.00 if sport == "futbol" else None; odd_away = 2.00

    conn = get_db()
    try:
        eid = lastrowid(conn, """INSERT INTO events
            (sport,home,away,league,entry_fee,house_budget,pool,status,field_cut_pct,odds_mode,created_at)
            VALUES (?,?,?,?,?,?,0,'open',?,?,?)""",
            (sport, home, away, league, entry_fee, initial_budget, field_cut_pct, odds_mode, now()))
        if initial_budget > 0:
            execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                (eid, initial_budget, "income", "Presupuesto inicial de la casa", now()))
        options = [("home","Local",odd_home),("draw","Empate",odd_draw),("away","Visitante",odd_away)] if sport=="futbol" \
              else [("home","Local",odd_home),("away","Visitante",odd_away)]
        for key, label, odd in options:
            execute(conn, "INSERT INTO event_odds (event_id,option_key,label,odd,total_bet) VALUES (?,?,?,?,0)",
                (eid, key, label, odd))
        conn.commit()
    finally:
        conn.close()
    flash(f"Evento '{home} vs {away}' creado.", "success"); return redirect(url_for("admin_panel"))

@app.route("/admin/event/delete/<int:eid>", methods=["POST"])
@login_required
@admin_required
def delete_event(eid):
    conn = get_db()
    try:
        for t in ["event_odds","entries","bets","bet_requests","field_players","house_log"]:
            execute(conn, f"DELETE FROM {t} WHERE event_id=?", (eid,))
        execute(conn, "DELETE FROM events WHERE id=?", (eid,))
        conn.commit()
    finally:
        conn.close()
    flash("Evento eliminado.", "success"); return redirect(url_for("admin_panel"))

@app.route("/admin/event/close/<int:eid>", methods=["POST"])
@login_required
@admin_required
def close_event(eid):
    conn = get_db()
    try:
        execute(conn, "UPDATE events SET status='closed' WHERE id=?", (eid,))
        execute(conn, "UPDATE bet_requests SET status='cancelled' WHERE event_id=? AND status='pending'", (eid,))
        conn.commit()
    finally:
        conn.close()
    flash("Evento cerrado.", "success")
    emit_update("event_updated")
    return redirect(url_for("admin_panel"))

@app.route("/admin/event/reopen/<int:eid>", methods=["POST"])
@login_required
@admin_required
def reopen_event(eid):
    conn = get_db()
    try:
        execute(conn, "UPDATE events SET status='open' WHERE id=? AND status='closed'", (eid,))
        conn.commit()
    finally:
        conn.close()
    flash("Evento reabierto.", "success")
    emit_update("event_updated")
    return redirect(url_for("admin_panel"))

# ── AJUSTE MANUAL DE ODDS ──────────────────────────────────────────────────────

@app.route("/admin/event/<int:eid>/odds/adjust", methods=["POST"])
@login_required
@admin_required
def adjust_odds(eid):
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=? AND status!='finished'", (eid,))
        if not ev: flash("Evento no valido.", "error"); return redirect(url_for("admin_panel"))
        new_pct = request.form.get("field_cut_pct", "").strip()
        if new_pct:
            try:
                pct_raw = float(new_pct)
                pct = round(pct_raw / 100.0, 4) if pct_raw > 1 else round(pct_raw, 4)
                execute(conn, "UPDATE events SET field_cut_pct=? WHERE id=?", (max(0.0, min(0.50, pct)), eid))
            except ValueError:
                flash("Porcentaje invalido.", "error"); return redirect(url_for("admin_panel"))
        new_mode = request.form.get("odds_mode", "").strip()
        if new_mode in ("manual","auto"):
            execute(conn, "UPDATE events SET odds_mode=? WHERE id=?", (new_mode, eid))
        for o in fetchall(conn, "SELECT * FROM event_odds WHERE event_id=?", (eid,)):
            val = request.form.get(f"odd_{o['option_key']}", "").strip()
            if val:
                try:
                    new_odd = round(float(val), 2)
                    if new_odd >= MIN_ODD:
                        execute(conn, "UPDATE event_odds SET odd=? WHERE id=?", (new_odd, o["id"]))
                except ValueError:
                    pass
        conn.commit()
    finally:
        conn.close()
    flash("Configuracion actualizada.", "success"); return redirect(url_for("admin_panel"))

# ── JUGADORES DE CANCHA ────────────────────────────────────────────────────────

@app.route("/admin/event/<int:eid>/field_player/add", methods=["POST"])
@login_required
@admin_required
def add_field_player(eid):
    name       = request.form.get("name", "").strip()
    team_key   = request.form.get("team_key", "")
    entry_paid = float(request.form.get("entry_paid", 0))
    if not name or team_key not in ("home","away"):
        flash("Datos invalidos.", "error"); return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=?", (eid,))
        if not ev: flash("Evento no encontrado.", "error"); return redirect(url_for("admin_panel"))
        execute(conn, "INSERT INTO field_players (event_id,name,team_key,entry_paid,payout,created_at) VALUES (?,?,?,?,0,?)",
            (eid, name, team_key, entry_paid, now()))
        execute(conn, "UPDATE events SET house_budget=house_budget+? WHERE id=?", (entry_paid, eid))
        execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
            (eid, entry_paid, "cancha", f"Cuota cancha: {name} ({team_key})", now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"Jugador '{name}' agregado.", "success"); return redirect(url_for("admin_panel"))

@app.route("/admin/event/<int:eid>/field_players/bulk", methods=["POST"])
@login_required
@admin_required
def add_field_players_bulk(eid):
    team_key = request.form.get("team_key", "")
    if team_key not in ("home","away"): flash("Equipo invalido.", "error"); return redirect(url_for("admin_panel"))
    try: count = int(request.form.get("count", 0))
    except: flash("Cantidad invalida.", "error"); return redirect(url_for("admin_panel"))
    if count < 1 or count > 30: flash("Entre 1 y 30 jugadores.", "error"); return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=?", (eid,))
        if not ev: flash("Evento no encontrado.", "error"); return redirect(url_for("admin_panel"))
        added = 0; total_added = 0.0
        for i in range(1, count + 1):
            name = request.form.get(f"name_{i}", "").strip()
            try: fee = float(request.form.get(f"fee_{i}", 0))
            except: fee = 0.0
            if not name: continue
            execute(conn, "INSERT INTO field_players (event_id,name,team_key,entry_paid,payout,created_at) VALUES (?,?,?,?,0,?)",
                (eid, name, team_key, fee, now()))
            total_added += fee; added += 1
        if total_added > 0:
            execute(conn, "UPDATE events SET house_budget=house_budget+? WHERE id=?", (total_added, eid))
            execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                (eid, total_added, "cancha", f"{added} jugadores {team_key} en bloque — ${total_added:,.0f}", now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"{added} jugadores agregados. Total: ${total_added:,.0f}.", "success"); return redirect(url_for("admin_panel"))

@app.route("/admin/field_player/<int:fpid>/delete", methods=["POST"])
@login_required
@admin_required
def delete_field_player(fpid):
    conn = get_db()
    try:
        fp = fetchone(conn, "SELECT * FROM field_players WHERE id=?", (fpid,))
        if not fp: flash("Jugador no encontrado.", "error"); return redirect(url_for("admin_panel"))
        ev = fetchone(conn, "SELECT * FROM events WHERE id=?", (fp["event_id"],))
        if ev["status"] == "finished": flash("No se puede eliminar de evento finalizado.", "error"); return redirect(url_for("admin_panel"))
        execute(conn, "UPDATE events SET house_budget=house_budget-? WHERE id=?", (fp["entry_paid"], fp["event_id"]))
        execute(conn, "DELETE FROM field_players WHERE id=?", (fpid,))
        conn.commit()
    finally:
        conn.close()
    flash("Jugador eliminado.", "success"); return redirect(url_for("admin_panel"))

# ── APROBAR UNA APUESTA ────────────────────────────────────────────────────────

def _do_approve_bet(conn, brid):
    br = fetchone(conn, "SELECT * FROM bet_requests WHERE id=? AND status='pending'", (brid,))
    if not br: return False, "Solicitud no encontrada."
    ev = fetchone(conn, "SELECT * FROM events WHERE id=?", (br["event_id"],))
    if ev["status"] not in ("open","closed"): return False, "El evento ya fue finalizado."
    odd_row = fetchone(conn, "SELECT * FROM event_odds WHERE event_id=? AND option_key=?", (br["event_id"], br["option_key"]))
    if not odd_row: return False, "Opcion no encontrada."

    amount     = br["amount"]
    locked_odd = br["odd_at_request"] if br["odd_at_request"] > 1.0 else odd_row["odd"]
    potential  = round(amount * locked_odd, 2)

    house_budget  = ev["house_budget"]
    field_cut_pct = ev["field_cut_pct"]
    all_odds = {o["option_key"]: o for o in fetchall(conn, "SELECT * FROM event_odds WHERE event_id=?", (br["event_id"],))}

    ganancias_por_opcion = {}
    pool_por_opcion = {}
    for okey in all_odds:
        ganancias_por_opcion[okey] = fetchone(conn,
            "SELECT COALESCE(SUM(potential-amount),0) as t FROM bets WHERE event_id=? AND option_key=? AND result='pending'",
            (br["event_id"], okey))["t"]
        pool_por_opcion[okey] = fetchone(conn,
            "SELECT COALESCE(SUM(amount),0) as t FROM bets WHERE event_id=? AND option_key=? AND result='pending'",
            (br["event_id"], okey))["t"]

    okey_new = br["option_key"]
    ganancias_por_opcion[okey_new] = ganancias_por_opcion.get(okey_new, 0) + round(potential - amount, 2)
    pool_por_opcion[okey_new]       = pool_por_opcion.get(okey_new, 0) + amount

    peor_deficit = 0.0; peor_label = None
    for okey, orow in all_odds.items():
        pool_perdedor = sum(v for k,v in pool_por_opcion.items() if k != okey)
        pool_neto     = round(pool_perdedor * (1 - field_cut_pct), 2)
        ganancias_a_pagar = ganancias_por_opcion[okey]
        deficit = round(ganancias_a_pagar - round(pool_neto + house_budget, 2), 2)
        if deficit > peor_deficit:
            peor_deficit = deficit; peor_label = orow["label"]

    if peor_deficit > 0.01:
        return False, (f"Apuesta ${amount:,.0f} rechazada: si gana '{peor_label}', "
                       f"déficit ${peor_deficit:,.0f}. Agrega más presupuesto a la casa.")

    execute(conn, """INSERT INTO bets
        (user_id,event_id,option_key,option_label,amount,odd_at_bet,potential,result,payout,created_at)
        VALUES (?,?,?,?,?,?,?,'pending',0.0,?)""",
        (br["user_id"], br["event_id"], br["option_key"], br["option_label"], amount, locked_odd, potential, now()))
    execute(conn, "UPDATE bet_requests SET status='approved' WHERE id=?", (brid,))
    execute(conn, "UPDATE event_odds SET total_bet=total_bet+? WHERE id=?", (amount, odd_row["id"]))
    execute(conn, "UPDATE events SET pool=pool+? WHERE id=?", (amount, br["event_id"]))

    if ev["odds_mode"] == "auto":
        recalc_auto_odds(conn, br["event_id"])
    else:
        factor  = amount / 1000.0
        new_odd = max(MIN_ODD, round(odd_row["odd"] - (odd_row["odd"] - 1.0) * factor * 0.18, 2))
        execute(conn, "UPDATE event_odds SET odd=? WHERE id=?", (new_odd, odd_row["id"]))
        for o in fetchall(conn, "SELECT * FROM event_odds WHERE event_id=? AND option_key!=?", (br["event_id"], br["option_key"])):
            execute(conn, "UPDATE event_odds SET odd=? WHERE id=?", (round(min(9.99, o["odd"] + o["odd"] * 0.06 * factor), 2), o["id"]))

    return True, f"Apuesta aprobada: ${amount:,.0f} a {locked_odd:.2f}x — potencial ${potential:,.0f}."

@app.route("/admin/bet_request/<int:brid>/approve", methods=["POST"])
@login_required
@admin_required
def approve_bet_request(brid):
    conn = get_db()
    try:
        ok, msg = _do_approve_bet(conn, brid)
        if ok: conn.commit()
    finally:
        conn.close()
    if ok: emit_update("bet_approved")
    flash(msg, "success" if ok else "error"); return redirect(url_for("admin_panel"))

@app.route("/admin/event/<int:eid>/approve_all_bets", methods=["POST"])
@login_required
@admin_required
def approve_all_bets(eid):
    conn = get_db()
    try:
        pending = fetchall(conn, "SELECT id FROM bet_requests WHERE event_id=? AND status='pending' ORDER BY created_at", (eid,))
        aprobadas = rechazadas = 0
        for row in pending:
            ok, _ = _do_approve_bet(conn, row["id"])
            if ok: aprobadas += 1
            else:  rechazadas += 1
        conn.commit()
    finally:
        conn.close()
    flash(f"{aprobadas} aprobadas, {rechazadas} rechazadas.", "success" if rechazadas == 0 else "info")
    emit_update("bet_approved")
    return redirect(url_for("admin_panel"))

@app.route("/admin/bet_request/<int:brid>/reject", methods=["POST"])
@login_required
@admin_required
def reject_bet_request(brid):
    conn = get_db()
    try:
        execute(conn, "UPDATE bet_requests SET status='rejected' WHERE id=? AND status='pending'", (brid,))
        conn.commit()
    finally:
        conn.close()
    flash("Solicitud rechazada.", "info")
    emit_update("bet_rejected")
    return redirect(url_for("admin_panel"))

# ── APROBAR / RECHAZAR PAGOS DE ENTRADA ───────────────────────────────────────

@app.route("/admin/cash/approve/<int:rid>", methods=["POST"])
@login_required
@admin_required
def approve_cash(rid):
    conn = get_db()
    try:
        req = fetchone(conn, "SELECT * FROM cash_requests WHERE id=?", (rid,))
        if not req: flash("Solicitud no encontrada.", "error"); return redirect(url_for("admin_panel"))
        t = req["type"]
        if t.startswith("entry_"):
            eid = int(t.split("_")[1])
            execute(conn, "INSERT INTO entries (user_id,event_id,paid_at) SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM entries WHERE user_id=? AND event_id=?)",
                (req["user_id"], eid, now(), req["user_id"], eid))
            execute(conn, "UPDATE events SET house_budget=house_budget+? WHERE id=?", (req["amount"], eid))
            execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                (eid, req["amount"], "income", f"Cuota entrada jugador ID {req['user_id']}", now()))
        elif t in ("deposit","manual_adjust"):
            execute(conn, "UPDATE users SET balance=balance+? WHERE id=?", (req["amount"], req["user_id"]))
        execute(conn, "UPDATE cash_requests SET status='approved', resolved_at=? WHERE id=?", (now(), rid))
        conn.commit()
    finally:
        conn.close()
    flash("Entrada confirmada.", "success")
    emit_update("entry_approved")
    return redirect(url_for("admin_panel"))

@app.route("/admin/cash/reject/<int:rid>", methods=["POST"])
@login_required
@admin_required
def reject_cash(rid):
    conn = get_db()
    try:
        execute(conn, "UPDATE cash_requests SET status='rejected', resolved_at=? WHERE id=?", (now(), rid))
        conn.commit()
    finally:
        conn.close()
    flash("Solicitud rechazada.", "info")
    emit_update("entry_rejected")
    return redirect(url_for("admin_panel"))

# ── FINALIZAR EVENTO Y PAGAR ──────────────────────────────────────────────────

@app.route("/admin/event/finish/<int:eid>", methods=["POST"])
@login_required
@admin_required
def finish_event(eid):
    winner_key = request.form["winner_key"]
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=? AND status!='finished'", (eid,))
        if not ev: flash("Evento no valido.", "error"); return redirect(url_for("admin_panel"))
        execute(conn, "UPDATE events SET status='finished', winner_key=? WHERE id=?", (winner_key, eid))

        all_bets     = fetchall(conn, "SELECT * FROM bets WHERE event_id=? AND result='pending'", (eid,))
        winning_bets = [b for b in all_bets if b["option_key"] == winner_key]
        losing_bets  = [b for b in all_bets if b["option_key"] != winner_key]
        losing_pool  = sum(b["amount"] for b in losing_bets)
        field_cut_pct= ev["field_cut_pct"]
        field_bonus  = round(losing_pool * field_cut_pct, 2)
        pool_para_ganancias = round(losing_pool - field_bonus, 2)
        total_ganancias = sum(round(b["potential"] - b["amount"], 2) for b in winning_bets)

        def pagar_ganadores(bets, pool_disponible, house_cover=0):
            for b in bets:
                ganancia_bruta = round(b["potential"] - b["amount"], 2)
                payout_bruto   = round(b["amount"] + ganancia_bruta, 2)
                payout         = round50(payout_bruto)
                redondeo       = round(payout_bruto - payout, 2)
                if redondeo > 0:
                    execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                        (eid, redondeo, "redondeo", f"Redondeo bet ID {b['id']}", now()))
                execute(conn, "UPDATE bets SET result='won', payout=? WHERE id=?", (payout, b["id"]))
                execute(conn, "UPDATE users SET balance=balance+? WHERE id=?", (payout, b["user_id"]))

        if pool_para_ganancias >= total_ganancias:
            house_profit = round(pool_para_ganancias - total_ganancias, 2)
            if house_profit > 0:
                execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                    (eid, house_profit, "profit", f"Pool perdedor ${losing_pool:,.0f}", now()))
            pagar_ganadores(winning_bets, pool_para_ganancias)
        else:
            deficit = round(total_ganancias - pool_para_ganancias, 2)
            hb = ev["house_budget"]
            if hb >= deficit:
                execute(conn, "UPDATE events SET house_budget=house_budget-? WHERE id=?", (deficit, eid))
                execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                    (eid, -deficit, "expense", f"Casa cubrió déficit ${deficit:,.0f}", now()))
                pagar_ganadores(winning_bets, pool_para_ganancias, deficit)
            else:
                total_disp = pool_para_ganancias + hb
                if hb > 0:
                    execute(conn, "UPDATE events SET house_budget=0 WHERE id=?", (eid,))
                    execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                        (eid, -hb, "expense", "Casa usó todo su presupuesto", now()))
                for b in winning_bets:
                    ratio  = round((b["potential"]-b["amount"]) / total_ganancias, 6) if total_ganancias > 0 else 0
                    payout_bruto = round(b["amount"] + total_disp * ratio, 2)
                    payout = round50(payout_bruto)
                    redondeo = round(payout_bruto - payout, 2)
                    if redondeo > 0:
                        execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                            (eid, redondeo, "redondeo", f"Redondeo bet ID {b['id']}", now()))
                    execute(conn, "UPDATE bets SET result='won', payout=? WHERE id=?", (payout, b["id"]))
                    execute(conn, "UPDATE users SET balance=balance+? WHERE id=?", (payout, b["user_id"]))

        for b in losing_bets:
            execute(conn, "UPDATE bets SET result='lost' WHERE id=?", (b["id"],))

        fp_home = fetchall(conn, "SELECT * FROM field_players WHERE event_id=? AND team_key='home'", (eid,))
        fp_away = fetchall(conn, "SELECT * FROM field_players WHERE event_id=? AND team_key='away'", (eid,))
        if winner_key == "draw":
            extra = sum(p["entry_paid"] for p in fp_home) + sum(p["entry_paid"] for p in fp_away) + field_bonus
            if extra > 0:
                execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                    (eid, extra, "cancha", "Empate: todo va a casa", now()))
        else:
            winner_fp = fp_home if winner_key == "home" else fp_away
            loser_fp  = fp_away if winner_key == "home" else fp_home
            n = len(winner_fp)
            if n > 0:
                fondo = sum(p["entry_paid"] for p in winner_fp) + sum(p["entry_paid"] for p in loser_fp) + field_bonus
                per_bruto = round(fondo / n, 2)
                per = round50(per_bruto)
                if round(per_bruto - per, 2) * n > 0:
                    execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                        (eid, round((per_bruto-per)*n, 2), "redondeo", f"Redondeo jugadores cancha", now()))
                for p in winner_fp:
                    execute(conn, "UPDATE field_players SET payout=? WHERE id=?", (per, p["id"]))
            else:
                extra = sum(p["entry_paid"] for p in loser_fp) + field_bonus
                if extra > 0:
                    execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                        (eid, extra, "cancha", "Sin ganadores cancha", now()))

        execute(conn, "UPDATE bet_requests SET status='cancelled' WHERE event_id=? AND status='pending'", (eid,))
        conn.commit()
    finally:
        conn.close()
    flash(f"Evento finalizado. {len(winning_bets)} apostadores ganadores.", "success")
    emit_update("event_finished")
    return redirect(url_for("admin_panel"))

# ── VER PERFIL JUGADOR (admin) ─────────────────────────────────────────────────

@app.route("/admin/player/<int:uid>")
@login_required
@admin_required
def view_player(uid):
    conn = get_db()
    try:
        user = fetchone(conn, "SELECT * FROM users WHERE id=?", (uid,))
        if not user: flash("Jugador no encontrado.", "error"); return redirect(url_for("admin_panel"))
        bets    = fetchall(conn, """SELECT b.*,e.home,e.away,e.sport,e.league,e.status,e.winner_key
            FROM bets b JOIN events e ON b.event_id=e.id WHERE b.user_id=? ORDER BY b.created_at DESC""", (uid,))
        entries = fetchall(conn, """SELECT en.*,e.home,e.away,e.sport,e.entry_fee
            FROM entries en JOIN events e ON en.event_id=e.id WHERE en.user_id=?""", (uid,))
        reqs    = fetchall(conn, "SELECT * FROM cash_requests WHERE user_id=? ORDER BY created_at DESC", (uid,))
        bet_reqs= fetchall(conn, """SELECT br.*,e.home,e.away FROM bet_requests br JOIN events e ON br.event_id=e.id
            WHERE br.user_id=? ORDER BY br.created_at DESC""", (uid,))
        entry_ids = {e["event_id"] for e in entries}
        all_active= fetchall(conn, "SELECT * FROM events WHERE status IN ('open','closed') ORDER BY created_at DESC")
        available_events = [ev for ev in all_active if ev["id"] not in entry_ids]
        class Stats: pass
        stats = Stats()
        stats.total_bet = sum(b["amount"] for b in bets)
        stats.total_won = sum(b["payout"] for b in bets if b["result"] == "won")
        stats.bets_won  = sum(1 for b in bets if b["result"] == "won")
        stats.bets_lost = sum(1 for b in bets if b["result"] == "lost")
    finally:
        conn.close()
    return render_template("player_profile.html", user=user, bets=bets, entries=entries,
        reqs=reqs, bet_reqs=bet_reqs, stats=stats, available_events=available_events)

@app.route("/admin/player/<int:uid>/adjust", methods=["POST"])
@login_required
@admin_required
def adjust_balance(uid):
    try: amount = float(request.form["amount"])
    except: flash("Monto invalido.", "error"); return redirect(url_for("view_player", uid=uid))
    note = request.form.get("note", "").strip()
    conn = get_db()
    try:
        execute(conn, "UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
        execute(conn, """INSERT INTO cash_requests (user_id,type,amount,status,note,resolved_at,created_at)
            VALUES (?,'manual_adjust',?,'approved',?,?,?)""", (uid, amount, note, now(), now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"Saldo ajustado ${amount:,.0f}.", "success"); return redirect(url_for("view_player", uid=uid))

@app.route("/admin/player/<int:uid>/add_entry/<int:eid>", methods=["POST"])
@login_required
@admin_required
def add_entry(uid, eid):
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=?", (eid,))
        if not ev: flash("Evento no encontrado.", "error"); return redirect(url_for("view_player", uid=uid))
        execute(conn, "INSERT INTO entries (user_id,event_id,paid_at) SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM entries WHERE user_id=? AND event_id=?)",
            (uid, eid, now(), uid, eid))
        if ev["entry_fee"] > 0:
            execute(conn, "UPDATE events SET house_budget=house_budget+? WHERE id=?", (ev["entry_fee"], eid))
            execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)",
                (eid, ev["entry_fee"], "income", f"Entrada manual jugador ID {uid}", now()))
        conn.commit()
    finally:
        conn.close()
    flash("Entrada confirmada.", "success"); return redirect(url_for("view_player", uid=uid))

@app.route("/admin/player/<int:uid>/remove_entry/<int:eid>", methods=["POST"])
@login_required
@admin_required
def remove_entry(uid, eid):
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=?", (eid,))
        if not ev: flash("Evento no encontrado.", "error"); return redirect(url_for("view_player", uid=uid))
        if ev["status"] == "finished": flash("No se puede quitar entrada de evento finalizado.", "error"); return redirect(url_for("view_player", uid=uid))
        execute(conn, "DELETE FROM entries WHERE user_id=? AND event_id=?", (uid, eid))
        if ev["entry_fee"] > 0:
            execute(conn, "UPDATE events SET house_budget=GREATEST(0,house_budget-?) WHERE id=?", (ev["entry_fee"], eid))
        conn.commit()
    finally:
        conn.close()
    flash("Entrada quitada.", "success"); return redirect(url_for("view_player", uid=uid))

@app.route("/admin/event/<int:eid>/house_budget/adjust", methods=["POST"])
@login_required
@admin_required
def adjust_house_budget(eid):
    try: amount = float(request.form["amount"])
    except: flash("Monto invalido.", "error"); return redirect(url_for("admin_panel"))
    note = request.form.get("note", "Ajuste manual").strip()
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=? AND status!='finished'", (eid,))
        if not ev: flash("Evento no valido.", "error"); return redirect(url_for("admin_panel"))
        execute(conn, "UPDATE events SET house_budget=GREATEST(0, house_budget+?) WHERE id=?", (amount, eid))
        execute(conn, "INSERT INTO house_log (event_id,amount,type,note,created_at) VALUES (?,?,?,?,?)", (eid, amount, "income", note, now()))
        conn.commit()
    finally:
        conn.close()
    flash(f"Presupuesto ajustado ${amount:+,.0f}.", "success"); return redirect(url_for("admin_panel"))

@app.route("/admin/event/<int:eid>/entry_fee/update", methods=["POST"])
@login_required
@admin_required
def update_entry_fee(eid):
    try: fee = float(request.form["entry_fee"])
    except: flash("Monto invalido.", "error"); return redirect(url_for("admin_panel"))
    if fee < 0: flash("Cuota negativa no permitida.", "error"); return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        ev = fetchone(conn, "SELECT * FROM events WHERE id=? AND status!='finished'", (eid,))
        if not ev: flash("Evento no valido.", "error"); return redirect(url_for("admin_panel"))
        execute(conn, "UPDATE events SET entry_fee=? WHERE id=?", (fee, eid))
        conn.commit()
    finally:
        conn.close()
    flash(f"Cuota actualizada a ${fee:,.0f}.", "success"); return redirect(url_for("admin_panel"))

@app.route("/admin/reset_data", methods=["POST"])
@login_required
@admin_required
def reset_data():
    if request.form.get("confirm", "") != "RESET":
        flash("Escribe RESET para confirmar.", "error"); return redirect(url_for("admin_panel"))
    conn = get_db()
    try:
        for t in ["events","event_odds","entries","bets","bet_requests","cash_requests","house_log","field_players"]:
            execute(conn, f"DELETE FROM {t}")
        execute(conn, "UPDATE users SET balance=0.0 WHERE role='player'")
        conn.commit()
    finally:
        conn.close()
    flash("Datos reseteados.", "success"); return redirect(url_for("admin_panel"))

# ── INIT ───────────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()

if __name__ == "__main__":
    socketio.run(app, debug=True, port=5000)
