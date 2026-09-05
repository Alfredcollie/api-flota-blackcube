# -*- coding: utf-8 -*-
"""
CONEXION.PY (v3 SEGURA + OPTIMIZADA)
- Credenciales de Supabase en llavero del sistema (keyring).
- Pool de Conexiones Persistente (ThreadedConnectionPool).
- Auditoría Asíncrona (Background Threading).
"""
import logging
import os
import sys
import psycopg2
from psycopg2 import pool
import keyring
import threading
from datetime import datetime

# Bundle de certificados raíz (necesario para SSL a Supabase en macOS/PyInstaller)
try:
    import certifi
    _CA_BUNDLE = certifi.where()
except Exception:
    _CA_BUNDLE = None

SERVICE_NAME = "ControlFlota"

# =========================================================
# ⚙️ CREDENCIALES (Supabase)
# YA NO hay secretos en el código. Se leen, en este orden:
#   1) Llavero del sistema (keyring)  -> recomendado
#   2) Variables de entorno:
#        SUPABASE_DB_HOST      SUPABASE_DB_PORT
#        SUPABASE_DB_NAME      SUPABASE_DB_USER
#        SUPABASE_DB_PASSWORD
#   3) Archivo config_db.json (junto al ejecutable/.app)
# Para guardarlas en el llavero:  python configurar_credenciales.py
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] conexion_supabase: %(message)s"
)

# Variable global para el Pool de conexiones
_connection_pool = None


def _credenciales_llavero():
    """Credenciales leídas del llavero del sistema (None si no existen)."""
    def _get(clave):
        try:
            return keyring.get_password(SERVICE_NAME, clave)
        except Exception:
            return None
    return {
        "host": _get("SUPABASE_DB_HOST"),
        "port": _get("SUPABASE_DB_PORT"),
        "dbname": _get("SUPABASE_DB_NAME"),
        "user": _get("SUPABASE_DB_USER"),
        "password": _get("SUPABASE_DB_PASSWORD"),
    }


def _credenciales_entorno():
    """Credenciales desde variables de entorno (alternativa al llavero)."""
    return {
        "host": os.environ.get("SUPABASE_DB_HOST", ""),
        "port": os.environ.get("SUPABASE_DB_PORT", ""),
        "dbname": os.environ.get("SUPABASE_DB_NAME", ""),
        "user": os.environ.get("SUPABASE_DB_USER", ""),
        "password": os.environ.get("SUPABASE_DB_PASSWORD", ""),
    }


