# -*- coding: utf-8 -*-
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import base64
import os
import json
import re
from datetime import datetime
from conexion import conectar_db
from google import genai
from google.genai import types

# --- CONFIGURACIÓN DE LA INTELIGENCIA ARTIFICIAL ---
# ⚠️ REEMPLAZA con tu API Key real de Google AI Studio (Empieza con AIza...)
cliente_ia = genai.Client(api_key="AQ.Ab8RN6KTR-IvzpzPC0p6tMWChrzVQjyE1Y0lGQn7zXyOOSGsLg")
# ---------------------------------------------------

app = FastAPI(title="API - Flota Automotriz Black Cube")

# =================================================================
# 1. MÓDULO DE LECTURA DE TICKETS
# =================================================================
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
        foto_bytes = await foto.read()
        foto_b64 = base64.b64encode(foto_bytes).decode('utf-8')

        numero_doc = "POR-ASIGNAR"
        subtotal_monto = 0.0
        igv_monto = 0.0
        total_monto = 0.0
        tipo_combustible = "Desconocido"
        cantidad_combustible = "0"
        proveedor_ia = "GRIFO (Desde App)"
        ruc_ia = ""
        direccion_ia = ""
        
        try:
            print(f"🤖 IA Analizando el ticket de la placa {placa}...")
            
            # Formato oficial para enviar bytes de imagen en el nuevo SDK
            parte_imagen = types.Part.from_bytes(
                data=foto_bytes,
                mime_type=foto.content_type
            )
            
            prompt = """
            Eres un auditor experto y muy detallista. Tu tarea es leer EXACTAMENTE lo que está impreso en la imagen. Extrae la información en formato JSON estricto:
            - "numero_documento": (El número de serie y correlativo exacto impreso)
            - "subtotal": (solo el número decimal)
            - "igv": (solo el número decimal)
            - "total": (solo el número decimal)
            - "tipo_combustible": (ej. Diesel, Gasohol 95)
            - "cantidad": (ej. 10.500 GAL)
            - "proveedor": (El nombre del establecimiento comercial)
            - "ruc": (Los 11 dígitos del RUC)
            - "direccion": (La dirección del comprobante)
            """
            
            respuesta = cliente_ia.models.generate_content(
                model='gemini-1.5-flash', 
                contents=[parte_imagen, prompt]
            )
            
            match = re.search(r'\{.*\}', respuesta.text, re.DOTALL)
            
            if match:
                datos_ia = json.loads(match.group(0))
                numero_doc = datos_ia.get("numero_documento", "POR-ASIGNAR")
                subtotal_monto = float(datos_ia.get("subtotal", 0.0))
                igv_monto = float(datos_ia.get("igv", 0.0))
                total_monto = float(datos_ia.get("total", 0.0))
                tipo_combustible = datos_ia.get("tipo_combustible", "NO INDICA")
                cantidad_combustible = datos_ia.get("cantidad", "0")
                proveedor_ia = datos_ia.get("proveedor", "GRIFO (Desde App)").upper()
                ruc_ia = datos_ia.get("ruc", "")
                direccion_ia = datos_ia.get("direccion", "Dirección no indicada")
                
                if subtotal_monto == 0.0 and total_monto > 0:
                    subtotal_monto = round(total_monto / 1.18, 2)
                    igv_monto = round(total_monto - subtotal_monto, 2)
            else:
                raise ValueError("No JSON found")
        except Exception as e:
            print(f"⚠️ Error IA: {e}")

        cursor = conn.cursor()
        if ruc_ia and ruc_ia.isdigit() and len(ruc_ia) == 11:
            try:
                cursor.execute("SELECT ruc FROM proveedores WHERE ruc = %s", (ruc_ia,))
                if not cursor.fetchone():
                    cursor.execute("INSERT INTO proveedores (ruc, nombre, direccion_fiscal, categoria) VALUES (%s, %s, %s, %s)", (ruc_ia, proveedor_ia, direccion_ia, "Combustible / Grifo"))
                    conn.commit()
            except Exception: conn.rollback()

        if numero_doc and numero_doc not in ["POR-ASIGNAR", "ERROR-LECTURA"]:
            cursor.execute("SELECT COUNT(*) FROM facturas_recibidas WHERE numero_documento = %s AND proveedor = %s", (numero_doc, proveedor_ia))
            if cursor.fetchone()[0] > 0:
                conn.close()
                return {"status": "warning", "mensaje": f"El ticket {numero_doc} ya está registrado."}

        cursor.execute("UPDATE flota_vehiculos SET kilometraje = %s WHERE placa = %s", (kilometraje, placa))

        for col in ["kilometraje", "cantidad_combustible", "ruc"]:
            try:
                cursor.execute(f"ALTER TABLE facturas_recibidas ADD COLUMN {col} VARCHAR(50);")
                conn.commit()
            except Exception: conn.rollback() 
            
        try:
            cursor.execute("ALTER TABLE facturas_recibidas ADD COLUMN imagen_base64 TEXT;")
            conn.commit()
        except Exception: conn.rollback() 

        descripcion_final = f"Combustible: {tipo_combustible} | Cant: {cantidad_combustible}"
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        tipo_doc_final = "Factura (18% IGV)" if numero_doc.startswith("F") else "Boleta / Ticket"
        
        cursor.execute("""
            INSERT INTO facturas_recibidas (
                tipo_documento, numero_documento, fecha, proveedor, 
                descripcion, evento_asociado, subtotal, impuesto, 
                total, archivo_ruta, categoria, kilometraje, cantidad_combustible, ruc, imagen_base64
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            tipo_doc_final, numero_doc, fecha_hoy, proveedor_ia, 
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
        return {"status": "success", "mensaje": "Ticket procesado y subido a la nube."}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

# =================================================================
# 2. MÓDULO DE GPS Y ASISTENCIA
# =================================================================

@app.get("/geocerca-config/")
async def obtener_geocerca():
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error de BD")
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT latitud, longitud, radio FROM configuracion_geocerca LIMIT 1")
        res = cursor.fetchone()
        if res:
            return {"latitud": float(res[0]), "longitud": float(res[1]), "radio": float(res[2])}
        else:
            return {"latitud": -12.046374, "longitud": -77.042793, "radio": 100.0}
    finally:
        conn.close()

@app.post("/registrar-asistencia/")
async def registrar_asistencia(placa: str = Form(...), evento: str = Form(...)):
    conn = conectar_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Error de BD")
    
    try:
        cursor = conn.cursor()
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        hora_actual = datetime.now().strftime("%H:%M:%S")
        placa = placa.upper().strip()
        evento = evento.upper().strip()

        cursor.execute("SELECT id, hora_entrada, hora_salida FROM registro_asistencia WHERE placa = %s AND fecha = %s", (placa, fecha_hoy))
        registro = cursor.fetchone()

        if evento == "ENTRADA":
            if not registro:
                cursor.execute("""
                    INSERT INTO registro_asistencia (placa, fecha, hora_entrada, hora_salida, estado) 
                    VALUES (%s, %s, %s, %s, %s)
                """, (placa, fecha_hoy, hora_actual, "", "En Base"))
                
        elif evento == "SALIDA":
            if registro:
                id_reg = registro[0]
                cursor.execute("UPDATE registro_asistencia SET hora_salida = %s, estado = %s WHERE id = %s", (hora_actual, "En Ruta", id_reg))
            else:
                cursor.execute("""
                    INSERT INTO registro_asistencia (placa, fecha, hora_entrada, hora_salida, estado) 
                    VALUES (%s, %s, %s, %s, %s)
                """, (placa, fecha_hoy, "Sin registro", hora_actual, "En Ruta"))
                
        conn.commit()
        return {"status": "success", "mensaje": f"{evento} registrada para {placa}"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
