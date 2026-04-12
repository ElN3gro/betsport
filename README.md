# BetSport 🏆

Plataforma privada de apuestas deportivas (fútbol y basketball).
Flask + SQLite | Acceso por token | Pagos en efectivo.

---

## Credenciales por defecto
- **Usuario admin:** `admin`
- **Contraseña admin:** `admin123`
> ⚠️ Cámbiala en producción desde la base de datos o agrega un endpoint de cambio de contraseña.

---

## Correr localmente

```bash
pip install -r requirements.txt
python app.py
# Abre http://localhost:5000
```

---

## Subir a GitHub

```bash
git init
git add .
git commit -m "BetSport inicial"
# Crea un repo en github.com, luego:
git remote add origin https://github.com/TU_USUARIO/betsport.git
git branch -M main
git push -u origin main
```

---

## Deploy en Render (gratis)

1. Ve a **render.com** → crear cuenta con GitHub
2. **New +** → **Web Service** → conecta el repo `betsport`
3. Configura:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Instance Type:** Free
4. En **Environment Variables** agrega:
   - `SECRET_KEY` = (una cadena aleatoria larga, ej: `mi-clave-super-secreta-2024`)
5. Click **Create Web Service** → en ~2 min tienes tu URL pública

---

## Flujo del sistema

### Como Admin:
1. Generar tokens → dárselos a los jugadores
2. Crear eventos (partido, liga, entrada, presupuesto de la casa)
3. Aprobar pagos en efectivo cuando el jugador pague físicamente
4. Al terminar el partido → declarar ganador → el sistema paga automáticamente

### Como Jugador:
1. Registrarse con token en `/register`
2. Ver eventos abiertos en el dashboard
3. Solicitar entrada → pagar en efectivo al admin → esperar confirmación
4. Una vez confirmado: apostar con el saldo acreditado
5. Ver resultados y cobros en "Mi Perfil"

---

## Distribución del dinero

- **90%** del pool de entradas → saldo apostable de los jugadores
- **10%** del pool de entradas → presupuesto visible de la casa
- Al finalizar un evento:
  - **10%** de lo apostado por perdedores → va a la casa
  - **90%** de lo apostado por perdedores → se reparte entre apostadores ganadores (proporcional a lo apostado)

---

## Lógica de multiplicadores

- Cada apuesta **baja** el odd de la opción apostada (proporcional al monto)
- Las opciones contrarias **suben** automáticamente
- Si un odd llega a **1.01x** → se bloquea esa opción
- Si la ganancia potencial supera el presupuesto de la casa → apuesta rechazada
