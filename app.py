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

app = Flask(__name__)

app.secret_key = "clave_super_segura"

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app.permanent_session_lifetime = timedelta(days=30)

SECURITY_PASSWORD_SALT = "recovery-salt"
serializer = URLSafeTimedSerializer(app.secret_key)


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

def generar_codigo_ruta():
    letras = ''.join(random.choices(string.ascii_uppercase, k=3))
    numeros = ''.join(random.choices(string.digits, k=4))
    return f"R-{letras}{numeros}"

@app.route("/cerrar_cajas_automatico")
def cerrar_cajas_automatico():

    hoy = date.today().isoformat()

    rutas = supabase.table("rutas").select("id").execute()

    for r in rutas.data:

        ruta_id = r["id"]

        # Verificar si ya existe cierre hoy
        caja = supabase.table("caja_diaria") \
            .select("id") \
            .eq("ruta_id", ruta_id) \
            .eq("fecha", hoy) \
            .execute()

        if not caja.data:

            # Obtener último saldo
            cierre_anterior = supabase.table("caja_diaria") \
                .select("saldo_cierre") \
                .eq("ruta_id", ruta_id) \
                .order("fecha", desc=True) \
                .limit(1) \
                .execute()

            saldo = 0

            if cierre_anterior.data:
                saldo = float(cierre_anterior.data[0]["saldo_cierre"])

            # Crear cierre automático
            supabase.table("caja_diaria").insert({
                "ruta_id": ruta_id,
                "fecha": hoy,
                "saldo_inicio": saldo,
                "saldo_cierre": saldo
            }).execute()

    return "Cierre automático ejecutado"
# -----------------------
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

# -----------------------
# LOGIN APP
# -----------------------
@app.route("/login_app", methods=["GET", "POST"])
def login_app():

    if request.method == "POST":

        email = request.form.get("email")
        password = request.form.get("password")
        recordar = request.form.get("recordar")  # 👈 NUEVO

        if not email or not password:
            return render_template(
                "login_app/login_app.html",
                error="Debe ingresar correo y contraseña"
            )

        # 🔎 Buscar solo cobradores activos
        response = supabase.table("usuarios") \
            .select("*") \
            .eq("email", email) \
            .eq("estado", True) \
            .eq("rol", "Cobrador") \
            .execute()

        if not response.data:
            return render_template(
                "login_app/login_app.html",
                error="Usuario no encontrado o no autorizado"
            )

        user = response.data[0]
        stored_password = user["password"]

        login_ok = False

        # 🔐 Si está encriptada
        if stored_password.startswith("scrypt:"):
            if check_password_hash(stored_password, password):
                login_ok = True
        else:
            # 🔄 Migración automática si estaba en texto plano
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

        # 🔐 LOGIN EXITOSO
        session.clear()

        # 👇 AQUÍ VA RECORDAR SESIÓN
        if recordar:
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=30)
        else:
            session.permanent = False

        session["pending_user_id"] = user["id"]
        session["login_tipo"] = "app"

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
            .eq("rol", "Cobrador") \
            .execute()

        if not response.data:
            return redirect(url_for("login_app"))

        user = response.data[0]

        if user["token_ingreso"] == token_ingresado:

            # 🔐 Crear sesión REAL
            session.pop("pending_user_id", None)

            session["user_id"] = user["id"]
            session["rol"] = user["rol"].lower()

            # ✅ Nombre
            nombres = user.get("nombres", "")
            apellidos = user.get("apellidos", "")
            email = user.get("email", "")

            session["nombre"] = nombres
            session["nombre_completo"] = f"{nombres} {apellidos}".strip()
            session["email"] = email


            session.permanent = True
            app.permanent_session_lifetime = timedelta(hours=8)


            return redirect(url_for("dashboard_cobrador"))

        else:
            flash("Token incorrecto.", "error")
            return redirect(url_for("verificar_token_app"))

    return render_template("login_app/verificar_token_app.html")


