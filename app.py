from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
import os
import random
import string
from datetime import datetime, timedelta, date, time
from utils.email_service import send_email
from itsdangerous import URLSafeTimedSerializer
import uuid
import base64
from io import BytesIO
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from io import BytesIO
from flask import send_file

import pytz
from collections import defaultdict
from datetime import date, datetime, timedelta


app = Flask(__name__)

app.secret_key = "clave_super_segura"

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app.permanent_session_lifetime = timedelta(days=30)

SECURITY_PASSWORD_SALT = "recovery-salt"
serializer = URLSafeTimedSerializer(app.secret_key)


zona = pytz.timezone("America/Bogota")
hoy = datetime.now(zona).date()
hoy_iso = hoy.isoformat()

inicio_dia = hoy_iso + "T00:00:00"
fin_dia = hoy_iso + "T23:59:59"

@app.route("/")
def index():
    return redirect(url_for("login"))

@app.context_processor
def notificaciones_admin():

    try:
        pendientes = supabase.table("solicitudes_aumento_cupo") \
            .select("id", count="exact") \
            .eq("estado", "pendiente") \
            .execute()

        total = pendientes.count if pendientes.count else 0

    except:
        total = 0

    return dict(total_solicitudes_pendientes=total)

@app.context_processor
def utility_processor():
    def is_active(endpoint):
        return 'active' if request.endpoint == endpoint else ''

    return dict(is_active=is_active)
def generar_codigo_ruta():
    letras = ''.join(random.choices(string.ascii_uppercase, k=3))
    numeros = ''.join(random.choices(string.digits, k=4))
    return f"R-{letras}{numeros}"

from datetime import datetime, timedelta

@app.route("/cerrar_cajas_automatico")
def cerrar_cajas_automatico():

    # =====================================
    # FECHA COLOMBIA
    # =====================================
    ahora_col = datetime.utcnow() - timedelta(hours=5)

    # 🔥 EL CIERRE DEBE HACERSE SOBRE EL DÍA ANTERIOR
    fecha_cierre_col = ahora_col.date() - timedelta(days=1)
    fecha_cierre = fecha_cierre_col.isoformat()

    # Ventana UTC del día colombiano a cerrar
    inicio_dia = fecha_cierre + "T05:00:00"
    fin_dia = (fecha_cierre_col + timedelta(days=1)).isoformat() + "T05:00:00"

    print("=====================================")
    print("CIERRE AUTOMÁTICO")
    print("FECHA A CERRAR:", fecha_cierre)
    print("INICIO UTC:", inicio_dia)
    print("FIN UTC:", fin_dia)
    print("=====================================")

    rutas = supabase.table("rutas").select("id").execute()

    for r in rutas.data or []:
        ruta_id = r["id"]

        # =====================================
        # BUSCAR SI YA EXISTE CIERRE DE ESA FECHA
        # =====================================
        caja_existente = supabase.table("caja_diaria") \
            .select("id") \
            .eq("ruta_id", ruta_id) \
            .eq("fecha", fecha_cierre) \
            .limit(1) \
            .execute()

        registro_existente = caja_existente.data[0] if caja_existente.data else None

        # =====================================
        # SALDO INICIO = ÚLTIMO CIERRE ANTERIOR A ESA FECHA
        # =====================================
        caja_anterior = supabase.table("caja_diaria") \
            .select("saldo_cierre") \
            .eq("ruta_id", ruta_id) \
            .lt("fecha", fecha_cierre) \
            .order("fecha", desc=True) \
            .limit(1) \
            .execute()

        saldo_inicio = float(caja_anterior.data[0]["saldo_cierre"]) if caja_anterior.data else 0

        # =====================================
        # COBROS DEL DÍA
        # =====================================
        pagos = supabase.table("pagos") \
            .select("""
                monto,
                creditos!inner(
                    ruta_id
                )
            """) \
            .eq("creditos.ruta_id", ruta_id) \
            .gte("fecha", inicio_dia) \
            .lt("fecha", fin_dia) \
            .execute()

        total_cobros = sum(
            float(p["monto"] or 0)
            for p in pagos.data or []
        )

        # =====================================
        # GASTOS DEL DÍA
        # =====================================
        gastos = supabase.table("gastos") \
            .select("valor") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio_dia) \
            .lt("created_at", fin_dia) \
            .execute()

        total_gastos = sum(
            float(g["valor"] or 0)
            for g in gastos.data or []
        )

        # =====================================
        # PRÉSTAMOS DEL DÍA
        # =====================================
        prestamos = supabase.table("creditos") \
            .select("valor_venta") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio_dia) \
            .lt("created_at", fin_dia) \
            .execute()

        total_prestamos = sum(
            float(p["valor_venta"] or 0)
            for p in prestamos.data or []
        )

        # =====================================
        # ABONOS A CAPITAL DEL DÍA
        # =====================================
        capital = supabase.table("capital") \
            .select("valor") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio_dia) \
            .lt("created_at", fin_dia) \
            .execute()

        total_capital = sum(
            float(c["valor"] or 0)
            for c in capital.data or []
        )

        # =====================================
        # SALDO FINAL
        # =====================================
        saldo_cierre = (
            saldo_inicio
            + total_cobros
            + total_capital
            - total_prestamos
            - total_gastos
        )

        print(f"RUTA {ruta_id}")
        print("SALDO INICIO:", saldo_inicio)
        print("COBROS:", total_cobros)
        print("CAPITAL:", total_capital)
        print("PRESTAMOS:", total_prestamos)
        print("GASTOS:", total_gastos)
        print("SALDO CIERRE:", saldo_cierre)

        # =====================================
        # INSERTAR O ACTUALIZAR CIERRE
        # =====================================
        data_cierre = {
            "ruta_id": ruta_id,
            "fecha": fecha_cierre,
            "saldo_inicio": saldo_inicio,
            "saldo_cierre": saldo_cierre
        }

        if registro_existente:
            supabase.table("caja_diaria") \
                .update(data_cierre) \
                .eq("id", registro_existente["id"]) \
                .execute()

            print(f"✅ Cierre actualizado ruta {ruta_id} fecha {fecha_cierre}")
        else:
            supabase.table("caja_diaria") \
                .insert(data_cierre) \
                .execute()

            print(f"✅ Cierre creado ruta {ruta_id} fecha {fecha_cierre}")

    return "Cierre automático ejecutado correctamente"
# LOGIN
# -----------------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get("email")
        password = request.form.get("password")
        recordar = request.form.get("recordar")  # 👈 NUEVO

        # 🔎 Buscar solo por email y estado
        response = supabase.table("usuarios") \
            .select("*") \
            .eq("email", email) \
            .eq("estado", True) \
            .execute()

        if response.data:

            user = response.data[0]
            stored_password = user["password"]
            login_ok = False

            # 🔐 Si ya está encriptada
            if stored_password.startswith("scrypt:"):
                if check_password_hash(stored_password, password):
                    login_ok = True
            else:
                # 🟡 Usuario viejo con contraseña en texto plano
                if stored_password == password:
                    login_ok = True

                    # Migramos automáticamente a hash
                    new_hash = generate_password_hash(password)

                    supabase.table("usuarios").update({
                        "password": new_hash
                    }).eq("email", email).execute()

            if login_ok:

                session.clear()

                # 👇 RECORDAR SESIÓN
                if recordar:
                    session.permanent = True
                    app.permanent_session_lifetime = timedelta(days=30)
                else:
                    session.permanent = False

                session["pending_user_id"] = user["id"]

                response = redirect(url_for("verificar_token"))
                response.headers["Cache-Control"] = "no-cache"
                return response

        return render_template("login.html", error="Credenciales incorrectas")

    return render_template("login.html")


def generar_token_unico():
    while True:
        token = str(random.randint(100000, 999999))

        response = supabase.table("usuarios") \
            .select("id") \
            .eq("token_ingreso", token) \
            .execute()

        if not response.data:
            return token


@app.route("/usuarios/generar-token/<int:user_id>")
def generar_token_usuario(user_id):

    response = supabase.table("usuarios") \
        .select("*") \
        .eq("id", user_id) \
        .execute()

    if not response.data:
        return {"error": "Usuario no encontrado"}, 404

    user = response.data[0]

    # 🔥 SI YA TIENE TOKEN, LO MANTENEMOS
    if user.get("token_ingreso"):
        token = user["token_ingreso"]
    else:
        token = generar_token_unico()

        supabase.table("usuarios").update({
            "token_ingreso": token
        }).eq("id", user_id).execute()

    return {
        "email": user["email"],
        "nombre_completo": f"{user['nombres']} {user['apellidos']}",
        "token": token
    }

@app.route("/usuarios/ver-datos/<int:user_id>")
def ver_datos_usuario(user_id):

    response = supabase.table("usuarios") \
        .select("*") \
        .eq("id", user_id) \
        .execute()

    if not response.data:
        return {"error": "Usuario no encontrado"}, 404

    user = response.data[0]

    return {
        "nombre_completo": f"{user['nombres']} {user['apellidos']}",
        "email": user["email"],
        "rol": user["rol"],
        "estado": user["estado"],
        "token": user.get("token_ingreso") or "No generado"
    }
@app.route("/verificar-token", methods=["GET", "POST"])
def verificar_token():

    if "pending_user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        token_ingresado = request.form.get("token")

        response = supabase.table("usuarios") \
            .select("*") \
            .eq("id", session["pending_user_id"]) \
            .execute()

        if not response.data:
            return redirect(url_for("login"))

        user = response.data[0]

        if user["token_ingreso"] == token_ingresado:


            session.pop("pending_user_id", None)

            session["user_id"] = user["id"]
            session["nombre"] = user["nombres"]
            session["apellido"] = user["apellidos"]
            session["rol"] = user["rol"]

            flash("Acceso autorizado.", "success")
            return redirect(url_for("cambiar_oficina"))

        else:
            flash("Token incorrecto.", "error")
            return redirect(url_for("verificar_token"))


    return render_template("forgot_password/verificar_token.html")


@app.route("/actualizar_posicion_cliente", methods=["POST"])
def actualizar_posicion_cliente():

    credito_id = request.form.get("credito_id")
    nueva_posicion = request.form.get("posicion")

    if not credito_id or not nueva_posicion:
        flash("Datos inválidos", "danger")
        return redirect(url_for("listar_ventas"))

    try:
        nueva_posicion = int(nueva_posicion)
    except:
        flash("Posición inválida", "danger")
        return redirect(url_for("listar_ventas"))

    # Traer todos los créditos ordenados
    creditos = supabase.table("creditos") \
        .select("id") \
        .order("posicion") \
        .execute().data

    if not creditos:
        flash("No hay registros", "danger")
        return redirect(url_for("listar_ventas"))

    # Eliminar el actual de la lista
    ids = [c["id"] for c in creditos if c["id"] != credito_id]

    # Insertarlo en la nueva posición
    nueva_posicion = max(1, min(nueva_posicion, len(ids)+1))
    ids.insert(nueva_posicion - 1, credito_id)

    # Reordenar completamente
    for index, cid in enumerate(ids, start=1):
        supabase.table("creditos").update({
            "posicion": index
        }).eq("id", cid).execute()

    flash("Posición actualizada correctamente", "success")
    return redirect(url_for("listar_ventas"))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():

    if request.method == 'POST':

        documento = request.form.get("documento")

        response = supabase.table("usuarios") \
            .select("email, documento") \
            .eq("documento", documento) \
            .execute()

        if response.data:

            user = response.data[0]
            email = user["email"]

            token = serializer.dumps(email, salt=SECURITY_PASSWORD_SALT)
            reset_url = url_for('reset_password', token=token, _external=True)

            subject = "Recuperación de contraseña"
            body = f"""
            Hola,

            Haz clic en el siguiente enlace para restablecer tu contraseña:

            {reset_url}

            Si no solicitaste esto, ignora el mensaje.
            """

            send_email(email, subject, body)


            flash("Se envió un enlace al correo registrado.", "success")

        else:
            flash("El documento ingresado no existe en nuestra base de datos.", "error")
            return redirect(url_for("forgot_password"))  # 👈 IMPORTANTE

    return render_template('forgot_password/forgot_password.html')



@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):

    try:
        email = serializer.loads(
            token,
            salt=SECURITY_PASSWORD_SALT,
            max_age=3600
        )
    except:
        flash("El enlace ha expirado o es inválido.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == 'POST':

        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        # Validaciones
        if not password or not confirm_password:
            flash("Todos los campos son obligatorios.", "error")
            return redirect(request.url)

        if len(password) < 6:
            flash("La contraseña debe tener mínimo 6 caracteres.", "error")
            return redirect(request.url)

        if password != confirm_password:
            flash("Las contraseñas no coinciden.", "error")
            return redirect(request.url)

        from werkzeug.security import generate_password_hash
        hashed_password = generate_password_hash(password)

        supabase.table("usuarios").update({
            "password": hashed_password
        }).eq("email", email).execute()

        flash("Contraseña actualizada correctamente.", "success")

    return render_template("forgot_password/reset_password.html")

@app.route("/login_app", methods=["GET", "POST"])
def login_app():

    if request.method == "POST":

        email = request.form.get("email")
        password = request.form.get("password")
        recordar = request.form.get("recordar")

        if not email or not password:
            return render_template(
                "login_app/login_app.html",
                error="Debe ingresar correo y contraseña"
            )

        # 🔎 Buscar usuario activo
        response = supabase.table("usuarios") \
            .select("*") \
            .eq("email", email) \
            .eq("estado", True) \
            .execute()

        if not response.data:
            return render_template(
                "login_app/login_app.html",
                error="Usuario no encontrado o inactivo"
            )

        user = response.data[0]

        # 🔐 Validar rol permitido
        if user["rol"] not in ["Cobrador", "Supervisor", "Administrador"]:
            return render_template(
                "login_app/login_app.html",
                error="Este usuario no tiene acceso a la app"
            )

        stored_password = user["password"]
        login_ok = False

        # 🔐 Password encriptada
        if stored_password.startswith("scrypt:"):
            if check_password_hash(stored_password, password):
                login_ok = True

        else:
            # 🔄 Migrar password texto plano
            if stored_password == password:

                login_ok = True

                new_hash = generate_password_hash(password)

                supabase.table("usuarios").update({
                    "password": new_hash
                }).eq("id", user["id"]).execute()

        if not login_ok:
            return render_template(
                "login_app/login_app.html",
                error="Contraseña incorrecta"
            )

        # 🔐 LOGIN OK
        session.clear()

        if recordar:
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=30)
        else:
            session.permanent = False

        # guardar temporalmente para token
        session["pending_user_id"] = user["id"]
        session["login_tipo"] = "app"
        session["rol"] = user["rol"]

        return redirect(url_for("verificar_token_app"))

    return render_template("login_app/login_app.html")
@app.route("/verificar-token-app", methods=["GET", "POST"])
def verificar_token_app():

    if "pending_user_id" not in session:
        return redirect(url_for("login_app"))

    if request.method == "POST":

        token_ingresado = request.form.get("token")

        response = supabase.table("usuarios") \
            .select("*") \
            .eq("id", session["pending_user_id"]) \
            .execute()

        if not response.data:
            return redirect(url_for("login_app"))

        user = response.data[0]

        # ✅ Permitir solo Cobrador o Supervisor
        if user["rol"] not in ["Cobrador", "Supervisor", "Administrador"]:
            return redirect(url_for("login_app"))

        if str(user["token_ingreso"]) == str(token_ingresado):

            # 🔐 Crear sesión REAL
            session.pop("pending_user_id", None)

            session["user_id"] = user["id"]
            session["rol"] = user["rol"].lower()

            # 👤 Datos usuario
            nombres = user.get("nombres", "")
            apellidos = user.get("apellidos", "")
            email = user.get("email", "")

            session["nombre"] = nombres
            session["nombre_completo"] = f"{nombres} {apellidos}".strip()
            session["email"] = email

            session.permanent = True
            app.permanent_session_lifetime = timedelta(hours=8)

            # 🔀 Redirigir según rol
            if user["rol"] == "Supervisor":
                return redirect(url_for("dashboard_cobrador"))
            else:
                return redirect(url_for("dashboard_cobrador"))

        else:
            flash("Token incorrecto.", "error")
            return redirect(url_for("verificar_token_app"))

    return render_template("login_app/verificar_token_app.html")

