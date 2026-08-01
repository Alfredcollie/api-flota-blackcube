# -*- coding: utf-8 -*-
import psycopg2
from psycopg2 import OperationalError
import tkinter as tk
from tkinter import messagebox
from datetime import datetime

# ============================================================
# CREDENCIALES DEL SERVIDOR POSTGRESQL (SUPABASE - POOLER IPv4)
# ============================================================

DB_CONFIG = {
    "dbname": "postgres",
    "user": "postgres.lnmuzwlxcxewobdvlgrh",
    "password": "Ruc20613993682*",                            
    "host": "aws-0-us-east-2.pooler.supabase.com",
    "port": "6543"
}

_conexion_nube = None

class ConexionPersistente:
    def __init__(self, conexion_real):
        self._conn = conexion_real

    def close(self):
        pass
        
    def __getattr__(self, attr):
        if self._conn.closed:
            global _conexion_nube
            _conexion_nube = psycopg2.connect(**DB_CONFIG)
            _conexion_nube.autocommit = True
            self._conn = _conexion_nube
        return getattr(self._conn, attr)


def conectar_db(silencioso=False):
    global _conexion_nube
    try:
        if _conexion_nube is None or _conexion_nube.closed:
            _conexion_nube = psycopg2.connect(**DB_CONFIG)
            _conexion_nube.autocommit = True  
            
        return ConexionPersistente(_conexion_nube)

    except OperationalError as e:
        if not silencioso:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Error Crítico de Conexión", f"No se pudo establecer conexión con la Base de Datos.\n\nDetalle:\n{e}")
            root.destroy()
        return None


def registrar_auditoria(usuario, modulo, accion):
    conn = conectar_db(silencioso=True)
    if not conn: return
    try:
        cursor = conn.cursor()
        fecha_actual = datetime.now().strftime("%Y-%m-%d")
        hora_actual = datetime.now().strftime("%H:%M:%S")
        
        cursor.execute("""
            INSERT INTO bitacora_auditoria (fecha, hora, usuario, modulo, accion) 
            VALUES (%s, %s, %s, %s, %s)
        """, (fecha_actual, hora_actual, usuario, modulo, accion))
        conn.commit()
    except Exception as e:
        print("Error al registrar auditoría:", e)
    finally:
        conn.close()