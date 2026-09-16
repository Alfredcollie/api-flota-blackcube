# -*- coding: utf-8 -*-
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends, Header
import base64
import os
import json
import re
from datetime import datetime
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
        "gemini-flash-latest,gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.0-flash-lite",
    ).split(",") if m.strip()
]

if not GEMINI_API_KEY:
    print("[AVISO] GEMINI_API_KEY no configurada: el OCR con IA quedara desactivado hasta definir la variable de entorno.")
cliente_ia = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def _modelos_a_probar():
    """Modelos a intentar, en orden: el preferido, los de respaldo y, como última red,
    los modelos 'flash' que la propia clave tenga disponibles (auto-ajuste si Google
    renombra o retira modelos)."""
    orden, vistos = [], set()
    for m in [GEMINI_MODELO] + GEMINI_MODELOS_RESPALDO:
        if m and m not in vistos:
            vistos.add(m)
            orden.append(m)
    try:
        agregados = 0
        for m in cliente_ia.models.list():
            nombre = (getattr(m, "name", "") or "").replace("models/", "").strip()
            if nombre and "flash" in nombre and nombre not in vistos:
                vistos.add(nombre)
                orden.append(nombre)
                agregados += 1
                if agregados >= 3:
                    break
    except Exception as e:
        print(f"⚠️ No se pudo listar modelos disponibles: {e}")
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


@app.post("/login/")
async def login(request: Request):
    """Valida usuario y clave, y devuelve un token de acceso."""
    datos = await request.json()
    username = (datos.get("username") or "").strip()
    password = datos.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="Faltan credenciales.")

    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error conectando a la base de datos.")
    try:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS app_usuarios ("
            " username VARCHAR(150) PRIMARY KEY,"
            " password_hash TEXT NOT NULL,"
            " nombre VARCHAR(200),"
            " activo BOOLEAN DEFAULT TRUE,"
            " creado_en TIMESTAMPTZ DEFAULT now())"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS app_tokens_acceso ("
            " token TEXT PRIMARY KEY,"
            " username VARCHAR(150) NOT NULL,"
            " creado_en TIMESTAMPTZ DEFAULT now())"
        )
        conn.commit()

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
            
            # Prueba los modelos en orden hasta que uno responda. Si Google retira o
            # renombra un modelo, el respaldo automático lo resuelve sin tocar el código.
            texto_ia = ""
            errores_ia = []
            for modelo in _modelos_a_probar():
                try:
                    print(f"🤖 IA leyendo el ticket con {modelo}...")
                    respuesta = cliente_ia.models.generate_content(
                        model=modelo,
                        contents=[prompt, archivo_ia],
                    )
                    texto_ia = (respuesta.text or "").strip()
                    if texto_ia:
                        print(f"✅ OCR correcto con {modelo}")
                        break
                    errores_ia.append(f"{modelo}: respuesta vacía")
                except Exception as e:
                    errores_ia.append(f"{modelo}: {e}")
                    print(f"⚠️ Error IA con {modelo}: {e}")
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
        cuenta_grifo = ""
        try:
            cursor.execute("CREATE TABLE IF NOT EXISTS config_general (clave VARCHAR(255) PRIMARY KEY, valor TEXT)")
            cursor.execute("ALTER TABLE pagos_comprobantes ADD COLUMN IF NOT EXISTS cuenta_origen VARCHAR(255) DEFAULT ''")
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

        # Crear columnas dinámicas (incluido el almacén temporal de la foto "imagen_base64").
        # "ADD COLUMN IF NOT EXISTS" evita errores y rollbacks en cada envío (más rápido).
        for col in ["kilometraje", "cantidad_combustible", "ruc", "hora", "imagen_base64"]:
            try:
                cursor.execute(f"ALTER TABLE facturas_recibidas ADD COLUMN IF NOT EXISTS {col} TEXT;")
            except Exception:
                conn.rollback()
        conn.commit()

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
        # Asegura la existencia de la tabla (también puedes ejecutar inspeccion_vehicular.sql).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inspecciones (
                id SERIAL PRIMARY KEY,
                placa TEXT,
                chofer TEXT,
                inspector TEXT,
                fecha_hora TEXT,
                payload TEXT,
                creado_en TIMESTAMPTZ DEFAULT now()
            )
        """)
        conn.commit()

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
async def diagnostico_ia(token: str = "", authorization: str = Header(default="")):
    """Diagnóstico del OCR. Ábrelo en el navegador para ver la causa exacta:

        https://TU-API/diagnostico-ia/?token=TU_TOKEN

    Devuelve si la clave está configurada en el servidor, qué modelos acepta esa clave
    y el error literal que responde Google al intentar generar texto."""
    usuario = _validar_token(authorization or token)

    info = {
        "usuario": usuario,
        "clave_configurada": bool(GEMINI_API_KEY),
        "longitud_clave": len(GEMINI_API_KEY),
        "modelo_preferido": GEMINI_MODELO,
        "modelos_a_probar": _modelos_a_probar(),
    }

    if not GEMINI_API_KEY:
        info["resultado"] = (
            "FALTA la variable GEMINI_API_KEY en el servidor. En Render: tu servicio > "
            "Environment > Add Environment Variable > GEMINI_API_KEY. Un GitHub Secret "
            "NO llega a Render; guarda y espera el redeploy."
        )
        return info

    pruebas = {}
    for modelo in info["modelos_a_probar"]:
        try:
            r = cliente_ia.models.generate_content(model=modelo, contents=["Responde solo: OK"])
            pruebas[modelo] = "OK -> " + (r.text or "").strip()[:40]
        except Exception as e:
            pruebas[modelo] = "ERROR -> " + str(e)[:300]
    info["pruebas"] = pruebas

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