def _credenciales_archivo():
    """Credenciales desde config_db.json (junto al ejecutable/.app).

    Útil para apps compiladas donde no hay llavero ni variables de entorno:
    basta dejar un archivo config_db.json al lado del .app/.exe con:
        {"host": "...", "port": "...", "dbname": "...", "user": "...", "password": "..."}
    """
    try:
        base = (
            os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__))
        )
        ruta = os.path.join(base, "config_db.json")
        if os.path.exists(ruta):
            import json
            with open(ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
            return {
                "host": datos.get("host", ""),
                "port": str(datos.get("port", "")),
                "dbname": datos.get("dbname", ""),
                "user": datos.get("user", ""),
                "password": datos.get("password", ""),
            }
    except Exception:
        pass
    return {"host": "", "port": "", "dbname": "", "user": "", "password": ""}


def leer_credenciales():
    """Credenciales: llavero -> variables de entorno -> config_db.json."""
    ll = _credenciales_llavero()
    env = _credenciales_entorno()
    archivo = _credenciales_archivo()
    return {
        "host": ll["host"] or env["host"] or archivo["host"],
        "port": ll["port"] or env["port"] or archivo["port"],
        "dbname": ll["dbname"] or env["dbname"] or archivo["dbname"],
        "user": ll["user"] or env["user"] or archivo["user"],
        "password": ll["password"] or env["password"] or archivo["password"],
    }


def _crear_pool(cred):
    """Crea el ThreadedConnectionPool con unas credenciales dadas (o None)."""
    if not cred["host"] or not cred["user"] or not cred["password"]:
        return None
    base = {
        "minconn": 1,
        "maxconn": 15,
        "host": cred["host"],
        "port": int(cred["port"]),
        "database": cred["dbname"],
        "user": cred["user"],
        "password": cred["password"],
        "connect_timeout": 10,
        # Supabase exige SSL. En macOS (app de PyInstaller) no hay CA raíz del
        # sistema, por eso forzamos SSL; si certifi está disponible se verifica
        # el certificado, y si no, se conecta igual sin verificar.
        "sslmode": "require",
    }
    if _CA_BUNDLE and os.path.exists(_CA_BUNDLE):
        try:
            return psycopg2.pool.ThreadedConnectionPool(**{**base, "sslrootcert": _CA_BUNDLE})
        except Exception:
            pass
    return psycopg2.pool.ThreadedConnectionPool(**base)


def inicializar_pool(silencioso=False):
    """Inicializa el pool de conexiones persistentes.

    Lee las credenciales por prioridad: llavero -> variables de entorno ->
    archivo config_db.json (junto al ejecutable/.app)."""
    global _connection_pool
    if _connection_pool is not None:
        return

    cred = leer_credenciales()
    if not cred["password"]:
        if not silencioso:
            logging.error(
                "No se encontró la contraseña de Supabase. Configúrala con "
                "'python configurar_credenciales.py', con las variables de "
                "entorno SUPABASE_DB_* o con un archivo config_db.json junto al ejecutable."
            )
        return

    try:
        _connection_pool = _crear_pool(cred)
        if _connection_pool is None and not silencioso:
            logging.error("No se pudo crear el pool de conexiones (credenciales incompletas).")
    except Exception as e:
        if not silencioso:
            logging.error(f"Error al conectar a Supabase: {e}")
        _connection_pool = None


def conectar_db(silencioso=False):
    """Obtiene una conexión pre-creada del Pool en lugar de crear una nueva."""
    global _connection_pool
    if _connection_pool is None:
        inicializar_pool(silencioso)
        
    try:
        if _connection_pool:
            return _connection_pool.getconn()
    except Exception as e:
        if not silencioso:
            logging.error(f"Error al obtener conexión del pool: {e}")
    return None


def liberar_conexion(conn):
    """Devuelve la conexión al pool para que sea reutilizada por otro proceso."""
    global _connection_pool
    if _connection_pool and conn:
        try:
            _connection_pool.putconn(conn)
        except Exception:
            pass


def _tarea_auditoria_asincrona(usuario, modulo, accion):
    """Función interna que se ejecuta en un hilo separado (Background)."""
    conn = conectar_db(silencioso=True)
    if not conn:
        return
    try:
        ahora = datetime.now()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO bitacora_auditoria (fecha, hora, usuario, modulo, accion) VALUES (%s, %s, %s, %s, %s)",
            (ahora.strftime("%d/%m/%Y"), ahora.strftime("%H:%M:%S"), usuario, modulo, accion)
        )
        conn.commit()
    except Exception as e:
        logging.error(f"Error en auditoría asíncrona: {e}")
    finally:
        # IMPORTANTE: Liberamos la conexión en lugar de cerrarla
        liberar_conexion(conn)


def registrar_auditoria(usuario, modulo, accion):
    """Registra una acción en la bitácora sin congelar la interfaz (Asíncrono)."""
    if usuario in ["Desconocido", "Invitado", None]:
        return
    
    # Lanzamos la escritura a la base de datos en un "hilo" paralelo (Daemon)
    hilo = threading.Thread(
        target=_tarea_auditoria_asincrona, 
        args=(usuario, modulo, accion),
        daemon=True
    )
    hilo.start()


if __name__ == "__main__":
    c = conectar_db()
    if c:
        print("✅ Conexión (Pool) correcta leyendo desde el llavero del sistema.")
        liberar_conexion(c)
    else:
        print("❌ Sin conexión. Ejecuta primero: python configurar_credenciales.py")