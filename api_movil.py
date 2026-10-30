# -*- coding: utf-8 -*-
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends, Header
import base64
import calendar
import os
import json
import re
import time
from datetime import datetime, timedelta
from pydantic import BaseModel
from google import genai
from google.genai import types
from conexion import conectar_db, liberar_conexion
from auth import verify_password, generar_token

# --- CONFIGURACIÓN DE LA IA (GOOGLE GEMINI) PARA OCR DE TICKETS ---
# La clave NUNCA se escribe en el código: se lee de la variable de entorno GEMINI_API_KEY.
#   IMPORTANTE: el código se ejecuta en RENDER, no en GitHub Actions. Por eso la variable
#   debe estar en Render > (servicio) > Environment > GEMINI_API_KEY.
#   Un "GitHub Secret" (Settings > Secrets and variables > Actions) solo alimenta workflows
#   de GitHub; NO llega al servidor de Render.
# Clave GRATIS de Google AI Studio: https://aistudio.google.com/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Modelo de OCR. Si Google retira/renombra un modelo, cambia GEMINI_MODELO en Render o
# deja que el respaldo automático pruebe los siguientes de la lista.
GEMINI_MODELO = os.environ.get("GEMINI_MODELO", "gemini-flash-latest").strip()
GEMINI_MODELOS_RESPALDO = [
    m.strip() for m in os.environ.get(
        "GEMINI_MODELOS_RESPALDO",
        "gemini-flash-latest,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-2.5-flash",
    ).split(",") if m.strip()
]

if not GEMINI_API_KEY:
    print("[AVISO] GEMINI_API_KEY no configurada: el OCR con IA quedara desactivado hasta definir la variable de entorno.")

# Timeout y reintentos ACOTADOS: por defecto el SDK reintenta con esperas largas y, si un
# modelo falla, puede tardar más de un minuto (eso hacía parecer que el ticket se colgaba).
if GEMINI_API_KEY:
    try:
        cliente_ia = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(
                timeout=45000,
                retry_options=types.HttpRetryOptions(attempts=2),
            ),
        )
    except Exception as e:
        print(f"[AVISO] No se pudieron aplicar las opciones HTTP ({e}); se usan las de por defecto.")
        cliente_ia = genai.Client(api_key=GEMINI_API_KEY)
else:
    cliente_ia = None

# Modelos 'flash' que la clave tenga disponibles. Se consulta UNA sola vez por proceso y
# solo si los modelos configurados fallan (antes se consultaba en CADA ticket, y eso
# añadía una ida y vuelta a Google antes de empezar el OCR).
_modelos_descubiertos_cache = None


def _modelos_configurados():
    """Modelos configurados, en orden y sin repetidos (no llama a Google)."""
    orden, vistos = [], set()
    for m in [GEMINI_MODELO] + GEMINI_MODELOS_RESPALDO:
        if m and m not in vistos:
            vistos.add(m)
            orden.append(m)
    return orden


def _modelos_descubiertos():
    """Modelos que la clave tiene disponibles (una consulta por proceso, cacheada)."""
    global _modelos_descubiertos_cache
    if _modelos_descubiertos_cache is not None:
        return _modelos_descubiertos_cache
    if cliente_ia is None:
        return []
    # Modelos que no sirven para OCR (texto a voz, imagen, audio, embeddings...)
    excluir = ("tts", "image", "audio", "embedding", "embed", "aqa", "live")
    try:
        nombres = []
        for m in cliente_ia.models.list():
            nombre = (getattr(m, "name", "") or "").replace("models/", "").strip()
            if (nombre and "flash" in nombre
                    and not any(x in nombre for x in excluir)):
                nombres.append(nombre)
            if len(nombres) >= 6:
                break
        if nombres:
            _modelos_descubiertos_cache = nombres
        return nombres
    except Exception as e:
        print(f"[AVISO] No se pudo listar modelos disponibles: {e}")
        return []


def _modelos_a_probar():
    """Lista completa (configurados + descubiertos) para el diagnóstico."""
    orden = _modelos_configurados()
    for m in _modelos_descubiertos():
        if m not in orden:
            orden.append(m)
    return orden
# ---------------------------------------------------