@app.route("/dashboard_cobrador")
def dashboard_cobrador():

    # 1️⃣ Validar sesión
    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    user_id = int(session["user_id"])

    # 2️⃣ Traer rutas asignadas al usuario
    response = supabase.table("rutas") \
        .select("*") \
        .eq("usuario_id", user_id) \
        .eq("estado", "true") \
        .order("posicion") \
        .execute()

    rutas = response.data if response.data else []

    # 🔥 3️⃣ ASEGURAR RUTA ACTIVA
    if rutas and not session.get("ruta_id"):
        session["ruta_id"] = rutas[0]["id"]

    # 🔥 4️⃣ VALIDAR QUE LA RUTA ACTIVA SIGA EXISTIENDO
    if session.get("ruta_id"):
        ruta_activa_valida = any(r["id"] == session["ruta_id"] for r in rutas)
        if not ruta_activa_valida and rutas:
            session["ruta_id"] = rutas[0]["id"]

    # 5️⃣ Manejar oficinas
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
    
    notificaciones = supabase.table("notificaciones") \
    .select("*") \
    .eq("usuario_id", session["user_id"]) \
    .eq("leida", False) \
    .order("created_at", desc=True) \
    .execute().data

    return render_template(
        "cobrador/dashboard.html",
        rutas=rutas_completas,
        ruta_id=session.get("ruta_id") , # 👈 PASARLO AL TEMPLATE
        notificaciones=notificaciones   # 👈 AQUÍ

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
@app.route("/credito/<credito_id>")
def detalle_credito(credito_id):

    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    # 🔎 Traer crédito + cliente con campos necesarios
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

    # 🔎 Traer cuotas
    cuotas_db = supabase.table("cuotas") \
        .select("*") \
        .eq("credito_id", credito_id) \
        .order("numero") \
        .execute().data

    total_pagado = 0
    cuotas = []
    proxima_cuota = None

    for c in cuotas_db:

        dias_mora = 0

        # 🔹 Sumar pagadas
        if c["estado"] == "pagado":
            total_pagado += float(c.get("monto_pagado", c["valor"]))

        # 🔹 Detectar mora
        if c["estado"] == "pendiente":
            fecha = date.fromisoformat(c["fecha_pago"])
            if fecha < date.today():
                dias_mora = (date.today() - fecha).days

            # 🔹 Primera cuota pendiente = próxima cuota
            if not proxima_cuota:
                proxima_cuota = c["fecha_pago"]

        cuotas.append({
            "id": c["id"],
            "numero": c["numero"],
            "valor": c["valor"],
            "estado": c["estado"],
            "fecha_pago": c["fecha_pago"],
            "dias_mora": dias_mora
        })

    # 🔹 Calcular saldo
    saldo = float(credito["valor_total"]) - total_pagado

    return render_template(
        "cobrador/detalle_credito.html",
        credito=credito,
        cuotas=cuotas,
        saldo=saldo,
        total_pagado=total_pagado,
        proxima_cuota=proxima_cuota
    )

@app.route("/registrar_pago", methods=["POST"])
def registrar_pago():

    cuota_id = request.form.get("cuota_id")

    monto_pago_raw = request.form.get("monto_pago", "").strip()
    monto_adicional_raw = request.form.get("monto_adicional", "").strip()

    monto_pago = float(monto_pago_raw) if monto_pago_raw != "" else None
    monto_adicional = float(monto_adicional_raw) if monto_adicional_raw != "" else 0.0

    if not cuota_id:
        return redirect(request.referrer)

    cuota_resp = supabase.table("cuotas") \
        .select("*") \
        .eq("id", cuota_id) \
        .single() \
        .execute()

    if not cuota_resp.data:
        return redirect(request.referrer)

    cuota = cuota_resp.data
    credito_id = cuota["credito_id"]

    valor_cuota = float(cuota.get("valor") or 0)

    # =====================================================
    # CALCULAR PAGO
    # =====================================================

    if monto_pago is not None:
        dinero = monto_pago
    else:
        dinero = valor_cuota + monto_adicional

    dinero_restante = dinero

    # =====================================================
    # BUSCAR CUOTAS PENDIENTES
    # =====================================================

    cuotas_resp = supabase.table("cuotas") \
        .select("*") \
        .eq("credito_id", credito_id) \
        .order("numero") \
        .execute()

    cuotas = cuotas_resp.data or []

    primera_cuota_afectada = None

    for c in cuotas:

        if dinero_restante <= 0:
            break

        valor = float(c.get("valor") or 0)
        pagado = float(c.get("monto_pagado") or 0)

        saldo = valor - pagado

        if saldo <= 0:
            continue

        if primera_cuota_afectada is None:
            primera_cuota_afectada = c["id"]

        # ==========================================
        # PAGO COMPLETO
        # ==========================================

        if dinero_restante >= saldo:

            nuevo_pagado = pagado + saldo

            supabase.table("cuotas").update({
                "monto_pagado": nuevo_pagado,
                "estado": "pagado",
                "fecha_pago_real": datetime.now().isoformat()
            }).eq("id", c["id"]).execute()

            dinero_restante -= saldo

        # ==========================================
        # PAGO PARCIAL
        # ==========================================

        else:

            nuevo_pagado = pagado + dinero_restante

            supabase.table("cuotas").update({
                "monto_pagado": nuevo_pagado
            }).eq("id", c["id"]).execute()

            dinero_restante = 0

    # =====================================================
    # REGISTRAR PAGO
    # =====================================================

    pago_resp = supabase.table("pagos").insert({
        "cuota_id": primera_cuota_afectada,
        "credito_id": credito_id,
        "monto": dinero,
        "fecha": datetime.now().isoformat(),
        "cobrador_id": session["user_id"]
    }).execute()

    pago_id = pago_resp.data[0]["id"]

    # =====================================================
    # VERIFICAR SI EL CRÉDITO TERMINÓ
    # =====================================================

    cuotas_pendientes = supabase.table("cuotas") \
        .select("id") \
        .eq("credito_id", credito_id) \
        .eq("estado", "pendiente") \
        .execute()

    if not cuotas_pendientes.data:
        supabase.table("creditos").update({
            "estado": "pagado"
        }).eq("id", credito_id).execute()

    return redirect(url_for("recibo_pago", pago_id=pago_id))

    
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

    # Calcular saldo restante
    credito_id = pago["cuotas"]["credito_id"]

    cuotas = supabase.table("cuotas") \
        .select("*") \
        .eq("credito_id", credito_id) \
        .execute().data

    total_pagado = sum(float(c.get("monto_pagado", 0)) for c in cuotas if c["estado"] == "pagado")
    saldo_restante = float(pago["cuotas"]["creditos"]["valor_total"]) - total_pagado

    return render_template(
        "cobrador/recibo_pago.html",
        pago=pago,
        saldo_restante=saldo_restante
    )




# =============================
# NUEVA VENTA COBRADOR (CONTROL FLUJO)
# =============================
@app.route("/nueva_venta_cobrador")
def nueva_venta_cobrador():

    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    user_id = int(session["user_id"])

    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("usuario_id", user_id) \
        .eq("estado", "true") \
        .order("posicion") \
        .execute().data or []

    ruta_actual = session.get("ruta_id")

    # 🔥 Detectar si viene de aumento
    cedula_aprobada = request.args.get("cedula")
    monto_aprobado = request.args.get("monto")

    cliente_data = None

    if cedula_aprobada:

        cedula_busqueda = cedula_aprobada.strip()

        cliente_resp = supabase.table("clientes") \
            .select("*") \
            .ilike("identificacion", cedula_busqueda) \
            .limit(1) \
            .execute()

        print("DEBUG CLIENTE:", cliente_resp.data)

        if cliente_resp.data:
            cliente_data = cliente_resp.data[0]

    return render_template(
        "cobrador/nueva_venta_cobrador.html",
        rutas=rutas,
        ruta_actual=ruta_actual,
        cliente_aprobado=cliente_data,
        monto_aprobado=monto_aprobado
    )

@app.route("/buzon_aumento_cupo")
def buzon_aumento_cupo():

    # 🔐 Validar sesión
    if "user_id" not in session or session.get("rol") != "cobrador":
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
    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    # 🔒 Validar ruta activa
    if not session.get("ruta_id"):
        flash("Debe seleccionar una ruta", "warning")
        return redirect(url_for("dashboard_cobrador"))

    return render_template("cobrador/nueva_solicitud_cupo.html")


@app.route("/buscar_cliente_por_cedula/<cedula>")
def buscar_cliente_por_cedula(cedula):

    try:
        cedula_int = int(cedula)
    except:
        return jsonify({"success": False})

    cliente = supabase.table("clientes") \
        .select("id, nombre, direccion, telefono_principal") \
        .eq("identificacion", cedula_int) \
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
    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    cedula = request.form.get("cedula", "").strip()
    monto_raw = request.form.get("monto", "").strip()

    # ==========================
    # VALIDAR DATOS
    # ==========================

    if not cedula or not monto_raw:
        flash("Todos los campos son obligatorios", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    try:
        monto = float(monto_raw.replace(".", "").replace(",", "."))
        if monto <= 0:
            raise ValueError
    except:
        flash("Monto inválido", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    # ==========================
    # VALIDAR QUE CLIENTE EXISTA
    # ==========================

    cliente = supabase.table("clientes") \
        .select("id, nombre, identificacion") \
        .eq("identificacion", cedula) \
        .limit(1) \
        .execute()

    if not cliente.data:
        flash("El cliente no existe en el sistema", "danger")
        return redirect(url_for("nueva_solicitud_cupo"))

    cliente_data = cliente.data[0]

    # ==========================
    # VALIDAR QUE NO EXISTA PENDIENTE
    # ==========================

    pendiente = supabase.table("solicitudes_aumento_cupo") \
        .select("id") \
        .eq("cedula", cedula) \
        .eq("estado", "pendiente") \
        .limit(1) \
        .execute()

    if pendiente.data:
        flash("Ya existe una solicitud pendiente para este cliente", "warning")
        return redirect(url_for("buzon_aumento_cupo"))

    # ==========================
    # CREAR SOLICITUD
    # ==========================

    insert = supabase.table("solicitudes_aumento_cupo").insert({
        "cliente_id": cliente_data["id"],  # 🔥 mejor guardar id real
        "cliente_nombre": cliente_data["nombre"],
        "cedula": cedula,
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

@app.route("/admin/solicitud/<id>/<accion>")
def procesar_solicitud(id, accion):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if accion not in ["aprobado", "rechazado"]:
        return redirect(url_for("ver_solicitudes_cupo"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔎 Traer solicitud
    solicitud_resp = supabase.table("solicitudes_aumento_cupo") \
        .select("*") \
        .eq("id", id) \
        .single() \
        .execute()

    if not solicitud_resp.data:
        flash("Solicitud no encontrada", "danger")
        return redirect(url_for("ver_solicitudes_cupo"))

    solicitud = solicitud_resp.data

    # 🔒 Validar que la solicitud pertenezca a una ruta de la oficina activa
    ruta_validacion = supabase.table("rutas") \
        .select("id, oficina_id") \
        .eq("id", solicitud["ruta_id"]) \
        .single() \
        .execute().data

    if not ruta_validacion or ruta_validacion["oficina_id"] != oficina_id:
        flash("No tiene permiso para modificar esta solicitud", "danger")
        return redirect(url_for("ver_solicitudes_cupo"))

    # 🔥 Actualizar estado
    supabase.table("solicitudes_aumento_cupo") \
        .update({"estado": accion}) \
        .eq("id", id) \
        .execute()

    # 🔔 Crear notificación si existe usuario
    if solicitud.get("usuario_id"):

        supabase.table("notificaciones").insert({
            "usuario_id": solicitud["usuario_id"],
            "titulo": "Solicitud de cupo actualizada",
            "mensaje": f"Tu solicitud para {solicitud['cliente_nombre']} fue {accion.upper()}",
            "tipo": "success" if accion == "aprobado" else "danger",
            "leida": False
        }).execute()

    flash("Solicitud actualizada", "success")
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

    return render_template(
        "solicitudes_cupo.html",
        solicitudes=solicitudes
    )
@app.route("/guardar_venta_cobrador", methods=["POST"])
def guardar_venta_cobrador():

    if "user_id" not in session:
        return redirect(url_for("login_app"))

    # 🔹 Traer rutas
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

    # ==========================
    # VALIDAR CAMPOS NUMÉRICOS
    # ==========================
    try:
        valor_venta_raw = request.form.get("valor_venta", "").strip()
        tasa_raw = request.form.get("tasa", "").strip()
        cuotas_raw = request.form.get("cuotas", "").strip()

        valor_venta = float(valor_venta_raw.replace(".", "").replace(",", "."))
        tasa = float(tasa_raw.replace(",", "."))
        cuotas = int(cuotas_raw)

        if valor_venta <= 0 or cuotas <= 0:
            raise ValueError

    except Exception as e:
        print("ERROR NUMERICO:", e)
        flash("Datos numéricos inválidos", "danger")
        return render_template(
            "cobrador/nueva_venta_cobrador.html",
            rutas=rutas,
            ruta_actual=ruta_id,
            form_data=request.form
        )

    identificacion = (request.form.get("identificacion") or "").strip()
    nombre = request.form.get("nombre")
    direccion = request.form.get("direccion")
    direccion_negocio = request.form.get("direccion_negocio")
    codigo_pais = request.form.get("codigo_pais") or "57"
    telefono = request.form.get("telefono")
    fecha_inicio = (date.today() + timedelta(days=1)).isoformat()
    tipo_prestamo = request.form.get("tipo_prestamo")

    # ==========================
    # VALIDAR CUPO MÁXIMO RUTA COBRADOR
    # ==========================
    ruta_data = supabase.table("rutas") \
        .select("venta_maxima") \
        .eq("id", ruta_id) \
        .single() \
        .execute()

    if not ruta_data.data:
        flash("Ruta no válida", "danger")
        return redirect(url_for("dashboard_cobrador"))

    venta_maxima_permitida = float(ruta_data.data["venta_maxima"])

    if valor_venta > venta_maxima_permitida:
        flash(
            f"El monto supera la venta máxima permitida para esta ruta (${venta_maxima_permitida:,.0f})",
            "danger"
        )
        return render_template(
            "cobrador/nueva_venta_cobrador.html",
            rutas=rutas,
            ruta_actual=ruta_id,
            form_data=request.form
        )

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

    if posiciones:
        nueva_posicion = max(posiciones) + 1
    else:
        nueva_posicion = 1
    # ==========================
    # ✅ EVITAR CRÉDITO DUPLICADO POR CÉDULA (CLIENTE + RUTA)
    # (ANTES de subir firma/fotos para no crear archivos huérfanos)
    # ==========================
    cliente_existente_resp = supabase.table("clientes") \
        .select("id") \
        .eq("identificacion", identificacion) \
        .limit(1) \
        .execute()

    if cliente_existente_resp.data:
        cliente_id_existente = cliente_existente_resp.data[0]["id"]

        credito_dup_resp = supabase.table("creditos") \
            .select("id") \
            .eq("cliente_id", cliente_id_existente) \
            .eq("ruta_id", ruta_id) \
            .eq("estado", "activo") \
            .limit(1) \
            .execute()

        if credito_dup_resp.data:
            credito_existente_id = credito_dup_resp.data[0]["id"]
            flash(
                "Este cliente (cédula) ya tiene un crédito activo en esta ruta. No se puede registrar duplicado.",
                "danger"
            )
            return redirect(url_for("detalle_credito", credito_id=credito_existente_id))

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

        # 🔥 Actualizar teléfono y país si cambian
        supabase.table("clientes").update({
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
            return render_template(
                "cobrador/nueva_venta_cobrador.html",
                rutas=rutas,
                ruta_actual=ruta_id,
                form_data=request.form
            )

        cliente_id = nuevo_cliente.data[0]["id"]

    # ==========================
    # PROCESAR FIRMA
    # ==========================
    firma_url = None
    firma_base64 = request.form.get("firma_cliente")

    if firma_base64 and "base64," in firma_base64:
        try:
            header, encoded = firma_base64.split(",", 1)
            firma_bytes = base64.b64decode(encoded)

            image = Image.open(BytesIO(firma_bytes)).convert("RGBA")

            # 🔹 Crear fondo blanco
            background = Image.new("RGB", image.size, (255, 255, 255))

            # 🔹 Pegar firma sobre fondo blanco usando canal alpha
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

            firma_url = supabase.storage.from_("clientes").get_public_url(firma_filename)

        except Exception as e:
            print("Error procesando firma:", e)

    # ==========================
    # SUBIR FOTOS
    # ==========================
    foto_cliente = request.files.get("foto_cliente")
    foto_cedula = request.files.get("foto_cedula")
    foto_negocio = request.files.get("foto_negocio")

    cliente_url = None
    if foto_cliente:
        try:
            cliente_path = f"{cliente_id}_{uuid.uuid4()}_cliente.jpg"

            supabase.storage.from_("clientes").upload(
                cliente_path,
                foto_cliente.read(),
                {"content-type": foto_cliente.content_type}
            )

            cliente_url = supabase.storage.from_("clientes").get_public_url(cliente_path)

        except Exception as e:
            print("Error subiendo foto cliente:", e)

    cedula_url = None
    if foto_cedula:
        try:
            cedula_path = f"{cliente_id}_{uuid.uuid4()}_cedula.jpg"

            supabase.storage.from_("clientes").upload(
                cedula_path,
                foto_cedula.read(),
                {"content-type": foto_cedula.content_type}
            )

            cedula_url = supabase.storage.from_("clientes").get_public_url(cedula_path)

        except Exception as e:
            print("Error subiendo cédula:", e)

    negocio_url = None
    if foto_negocio:
        try:
            negocio_path = f"{cliente_id}_{uuid.uuid4()}_negocio.jpg"

            supabase.storage.from_("clientes").upload(
                negocio_path,
                foto_negocio.read(),
                {"content-type": foto_negocio.content_type}
            )

            negocio_url = supabase.storage.from_("clientes").get_public_url(negocio_path)

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
    valor_total = valor_venta + (valor_venta * tasa / 100)
    valor_cuota = valor_total / cuotas

    credito_resp = supabase.table("creditos").insert({
        "cliente_id": cliente_id,
        "ruta_id": ruta_id,
        "posicion": nueva_posicion,
        "tipo_prestamo": tipo_prestamo,
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
        "longitud": float(longitud) if longitud else None
    }).execute()

    if not credito_resp.data:
        flash("Error al registrar el crédito", "danger")
        return render_template(
            "cobrador/nueva_venta_cobrador.html",
            rutas=rutas,
            ruta_actual=ruta_id,
            form_data=request.form
        )

    credito_id = credito_resp.data[0]["id"]

    # ==========================
    # CREAR CUOTAS SEGÚN TIPO
    # ==========================
    fecha_base = datetime.strptime(fecha_inicio, "%Y-%m-%d")  # mañana
    fecha_actual = fecha_base
    cuotas_creadas = 0

    while cuotas_creadas < cuotas:

        crear_cuota = False

        # ==========================
        # 🔵 SEMANAL
        # ==========================
        if tipo_prestamo == "Semanal":
            fecha_pago = fecha_base + timedelta(days=(cuotas_creadas + 1) * 7)
            crear_cuota = True

        # ==========================
        # 🟢 DIARIO LUNES A VIERNES
        # ==========================
        elif tipo_prestamo == "Diario Lunes a Viernes":
            if fecha_actual.weekday() < 5:  # 0-4 = Lunes a Viernes
                fecha_pago = fecha_actual
                crear_cuota = True

        # ==========================
        # 🟡 DIARIO LUNES A SÁBADO
        # ==========================
        elif tipo_prestamo == "Diario Lunes a Sábado":
            if fecha_actual.weekday() < 6:  # 0-5 = Lunes a Sábado
                fecha_pago = fecha_actual
                crear_cuota = True

        # ==========================
        # 🔹 DEFAULT
        # ==========================
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

        # 🔥 Solo avanzar día en modo diario
        if tipo_prestamo != "Semanal":
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

    # mover temporalmente
    supabase.table("creditos") \
        .update({"posicion": -1}) \
        .eq("id", credito_id) \
        .execute()

    if nueva_posicion < vieja_posicion:
        # mover hacia arriba
        creditos = supabase.table("creditos") \
            .select("id, posicion") \
            .eq("ruta_id", ruta_id) \
            .gte("posicion", nueva_posicion) \
            .lt("posicion", vieja_posicion) \
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
            .gt("posicion", vieja_posicion) \
            .lte("posicion", nueva_posicion) \
            .execute().data

        for c in creditos:
            supabase.table("creditos") \
                .update({"posicion": c["posicion"] - 1}) \
                .eq("id", c["id"]) \
                .execute()

    # colocar en posición final
    supabase.table("creditos") \
        .update({"posicion": nueva_posicion}) \
        .eq("id", credito_id) \
        .execute()

    return redirect(url_for("todas_las_ventas"))

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

    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    hoy = date.today().isoformat()

    # Traer créditos activos con info cliente
    response = supabase.table("creditos") \
        .select("""
            id,
            cliente_id,
            posicion,
            valor_cuota,
            valor_total,
            tipo_prestamo,
            clientes(
                nombre,
                identificacion,
                telefono_principal
            )
        """) \
        .eq("ruta_id", ruta_id) \
        .eq("estado", "activo") \
        .order("posicion") \
        .execute()

    creditos = response.data if response.data else []
    lista = []
    for c in creditos:

        cuotas = supabase.table("cuotas") \
            .select("estado, valor, fecha_pago") \
            .eq("credito_id", c["id"]) \
            .order("fecha_pago") \
            .execute().data

        pago_hoy = None   # 🔥 importante
        valor_hoy = 0
        proxima_cuota = None

        for cuota in cuotas:

            # 🔹 Detectar cuota de hoy
            if cuota["fecha_pago"] == hoy:
                valor_hoy = cuota["valor"]

                if cuota["estado"] == "pagado":
                    pago_hoy = True
                else:
                    pago_hoy = False

            # 🔹 Detectar próxima pendiente
            if cuota["estado"] == "pendiente" and not proxima_cuota:
                proxima_cuota = cuota["fecha_pago"]

        # 🔥 Si no tiene cuota hoy, no debe pagar hoy
        if pago_hoy is None:
            pago_hoy = True

        lista.append({
            "id": c["id"],
            "cliente_id": c["cliente_id"], 
            "posicion": c["posicion"],
            "cliente": c["clientes"]["nombre"],
            "telefono": c["clientes"]["telefono_principal"],
            "valor_total": "{:,.0f}".format(c["valor_total"]),
            "valor_hoy": "{:,.0f}".format(valor_hoy),
            "proxima_cuota": proxima_cuota,
            "pago_hoy": pago_hoy
        })



    return render_template(
        "cobrador/todas_las_ventas.html",
        creditos=lista,
        ruta_id=ruta_id
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
        # CAPITAL COLOCADO (SOLO CAPITAL)
        # =============================

        creditos_activos = supabase.table("creditos") \
            .select("id, valor_venta") \
            .eq("ruta_id", ruta_id) \
            .eq("estado", "activo") \
            .execute().data or []

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
        # =============================

        capital_disponible = (
            capital_asignado
            + total_transferencias_recibidas
            - total_transferencias_enviadas
            - capital_colocado
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

        # 🔥 ESTE ES EL QUE FALTABA
        saldo_actual = total_cobros - total_prestamos - total_gastos
        lista_rutas.append({
            "ruta_id": ruta_id,
            "ruta_nombre": r["nombre"],
            "capital_asignado": capital_asignado,
            "capital_colocado": capital_colocado,
            "capital_disponible": capital_disponible,
            "saldo_actual": saldo_actual,  # 👈 ESTE FALTABA
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

    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    ruta_id = session.get("ruta_id")

    if not ruta_id:
        return redirect(url_for("dashboard_cobrador"))

    hoy = date.today()
    hoy_iso = hoy.isoformat()

    inicio_dia = hoy_iso + "T00:00:00"
    fin_dia = hoy_iso + "T23:59:59"

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
        .lte("created_at", fin_dia) \
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
        .lte("created_at", fin_dia) \
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
        .lte("fecha", fin_dia) \
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
        .select("valor, categoria_id, descripcion") \
        .eq("ruta_id", ruta_id) \
        .gte("created_at", inicio_dia) \
        .lte("created_at", fin_dia) \
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
    # SALDO DISPONIBLE (FÓRMULA CORRECTA)
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
    # RENDER
    # =====================================================
    print("SALDO ANTERIOR:", saldo_anterior)
    print("ABONO CAPITAL:", total_abono_capital)
    print("PRESTAMOS:", total_prestamos)
    print("SALDO ACTUAL:", saldo_actual)
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

    if "user_id" not in session or session.get("rol") != "cobrador":
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

    if "user_id" not in session or session.get("rol") != "cobrador":
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
    fotos = []
    credito_ids = []

    for c in historial_creditos:

        credito_ids.append(c["id"])
        total_prestado += float(c.get("valor_total") or 0)

        # 👇 Este mantiene tu comportamiento anterior
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

    # =====================================================
    # PAGOS (agregamos registrado_por)
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
    puede_renovar = False

    if credito_activo:

        cuotas_resp = supabase.table("cuotas") \
            .select("estado") \
            .eq("credito_id", credito_activo["id"]) \
            .execute()

        cuotas = cuotas_resp.data or []

        # 🔹 Validar si todas las cuotas están pagadas
        todas_pagadas = cuotas and all(
            c["estado"] == "pagado"
            for c in cuotas
        )

        # 🔹 Calcular saldo real del crédito activo
        saldo_credito_activo = float(credito_activo.get("valor_total") or 0) - total_pagado

        # 🔹 Puede renovar SOLO si:
        #    - Todas las cuotas están pagadas
        #    - El saldo es 0
        if todas_pagadas and saldo_credito_activo <= 0:

            puede_renovar = True

            # 🔥 Cerrar automáticamente el crédito
            supabase.table("creditos") \
                .update({"estado": "finalizado"}) \
                .eq("id", credito_activo["id"]) \
                .execute()

    saldo_total_cliente = round(total_prestado - total_pagado, 2)

    return render_template(
        "cobrador/detalle_cliente.html",
        cliente=cliente,
        credito=credito,  # 👈 mantiene tu HTML original
        credito_activo=credito_activo,
        historial_creditos=historial_creditos,
        historial_pagos=historial_pagos,
        cuotas=cuotas,
        puede_renovar=puede_renovar,
        ruta_id=ruta_id,
        total_prestado=total_prestado,
        total_pagado=total_pagado,
        saldo=saldo_total_cliente,
        fotos=fotos
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

    if "user_id" not in session or session.get("rol") != "cobrador":
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
    if "user_id" not in session or session.get("rol") != "cobrador":
        return redirect(url_for("login_app"))

    user_id = int(session["user_id"])

    # 🔎 2️⃣ Validar que la ruta le pertenezca
    ruta_resp = supabase.table("rutas") \
        .select("*") \
        .eq("id", ruta_id) \
        .eq("usuario_id", user_id) \
        .single() \
        .execute()

    if not ruta_resp.data:
        return redirect(url_for("dashboard_cobrador"))

    ruta = ruta_resp.data

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

    # 🔎 5️⃣ Procesar cada crédito
    for c in creditos:

        cuotas = supabase.table("cuotas") \
            .select("valor, estado, fecha_pago") \
            .eq("credito_id", c["id"]) \
            .order("numero") \
            .execute().data

        total_pagado = 0
        dias_mora = 0
        proxima_cuota = None
        mostrar_en_buzon = False
        hoy = date.today()

        for cuota in cuotas:

            fecha_pago = date.fromisoformat(cuota["fecha_pago"])

            if cuota["estado"] == "pagado":
                total_pagado += float(cuota["valor"])

            if cuota["estado"] == "pendiente":

                # Mostrar si es hoy o vencida
                if fecha_pago <= hoy:
                    mostrar_en_buzon = True

                # Calcular mora real
                if fecha_pago < hoy:
                    dias_mora += (hoy - fecha_pago).days

                if not proxima_cuota:
                    proxima_cuota = cuota["fecha_pago"]

        saldo = float(c["valor_total"]) - total_pagado

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
            "saldo": "{:,.0f}".format(saldo),
            "dias_mora": dias_mora,
            "proxima_cuota": proxima_cuota,
            "codigo": c["id"][:6],
            "color_estado": color_estado  # 👈 IMPORTANTE
        })

    # 🔥 6️⃣ Enviar ruta_id al template (clave para el layout)
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
# =============================
# NUEVA VENTA (CONTROL FLUJO)
# =============================
@app.route("/nueva_venta")
def nueva_venta():

    if "user_id" not in session:
        return redirect(url_for("login"))

    # 🔥 Tomar oficina desde sesión (como en usuarios)
    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔹 Traer solo rutas de esa oficina
    rutas_resp = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute()

    rutas = rutas_resp.data if rutas_resp.data else []

    cliente = None
    valor_anterior = None

    # 🔹 Si viene cliente en sesión (normal o renovación)
    cliente_id = session.get("cliente_id")

    if cliente_id:
        cliente_resp = supabase.table("clientes") \
            .select("*") \
            .eq("id", cliente_id) \
            .execute()

        if cliente_resp.data:
            cliente = cliente_resp.data[0]

    # 🔹 Si es renovación, traer valor anterior
    valor_anterior = session.get("valor_anterior")

    return render_template(
        "nueva_venta.html",
        rutas=rutas,
        cliente=cliente,
        valor_anterior=valor_anterior
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
        .execute().data

    for credito in creditos_cliente:

        cuotas_pendientes = supabase.table("cuotas") \
            .select("id") \
            .eq("credito_id", credito["id"]) \
            .neq("estado", "pagado") \
            .execute()

        if cuotas_pendientes.data:
            flash("El cliente tiene un crédito con saldo pendiente", "error")
            return redirect(url_for("nueva_venta"))


    # 🔹 Datos del formulario
    valor_venta = float(request.form["valor_venta"])
    tasa = float(request.form["tasa"])
    cuotas = int(request.form["cuotas"])

    valor_total = valor_venta + (valor_venta * tasa / 100)
    valor_cuota = valor_total / cuotas

    ruta_id = request.form["ruta_id"]
    # 🔹 Obtener última posición en esa ruta
    ultimo = supabase.table("creditos") \
        .select("posicion") \
        .eq("ruta_id", ruta_id) \
        .order("posicion", desc=True) \
        .limit(1) \
        .execute().data

    if ultimo:
        nueva_posicion = ultimo[0]["posicion"] + 1
    else:
        nueva_posicion = 1
    # 🔹 Insertar nuevo crédito
    credito_data = {
        "cliente_id": cliente_id,
        "ruta_id": request.form["ruta_id"],
        "tipo_prestamo": request.form["tipo_prestamo"],
        "posicion": nueva_posicion,
        "valor_venta": valor_venta,
        "tasa": tasa,
        "valor_total": valor_total,
        "cantidad_cuotas": cuotas,
        "valor_cuota": valor_cuota,
        "fecha_inicio": request.form["fecha_inicio"],
        "estado": "activo"
    }

    credito_resp = supabase.table("creditos").insert(credito_data).execute()

    if not credito_resp.data:
        flash("Error al registrar el crédito", "error")
        return redirect(url_for("nueva_venta"))

    credito_id = credito_resp.data[0]["id"]

    # 🔹 Crear cuotas automáticamente
    fecha_inicio = datetime.strptime(request.form["fecha_inicio"], "%Y-%m-%d")

    for i in range(cuotas):

        cuota_data = {
            "credito_id": credito_id,
            "numero": i + 1,
            "valor": valor_cuota,
            "estado": "pendiente",
            "fecha_pago": (fecha_inicio + timedelta(days=i)).date().isoformat()
        }

        supabase.table("cuotas").insert(cuota_data).execute()

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

    # 🔹 Limpiar sesión
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

    # 🔥 SOLO rutas de la oficina activa
    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("posicion") \
        .execute().data or []

    rutas_ids = [r["id"] for r in rutas]

    # 🔥 SOLO movimientos de capital de esas rutas
    movimientos = supabase.table("capital") \
        .select("*, rutas(nombre)") \
        .in_("ruta_id", rutas_ids) \
        .order("created_at", desc=True) \
        .execute().data or []

    return render_template(
        "capital.html",
        rutas=rutas,
        capital=movimientos
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
@app.route("/gastos")
def gastos():

    if "user_id" not in session:
        return redirect(url_for("login"))

    oficina_id = session.get("oficina_id")

    if not oficina_id:
        flash("Debe seleccionar una oficina", "warning")
        return redirect(url_for("cambiar_oficina"))

    # 🔥 SOLO rutas de la oficina activa
    rutas = supabase.table("rutas") \
        .select("*") \
        .eq("oficina_id", oficina_id) \
        .order("nombre") \
        .execute().data or []

    rutas_ids = [r["id"] for r in rutas]

    # 🔥 SOLO gastos de rutas de esta oficina
    gastos = supabase.table("gastos") \
        .select("""
            *,
            rutas(nombre),
            usuarios(nombres, apellidos),
            categorias_gastos(nombre)
        """) \
        .in_("ruta_id", rutas_ids) \
        .order("created_at", desc=True) \
        .execute().data or []

    for g in gastos:

        if g.get("created_at"):
            created = g["created_at"].replace("Z", "+00:00")

            try:
                fecha_utc = datetime.fromisoformat(created)
            except:
                fecha_utc = datetime.fromisoformat(created.split(".")[0] + "+00:00")

            fecha_colombia = fecha_utc - timedelta(hours=5)
            g["fecha_formateada"] = fecha_colombia.strftime("%Y-%m-%d %H:%M:%S")

        if g.get("usuarios"):
            nombres = g["usuarios"].get("nombres", "")
            apellidos = g["usuarios"].get("apellidos", "")
            g["cobrador_nombre"] = f"{nombres} {apellidos}".strip()
        else:
            g["cobrador_nombre"] = ""

    # 🔥 SOLO categorías activas
    categorias = supabase.table("categorias_gastos") \
        .select("*") \
        .eq("estado", True) \
        .order("nombre") \
        .execute().data or []

    return render_template(
        "gastos.html",
        gastos=gastos,
        rutas=rutas,
        categorias=categorias
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
    return redirect(url_for("login_app/login_app.html"))


if __name__ == "__main__":
    app.run(debug=True)


