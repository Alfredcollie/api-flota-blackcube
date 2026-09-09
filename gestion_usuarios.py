# -*- coding: utf-8 -*-
"""
GESTION_USUARIOS.PY
Panel amigable para gestionar los usuarios de la app móvil (Flota Black Cube).

Permite: CREAR, ELIMINAR y BLOQUEAR/DESBLOQUEAR usuarios con un clic,
y cambiar la clave.

Uso:  python gestion_usuarios.py

Requiere conexión a Supabase (igual que conexion.py): llavero del sistema,
variables de entorno SUPABASE_DB_*, o el archivo config_db.json.
"""
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from conexion import conectar_db, liberar_conexion
from auth import hash_password


def _conectar_o_error():
    """Abre una conexión y asegura las tablas 'app_usuarios' y 'app_tokens_acceso'."""
    conn = conectar_db()
    if not conn:
        messagebox.showerror(
            "Sin conexión",
            "No se pudo conectar a la base de datos.\n\n"
            "Revisa config_db.json, las variables SUPABASE_DB_* o el llavero.",
        )
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS app_usuarios ("
            " username VARCHAR(150) PRIMARY KEY,"
            " password_hash TEXT NOT NULL,"
            " nombre VARCHAR(200),"
            " activo BOOLEAN DEFAULT TRUE,"
            " creado_en TIMESTAMPTZ DEFAULT now())"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS app_tokens_acceso ("
            " token TEXT PRIMARY KEY,"
            " username VARCHAR(150) NOT NULL,"
            " creado_en TIMESTAMPTZ DEFAULT now())"
        )
        conn.commit()
        return conn
    except Exception:
        conn.rollback()
        liberar_conexion(conn)
        return None


class PanelUsuarios:
    def __init__(self, root):
        self.root = root
        root.title("Gestión de Usuarios - Flota Black Cube")
        root.geometry("780x520")
        root.minsize(680, 460)

        self._build_ui()
        self.refrescar()

    # ---------- INTERFAZ ----------
    def _build_ui(self):
        # Encabezado
        tk.Label(
            self.root,
            text="👥 Gestión de Usuarios de la App",
            font=("Segoe UI", 16, "bold"),
            bg="#1F538D",
            fg="white",
            pady=10,
        ).pack(fill="x")

        # ---- Formulario (crear) ----
        frm = ttk.LabelFrame(self.root, text="Crear usuario nuevo", padding=10)
        frm.pack(fill="x", padx=12, pady=(12, 6))

        self.e_usuario = ttk.Entry(frm, width=22)
        self.e_nombre = ttk.Entry(frm, width=30)
        self.e_clave = ttk.Entry(frm, width=22, show="*")

        ttk.Label(frm, text="Usuario:").grid(row=0, column=0, sticky="e", padx=(0, 6))
        self.e_usuario.grid(row=0, column=1, sticky="w")
        ttk.Label(frm, text="Nombre completo:").grid(row=0, column=2, sticky="e", padx=(16, 6))
        self.e_nombre.grid(row=0, column=3, sticky="w")
        ttk.Label(frm, text="Clave:").grid(row=0, column=4, sticky="e", padx=(16, 6))
        self.e_clave.grid(row=0, column=5, sticky="w")
        ttk.Button(frm, text="➕ Crear usuario", command=self.crear).grid(row=0, column=6, padx=(16, 0))

        # ---- Tabla de usuarios ----
        tfrm = ttk.LabelFrame(self.root, text="Usuarios existentes", padding=8)
        tfrm.pack(fill="both", expand=True, padx=12, pady=6)

        cols = ("usuario", "nombre", "estado")
        self.tabla = ttk.Treeview(tfrm, columns=cols, show="headings", selectmode="browse")
        self.tabla.heading("usuario", text="Usuario")
        self.tabla.heading("nombre", text="Nombre completo")
        self.tabla.heading("estado", text="Estado")
        self.tabla.column("usuario", width=150, anchor="w")
        self.tabla.column("nombre", width=320, anchor="w")
        self.tabla.column("estado", width=120, anchor="center")
        self.tabla.tag_configure("activo", foreground="#1E7C2B")
        self.tabla.tag_configure("bloqueado", foreground="#B00020")

        sb = ttk.Scrollbar(tfrm, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=sb.set)
        self.tabla.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ---- Botones de acción ----
        brm = ttk.Frame(self.root, padding=8)
        brm.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(brm, text="🔒 Bloquear / 🔓 Desbloquear", command=self.bloquear).pack(side="left", padx=(0, 8))
        ttk.Button(brm, text="🔑 Cambiar clave", command=self.cambiar_clave).pack(side="left", padx=(0, 8))
        ttk.Button(brm, text="🗑️ Eliminar", command=self.eliminar).pack(side="left", padx=(0, 8))
        ttk.Button(brm, text="🔄 Actualizar", command=self.refrescar).pack(side="left", padx=(0, 8))

        # ---- Barra de estado ----
        self.status = tk.Label(self.root, text="Listo", anchor="w", padx=12, pady=4, bg="#f0f0f0")
        self.status.pack(fill="x", side="bottom")

    # ---------- UTILIDADES ----------
    def _set_status(self, msg):
        self.status.config(text=msg)

    def _seleccion(self):
        sel = self.tabla.selection()
        if not sel:
            messagebox.showwarning("Selecciona uno", "Selecciona un usuario de la lista.")
            return None
        return self.tabla.item(sel[0])["values"][0]

    def _ejecutar(self, sql, params=()):
        """Ejecuta una sentencia y devuelve filas (si es SELECT) o None."""
        conn = _conectar_o_error()
        if conn is None:
            return None
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            if sql.strip().upper().startswith("SELECT"):
                filas = cur.fetchall()
            else:
                filas = None
            conn.commit()
            return filas
        except Exception as e:
            conn.rollback()
            messagebox.showerror("Error de base de datos", str(e))
            return None
        finally:
            liberar_conexion(conn)

    # ---------- ACCIONES ----------
    def refrescar(self):
        filas = self._ejecutar(
            "SELECT username, nombre, activo FROM app_usuarios ORDER BY username"
        )
        self.tabla.delete(*self.tabla.get_children())
        if filas is None:
            self._set_status("No se pudo cargar la lista.")
            return
        for u, nombre, activo in filas:
            estado = "Activo" if activo else "Bloqueado"
            tag = "activo" if activo else "bloqueado"
            self.tabla.insert(
                "", "end", values=(u, nombre or "", estado), tags=(tag,)
            )
        self._set_status("Mostrando %d usuario(s)." % len(filas))

    def crear(self):
        usuario = self.e_usuario.get().strip()
        nombre = self.e_nombre.get().strip()
        clave = self.e_clave.get()
        if not usuario or not nombre or not clave:
            messagebox.showwarning("Faltan datos", "Completa usuario, nombre y clave.")
            return
        # Si ya existe, preguntar si se actualiza
        fila = self._ejecutar(
            "SELECT username FROM app_usuarios WHERE username = %s", (usuario,)
        )
        if fila:
            if not messagebox.askyesno(
                "Usuario existente",
                "El usuario '%s' ya existe.\n¿Quieres actualizar su nombre y clave?" % usuario,
            ):
                return
            self._ejecutar(
                "UPDATE app_usuarios SET password_hash=%s, nombre=%s WHERE username=%s",
                (hash_password(clave), nombre, usuario),
            )
            msg = "Usuario '%s' actualizado." % usuario
        else:
            self._ejecutar(
                "INSERT INTO app_usuarios (username, password_hash, nombre, activo) "
                "VALUES (%s, %s, %s, TRUE)",
                (usuario, hash_password(clave), nombre),
            )
            msg = "Usuario '%s' creado." % usuario

        self.e_usuario.delete(0, "end")
        self.e_nombre.delete(0, "end")
        self.e_clave.delete(0, "end")
        self.refrescar()
        self._set_status(msg)

    def bloquear(self):
        usuario = self._seleccion()
        if usuario is None:
            return
        fila = self._ejecutar(
            "SELECT activo FROM app_usuarios WHERE username = %s", (usuario,)
        )
        if not fila:
            return
        activo = fila[0][0]
        nuevo = not activo
        # Al bloquear, se borran los tokens para expulsarlo de la app ya.
        if not nuevo:
            self._ejecutar("DELETE FROM app_tokens_acceso WHERE username = %s", (usuario,))
        self._ejecutar(
            "UPDATE app_usuarios SET activo = %s WHERE username = %s", (nuevo, usuario)
        )
        self.refrescar()
        self._set_status(
            "Usuario '%s' %s." % (usuario, "DESBLOQUEADO" if nuevo else "BLOQUEADO")
        )

    def cambiar_clave(self):
        usuario = self._seleccion()
        if usuario is None:
            return
        clave = simpledialog.askstring(
            "Cambiar clave", "Nueva clave para '%s':" % usuario, show="*", parent=self.root
        )
        if not clave:
            return
        self._ejecutar(
            "UPDATE app_usuarios SET password_hash=%s WHERE username=%s",
            (hash_password(clave), usuario),
        )
        self.refrescar()
        self._set_status("Clave de '%s' actualizada." % usuario)

    def eliminar(self):
        usuario = self._seleccion()
        if usuario is None:
            return
        if not messagebox.askyesno(
            "Confirmar", "¿Eliminar al usuario '%s'?\nSe borrará su acceso y sus sesiones." % usuario
        ):
            return
        self._ejecutar("DELETE FROM app_tokens_acceso WHERE username = %s", (usuario,))
        self._ejecutar("DELETE FROM app_usuarios WHERE username = %s", (usuario,))
        self.refrescar()
        self._set_status("Usuario '%s' eliminado." % usuario)


def main():
    root = tk.Tk()
    PanelUsuarios(root)
    root.mainloop()


if __name__ == "__main__":
    main()
