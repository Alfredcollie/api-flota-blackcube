# -*- coding: utf-8 -*-
"""
CREAR_USUARIO.PY - Crea o actualiza un usuario para la app (control de acceso).

USO (desde tu computadora, en la carpeta del proyecto):
    python crear_usuario.py USUARIO "NOMBRE COMPLETO" CLAVE
    python crear_usuario.py USUARIO "NOMBRE" CLAVE --inactivo

  - Sin --inactivo  -> crea/actualiza el usuario con ACCESO ACTIVADO (puede entrar).
  - Con --inactivo  -> crea/actualiza el usuario con ACCESO DESACTIVADO (no puede entrar).

Para conectarse a la base de datos, igual que conexion.py, necesita las credenciales
de Supabase por una de estas vías:
  1) Llavero del sistema (configurado con 'python configurar_credenciales.py')
  2) Variables de entorno: SUPABASE_DB_HOST/PORT/NAME/USER/PASSWORD
  3) Archivo config_db.json junto a este script.

El usuario y la clave se guardan con la clave ENCRIPTADA (nunca en texto plano).
"""
import sys

from conexion import conectar_db, liberar_conexion
from auth import hash_password


def _crear_tabla(cursor):
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


def main():
    args = [a for a in sys.argv[1:]]
    inactivo = "--inactivo" in args
    args = [a for a in args if a != "--inactivo"]

    if len(args) != 3:
        print("Uso: python crear_usuario.py USUARIO \"NOMBRE COMPLETO\" CLAVE [--inactivo]")
        sys.exit(1)

    username, nombre, clave = args[0], args[1], args[2]

    conn = conectar_db()
    if not conn:
        print("❌ No se pudo conectar a la base de datos.")
        print("   Configura las credenciales de Supabase (llavero, variables de entorno")
        print("   o archivo config_db.json).")
        sys.exit(1)

    try:
        cursor = conn.cursor()
        _crear_tabla(cursor)
        conn.commit()

        cursor.execute("SELECT username FROM app_usuarios WHERE username = %s", (username,))
        existe = cursor.fetchone()
        activo = not inactivo

        if existe:
            cursor.execute(
                "UPDATE app_usuarios SET password_hash = %s, nombre = %s, activo = %s "
                "WHERE username = %s",
                (hash_password(clave), nombre, activo, username),
            )
            print("✅ Usuario '%s' ACTUALIZADO (acceso %s)." % (username, "ACTIVADO" if activo else "DESACTIVADO"))
        else:
            cursor.execute(
                "INSERT INTO app_usuarios (username, password_hash, nombre, activo) "
                "VALUES (%s, %s, %s, %s)",
                (username, hash_password(clave), nombre, activo),
            )
            print("✅ Usuario '%s' CREADO (acceso %s)." % (username, "ACTIVADO" if activo else "DESACTIVADO"))

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("❌ Error: %s" % e)
        sys.exit(1)
    finally:
        liberar_conexion(conn)


if __name__ == "__main__":
    main()
