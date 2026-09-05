# -*- coding: utf-8 -*-
"""
APP_PATHS.PY - Rutas de configuración y recursos.
- BASE_DIR / RESOURCES_DIR: carpeta del ejecutable (recursos de SOLO LECTURA).
- DATA_DIR / CONFIG_FILE: carpeta de datos del usuario (ESCRIBIBLE):
    Windows -> %APPDATA%\\ControlFlota
    macOS   -> ~/Library/Application Support/ControlFlota
    Linux   -> ~/.config/ControlFlota
En desarrollo (python) la config sigue en la carpeta del proyecto.
"""
import os
import sys
import shutil
from pathlib import Path

APP_NAME = "ControlFlota"


def _base_dir():
    """Carpeta base: la del .exe/.app si está compilado, o la del script."""
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable))
    return Path(__file__).resolve().parent


def _data_dir():
    """Carpeta de datos del usuario (escribible)."""
    if sys.platform == "win32":
        raiz = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        raiz = Path.home() / "Library" / "Application Support"
    else:
        raiz = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    carpeta = raiz / APP_NAME
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return carpeta


BASE_DIR = _base_dir()
DATA_DIR = _data_dir()

# Archivo de configuración: en la carpeta de datos si está compilado
# (para poder escribir sin permisos de administrador), o en el proyecto en desarrollo.
CONFIG_FILE = (DATA_DIR if getattr(sys, "frozen", False) else BASE_DIR) / "config_local.json"

# Carpeta para recursos (logos, etc.) — junto al ejecutable
RESOURCES_DIR = BASE_DIR


def _migrar_config():
    """Copia config_local.json desde el ejecutable (config por defecto del
    instalador) a la carpeta de datos del usuario, solo la primera vez."""
    if not getattr(sys, "frozen", False):
        return
    try:
        if not CONFIG_FILE.exists():
            origen = BASE_DIR / "config_local.json"
            if origen.exists():
                shutil.copy2(str(origen), str(CONFIG_FILE))
    except Exception:
        pass


_migrar_config()