@app.route("/dashboard_cobrador")
def dashboard_cobrador():

    # =============================
    # VALIDAR SESIÓN
    # =============================
    if "user_id" not in session or session.get("rol", "").lower() not in ["cobrador", "supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    user_id = int(session["user_id"])
    rol = session.get("rol", "").lower()

    rutas = []

    # =============================
    # COBRADOR
    # =============================
    if rol == "cobrador":

        rutas_resp = supabase.table("rutas") \
            .select("*") \
            .eq("usuario_id", user_id) \
            .order("posicion") \
            .execute()

        rutas = rutas_resp.data or []

    # =============================
    # SUPERVISOR
    # =============================
    elif rol == "supervisor":

        asignaciones = supabase.table("usuarios_rutas") \
            .select("ruta_id") \
            .eq("usuario_id", user_id) \
            .execute()

        rutas_ids = [r["ruta_id"] for r in asignaciones.data] if asignaciones.data else []

        if rutas_ids:

            rutas_resp = supabase.table("rutas") \
                .select("*") \
                .in_("id", rutas_ids) \
                .order("posicion") \
                .execute()

            rutas = rutas_resp.data or []

    # =============================
    # ADMINISTRADOR
    # =============================
    elif rol == "administrador":

        rutas_resp = supabase.table("rutas") \
            .select("*") \
            .order("posicion") \
            .execute()

        rutas = rutas_resp.data or []

    # =============================
    # ASEGURAR RUTA ACTIVA
    # =============================

    if rutas and not session.get("ruta_id"):
        session["ruta_id"] = rutas[0]["id"]

    if session.get("ruta_id"):
        ruta_valida = any(r["id"] == session["ruta_id"] for r in rutas)

        if not ruta_valida and rutas:
            session["ruta_id"] = rutas[0]["id"]

    # =============================
    # TRAER OFICINA DE CADA RUTA
    # =============================

    rutas_completas = []

    for ruta in rutas:

        oficina_info = None

        if ruta.get("oficina_id"):

            oficina_resp = supabase.table("oficinas") \
                .select("*") \
                .eq("id", ruta["oficina_id"]) \
                .execute()

            if oficina_resp.data:
                oficina_info = oficina_resp.data[0]

        ruta["oficina"] = oficina_info
        rutas_completas.append(ruta)

    # =============================
    # NOTIFICACIONES
    # =============================

    notificaciones = supabase.table("notificaciones") \
        .select("*") \
        .eq("usuario_id", user_id) \
        .eq("leida", False) \
        .order("created_at", desc=True) \
        .execute().data or []

    # =============================
    # RENDER
    # =============================

    return render_template(
        "cobrador/dashboard.html",
        rutas=rutas_completas,
        ruta_id=session.get("ruta_id"),
        notificaciones=notificaciones
    )

@app.route("/usuarios/actualizar", methods=["POST"])
def actualizar_usuario():

    user_id = request.form.get("id")
    documento = request.form.get("documento")
    nombres = request.form.get("nombres")
    apellidos = request.form.get("apellidos")
    email = request.form.get("email")
    rol = request.form.get("rol")
    password = request.form.get("password")

    update_data = {
        "documento": documento,
        "nombres": nombres,
        "apellidos": apellidos,
        "email": email,
        "rol": rol
    }

    # Si escribió nueva contraseña
    if password:
        update_data["password"] = password  # si usas hash, aquí haces el hash

    supabase.table("usuarios") \
        .update(update_data) \
        .eq("id", user_id) \
        .execute()

    flash("Usuario actualizado correctamente", "success")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/eliminar", methods=["POST"])
def eliminar_usuario():

    user_id = request.form.get("user_id")

    # 🔒 Evitar que se elimine a sí mismo
    if str(session.get("user_id")) == str(user_id):
        flash("No puedes eliminar tu propio usuario", "danger")
        return redirect(url_for("usuarios"))

    supabase.table("usuarios") \
        .delete() \
        .eq("id", user_id) \
        .execute()

    flash("Usuario eliminado correctamente", "success")
    return redirect(url_for("usuarios"))


@app.route("/editar_venta_maxima", methods=["POST"])
def editar_venta_maxima():

    if "user_id" not in session:
        return redirect(url_for("login"))

    ruta_id = request.form.get("ruta_id")
    venta_maxima = request.form.get("venta_maxima")

    try:
        venta_maxima = float(venta_maxima)
    except:
        flash("Valor inválido", "danger")
        return redirect(url_for("listar_rutas"))  # ← usa tu vista principal aquí

    supabase.table("rutas").update({
        "venta_maxima": venta_maxima
    }).eq("id", ruta_id).execute()

    flash("Venta máxima actualizada correctamente", "success")

    return redirect(url_for("listar_rutas"))  # ← NO ver_ruta

# 🔎 Traer crédito + cliente APP

# 🔎 Traer crédito + cliente APP
# 🔎 Traer crédito + cliente APP
@app.route("/credito/<credito_id>")
def detalle_credito(credito_id):

    if "user_id" not in session or session.get("rol") not in ["cobrador","supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    credito = supabase.table("creditos") \
        .select("""
            *,
            clientes(
                id,
                nombre,
                identificacion,
                telefono_principal,
                direccion,
                codigo_pais
            )
        """) \
        .eq("id", credito_id) \
        .single() \
        .execute().data

    if not credito:
        return redirect(url_for("dashboard_cobrador"))

    cuotas_db = supabase.table("cuotas") \
        .select("*") \
        .eq("credito_id", credito_id) \
        .order("numero") \
        .execute().data

    # 🔹 Fecha de finalización del crédito = fecha de la última cuota
    fecha_finalizacion_credito = None

    if cuotas_db:
        ultima_cuota = max(
            cuotas_db,
            key=lambda c: date.fromisoformat((c.get("fecha_pago") or "1900-01-01").split("T")[0])
        )

        fecha_ultima = (ultima_cuota.get("fecha_pago") or "").split("T")[0]

        if fecha_ultima:
            try:
                fecha_finalizacion_credito = date.fromisoformat(fecha_ultima).strftime("%d/%m/%Y")
            except Exception:
                fecha_finalizacion_credito = fecha_ultima

    total_pagado = 0
    cuotas = []
    proxima_cuota = None

    valor_venta = float(credito.get("valor_venta") or 0)
    tasa = float(credito.get("tasa") or 0)
    cantidad_cuotas = int(credito.get("cantidad_cuotas") or 1)

    interes_total = valor_venta * tasa / 100
    interes_cuota = interes_total / cantidad_cuotas

    for c in cuotas_db:

        dias_mora = 0
        valor_cuota = float(c["valor"] or 0)
        pagado = float(c.get("monto_pagado") or 0)

        # 🔹 SUMAR TODO LO PAGADO (clave)
        total_pagado += pagado

        # 🔹 detectar mora
        if c["estado"] == "pendiente":
            fecha = date.fromisoformat(c["fecha_pago"])

            if fecha < date.today():
                dias_mora = (date.today() - fecha).days

            if not proxima_cuota:
                proxima_cuota = c["fecha_pago"]

        capital = valor_cuota - interes_cuota

        cuotas.append({
            "id": c["id"],
            "numero": c["numero"],
            "valor": valor_cuota,
            "pagado": pagado,
            "capital": capital,
            "interes": interes_cuota,
            "estado": c["estado"],
            "fecha_pago": c["fecha_pago"],
            "dias_mora": dias_mora,
            "valor_interes_mora": float(c.get("valor_interes_mora", 0) or 0),
            "porcentaje_mora": float(c.get("porcentaje_mora", 0) or 0),
            
        })

    saldo = float(credito["valor_total"]) - total_pagado

    cuotas_pagadas = sum(
        1 for c in cuotas_db
        if float(c.get("monto_pagado", 0)) >= float(c.get("valor", 0))
    )

    puede_renovar = cuotas_pagadas >= 3

    return render_template(
        "cobrador/detalle_credito.html",
        credito=credito,
        cuotas=cuotas,
        saldo=saldo,
        total_pagado=total_pagado,
        proxima_cuota=proxima_cuota,
        puede_renovar=puede_renovar,
        cuotas_pagadas=cuotas_pagadas,
        fecha_finalizacion_credito=fecha_finalizacion_credito

    )
    
from datetime import datetime
def ahora_colombia():
    return datetime.utcnow() - timedelta(hours=5)
@app.route("/registrar_pago", methods=["POST"])
def registrar_pago():

    cuota_id = request.form.get("cuota_id")
    monto_pago = float(request.form.get("monto_pago") or 0)
    tipo_pago = request.form.get("tipo_pago", "normal")
    extra_interes = float(request.form.get("extra_interes") or 0)
    aplicar_interes = request.form.get("aplicar_interes") == "true"

    if not cuota_id or monto_pago <= 0:
        return redirect(request.referrer)

    # =========================
    # TRAER CUOTA SELECCIONADA
    # =========================
    cuota_resp = supabase.table("cuotas") \
        .select("*") \
        .eq("id", cuota_id) \
        .single() \
        .execute()

    if not cuota_resp.data:
        return redirect(request.referrer)

    cuota = cuota_resp.data
    credito_id = cuota["credito_id"]
    numero_cuota_inicio = cuota["numero"]

    # =========================
    # PAGO COMPENSATORIO
    # =========================
    if tipo_pago == "compensatorio":

        pago_resp = supabase.table("pagos").insert({
            "cuota_id": cuota_id,
            "credito_id": credito_id,
            "monto": monto_pago,
            "fecha": ahora_colombia().isoformat(),
            "cobrador_id": session["user_id"],
            "tipo_pago": "compensatorio"
        }).execute()

        pago_id = pago_resp.data[0]["id"]
        return redirect(url_for("recibo_pago", pago_id=pago_id))

    # =========================
    # SOLO INTERÉS
    # No descuenta cuota ni saldo
    # =========================
    if tipo_pago == "intereses":

        pago_resp = supabase.table("pagos").insert({
            "cuota_id": cuota_id,
            "credito_id": credito_id,
            "monto": monto_pago,
            "fecha": ahora_colombia().isoformat(),
            "cobrador_id": session["user_id"],
            "tipo_pago": "intereses"
        }).execute()

        pago_id = pago_resp.data[0]["id"]
        return redirect(url_for("recibo_pago", pago_id=pago_id))

    # =========================
    # APLICAR INTERÉS A LA CUOTA
    # =========================
    if tipo_pago == "normal" and aplicar_interes and extra_interes > 0:

        interes_actual_mora = float(cuota.get("valor_interes_mora") or 0)

        supabase.table("cuotas").update({
            "valor_interes_mora": interes_actual_mora + extra_interes
        }).eq("id", cuota_id).execute()

        # refrescar cuota seleccionada
        cuota_resp = supabase.table("cuotas") \
            .select("*") \
            .eq("id", cuota_id) \
            .single() \
            .execute()

        if cuota_resp.data:
            cuota = cuota_resp.data

    # =========================
    # TRAER CUOTAS DEL CRÉDITO
    # =========================
    cuotas = supabase.table("cuotas") \
        .select("*") \
        .eq("credito_id", credito_id) \
        .order("numero") \
        .execute().data or []

    monto_restante = monto_pago

    def total_cuota(c):
        return float(c.get("valor") or 0) + float(c.get("valor_interes_mora") or 0)

    def pagado_cuota(c):
        return float(c.get("monto_pagado") or 0)

    # =========================
    # CALCULAR DEUDA TOTAL
    # =========================
    total_deuda = 0

    for c in cuotas:
        valor_real = total_cuota(c)
        pagado = pagado_cuota(c)
        faltante = valor_real - pagado

        if faltante > 0:
            total_deuda += faltante

    # =========================
    # SI EL PAGO CUBRE TODA LA DEUDA
    # =========================
    if monto_pago >= total_deuda:

        for c in cuotas:
            valor_real = total_cuota(c)

            supabase.table("cuotas").update({
                "monto_pagado": valor_real,
                "estado": "pagado",
                "fecha_pago_real": ahora_colombia().isoformat()
            }).eq("id", c["id"]).execute()

        monto_restante = 0

    # =========================
    # DISTRIBUIR PAGO NORMAL
    # =========================
    else:
        for c in cuotas:

            if monto_restante <= 0:
                break

            # no tocar cuotas anteriores
            if c["numero"] < numero_cuota_inicio:
                continue

            valor_real = total_cuota(c)
            pagado = pagado_cuota(c)

            if pagado >= valor_real:
                continue

            faltante = valor_real - pagado

            # paga completa esa cuota
            if monto_restante >= faltante:
                nuevo_pagado = pagado + faltante
                estado = "pagado"
                monto_restante -= faltante
            else:
                nuevo_pagado = pagado + monto_restante
                estado = "pendiente"
                monto_restante = 0

            supabase.table("cuotas").update({
                "monto_pagado": nuevo_pagado,
                "estado": estado,
                "fecha_pago_real": ahora_colombia().isoformat()
            }).eq("id", c["id"]).execute()

    # =========================
    # REGISTRAR PAGO
    # =========================
    pago_resp = supabase.table("pagos").insert({
        "cuota_id": cuota_id,
        "credito_id": credito_id,
        "monto": monto_pago,
        "extra_interes": extra_interes if aplicar_interes else 0,  # 🔥 CLAVE
        "fecha": ahora_colombia().isoformat(),
        "cobrador_id": session["user_id"],
        "tipo_pago": tipo_pago
    }).execute()

    pago_id = pago_resp.data[0]["id"]

        # 🔥 REPARAR TODAS LAS CUOTAS SIEMPRE
    recalcular_credito(credito_id)


    # =========================
    # VERIFICAR SI CRÉDITO TERMINÓ
    # =========================
    cuotas_actualizadas = supabase.table("cuotas") \
        .select("id, valor, monto_pagado, valor_interes_mora") \
        .eq("credito_id", credito_id) \
        .execute().data or []

    credito_terminado = True

    for c in cuotas_actualizadas:
        valor_real = float(c.get("valor") or 0) + float(c.get("valor_interes_mora") or 0)
        pagado = float(c.get("monto_pagado") or 0)

        if pagado < valor_real:
            credito_terminado = False
            break

    if credito_terminado:
        supabase.table("creditos").update({
            "estado": "pagado"
        }).eq("id", credito_id).execute()

    return redirect(url_for("recibo_pago", pago_id=pago_id))


# =====================================
# RECIBO
# =====================================

from datetime import datetime, timedelta

@app.route("/recibo/<pago_id>")
def recibo_pago(pago_id):

    pago = supabase.table("pagos") \
        .select("""
            *,
            cuotas(
                numero,
                credito_id,
                creditos(
                    id,
                    valor_total,
                    rutas(
                        codigo,
                        nombre
                    ),
                    clientes(
                        nombre
                    )
                )
            )
        """) \
        .eq("id", pago_id) \
        .single() \
        .execute().data

    # =========================
    # FORMATEAR FECHA
    # =========================
    fecha_iso = pago.get("fecha")

    if fecha_iso:
        fecha_obj = datetime.fromisoformat(fecha_iso)
        pago["fecha_formateada"] = fecha_obj.strftime("%d/%m/%Y %H:%M")

    # =========================
    # CALCULAR SALDO RESTANTE
    # =========================
    credito_id = pago["cuotas"]["credito_id"]

    cuotas = supabase.table("cuotas") \
        .select("monto_pagado") \
        .eq("credito_id", credito_id) \
        .execute().data

    total_pagado = sum(float(c.get("monto_pagado") or 0) for c in cuotas)

    valor_total = float(pago["cuotas"]["creditos"]["valor_total"])

    saldo_restante = valor_total - total_pagado

    return render_template(
        "cobrador/recibo_pago.html",
        pago=pago,
        saldo_restante=saldo_restante
    )


def recalcular_credito(credito_id):

    # traer cuotas
    cuotas = supabase.table("cuotas") \
        .select("*") \
        .eq("credito_id", credito_id) \
        .order("numero") \
        .execute().data

    # traer pagos
    pagos = supabase.table("pagos") \
        .select("*") \
        .eq("credito_id", credito_id) \
        .order("fecha") \
        .execute().data

    total_pagado = sum(float(p["monto"]) for p in pagos)

    saldo = total_pagado

    for c in cuotas:

        valor = float(c["valor"])

        if saldo >= valor:

            nuevo_pagado = valor
            estado = "pagado"
            saldo -= valor

        else:

            nuevo_pagado = saldo
            estado = "pendiente"
            saldo = 0

        supabase.table("cuotas").update({
            "monto_pagado": nuevo_pagado,
            "estado": estado
        }).eq("id", c["id"]).execute()
        
@app.route("/recalcular/<credito_id>")
def recalcular(credito_id):

    recalcular_credito(credito_id)

    return "Recalculado"
# =============================
# NUEVA VENTA COBRADOR (CONTROL FLUJO)
# =============================
# =============================
# NUEVA VENTA COBRADOR (CONTROL FLUJO)
@app.route("/nueva_venta_cobrador", methods=["GET", "POST"])
def nueva_venta_cobrador():

    if "user_id" not in session or session.get("rol") not in ["cobrador", "supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    user_id = int(session["user_id"])

    if session.get("rol") == "cobrador":
        rutas = supabase.table("rutas") \
            .select("*") \
            .eq("usuario_id", user_id) \
            .eq("estado", "true") \
            .order("posicion") \
            .execute().data or []
    else:
        rutas = supabase.table("rutas") \
            .select("*") \
            .eq("estado", "true") \
            .order("posicion") \
            .execute().data or []

    params = request.values
    ruta_actual = params.get("ruta_id") or session.get("ruta_id")

    cedula_aprobada = (params.get("cedula") or "").strip()
    cliente_id_aprobado_raw = (params.get("cliente_id_aprobado") or "").strip()
    monto_aprobado_raw = (params.get("monto") or "").strip()
    solicitud_id_raw = (params.get("solicitud_id") or "").strip()

    cliente_id_renovacion = params.get("cliente_id_renovacion")
    es_renovacion = params.get("renovar") == "1"

    cliente_data = None
    ultimo_credito_data = {}
    form_data = {}

    def safe_int(value):
        try:
            return int(str(value).strip())
        except Exception:
            return None

    def normalizar_cedula(valor):
        return (
            str(valor or "")
            .replace(".", "")
            .replace(",", "")
            .replace(" ", "")
            .strip()
        )

    def armar_form_data_desde_solicitud(solicitud, monto):
        return {
            "cliente_id": "",
            "identificacion": solicitud.get("cedula", ""),
            "nombre": solicitud.get("cliente_nombre", ""),
            "direccion": solicitud.get("direccion", ""),
            "direccion_negocio": solicitud.get("direccion", ""),
            "codigo_pais": "57",
            "telefono": solicitud.get("telefono") or solicitud.get("telefono_principal") or "",
            "valor_venta": monto if monto > 0 else "",
            "descripcion_credito": solicitud.get("descripcion_actividad", ""),

            "foto_cliente": "",
            "foto_cedula": "",
            "foto_negocio": "",
            "firma_cliente": "",

            "foto_cliente_actual": "",
            "foto_cedula_actual": "",
            "foto_negocio_actual": "",
            "firma_cliente_actual": ""
        }

    cliente_id_aprobado = safe_int(cliente_id_aprobado_raw)
    solicitud_id = safe_int(solicitud_id_raw)

    monto_aprobado = 0
    try:
        monto_aprobado = float(str(monto_aprobado_raw).replace(".", "").replace(",", ".")) if monto_aprobado_raw else 0
    except Exception:
        monto_aprobado = 0

    print("DEBUG PARAMS NUEVA VENTA:", params.to_dict())
    print("DEBUG solicitud_id:", solicitud_id)
    print("DEBUG cedula_aprobada:", cedula_aprobada)

    # =====================================================
    # PRIORIDAD 1: RENOVACIÓN
    # =====================================================
    if es_renovacion and cliente_id_renovacion:
        cliente_resp = supabase.table("clientes") \
            .select("*") \
            .eq("id", cliente_id_renovacion) \
            .single() \
            .execute()

        if cliente_resp.data:
            cliente_data = cliente_resp.data

    # =====================================================
    # PRIORIDAD 2: SOLICITUD APROBADA POR ID
    # =====================================================
    elif solicitud_id:
        solicitud_resp = supabase.table("solicitudes_aumento_cupo") \
            .select("*") \
            .eq("id", solicitud_id) \
            .single() \
            .execute()

        solicitud = solicitud_resp.data
        print("DEBUG SOLICITUD POR ID:", solicitud)

        if solicitud:
            if not monto_aprobado:
                try:
                    monto_aprobado = float(solicitud.get("monto_solicitado") or 0)
                except Exception:
                    monto_aprobado = 0

            # CLIENTE EXISTENTE
            if solicitud.get("cliente_id"):
                cliente_resp = supabase.table("clientes") \
                    .select("*") \
                    .eq("id", solicitud["cliente_id"]) \
                    .single() \
                    .execute()

                print("DEBUG CLIENTE DESDE SOLICITUD:", cliente_resp.data)

                if cliente_resp.data:
                    cliente_data = cliente_resp.data

            # CLIENTE NUEVO
            else:
                form_data = armar_form_data_desde_solicitud(solicitud, monto_aprobado)
                print("DEBUG FORM DATA CLIENTE NUEVO DESDE SOLICITUD_ID:", form_data)

    # =====================================================
    # PRIORIDAD 3: AUMENTO APROBADO POR ID DE CLIENTE
    # =====================================================
    elif cliente_id_aprobado:
        cliente_resp = supabase.table("clientes") \
            .select("*") \
            .eq("id", cliente_id_aprobado) \
            .single() \
            .execute()

        if cliente_resp.data:
            cliente_data = cliente_resp.data

    # =====================================================
    # PRIORIDAD 4: AUMENTO APROBADO POR CÉDULA
    # =====================================================
    elif cedula_aprobada:
        cedula_normalizada = normalizar_cedula(cedula_aprobada)

        # 1) Primero intentar como cliente existente
        cliente_resp = supabase.table("clientes") \
            .select("*") \
            .eq("identificacion", cedula_normalizada) \
            .limit(1) \
            .execute()

        if cliente_resp.data:
            cliente_data = cliente_resp.data[0]
            print("DEBUG CLIENTE ENCONTRADO POR CÉDULA:", cliente_data)

        else:
            # 2) Si no existe como cliente, intentar cargar desde la solicitud
            solicitud_resp = supabase.table("solicitudes_aumento_cupo") \
                .select("*") \
                .eq("cedula", cedula_normalizada) \
                .order("id", desc=True) \
                .limit(1) \
                .execute()

            solicitud = solicitud_resp.data[0] if solicitud_resp.data else None
            print("DEBUG SOLICITUD ENCONTRADA POR CÉDULA:", solicitud)

            if solicitud:
                if not monto_aprobado:
                    try:
                        monto_aprobado = float(solicitud.get("monto_solicitado") or 0)
                    except Exception:
                        monto_aprobado = 0

                # Si esa solicitud sí tiene cliente_id, cargar cliente
                if solicitud.get("cliente_id"):
                    cliente_resp = supabase.table("clientes") \
                        .select("*") \
                        .eq("id", solicitud["cliente_id"]) \
                        .single() \
                        .execute()

                    if cliente_resp.data:
                        cliente_data = cliente_resp.data
                else:
                    # Cliente nuevo: llenar desde la solicitud
                    form_data = armar_form_data_desde_solicitud(solicitud, monto_aprobado)
                    print("DEBUG FORM DATA CLIENTE NUEVO DESDE CÉDULA:", form_data)

    # =====================================================
    # SI ENCONTRÓ CLIENTE EN BD
    # =====================================================
    if cliente_data:
        ultimo_credito_resp = supabase.table("creditos") \
            .select("id, foto_cliente, foto_cedula, foto_negocio, firma_cliente") \
            .eq("cliente_id", cliente_data["id"]) \
            .order("id", desc=True) \
            .limit(1) \
            .execute()

        if ultimo_credito_resp.data:
            ultimo_credito_data = ultimo_credito_resp.data[0]

        form_data = {
            "cliente_id": cliente_data.get("id", ""),
            "identificacion": cliente_data.get("identificacion", ""),
            "nombre": cliente_data.get("nombre", ""),
            "direccion": cliente_data.get("direccion", ""),
            "direccion_negocio": cliente_data.get("direccion_negocio", ""),
            "codigo_pais": cliente_data.get("codigo_pais", "57"),
            "telefono": cliente_data.get("telefono_principal", ""),
            "valor_venta": monto_aprobado if monto_aprobado > 0 else "",
            "descripcion_credito": "",

            "foto_cliente": ultimo_credito_data.get("foto_cliente", ""),
            "foto_cedula": ultimo_credito_data.get("foto_cedula", ""),
            "foto_negocio": ultimo_credito_data.get("foto_negocio", ""),
            "firma_cliente": ultimo_credito_data.get("firma_cliente", ""),

            "foto_cliente_actual": ultimo_credito_data.get("foto_cliente", ""),
            "foto_cedula_actual": ultimo_credito_data.get("foto_cedula", ""),
            "foto_negocio_actual": ultimo_credito_data.get("foto_negocio", ""),
            "firma_cliente_actual": ultimo_credito_data.get("firma_cliente", "")
        }

    modo_aumento = True if (solicitud_id or cliente_id_aprobado or cedula_aprobada) and not es_renovacion else False

    print("DEBUG cliente_data FINAL:", cliente_data)
    print("DEBUG form_data FINAL:", form_data)

    return render_template(
        "cobrador/nueva_venta_cobrador.html",
        rutas=rutas,
        ruta_actual=ruta_actual,
        cliente_aprobado=cliente_data,
        monto_aprobado=monto_aprobado,
        form_data=form_data,
        es_renovacion=es_renovacion,
        modo_aumento=modo_aumento,
        solicitud_id=solicitud_id
    )

@app.route("/buzon_aumento_cupo")
def buzon_aumento_cupo():

    # 🔐 Validar sesión
    if "user_id" not in session or session.get("rol") not in ["cobrador","supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    user_id = session["user_id"]
    ruta_id = session.get("ruta_id")

    # 🔒 Validar que exista ruta activa
    if not ruta_id:
        flash("Debe seleccionar una ruta", "warning")
        return redirect(url_for("dashboard_cobrador"))

    # 🔎 Traer solo solicitudes de esa ruta y ese cobrador
    solicitudes_resp = supabase.table("solicitudes_aumento_cupo") \
        .select("*") \
        .eq("usuario_id", user_id) \
        .eq("ruta_id", ruta_id) \
        .order("fecha", desc=True) \
        .execute()

    solicitudes = solicitudes_resp.data or []

    # 🕒 Formatear fechas
    for s in solicitudes:
        fecha = s.get("created_at")
        if fecha:
            try:
                fecha_utc = datetime.fromisoformat(fecha.replace("Z", "+00:00"))
                fecha_colombia = fecha_utc - timedelta(hours=5)
                s["fecha_formateada"] = fecha_colombia.strftime("%d/%m/%Y %I:%M %p")
            except:
                s["fecha_formateada"] = fecha
        else:
            s["fecha_formateada"] = ""
    
    notificaciones = supabase.table("notificaciones") \
        .select("*") \
        .eq("usuario_id", session["user_id"]) \
        .eq("leida", False) \
        .order("created_at", desc=True) \
        .execute().data

    return render_template(
        "cobrador/buzon_aumento_cupo.html",
        solicitudes=solicitudes,
        notificaciones=notificaciones

    )
@app.context_processor
def inyectar_notificaciones():
    if "user_id" in session:
        resp = supabase.table("notificaciones") \
            .select("*") \
            .eq("usuario_id", session["user_id"]) \
            .eq("leida", False) \
            .order("created_at", desc=True) \
            .execute()
        return dict(notificaciones=resp.data or [])
    return dict(notificaciones=[])

@app.route("/nueva_solicitud_cupo")
def nueva_solicitud_cupo():

    # 🔐 Validar sesión
    if "user_id" not in session or session.get("rol") not in ["cobrador", "supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    # 🔒 Validar ruta activa
    if not session.get("ruta_id"):
        flash("Debe seleccionar una ruta", "warning")
        return redirect(url_for("dashboard_cobrador"))

    return render_template("cobrador/nueva_solicitud_cupo.html")


@app.route("/buscar_cliente_por_cedula/<cedula>")
def buscar_cliente_por_cedula(cedula):

    cedula = str(cedula).strip()

    if not cedula:
        return jsonify({"success": False})

    cliente = supabase.table("clientes") \
        .select("id, nombre, identificacion, direccion, telefono_principal") \
        .eq("identificacion", cedula) \
        .limit(1) \
        .execute()

    if cliente.data:
        return jsonify({
            "success": True,
            "cliente": cliente.data[0]
        })
    else:
        return jsonify({"success": False})


@app.route("/guardar_solicitud_cupo", methods=["POST"])
def guardar_solicitud_cupo():

    # 🔐 Validar sesión
    if "user_id" not in session or session.get("rol") not in ["cobrador", "supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    if not session.get("ruta_id"):
        flash("Debe seleccionar una ruta", "warning")
        return redirect(url_for("dashboard_cobrador"))

    tipo_cliente = (request.form.get("tipo_cliente") or "").strip().lower()
    monto_raw = request.form.get("monto", "").strip()

    # ==========================
    # VALIDAR MONTO
    # ==========================
    if not monto_raw:
        flash("Debe ingresar el monto solicitado", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    try:
        monto = float(monto_raw.replace(".", "").replace(",", "."))
        if monto <= 0:
            raise ValueError
    except:
        flash("Monto inválido", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    if tipo_cliente not in ["existente", "nuevo"]:
        flash("Debe seleccionar el tipo de cliente", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    # =========================================================
    # CASO 1: CLIENTE EXISTENTE
    # =========================================================
    if tipo_cliente == "existente":

        cedula = request.form.get("cedula_existente", "").strip()

        if not cedula:
            flash("Debe ingresar la cédula del cliente", "danger")
            return redirect(url_for("nueva_solicitud_cupo"))

        cliente = supabase.table("clientes") \
            .select("id, nombre, identificacion, direccion") \
            .eq("identificacion", cedula) \
            .limit(1) \
            .execute()

        if not cliente.data:
            flash("El cliente no existe en el sistema", "danger")
            return redirect(url_for("nueva_solicitud_cupo"))

        cliente_data = cliente.data[0]

        pendiente = supabase.table("solicitudes_aumento_cupo") \
            .select("id") \
            .eq("cedula", cedula) \
            .eq("estado", "pendiente") \
            .limit(1) \
            .execute()

        if pendiente.data:
            flash("Ya existe una solicitud pendiente para este cliente", "warning")
            return redirect(url_for("buzon_aumento_cupo"))

        insert = supabase.table("solicitudes_aumento_cupo").insert({
            "tipo_cliente": "existente",
            "cliente_id": cliente_data["id"],
            "cliente_nombre": cliente_data["nombre"],
            "cedula": cedula,
            "direccion": cliente_data.get("direccion"),
            "descripcion_actividad": None,
            "monto_solicitado": monto,
            "usuario_id": session["user_id"],
            "ruta_id": session.get("ruta_id"),
            "estado": "pendiente"
        }).execute()

        if not insert.data:
            flash("Error al enviar la solicitud", "danger")
            return redirect(url_for("nueva_solicitud_cupo"))

        flash("Solicitud enviada correctamente", "success")
        return redirect(url_for("buzon_aumento_cupo"))

    # =========================================================
    # CASO 2: CLIENTE NUEVO
    # =========================================================
    nombre_nuevo = request.form.get("nombre_nuevo", "").strip()
    cedula_nuevo = request.form.get("cedula_nuevo", "").strip()
    direccion_nuevo = request.form.get("direccion_nuevo", "").strip()
    descripcion_nuevo = request.form.get("descripcion_nuevo", "").strip()

    if not nombre_nuevo or not cedula_nuevo or not direccion_nuevo or not descripcion_nuevo:
        flash("Todos los campos del cliente nuevo son obligatorios", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    # Validar que no exista ya como cliente
    cliente_existente = supabase.table("clientes") \
        .select("id") \
        .eq("identificacion", cedula_nuevo) \
        .limit(1) \
        .execute()

    if cliente_existente.data:
        flash("Ese cliente ya existe en el sistema. Seleccione 'Cliente existente'.", "warning")
        return redirect(url_for("nueva_solicitud_cupo"))

    # Validar que no tenga solicitud pendiente
    pendiente = supabase.table("solicitudes_aumento_cupo") \
        .select("id") \
        .eq("cedula", cedula_nuevo) \
        .eq("estado", "pendiente") \
        .limit(1) \
        .execute()

    if pendiente.data:
        flash("Ya existe una solicitud pendiente para esta cédula", "warning")
        return redirect(url_for("buzon_aumento_cupo"))

    insert = supabase.table("solicitudes_aumento_cupo").insert({
        "tipo_cliente": "nuevo",
        "cliente_id": None,
        "cliente_nombre": nombre_nuevo,
        "cedula": cedula_nuevo,
        "direccion": direccion_nuevo,
        "descripcion_actividad": descripcion_nuevo,
        "monto_solicitado": monto,
        "usuario_id": session["user_id"],
        "ruta_id": session.get("ruta_id"),
        "estado": "pendiente"
    }).execute()

    if not insert.data:
        flash("Error al enviar la solicitud", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    flash("Solicitud enviada correctamente", "success")
    return redirect(url_for("buzon_aumento_cupo"))

@app.route("/admin/solicitud/procesar", methods=["POST"])
def procesar_solicitud():

    if "user_id" not in session:
        return redirect(url_for("login"))

    solicitud_id = request.form.get("solicitud_id")
    accion = request.form.get("accion")
    motivo = (request.form.get("motivo") or "").strip()

    if accion not in ["aprobado", "rechazado"]:
        flash("Acción inválida", "danger")
        return redirect(url_for("ver_solicitudes_cupo"))

    if not solicitud_id:
        flash("Solicitud inválida", "danger")
        return redirect(url_for("ver_solicitudes_cupo"))

    if not motivo:
        flash("Debe ingresar el motivo de aprobación o rechazo", "warning")
        return redirect(url_for("ver_solicitudes_cupo"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔎 Traer solicitud
    solicitud_resp = supabase.table("solicitudes_aumento_cupo") \
        .select("*") \
        .eq("id", solicitud_id) \
        .single() \
        .execute()

    if not solicitud_resp.data:
        flash("Solicitud no encontrada", "danger")
        return redirect(url_for("ver_solicitudes_cupo"))

    solicitud = solicitud_resp.data

    # 🔒 Validar que pertenezca a una ruta de la oficina activa
    ruta_validacion = supabase.table("rutas") \
        .select("id, oficina_id") \
        .eq("id", solicitud["ruta_id"]) \
        .single() \
        .execute().data

    if not ruta_validacion or ruta_validacion["oficina_id"] != oficina_id:
        flash("No tiene permiso para modificar esta solicitud", "danger")
        return redirect(url_for("ver_solicitudes_cupo"))

    # 🔥 Actualizar estado y motivo
    update_resp = supabase.table("solicitudes_aumento_cupo") \
        .update({
            "estado": accion,
            "motivo_respuesta": motivo
        }) \
        .eq("id", solicitud_id) \
        .execute()

    if not update_resp.data:
        flash("No fue posible actualizar la solicitud", "danger")
        return redirect(url_for("ver_solicitudes_cupo"))

    # 🔔 Crear notificación si existe usuario
    if solicitud.get("usuario_id"):
        texto_accion = "APROBADA" if accion == "aprobado" else "RECHAZADA"

        supabase.table("notificaciones").insert({
            "usuario_id": solicitud["usuario_id"],
            "titulo": "Solicitud de cupo actualizada",
            "mensaje": f"Tu solicitud para {solicitud.get('cliente_nombre', 'cliente')} fue {texto_accion}. Motivo: {motivo}",
            "tipo": "success" if accion == "aprobado" else "danger",
            "leida": False
        }).execute()

    flash("Solicitud actualizada correctamente", "success")
    return redirect(url_for("ver_solicitudes_cupo"))

@app.route("/admin/solicitudes_cupo")
def ver_solicitudes_cupo():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔥 Obtener rutas de esta oficina
    rutas = supabase.table("rutas") \
        .select("id") \
        .eq("oficina_id", oficina_id) \
        .execute().data or []

    rutas_ids = [r["id"] for r in rutas]

    if not rutas_ids:
        return render_template("solicitudes_cupo.html", solicitudes=[])

    # 🔥 Solo solicitudes de rutas de esta oficina
    solicitudes = supabase.table("solicitudes_aumento_cupo") \
        .select("*") \
        .in_("ruta_id", rutas_ids) \
        .order("fecha", desc=True) \
        .execute().data or []

    # Formatear fecha para la vista
    for s in solicitudes:
        fecha_iso = s.get("fecha")

        if fecha_iso:
            try:
                fecha_obj = datetime.fromisoformat(fecha_iso.replace("Z", "+00:00"))
                s["fecha_formateada"] = fecha_obj.strftime("%d/%m/%Y %H:%M")
                s["fecha_filtro"] = fecha_obj.strftime("%Y-%m-%d")
            except:
                s["fecha_formateada"] = fecha_iso
                s["fecha_filtro"] = fecha_iso[:10]
        else:
            s["fecha_formateada"] = ""
            s["fecha_filtro"] = ""

        s["tipo_cliente"] = s.get("tipo_cliente") or "existente"

    return render_template(
        "solicitudes_cupo.html",
        solicitudes=solicitudes
    )
import calendar

def sumar_meses(fecha_dt, meses):
    year = fecha_dt.year + ((fecha_dt.month - 1 + meses) // 12)
    month = ((fecha_dt.month - 1 + meses) % 12) + 1
    day = min(fecha_dt.day, calendar.monthrange(year, month)[1])
    return fecha_dt.replace(year=year, month=month, day=day)

def obtener_url_publica(bucket, file_path):
    url = supabase.storage.from_(bucket).get_public_url(file_path)

    if isinstance(url, dict):
        return url.get("publicUrl") or url.get("publicURL") or url.get("signedURL")

    return url

@app.route("/guardar_venta_cobrador", methods=["POST"])
def guardar_venta_cobrador():

    if "user_id" not in session:
        return redirect(url_for("login_app"))

    rutas_resp = supabase.table("rutas") \
        .select("*") \
        .eq("usuario_id", session["user_id"]) \
        .eq("estado", "true") \
        .order("posicion") \
        .execute()

    rutas = rutas_resp.data if rutas_resp.data else []

    ruta_id = request.form.get("ruta_id")
    session["ruta_id"] = ruta_id

    if not ruta_id:
        flash("No hay ruta activa seleccionada", "danger")
        return redirect(url_for("dashboard_cobrador"))

    form_data_error = request.form.to_dict()
    form_data_error["foto_cliente"] = request.form.get("foto_cliente_actual", "")
    form_data_error["foto_cedula"] = request.form.get("foto_cedula_actual", "")
    form_data_error["foto_negocio"] = request.form.get("foto_negocio_actual", "")
    form_data_error["firma_cliente"] = request.form.get("firma_cliente_actual", "")

    def safe_int(value):
        try:
            return int(str(value).strip())
        except Exception:
            return None

    def safe_float(value, default=0):
        try:
            return float(str(value).replace(".", "").replace(",", ".").strip())
        except Exception:
            return default

    solicitud_id = safe_int(request.form.get("solicitud_id"))
    modo_aumento = str(request.form.get("modo_aumento") or "0").strip() == "1"
    es_renovacion = request.form.get("es_renovacion") == "1"
    monto_aprobado_form = safe_float(request.form.get("monto_aprobado"), 0)

    def render_form_error():
        return render_template(
            "cobrador/nueva_venta_cobrador.html",
            rutas=rutas,
            ruta_actual=ruta_id,
            form_data=form_data_error,
            es_renovacion=es_renovacion,
            monto_aprobado=monto_aprobado_form if monto_aprobado_form > 0 else None,
            cliente_aprobado=None,
            modo_aumento=modo_aumento,
            solicitud_id=solicitud_id
        )

    # ==========================
    # VALIDAR CAMPOS NUMÉRICOS
    # ==========================
    try:
        valor_venta_raw = request.form.get("valor_venta", "").strip()
        tasa_raw = request.form.get("tasa", "").strip()
        cuotas_raw = request.form.get("cuotas", "").strip()
        fecha_inicio = (request.form.get("fecha_inicio") or (date.today() + timedelta(days=1)).isoformat()).strip()

        valor_venta = float(valor_venta_raw.replace(".", "").replace(",", "."))
        tasa = float(tasa_raw.replace(",", "."))
        cuotas = int(cuotas_raw)

        datetime.strptime(fecha_inicio, "%Y-%m-%d")

        if valor_venta <= 0 or cuotas <= 0 or tasa < 0:
            raise ValueError

    except Exception as e:
        print("ERROR NUMERICO:", e)
        flash("Datos numéricos inválidos", "danger")
        return render_form_error()

    identificacion = (request.form.get("identificacion") or "").strip()
    nombre = (request.form.get("nombre") or "").strip()
    direccion = (request.form.get("direccion") or "").strip()
    direccion_negocio = (request.form.get("direccion_negocio") or "").strip()
    codigo_pais = request.form.get("codigo_pais") or "57"
    telefono = (request.form.get("telefono") or "").strip()
    tipo_prestamo = (request.form.get("tipo_prestamo") or "").strip()
    descripcion_credito = (request.form.get("descripcion_credito") or "").strip()

    # ==========================
    # FIADOR
    # ==========================
    requiere_fiador = (request.form.get("requiere_fiador") or "no").strip().lower()
    fiador_nombre = (request.form.get("fiador_nombre") or "").strip()
    fiador_telefono = (request.form.get("fiador_telefono") or "").strip()
    fiador_cedula = (request.form.get("fiador_cedula") or "").strip()

    if requiere_fiador == "si":
        if not fiador_nombre or not fiador_telefono or not fiador_cedula:
            flash("Debes completar los datos del fiador", "danger")
            return render_form_error()
    else:
        fiador_nombre = None
        fiador_telefono = None
        fiador_cedula = None

    foto_cliente_actual = request.form.get("foto_cliente_actual") or None
    foto_cedula_actual = request.form.get("foto_cedula_actual") or None
    foto_negocio_actual = request.form.get("foto_negocio_actual") or None
    firma_cliente_actual = request.form.get("firma_cliente_actual") or None

    # ==========================
    # VALIDAR TOPE PERMITIDO
    # ==========================
    ruta_data = supabase.table("rutas") \
        .select("venta_maxima") \
        .eq("id", ruta_id) \
        .single() \
        .execute()

    if not ruta_data.data:
        flash("Ruta no válida", "danger")
        return redirect(url_for("dashboard_cobrador"))

    venta_maxima_ruta = float(ruta_data.data["venta_maxima"] or 0)

    monto_tope_permitido = venta_maxima_ruta
    origen_tope = "ruta"

    # Si viene de aumento, intentar usar el monto aprobado real
    if modo_aumento and not es_renovacion:
        monto_aprobado_real = monto_aprobado_form

        if solicitud_id:
            solicitud_resp = supabase.table("solicitudes_aumento_cupo") \
                .select("id, monto_solicitado, estado") \
                .eq("id", solicitud_id) \
                .single() \
                .execute()

            solicitud_data = solicitud_resp.data or {}

            if solicitud_data:
                # opcional: validar estado aprobado si tienes ese flujo
                # if solicitud_data.get("estado") != "aprobado":
                #     flash("La solicitud no está aprobada", "danger")
                #     return render_form_error()

                monto_aprobado_real = float(solicitud_data.get("monto_solicitado") or 0)

        if monto_aprobado_real > 0:
            monto_tope_permitido = monto_aprobado_real
            origen_tope = "solicitud_aprobada"

    if valor_venta > monto_tope_permitido:
        if origen_tope == "solicitud_aprobada":
            flash(
                f"El monto supera el cupo aprobado para esta solicitud (${monto_tope_permitido:,.0f})",
                "danger"
            )
        else:
            flash(
                f"El monto supera la venta máxima permitida para esta ruta (${monto_tope_permitido:,.0f})",
                "danger"
            )
        return render_form_error()

    # ==========================
    # ASIGNAR POSICIÓN AUTOMÁTICA POR RUTA
    # ==========================
    posicion_resp = supabase.table("creditos") \
        .select("posicion") \
        .eq("ruta_id", ruta_id) \
        .eq("estado", "activo") \
        .execute()

    posiciones = [
        int(c["posicion"]) for c in (posicion_resp.data or [])
        if c.get("posicion") is not None
    ]

    nueva_posicion = max(posiciones) + 1 if posiciones else 1

    # ==========================
    # EVITAR CRÉDITO DUPLICADO POR CÉDULA
    # ==========================
    cliente_existente_resp = supabase.table("clientes") \
        .select("id") \
        .eq("identificacion", identificacion) \
        .limit(1) \
        .execute()

    if cliente_existente_resp.data:
        cliente_id_existente = cliente_existente_resp.data[0]["id"]

        credito_dup_resp = supabase.table("creditos") \
            .select("*") \
            .eq("cliente_id", cliente_id_existente) \
            .eq("ruta_id", ruta_id) \
            .eq("estado", "activo") \
            .limit(1) \
            .execute()

        if credito_dup_resp.data:
            credito_existente = credito_dup_resp.data[0]
            credito_existente_id = credito_existente["id"]

            cuotas_dup_resp = supabase.table("cuotas") \
                .select("estado") \
                .eq("credito_id", credito_existente_id) \
                .execute()

            cuotas_dup = cuotas_dup_resp.data or []

            pagos_dup_resp = supabase.table("pagos") \
                .select("monto") \
                .eq("credito_id", credito_existente_id) \
                .execute()

            pagos_dup = pagos_dup_resp.data or []

            total_pagado_dup = sum(float(p.get("monto") or 0) for p in pagos_dup)

            saldo_dup = round(float(credito_existente.get("valor_total") or 0) - total_pagado_dup, 2)

            todas_pagadas_dup = len(cuotas_dup) > 0 and all(c.get("estado") == "pagado" for c in cuotas_dup)

            if todas_pagadas_dup and saldo_dup <= 0:
                supabase.table("creditos") \
                    .update({"estado": "finalizado"}) \
                    .eq("id", credito_existente_id) \
                    .execute()
            else:
                flash(
                    "Este cliente (cédula) ya tiene un crédito activo en esta ruta. No se puede registrar duplicado.",
                    "danger"
                )
                return redirect(url_for("detalle_cliente", cliente_id=cliente_id_existente, ruta_id=ruta_id))

    # ==========================
    # CREAR O BUSCAR CLIENTE
    # ==========================
    cliente_resp = supabase.table("clientes") \
        .select("*") \
        .eq("identificacion", identificacion) \
        .limit(1) \
        .execute()

    if cliente_resp.data:
        cliente_id = cliente_resp.data[0]["id"]

        supabase.table("clientes").update({
            "nombre": nombre,
            "direccion": direccion,
            "direccion_negocio": direccion_negocio,
            "telefono_principal": telefono,
            "codigo_pais": codigo_pais
        }).eq("id", cliente_id).execute()

    else:
        nuevo_cliente = supabase.table("clientes").insert({
            "identificacion": identificacion,
            "nombre": nombre,
            "direccion": direccion,
            "direccion_negocio": direccion_negocio,
            "telefono_principal": telefono,
            "codigo_pais": codigo_pais
        }).execute()

        if not nuevo_cliente.data:
            flash("Error creando cliente", "danger")
            return render_form_error()

        cliente_id = nuevo_cliente.data[0]["id"]

    # ==========================
    # PROCESAR FIRMA
    # ==========================
    firma_url = firma_cliente_actual
    firma_base64 = request.form.get("firma_cliente")

    if firma_base64 and "base64," in firma_base64:
        try:
            header, encoded = firma_base64.split(",", 1)
            firma_bytes = base64.b64decode(encoded)

            image = Image.open(BytesIO(firma_bytes)).convert("RGBA")

            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[3])

            firma_filename = f"{cliente_id}_{uuid.uuid4()}_firma.jpg"

            buffer = BytesIO()
            background.save(buffer, format="JPEG", quality=70)
            buffer.seek(0)

            supabase.storage.from_("clientes").upload(
                firma_filename,
                buffer.read(),
                {"content-type": "image/jpeg"}
            )

            firma_url = obtener_url_publica("clientes", firma_filename)

        except Exception as e:
            print("Error procesando firma:", e)

    # ==========================
    # SUBIR FOTOS
    # ==========================
    foto_cliente = request.files.get("foto_cliente")
    foto_cedula = request.files.get("foto_cedula")
    foto_negocio = request.files.get("foto_negocio")

    cliente_url = foto_cliente_actual
    if foto_cliente and foto_cliente.filename:
        try:
            cliente_path = f"{cliente_id}_{uuid.uuid4()}_cliente.jpg"

            supabase.storage.from_("clientes").upload(
                cliente_path,
                foto_cliente.read(),
                {"content-type": foto_cliente.content_type}
            )

            cliente_url = obtener_url_publica("clientes", cliente_path)

        except Exception as e:
            print("Error subiendo foto cliente:", e)

    cedula_url = foto_cedula_actual
    if foto_cedula and foto_cedula.filename:
        try:
            cedula_path = f"{cliente_id}_{uuid.uuid4()}_cedula.jpg"

            supabase.storage.from_("clientes").upload(
                cedula_path,
                foto_cedula.read(),
                {"content-type": foto_cedula.content_type}
            )

            cedula_url = obtener_url_publica("clientes", cedula_path)

        except Exception as e:
            print("Error subiendo cédula:", e)

    negocio_url = foto_negocio_actual
    if foto_negocio and foto_negocio.filename:
        try:
            negocio_path = f"{cliente_id}_{uuid.uuid4()}_negocio.jpg"

            supabase.storage.from_("clientes").upload(
                negocio_path,
                foto_negocio.read(),
                {"content-type": foto_negocio.content_type}
            )

            negocio_url = obtener_url_publica("clientes", negocio_path)

        except Exception as e:
            print("Error subiendo negocio:", e)

    # ==========================
    # UBICACIÓN
    # ==========================
    latitud = request.form.get("latitud")
    longitud = request.form.get("longitud")

    # ==========================
    # CREAR CRÉDITO
    # ==========================
    valor_total = round(valor_venta + (valor_venta * tasa / 100), 2)
    valor_cuota = round(valor_total / cuotas, 2)

    credito_resp = supabase.table("creditos").insert({
        "cliente_id": cliente_id,
        "ruta_id": ruta_id,
        "posicion": nueva_posicion,
        "tipo_prestamo": tipo_prestamo,
        "descripcion": descripcion_credito or None,
        "valor_venta": valor_venta,
        "tasa": tasa,
        "valor_total": valor_total,
        "cantidad_cuotas": cuotas,
        "valor_cuota": valor_cuota,
        "fecha_inicio": fecha_inicio,
        "estado": "activo",
        "foto_cedula": cedula_url,
        "foto_negocio": negocio_url,
        "foto_cliente": cliente_url,
        "firma_cliente": firma_url,
        "latitud": float(latitud) if latitud else None,
        "longitud": float(longitud) if longitud else None,
        "requiere_fiador": True if requiere_fiador == "si" else False,
        "fiador_nombre": fiador_nombre,
        "fiador_telefono": fiador_telefono,
        "fiador_cedula": fiador_cedula
    }).execute()

    if not credito_resp.data:
        flash("Error al registrar el crédito", "danger")
        return render_form_error()

    credito_id = credito_resp.data[0]["id"]

    # ==========================
    # MARCAR SOLICITUD COMO USADA / ATENDIDA
    # ==========================
    if solicitud_id:
        try:
            supabase.table("solicitudes_aumento_cupo").update({
                "estado": "usada"
            }).eq("id", solicitud_id).execute()
        except Exception as e:
            print("Error actualizando solicitud usada:", e)

    # ==========================
    # CREAR CUOTAS SEGÚN TIPO
    # ==========================
    fecha_base = datetime.strptime(fecha_inicio, "%Y-%m-%d")

    if tipo_prestamo == "Semanal":
        for i in range(cuotas):
            fecha_pago = fecha_base + timedelta(days=(i + 1) * 7)
            supabase.table("cuotas").insert({
                "credito_id": credito_id,
                "numero": i + 1,
                "valor": valor_cuota,
                "estado": "pendiente",
                "fecha_pago": fecha_pago.date().isoformat()
            }).execute()

    elif tipo_prestamo == "Quincenal":
        for i in range(cuotas):
            fecha_pago = fecha_base + timedelta(days=(i + 1) * 15)
            supabase.table("cuotas").insert({
                "credito_id": credito_id,
                "numero": i + 1,
                "valor": valor_cuota,
                "estado": "pendiente",
                "fecha_pago": fecha_pago.date().isoformat()
            }).execute()

    elif tipo_prestamo == "Mensual":
        for i in range(cuotas):
            fecha_pago = sumar_meses(fecha_base, i + 1)
            supabase.table("cuotas").insert({
                "credito_id": credito_id,
                "numero": i + 1,
                "valor": valor_cuota,
                "estado": "pendiente",
                "fecha_pago": fecha_pago.date().isoformat()
            }).execute()

    else:
        fecha_actual = fecha_base
        cuotas_creadas = 0

        while cuotas_creadas < cuotas:
            crear_cuota = False

            if tipo_prestamo == "Diario Lunes a Viernes":
                if fecha_actual.weekday() < 5:
                    fecha_pago = fecha_actual
                    crear_cuota = True

            elif tipo_prestamo == "Diario Lunes a Sábado":
                if fecha_actual.weekday() < 6:
                    fecha_pago = fecha_actual
                    crear_cuota = True

            else:
                fecha_pago = fecha_actual
                crear_cuota = True

            if crear_cuota:
                supabase.table("cuotas").insert({
                    "credito_id": credito_id,
                    "numero": cuotas_creadas + 1,
                    "valor": valor_cuota,
                    "estado": "pendiente",
                    "fecha_pago": fecha_pago.date().isoformat()
                }).execute()

                cuotas_creadas += 1

            fecha_actual += timedelta(days=1)

    flash("Venta registrada correctamente", "success")
    return redirect(url_for("ver_ruta", ruta_id=ruta_id))
    
@app.route("/cambiar_posicion", methods=["POST"])
def cambiar_posicion():

    credito_id = request.form.get("credito_id")
    nueva_posicion = int(request.form.get("nueva_posicion"))

    credito = supabase.table("creditos") \
        .select("ruta_id, posicion") \
        .eq("id", credito_id) \
        .single() \
        .execute().data

    ruta_id = credito["ruta_id"]
    vieja_posicion = credito["posicion"]

    if nueva_posicion == vieja_posicion:
        return redirect(url_for("todas_las_ventas"))

    # 1️⃣ sacar temporalmente el crédito
    supabase.table("creditos") \
        .update({"posicion": -9999}) \
        .eq("id", credito_id) \
        .execute()

    if nueva_posicion < vieja_posicion:
        # mover hacia arriba
        creditos = supabase.table("creditos") \
            .select("id, posicion") \
            .eq("ruta_id", ruta_id) \
            .eq("estado", "activo") \
            .gte("posicion", nueva_posicion) \
            .lt("posicion", vieja_posicion) \
            .order("posicion", desc=True) \
            .execute().data

        for c in creditos:
            supabase.table("creditos") \
                .update({"posicion": c["posicion"] + 1}) \
                .eq("id", c["id"]) \
                .execute()

    else:
        # mover hacia abajo
        creditos = supabase.table("creditos") \
            .select("id, posicion") \
            .eq("ruta_id", ruta_id) \
            .eq("estado", "activo") \
            .gt("posicion", vieja_posicion) \
            .lte("posicion", nueva_posicion) \
            .order("posicion") \
            .execute().data

        for c in creditos:
            supabase.table("creditos") \
                .update({"posicion": c["posicion"] - 1}) \
                .eq("id", c["id"]) \
                .execute()

    # 3️⃣ colocar el crédito en su posición final
    supabase.table("creditos") \
        .update({"posicion": nueva_posicion}) \
        .eq("id", credito_id) \
        .execute()

    return redirect(url_for("todas_las_ventas", ruta_id=ruta_id))

@app.route("/rutas/asignar-cobrador", methods=["POST"])
def asignar_cobrador_ruta():

    ruta_id = request.form.get("ruta_id")
    usuario_id = request.form.get("usuario_id")

    if not ruta_id or not usuario_id:
        flash("Datos inválidos", "danger")
        return redirect(url_for("listar_rutas"))

    # 🔥 Validar que el usuario sea cobrador
    user_check = supabase.table("usuarios") \
        .select("rol") \
        .eq("id", usuario_id) \
        .single() \
        .execute()

    if not user_check.data or user_check.data["rol"] != "Cobrador":
        flash("Solo se pueden asignar usuarios con rol Cobrador", "danger")
        return redirect(url_for("listar_rutas"))

    # 🔥 Actualizar ruta
    supabase.table("rutas") \
        .update({"usuario_id": usuario_id}) \
        .eq("id", ruta_id) \
        .execute()

    flash("Cobrador asignado correctamente", "success")
    return redirect(url_for("listar_rutas"))

# listar todas las ventas en el motudlo de cobrador

@app.route("/eliminar_credito/<credito_id>")
def eliminar_credito(credito_id):

    if "user_id" not in session:
        return redirect(url_for("login_app"))

    try:

        # 🔹 1. Eliminar pagos
        supabase.table("pagos") \
            .delete() \
            .eq("credito_id", credito_id) \
            .execute()

        # 🔹 2. Eliminar cuotas
        supabase.table("cuotas") \
            .delete() \
            .eq("credito_id", credito_id) \
            .execute()

        # 🔹 3. Eliminar crédito
        supabase.table("creditos") \
            .delete() \
            .eq("id", credito_id) \
            .execute()

        flash("Venta eliminada completamente.", "success")

    except Exception as e:
        print("Error eliminando crédito:", e)
        flash("Ocurrió un error al eliminar la venta.", "danger")

    return redirect(request.referrer)

@app.route("/todas_las_ventas/<ruta_id>")
def todas_las_ventas(ruta_id):

    if "user_id" not in session or session.get("rol") not in ["cobrador", "supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    hoy_fecha = date.today()
    hoy = hoy_fecha.isoformat()
    manana = (hoy_fecha + timedelta(days=1)).isoformat()

    def parse_fecha(fecha):
        try:
            return date.fromisoformat(str(fecha)[:10])
        except Exception:
            return None

    def money(valor):
        try:
            return "{:,.0f}".format(float(valor or 0))
        except Exception:
            return "0"

    def format_fecha(fecha):
        try:
            f = str(fecha)
            return f"{f[8:10]}/{f[5:7]}/{f[:4]}"
        except Exception:
            return fecha or ""

    def tipo_to_grupo(tipo_prestamo):
        tipo = str(tipo_prestamo or "").strip().lower()

        if "semanal" in tipo:
            return "semanal"

        if "quincenal" in tipo:
            return "quincenal"

        if "mensual" in tipo:
            return "mensual"

        if "diario" in tipo and "viernes" in tipo:
            return "diario_lv"

        if "diario" in tipo and ("sábado" in tipo or "sabado" in tipo):
            return "diario_ls"

        if "diario" in tipo:
            return "diario"

        return "otro"

    stats = {
        "total": 0,
        "activos": 0,
        "cobrar_hoy": 0,
        "mora": 0,
        "pagados_hoy": 0,
        "finalizados": 0,
        "semanal": 0,
        "quincenal": 0,
        "mensual": 0,
        "diario": 0,
        "diario_lv": 0,
        "diario_ls": 0,
        "otro": 0,
    }

    # ==========================
    # 1. TRAER CRÉDITOS DE LA RUTA
    # ==========================
    creditos_resp = supabase.table("creditos") \
        .select("""
            id,
            cliente_id,
            ruta_id,
            posicion,
            valor_cuota,
            valor_total,
            valor_venta,
            tipo_prestamo,
            estado,
            created_at,
            clientes(
                nombre,
                identificacion,
                telefono_principal
            )
        """) \
        .eq("ruta_id", ruta_id) \
        .order("posicion") \
        .execute()

    creditos_db = creditos_resp.data or []

    if not creditos_db:
        return render_template(
            "cobrador/todas_las_ventas.html",
            creditos=[],
            ruta_id=ruta_id,
            stats=stats
        )

    credito_ids = [c["id"] for c in creditos_db]

    # ==========================
    # 2. TRAER CUOTAS DE TODOS LOS CRÉDITOS
    # ==========================
    cuotas_resp = supabase.table("cuotas") \
        .select("id, credito_id, numero, estado, valor, fecha_pago, monto_pagado") \
        .in_("credito_id", credito_ids) \
        .order("fecha_pago") \
        .execute()

    cuotas_db = cuotas_resp.data or []

    cuotas_por_credito = defaultdict(list)

    for cuota in cuotas_db:
        cuotas_por_credito[cuota["credito_id"]].append(cuota)

    # ==========================
    # 3. TRAER PAGOS DE HOY DE TODOS LOS CRÉDITOS
    # ==========================
    pagos_hoy_resp = supabase.table("pagos") \
        .select("id, credito_id, monto, fecha") \
        .in_("credito_id", credito_ids) \
        .gte("fecha", hoy + "T00:00:00") \
        .lt("fecha", manana + "T00:00:00") \
        .execute()

    pagos_hoy_db = pagos_hoy_resp.data or []

    pagos_hoy_por_credito = defaultdict(list)

    for pago in pagos_hoy_db:
        pagos_hoy_por_credito[pago["credito_id"]].append(pago)

    # ==========================
    # 4. ARMAR LISTA PARA LA VISTA
    # ==========================
    lista = []

    for c in creditos_db:

        cliente = c.get("clientes") or {}
        cuotas = cuotas_por_credito.get(c["id"], [])
        pagos_hoy = pagos_hoy_por_credito.get(c["id"], [])

        ya_pago_hoy = len(pagos_hoy) > 0
        total_pagado_hoy = sum(float(p.get("monto") or 0) for p in pagos_hoy)

        proxima_cuota = None
        valor_hoy = 0
        saldo_pendiente = 0
        fechas_vencidas = []

        tiene_cuota_hoy = False
        cuota_hoy_pendiente = False

        todas_pagadas = True if cuotas else False

        for cuota in cuotas:

            fecha_cuota = parse_fecha(cuota.get("fecha_pago"))
            valor_cuota = float(cuota.get("valor") or 0)
            monto_pagado = float(cuota.get("monto_pagado") or 0)

            cuota_pagada = cuota.get("estado") == "pagado" or monto_pagado >= valor_cuota
            cuota_pendiente = not cuota_pagada

            if cuota_pendiente:
                todas_pagadas = False

                restante = max(valor_cuota - monto_pagado, 0)
                saldo_pendiente += restante

                if not proxima_cuota:
                    proxima_cuota = cuota.get("fecha_pago")

                if fecha_cuota and fecha_cuota < hoy_fecha:
                    fechas_vencidas.append(fecha_cuota)

            if fecha_cuota == hoy_fecha:
                tiene_cuota_hoy = True

                if cuota_pendiente:
                    cuota_hoy_pendiente = True
                    valor_hoy += max(valor_cuota - monto_pagado, 0)

        dias_mora = 0

        if fechas_vencidas:
            fecha_mas_antigua = min(fechas_vencidas)
            dias_mora = (hoy_fecha - fecha_mas_antigua).days

        estado_credito = str(c.get("estado") or "").lower()

        finalizado = (
            estado_credito in ["finalizado", "pagado"]
            or todas_pagadas
        )

        en_mora = dias_mora > 0 and not finalizado

        cobrar_hoy = (
            cuota_hoy_pendiente
            and not ya_pago_hoy
            and not finalizado
        )

        activo = not finalizado

        tipo_prestamo = c.get("tipo_prestamo") or "Sin tipo"
        tipo_grupo = tipo_to_grupo(tipo_prestamo)

        diario_total = tipo_grupo in ["diario", "diario_lv", "diario_ls"]

        if finalizado:
            color_estado = "azul"
            texto_estado = "Finalizado"
            prioridad_estado = 5
        elif dias_mora >= 30:
            color_estado = "rojo"
            texto_estado = f"Mora alta {dias_mora} días"
            prioridad_estado = 1
        elif dias_mora >= 7:
            color_estado = "naranja"
            texto_estado = f"Mora media {dias_mora} días"
            prioridad_estado = 2
        elif dias_mora > 0:
            color_estado = "amarillo"
            texto_estado = f"Atraso {dias_mora} días"
            prioridad_estado = 3
        elif cobrar_hoy:
            color_estado = "verde"
            texto_estado = "Cobrar hoy"
            prioridad_estado = 0
        elif ya_pago_hoy:
            color_estado = "verde"
            texto_estado = "Pagó hoy"
            prioridad_estado = 4
        else:
            color_estado = "gris"
            texto_estado = "Al día"
            prioridad_estado = 4

        posicion_raw = c.get("posicion")

        try:
            posicion_num = int(posicion_raw)
        except Exception:
            posicion_num = 999999

        created_at = c.get("created_at") or ""

        item = {
            "id": c["id"],
            "posicion": posicion_raw or "-",
            "posicion_num": posicion_num,

            "cliente": cliente.get("nombre") or "Sin nombre",
            "identificacion": cliente.get("identificacion") or "",
            "telefono": cliente.get("telefono_principal") or "",

            "valor_total": float(c.get("valor_total") or 0),
            "valor_total_fmt": money(c.get("valor_total")),

            "valor_hoy": valor_hoy,
            "valor_hoy_fmt": money(valor_hoy),

            "saldo_pendiente": saldo_pendiente,
            "saldo_pendiente_fmt": money(saldo_pendiente),

            "total_pagado_hoy": total_pagado_hoy,
            "total_pagado_hoy_fmt": money(total_pagado_hoy),

            "tipo_prestamo": tipo_prestamo,
            "tipo_grupo": tipo_grupo,
            "diario_total": diario_total,

            "proxima_cuota": proxima_cuota,
            "proxima_cuota_fmt": format_fecha(proxima_cuota) if proxima_cuota else "",

            "created_at": created_at,
            "created_at_fmt": format_fecha(created_at),

            "estado": estado_credito,
            "activo": activo,
            "finalizado": finalizado,
            "en_mora": en_mora,
            "dias_mora": dias_mora,
            "cobrar_hoy": cobrar_hoy,
            "ya_pago_hoy": ya_pago_hoy,
            "tiene_cuota_hoy": tiene_cuota_hoy,

            "color_estado": color_estado,
            "texto_estado": texto_estado,
            "prioridad_estado": prioridad_estado,
        }

        lista.append(item)

        stats["total"] += 1

        if activo:
            stats["activos"] += 1

        if cobrar_hoy:
            stats["cobrar_hoy"] += 1

        if en_mora:
            stats["mora"] += 1

        if ya_pago_hoy:
            stats["pagados_hoy"] += 1

        if finalizado:
            stats["finalizados"] += 1

        if tipo_grupo in stats:
            stats[tipo_grupo] += 1
        else:
            stats["otro"] += 1

        if diario_total:
            stats["diario"] += 1

    lista.sort(key=lambda x: (
        1 if x["finalizado"] else 0,
        x["prioridad_estado"],
        -x["dias_mora"],
        x["posicion_num"]
    ))

    return render_template(
        "cobrador/todas_las_ventas.html",
        creditos=lista,
        ruta_id=ruta_id,
        stats=stats
    )

@app.route("/liquidacion")
def liquidacion():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")
    ruta_id_filtro = request.args.get("ruta_id")

    hoy = date.today()

    if fecha_inicio and fecha_fin:
        inicio = datetime.fromisoformat(fecha_inicio + "T00:00:00")
        fin = datetime.fromisoformat(fecha_fin + "T23:59:59")
    else:
        inicio = datetime.combine(hoy, time.min)
        fin = datetime.combine(hoy, time.max)
        fecha_inicio = hoy.isoformat()
        fecha_fin = hoy.isoformat()

    # =============================
    # RUTAS ACTIVAS DE LA OFICINA
    # =============================
    rutas_query = supabase.table("rutas") \
        .select("id, nombre") \
        .eq("oficina_id", oficina_id) \
        .execute()

    rutas_db = rutas_query.data or []

    if ruta_id_filtro:
        rutas_db = [r for r in rutas_db if str(r["id"]) == str(ruta_id_filtro)]

    lista_rutas = []
    total_estimado_intereses_general = 0
    total_ganancia_alcanzada_general = 0
    total_saldo_anterior = 0
    total_transferencias_recibidas = 0
    total_capital_entregado = 0
    total_saldo_final = 0
    total_cartera_final = 0
    total_creditos_colocados = 0
    total_saldo_por_recuperar = 0
    total_gastos_general = 0

    for r in rutas_db:

        ruta_id = r["id"]

        # =============================
        # 1. SALDO ANTERIOR AL INICIO
        # =============================
        cierre_anterior = supabase.table("caja_diaria") \
            .select("saldo_cierre") \
            .eq("ruta_id", ruta_id) \
            .lt("fecha", fecha_inicio) \
            .order("fecha", desc=True) \
            .limit(1) \
            .execute()

        saldo_anterior = 0
        if cierre_anterior.data:
            saldo_anterior = float(cierre_anterior.data[0]["saldo_cierre"] or 0)

        # =============================
        # 2. TRANSFERENCIAS RECIBIDAS
        # =============================
        transferencias_recibidas = supabase.table("transferencias") \
            .select("valor, created_at") \
            .eq("ruta_destino", ruta_id) \
            .gte("created_at", inicio.isoformat()) \
            .lte("created_at", fin.isoformat()) \
            .execute().data or []

        total_recibido_transferencias = sum(
            float(t["valor"] or 0)
            for t in transferencias_recibidas
        )

        # =============================
        # 3. CAPITAL ENTREGADO
        # =============================
        capital_entregado_db = supabase.table("capital") \
            .select("valor, created_at") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio.isoformat()) \
            .lte("created_at", fin.isoformat()) \
            .execute().data or []

        total_capital_entregado_rango = sum(
            float(c["valor"] or 0)
            for c in capital_entregado_db
        )
        # =============================
        # 4. COBROS
        # =============================
        pagos_rango = supabase.table("pagos") \
            .select("monto, fecha, creditos!inner(ruta_id)") \
            .eq("creditos.ruta_id", ruta_id) \
            .gte("fecha", inicio.isoformat()) \
            .lte("fecha", fin.isoformat()) \
            .execute()

        pagos_rango = pagos_rango.data or []

        total_cobros = sum(
            float(p.get("monto") or 0)
            for p in pagos_rango
        )

        # =============================
        # 5. PRÉSTAMOS
        # =============================
        prestamos_rango = supabase.table("creditos") \
            .select("valor_venta, created_at") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio.isoformat()) \
            .lte("created_at", fin.isoformat()) \
            .execute().data or []

        total_prestamos_rango = sum(
            float(p["valor_venta"] or 0)
            for p in prestamos_rango
        )

        # =============================
        # 6. GASTOS
        # =============================
        gastos_rango = supabase.table("gastos") \
            .select("valor, created_at") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio.isoformat()) \
            .lte("created_at", fin.isoformat()) \
            .execute().data or []

        total_gastos_rango = sum(
            float(g["valor"] or 0)
            for g in gastos_rango
        )

        # =============================
        # 7. TRANSFERENCIAS ENVIADAS
        # =============================
        transferencias_enviadas = supabase.table("transferencias") \
            .select("valor, created_at") \
            .eq("ruta_origen", ruta_id) \
            .gte("created_at", inicio.isoformat()) \
            .lte("created_at", fin.isoformat()) \
            .execute().data or []

        total_enviado_transferencias = sum(
            float(t["valor"] or 0)
            for t in transferencias_enviadas
        )

        # =============================
        # 8. SALDO FINAL CAJA
        # =============================
        saldo_final = (
            saldo_anterior
            + total_recibido_transferencias
            + total_capital_entregado_rango
            + total_cobros
            - total_prestamos_rango
            - total_gastos_rango
            - total_enviado_transferencias
        )

        # =============================
        # 9. CRÉDITOS / CARTERA
        # =============================
        creditos_ruta = supabase.table("creditos") \
            .select("id, valor_venta, valor_total, estado") \
            .eq("ruta_id", ruta_id) \
            .execute().data or []

        total_creditos_solo_capital = 0
        saldo_por_recuperar_ruta = 0
        total_cobrado_historico = 0
        total_estimado_con_intereses = 0
        total_ganancia_alcanzada = 0
        total_cobros_general = 0

        for credito in creditos_ruta:

            credito_id = credito["id"]

            cuotas = supabase.table("cuotas") \
                .select("valor, monto_pagado") \
                .eq("credito_id", credito_id) \
                .execute().data or []

            pagado_credito = sum(
                float(c.get("monto_pagado") or 0)
                for c in cuotas
            )

            valor_venta = float(credito.get("valor_venta") or 0)
            valor_total = float(credito.get("valor_total") or 0)

            saldo_capital_credito = valor_venta - pagado_credito
            saldo_total_credito = valor_total - pagado_credito

            if saldo_capital_credito > 0:
                total_creditos_solo_capital += saldo_capital_credito

            if saldo_total_credito > 0:
                saldo_por_recuperar_ruta += saldo_total_credito

            total_cobrado_historico += pagado_credito

            total_estimado_con_intereses += valor_total

            ganancia_esperada_credito = valor_total - valor_venta

            porcentaje_recuperado = 0
            if valor_total > 0:
                porcentaje_recuperado = pagado_credito / valor_total

            if porcentaje_recuperado > 1:
                porcentaje_recuperado = 1

            ganancia_alcanzada_credito = ganancia_esperada_credito * porcentaje_recuperado
            total_ganancia_alcanzada += ganancia_alcanzada_credito

        # =============================
        # 10. CARTERA FINAL
        # cuotas pagadas + gastos
        # =============================
        cartera_final = total_cobrado_historico + total_gastos_rango

        lista_rutas.append({
            "ruta_id": ruta_id,
            "ruta_nombre": r["nombre"],
            "saldo_anterior": saldo_anterior,
            "transferencias_recibidas": total_recibido_transferencias,
            "gastos": total_gastos_rango,
            "capital_entregado": total_capital_entregado_rango,
            "saldo_final": saldo_final,
            "cartera_final": cartera_final,
            "creditos_colocados": total_creditos_solo_capital,
            "saldo_por_recuperar": saldo_por_recuperar_ruta,
            "total_estimado_con_intereses": total_estimado_con_intereses,
            "ganancia_alcanzada": total_ganancia_alcanzada,
            "total_cobros": total_cobros,
            "total_prestamos": total_prestamos_rango,
            "total_gastos": total_gastos_rango,
            "transferencias_enviadas": total_enviado_transferencias
        })

        total_saldo_anterior += saldo_anterior
        total_transferencias_recibidas += total_recibido_transferencias
        total_capital_entregado += total_capital_entregado_rango
        total_saldo_final += saldo_final
        total_cartera_final += cartera_final
        total_creditos_colocados += total_creditos_solo_capital
        total_saldo_por_recuperar += saldo_por_recuperar_ruta
        total_estimado_intereses_general += total_estimado_con_intereses
        total_ganancia_alcanzada_general += total_ganancia_alcanzada
        total_gastos_general += total_gastos_rango
        total_cobros_general += total_cobros

    cantidad_rutas = len(lista_rutas) if lista_rutas else 1

    promedio_saldo_rutas = total_saldo_final / cantidad_rutas
    promedio_cartera_final = total_cartera_final / cantidad_rutas
    promedio_creditos_colocados = total_creditos_colocados / cantidad_rutas
    promedio_saldo_recuperar = total_saldo_por_recuperar / cantidad_rutas

    return render_template(
        "liquidacion.html",
        rutas=lista_rutas,
        rutas_activas=rutas_query.data or [],
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        ruta_id_filtro=ruta_id_filtro,
        promedio_saldo_rutas=promedio_saldo_rutas,
        promedio_cartera_final=promedio_cartera_final,
        promedio_creditos_colocados=promedio_creditos_colocados,
        promedio_saldo_recuperar=promedio_saldo_recuperar,
        total_saldo_anterior=total_saldo_anterior,
        total_transferencias_recibidas=total_transferencias_recibidas,
        total_capital_entregado=total_capital_entregado,
        total_saldo_final=total_saldo_final,
        total_cartera_final=total_cartera_final,
        total_creditos_colocados=total_creditos_colocados,
        total_saldo_por_recuperar=total_saldo_por_recuperar,
        total_estimado_intereses_general=total_estimado_intereses_general,
        total_ganancia_alcanzada_general=total_ganancia_alcanzada_general,
        total_gastos_general=total_gastos_general,
        total_cobros=total_cobros_general
    )
@app.route("/caja_oficina")
def caja_oficina():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")

    hoy = date.today()

    if fecha_inicio and fecha_fin:
        inicio = datetime.fromisoformat(fecha_inicio + "T00:00:00")
        fin = datetime.fromisoformat(fecha_fin + "T23:59:59")
    else:
        inicio = datetime.combine(hoy, time.min)
        fin = datetime.combine(hoy, time.max)

    rutas_db = supabase.table("rutas") \
        .select("id, nombre") \
        .eq("oficina_id", oficina_id) \
        .execute().data or []

    lista_rutas = []
    saldo_total_consolidado = 0

    for r in rutas_db:

        ruta_id = r["id"]

        # =============================
        # CAPITAL ASIGNADO
        # =============================

        capital_resp = supabase.table("capital") \
            .select("valor") \
            .eq("ruta_id", ruta_id) \
            .execute().data or []

        capital_asignado = sum(float(c["valor"] or 0) for c in capital_resp)

        # =============================
        # CRÉDITOS HISTÓRICOS DE LA RUTA
        # =============================

        creditos_historicos = supabase.table("creditos") \
            .select("id, valor_venta, estado") \
            .eq("ruta_id", ruta_id) \
            .execute().data or []

        total_prestamos_historicos = sum(
            float(c["valor_venta"] or 0)
            for c in creditos_historicos
        )

        credito_ids = [c["id"] for c in creditos_historicos]

        # =============================
        # COBROS HISTÓRICOS DE LA RUTA
        # =============================

        total_cobros_historicos = 0

        if credito_ids:
            pagos_historicos = supabase.table("pagos") \
                .select("monto, credito_id") \
                .in_("credito_id", credito_ids) \
                .execute().data or []

            total_cobros_historicos = sum(
                float(p["monto"] or 0)
                for p in pagos_historicos
            )

        # =============================
        # CAPITAL COLOCADO ACTUAL
        # =============================

        creditos_activos = [c for c in creditos_historicos if c.get("estado") == "activo"]

        capital_colocado = 0

        for credito in creditos_activos:

            pagos = supabase.table("pagos") \
                .select("monto") \
                .eq("credito_id", credito["id"]) \
                .execute().data or []

            total_pagado = sum(float(p["monto"] or 0) for p in pagos)

            saldo_capital = float(credito["valor_venta"] or 0) - total_pagado

            if saldo_capital > 0:
                capital_colocado += saldo_capital

        # =============================
        # TRANSFERENCIAS
        # =============================

        transferencias_recibidas = supabase.table("transferencias") \
            .select("valor") \
            .eq("ruta_destino", ruta_id) \
            .execute().data or []

        total_transferencias_recibidas = sum(
            float(t["valor"] or 0)
            for t in transferencias_recibidas
        )

        transferencias_enviadas = supabase.table("transferencias") \
            .select("valor") \
            .eq("ruta_origen", ruta_id) \
            .execute().data or []

        total_transferencias_enviadas = sum(
            float(t["valor"] or 0)
            for t in transferencias_enviadas
        )

        # =============================
        # GASTOS HISTÓRICOS
        # =============================

        gastos_totales = supabase.table("gastos") \
            .select("valor") \
            .eq("ruta_id", ruta_id) \
            .execute().data or []

        total_gastos_ruta = sum(
            float(g["valor"] or 0)
            for g in gastos_totales
        )

        # =============================
        # CAPITAL DISPONIBLE REAL
        # 🔥 AJUSTE: ahora sí suma cobros históricos
        # y resta préstamos históricos
        # =============================

        capital_disponible = (
            capital_asignado
            + total_transferencias_recibidas
            - total_transferencias_enviadas
            + total_cobros_historicos
            - total_prestamos_historicos
            - total_gastos_ruta
        )

        saldo_total_consolidado += capital_disponible

        # =============================
        # MOVIMIENTO POR FECHA
        # =============================

        pagos = supabase.table("pagos") \
            .select("monto, fecha, creditos!inner(ruta_id)") \
            .eq("creditos.ruta_id", ruta_id) \
            .gte("fecha", inicio.isoformat()) \
            .lte("fecha", fin.isoformat()) \
            .execute().data or []

        total_cobros = sum(float(p["monto"] or 0) for p in pagos)

        prestamos = supabase.table("creditos") \
            .select("valor_venta") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio.isoformat()) \
            .lte("created_at", fin.isoformat()) \
            .execute().data or []

        total_prestamos = sum(float(p["valor_venta"] or 0) for p in prestamos)

        gastos = supabase.table("gastos") \
            .select("valor") \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio.isoformat()) \
            .lte("created_at", fin.isoformat()) \
            .execute().data or []

        total_gastos = sum(float(g["valor"] or 0) for g in gastos)

        saldo_actual = total_cobros - total_prestamos - total_gastos

        lista_rutas.append({
            "ruta_id": ruta_id,
            "ruta_nombre": r["nombre"],
            "capital_asignado": capital_asignado,
            "capital_colocado": capital_colocado,
            "capital_disponible": capital_disponible,
            "saldo_actual": saldo_actual,
            "total_cobros": total_cobros,
            "total_prestamos": total_prestamos,
            "total_gastos": total_gastos
        })

    return render_template(
        "cajas.html",
        rutas=lista_rutas,
        saldo_total=saldo_total_consolidado,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin
    )
@app.route("/caja/ruta/<ruta_id>")
def detalle_caja_ruta(ruta_id):

    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")

    if not fecha_inicio or not fecha_fin:
        hoy = date.today().isoformat()
        fecha_inicio = hoy
        fecha_fin = hoy

    inicio = datetime.fromisoformat(fecha_inicio + "T00:00:00")
    fin = datetime.fromisoformat(fecha_fin + "T23:59:59")

    # =====================================================
    # 🔹 SALDO ANTERIOR
    # =====================================================

    cierre_resp = supabase.table("caja_diaria") \
        .select("saldo_cierre") \
        .eq("ruta_id", ruta_id) \
        .lt("fecha", fecha_inicio) \
        .order("fecha", desc=True) \
        .limit(1) \
        .execute()

    saldo_anterior = 0
    if cierre_resp.data:
        saldo_anterior = float(cierre_resp.data[0]["saldo_cierre"] or 0)

    # =====================================================
    # 🔹 COBROS
    # =====================================================

    cobros = supabase.table("pagos") \
        .select("""
            monto,
            fecha,
            cobrador_id,
            creditos!inner(
                ruta_id,
                clientes(nombre)
            )
        """) \
        .eq("creditos.ruta_id", ruta_id) \
        .gte("fecha", inicio.isoformat()) \
        .lte("fecha", fin.isoformat()) \
        .order("fecha", desc=True) \
        .execute().data or []

    total_cobros = sum(float(c["monto"] or 0) for c in cobros)

    # =====================================================
    # 🔹 PRÉSTAMOS
    # =====================================================

    prestamos = supabase.table("creditos") \
        .select("""
            id,
            valor_venta,
            created_at,
            tipo_prestamo,
            clientes(nombre)
        """) \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", inicio.isoformat()) \
        .lte("created_at", fin.isoformat()) \
        .execute().data or []

    total_prestamos = sum(float(p["valor_venta"] or 0) for p in prestamos)

    # =====================================================
    # 🔹 GASTOS
    # =====================================================

    gastos = supabase.table("gastos") \
        .select("categoria_id, descripcion, valor, created_at") \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", inicio.isoformat()) \
        .lte("created_at", fin.isoformat()) \
        .execute().data or []

    total_gastos = sum(float(g["valor"] or 0) for g in gastos)

    # =====================================================
    # 🔹 TRANSFERENCIAS (RECIBIDAS)
    # =====================================================

    transferencias = supabase.table("transferencias") \
        .select("*") \
        .eq("ruta_destino", ruta_id) \
        .gte("fecha", inicio.isoformat()) \
        .lte("fecha", fin.isoformat()) \
        .execute().data or []

    total_transferencias = sum(float(t["valor"] or 0) for t in transferencias)

    # =====================================================
    # 🔹 SALDO DEL PERIODO
    # =====================================================

    saldo_actual = (
        saldo_anterior
        + total_cobros
        + total_transferencias
        - total_prestamos
        - total_gastos
    )

    # =====================================================
    # 🔥 CAPITAL HISTÓRICO DE LA RUTA
    # =====================================================

    # CAPITAL ASIGNADO
    capital_resp = supabase.table("capital") \
        .select("valor") \
        .eq("ruta_id", ruta_id) \
        .execute().data or []

    capital_asignado = sum(float(c["valor"] or 0) for c in capital_resp)

    # CAPITAL COLOCADO (SOLO CAPITAL SIN INTERÉS)
    creditos_activos = supabase.table("creditos") \
        .select("id, valor_venta") \
        .eq("ruta_id", ruta_id) \
        .eq("estado", "activo") \
        .execute().data or []

    capital_colocado = 0

    for credito in creditos_activos:

        pagos_credito = supabase.table("pagos") \
            .select("monto") \
            .eq("credito_id", credito["id"]) \
            .execute().data or []

        total_pagado = sum(float(p["monto"] or 0) for p in pagos_credito)

        saldo_capital = float(credito["valor_venta"] or 0) - total_pagado

        if saldo_capital > 0:
            capital_colocado += saldo_capital

    # TRANSFERENCIAS HISTÓRICAS
    transferencias_recibidas = supabase.table("transferencias") \
        .select("valor") \
        .eq("ruta_destino", ruta_id) \
        .execute().data or []

    total_transferencias_recibidas = sum(
        float(t["valor"] or 0)
        for t in transferencias_recibidas
    )

    transferencias_enviadas = supabase.table("transferencias") \
        .select("valor") \
        .eq("ruta_origen", ruta_id) \
        .execute().data or []

    total_transferencias_enviadas = sum(
        float(t["valor"] or 0)
        for t in transferencias_enviadas
    )

    # GASTOS HISTÓRICOS
    gastos_totales = supabase.table("gastos") \
        .select("valor") \
        .eq("ruta_id", ruta_id) \
        .execute().data or []

    total_gastos_ruta = sum(
        float(g["valor"] or 0)
        for g in gastos_totales
    )

    # CAPITAL DISPONIBLE REAL
    capital_disponible = (
        capital_asignado
        + total_transferencias_recibidas
        - total_transferencias_enviadas
        - capital_colocado
        - total_gastos_ruta
    )

    # =====================================================
    # 🔹 RENDER
    # =====================================================

    return render_template(
        "detalle_caja_ruta.html",
        saldo_anterior=saldo_anterior or 0,
        total_cobros=total_cobros or 0,
        total_prestamos=total_prestamos or 0,
        total_gastos=total_gastos or 0,
        total_transferencias=total_transferencias or 0,
        saldo_actual=saldo_actual or 0,
        capital_asignado=capital_asignado or 0,
        capital_colocado=capital_colocado or 0,
        capital_disponible=capital_disponible or 0,
        cobros=cobros,
        prestamos=prestamos,
        gastos=gastos,
        transferencias=transferencias,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin
    )


@app.route("/caja_reportes")
def caja_reportes():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")
    ruta_id = request.args.get("ruta_id")

    # 🔹 Valores por defecto
    if not fecha_inicio or not fecha_fin:
        hoy = date.today().isoformat()
        fecha_inicio = hoy
        fecha_fin = hoy

    inicio = fecha_inicio + "T00:00:00"
    fin = fecha_fin + "T23:59:59"

    # 🔥 SOLO rutas de la oficina activa
    rutas = supabase.table("rutas") \
        .select("id, nombre") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute().data or []

    ventas = []
    ventas_finalizadas = []
    ventas_activas = []

    if ruta_id:

        # 🔒 VALIDAR QUE LA RUTA PERTENEZCA A LA OFICINA
        ruta_validacion = supabase.table("rutas") \
            .select("id, oficina_id") \
            .eq("id", ruta_id) \
            .single() \
            .execute().data

        if not ruta_validacion or ruta_validacion["oficina_id"] != oficina_id:
            flash("No tiene acceso a esta ruta", "error")
            return redirect(url_for("caja_reportes"))

        # 🔹 Traer créditos filtrados por ruta y fecha
        response = supabase.table("creditos") \
            .select("""
                id,
                valor_total,
                valor_venta,
                tasa,
                cantidad_cuotas,
                tipo_prestamo,
                estado,
                created_at,
                clientes(nombre)
            """) \
            .eq("ruta_id", ruta_id) \
            .gte("created_at", inicio) \
            .lte("created_at", fin) \
            .execute()

        ventas = response.data or []

        for v in ventas:

            total_interes = float(v["valor_total"]) - float(v["valor_venta"])

            # 🔹 Calcular saldo actual
            pagos = supabase.table("pagos") \
                .select("monto") \
                .eq("credito_id", v["id"]) \
                .execute().data or []

            total_pagado = sum(float(p["monto"] or 0) for p in pagos)
            saldo_actual = float(v["valor_total"]) - total_pagado

            v["valor_interes"] = total_interes
            v["saldo_actual"] = saldo_actual

            if v["estado"] in ["finalizado", "pagado"]:
                ventas_finalizadas.append(v)
            else:
                ventas_activas.append(v)

    return render_template(
        "caja_reportes.html",
        rutas=rutas,
        ventas=ventas,
        ventas_activas=ventas_activas,
        ventas_finalizadas=ventas_finalizadas,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        ruta_id=ruta_id
    )

@app.route("/categorias_gastos")
def categorias_gastos():

    categorias = supabase.table("categorias_gastos") \
        .select("*") \
        .eq("estado", True) \
        .order("nombre") \
        .execute().data or []

    return render_template(
        "categorias_gastos.html",
        categorias=categorias
    )

@app.route("/guardar_categoria_gasto", methods=["POST"])
def guardar_categoria_gasto():

    nombre = request.form.get("nombre")
    descripcion = request.form.get("descripcion")

    supabase.table("categorias_gastos").insert({
        "nombre": nombre,
        "descripcion": descripcion
    }).execute()

    return redirect(url_for("categorias_gastos"))


    # =============================
    # CAJA COBRADOR
    # =============================
@app.route("/caja_cobrador")
def caja_cobrador():

    if "user_id" not in session or session.get("rol") not in ["cobrador","supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    ruta_id = session.get("ruta_id")

    if not ruta_id:
        return redirect(url_for("dashboard_cobrador"))

    # =====================================================
    # FECHA COLOMBIA (UTC-5)
    # =====================================================

    ahora_utc = datetime.utcnow()

    # Hora colombiana
    ahora_col = ahora_utc - timedelta(hours=5)

    hoy_col = ahora_col.date()
    hoy_iso = hoy_col.isoformat()

    # Ventana del día colombiano convertida a UTC
    inicio_dia = hoy_iso + "T05:00:00"
    fin_dia = (hoy_col + timedelta(days=1)).isoformat() + "T05:00:00"

    print("VENTANA UTC:")
    print("INICIO:", inicio_dia)
    print("FIN:", fin_dia)


    # =====================================================
    # SALDO ANTERIOR (CIERRE DE CAJA ANTERIOR)
    # =====================================================

    cierre_resp = supabase.table("caja_diaria") \
        .select("saldo_cierre") \
        .eq("ruta_id", ruta_id) \
        .lt("fecha", hoy_iso) \
        .order("fecha", desc=True) \
        .limit(1) \
        .execute()

    saldo_anterior = float(cierre_resp.data[0]["saldo_cierre"]) if cierre_resp.data else 0


    # =====================================================
    # ABONO A CAPITAL HOY
    # =====================================================

    capital_resp = supabase.table("capital") \
        .select("valor, descripcion, created_at") \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", inicio_dia) \
        .lt("created_at", fin_dia) \
        .order("created_at", desc=True) \
        .execute()

    capital_hoy_lista = capital_resp.data or []

    total_abono_capital = sum(
        float(c["valor"] or 0)
        for c in capital_hoy_lista
    )


    # =====================================================
    # PRÉSTAMOS REALIZADOS HOY
    # =====================================================

    prestamos_resp = supabase.table("creditos") \
        .select("id, valor_venta, created_at, clientes(nombre)") \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", inicio_dia) \
        .lt("created_at", fin_dia) \
        .execute()

    total_prestamos = 0
    lista_prestamos = []

    for p in prestamos_resp.data or []:

        valor = float(p["valor_venta"] or 0)
        total_prestamos += valor

        lista_prestamos.append({
            "cliente": p["clientes"]["nombre"],
            "valor": valor,
            "fecha": p["created_at"]
        })


    # =====================================================
    # COBROS REALIZADOS HOY
    # =====================================================

    pagos_resp = supabase.table("pagos") \
        .select("""
            monto,
            fecha,
            creditos!inner(
                ruta_id,
                clientes(nombre)
            )
        """) \
        .eq("creditos.ruta_id", ruta_id) \
        .gte("fecha", inicio_dia) \
        .lt("fecha", fin_dia) \
        .execute()

    total_cobros = 0
    lista_cobros = []

    for pago in pagos_resp.data or []:

        monto = float(pago["monto"] or 0)
        total_cobros += monto

        lista_cobros.append({
            "cliente": pago["creditos"]["clientes"]["nombre"],
            "valor": monto
        })


    # =====================================================
    # GASTOS HOY
    # =====================================================

    gastos_resp = supabase.table("gastos") \
        .select("valor, categoria_id, descripcion, created_at") \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", inicio_dia) \
        .lt("created_at", fin_dia) \
        .execute()

    lista_gastos = []
    total_gastos = 0

    for g in gastos_resp.data or []:

        valor = float(g["valor"] or 0)
        total_gastos += valor

        lista_gastos.append({
            "valor": valor,
            "categoria": f"Categoría {g.get('categoria_id')}",
            "descripcion": g.get("descripcion"),
            "usuario": "Admin"
        })


    # =====================================================
    # SALDO DISPONIBLE
    # =====================================================

    saldo_actual = (
        saldo_anterior
        + total_abono_capital
        + total_cobros
        - total_prestamos
        - total_gastos
    )


    # =====================================================
    # VALIDAR SI YA SE CERRÓ CAJA HOY
    # =====================================================

    caja_hoy = supabase.table("caja_diaria") \
        .select("saldo_cierre") \
        .eq("ruta_id", ruta_id) \
        .eq("fecha", hoy_iso) \
        .limit(1) \
        .execute()

    caja_cerrada = bool(caja_hoy.data)


    # =====================================================
    # DEBUG
    # =====================================================

    print("SALDO ANTERIOR:", saldo_anterior)
    print("ABONO CAPITAL:", total_abono_capital)
    print("PRESTAMOS:", total_prestamos)
    print("COBROS:", total_cobros)
    print("GASTOS:", total_gastos)
    print("SALDO ACTUAL:", saldo_actual)


    # =====================================================
    # RENDER
    # =====================================================

    return render_template(
        "cobrador/caja.html",
        saldo_actual=saldo_actual,
        saldo_anterior=saldo_anterior,
        total_prestamos=total_prestamos,
        total_cobros=total_cobros,
        total_gastos=total_gastos,
        total_abono_capital=total_abono_capital,
        capital_hoy_lista=capital_hoy_lista,
        cobros=lista_cobros,
        prestamos=lista_prestamos,
        gastos=lista_gastos
    )

@app.route("/cerrar_dia", methods=["POST"])
def cerrar_dia():

    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    ruta_id = session.get("ruta_id")
    user_id = session.get("user_id")

    if not ruta_id:
        return redirect(url_for("dashboard_cobrador"))

    hoy = date.today().isoformat()

    # 🔹 Traer saldo anterior correctamente
    cierre_anterior = supabase.table("caja_diaria") \
        .select("saldo_cierre") \
        .eq("ruta_id", ruta_id) \
        .lt("fecha", hoy) \
        .order("fecha", desc=True) \
        .limit(1) \
        .execute()

    if cierre_anterior.data:
        saldo_anterior = float(cierre_anterior.data[0]["saldo_cierre"] or 0)
    else:
        saldo_anterior = 0

    # 🔹 Calcular movimientos del día
    pagos_hoy = supabase.table("pagos") \
        .select("monto, creditos(ruta_id)") \
        .gte("fecha", hoy + "T00:00:00") \
        .lte("fecha", hoy + "T23:59:59") \
        .execute()

    total_cobros = 0
    for p in pagos_hoy.data or []:
        if p["creditos"] and int(p["creditos"]["ruta_id"]) == int(ruta_id):
            total_cobros += float(p["monto"] or 0)

    prestamos_hoy = supabase.table("creditos") \
        .select("valor_venta") \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", hoy + "T00:00:00") \
        .lte("created_at", hoy + "T23:59:59") \
        .execute()

    total_prestamos = sum(
        float(p["valor_venta"] or 0)
        for p in prestamos_hoy.data or []
    )

    gastos_hoy = supabase.table("gastos") \
        .select("valor") \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", hoy + "T00:00:00") \
        .lte("created_at", hoy + "T23:59:59") \
        .execute()

    total_gastos = sum(
        float(g["valor"] or 0)
        for g in gastos_hoy.data or []
    )

    # 🔹 Saldo final real del día
    saldo_cierre = saldo_anterior + total_cobros - total_prestamos - total_gastos

    # 🔹 Evitar doble cierre
    ya_cerrado = supabase.table("caja_diaria") \
        .select("id") \
        .eq("ruta_id", ruta_id) \
        .eq("fecha", hoy) \
        .execute()

    if ya_cerrado.data:
        flash("La caja ya fue cerrada hoy", "warning")
        return redirect(url_for("caja_cobrador"))

    # 🔹 Guardar cierre correcto
    supabase.table("caja_diaria").insert({
        "ruta_id": ruta_id,
        "usuario_id": user_id,
        "fecha": hoy,
        "saldo_inicio": saldo_anterior,
        "saldo_cierre": saldo_cierre
    }).execute()

    flash("Caja cerrada correctamente", "success")
    return redirect(url_for("caja_cobrador"))


# Traer todos los clientes de la eruta para el modulo CLIENTES

@app.route("/clientes_ruta/<ruta_id>")
def clientes_ruta(ruta_id):

    if "user_id" not in session or session.get("rol") not in ["cobrador","supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    # Traer todos los créditos de la ruta (activos o no)
    creditos_resp = supabase.table("creditos") \
        .select("""
            cliente_id,
            estado,
            clientes(
                id,
                nombre,
                identificacion,
                telefono_principal,
                direccion
            )
        """) \
        .eq("ruta_id", ruta_id) \
        .execute()

    creditos = creditos_resp.data or []

    clientes_dict = {}

    for c in creditos:
        cliente = c["clientes"]
        cliente_id = cliente["id"]

        # Si no existe lo agregamos
        if cliente_id not in clientes_dict:
            clientes_dict[cliente_id] = {
                "id": cliente_id,
                "nombre": cliente["nombre"],
                "identificacion": cliente["identificacion"],
                "telefono": cliente["telefono_principal"],
                "direccion": cliente["direccion"],
                "credito_activo": False
            }

        # Si alguno está activo → marcar
        if c["estado"] == "activo":
            clientes_dict[cliente_id]["credito_activo"] = True

    clientes_lista = list(clientes_dict.values())

    return render_template(
        "cobrador/clientes_ruta.html",
        clientes=clientes_lista,
        ruta_id=ruta_id

    )

@app.route("/detalle_cliente/<cliente_id>/<ruta_id>")
def detalle_cliente(cliente_id, ruta_id):

    if "user_id" not in session or session.get("rol") not in ["cobrador", "supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    # =====================================================
    # CLIENTE
    # =====================================================
    cliente_resp = supabase.table("clientes") \
        .select("*") \
        .eq("id", cliente_id) \
        .single() \
        .execute()

    if not cliente_resp.data:
        return redirect(url_for("dashboard_cobrador"))

    cliente = cliente_resp.data

    # =====================================================
    # CRÉDITOS
    # =====================================================
    creditos_resp = supabase.table("creditos") \
        .select("*") \
        .eq("cliente_id", cliente_id) \
        .order("created_at", desc=True) \
        .execute()

    historial_creditos = creditos_resp.data or []

    credito = None
    credito_activo = None
    total_prestado = 0
    total_pagado = 0
    total_pagado_credito_activo = 0
    saldo_credito_activo = 0
    fotos = []
    credito_ids = []

    for c in historial_creditos:

        credito_ids.append(c["id"])
        total_prestado += float(c.get("valor_total") or 0)

        # 👇 Mantiene tu comportamiento anterior
        if not credito:
            credito = c

        if c.get("estado") == "activo":
            credito_activo = c

        if c.get("foto_cedula"):
            fotos.append({
                "url": c["foto_cedula"],
                "tipo": "Cédula",
                "fecha": c.get("created_at")
            })

        if c.get("foto_negocio"):
            fotos.append({
                "url": c["foto_negocio"],
                "tipo": "Negocio",
                "fecha": c.get("created_at")
            })

        if c.get("foto_vivienda"):
            fotos.append({
                "url": c["foto_vivienda"],
                "tipo": "Vivienda",
                "fecha": c.get("created_at")
            })

        if c.get("foto_cliente"):
            fotos.append({
                "url": c["foto_cliente"],
                "tipo": "Cliente",
                "fecha": c.get("created_at")
            })

    # =====================================================
    # PAGOS
    # =====================================================
    historial_pagos = []

    if credito_ids:
        pagos_resp = supabase.table("pagos") \
            .select("monto, fecha, credito_id, cobrador_id") \
            .in_("credito_id", credito_ids) \
            .order("fecha", desc=True) \
            .execute()

        historial_pagos = pagos_resp.data or []

        total_pagado = sum(
            float(p.get("monto") or 0)
            for p in historial_pagos
        )

    # =====================================================
    # VALIDAR RENOVACIÓN
    # =====================================================
    cuotas = []

    # ✅ Si no tiene crédito activo, sí puede crear uno nuevo
    puede_renovar = credito_activo is None

    if credito_activo:

        cuotas_resp = supabase.table("cuotas") \
            .select("estado") \
            .eq("credito_id", credito_activo["id"]) \
            .execute()

        cuotas = cuotas_resp.data or []

        total_pagado_credito_activo = sum(
            float(p.get("monto") or 0)
            for p in historial_pagos
            if str(p.get("credito_id")) == str(credito_activo["id"])
        )

        # 🔹 Validar si todas las cuotas están pagadas
        todas_pagadas = len(cuotas) > 0 and all(
            c.get("estado") == "pagado"
            for c in cuotas
        )

        # 🔹 Calcular saldo real SOLO del crédito activo
        saldo_credito_activo = round(
            float(credito_activo.get("valor_total") or 0) - total_pagado_credito_activo,
            2
        )

        # 🔹 Puede renovar SOLO si:
        #    - Todas las cuotas están pagadas
        #    - El saldo es 0
        if todas_pagadas and saldo_credito_activo <= 0:
            puede_renovar = True

            # 🔥 Cerrar automáticamente el crédito si ya quedó pago
            if credito_activo.get("estado") != "finalizado":
                supabase.table("creditos") \
                    .update({"estado": "finalizado"}) \
                    .eq("id", credito_activo["id"]) \
                    .execute()
        else:
            puede_renovar = False

    saldo_total_cliente = round(total_prestado - total_pagado, 2)

    # ✅ Para que el HTML sepa si realmente debe mostrar “Crédito Activo”
    tiene_credito_activo = credito_activo is not None and not puede_renovar

    return render_template(
        "cobrador/detalle_cliente.html",
        cliente=cliente,
        credito=credito,
        credito_activo=credito_activo,
        historial_creditos=historial_creditos,
        historial_pagos=historial_pagos,
        cuotas=cuotas,
        puede_renovar=puede_renovar,
        ruta_id=ruta_id,
        total_prestado=total_prestado,
        total_pagado=total_pagado,
        total_pagado_credito_activo=total_pagado_credito_activo,
        saldo_credito_activo=saldo_credito_activo,
        saldo=saldo_total_cliente,
        saldo_total_cliente=saldo_total_cliente,
        fotos=fotos,
        tiene_credito_activo=tiene_credito_activo
    )

@app.route("/renovar_credito/<cliente_id>/<ruta_id>")
def renovar_credito(cliente_id, ruta_id):

    if "user_id" not in session or session.get("rol") not in ["cobrador", "supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    # =====================================================
    # VALIDAR CLIENTE
    # =====================================================
    cliente_resp = supabase.table("clientes") \
        .select("*") \
        .eq("id", cliente_id) \
        .single() \
        .execute()

    if not cliente_resp.data:
        flash("Cliente no encontrado", "danger")
        return redirect(url_for("dashboard_cobrador"))

    # =====================================================
    # BUSCAR CRÉDITO ACTIVO DEL CLIENTE EN ESA RUTA
    # =====================================================
    credito_activo_resp = supabase.table("creditos") \
        .select("*") \
        .eq("cliente_id", cliente_id) \
        .eq("ruta_id", ruta_id) \
        .eq("estado", "activo") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    credito_activo = credito_activo_resp.data[0] if credito_activo_resp.data else None

    # =====================================================
    # SI HAY CRÉDITO ACTIVO, VALIDAR QUE ESTÉ TOTALMENTE PAGO
    # =====================================================
    if credito_activo:

        cuotas_resp = supabase.table("cuotas") \
            .select("estado") \
            .eq("credito_id", credito_activo["id"]) \
            .execute()

        cuotas = cuotas_resp.data or []

        pagos_resp = supabase.table("pagos") \
            .select("monto") \
            .eq("credito_id", credito_activo["id"]) \
            .execute()

        pagos = pagos_resp.data or []

        total_pagado_credito = sum(
            float(p.get("monto") or 0)
            for p in pagos
        )

        saldo_credito = round(
            float(credito_activo.get("valor_total") or 0) - total_pagado_credito,
            2
        )

        todas_pagadas = len(cuotas) > 0 and all(
            c.get("estado") == "pagado"
            for c in cuotas
        )

        if not (todas_pagadas and saldo_credito <= 0):
            flash("No se puede renovar este crédito porque aún no está totalmente pago.", "warning")
            return redirect(url_for("detalle_cliente", cliente_id=cliente_id, ruta_id=ruta_id))

        # 🔥 Si ya quedó pago, lo finalizamos antes de renovar
        supabase.table("creditos") \
            .update({"estado": "finalizado"}) \
            .eq("id", credito_activo["id"]) \
            .execute()

    # =====================================================
    # REDIRIGIR AL FORMULARIO DE NUEVA VENTA PRECARGADO
    # =====================================================
    return redirect(
        url_for(
            "nueva_venta_cobrador",
            cliente_id=cliente_id,
            ruta_id=ruta_id,
            renovar=1
        )
    )

@app.route("/transferencias_app", methods=["GET", "POST"])
def transferencias_app():

    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    ruta_id = session.get("ruta_id")

    if not ruta_id:
        return redirect(url_for("dashboard_cobrador"))

    # 🔹 Siempre iniciar en 0
    resumen = {
        "saldo_anterior": 0,
        "recibido": 0,
        "cobros": 0,
        "ventas": 0,
        "gastos": 0,
        "saldo_actual": 0
    }

    detalle_ventas = []
    detalle_cobros = []
    fecha_inicio = ""
    fecha_fin = ""

    if request.method == "POST":

        fecha_inicio = request.form.get("fecha_inicio")
        fecha_fin = request.form.get("fecha_fin")

        if not fecha_inicio or not fecha_fin:
            flash("Debe seleccionar ambas fechas", "warning")
            return redirect(url_for("transferencias_app"))

        # =====================================================
        # 1️⃣ TRAER CRÉDITOS DE ESA RUTA
        # =====================================================
        creditos = supabase.table("creditos") \
            .select("id, valor_total, created_at, clientes(nombre)") \
            .eq("ruta_id", ruta_id) \
            .execute().data or []

        credito_ids = []
        total_ventas = 0
        total_ventas_antes = 0

        for c in creditos:

            credito_ids.append(c["id"])
            fecha_credito = c["created_at"][:10]
            valor = float(c.get("valor_total") or 0)

            # Ventas dentro del rango
            if fecha_inicio <= fecha_credito <= fecha_fin:
                total_ventas += valor
                detalle_ventas.append({
                    "fecha": fecha_credito,
                    "cliente": c["clientes"]["nombre"],
                    "valor": valor
                })

            # Ventas antes del rango
            if fecha_credito < fecha_inicio:
                total_ventas_antes += valor

        # =====================================================
        # 2️⃣ TRAER CUOTAS PAGADAS DE ESA RUTA
        # =====================================================
        total_cobros = 0
        total_cobros_antes = 0

        if credito_ids:

            cuotas = supabase.table("cuotas") \
                .select("valor, fecha_pago, estado, credito_id, creditos(clientes(nombre))") \
                .in_("credito_id", credito_ids) \
                .eq("estado", "pagado") \
                .execute().data or []

            for cuota in cuotas:

                fecha_pago = cuota["fecha_pago"]
                valor = float(cuota.get("valor") or 0)

                # Cobros en rango
                if fecha_inicio <= fecha_pago <= fecha_fin:
                    total_cobros += valor
                    detalle_cobros.append({
                        "fecha": fecha_pago,
                        "cliente": cuota["creditos"]["clientes"]["nombre"],
                        "valor": valor
                    })

                # Cobros antes del rango
                if fecha_pago < fecha_inicio:
                    total_cobros_antes += valor

        # =====================================================
        # 3️⃣ CÁLCULO DE SALDOS
        # =====================================================
        saldo_anterior = total_ventas_antes - total_cobros_antes
        saldo_actual = saldo_anterior + total_cobros - total_ventas

        resumen["saldo_anterior"] = round(saldo_anterior, 2)
        resumen["cobros"] = round(total_cobros, 2)
        resumen["ventas"] = round(total_ventas, 2)
        resumen["saldo_actual"] = round(saldo_actual, 2)

    return render_template(
        "cobrador/transferencias_app.html",
        resumen=resumen,
        detalle_ventas=detalle_ventas,
        detalle_cobros=detalle_cobros,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin
    )


@app.route("/metas_dia")
def metas_dia():

    if "user_id" not in session or session.get("rol") not in ["cobrador","supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    ruta_id = session.get("ruta_id")

    if not ruta_id:
        return redirect(url_for("dashboard_cobrador"))

    hoy = date.today().isoformat()

    total_estimado = 0
    total_cobrado = 0
    clientes_asignados = set()
    clientes_pendientes = []

    # =====================================================
    # 1️⃣ CUOTAS PROGRAMADAS PARA HOY (META REAL)
    # =====================================================
    cuotas_hoy = supabase.table("cuotas") \
        .select("id, valor, estado, fecha_pago_real, creditos(ruta_id, clientes(nombre))") \
        .eq("fecha_pago", hoy) \
        .execute().data or []

    for cuota in cuotas_hoy:

        if cuota["creditos"] and int(cuota["creditos"]["ruta_id"]) == int(ruta_id):

            valor = float(cuota["valor"] or 0)
            total_estimado += valor

            nombre_cliente = cuota["creditos"]["clientes"]["nombre"]
            clientes_asignados.add(nombre_cliente)

            # Si ya fue pagada HOY
            if cuota["estado"] == "pagado" and cuota["fecha_pago_real"] and cuota["fecha_pago_real"][:10] == hoy:
                total_cobrado += valor
            else:
                clientes_pendientes.append({
                    "cliente": nombre_cliente,
                    "valor": valor
                })

    por_cobrar = total_estimado - total_cobrado

    porcentaje = 0
    if total_estimado > 0:
        porcentaje = round((total_cobrado / total_estimado) * 100, 2)

    return render_template(
        "cobrador/metas_dia.html",
        total_estimado=round(total_estimado, 2),
        total_cobrado=round(total_cobrado, 2),
        por_cobrar=round(por_cobrar, 2),
        porcentaje=porcentaje,
        total_clientes=len(clientes_asignados),
        clientes_pendientes=clientes_pendientes
    )


@app.route("/ruta/<ruta_id>")
def ver_ruta(ruta_id):

    # 🔐 1️⃣ Validar sesión
    if "user_id" not in session or session.get("rol") not in ["cobrador","supervisor", "administrador"]:
        return redirect(url_for("login_app"))

    user_id = int(session["user_id"])
    rol = session.get("rol")

    # 🔎 2️⃣ Validar acceso a la ruta
    if rol == "cobrador":

        ruta_resp = supabase.table("rutas") \
            .select("*") \
            .eq("id", ruta_id) \
            .eq("usuario_id", user_id) \
            .execute()

    else:  # supervisor

        asignacion = supabase.table("usuarios_rutas") \
            .select("*") \
            .eq("usuario_id", user_id) \
            .eq("ruta_id", ruta_id) \
            .execute()

        if not asignacion.data:
            return redirect(url_for("dashboard_cobrador"))

        ruta_resp = supabase.table("rutas") \
            .select("*") \
            .eq("id", ruta_id) \
            .execute()

    # validar resultado
    if not ruta_resp.data:
        return redirect(url_for("dashboard_cobrador"))

    ruta = ruta_resp.data[0]

    # 🔥 3️⃣ Guardar ruta activa en sesión
    session["ruta_id"] = ruta_id

    # 🔎 4️⃣ Traer créditos activos con info cliente
    response = supabase.table("creditos") \
        .select("""
            id,
            cliente_id,
            posicion,
            valor_total,
            tipo_prestamo,
            created_at,
            clientes(
                nombre,
                identificacion,
                telefono_principal,
                direccion
            )
        """) \
        .eq("ruta_id", ruta_id) \
        .eq("estado", "activo") \
        .order("posicion") \
        .execute()

    creditos = response.data if response.data else []
    lista_creditos = []

    hoy = date.today()
    hoy_iso = hoy.isoformat()

    # 🔎 5️⃣ Procesar cada crédito
    for c in creditos:

        # 🔥 Si hoy hizo cualquier pago a este crédito, sale del buzón hoy
        pago_hoy_credito = supabase.table("pagos") \
            .select("id") \
            .eq("credito_id", c["id"]) \
            .gte("fecha", hoy_iso + "T00:00:00") \
            .lt("fecha", hoy_iso + "T23:59:59") \
            .limit(1) \
            .execute()

        ya_pago_hoy = True if pago_hoy_credito.data else False

        if ya_pago_hoy:
            continue

        cuotas = supabase.table("cuotas") \
            .select("valor, monto_pagado, estado, fecha_pago") \
            .eq("credito_id", c["id"]) \
            .order("numero") \
            .execute().data or []

        total_pagado = 0
        dias_mora = 0
        proxima_cuota = None
        mostrar_en_buzon = False
        primera_cuota_vencida = None

        for cuota in cuotas:

            fecha_pago = date.fromisoformat(cuota["fecha_pago"])

            pagado = float(cuota.get("monto_pagado") or 0)
            total_pagado += pagado

            if cuota["estado"] == "pendiente":

                if fecha_pago <= hoy:
                    mostrar_en_buzon = True

                # 🔥 Guardar solo la cuota vencida más antigua
                if fecha_pago < hoy and not primera_cuota_vencida:
                    primera_cuota_vencida = fecha_pago

                if not proxima_cuota:
                    proxima_cuota = cuota["fecha_pago"]

        # 🔥 Mora real = desde la cuota vencida más antigua
        if primera_cuota_vencida:
            dias_mora = (hoy - primera_cuota_vencida).days

        saldo = float(c.get("valor_total") or 0) - total_pagado

        # evitar negativos
        if saldo < 0:
            saldo = 0

        # 🚫 Si no debe mostrarse hoy, saltar
        if not mostrar_en_buzon:
            continue

        # 🎨 Semáforo
        if dias_mora >= 30:
            color_estado = "rojo"
        elif dias_mora >= 7:
            color_estado = "naranja"
        elif dias_mora > 0:
            color_estado = "verde"
        else:
            color_estado = "verde"

        lista_creditos.append({
            "id": c["id"],
            "posicion": c["posicion"],
            "cliente": c["clientes"]["nombre"],
            "identificacion": c["clientes"]["identificacion"],
            "telefono": c["clientes"]["telefono_principal"],
            "direccion": c["clientes"]["direccion"],
            "tipo_prestamo": c["tipo_prestamo"],
            "saldo": saldo,
            "dias_mora": dias_mora,
            "proxima_cuota": proxima_cuota,
            "codigo": c["id"][:6],
            "color_estado": color_estado
        })

    # 🔥 6️⃣ Render
    return render_template(
        "cobrador/ventas_ruta.html",
        ruta=ruta,
        creditos=lista_creditos,
        ruta_id=ruta_id
    )
@app.route("/oficinas/crear", methods=["POST"])
def crear_oficina():

    nombre = request.form.get("nombre")
    pais = request.form.get("pais")
    codigo = request.form.get("codigo")

    if not nombre or not pais:
        flash("Nombre y país son obligatorios.", "danger")
        return redirect("/oficina/change")

    supabase.table("oficinas").insert({
        "nombre": nombre,
        "pais": pais,
        "codigo": codigo,
        "rutas_activas": 0
    }).execute()

    flash("Oficina creada correctamente.", "success")
    return redirect("/oficina/change")

# -----------------------
# SELECCIONAR OFICINA
# -----------------------
@app.route("/oficina/change")
def cambiar_oficina():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficinas_resp = supabase.table("oficinas") \
        .select("id,nombre,pais") \
        .order("created_at", desc=True) \
        .execute()

    oficinas = oficinas_resp.data

    for oficina in oficinas:

        rutas_resp = supabase.table("rutas") \
            .select("id,estado") \
            .eq("oficina_id", str(oficina["id"])) \
            .execute()

        rutas = rutas_resp.data or []

        oficina["rutas_activas"] = len([
            r for r in rutas if r["estado"] == "true"
        ])

    return render_template("oficinas.html", oficinas=oficinas)


# -----------------------
# SELECCIONAR OFICINA (GUARDAR EN SESSION)
# -----------------------
@app.route("/oficina/select/<oficina_id>")
def seleccionar_oficina(oficina_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    response = supabase.table("oficinas") \
        .select("*") \
        .eq("id", oficina_id) \
        .single() \
        .execute()

    if not response.data:
        flash("Oficina no encontrada", "danger")
        return redirect(url_for("dashboard"))

    oficina = response.data

    session["oficina_id"] = oficina["id"]   # UUID string
    session["oficina_nombre"] = oficina["nombre"]
    session["oficina_pais"] = oficina["pais"]

    return redirect(url_for("dashboard"))

# -----------------------
# LISTAR EL REPORTE DE USUARIOS
# -----------------------
@app.route("/usuarios")
def usuarios():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    response = supabase.table("usuarios") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("fecha_registro", desc=True) \
        .execute()

    usuarios = response.data or []

    return render_template("users.html", usuarios=usuarios)
# -----------------------
# CREAR UN NUEVO USUARIO EN EL SISTEMA
# -----------------------
@app.route("/usuarios/crear", methods=["POST"])
def crear_usuario():

    nombres = request.form["nombres"]
    apellidos = request.form["apellidos"]
    documento = request.form["documento"]
    email = request.form["email"]
    rol = request.form["rol"]
    password = request.form["password"]

    # Validar email
    existe_email = supabase.table("usuarios") \
        .select("id") \
        .eq("email", email) \
        .execute()

    if existe_email.data:
        flash("El correo electrónico ya está registrado.", "danger")

        response = supabase.table("usuarios") \
            .select("*") \
            .order("fecha_registro", desc=True) \
            .execute()

        usuarios = response.data

        return render_template("users.html", usuarios=usuarios, abrir_modal=True)


    # Validar documento
    existe_doc = supabase.table("usuarios") \
        .select("id") \
        .eq("documento", documento) \
        .execute()

    if existe_doc.data:
        flash("Ya existe un usuario con esa cédula.", "danger")
        
        response = supabase.table("usuarios") \
            .select("*") \
            .order("fecha_registro", desc=True) \
            .execute()

        usuarios = response.data

        return render_template("users.html", usuarios=usuarios, abrir_modal=True)

    # Insertar
    supabase.table("usuarios").insert({
        "nombres": nombres,
        "apellidos": apellidos,
        "documento": documento,
        "email": email,
        "rol": rol,
        "password": password,
        "estado": True,
        "oficina_id": session.get("oficina_id"),  # 🔥 importante

    }).execute()

    flash("Usuario creado correctamente.", "success")
    return redirect(url_for("usuarios"))


# -----------------------
# EDITAR USUARIO
# -----------------------
@app.route("/usuarios/editar/<int:id>", methods=["POST"])
def editar_usuario(id):

    nombres = request.form["nombres"]
    apellidos = request.form["apellidos"]
    documento = request.form["documento"]
    email = request.form["email"]
    direccion = request.form.get("direccion")
    telefono = request.form.get("telefono")
    rol = request.form["rol"]

    # Validar email repetido (excepto el mismo usuario)
    existe_email = supabase.table("usuarios") \
        .select("id") \
        .eq("email", email) \
        .neq("id", id) \
        .execute()

    if existe_email.data:
        flash("El correo ya pertenece a otro usuario.", "danger")
        return redirect(url_for("usuarios"))

    # Actualizar
    supabase.table("usuarios") \
        .update({
            "nombres": nombres,
            "apellidos": apellidos,
            "documento": documento,
            "email": email,
            "direccion": direccion,
            "telefono": telefono,
            "rol": rol
        }) \
        .eq("id", id) \
        .execute()

    flash("Usuario actualizado correctamente.", "success")
    return redirect(url_for("usuarios"))

@app.route("/usuarios/rutas/<int:usuario_id>")
def rutas_usuario(usuario_id):

    oficina_id = session.get("oficina_id")

    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .execute()

    asignadas = supabase.table("usuarios_rutas") \
        .select("ruta_id") \
        .eq("usuario_id", usuario_id) \
        .execute()

    rutas_asignadas = [r["ruta_id"] for r in asignadas.data]

    resultado=[]

    for r in rutas.data:

        resultado.append({
            "id": r["id"],
            "nombre": r["nombre"],
            "asignada": r["id"] in rutas_asignadas
        })

    return jsonify({"rutas":resultado})
@app.route("/usuarios/asignar-rutas", methods=["POST"])
def asignar_rutas():

    usuario_id = request.form["usuario_id"]
    rutas = request.form.getlist("rutas")

    # borrar asignaciones actuales
    supabase.table("usuarios_rutas") \
        .delete() \
        .eq("usuario_id", usuario_id) \
        .execute()

    for ruta in rutas:

        supabase.table("usuarios_rutas").insert({
            "usuario_id": usuario_id,
            "ruta_id": ruta
        }).execute()

    flash("Rutas asignadas correctamente", "success")

    return redirect(url_for("usuarios"))
# -----------------------
# ELIMINAR USUARIO
# -----------------------


@app.route('/usuarios/ver/<int:id>')
def ver_usuario(id):
    return f"ver usuario {id}"


# -----------------------
# INACTIVAR USUARIO O ACTIVAR
# -----------------------

@app.route("/usuarios/toggle/<int:user_id>", methods=["POST"])
def toggle_usuario(user_id):

    response = supabase.table("usuarios") \
        .select("estado") \
        .eq("id", user_id) \
        .single() \
        .execute()

    usuario = response.data
    nuevo_estado = not usuario["estado"]

    supabase.table("usuarios") \
        .update({"estado": nuevo_estado}) \
        .eq("id", user_id) \
        .execute()

    if nuevo_estado:
        flash("Usuario activado correctamente.", "success")
    else:
        flash("Usuario inactivado correctamente.", "danger")

    return redirect(url_for("usuarios"))


# -----------
# CREAR RUTAS
# -----------------------
@app.route("/rutas/crear", methods=["POST"])
def crear_ruta():

    if "oficina_id" not in session:
        return redirect(url_for("cambiar_oficina"))

    oficina_id = session["oficina_id"]

    posicion = request.form["posicion"]
    nombre = request.form["nombre"]
    tasa = request.form["tasa"]
    venta_maxima = request.form["venta_maxima"]

    # 🔥 USAR EL MISMO USER_ID DE LA SESIÓN
    usuario_id = session["user_id"]

    codigo = generar_codigo_ruta()

    supabase.table("rutas").insert({
        "posicion": posicion,
        "codigo": codigo,
        "nombre": nombre,
        "tasa": tasa,
        "venta_maxima": venta_maxima,
        "usuario_id": usuario_id,   # 🔥 AQUÍ
        "oficina_id": oficina_id,
        "estado": True
    }).execute()

    flash("Ruta creada correctamente", "success")
    return redirect(url_for("listar_rutas"))




@app.route("/oficinas")
def listar_oficinas():

    print("ENTRANDO A LISTAR OFICINAS 🔥")

    response = supabase.table("oficinas") \
        .select("*, rutas(*)") \
        .execute()

    print("RESPUESTA SUPABASE:", response)

    oficinas = response.data
    print("DATA:", oficinas)

    return render_template("oficinas.html", oficinas=oficinas)

@app.route("/rutas")
def listar_rutas():

    if "oficina_id" not in session:
        return redirect(url_for("cambiar_oficina"))

    oficina_id = session["oficina_id"]

    rutas = supabase.table("rutas") \
        .select("*, usuarios(*)") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute()

    usuarios = supabase.table("usuarios") \
        .select("id, nombres, apellidos, rol") \
        .in_("rol", ["Supervisor", "Cobrador"]) \
        .execute()

    return render_template(
        "rutas.html",
        rutas=rutas.data or [],
        usuarios=usuarios.data or []
    )


# -----------------------
# ESTADO RUTAS
# -----------------------

@app.route("/rutas/toggle/<int:id>", methods=["POST"])
def toggle_ruta(id):

    ruta = supabase.table("rutas") \
        .select("estado") \
        .eq("id", id) \
        .single() \
        .execute()

    nuevo_estado = not ruta.data["estado"]

    supabase.table("rutas") \
        .update({"estado": nuevo_estado}) \
        .eq("id", id) \
        .execute()

    return redirect(url_for("listar_rutas"))

## -----------------------
# LISTAR VENTAS ACTIVAS
# -----------------------
from datetime import date

@app.route("/ventas")
def listar_ventas():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    ruta_id = request.args.get("ruta_id")
    buscar = request.args.get("buscar", "").strip().lower()
    filtro_mora = request.args.get("filtro_mora")
    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")

    # 🔥 SOLO rutas de la oficina activa
    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute().data or []

    ventas = []
    saldo_total = 0

    if ruta_id:

        # 🔒 VALIDAR QUE LA RUTA PERTENEZCA A LA OFICINA
        ruta_validacion = supabase.table("rutas") \
            .select("id, oficina_id") \
            .eq("id", ruta_id) \
            .single() \
            .execute().data

        if not ruta_validacion or ruta_validacion["oficina_id"] != oficina_id:
            flash("No tiene acceso a esta ruta", "error")
            return redirect(url_for("listar_ventas"))

        # 🔥 Traer créditos de la ruta validada
        response = supabase.table("creditos") \
            .select("""
                id,
                cliente_id,
                posicion,
                valor_venta,
                valor_total,
                created_at,
                clientes(nombre, identificacion),
                latitud,
                longitud
            """) \
            .eq("ruta_id", ruta_id) \
            .order("posicion") \
            .execute()

        creditos = response.data or []

        for c in creditos:

            cliente_nombre = c["clientes"]["nombre"]
            identificacion = c["clientes"]["identificacion"]

            # 🔎 BUSCADOR
            if buscar:
                if buscar not in cliente_nombre.lower() and buscar not in identificacion.lower():
                    continue

            cuotas = supabase.table("cuotas") \
                .select("valor, estado, fecha_pago") \
                .eq("credito_id", c["id"]) \
                .execute().data or []

            total_pagado = 0
            dias_mora = 0

            for cuota in cuotas:

                if cuota["estado"] == "pagado":
                    total_pagado += float(cuota["valor"])

                if cuota["estado"] == "pendiente":
                    fecha_pago = date.fromisoformat(cuota["fecha_pago"])
                    if fecha_pago < date.today():
                        dias_mora += (date.today() - fecha_pago).days

            # 🔥 FILTRO POR MORA
            if filtro_mora == "21" and dias_mora < 21:
                continue
            if filtro_mora == "11" and (dias_mora < 11 or dias_mora >= 21):
                continue
            if filtro_mora == "0" and dias_mora > 0:
                continue

            fecha_registro = c["created_at"][:10]

            # 🔥 FILTRO POR FECHAS (CORRECTAMENTE UBICADO)
            if fecha_inicio and fecha_registro < fecha_inicio:
                continue

            if fecha_fin and fecha_registro > fecha_fin:
                continue

            saldo = float(c["valor_total"]) - total_pagado

            ventas.append({
                "credito_id": c["id"],
                "cliente_id": c["cliente_id"],
                "posicion": c["posicion"],
                "codigo": c["id"][:8],
                "valor_venta": "{:,.0f}".format(c["valor_venta"]),
                "valor_total": "{:,.0f}".format(c["valor_total"]),
                "saldo": "{:,.0f}".format(saldo),
                "cliente": cliente_nombre,
                "identificacion": identificacion,
                "fecha_registro": fecha_registro,
                "dias_mora": dias_mora,
                "latitud": c["latitud"],
                "longitud": c["longitud"],
            })

            saldo_total += saldo

    return render_template(
        "ventas.html",
        rutas=rutas,
        ventas=ventas,
        ruta_id=int(ruta_id) if ruta_id else None,
        saldo_total=saldo_total
    )




# =============================
# NUEVA VENTA (CONTROL FLUJO)
# =============================
@app.route("/nueva_venta")
def nueva_venta():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    rutas_resp = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute()

    rutas = rutas_resp.data if rutas_resp.data else []

    cliente = None 

    
    valor_anterior = None
    form_data = {}

    cliente_id = session.get("cliente_id")

    if cliente_id:
        cliente_resp = supabase.table("clientes") \
            .select("*") \
            .eq("id", cliente_id) \
            .execute()

        if cliente_resp.data:
            cliente = cliente_resp.data[0]

    valor_anterior = session.get("valor_anterior")

    return render_template(
        "nueva_venta.html",
        rutas=rutas,
        cliente=cliente,
        valor_anterior=valor_anterior,
        form_data=form_data
    )

@app.route("/cancelar_venta")
def cancelar_venta():

    session.pop("cliente_id", None)

    flash("Venta pendiente cancelada", "warning")

    return redirect(url_for("nueva_venta"))



# GUARDAR CLIENTE
from postgrest.exceptions import APIError

# GUARDAR CLIENTE
@app.route("/guardar_cliente", methods=["POST"])
def guardar_cliente():

    nombre = request.form["nombre"]
    identificacion = request.form["identificacion"]
    telefono = request.form.get("telefono_principal")

    foto_url = None

    try:

        # 🔥 Subir imagen si existe
        if "foto" in request.files:
            foto = request.files["foto"]

            if foto.filename != "":
                filename = f"{identificacion}.jpg"

                supabase.storage.from_("clientes").upload(
                    filename,
                    foto.read(),
                    {"content-type": foto.content_type}
                )

                foto_url = supabase.storage.from_("clientes").get_public_url(filename)

        data = {
            "nombre": nombre,
            "identificacion": identificacion,
            "telefono_principal": telefono,
            "foto": foto_url
        }

        response = supabase.table("clientes").insert(data).execute()

        session["cliente_id"] = response.data[0]["id"]
        flash("Cliente guardado correctamente", "success")

    except APIError as e:

        # Código 23505 = duplicate key
        if "23505" in str(e):
            flash("Ya existe un cliente con esa identificación", "warning")
        else:
            flash("Ocurrió un error al guardar el cliente", "danger")

        return redirect(url_for("nueva_venta"))

    return redirect(url_for("nueva_venta"))

    
@app.route("/buscar_cliente_renovacion", methods=["POST"])
def buscar_cliente_renovacion():

    identificacion = request.form.get("identificacion")

    # 🔎 Buscar cliente por cédula
    cliente_resp = supabase.table("clientes") \
        .select("*") \
        .eq("identificacion", identificacion) \
        .execute()

    if not cliente_resp.data:
        flash("Cliente no encontrado", "danger")
        return redirect(url_for("nueva_venta"))

    cliente = cliente_resp.data[0]

    # 🔎 Buscar todos los créditos del cliente
    creditos = supabase.table("creditos") \
        .select("id, valor_venta") \
        .eq("cliente_id", cliente["id"]) \
        .execute().data

    if not creditos:
        flash("El cliente no tiene créditos para renovar", "warning")
        return redirect(url_for("nueva_venta"))

    # 🔥 VALIDACIÓN REAL (NO usar estado del crédito)
    for credito in creditos:

        cuotas_pendientes = supabase.table("cuotas") \
            .select("id") \
            .eq("credito_id", credito["id"]) \
            .neq("estado", "pagado") \
            .execute()

        if cuotas_pendientes.data:
            flash("El cliente aún tiene saldo pendiente y no es posible renovar", "warning")
            return redirect(url_for("nueva_venta"))

    # 🔥 Si llega aquí → todos los créditos están pagados
    session["cliente_id"] = cliente["id"]
    session["valor_anterior"] = creditos[-1]["valor_venta"]

    flash("Cliente listo para renovación", "success")
    return redirect(url_for("nueva_venta"))


# LIMPIAR CLIENTE

@app.route("/limpiar_cliente")
def limpiar_cliente():

    session.pop("cliente_id", None)
    session.pop("valor_anterior", None)

    flash("Cliente removido correctamente", "warning")

    return redirect(url_for("nueva_venta"))


# LISTAR CREDITO
@app.route("/creditos")
def listar_creditos():

    filtro = request.args.get("mora")

    query = supabase.table("vista_creditos_mora").select("*")

    if filtro == "21":
        query = query.gte("dias_mora", 21)
    elif filtro == "11":
        query = query.gte("dias_mora", 11).lt("dias_mora", 21)
    elif filtro == "0":
        query = query.eq("dias_mora", 0)

    creditos = query.execute()

    return render_template("creditos.html", creditos=creditos.data)

# =============================
# GUARDAR VENTA
# =============================
@app.route("/guardar_venta", methods=["POST"])
def guardar_venta():

    cliente_id = session.get("cliente_id")

    if not cliente_id:
        flash("Debe seleccionar un cliente", "warning")
        return redirect(url_for("nueva_venta"))

    # 🔎 Validar si tiene crédito con cuotas pendientes
    creditos_cliente = supabase.table("creditos") \
        .select("id") \
        .eq("cliente_id", cliente_id) \
        .execute().data or []

    for credito in creditos_cliente:
        cuotas_pendientes = supabase.table("cuotas") \
            .select("id") \
            .eq("credito_id", credito["id"]) \
            .neq("estado", "pagado") \
            .execute()

        if cuotas_pendientes.data:
            flash("El cliente tiene un crédito con saldo pendiente", "error")
            return redirect(url_for("nueva_venta"))

    try:
        valor_venta = float((request.form.get("valor_venta") or "0").replace(",", "."))
        tasa = float((request.form.get("tasa") or "0").replace(",", "."))
        cuotas = int(request.form.get("cuotas") or 0)
        tipo_prestamo = (request.form.get("tipo_prestamo") or "").strip()
        fecha_inicio_str = (request.form.get("fecha_inicio") or "").strip()
        ruta_id = request.form.get("ruta_id")

        if valor_venta <= 0 or cuotas <= 0 or tasa < 0 or not tipo_prestamo or not fecha_inicio_str or not ruta_id:
            raise ValueError("Datos incompletos o inválidos")

        fecha_inicio = datetime.strptime(fecha_inicio_str, "%Y-%m-%d")

    except Exception as e:
        print("ERROR DATOS VENTA:", e)
        flash("Datos inválidos para registrar la venta", "error")
        return redirect(url_for("nueva_venta"))

    valor_total = round(valor_venta + (valor_venta * tasa / 100), 2)
    valor_cuota = round(valor_total / cuotas, 2)

    # 🔹 Obtener última posición en esa ruta
    ultimo = supabase.table("creditos") \
        .select("posicion") \
        .eq("ruta_id", ruta_id) \
        .order("posicion", desc=True) \
        .limit(1) \
        .execute().data

    if ultimo:
        nueva_posicion = int(ultimo[0]["posicion"]) + 1
    else:
        nueva_posicion = 1

    credito_data = {
        "cliente_id": cliente_id,
        "ruta_id": ruta_id,
        "tipo_prestamo": tipo_prestamo,
        "posicion": nueva_posicion,
        "valor_venta": valor_venta,
        "tasa": tasa,
        "valor_total": valor_total,
        "cantidad_cuotas": cuotas,
        "valor_cuota": valor_cuota,
        "fecha_inicio": fecha_inicio_str,
        "estado": "activo"
    }

    credito_resp = supabase.table("creditos").insert(credito_data).execute()

    if not credito_resp.data:
        flash("Error al registrar el crédito", "error")
        return redirect(url_for("nueva_venta"))

    credito_id = credito_resp.data[0]["id"]

    def sumar_meses(base_date, meses):
        year = base_date.year + ((base_date.month - 1 + meses) // 12)
        month = ((base_date.month - 1 + meses) % 12) + 1
        day = base_date.day

        # último día del mes destino
        if month == 12:
            siguiente_mes = datetime(year + 1, 1, 1)
        else:
            siguiente_mes = datetime(year, month + 1, 1)

        ultimo_dia = (siguiente_mes - timedelta(days=1)).day
        day = min(day, ultimo_dia)

        return datetime(year, month, day)

    # 🔹 Crear cuotas según tipo de préstamo
    if tipo_prestamo == "Semanal":
        for i in range(cuotas):
            fecha_pago = fecha_inicio + timedelta(days=(i + 1) * 7)

            supabase.table("cuotas").insert({
                "credito_id": credito_id,
                "numero": i + 1,
                "valor": valor_cuota,
                "estado": "pendiente",
                "fecha_pago": fecha_pago.date().isoformat()
            }).execute()

    elif tipo_prestamo == "Quincenal":
        for i in range(cuotas):
            fecha_pago = fecha_inicio + timedelta(days=(i + 1) * 15)

            supabase.table("cuotas").insert({
                "credito_id": credito_id,
                "numero": i + 1,
                "valor": valor_cuota,
                "estado": "pendiente",
                "fecha_pago": fecha_pago.date().isoformat()
            }).execute()

    elif tipo_prestamo == "Mensual":
        for i in range(cuotas):
            fecha_pago = sumar_meses(fecha_inicio, i + 1)

            supabase.table("cuotas").insert({
                "credito_id": credito_id,
                "numero": i + 1,
                "valor": valor_cuota,
                "estado": "pendiente",
                "fecha_pago": fecha_pago.date().isoformat()
            }).execute()

    else:
        fecha_actual = fecha_inicio
        cuotas_creadas = 0

        while cuotas_creadas < cuotas:
            crear_cuota = False
            fecha_pago = fecha_actual

            if tipo_prestamo == "Diario Lunes a Viernes":
                if fecha_actual.weekday() < 5:
                    crear_cuota = True

            elif tipo_prestamo == "Diario Lunes a Sábado":
                if fecha_actual.weekday() < 6:
                    crear_cuota = True

            else:
                crear_cuota = True

            if crear_cuota:
                supabase.table("cuotas").insert({
                    "credito_id": credito_id,
                    "numero": cuotas_creadas + 1,
                    "valor": valor_cuota,
                    "estado": "pendiente",
                    "fecha_pago": fecha_pago.date().isoformat()
                }).execute()

                cuotas_creadas += 1

            fecha_actual += timedelta(days=1)

    # 🔹 Si era renovación, marcar crédito anterior como renovado
    valor_anterior = session.get("valor_anterior")

    if valor_anterior:
        ultimo_credito = supabase.table("creditos") \
            .select("id") \
            .eq("cliente_id", cliente_id) \
            .neq("id", credito_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if ultimo_credito.data:
            supabase.table("creditos").update({
                "estado": "renovado"
            }).eq("id", ultimo_credito.data[0]["id"]).execute()

    session.pop("cliente_id", None)
    session.pop("valor_anterior", None)

    flash("Venta registrada correctamente", "success")
    return redirect(url_for("nueva_venta"))
# LISTAR PAGOS
@app.route("/pagos")
def vista_pagos():

    ruta_id = request.args.get("ruta_id")
    credito_id = request.args.get("credito_id")

    rutas = supabase.table("rutas").select("*").execute().data

    creditos = []
    credito_detalle = None
    cuotas = []
    saldo = 0

    if ruta_id:
        creditos = supabase.table("creditos") \
            .select("id, clientes(nombre)") \
            .eq("ruta_id", ruta_id) \
            .eq("estado", "activo") \
            .execute().data

    if credito_id:

        credito = supabase.table("creditos") \
            .select("*, clientes(*)") \
            .eq("id", credito_id) \
            .single() \
            .execute().data

        cuotas = supabase.table("cuotas") \
            .select("*") \
            .eq("credito_id", credito_id) \
            .order("numero") \
            .execute().data

        total_pagado = sum(float(c["valor"]) for c in cuotas if c["estado"] == "pagado")
        saldo = float(credito["valor_total"]) - total_pagado

        credito_detalle = credito

    return render_template(
        "registro_pagos/pagos.html",
        rutas=rutas,
        creditos=creditos,
        credito=credito_detalle,
        cuotas=cuotas,
        saldo=saldo
    )

@app.route("/eliminar_pago/<pago_id>")
def eliminar_pago(pago_id):

    # Traer el pago
    pago = supabase.table("pagos") \
        .select("*") \
        .eq("id", pago_id) \
        .single() \
        .execute().data

    if not pago:
        flash("Pago no encontrado", "danger")
        return redirect(request.referrer)

    cuota_id = pago["cuota_id"]
    credito_id = pago["credito_id"]

    # 1️⃣ Eliminar el pago
    supabase.table("pagos") \
        .delete() \
        .eq("id", pago_id) \
        .execute()

    # 2️⃣ Volver cuota a pendiente
    supabase.table("cuotas") \
        .update({
            "estado": "pendiente"
        }) \
        .eq("id", cuota_id) \
        .execute()

    flash("Pago eliminado correctamente", "success")

    return redirect(request.referrer)
# HISTORIAL DE CUOTAS
@app.route("/historial_creditos/<cliente_id>")
def historial_creditos(cliente_id):

    # ==========================
    # TRAER CLIENTE
    # ==========================
    cliente_resp = supabase.table("clientes") \
        .select("*") \
        .eq("id", cliente_id) \
        .single() \
        .execute()

    cliente = cliente_resp.data if cliente_resp.data else None

    if not cliente:
        flash("Cliente no encontrado", "danger")
        return redirect(url_for("clientes"))

    # ==========================
    # TRAER CRÉDITOS
    # ==========================
    creditos_resp = supabase.table("creditos") \
        .select("*") \
        .eq("cliente_id", cliente_id) \
        .order("fecha_inicio", desc=True) \
        .execute()

    creditos = creditos_resp.data if creditos_resp.data else []

    # ==========================
    # PROCESAR CADA CRÉDITO
    # ==========================
    for credito in creditos:

        # -------- CUOTAS --------
        cuotas_db = supabase.table("cuotas") \
            .select("*") \
            .eq("credito_id", credito["id"]) \
            .order("numero") \
            .execute().data or []

        cuotas = []

        for c in cuotas_db:

            dias_mora = 0

            if c["estado"] == "pendiente":
                fecha = date.fromisoformat(c["fecha_pago"])
                if fecha < date.today():
                    dias_mora = (date.today() - fecha).days

            cuotas.append({
                "id": c["id"],
                "numero": c["numero"],
                "fecha_programada": c["fecha_pago"],
                "valor": c["valor"],
                "estado": c["estado"],
                "dias_mora": dias_mora
            })

        # -------- PAGOS --------
        pagos_db = supabase.table("pagos") \
            .select("*") \
            .eq("credito_id", credito["id"]) \
            .order("fecha", desc=True) \
            .execute().data or []

        pagos = []
        total_pagado = 0

        for p in pagos_db:

            monto = float(p.get("monto", 0))
            total_pagado += monto

            pagos.append({
                "id": p["id"],
                "cuota_id": p.get("cuota_id"),
                "numero": p.get("numero_cuota"),
                "fecha": p["fecha"],
                "monto": monto
            })

        # -------- ASIGNAR DATOS AL CRÉDITO --------
        credito["cuotas"] = cuotas
        credito["pagos"] = pagos
        credito["total_pagado"] = total_pagado
        credito["saldo"] = float(credito["valor_total"]) - total_pagado

    # ==========================
    # RENDER
    # ==========================
    return render_template(
        "historial_creditos/historial_creditos.html",
        cliente=cliente,
        creditos=creditos
    )
@app.route("/historial_cliente/<cliente_id>")
def historial_cliente(cliente_id):

    cliente_resp = supabase.table("clientes") \
        .select("*") \
        .eq("id", cliente_id) \
        .single() \
        .execute()

    if not cliente_resp.data:
        return redirect(url_for("dashboard_cobrador"))

    cliente = cliente_resp.data

    creditos_resp = supabase.table("creditos") \
        .select("*") \
        .eq("cliente_id", cliente_id) \
        .order("created_at", desc=True) \
        .execute()

    creditos = creditos_resp.data if creditos_resp.data else []

    lista_creditos = []

    for c in creditos:

        # 🔹 CUOTAS
        cuotas = supabase.table("cuotas") \
            .select("*") \
            .eq("credito_id", c["id"]) \
            .order("numero") \
            .execute().data

        total_pagado = 0
        pagos = []  # 🔥 IMPORTANTE: definir aquí
        print("CREDITO:", c["id"])
        print("CUOTAS:", cuotas)
        for cuota in cuotas:

            if cuota["estado"] and "pag" in cuota["estado"].lower():
                print("ESTADO CUOTA:", cuota["estado"])
                total_pagado += float(cuota["valor"])

                pagos.append({
                    "numero": cuota["numero"],
                    "fecha": cuota["fecha_pago"],
                    "monto": float(cuota["valor"])
                })


        saldo = float(c["valor_total"]) - total_pagado

        lista_creditos.append({
            "id": c["id"],
            "estado": c["estado"],
            "valor_total": c["valor_total"],
            "valor_venta": c["valor_venta"],
            "saldo": saldo,
            "fecha": c["created_at"][:10],
            "foto_cedula": c.get("foto_cedula"),
            "foto_negocio": c.get("foto_negocio"),
            "cuotas": cuotas,
            "pagos": pagos  # 🔥 ahora sí existe
        })

    return render_template(
        "cobrador/historial_cliente.html",
        cliente=cliente,
        creditos=lista_creditos
    )
    
@app.route("/clientes")
def clientes():

    if "user_id" not in session:
        return redirect(url_for("login"))

    # 🔥 Usar oficina desde sesión (igual que en usuarios)
    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 1️⃣ Obtener rutas de esa oficina
    rutas = supabase.table("rutas") \
        .select("id") \
        .eq("oficina_id", oficina_id) \
        .execute().data

    ruta_ids = [r["id"] for r in rutas]

    if not ruta_ids:
        return render_template("clientes.html", clientes=[])

    # 2️⃣ Obtener créditos de esas rutas
    creditos = supabase.table("creditos") \
        .select("cliente_id") \
        .in_("ruta_id", ruta_ids) \
        .execute().data

    cliente_ids = list(set([c["cliente_id"] for c in creditos]))

    if not cliente_ids:
        return render_template("clientes.html", clientes=[])

    # 3️⃣ Obtener clientes finales
    clientes = supabase.table("clientes") \
        .select("*") \
        .in_("id", cliente_ids) \
        .order("posicion") \
        .execute().data

    # 🔥 Mantengo tu cálculo de mora EXACTAMENTE igual
    for cliente in clientes:

        mayor_mora = 0

        creditos_cliente = supabase.table("creditos") \
            .select("id") \
            .eq("cliente_id", cliente["id"]) \
            .execute().data

        for credito in creditos_cliente:

            cuotas = supabase.table("cuotas") \
                .select("fecha_pago, estado") \
                .eq("credito_id", credito["id"]) \
                .eq("estado", "pendiente") \
                .execute().data

            for cuota in cuotas:
                fecha = date.fromisoformat(cuota["fecha_pago"])

                if fecha < date.today():
                    dias = (date.today() - fecha).days
                    if dias > mayor_mora:
                        mayor_mora = dias

        cliente["dias_mora"] = mayor_mora

    return render_template(
        "clientes.html",
        clientes=clientes
    )

@app.route("/editar_cliente/<cliente_id>")
def editar_cliente(cliente_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    # ==========================
    # CLIENTE
    # ==========================
    cliente_resp = supabase.table("clientes") \
        .select("*") \
        .eq("id", cliente_id) \
        .single() \
        .execute()

    if not cliente_resp.data:
        flash("Cliente no encontrado","danger")
        return redirect(url_for("clientes"))

    cliente = cliente_resp.data


    # ==========================
    # ÚLTIMO CRÉDITO (para fotos y ubicación)
    # ==========================
    credito_resp = supabase.table("creditos") \
        .select("*") \
        .eq("cliente_id", cliente_id) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    credito = credito_resp.data[0] if credito_resp.data else None


    # ==========================
    # UBICACIÓN
    # ==========================
    latitud = credito.get("latitud") if credito else None
    longitud = credito.get("longitud") if credito else None


    # ==========================
    # FOTOS
    # ==========================
    fotos = {
        "cliente": credito.get("foto_cliente") if credito else None,
        "cedula": credito.get("foto_cedula") if credito else None,
        "negocio": credito.get("foto_negocio") if credito else None,
        "firma": credito.get("firma_cliente") if credito else None
    }


    return render_template(
        "editar_cliente.html",
        cliente=cliente,
        credito=credito,
        fotos=fotos,
        latitud=latitud,
        longitud=longitud
    )

@app.route("/actualizar_cliente", methods=["POST"])
def actualizar_cliente():

    if "user_id" not in session:
        return redirect(url_for("login"))

    cliente_id = request.form.get("cliente_id")

    nombre = request.form.get("nombre")
    identificacion = request.form.get("identificacion")
    direccion = request.form.get("direccion")
    direccion_negocio = request.form.get("direccion_negocio")
    telefono = request.form.get("telefono")
    codigo_pais = request.form.get("codigo_pais")

    # ==========================
    # ACTUALIZAR CLIENTE
    # ==========================

    supabase.table("clientes").update({

        "nombre": nombre,
        "identificacion": identificacion,
        "direccion": direccion,
        "direccion_negocio": direccion_negocio,
        "telefono_principal": telefono,
        "codigo_pais": codigo_pais

    }).eq("id", cliente_id).execute()


    # ==========================
    # OBTENER ÚLTIMO CRÉDITO
    # ==========================

    credito_resp = supabase.table("creditos") \
        .select("id") \
        .eq("cliente_id", cliente_id) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    credito = credito_resp.data[0] if credito_resp.data else None


    if credito:

        credito_id = credito["id"]

        foto_cliente = request.files.get("foto_cliente")
        foto_cedula = request.files.get("foto_cedula")
        foto_negocio = request.files.get("foto_negocio")
        firma_cliente = request.files.get("firma_cliente")

        update_data = {}

        # ==========================
        # FOTO CLIENTE
        # ==========================

        if foto_cliente and foto_cliente.filename:

            filename = f"{cliente_id}_{uuid.uuid4()}_cliente.jpg"

            supabase.storage.from_("clientes").upload(
                filename,
                foto_cliente.read(),
                {"content-type": foto_cliente.content_type}
            )

            url = supabase.storage.from_("clientes").get_public_url(filename)

            update_data["foto_cliente"] = url


        # ==========================
        # FOTO CEDULA
        # ==========================

        if foto_cedula and foto_cedula.filename:

            filename = f"{cliente_id}_{uuid.uuid4()}_cedula.jpg"

            supabase.storage.from_("clientes").upload(
                filename,
                foto_cedula.read(),
                {"content-type": foto_cedula.content_type}
            )

            url = supabase.storage.from_("clientes").get_public_url(filename)

            update_data["foto_cedula"] = url


        # ==========================
        # FOTO NEGOCIO
        # ==========================

        if foto_negocio and foto_negocio.filename:

            filename = f"{cliente_id}_{uuid.uuid4()}_negocio.jpg"

            supabase.storage.from_("clientes").upload(
                filename,
                foto_negocio.read(),
                {"content-type": foto_negocio.content_type}
            )

            url = supabase.storage.from_("clientes").get_public_url(filename)

            update_data["foto_negocio"] = url


        # ==========================
        # FIRMA
        # ==========================

        if firma_cliente and firma_cliente.filename:

            filename = f"{cliente_id}_{uuid.uuid4()}_firma.jpg"

            supabase.storage.from_("clientes").upload(
                filename,
                firma_cliente.read(),
                {"content-type": firma_cliente.content_type}
            )

            url = supabase.storage.from_("clientes").get_public_url(filename)

            update_data["firma_cliente"] = url


        # ==========================
        # ACTUALIZAR CRÉDITO
        # ==========================

        if update_data:

            supabase.table("creditos").update(update_data) \
                .eq("id", credito_id) \
                .execute()


    flash("Cliente actualizado correctamente", "success")

    return redirect(url_for("clientes"))

@app.route("/historico-bancario/<cliente_id>")
def historico_bancario_cliente(cliente_id):

    cliente = supabase.table("clientes") \
        .select("*") \
        .eq("id", cliente_id) \
        .single() \
        .execute().data

    creditos_db = supabase.table("creditos") \
        .select("*") \
        .eq("cliente_id", cliente_id) \
        .order("created_at", desc=True) \
        .execute().data

    creditos = []
    total_mora = 0  # ← será la MAYOR mora del cliente, no suma

    for c in creditos_db:

        cuotas = supabase.table("cuotas") \
            .select("valor, estado, fecha_pago") \
            .eq("credito_id", c["id"]) \
            .execute().data

        total_pagado = 0
        dias_mora_credito = 0  # ← mora máxima del crédito

        for q in cuotas:

            if q["estado"] == "pagado":
                total_pagado += float(q["valor"])

            if q["estado"] == "pendiente":
                fecha = date.fromisoformat(q["fecha_pago"])

                if fecha < date.today():
                    dias = (date.today() - fecha).days

                    # 🔥 TOMAMOS LA MAYOR MORA, NO SUMAMOS
                    if dias > dias_mora_credito:
                        dias_mora_credito = dias

        # 🔥 LA MORA DEL CLIENTE ES LA MAYOR DE TODOS SUS CRÉDITOS
        if dias_mora_credito > total_mora:
            total_mora = dias_mora_credito

        saldo = float(c["valor_total"]) - total_pagado
        intereses = float(c["valor_total"]) - float(c["valor_venta"])

        # Clasificación por crédito
        if dias_mora_credito == 0 and saldo == 0:
            comportamiento = "Pagó al día"
        elif dias_mora_credito == 0:
            comportamiento = "Al día"
        elif dias_mora_credito <= 10:
            comportamiento = "Tuvo mora leve"
        else:
            comportamiento = "Mora significativa"

        creditos.append({
            "codigo": c["id"][:8],
            "capital": float(c["valor_venta"]),
            "intereses": intereses,
            "total": float(c["valor_total"]),
            "pagado": total_pagado,
            "saldo": saldo,
            "estado": c["estado"],
            "fecha": c["created_at"][:10],
            "mora": dias_mora_credito,
            "comportamiento": comportamiento
        })

    # ================= SCORE =================

    score_base = 0

    # 1️⃣ Puntualidad (40)
    if total_mora == 0:
        puntos_puntualidad = 40
    elif total_mora <= 10:
        puntos_puntualidad = 30
    elif total_mora <= 30:
        puntos_puntualidad = 20
    else:
        puntos_puntualidad = 5

    score_base += puntos_puntualidad

    # 2️⃣ Créditos finalizados (25)
    creditos_finalizados = len([c for c in creditos if c["estado"] == "finalizado"])

    if creditos_finalizados >= 3:
        puntos_historial = 25
    elif creditos_finalizados >= 1:
        puntos_historial = 15
    else:
        puntos_historial = 5

    score_base += puntos_historial

    # 3️⃣ Endeudamiento (20)
    saldo_total = sum(c["saldo"] for c in creditos)
    total_creditado = sum(c["total"] for c in creditos)

    ratio = (saldo_total / total_creditado) if total_creditado > 0 else 0

    if saldo_total == 0:
        puntos_endeudamiento = 20
    elif ratio < 0.3:
        puntos_endeudamiento = 15
    elif ratio < 0.6:
        puntos_endeudamiento = 10
    else:
        puntos_endeudamiento = 5

    score_base += puntos_endeudamiento

    # 4️⃣ Antigüedad (15)
    fecha_registro = date.fromisoformat(cliente["created_at"][:10])
    años = (date.today() - fecha_registro).days / 365

    if años >= 2:
        puntos_antiguedad = 15
    elif años >= 1:
        puntos_antiguedad = 10
    else:
        puntos_antiguedad = 5

    score_base += puntos_antiguedad

    # Score final 300-900
    score = 300 + (score_base * 6)

    # ================= CLASIFICACIÓN =================

    if total_mora > 30:
        clasificacion = "Cliente riesgoso"
    elif total_mora > 0:
        clasificacion = "Cliente con mora leve"
    elif creditos_finalizados == 0:
        clasificacion = "Cliente nuevo"
    elif score >= 750:
        clasificacion = "Buen cliente"
    else:
        clasificacion = "Cliente regular"

    return render_template(
        "historico_bancario.html",
        cliente=cliente,
        creditos=creditos,
        score=score,
        clasificacion=clasificacion,
        puntos_puntualidad=puntos_puntualidad,
        puntos_historial=puntos_historial,
        puntos_endeudamiento=puntos_endeudamiento,
        puntos_antiguedad=puntos_antiguedad
    )

@app.route("/cliente/<int:cliente_id>/mapa")
def ver_mapa_cliente(cliente_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    cliente = supabase.table("clientes") \
        .select("*") \
        .eq("id", cliente_id) \
        .single() \
        .execute().data

    if not cliente:
        return redirect(url_for("listar_clientes"))

    return render_template("mapa_cliente.html", cliente=cliente)


# -----------------------
# MODULO CAPITAL
# -----------------------

@app.route("/capital")
def capital():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔹 FILTROS
    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")
    ruta_id_filtro = request.args.get("ruta_id")

    # 🔥 SOLO rutas de la oficina activa
    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute().data or []

    rutas_ids = [r["id"] for r in rutas]

    # 🔥 QUERY BASE (MISMO CONCEPTO TUYO)
    query = supabase.table("capital") \
        .select("*, rutas(nombre)") \
        .in_("ruta_id", rutas_ids)

    # 🔹 FILTRO POR RUTA
    if ruta_id_filtro:
        query = query.eq("ruta_id", ruta_id_filtro)

    # 🔹 FILTRO POR FECHAS
    if fecha_inicio:
        query = query.gte("created_at", fecha_inicio + "T00:00:00")

    if fecha_fin:
        query = query.lte("created_at", fecha_fin + "T23:59:59")

    # 🔥 EJECUTAR QUERY
    movimientos = query \
        .order("created_at", desc=True) \
        .execute().data or []

    # 🔥 TOTAL CAPITAL
    total_capital = sum(float(m.get("valor", 0)) for m in movimientos)

    return render_template(
        "capital.html",
        rutas=rutas,
        capital=movimientos,
        total_capital=total_capital,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        ruta_id_filtro=ruta_id_filtro
    )

@app.route("/capital/crear", methods=["POST"])
def crear_capital():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    ruta_id = request.form.get("ruta_id")
    valor = request.form.get("valor")
    descripcion = request.form.get("descripcion")

    # 🔒 Validar que la ruta pertenezca a la oficina
    ruta_validacion = supabase.table("rutas") \
        .select("id, oficina_id") \
        .eq("id", ruta_id) \
        .single() \
        .execute().data

    if not ruta_validacion or ruta_validacion["oficina_id"] != oficina_id:
        flash("No tiene acceso a esta ruta", "error")
        return redirect(url_for("capital"))

    supabase.table("capital").insert({
        "ruta_id": ruta_id,
        "valor": float(valor),
        "descripcion": descripcion
    }).execute()

    flash("Capital asignado correctamente", "success")
    return redirect(url_for("capital"))
# -----------------------
# MODULO GASTOS
# -----------------------
@app.route("/gastos")
def gastos():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔹 FILTROS
    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")
    ruta_id_filtro = request.args.get("ruta_id")

    # 🔥 RUTAS DE LA OFICINA
    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("nombre") \
        .execute().data or []

    rutas_ids = [r["id"] for r in rutas]

    # 🔥 QUERY BASE
    query = supabase.table("gastos") \
        .select("""
            *,
            rutas(nombre),
            usuarios(nombres, apellidos),
            categorias_gastos(nombre)
        """) \
        .in_("ruta_id", rutas_ids)

    # 🔹 FILTRO POR RUTA
    if ruta_id_filtro:
        query = query.eq("ruta_id", ruta_id_filtro)

    # 🔹 FILTRO POR FECHA
    if fecha_inicio:
        query = query.gte("created_at", fecha_inicio + "T00:00:00")

    if fecha_fin:
        query = query.lte("created_at", fecha_fin + "T23:59:59")

    # 🔥 EJECUTAR
    gastos = query.order("created_at", desc=True).execute().data or []

    total_gastos = 0

    for g in gastos:

        # SUMATORIA
        total_gastos += float(g.get("valor", 0))

        # FECHA
        if g.get("created_at"):
            created = g["created_at"].replace("Z", "+00:00")

            try:
                fecha_utc = datetime.fromisoformat(created)
            except:
                fecha_utc = datetime.fromisoformat(created.split(".")[0] + "+00:00")

            fecha_colombia = fecha_utc - timedelta(hours=5)
            g["fecha_formateada"] = fecha_colombia.strftime("%Y-%m-%d %H:%M:%S")

        # NOMBRE USUARIO
        if g.get("usuarios"):
            nombres = g["usuarios"].get("nombres", "")
            apellidos = g["usuarios"].get("apellidos", "")
            g["cobrador_nombre"] = f"{nombres} {apellidos}".strip()
        else:
            g["cobrador_nombre"] = ""

    # 🔥 CATEGORÍAS
    categorias = supabase.table("categorias_gastos") \
        .select("*") \
        .eq("estado", True) \
        .order("nombre") \
        .execute().data or []

    return render_template(
        "gastos.html",
        gastos=gastos,
        rutas=rutas,
        categorias=categorias,
        total_gastos=total_gastos,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        ruta_id_filtro=ruta_id_filtro
    )
    
@app.route("/guardar_gasto", methods=["POST"])
def guardar_gasto():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    categoria_id = request.form.get("categoria_id")
    descripcion = request.form.get("descripcion")
    valor = float(request.form.get("valor"))
    ruta_id = request.form.get("ruta_id")

    # 🔒 Validar que la ruta pertenezca a la oficina
    ruta_validacion = supabase.table("rutas") \
        .select("id, oficina_id") \
        .eq("id", ruta_id) \
        .single() \
        .execute().data

    if not ruta_validacion or ruta_validacion["oficina_id"] != oficina_id:
        flash("No tiene acceso a esta ruta", "error")
        return redirect(url_for("gastos"))

    codigo = f"GAS-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    supabase.table("gastos").insert({
        "codigo": codigo,
        "ruta_id": ruta_id,
        "usuario_id": session["user_id"],
        "categoria_id": categoria_id,
        "descripcion": descripcion,
        "valor": valor
    }).execute()

    flash("Gasto registrado correctamente", "success")
    return redirect(url_for("gastos"))

@app.route("/eliminar_gasto/<gasto_id>")
def eliminar_gasto(gasto_id):

    supabase.table("gastos") \
        .delete() \
        .eq("id", gasto_id) \
        .execute()

    flash("Gasto eliminado", "warning")
    return redirect(url_for("gastos"))



@app.route("/gastos_cobrador")
def gastos_cobrador():

    if "user_id" not in session:
        return redirect(url_for("login_app"))

    ruta_id = session.get("ruta_id")

    gastos = supabase.table("gastos") \
        .select("*, rutas(nombre), usuarios(nombres, apellidos)") \
        .eq("ruta_id", ruta_id) \
        .order("created_at", desc=True) \
        .execute().data

    for g in gastos:
        if g.get("created_at"):
            fecha_utc = datetime.fromisoformat(g["created_at"].replace("Z", ""))
            fecha_colombia = fecha_utc - timedelta(hours=5)
            g["fecha_formateada"] = fecha_colombia.strftime("%Y-%m-%d %H:%M:%S")

    return render_template(
        "cobrador/gastos.html",
        gastos=gastos
    )

@app.route("/guardar_gasto_cobrador", methods=["POST"])
def guardar_gasto_cobrador():

    if "user_id" not in session:
        return redirect(url_for("login_app"))

    ruta_id = session.get("ruta_id")

    categoria_id = request.form.get("categoria_id")
    descripcion = request.form.get("descripcion")
    valor = request.form.get("valor")

    # 🔥 Generar código automático
    codigo = f"GAS-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    supabase.table("gastos").insert({
        "codigo": codigo,
        "ruta_id": ruta_id,
        "usuario_id": session["user_id"],
        "categoria_id": categoria_id,
        "descripcion": descripcion,
        "valor": float(valor)
    }).execute()

    flash("Gasto registrado correctamente", "success")
    return redirect(url_for("gastos_cobrador"))
    
# -----------------------
# MODULO TRANSFERENCIAS
# -----------------------

@app.route("/transferencias", methods=["GET", "POST"])
def transferencias():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔥 Traer SOLO rutas de la oficina activa
    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute().data or []

    rutas_ids = [r["id"] for r in rutas]

    # ================= POST =================
    if request.method == "POST":

        ruta_origen = int(request.form.get("ruta_origen"))
        ruta_destino = int(request.form.get("ruta_destino"))
        valor = float(request.form.get("valor"))
        fecha = request.form.get("fecha")
        descripcion = request.form.get("descripcion")

        # 🔒 Validar que ambas rutas pertenezcan a la oficina
        if ruta_origen not in rutas_ids or ruta_destino not in rutas_ids:
            flash("No tiene acceso a estas rutas", "error")
            return redirect(url_for("transferencias"))

        if ruta_origen == ruta_destino:
            flash("No puede transferir a la misma ruta", "error")
            return redirect(url_for("transferencias"))

        # 🔎 Capital disponible origen
        capital_origen = supabase.table("capital") \
            .select("valor") \
            .eq("ruta_id", ruta_origen) \
            .execute()

        total_origen = sum(float(c["valor"] or 0) for c in capital_origen.data or [])

        if total_origen < valor:
            flash("Capital insuficiente en la ruta origen", "error")
            return redirect(url_for("transferencias"))

        # 📝 Guardar registro
        supabase.table("transferencias").insert({
            "ruta_origen": ruta_origen,
            "ruta_destino": ruta_destino,
            "valor": valor,
            "descripcion": descripcion,
            "fecha": fecha
        }).execute()

        flash("Transferencia realizada correctamente", "success")
        return redirect(url_for("transferencias"))

    # ================= GET =================

    # Crear diccionario id → nombre (solo oficina activa)
    rutas_dict = {r["id"]: r["nombre"] for r in rutas}

    # 🔥 Traer SOLO transferencias donde participen rutas de la oficina
    transferencias_db = supabase.table("transferencias") \
        .select("*") \
        .in_("ruta_origen", rutas_ids) \
        .order("created_at", desc=True) \
        .execute().data or []

    lista = []
    total = 0

    for t in transferencias_db:
        total += float(t["valor"] or 0)

        created = t["created_at"].replace("Z", "+00:00")

        try:
            fecha_utc = datetime.fromisoformat(created)
        except:
            fecha_utc = datetime.fromisoformat(created.split(".")[0] + "+00:00")

        fecha_colombia = fecha_utc - timedelta(hours=5)
        fecha_formateada = fecha_colombia.strftime("%d/%m/%Y %I:%M %p")

        lista.append({
            "fecha": fecha_formateada,
            "ruta_entrega": rutas_dict.get(t["ruta_origen"], "N/A"),
            "ruta_recibe": rutas_dict.get(t["ruta_destino"], "N/A"),
            "valor": t["valor"],
            "descripcion": t["descripcion"]
        })

    return render_template(
        "transferencias.html",
        rutas=rutas,
        transferencias=lista,
        total=total,
        hoy=date.today().isoformat()
    )
# -----------------------
# MODULO RETIROS
# -----------------------
# -----------------------
# MODULO RETIROS
# -----------------------
@app.route("/retiros")
def retiros():

    retiros = [
        {
            "id": 754,
            "fecha": "2025-12-27",
            "valor": 500000,
            "descripcion": "Reembolso don Jim",
            "ruta": "CENTRO"
        },
        {
            "id": 674,
            "fecha": "2025-12-19",
            "valor": 450000,
            "descripcion": "Intereses don Jim",
            "ruta": "4VIVIANA MILAGROS"
        },
        {
            "id": 614,
            "fecha": "2025-12-06",
            "valor": 725000,
            "descripcion": "Intereses noviembre",
            "ruta": "3MILAGROS"
        },
        {
            "id": 556,
            "fecha": "2025-11-29",
            "valor": 450000,
            "descripcion": "Intereses noviembre",
            "ruta": "4VIVIANA MILAGROS"
        }
    ]

    total = sum(r["valor"] for r in retiros)

    return render_template(
        "retiros.html",
        retiros=retiros,
        total=total,
        hoy=date.today()
    )


# -----------------------
# MODULO CAJA
# -----------------------
@app.route("/reportes")
def reportes():

    pestaña = request.args.get("tab", "ventas")

    # -------------------------
    # DATOS SIMULADOS
    # -------------------------

    ventas = [
        {
            "cliente": "Juan Pérez",
            "total_venta": 500000,
            "tasa": "10%",
            "interes": 50000,
            "total": 550000,
            "saldo": 200000,
            "fecha_registro": "2026-02-01",
            "fecha_final": "2026-02-10",
            "registrado": "Mauricio"
        }
    ]

    liquidacion = [
        {
            "ruta": "Ruta Norte",
            "total_cobrado": 1200000,
            "total_prestamos": 900000,
            "total_gastos": 150000,
            "saldo": 150000
        }
    ]

    return render_template(
        "reportes.html",
        pestaña=pestaña,
        ventas=ventas,
        liquidacion=liquidacion,
        hoy=date.today()
    )

# -----------------------
# DASHBOARD PRINCIPAL
# -----------------------
@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if "oficina_id" not in session:
        return redirect(url_for("cambiar_oficina"))

    return render_template("dashboard.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/logout_app")
def logout_app():
    session.clear()
    return redirect(url_for("login_app"))

if __name__ == "__main__":
    app.run(debug=True)