def _a_float(v):
    """Convierte a float de forma segura (tolera S/, $, espacios y coma decimal)."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("S/", "").replace("s/", "").replace("$", "").replace(" ", "")
    s = s.replace(",", ".")
    m = re.search(r'-?\d+(\.\d+)?', s)
    return float(m.group(0)) if m else 0.0


def _preprocesar_imagen(foto_bytes):
    """Prepara la foto para el OCR: redimensiona, escala de grises y realza contraste.
    Mejora mucho la lectura de tickets térmicos (desvanecidos, pequeños, torcidos)."""
    try:
        from PIL import Image, ImageEnhance, ImageOps
        import io
        img = Image.open(io.BytesIO(foto_bytes))
        img = ImageOps.exif_transpose(img).convert("RGB")
        max_dim = 1280
        w, h = img.size
        if max(w, h) > max_dim:
            escala = max_dim / max(w, h)
            img = img.resize((int(w * escala), int(h * escala)), Image.LANCZOS)
        img = ImageOps.grayscale(img)
        img = ImageEnhance.Contrast(img).enhance(2.0)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return foto_bytes


app = FastAPI(title="API - Flota Automotriz Black Cube")


def _validar_token(token: str):
    """Comprueba un token de acceso contra la base de datos y devuelve el usuario."""
    token = (token or "").replace("Token ", "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="No autorizado: falta token.")
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM app_tokens_acceso WHERE token = %s", (token,))
        fila = cursor.fetchone()
        if not fila:
            raise HTTPException(status_code=401, detail="Token invalido o expirado.")
        username = fila[0]
        cursor.execute("SELECT activo FROM app_usuarios WHERE username = %s", (username,))
        u = cursor.fetchone()
        if not u:
            raise HTTPException(status_code=401, detail="Usuario no existe.")
        if not u[0]:
            raise HTTPException(status_code=403, detail="Acceso desactivado.")
        return username
    finally:
        liberar_conexion(conn)


def _obtener_usuario_sesion(authorization: str = Header(default="")):
    """Valida el token de acceso (header Authorization: Token <clave>)."""
    return _validar_token(authorization)


class LoginDatos(BaseModel):
    """Cuerpo del login. Al declararlo, /docs muestra los campos y un POST vacío
    devuelve 422 (antes reventaba con 500 al no poder leer el JSON)."""
    username: str = ""
    password: str = ""


# El esquema se asegura UNA sola vez por proceso. Antes, los CREATE/ALTER TABLE se
# ejecutaban en CADA petición (Supabase cobra una ida y vuelta y bloqueos por cada uno),
# y eso se notaba en el login y sobre todo en cada ticket.
_esquema_listo = False


def _asegurar_esquema(cursor, conn):
    """Crea/actualiza las tablas necesarias (una vez por proceso)."""
    global _esquema_listo
    if _esquema_listo:
        return
    sentencias = [
        "CREATE TABLE IF NOT EXISTS app_usuarios ("
        " username VARCHAR(150) PRIMARY KEY,"
        " password_hash TEXT NOT NULL,"
        " nombre VARCHAR(200),"
        " activo BOOLEAN DEFAULT TRUE,"
        " creado_en TIMESTAMPTZ DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS app_tokens_acceso ("
        " token TEXT PRIMARY KEY,"
        " username VARCHAR(150) NOT NULL,"
        " creado_en TIMESTAMPTZ DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS config_general (clave VARCHAR(255) PRIMARY KEY, valor TEXT)",
        "ALTER TABLE pagos_comprobantes ADD COLUMN IF NOT EXISTS cuenta_origen VARCHAR(255) DEFAULT ''",
        "ALTER TABLE facturas_recibidas ADD COLUMN IF NOT EXISTS kilometraje TEXT",
        "ALTER TABLE facturas_recibidas ADD COLUMN IF NOT EXISTS cantidad_combustible TEXT",
        "ALTER TABLE facturas_recibidas ADD COLUMN IF NOT EXISTS ruc TEXT",
        "ALTER TABLE facturas_recibidas ADD COLUMN IF NOT EXISTS hora TEXT",
        "ALTER TABLE facturas_recibidas ADD COLUMN IF NOT EXISTS imagen_base64 TEXT",
        "CREATE TABLE IF NOT EXISTS inspecciones ("
        " id SERIAL PRIMARY KEY,"
        " placa TEXT,"
        " chofer TEXT,"
        " inspector TEXT,"
        " fecha_hora TEXT,"
        " payload TEXT,"
        " creado_en TIMESTAMPTZ DEFAULT now())",
    ]
    fallos = []
    for sql in sentencias:
        try:
            cursor.execute(sql)
        except Exception as e:
            fallos.append(f"{sql[:45]}... -> {e}")
            try:
                conn.rollback()
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    if fallos:
        print("[AVISO] No se pudieron aplicar todas las sentencias de esquema: " + " | ".join(fallos)[:400])
    else:
        _esquema_listo = True
        print("[OK] Esquema verificado (no se volverá a comprobar en cada petición).")


@app.post("/login/")
async def login(datos: LoginDatos):
    """Valida usuario y clave, y devuelve un token de acceso."""
    username = (datos.username or "").strip()
    password = datos.password or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="Faltan credenciales.")

    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")
    try:
        cursor = conn.cursor()
        _asegurar_esquema(cursor, conn)

        cursor.execute(
            "SELECT password_hash, nombre, activo FROM app_usuarios WHERE username = %s",
            (username,),
        )
        fila = cursor.fetchone()
        if not fila:
            raise HTTPException(status_code=401, detail="Credenciales invalidas.")
        password_hash, nombre, activo = fila
        if not verify_password(password, password_hash):
            raise HTTPException(status_code=401, detail="Credenciales invalidas.")
        if not activo:
            raise HTTPException(status_code=403, detail="Acceso desactivado.")

        token = generar_token()
        cursor.execute(
            "INSERT INTO app_tokens_acceso (token, username) VALUES (%s, %s)",
            (token, username),
        )
        conn.commit()
        return {"token": token, "username": username, "nombre": nombre or username}
    finally:
        liberar_conexion(conn)


@app.get("/me/")
async def me(username: str = Depends(_obtener_usuario_sesion)):
    """La app consulta aqui si la sesion sigue vigente y el usuario sigue activo."""
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT nombre FROM app_usuarios WHERE username = %s", (username,))
        fila = cursor.fetchone()
        return {"username": username, "nombre": (fila[0] if fila else username) or username}
    finally:
        liberar_conexion(conn)


@app.get("/vehiculos/")
async def listar_vehiculos(username: str = Depends(_obtener_usuario_sesion)):
    """Lista las placas de flota_vehiculos para el desplegable de la app."""
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT placa, kilometraje FROM flota_vehiculos ORDER BY placa")
        filas = cursor.fetchall()
        return {"vehiculos": [{"placa": fila[0], "kilometraje": fila[1] or ""} for fila in filas]}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        liberar_conexion(conn)


@app.get("/choferes/")
async def listar_choferes(username: str = Depends(_obtener_usuario_sesion)):
    """Lista los choferes activos (DNI + nombre) para elegirlos en la app.

    Se usa en la pantalla de Configuración de "Notificaciones de Flota" para
    marcar qué choferes se quieren ver. Devuelve solo los datos mínimos.
    """
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")
    try:
        cursor = conn.cursor()
        # La tabla puede no existir en bases muy antiguas: se responde vacío.
        cursor.execute("SELECT EXISTS (SELECT FROM information_schema.tables "
                       "WHERE table_name = 'choferes')")
        if not (cursor.fetchone() or [False])[0]:
            return {"choferes": []}
        cursor.execute("SELECT dni, nombres FROM choferes "
                       "WHERE estado = 'Activo' ORDER BY nombres")
        filas = cursor.fetchall()
        return {"choferes": [{"dni": str(fila[0] or "").strip(),
                              "nombre": str(fila[1] or "").strip()}
                             for fila in filas
                             if str(fila[1] or "").strip()]}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        liberar_conexion(conn)


# =====================================================================
# NOTIFICACIONES DE FLOTA (SOLO LECTURA)
# ---------------------------------------------------------------------
# La app "Notificaciones de Flota" únicamente VISUALIZA: nunca registra ni
# modifica nada. Este endpoint reproduce el "Panel de Notificaciones y
# Alertas" de ControlFlota (control_general.py -> panel_dashboard) pero
# LIMITADO al programa de flota: VEHÍCULOS y CHOFERES.
# Quedan fuera, a propósito, ventas, cobranza, compras y nómina.
# =====================================================================

# Umbrales por defecto: los mismos de ControlFlota cuando el equipo todavía
# no guardó su configuración (config_local.json es local, no vive en Supabase).
_ALERTAS_DEF = {
    "dias_alerta_vencimiento": "30",
    "alerta_aceite_km": "5000",
    "alerta_aceite_meses": "6",
    "alerta_mantenimiento_general_km": "10000",
    "alerta_mantenimiento_general_meses": "6",
    "alerta_gas_km": "10000",
    "alerta_neumaticos_km": "50000",
}

# Columnas del vehículo que alimentan las alertas.
_COLS_VEHICULO = [
    "placa", "vencimiento_soat", "vencimiento_seguro", "vencimiento_rt",
    "fec_rev_gas", "fec_venc_bat", "fec_venc_extintor", "fec_aceite",
    "km_prox_correa", "kilometraje", "fecha_ultimo_general",
    "km_ultimo_general", "km_ultimo_aceite", "emision_mant_gas",
    "km_mant_gas", "neu_fecha", "neu_km",
]

# Columnas del chofer que alimentan las alertas.
_COLS_CHOFER = [
    "nombres", "vencimiento_licencia", "seguro_salud_venc", "seguro_vida_venc",
    "fecha_nacimiento", "carnet_sanidad_venc", "vencimiento_licencia2",
    "fecha_fin_contrato",
]


def _consulta(conn, cursor, sql, params=()):
    """Ejecuta una consulta y devuelve (columnas, filas).

    Si la consulta falla (por ejemplo, una columna que aún no existe en una
    base antigua) deja la conexión sana y devuelve (None, None) para que el
    que llama pueda usar su plan B en lugar de tumbar todo el panel.
    """
    try:
        cursor.execute(sql, params)
        columnas = [d[0] for d in (cursor.description or [])]
        return columnas, cursor.fetchall()
    except Exception as e:
        print("[AVISO] /notificaciones/: consulta fallida -> %s" % e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None, None


def _fecha(valor):
    """'dd/mm/aaaa' (o ISO) -> date. None si no se puede leer."""
    txt = str(valor if valor is not None else "").strip()
    if not txt:
        return None
    for formato in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt, formato).date()
        except Exception:
            continue
    return None


def _sumar_meses(fecha_txt, meses):
    """Suma MESES de calendario a 'dd/mm/aaaa'. '' si la fecha no sirve."""
    dt = _fecha(fecha_txt)
    if dt is None:
        return ""
    try:
        total = (dt.month - 1) + int(meses)
        anio = dt.year + total // 12
        mes = total % 12 + 1
        ultimo_dia = calendar.monthrange(anio, mes)[1]
        return dt.replace(year=anio, month=mes, day=min(dt.day, ultimo_dia)).strftime("%d/%m/%Y")
    except Exception:
        return ""


def _numero(valor, decimales_con_comas=False):
    """'12,345' -> 12345.0 (o None si la celda está vacía / no es número).

    Las columnas de kilometraje usan la coma como separador de miles; los
    valores de configuración, en cambio, pueden traer coma decimal.
    """
    txt = str(valor if valor is not None else "").strip()
    if not txt:
        return None
    txt = txt.replace(",", ".") if decimales_con_comas else txt.replace(",", "")
    m = re.search(r"-?\d+(\.\d+)?", txt)
    return float(m.group(0)) if m else None


def _alerta(tipo, icono, titulo, mensaje, categoria, referencia, documento,
            fecha="", dias=None, km=None):
    """Arma una notificación con el mismo formato que usa ControlFlota."""
    return {
        "id": "%s|%s|%s" % (categoria, referencia, documento),
        "tipo": tipo,              # 'peligro' (vencido) | 'alerta' (por vencer)
        "icono": icono,
        "titulo": titulo,
        "mensaje": mensaje,
        "categoria": categoria,    # 'vehiculo' | 'chofer'
        "referencia": referencia,  # placa o nombre del chofer
        "documento": documento,
        "fecha": fecha,
        "dias": dias,              # negativos = vencido; None = por kilometraje
        "km": km,
    }


def _alertas_por_fecha(notificaciones, referencia, categoria, documento, icono,
                       fecha_valor, dias_alerta, hoy, sufijo_titulo="",
                       texto_genero="o"):
    """Compara una fecha de vencimiento con hoy y agrega la notificación."""
    dt = _fecha(fecha_valor)
    if dt is None:
        return
    dias_restantes = (dt - hoy).days
    if dias_restantes < 0:
        notificaciones.append(_alerta(
            "peligro", "🚨",
            "%s Vencid%s (%s)" % (documento, texto_genero, referencia),
            "Venci%s hace %d días (%s)." % (
                texto_genero, abs(dias_restantes), dt.strftime("%d/%m/%Y")),
            categoria, referencia, documento, dt.strftime("%d/%m/%Y"),
            dias_restantes))
    elif dias_restantes <= dias_alerta:
        notificaciones.append(_alerta(
            "alerta", icono,
            "%s por Vencer (%s)%s" % (documento, referencia, sufijo_titulo),
            "Vence%s en %d días (%s)." % (
                texto_genero, dias_restantes, dt.strftime("%d/%m/%Y")),
            categoria, referencia, documento, dt.strftime("%d/%m/%Y"),
            dias_restantes))


def _alertas_por_km(notificaciones, referencia, documento, icono, km_base,
                    km_actual, limite, margen_aviso):
    """Alerta por kilometraje: km_base + límite contra el kilometraje actual."""
    base = _numero(km_base)
    actual = _numero(km_actual)
    if base is None or actual is None or limite <= 0:
        return
    restantes = (base + limite) - actual
    if restantes < 0:
        notificaciones.append(_alerta(
            "peligro", "🚨",
            "%s Vencido (%s)" % (documento, referencia),
            "Excedido por %s KM (cada %s km)." % (
                "{:,.0f}".format(abs(restantes)), "{:,.0f}".format(limite)),
            "vehiculo", referencia, documento, "", None, round(restantes)))
    elif restantes <= margen_aviso:
        notificaciones.append(_alerta(
            "alerta", icono,
            "%s Próximo (%s)" % (documento, referencia),
            "Faltan %s KM." % "{:,.0f}".format(restantes),
            "vehiculo", referencia, documento, "", None, round(restantes)))


def _umbrales(conn, cursor):
    """Umbrales de alerta: config_general (si ya existen) o los de ControlFlota."""
    cfg = dict(_ALERTAS_DEF)
    columnas, filas = _consulta(conn, cursor, "SELECT clave, valor FROM config_general")
    for clave, valor in (filas or []):
        if clave in cfg and valor is not None and str(valor).strip():
            cfg[clave] = str(valor).strip()
    return cfg


def _entero(valor, defecto):
    try:
        return int(float(str(valor).replace(",", ".")))
    except Exception:
        return defecto


@app.get("/notificaciones/")
async def listar_notificaciones(dias: int = 0, username: str = Depends(_obtener_usuario_sesion)):
    """Notificaciones de la flota: SOLO vehículos y choferes. Solo lectura.

    Con ?dias=N se puede forzar la anticipación del aviso (por defecto se usa
    la que tenga configurada ControlFlota, o 30 días).
    """
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")
    try:
        cursor = conn.cursor()
        cfg = _umbrales(conn, cursor)

        dias_alerta = dias if dias and dias > 0 else _entero(
            cfg.get("dias_alerta_vencimiento"), 30)
        if dias_alerta <= 0:
            dias_alerta = 30

        hoy = datetime.now().date()
        notificaciones = []
        revisados = {"vehiculos": 0, "choferes": 0}

        # ---------------- VEHÍCULOS (solo los operativos) ----------------
        columnas, filas = _consulta(
            conn, cursor,
            "SELECT %s FROM flota_vehiculos WHERE estado = 'Operativo'"
            % ", ".join(_COLS_VEHICULO))
        if columnas is None:
            # Base antigua: al menos los documentos que nunca faltan + kilometraje.
            columnas, filas = _consulta(
                conn, cursor,
                "SELECT placa, vencimiento_soat, vencimiento_seguro, vencimiento_rt"
                " FROM flota_vehiculos WHERE estado = 'Operativo'")
        revisados["vehiculos"] = len(filas or [])

        for fila in (filas or []):
            v = dict(zip(columnas, fila))
            placa = str(v.get("placa") or "").strip()
            if not placa:
                continue

            # Documentos con fecha de vencimiento directa.
            documentos = {
                "SOAT": v.get("vencimiento_soat"),
                "Seguro": v.get("vencimiento_seguro"),
                "Revisión Técnica": v.get("vencimiento_rt"),
                "Rev. Sist. Gas": _sumar_meses(v.get("fec_rev_gas"), 12),
                "Garantía Batería": v.get("fec_venc_bat"),
                "Extintor": v.get("fec_venc_extintor"),
            }
            for documento, fecha_valor in documentos.items():
                _alertas_por_fecha(notificaciones, placa, "vehiculo", documento,
                                   "⚠️", fecha_valor, dias_alerta, hoy)

            # 🛢️ Cambio de aceite por TIEMPO (fecha del último cambio + N meses).
            meses_aceite = _entero(cfg.get("alerta_aceite_meses"), 6)
            if meses_aceite > 0:
                proximo = _sumar_meses(v.get("fec_aceite"), meses_aceite)
                if proximo:
                    _alertas_por_fecha(
                        notificaciones, placa, "vehiculo", "Cambio de Aceite",
                        "🛢️", proximo, dias_alerta, hoy,
                        sufijo_titulo="", texto_genero="o")

            # 🛠️ Mantenimiento preventivo por TIEMPO desde el último servicio.
            meses_general = _entero(cfg.get("alerta_mantenimiento_general_meses"), 6)
            ultimo_general = _fecha(v.get("fecha_ultimo_general"))
            if ultimo_general is not None and meses_general > 0:
                proximo = ultimo_general + timedelta(days=30 * meses_general)
                _alertas_por_fecha(
                    notificaciones, placa, "vehiculo", "Mantenimiento Preventivo",
                    "🛠️", proximo.strftime("%d/%m/%Y"), dias_alerta, hoy,
                    sufijo_titulo=" · último %s" % ultimo_general.strftime("%d/%m/%Y"))

            # Alertas por KILOMETRAJE (se comparan con el kilometraje actual).
            km_actual = v.get("kilometraje")
            _alertas_por_km(notificaciones, placa, "Mantenimiento General", "🛠️",
                            v.get("km_ultimo_general"), km_actual,
                            _numero(cfg.get("alerta_mantenimiento_general_km"),
                                    True) or 0, 1000)
            _alertas_por_km(notificaciones, placa, "Cambio de Aceite", "🛢️",
                            v.get("km_ultimo_aceite"), km_actual,
                            _numero(cfg.get("alerta_aceite_km"), True) or 0, 500)
            _alertas_por_km(notificaciones, placa, "Mantenimiento de Gas", "💨",
                            v.get("km_mant_gas"), km_actual,
                            _numero(cfg.get("alerta_gas_km"), True) or 0, 1000)
            _alertas_por_km(notificaciones, placa, "Cambio de Neumáticos", "🛞",
                            v.get("neu_km"), km_actual,
                            _numero(cfg.get("alerta_neumaticos_km"), True) or 0, 2000)

            # ⚙️ Correa de distribución: el punto de cambio viene dado.
            km_prox = _numero(v.get("km_prox_correa"))
            km_hoy = _numero(km_actual)
            if km_prox is not None and km_hoy is not None:
                restantes = km_prox - km_hoy
                if restantes < 0:
                    notificaciones.append(_alerta(
                        "peligro", "🚨", "Correa de Distribución Vencida (%s)" % placa,
                        "Excedido por %s KM." % "{:,.0f}".format(abs(restantes)),
                        "vehiculo", placa, "Correa de Distribución", "", None,
                        round(restantes)))
                elif restantes <= 1000:
                    notificaciones.append(_alerta(
                        "alerta", "⚙️", "Correa de Distribución Próxima (%s)" % placa,
                        "Faltan %s KM." % "{:,.0f}".format(restantes),
                        "vehiculo", placa, "Correa de Distribución", "", None,
                        round(restantes)))

        # ---------------- CHOFERES (solo los activos) ----------------
        existe_cols, existe_filas = _consulta(
            conn, cursor,
            "SELECT EXISTS (SELECT FROM information_schema.tables"
            " WHERE table_name = 'choferes')")
        hay_tabla = bool(existe_filas and existe_filas[0][0])

        if hay_tabla:
            columnas, filas = _consulta(
                conn, cursor,
                "SELECT %s FROM choferes WHERE estado = 'Activo'" % ", ".join(_COLS_CHOFER))
            if columnas is None:
                columnas, filas = _consulta(
                    conn, cursor,
                    "SELECT nombres, vencimiento_licencia FROM choferes WHERE estado = 'Activo'")
            revisados["choferes"] = len(filas or [])

            for fila in (filas or []):
                c = dict(zip(columnas, fila))
                nombre = str(c.get("nombres") or "").strip()
                if not nombre:
                    continue

                documentos = {
                    "Licencia": c.get("vencimiento_licencia"),
                    "Segunda Licencia": c.get("vencimiento_licencia2"),
                    "Seguro de Salud": c.get("seguro_salud_venc"),
                    "Seguro Vida Ley": c.get("seguro_vida_venc"),
                    "Carné de Sanidad": c.get("carnet_sanidad_venc"),
                    "Fin de Contrato": c.get("fecha_fin_contrato"),
                }
                for documento, fecha_valor in documentos.items():
                    _alertas_por_fecha(notificaciones, nombre, "chofer", documento,
                                       "⚠️", fecha_valor, dias_alerta, hoy,
                                       texto_genero="a")

                # 🎂 Cumpleaños: se avisa dentro de la ventana de anticipación.
                nacimiento = _fecha(c.get("fecha_nacimiento"))
                if nacimiento is not None:
                    try:
                        proximo = nacimiento.replace(year=hoy.year)
                        if proximo < hoy:
                            proximo = proximo.replace(year=hoy.year + 1)
                        dias_restantes = (proximo - hoy).days
                        if dias_restantes == 0:
                            notificaciones.append(_alerta(
                                "alerta", "🎉", "¡Cumpleaños de %s!" % nombre,
                                "Hoy es su cumpleaños.", "chofer", nombre,
                                "Cumpleaños", proximo.strftime("%d/%m/%Y"), 0))
                        elif dias_restantes <= dias_alerta:
                            notificaciones.append(_alerta(
                                "alerta", "🎂", "Cumpleaños de %s" % nombre,
                                "Es en %d días (%s)." % (
                                    dias_restantes, proximo.strftime("%d/%m")),
                                "chofer", nombre, "Cumpleaños",
                                proximo.strftime("%d/%m/%Y"), dias_restantes))
                    except Exception:
                        pass

        # Lo más urgente primero: vencidos antes que por vencer, y dentro de
        # cada grupo, el que está más cerca (o más pasado) de la fecha.
        notificaciones.sort(key=lambda n: (
            0 if n["tipo"] == "peligro" else 1,
            n["dias"] if n["dias"] is not None else 10 ** 6,
            n["referencia"].lower(),
        ))

        vencidas = sum(1 for n in notificaciones if n["tipo"] == "peligro")
        return {
            "generado": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "dias_alerta": dias_alerta,
            "revisados": revisados,
            "resumen": {
                "total": len(notificaciones),
                "vencidas": vencidas,
                "por_vencer": len(notificaciones) - vencidas,
                "vehiculos": sum(1 for n in notificaciones if n["categoria"] == "vehiculo"),
                "choferes": sum(1 for n in notificaciones if n["categoria"] == "chofer"),
            },
            "notificaciones": notificaciones,
        }
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        liberar_conexion(conn)


@app.post("/subir-ticket/")
async def subir_ticket_grifo(
    placa: str = Form(...),
    kilometraje: str = Form(...),
    foto: UploadFile = File(...),
    username: str = Depends(_obtener_usuario_sesion)
):
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")

    try:
        # 1. LEER LA FOTO Y CONVERTIRLA A TEXTO (Base64) PARA LA NUBE
        foto_bytes = await foto.read()
        foto_b64 = base64.b64encode(foto_bytes).decode('utf-8')

        # 2. LECTURA CON INTELIGENCIA ARTIFICIAL DESDE MEMORIA
        numero_doc = "POR-ASIGNAR"
        subtotal_monto = 0.0
        igv_monto = 0.0
        total_monto = 0.0
        tipo_combustible = "Desconocido"
        cantidad_combustible = "0"
        proveedor_ia = "GRIFO (Desde App)"
        ruc_ia = ""
        direccion_ia = ""
        fecha_ticket = ""
        hora_ticket = ""
        ocr_ok = False
        error_ia = ""
        
        try:
            if cliente_ia is None:
                raise ValueError("GEMINI_API_KEY no configurada en el servidor")
            print(f"🤖 IA Analizando el ticket de la placa {placa}...")
            
            # Preprocesamos la foto (resize + contraste) y se la pasamos a la IA.
            foto_ocr = _preprocesar_imagen(foto_bytes)
            archivo_ia = types.Part.from_bytes(data=foto_ocr, mime_type='image/jpeg')
            
            prompt = """
            Eres un lector OCR de boletas/facturas de combustible (grifo). La foto puede ser un ticket térmico pequeño, borroso o torcido. Devuelve SOLO un JSON válido, sin texto adicional, con estas claves exactas:
            - "numero_documento": serie y correlativo (ej. "F531-00129762"). Si no se ve, "".
            - "fecha": fecha en DD/MM/YYYY. Si no se ve, "".
            - "hora": hora en HH:MM. Si no se ve, "".
            - "proveedor": razón social o nombre del establecimiento. Si no se ve, "".
            - "ruc": exactamente 11 dígitos del RUC. Si no se ve o no son 11 dígitos, "".
            - "direccion": dirección del establecimiento. Si no se ve, "".
            - "tipo_combustible": ej. "Gasohol Premium", "Diesel", "GLP". Si no se ve, "".
            - "cantidad": cantidad con unidad (ej. "4.002 GAL"). Si no se ve, "".
            - "subtotal": importe sin IGV, número con punto decimal (ej. 84.75). Si no se ve, 0.
            - "igv": importe del IGV (ej. 15.25). Si no se ve, 0.
            - "total": importe total / gran total (ej. 100.00). Si no se ve, 0.
            Reglas: NO inventes datos. Montos como números con punto decimal, sin símbolo de moneda ni comas. Si un campo no se ve, devuélvelo vacío o 0.
            """
            
            # Prueba los modelos en orden hasta que uno responda. El caso normal se
            # resuelve con el PRIMER modelo (sin consultas extra a Google).
            texto_ia = ""
            errores_ia = []

            def _intentar_ocr(modelo):
                """Devuelve (texto, error) para un modelo concreto."""
                try:
                    print(f"🤖 IA leyendo el ticket con {modelo}...")
                    respuesta = cliente_ia.models.generate_content(
                        model=modelo,
                        contents=[prompt, archivo_ia],
                    )
                    texto = (respuesta.text or "").strip()
                    return texto, ("" if texto else "respuesta vacía")
                except Exception as e:
                    return "", str(e)

            for modelo in _modelos_configurados():
                texto_ia, err = _intentar_ocr(modelo)
                if texto_ia:
                    print(f"✅ OCR correcto con {modelo}")
                    break
                errores_ia.append(f"{modelo}: {err}")
                print(f"⚠️ Error IA con {modelo}: {err}")

            # Solo si TODOS los configurados fallaron, probamos los que la clave tenga
            # disponibles (una única consulta, cacheada).
            if not texto_ia:
                for modelo in _modelos_descubiertos():
                    if modelo in _modelos_configurados():
                        continue
                    texto_ia, err = _intentar_ocr(modelo)
                    if texto_ia:
                        print(f"✅ OCR correcto con {modelo}")
                        break
                    errores_ia.append(f"{modelo}: {err}")

            if not texto_ia:
                error_ia = " | ".join(errores_ia)[:400]
                raise ValueError(f"Ningún modelo respondió. {error_ia}")
            
            match = re.search(r'\{.*\}', texto_ia, re.DOTALL)
            
            if match:
                try:
                    datos_ia = json.loads(match.group(0))
                except Exception as e:
                    print(f"⚠️ JSON inválido devuelto por IA: {match.group(0)[:500]}")
                    raise ValueError(f"La IA devolvió un JSON inválido: {e}")
                numero_doc = str(datos_ia.get("numero_documento") or "POR-ASIGNAR")
                fecha_ticket = str(datos_ia.get("fecha") or "").strip()
                hora_ticket = str(datos_ia.get("hora") or "").strip()
                subtotal_monto = _a_float(datos_ia.get("subtotal"))
                igv_monto = _a_float(datos_ia.get("igv"))
                total_monto = _a_float(datos_ia.get("total"))
                tipo_combustible = str(datos_ia.get("tipo_combustible") or "NO INDICA")
                cantidad_combustible = str(datos_ia.get("cantidad") or "0")
                proveedor_ia = str(datos_ia.get("proveedor") or "GRIFO (Desde App)").upper()
                ruc_ia = str(datos_ia.get("ruc") or "")
                direccion_ia = str(datos_ia.get("direccion") or "Dirección no indicada")
                
                # Respaldo matemático
                if subtotal_monto == 0.0 and total_monto > 0:
                    subtotal_monto = round(total_monto / 1.18, 2)
                    igv_monto = round(total_monto - subtotal_monto, 2)
                ocr_ok = True
            else:
                print(f"⚠️ IA no devolvió JSON. Texto recibido: {texto_ia[:500]}")
                raise ValueError("La IA no devolvió JSON válido")
            
        except Exception as e:
            error_ia = str(e)
            print(f"⚠️ Error IA: {error_ia}")

        # 3. GUARDAR EN LA BASE DE DATOS (SUPABASE)
        cursor = conn.cursor()

        # Cuenta bancaria asignada para pagos del App Grifo (Configuración General).
        _asegurar_esquema(cursor, conn)
        cuenta_grifo = ""
        try:
            cursor.execute("SELECT valor FROM config_general WHERE clave = 'cuenta_grifo_pagos'")
            fila_cfg = cursor.fetchone()
            if fila_cfg:
                cuenta_grifo = (fila_cfg[0] or "").strip()
        except Exception:
            conn.rollback()
            cuenta_grifo = ""
        conn.commit()
        
        # Proveedores automáticos
        if ruc_ia and ruc_ia.isdigit() and len(ruc_ia) == 11:
            try:
                cursor.execute("SELECT ruc FROM proveedores WHERE ruc = %s", (ruc_ia,))
                if not cursor.fetchone():
                    cursor.execute("""
                        INSERT INTO proveedores (ruc, nombre, direccion_fiscal, categoria) 
                        VALUES (%s, %s, %s, %s)
                    """, (ruc_ia, proveedor_ia, direccion_ia, "Combustible / Grifo"))
                    conn.commit()
            except Exception: conn.rollback()

        # Candado Anti-duplicados
        if numero_doc and numero_doc not in ["POR-ASIGNAR", "ERROR-LECTURA"]:
            cursor.execute("SELECT COUNT(*) FROM facturas_recibidas WHERE numero_documento = %s AND proveedor = %s", (numero_doc, proveedor_ia))
            if cursor.fetchone()[0] > 0:
                liberar_conexion(conn)
                return {"status": "warning", "mensaje": f"El ticket {numero_doc} ya está registrado."}

        cursor.execute("UPDATE flota_vehiculos SET kilometraje = %s WHERE placa = %s", (kilometraje, placa))

        # Las columnas dinámicas ya quedaron aseguradas una sola vez en _asegurar_esquema().

        # El detalle va en la columna "descripcion": tipo de combustible + hora
        if hora_ticket:
            descripcion_final = f"{tipo_combustible} | Hora: {hora_ticket}"
        else:
            descripcion_final = tipo_combustible
        fecha_hoy = fecha_ticket or datetime.now().strftime("%d/%m/%Y")
        tipo_doc_final = "Factura (18% IGV)" if numero_doc.startswith("F") else "Boleta / Ticket"
        
        # INSERTAMOS INDICANDO QUE EL ARCHIVO ESTÁ "PENDIENTE_DESCARGA" Y METEMOS LA FOTO EN LA NUBE
        cursor.execute("""
            INSERT INTO facturas_recibidas (
                tipo_documento, numero_documento, fecha, hora, proveedor, 
                descripcion, evento_asociado, subtotal, impuesto, 
                total, archivo_ruta, categoria, kilometraje, cantidad_combustible, ruc, imagen_base64
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            tipo_doc_final, numero_doc, fecha_hoy, hora_ticket, proveedor_ia, 
            descripcion_final, placa, subtotal_monto, igv_monto, total_monto, "PENDIENTE_DESCARGA", "Combustible y Peajes", kilometraje, cantidad_combustible, ruc_ia, foto_b64
        ))
        id_factura = cursor.fetchone()[0]

        cursor.execute("""
            INSERT INTO pagos_comprobantes (
                id_factura, monto_pagado, archivo_ruta, proveedor_nombre, 
                fecha_pago, categoria_suministro, codigo_cotizacion, cuenta_origen
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            id_factura, total_monto, "PENDIENTE_DESCARGA", 
            proveedor_ia, fecha_hoy, "Combustible y Peajes", numero_doc, cuenta_grifo
        ))

        conn.commit()
        if ocr_ok:
            return {"status": "success", "mensaje": "Ticket procesado y subido a la nube."}
        return {"status": "warning", "mensaje": "Ticket guardado, pero la IA no pudo leerlo (datos incompletos).", "detalle_ia": error_ia[:300]}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        liberar_conexion(conn)


@app.post("/registrar-inspeccion/")
async def registrar_inspeccion(request: Request, username: str = Depends(_obtener_usuario_sesion)):
    """Recibe una inspección vehicular (JSON con fotos/firmas en base64) y la guarda en Supabase."""
    datos = await request.json()
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")

    try:
        cursor = conn.cursor()
        # La tabla se asegura una sola vez por proceso (ver _asegurar_esquema).
        _asegurar_esquema(cursor, conn)

        cursor.execute("""
            INSERT INTO inspecciones (placa, chofer, inspector, fecha_hora, payload)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            datos.get("placa"),
            datos.get("chofer"),
            datos.get("inspector"),
            datos.get("fecha_hora"),
            json.dumps(datos, ensure_ascii=False),
        ))
        conn.commit()
        return {"status": "success", "mensaje": "Inspección registrada correctamente."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        liberar_conexion(conn)


@app.get("/diagnostico-ia/")
async def diagnostico_ia(completo: int = 0):
    """Diagnóstico del OCR de la IA (sin datos sensibles: no muestra la clave).

    Ábrelo directamente en el navegador, sin token:

        https://api-flota-blackcube.onrender.com/diagnostico-ia/

    Por defecto prueba SOLO el modelo preferido, así responde en pocos segundos.
    Con ?completo=1 prueba toda la lista (tarda más, porque hace varias llamadas a Google).
    """
    inicio = time.time()
    info = {
        "clave_configurada": bool(GEMINI_API_KEY),
        "longitud_clave": len(GEMINI_API_KEY),
        "modelo_preferido": GEMINI_MODELO,
    }

    if not GEMINI_API_KEY:
        info["resultado"] = (
            "FALTA la variable GEMINI_API_KEY en el servidor. En Render: tu servicio > "
            "Environment > Add Environment Variable > GEMINI_API_KEY. Un GitHub Secret "
            "NO llega a Render; guarda y espera el redeploy."
        )
        return info

    def _probar(modelo):
        """Prueba real de generación. Devuelve un texto corto con el resultado."""
        t0 = time.time()
        try:
            r = cliente_ia.models.generate_content(model=modelo, contents=["Responde solo: OK"])
            return f"OK ({time.time() - t0:.1f}s) -> " + (r.text or "").strip()[:40]
        except Exception as e:
            return f"ERROR ({time.time() - t0:.1f}s) -> " + str(e)[:300]

    configurados = _modelos_configurados()
    pruebas = {configurados[0]: _probar(configurados[0])}

    # Si el preferido falla, se prueba el resto de la lista configurada.
    if not any(v.startswith("OK") for v in pruebas.values()):
        for modelo in configurados[1:]:
            pruebas[modelo] = _probar(modelo)

    # ?completo=1 añade los modelos que la clave tenga disponibles.
    if completo:
        for modelo in _modelos_descubiertos():
            if modelo not in pruebas:
                pruebas[modelo] = _probar(modelo)

    info["pruebas"] = pruebas
    info["segundos_totales"] = round(time.time() - inicio, 1)

    ok = [m for m, v in pruebas.items() if v.startswith("OK")]
    if ok:
        info["resultado"] = f"IA operativa. Usa preferentemente: {ok[0]}"
    else:
        info["resultado"] = (
            "Ningún modelo respondió. Revisa 'pruebas': si todas dicen 401/403 la clave "
            "pertenece a un proyecto de Google Cloud deshabilitado o con la cuenta de "
            "servicio borrada -> crea una clave nueva en https://aistudio.google.com/apikey "
            "y actualízala en Render."
        )
    return info


if __name__ == "__main__":
    import uvicorn
    # En la nube el puerto es dinámico, esto lo configura automáticamente
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)