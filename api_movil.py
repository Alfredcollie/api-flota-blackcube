# -*- coding: utf-8 -*-
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
import base64
import os
import json
import re
from datetime import datetime
import time
from google import genai
from google.genai import types
from conexion import conectar_db, liberar_conexion

# --- CONFIGURACIÓN DE LA IA (GOOGLE GEMINI) PARA OCR DE TICKETS ---
# Clave GRATIS de Google AI Studio -> variable de entorno GEMINI_API_KEY en Render.
# https://aistudio.google.com/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip() or "AQ.Ab8RN6LTyHmVNUALwk6Wk7b2EMSzbZrVXVjg-cKUH7cSwnJ0Iw"
cliente_ia = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Modelos de visión en orden de preferencia. Los alias "-latest" son estables
# aunque Google cambie el número de versión; los específicos sirven de respaldo.
MODELOS_OCR = (
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash",
)
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


app = FastAPI(title="API - Flota Automotriz Black Cube")

@app.post("/subir-ticket/")
async def subir_ticket_grifo(
    placa: str = Form(...),
    kilometraje: str = Form(...),
    foto: UploadFile = File(...)
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
            
            # Pasamos la imagen directamente sin guardarla en disco
            archivo_ia = types.Part.from_bytes(data=foto_bytes, mime_type=foto.content_type or 'image/jpeg')
            
            prompt = """
            Eres un auditor experto y muy detallista. Tu tarea es leer EXACTAMENTE lo que está impreso en esta boleta/factura electrónica de combustible (grifo). NO inventes datos. Extrae en formato JSON estricto:
            - "numero_documento": (serie y correlativo, ej. F531-00129762)
            - "fecha": (fecha del documento en DD/MM/YYYY, ej. 30/07/2026)
            - "hora": (hora del documento en HH:MM, ej. 17:12)
            - "proveedor": (razón social o nombre del establecimiento, ej. REPSOL COMERCIAL S.A.C.)
            - "ruc": (exactamente los 11 dígitos del RUC, ej. 2050384021)
            - "direccion": (dirección del establecimiento)
            - "tipo_combustible": (ej. Gasohol Premium, Diesel, GLP)
            - "cantidad": (cantidad con unidad, ej. 4.002 GAL)
            - "subtotal": (importe sin IGV, ej. 84.75)
            - "igv": (importe del IGV, ej. 15.25)
            - "total": (importe total / gran total, ej. 100.00)
            Reglas: montos como números con punto decimal, sin símbolo de moneda ni comas. RUC solo 11 dígitos.
            """
            
            config_ia = types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            )
            
            # Intentar varios modelos (con reintento ante saturación 503/429)
            texto_ia = ""
            for modelo in MODELOS_OCR:
                for intento in range(2):
                    try:
                        print(f"🤖 Probando modelo {modelo} (intento {intento+1})...")
                        respuesta = cliente_ia.models.generate_content(
                            model=modelo,
                            contents=[prompt, archivo_ia],
                            config=config_ia,
                        )
                        texto_ia = (respuesta.text or "").strip()
                        if texto_ia:
                            break
                    except Exception as e:
                        print(f"⚠️ Modelo {modelo} intento {intento+1} falló: {e}")
                        error_ia = str(e)
                        if intento < 1:
                            time.sleep(2)  # breve espera por saturación del servicio
                if texto_ia:
                    break
            if not texto_ia:
                raise ValueError(f"La IA no devolvió texto. Último error: {error_ia[:300]}")
            
            match = re.search(r'\{.*\}', texto_ia, re.DOTALL)
            
            if match:
                datos_ia = json.loads(match.group(0))
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
                raise ValueError("La IA no devolvió JSON válido")
            
        except Exception as e:
            error_ia = str(e)
            print(f"⚠️ Error IA: {error_ia}")

        # 3. GUARDAR EN LA BASE DE DATOS (SUPABASE)
        cursor = conn.cursor()
        
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

        # Crear columnas dinámicas (Incluyendo el almacén temporal de la foto "imagen_base64")
        for col in ["kilometraje", "cantidad_combustible", "ruc", "hora"]:
            try:
                cursor.execute(f"ALTER TABLE facturas_recibidas ADD COLUMN {col} VARCHAR(50);")
                conn.commit()
            except Exception: conn.rollback() 
            
        try:
            cursor.execute("ALTER TABLE facturas_recibidas ADD COLUMN imagen_base64 TEXT;")
            conn.commit()
        except Exception: conn.rollback() 

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
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            tipo_doc_final, numero_doc, fecha_hoy, hora_ticket, proveedor_ia, 
            descripcion_final, placa, subtotal_monto, igv_monto, total_monto, "PENDIENTE_DESCARGA", "Combustible y Peajes", kilometraje, cantidad_combustible, ruc_ia, foto_b64
        ))

        cursor.execute("SELECT id FROM facturas_recibidas ORDER BY id DESC LIMIT 1")
        id_factura = cursor.fetchone()[0]

        cursor.execute("""
            INSERT INTO pagos_comprobantes (
                id_factura, monto_pagado, archivo_ruta, proveedor_nombre, 
                fecha_pago, categoria_suministro, codigo_cotizacion
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            id_factura, total_monto, "PENDIENTE_DESCARGA", 
            proveedor_ia, fecha_hoy, "Combustible y Peajes", numero_doc
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
async def registrar_inspeccion(request: Request):
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


if __name__ == "__main__":
    import uvicorn
    # En la nube el puerto es dinámico, esto lo configura automáticamente
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)