# -*- coding: utf-8 -*-
import io
import sys
# Configurar UTF-8 para Windows
if sys.platform == 'win32':
    import os
    os.environ['PYTHONIOENCODING'] = 'utf-8'

import tkinter as tk
import ttkbootstrap as tb
from tkinter import ttk, messagebox, simpledialog
from ttkbootstrap.constants import *
import psycopg2
from db_pool import get_db_pool, db_connection
import os
import subprocess
import json
import base64
import pandas as pd
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle
import tempfile
import math
from collections import defaultdict
import hashlib
import time
from datetime import datetime, timedelta
from PIL import Image, ImageTk
from typing import Any, Dict, Optional, Tuple
import ctypes
import threading
import logging

# ============================================================================
# CONFIGURACIÓN DE LOGS CON EL MÓDULO LOGGING
# ============================================================================
import logging
import time

LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "labelsapp_debug.log")

# Para medir tiempos
_tiempo_inicio_envio = None

# Para feature de 5 minutos: almacenar pedidos finalizados
# Estructura: {id_pedido: {'timestamp': time.time(), 'factura': id_factura, 'producto': producto}}
_pedidos_finalizados_5min = {}
TIEMPO_MUESTRA_FINALIZADO_MIN = 5  # minutos

# Variable para control de parpadeo de pestaña
_parpadeando = False  # Flag para indicar si está parpadeando
_timer_parpadeo = None  # Timer del parpadeo

# ✅ NUEVO: Tracking de pedidos con sonido ya reproducido (evita sonido en bucle)
_pedidos_sonido_reproducido = set()

# ✅ NUEVO: Acumula pedidos para procesarlos en una sola alerta
_pedidos_pendientes_alerta = []
_timer_procesamiento_alertas = None
_pedidos_sonido_reproducido = set()

# ✅ DEBOUNCE: Timestamps de último evento para evitar ejecución múltiple
_last_editar_producto_en_lista = 0
_last_editar_producto = 0
_last_menu_ctx = 0
_last_menu_contextual = 0

# Configurar logging
try:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),
            logging.StreamHandler()  # También a consola
        ]
    )
    logger = logging.getLogger(__name__)
except Exception as e:
    logger = None

def escribir_log(mensaje):
    """Escribe mensaje a archivo y consola - MUY ROBUSTO con logging de Python"""
    # Intenta con logging primero (más robusto)
    if logger:
        try:
            logger.info(mensaje)
            return
        except Exception as e:
            pass
    
    # Fallback: escritura directa
    try:
        print(mensaje)
    except:
        pass
    
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{mensaje}\n")
            f.flush()
            os.fsync(f.fileno())  # Fuerza escritura a disco
    except Exception as e:
        try:
            print(f"[ERROR-LOG] {e}")
        except:
            pass

# ============================================================================
# SONIDO - MÚLTIPLES MÉTODOS
# ============================================================================

def reproducir_sonido_sistema():
    """Reproduce un único beep simple usando el método disponible en el sistema"""
    try:
        print("[SONIDO] 🔊 reproducir_sonido_sistema() LLAMADO")
        escribir_log("[SONIDO] 🔊 reproducir_sonido_sistema() LLAMADO")
        
        # Método 1: Windows winsound (más confiable en Windows)
        try:
            import winsound
            print("[SONIDO] ℹ️ Intentando winsound...")
            escribir_log("[SONIDO] ℹ️ Intentando winsound...")
            winsound.Beep(1000, 200)  # Un único beep: 1000 Hz por 200ms
            print("[SONIDO] ✅ Beep reproducido con winsound")
            escribir_log("[SONIDO] ✅ Beep reproducido con winsound")
            return True
        except ImportError as ie:
            print(f"[SONIDO] ⚠️ winsound no disponible: {ie}")
            escribir_log(f"[SONIDO] ⚠️ winsound no disponible: {ie}")
        except Exception as e:
            print(f"[SONIDO] ⚠️ Error en winsound: {e}")
            escribir_log(f"[SONIDO] ⚠️ Error en winsound: {e}")
        
        # Método 2: PowerShell Beep (fallback)
        try:
            print("[SONIDO] ℹ️ Intentando PowerShell...")
            escribir_log("[SONIDO] ℹ️ Intentando PowerShell...")
            import subprocess
            result = subprocess.run(['powershell', '-c', '[console]::Beep(1000, 200)'], 
                                  capture_output=True, text=True, timeout=2)
            print(f"[SONIDO] ✅ Beep reproducido con PowerShell: {result.returncode}")
            escribir_log(f"[SONIDO] ✅ Beep reproducido con PowerShell: {result.returncode}")
            return True
        except Exception as e:
            print(f"[SONIDO] ⚠️ PowerShell falló: {e}")
            escribir_log(f"[SONIDO] ⚠️ PowerShell falló: {e}")
        
        # Si nada funcionó
        print("[SONIDO] ⚠️ No se pudo reproducir sonido")
        escribir_log("[SONIDO] ⚠️ No se pudo reproducir sonido")
        return False
        
    except Exception as e:
        print(f"[SONIDO] ❌ Error general: {e}")
        escribir_log(f"[SONIDO] ❌ Error general: {e}")
        return False

# ============================================================================
# CONSTANTES DE CONFIGURACIÓN
# ============================================================================
# Usa versión global desde version_config.py
try:
    from version_config import APP_VERSION
except ImportError:
    APP_VERSION = "1.0.0"  # Fallback

DB_CONFIG: Dict[str, Any] = {
    "host": "dpg-d1b18u8dl3ps73e68v1g-a.oregon-postgres.render.com",
    "port": 5432,
    "database": "labels_app_db",
    "user": "admin",
    "password": "KCFjzM4KYzSQx63ArufESIXq03EFXHz3",
    "sslmode": "require"
}

URL_VERSION = "https://labelsapp.onrender.com/Version2.txt"
URL_EXE = "https://labelsapp.onrender.com/PaintFlow.exe"
DEBUG_LOGS = True
MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 24
DPI_MIN_SCALE = 1.0
DPI_MAX_SCALE = 2.5
DEFAULT_DPI_SCALE = 96.0

# ✅ CACHÉ GLOBAL para evitar queries redundantes
_CACHE_COLS_TABLA = {}  # {tabla_name: {col1, col2, ...}}
_CACHE_TABLAS_ASEGURADAS = set()  # Tablas que ya fueron verificadas/creadas

# Configurar logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if DEBUG_LOGS else logging.WARNING)

# ============================================================================
# SISTEMA DE ADAPTACIÓN DE ESCALA DPI
# ============================================================================
def obtener_escala_dpi() -> float:
    """Detecta el factor de escala DPI del sistema Windows.
    
    Returns:
        Factor de escala (1.0 = 96 DPI, 2.0 = 192 DPI, etc.)
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except OSError as e:
            logger.debug(f"DPI awareness setup failed: {e}")
    
    try:
        hdc = ctypes.windll.user32.GetDC(0) 
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        
        factor = dpi / DEFAULT_DPI_SCALE
        factor = max(DPI_MIN_SCALE, min(factor, DPI_MAX_SCALE))
        return factor
    except Exception as e:
        logger.warning(f"Failed to detect DPI, using default: {e}")
        return DPI_MIN_SCALE

# Variable global para almacenar el factor de escala
ESCALA_DPI = obtener_escala_dpi()
logger.info(f"DPI Scale Factor: {ESCALA_DPI}")

def adaptar_dimension(valor: int) -> int:
    """Ajusta un valor según el factor de escala DPI.
    
    Args:
        valor: Valor en píxeles a escalar
        
    Returns:
        Valor escalado según DPI del sistema
    """
    return int(valor * ESCALA_DPI)

def adaptar_geometria(ancho: int, alto: int) -> str:
    """Ajusta dimensiones de ventana según DPI y retorna string geometry.
    
    Args:
        ancho: Ancho base en píxeles
        alto: Alto base en píxeles
        
    Returns:
        String de geometría en formato 'WIDTHxHEIGHT'
    """
    nuevo_ancho = adaptar_dimension(ancho)
    nuevo_alto = adaptar_dimension(alto)
    return f"{nuevo_ancho}x{nuevo_alto}"

def centrar_ventana_adaptativa(ventana: tk.Tk, ancho_base: int, alto_base: int) -> None:
    """Centra una ventana en la pantalla usando dimensiones adaptativas.
    
    Args:
        ventana: Widget raíz de tkinter
        ancho_base: Ancho base sin escala
        alto_base: Alto base sin escala
    """
    ventana.update_idletasks()
    
    ancho = adaptar_dimension(ancho_base)
    alto = adaptar_dimension(alto_base)
    
    screen_width = ventana.winfo_screenwidth()
    screen_height = ventana.winfo_screenheight()
    
    x = (screen_width // 2) - (ancho // 2)
    y = (screen_height // 2) - (alto // 2)
    
    ventana.geometry(f"{ancho}x{alto}+{x}+{y}")

def adaptar_fuente(tamano_base: int) -> int:
    """Ajusta el tamaño de fuente según la escala DPI.
    
    Args:
        tamano_base: Tamaño de fuente base
        
    Returns:
        Tamaño de fuente escalado y limitado a rango válido
    """
    nuevo_tamano = int(tamano_base * ESCALA_DPI)
    nuevo_tamano = max(MIN_FONT_SIZE, min(nuevo_tamano, MAX_FONT_SIZE))
    return nuevo_tamano

def debug_log(*args: Any, **kwargs: Any) -> None:
    """Log debug messages si DEBUG_LOGS está habilitado.
    
    Args:
        *args: Argumentos para el log
        **kwargs: Keyword arguments para el log
    """
    if DEBUG_LOGS:
        logger.debug(" ".join(str(a) for a in args))

# Silenciar stdout por defecto para evitar mensajes en consola
if not DEBUG_LOGS:
    try:
        sys.stdout = open(os.devnull, 'w')
    except OSError:
        pass

def _normalizar_texto_sucursal(txt: str) -> str:
    """Normaliza texto de sucursal eliminando caracteres especiales.
    
    Args:
        txt: Texto de sucursal a normalizar
        
    Returns:
        Texto normalizado (minúsculas, sin espacios ni caracteres especiales)
    """
    s = (txt or "").lower().strip()
    for ch in [" ", "-", ".", ",", ":", ";", "_"]:
        s = s.replace(ch, "")
    return s

def _sucursal_desde_texto(texto: str) -> str:
    """Obtiene código de sucursal canónico a partir de texto libre.
    
    Devuelve 'principal' si no encuentra coincidencia.
    (Sin consulta a BD para evitar ciclos de llamada.)
    
    Args:
        texto: Texto con nombre de sucursal
        
    Returns:
        Código de sucursal normalizado
    """
    s = _normalizar_texto_sucursal(texto)
    synonyms: Dict[str, str] = {
        # Santo Domingo
        "alameda": "alameda",
        "churchill": "churchill",
        "bellavista": "bellavista",
        "tiradentes": "tiradentes",
        "zonaoriental": "zonaoriental",
        "arroyohondo": "arroyohondo",
        "villamella": "villamella",
        "sanisidro": "sanisidro",
        # Interior
        "bavaro": "bavaro",
        "puntacana": "puntacana",
        "puertoplata": "puertoplata",
        "lavega": "la_vega",
        "vega": "la_vega",
        "terrenas": "terrenas",
        "romana": "romana",
        "bani": "bani",
        "sanfrancisco": "sanfrancisco",
        "sanmartin": "sanmartin",
        "rafaelvidal": "rafaelvidal",
        # Santiago
        "santiago": "santiago1",
        "santiago1": "santiago1",
    }
    # Coincidencia por contiene (priorizar claves más largas)
    for alias in sorted(synonyms.keys(), key=len, reverse=True):
        if alias in s:
            return synonyms[alias]
    return "principal"

def get_db_connection():
    """Obtiene conexión del pool de conexiones.
    
    ⚠️  LEGACY: Esta función es para compatibilidad con código antiguo.
    En código nuevo, usar: with db_connection() as conn:
    
    Returns:
        Context manager que retorna una conexión PostgreSQL
    
    Uso:
        with get_db_connection() as conn:
            cur = conn.cursor()
            ...
    """
    from db_pool import db_connection
    return db_connection()

def obtener_cols_disponibles_cached(tabla_name: str, cur: psycopg2.extensions.cursor) -> set:
    """Obtiene columnas disponibles en tabla, con caché para evitar queries redundantes.
    
    Args:
        tabla_name: Nombre de la tabla
        cur: Cursor de base de datos
        
    Returns:
        Set de nombres de columna disponibles
    """
    global _CACHE_COLS_TABLA
    
    if tabla_name not in _CACHE_COLS_TABLA:
        try:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
                (tabla_name,)
            )
            _CACHE_COLS_TABLA[tabla_name] = {r[0] for r in cur.fetchall()}
            debug_log(f"🔷 [CACHE] Columnas de {tabla_name} cacheadas: {len(_CACHE_COLS_TABLA[tabla_name])}")
        except Exception as e:
            debug_log(f"⚠️ [CACHE] Error obteniendo columnas: {e}")
            _CACHE_COLS_TABLA[tabla_name] = set()
    
    return _CACHE_COLS_TABLA[tabla_name]

def asegurar_tabla_pendientes_para_sufijo(sufijo: str) -> None:
    """Crea la tabla pedidos_pendientes_{sufijo} si no existe (idempotente).
    Usa un esquema compatible con Gestor/Gestión Usuarios.
    
    ✅ OPTIMIZACIÓN: Usa caché para ejecutar solo UNA VEZ por tabla por sesión.
    
    Args:
        sufijo: Sufijo de sucursal para la tabla
    """
    global _CACHE_TABLAS_ASEGURADAS
    
    if not sufijo:
        return
    
    tabla = f"pedidos_pendientes_{sufijo}"
    
    # ✅ Si ya fue asegurada en esta sesión, saltear
    if tabla in _CACHE_TABLAS_ASEGURADAS:
        return
    
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {tabla} (
                    id SERIAL PRIMARY KEY,
                    id_orden_profesional VARCHAR(10) UNIQUE,
                    id_factura VARCHAR(120) NOT NULL,
                    codigo VARCHAR(60),
                    producto VARCHAR(160),
                    terminacion VARCHAR(120),
                    presentacion VARCHAR(40),
                    cantidad INTEGER DEFAULT 1,
                    prioridad VARCHAR(10) DEFAULT 'Media',
                    estado VARCHAR(20) DEFAULT 'Pendiente',
                    tiempo_estimado INTEGER DEFAULT 0,
                    base VARCHAR(60),
                    ubicacion VARCHAR(80),
                    sucursal VARCHAR(120),
                    operador VARCHAR(120),
                    codigo_base VARCHAR(80),
                    fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    fecha_asignacion TIMESTAMP NULL,
                    fecha_completado TIMESTAMP NULL
                );
            """)
            
            # Crear índices básicos (si fallan, continuamos)
            try:
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{tabla}_factura ON {tabla}(id_factura);")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{tabla}_estado ON {tabla}(estado);")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{tabla}_prioridad ON {tabla}(prioridad);")
            except psycopg2.Error as e:
                logger.debug(f"Index creation warning for {tabla}: {e}")
            
            conn.commit()
            cur.close()
            
            # ✅ Marcar tabla como asegurada para esta sesión
            _CACHE_TABLAS_ASEGURADAS.add(tabla)
            debug_log(f"✅ [TABLA] {tabla} asegurada (cacheado para sesión)")
    except psycopg2.Error as e:
        logger.error(f"Failed to ensure table {tabla}: {e}")
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

def generar_id_orden_rapido(id_factura: str, contador: int = 0) -> str:
    """
    Genera un ID de orden RÁPIDO y determinístico basado en factura.
    
    ✅ OPTIMIZACIÓN: Un solo UUID5, sin búsquedas en BD.
    Se llama UNA VEZ por inserción, muy eficiente.
    
    Args:
        id_factura: ID de la factura
        contador: Número secuencial para evitar colisiones (0, 1, 2...)
    
    Returns:
        ID único de 10 caracteres (O + 9 caracteres hex)
    
    Ejemplo:
        generar_id_orden_rapido("FAC001") → "OAFB1C2D3E"
        generar_id_orden_rapido("FAC001", 1) → "OAFB1C2D3F"
    """
    import uuid
    
    # UUID5 determinístico basado en factura + contador
    unique_str = f"{id_factura}#{contador}"
    id_hash = uuid.uuid5(uuid.NAMESPACE_DNS, unique_str)
    id_nuevo = f"O{id_hash.hex[:9].upper()}"
    return id_nuevo[:10]

def obtener_sucursal_usuario(usuario_id: str) -> str:
    """Obtiene la sucursal (código para la tabla) desde la base de datos.
    Prioridad:
    1) Leer sucursal del usuario en la tabla `usuarios` (join con `sucursales`).
    2) Si `sucursales` tiene columna `codigo`, usarla directamente.
    3) Si no hay `codigo`, mapear el nombre de la sucursal al sufijo de tabla existente
       buscando en information_schema las tablas `pedidos_pendientes_%`.
    4) Fallback final: inferir por texto (sinónimos) y si no, 'principal'.
    """
    try:
        if not usuario_id:
            return "principal"

        # 1) Intentar obtener nombre/código desde BD de usuarios
        # ✅ FIX 2: Usar get_db_connection() para usar el pool
        with get_db_connection() as conn:
            cur = conn.cursor()

            # Detectar si existe columna `codigo` en sucursales
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='sucursales'
                """
            )
            cols_suc = {r[0] for r in cur.fetchall()}
            tiene_codigo = 'codigo' in cols_suc

            if tiene_codigo:
                cur.execute(
                    """
                    SELECT COALESCE(s.codigo, '') AS codigo, COALESCE(s.nombre,'') AS nombre
                    FROM usuarios u
                    LEFT JOIN sucursales s ON u.sucursal_id = s.id
                    WHERE LOWER(u.username) = LOWER(%s) OR CAST(u.id AS TEXT) = %s
                    LIMIT 1
                    """,
                    (str(usuario_id), str(usuario_id))
                )
                row = cur.fetchone()
                if row and (row[0] or row[1]):
                    codigo, nombre = (row[0] or '').strip(), (row[1] or '').strip()
                    if codigo:
                        # Normalizar código simple (minúsculas, sin espacios)
                        codigo_norm = codigo.lower().strip().replace(" ", "")
                        try:
                            asegurar_tabla_pendientes_para_sufijo(codigo_norm)
                        except Exception:
                            pass
                        return codigo_norm or "principal"
                    if nombre:
                        # Si hay nombre pero no código, seguir mapeo por tablas
                        # (dejar cursor abierto, se reutiliza abajo)
                        suc_nombre = nombre
                    else:
                        suc_nombre = ''
            else:
                cur.execute(
                    """
                    SELECT COALESCE(s.nombre,'') AS nombre
                    FROM usuarios u
                    LEFT JOIN sucursales s ON u.sucursal_id = s.id
                    WHERE LOWER(u.username) = LOWER(%s) OR CAST(u.id AS TEXT) = %s
                    LIMIT 1
                    """,
                    (str(usuario_id), str(usuario_id))
                )
                r = cur.fetchone()
                suc_nombre = (r[0] or '').strip() if r else ''

            # 2) Mapear nombre de sucursal a sufijo de tabla existente
            def _norm_key(x: str) -> str:
                try:
                    import unicodedata, re
                    y = unicodedata.normalize('NFD', x or '')
                    y = ''.join(ch for ch in y if unicodedata.category(ch) != 'Mn')
                    y = y.lower()
                    y = re.sub(r"[^a-z0-9]", "", y)
                    return y
                except Exception:
                    return (x or '').lower().replace(' ', '').replace('_','').replace('-','')

            if suc_nombre:
                try:
                    cur.execute(
                        """
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema='public' AND table_name LIKE 'pedidos_pendientes_%'
                        """
                    )
                    tablas = [t[0] for t in cur.fetchall()]
                    destino = None
                    want = _norm_key(suc_nombre)
                    # Construir mapa de sufijos -> clave normalizada
                    candidatos = []
                    for t in tablas:
                        suf = t.replace('pedidos_pendientes_', '')
                        candidatos.append((suf, _norm_key(suf)))
                    # Coincidencia exacta por clave normalizada
                    for suf, key in candidatos:
                        if key == want:
                            destino = suf; break
                    # Coincidencia por contiene
                    if not destino:
                        for suf, key in candidatos:
                            if key in want or want in key:
                                destino = suf; break
                    if destino:
                        try:
                            asegurar_tabla_pendientes_para_sufijo(destino)
                        except Exception:
                            pass
                        return destino
                except Exception:
                    pass

        # 3) Fallback por texto (sinónimos conocidos) y por último 'principal'
        suf = _sucursal_desde_texto(suc_nombre or str(usuario_id))
        try:
            asegurar_tabla_pendientes_para_sufijo(suf)
        except Exception:
            pass
        return suf
    except Exception:
        # Fallback duro si hay error de conexión
        suf = _sucursal_desde_texto(usuario_id)
        try:
            asegurar_tabla_pendientes_para_sufijo(suf)
        except Exception:
            pass
        return suf

def version_tuple(v):
    return tuple(int(x) for x in v.strip().split(".") if x.isdigit())

def is_newer(latest, current):
    return version_tuple(latest) > version_tuple(current)

def run_windows_updater(new_exe_path, current_exe_path):
    # Creamos un .bat temporal que espera a que termine el proceso actual,
    # reemplaza el exe y lanza la nueva versión.
    bat = tempfile.NamedTemporaryFile(delete=False, suffix=".bat", mode="w", encoding="utf-8")
    new_p = new_exe_path.replace("/", "\\")
    cur_p = current_exe_path.replace("/", "\\")
    exe_name = os.path.basename(cur_p)
    bat_contents = f"""@echo off
timeout /t 2 /nobreak > nul
:waitloop
tasklist /FI "IMAGENAME eq {exe_name}" | find /I "{exe_name}" > nul
if %ERRORLEVEL%==0 (
  timeout /t 1 > nul
  goto waitloop
)
move /Y "{new_p}" "{cur_p}"
start "" "{cur_p}"
del "%~f0"
"""
    bat.write(bat_contents)
    bat.close()
    # lanzar el .bat y salir
    subprocess.Popen(["cmd", "/c", bat.name], creationflags=subprocess.CREATE_NEW_CONSOLE)
    sys.exit(0)

def _is_frozen_exe():
    return getattr(sys, "frozen", False)

def _current_binary_path():
    return sys.executable if _is_frozen_exe() else sys.argv[0]

def mostrar_ventana_actualizacion():
    """Muestra una ventana de progreso animada durante la actualización"""
    import threading
    
    # Crear ventana de progreso
    progress_window = tk.Tk()
    progress_window.title("PaintFlow - Actualización")
    progress_window.resizable(False, False)
    progress_window.configure(bg="#f0f0f0")
    
    # Aplicar geometría adaptativa y centrar
    centrar_ventana_adaptativa(progress_window, 400, 200)
    
    # Aplicar icono
    aplicar_icono(progress_window)
    
    # Contenedor principal
    main_frame = tk.Frame(progress_window, bg="#f0f0f0")
    main_frame.pack(fill="both", expand=True, padx=20, pady=20)
    
    # Título
    title_label = tk.Label(
        main_frame, 
        text="🔄 Actualizando PaintFlow", 
        font=("Segoe UI", 16, "bold"),
        bg="#f0f0f0",
        fg="#1976D2"
    )
    title_label.pack(pady=(0, 10))
    
    # Mensaje de estado
    status_label = tk.Label(
        main_frame,
        text="Preparando descarga...",
        font=("Segoe UI", 10),
        bg="#f0f0f0",
        fg="#333333"
    )
    status_label.pack(pady=(0, 15))
    
    # Barra de progreso
    try:
        progress_bar = ttk.Progressbar(
            main_frame,
            mode='indeterminate',
            length=300
        )
        progress_bar.pack(pady=(0, 15))
        progress_bar.start(10)  # Animación cada 10ms
    except:
        # Fallback si ttk no está disponible
        progress_label = tk.Label(
            main_frame,
            text="⏳ Descargando...",
            font=("Segoe UI", 12),
            bg="#f0f0f0",
            fg="#1976D2"
        )
        progress_label.pack(pady=(0, 15))
    
    # Texto informativo
    info_label = tk.Label(
        main_frame,
        text="Por favor espere mientras se descarga\nla nueva versión del sistema.",
        font=("Segoe UI", 9),
        bg="#f0f0f0",
        fg="#666666",
        justify="center"
    )
    info_label.pack()
    
    # Función para actualizar el estado
    def actualizar_estado(mensaje):
        status_label.config(text=mensaje)
        progress_window.update()
    
    # Función para cerrar ventana
    def cerrar_ventana():
        try:
            progress_window.destroy()
        except:
            pass
    
    # Almacenar referencias para uso externo
    progress_window.actualizar_estado = actualizar_estado
    progress_window.cerrar_ventana = cerrar_ventana
    
    return progress_window

def check_update():
    """Verifica versiones y actualiza sólo si corre como EXE en Windows.

    - Evita bloquear el arranque con timeouts. Se recomienda llamarla en hilo.
    - Omite el proceso si no es un ejecutable (útil durante desarrollo).
    """
    try:
        is_frozen = _is_frozen_exe()
        headers = {
            "User-Agent": f"LabelsApp/{APP_VERSION}",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        # Importación perezosa
        import requests  # type: ignore

        r = requests.get(URL_VERSION, timeout=5, headers=headers)
        r.raise_for_status()
        latest = r.text.strip()
        if not latest:
            return

        if is_newer(latest, APP_VERSION):
            # En desarrollo (no frozen), mostrar notificación elegante en lugar de actualizar
            if os.name == "nt" and not is_frozen:
                try:
                    # Crear ventana de notificación
                    notif_window = tk.Tk()
                    notif_window.title("PaintFlow - Nueva versión disponible")
                    notif_window.resizable(False, False)
                    notif_window.configure(bg="#f8f9fa")
                    
                    # Centrar ventana con dimensiones adaptativas
                    centrar_ventana_adaptativa(notif_window, 450, 250)
                    
                    # Aplicar icono
                    aplicar_icono(notif_window)
                    
                    main_frame = tk.Frame(notif_window, bg="#f8f9fa")
                    main_frame.pack(fill="both", expand=True, padx=30, pady=20)
                    
                    # Icono y título
                    title_frame = tk.Frame(main_frame, bg="#f8f9fa")
                    title_frame.pack(fill="x", pady=(0, 15))
                    
                    tk.Label(
                        title_frame,
                        text="🚀 Nueva versión disponible",
                        font=("Segoe UI", 14, "bold"),
                        bg="#f8f9fa",
                        fg="#28a745"
                    ).pack()
                    
                    # Información de versiones
                    info_frame = tk.Frame(main_frame, bg="#f8f9fa")
                    info_frame.pack(fill="x", pady=(0, 20))
                    
                    tk.Label(
                        info_frame,
                        text=f"Nueva versión: {latest}",
                        font=("Segoe UI", 11, "bold"),
                        bg="#f8f9fa",
                        fg="#333333"
                    ).pack(anchor="w")
                    
                    tk.Label(
                        info_frame,
                        text=f"Versión actual: {APP_VERSION}",
                        font=("Segoe UI", 10),
                        bg="#f8f9fa",
                        fg="#666666"
                    ).pack(anchor="w", pady=(5, 0))
                    
                    tk.Label(
                        info_frame,
                        text="Ejecuta el archivo EXE para actualizar automáticamente\no descarga la última versión del servidor.",
                        font=("Segoe UI", 9),
                        bg="#f8f9fa",
                        fg="#666666",
                        justify="left"
                    ).pack(anchor="w", pady=(10, 0))
                    
                    # Botón cerrar
                    btn_frame = tk.Frame(main_frame, bg="#f8f9fa")
                    btn_frame.pack(fill="x")
                    
                    tk.Button(
                        btn_frame,
                        text="Entendido",
                        font=("Segoe UI", 10),
                        bg="#007bff",
                        fg="white",
                        relief="flat",
                        padx=20,
                        pady=8,
                        command=notif_window.destroy
                    ).pack(side="right")
                    
                    notif_window.mainloop()
                except Exception:
                    # Fallback al MessageBox original
                    msg = (
                        f"Hay una nueva versión disponible: {latest}\n\n"
                        f"Versión actual: {APP_VERSION}\n\n"
                        f"Ejecuta el EXE para actualizar automáticamente o descarga la última versión."
                    )
                    ctypes.windll.user32.MessageBoxW(0, msg, "Actualización disponible", 0x40)
                return

            # Flujo normal de actualización cuando es EXE empaquetado (Windows)
            if os.name == "nt" and is_frozen:
                # Mostrar ventana de progreso
                try:
                    progress_window = mostrar_ventana_actualizacion()
                    progress_window.update()
                except Exception as e:
                    debug_log(f"⚠️ DEBUG UPDATE: No se pudo crear ventana de progreso: {e}")
                    progress_window = None
                
                base_path = os.path.dirname(_current_binary_path()) or os.getcwd()
                
                # Usar un nombre único para evitar conflictos
                timestamp = str(int(time.time()))
                new_exe = os.path.join(base_path, f"LabelsApp_new_{timestamp}.exe")
                
                debug_log(f"🔄 DEBUG UPDATE: Descargando a: {new_exe}")
                debug_log(f"🔄 DEBUG UPDATE: Directorio base: {base_path}")

                if progress_window:
                    progress_window.actualizar_estado("Verificando permisos...")

                # Verificar permisos de escritura en el directorio
                try:
                    test_file = os.path.join(base_path, "test_write_permissions.tmp")
                    with open(test_file, "w") as f:
                        f.write("test")
                    os.remove(test_file)
                    debug_log(f"🔄 DEBUG UPDATE: Permisos de escritura verificados")
                except Exception as e:
                    debug_log(f"❌ DEBUG UPDATE: Sin permisos de escritura en {base_path}: {e}")
                    if progress_window:
                        progress_window.actualizar_estado("Error: Sin permisos de escritura")
                        time.sleep(2)
                        progress_window.cerrar_ventana()
                    return

                if progress_window:
                    progress_window.actualizar_estado("Descargando nueva versión...")

                # Descargar ejecutable
                try:
                    with requests.get(URL_EXE, stream=True, timeout=20, headers=headers) as resp:
                        resp.raise_for_status()
                        total_size = int(resp.headers.get('content-length', 0))
                        downloaded = 0
                        
                        with open(new_exe, "wb") as f:
                            for chunk in resp.iter_content(8192):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    
                                    # Actualizar progreso si conocemos el tamaño total
                                    if progress_window and total_size > 0:
                                        percent = (downloaded / total_size) * 100
                                        progress_window.actualizar_estado(f"Descargando: {percent:.1f}%")
                                    elif progress_window:
                                        mb_downloaded = downloaded / (1024 * 1024)
                                        progress_window.actualizar_estado(f"Descargado: {mb_downloaded:.1f} MB")
                except Exception as e:
                    debug_log(f"❌ DEBUG UPDATE: Error en descarga: {e}")
                    if progress_window:
                        progress_window.actualizar_estado("Error en la descarga")
                        time.sleep(2)
                        progress_window.cerrar_ventana()
                    return
                
                debug_log(f"🔄 DEBUG UPDATE: Descarga completada")

                if progress_window:
                    progress_window.actualizar_estado("Verificando descarga...")

                # Verificación mínima de tamaño
                try:
                    size = os.path.getsize(new_exe)
                    debug_log(f"🔄 DEBUG UPDATE: Tamaño del archivo descargado: {size} bytes")
                    if size < 100_000:
                        debug_log(f"🔄 DEBUG UPDATE: Archivo muy pequeño, cancelando actualización")
                        if progress_window:
                            progress_window.actualizar_estado("Error: Archivo incompleto")
                            time.sleep(2)
                            progress_window.cerrar_ventana()
                        return
                except Exception as e:
                    debug_log(f"🔄 DEBUG UPDATE: Error verificando tamaño: {e}")
                    if progress_window:
                        progress_window.actualizar_estado("Error verificando descarga")
                        time.sleep(2)
                        progress_window.cerrar_ventana()
                    return

                if progress_window:
                    progress_window.actualizar_estado("Preparando instalación...")
                    time.sleep(1)
                    progress_window.actualizar_estado("Cerrando aplicación actual...")
                    time.sleep(1)
                    progress_window.cerrar_ventana()

                debug_log(f"🔄 DEBUG UPDATE: Iniciando proceso de reemplazo...")
                run_windows_updater(new_exe, _current_binary_path())
                return

            # Otros SO: permanecer en silencio
            return
    except Exception:
        # No bloquear inicio por fallas de actualización
        return

if __name__ == "__main__":
    # Verificación en segundo plano si no se pasa --no-update
    if "--no-update" not in sys.argv:
        try:
            threading.Thread(target=check_update, daemon=True).start()
        except Exception:
            pass

# === SISTEMA DE LOGIN INTEGRADO ===
class SistemaLoginIntegrado:
    """Sistema de login integrado para LabelsApp"""
    
    def __init__(self):
        self.db_config = {
            "host": "dpg-d1b18u8dl3ps73e68v1g-a.oregon-postgres.render.com",
            "port": 5432,
            "database": "labels_app_db",
            "user": "admin",
            "password": "KCFjzM4KYzSQx63ArufESIXq03EFXHz3",
            "sslmode": "require"
        }
        self.usuario_actual = None
        self.sucursal = None
        
    def conectar_bd(self):
        """Conecta a la base de datos"""
        try:
            return psycopg2.connect(**self.db_config)
        except Exception as e:
            debug_log(f"❌ Error conectando a BD: {e}")
            return None
    
    def hash_password(self, password):
        """Encripta la contraseña usando SHA-256"""
        return hashlib.sha256(password.encode()).hexdigest()
    
    def verificar_credenciales(self, username, password):
        """Verifica las credenciales del usuario"""
        try:
            from datetime import datetime
            
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    password_hash = self.hash_password(password)

                    query = """
                    SELECT u.id, u.username, u.password_hash, u.nombre_completo, u.rol, 
                           u.activo, u.sucursal_id, s.nombre as sucursal_nombre
                    FROM usuarios u
                    LEFT JOIN sucursales s ON u.sucursal_id = s.id
                    WHERE u.username = %s AND u.activo = true
                    """

                    cur.execute(query, (username,))
                    usuario = cur.fetchone()

                    if not usuario:
                        return {"error": "Usuario no encontrado o inactivo"}

                    if usuario[2] != password_hash:
                        return {"error": "Contraseña incorrecta"}

                    # Verificar que el rol sea apropiado para LabelsApp
                    roles_permitidos = ['facturador', 'cajero', 'administrador']
                    if usuario[4] not in roles_permitidos:
                        return {"error": f"Rol '{usuario[4]}' no tiene acceso a PaintFlow. Se requiere rol de cajero, facturador o administrador."}

                    # Actualizar ultimo_acceso
                    try:
                        update_query = """
                        UPDATE usuarios 
                        SET ultimo_acceso = %s, intentos_fallidos = 0, bloqueado = FALSE
                        WHERE username = %s
                        """
                        cur.execute(update_query, (datetime.now(), username))
                        conn.commit()
                    except Exception as e:
                        print(f"⚠️ Error al actualizar último acceso: {e}")
                        conn.rollback()

                    return {
                        "id": usuario[0],
                        "username": usuario[1],
                        "nombre_completo": usuario[3],
                        "rol": usuario[4],
                        "sucursal_id": usuario[6],
                        "sucursal_nombre": usuario[7] or "SUCURSAL PRINCIPAL"
                    }
        except Exception as e:
            return {"error": f"Error en verificación: {e}"}
    
    def mostrar_login(self, master=None):
        """Muestra la ventana de login.

        Si se pasa `master`, se crea como Toplevel modal para usar un único root.
        """
        if master is not None:
            ventana_login = tk.Toplevel(master)
            try:
                ventana_login.transient(master)
                ventana_login.grab_set()
            except Exception:
                pass
            try:
                ventana_login.deiconify(); ventana_login.lift(); ventana_login.focus_force()
                ventana_login.attributes('-topmost', True)
                ventana_login.after(300, lambda: ventana_login.attributes('-topmost', False))
            except Exception:
                pass
        else:
            ventana_login = tk.Tk()
        ventana_login.title("PaintFlow — Login")
        ventana_login.geometry(adaptar_geometria(600, 330))
        ventana_login.resizable(False, False)
        ventana_login.configure(bg="#f5f5f5")
        # Asegurar que la ventana aparezca visible y al frente
        try:
            ventana_login.lift()
            ventana_login.focus_force()
            ventana_login.attributes('-topmost', True)
            # Quitar topmost después de mostrar para permitir interacción normal
            ventana_login.after(600, lambda: ventana_login.attributes('-topmost', False))
        except Exception:
            pass
        # Aplicar estilo ttkbootstrap "flatly" (primary azul) en el login para bordes azules
        try:
            ttk.Style(theme="flatly")
            try:
                style = ttk.Style()
                style.configure('primary.TEntry', foreground="#1b1f23", insertcolor="#0D47A1")
                style.map('primary.TEntry', 
                          bordercolor=[('focus', '#1565C0'), ('!focus', '#1976D2')],
                          lightcolor=[('focus', '#1565C0')])
            except Exception:
                pass
        except Exception:
            try:
                ttk.Style()
            except Exception:
                pass
        
        # Cargar preferencias de login (recordar acceso: usuario + contraseña)
        saved_username = ""
        saved_password = ""
        remember_access_saved = False
        try:
            cfg_path = obtener_ruta_absoluta("paintflow_login.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    saved_username = data.get('usuario', "") or ""
                    # Back-compat: si existe cualquiera de las banderas anteriores, recordar acceso
                    remember_access_saved = bool(data.get('recordar', False) or data.get('recordar_pass', False))
                    enc_pwd = data.get('password')
                    if remember_access_saved and enc_pwd:
                        try:
                            saved_password = base64.b64decode(enc_pwd.encode('utf-8')).decode('utf-8')
                        except Exception:
                            saved_password = ""
        except Exception:
            pass
        
        # Configurar icono si existe usando ruta absoluta
        icono_path = obtener_ruta_absoluta("icono.ico")
        if os.path.exists(icono_path):
            try:
                ventana_login.iconbitmap(icono_path)
            except:
                pass
        
        # Centrar ventana con dimensiones adaptativas y asegurar visibilidad
        centrar_ventana_adaptativa(ventana_login, 600, 330)
        try:
            ventana_login.deiconify(); ventana_login.lift(); ventana_login.focus_force()
        except Exception:
            pass

        # Contenedor principal (card horizontal: logo | divisor | formulario)
        main_frame = tk.Frame(ventana_login, bg="white", relief="flat", bd=0)
        main_frame.pack(fill="both", expand=True, padx=16, pady=12)

        content_frame = tk.Frame(main_frame, bg="white")
        content_frame.pack(fill="both", expand=True, padx=8, pady=8)

        card = tk.Frame(content_frame, bg="white", bd=0, highlightthickness=0)
        card.pack(fill="both", expand=True, padx=2, pady=4)
        card.grid_columnconfigure(0, weight=0)
        card.grid_columnconfigure(1, weight=0)
        card.grid_columnconfigure(2, weight=1)
        card.grid_rowconfigure(0, weight=1)

        # Panel izquierdo con logo
        left_panel = tk.Frame(card, bg="white")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=(2, 6))

        logo_path = obtener_ruta_absoluta("logo.png")
        if os.path.exists(logo_path):
            try:
                logo_img = Image.open(logo_path)
                logo_img = logo_img.resize((200, 200), Image.Resampling.LANCZOS)
                self.logo_photo = ImageTk.PhotoImage(logo_img)
                tk.Label(left_panel, image=self.logo_photo, bg="white").pack(anchor="nw", padx=0, pady=0)
            except Exception as e:
                debug_log(f"Error cargando logo: {e}")
                tk.Label(left_panel, text="PAINTFLOW", font=("Segoe UI", 18, "bold"), fg="#1976D2", bg="white").pack(anchor="nw", padx=0, pady=0)
        else:
            tk.Label(left_panel, text="PAINTFLOW", font=("Segoe UI", 18, "bold"), fg="#1976D2", bg="white").pack(anchor="nw", padx=0, pady=0)

        # Divisor vertical
        divider = ttk.Separator(card, orient="vertical")
        divider.grid(row=0, column=1, sticky="ns", pady=8)

        # Panel derecho con formulario
        right_panel = tk.Frame(card, bg="white")
        right_panel.grid(row=0, column=2, sticky="nsew", padx=(10, 12), pady=12)

        form_frame = tk.Frame(right_panel, bg="white")
        form_frame.pack(fill="both", expand=True)

        fields_frame = tk.Frame(form_frame, bg="white")
        fields_frame.pack(anchor="w")

        tk.Label(fields_frame, text="Usuario", font=("Segoe UI", 10, "normal"), fg="#333333", bg="white").pack(anchor="w", pady=(6, 4))
        entry_usuario = ttk.Entry(fields_frame, bootstyle="primary", font=("Segoe UI", 10), width=28)
        entry_usuario.pack(anchor="w", ipady=1, pady=(0, 6))
        if remember_access_saved and saved_username:
            try:
                entry_usuario.insert(0, saved_username)
            except Exception:
                pass

        tk.Label(fields_frame, text="Contraseña", font=("Segoe UI", 10, "normal"), fg="#333333", bg="white").pack(anchor="w", pady=(0, 4))
        entry_password = ttk.Entry(fields_frame, bootstyle="primary", font=("Segoe UI", 10), show="*", width=28)
        entry_password.pack(anchor="w", ipady=1, pady=(0, 4))
        if remember_access_saved and saved_password:
            try:
                entry_password.insert(0, saved_password)
            except Exception:
                pass

        controls_frame = tk.Frame(fields_frame, bg="white")
        controls_frame.pack(anchor="w", pady=(0, 4))
        mostrar_var = tk.BooleanVar(value=False)
        recordar_acceso_var = tk.BooleanVar(value=remember_access_saved)

        def toggle_password():
            try:
                entry_password.configure(show="" if mostrar_var.get() else "*")
            except Exception:
                pass

        mostrar_row = tk.Frame(controls_frame, bg="white")
        mostrar_row.pack(fill="x")
        chk_mostrar = ttk.Checkbutton(mostrar_row, text="", variable=mostrar_var, bootstyle="primary-round-toggle", command=toggle_password)
        chk_mostrar.pack(side="left")
        tk.Label(mostrar_row, text="Mostrar contraseña", font=("Segoe UI", 9), bg="white", fg="#333333").pack(side="left", padx=(6, 0))

        remember_row = tk.Frame(controls_frame, bg="white")
        remember_row.pack(fill="x", pady=(4, 0))
        chk_recordar_inline = ttk.Checkbutton(remember_row, text="", variable=recordar_acceso_var, bootstyle="primary-round-toggle")
        chk_recordar_inline.pack(side="left")
        tk.Label(remember_row, text="Recordar usuario", font=("Segoe UI", 9), bg="white", fg="#333333").pack(side="left", padx=(6, 0))

        mensaje_frame = tk.Frame(fields_frame, bg="white")
        mensaje_frame.pack(anchor="w", pady=(0, 4))
        label_mensaje = tk.Label(mensaje_frame, text="", font=("Segoe UI", 10), bg="white", wraplength=260, justify="left")
        label_mensaje.pack()

        pb_login = ttk.Progressbar(mensaje_frame, mode="indeterminate", bootstyle="info-striped")
        pb_login.pack(anchor="w", pady=(4, 0))
        pb_login.stop()
        pb_login.pack_forget()
        
        def mostrar_mensaje(mensaje, tipo="error"):
            if tipo == "error":
                label_mensaje.configure(text=mensaje, fg="#d32f2f")
            elif tipo == "exito":
                label_mensaje.configure(text=mensaje, fg="#388e3c")
            
            tiempo = 5000 if tipo == "error" else 2000
            
            def limpiar_login():
                try:
                    label_mensaje.configure(text="")
                except:
                    pass  # Ignorar si la ventana ya se cerró
            
            ventana_login.after(tiempo, limpiar_login)
        
        def procesar_login():
            username = entry_usuario.get().strip()
            password = entry_password.get()
            
            if not username or not password:
                mostrar_mensaje("Por favor ingresa usuario y contraseña")
                return
            
            # Deshabilitar botón mientras se verifica
            try:
                btn_login.configure(state="disabled", text="Verificando…")
                # Mostrar y arrancar la barra de progreso
                try:
                    pb_login.pack(fill="x", pady=(6, 0))
                    pb_login.start(10)
                except Exception:
                    pass
                ventana_login.update_idletasks()
            except Exception:
                pass

            resultado = self.verificar_credenciales(username, password)
            
            if "error" in resultado:
                mostrar_mensaje(resultado["error"])
                try:
                    btn_login.configure(state="normal", text="INICIAR SESIÓN")
                    # Ocultar barra de progreso
                    try:
                        pb_login.stop()
                        pb_login.pack_forget()
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            
            # Login exitoso
            self.usuario_actual = resultado
            self.sucursal = resultado.get('sucursal_nombre', 'SUCURSAL PRINCIPAL')
            
            # Guardar preferencias si corresponde (recordar acceso combinado)
            try:
                cfg_path_local = obtener_ruta_absoluta("paintflow_login.json")
                if recordar_acceso_var.get():
                    try:
                        enc_pwd = base64.b64encode(password.encode('utf-8')).decode('utf-8')
                    except Exception:
                        enc_pwd = ""
                    payload = {
                        "usuario": username,
                        "recordar": True,
                        "recordar_pass": True,
                        "password": enc_pwd
                    }
                    with open(cfg_path_local, 'w', encoding='utf-8') as f:
                        json.dump(payload, f, ensure_ascii=False)
                else:
                    # Si no desea recordar nada, borrar archivo si existe
                    if os.path.exists(cfg_path_local):
                        os.remove(cfg_path_local)
            except Exception:
                pass
            
            mostrar_mensaje(f"¡Bienvenido {resultado['nombre_completo']}!", "exito")
            
            def cerrar_ventana():
                try:
                    ventana_login.destroy()
                except:
                    pass  # Ignorar si la ventana ya se cerró
            
            ventana_login.after(1500, cerrar_ventana)
            try:
                btn_login.configure(state="normal", text="INICIAR SESIÓN")
                # Ocultar barra de progreso al finalizar
                try:
                    pb_login.stop()
                    pb_login.pack_forget()
                except Exception:
                    pass
            except Exception:
                pass
        
        # Botón de iniciar sesión forzado como tk.Button para asegurar color azul claro
        # Contenedor para centrar el botón en el ancho del formulario
        btn_wrapper = tk.Frame(fields_frame, bg="white")
        btn_wrapper.pack(fill="x", pady=(10, 8))
        btn_login = tk.Button(
            btn_wrapper,
            text="INICIAR SESIÓN",
            command=procesar_login,
            width=22,
            bg="#64B5F6",
            activebackground="#42A5F5",
            fg="white",
            font=("Segoe UI", adaptar_fuente(10), "bold"),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=4,
            pady=6
        )
        btn_login.pack(anchor="center")
        # (Se elimina el uso de ttk.Style para evitar que el theme sobreescriba el color)

        # (Recordar usuario ya fue colocado debajo de "Mostrar contraseña")
        
        # (Recordar usuario ya se muestra debajo de 'Mostrar contraseña')
        
        # UX: atajos y enfoque
        try:
            entry_usuario.focus_set()
            entry_password.bind("<Return>", lambda e: procesar_login())
            ventana_login.bind("<Escape>", lambda e: ventana_login.destroy())
        except Exception:
            pass
        entry_password.bind("<Return>", lambda e: procesar_login())
        entry_usuario.bind("<Return>", lambda e: entry_password.focus())
        entry_usuario.focus()
        if master is not None:
            # Esperar cierre del login sin crear otro mainloop
            try:
                master.wait_window(ventana_login)
            except Exception:
                pass
        else:
            ventana_login.mainloop()
        
        return self.usuario_actual is not None
    
    def debug_verificar_bd(self):
        """Método de debug para verificar la base de datos"""
        # Verificación silenciosa de base de datos
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    # Verificación rápida sin mensajes
                    cur.execute("SELECT COUNT(*) FROM usuarios WHERE activo = true")
                    _ = cur.fetchone()[0]
        except Exception:
            pass  # Verificación silenciosa

# Función para ejecutar el login
def ejecutar_login(master=None):
    """Ejecuta el sistema de login y retorna la información del usuario.

    Si se pasa `master`, el login se muestra como Toplevel sobre ese root.
    """
    sistema_login = SistemaLoginIntegrado()
    ok = sistema_login.mostrar_login(master=master)
    if ok:
        return sistema_login.usuario_actual, sistema_login.sucursal
    return None, None

   # Intentar importar win32print con manejo de errores
try:
    import win32print
    import win32api
    WIN32_AVAILABLE = True
except ImportError as e:
    WIN32_AVAILABLE = False


# ============================================================================
# CACHÉ GLOBAL PARA OPTIMIZACIÓN (Evitar consultas repetidas)
# ============================================================================

# Variables globales de caché (se inicializan en startup)
_CACHE_TABLA_COLUMNAS = {}  # {tabla_name: set(columnas)}
_CACHE_CODIGO_BASE = {}     # {(base, producto, terminacion): codigo}
_CACHE_SUCURSAL_TABLAS = {}  # {sucursal: tabla_name}

def get_table_columns_cached(tabla):
    """Obtiene columnas de tabla desde caché o BD.
    Evita consultar information_schema múltiples veces.
    Impacto: -50-100ms por envío.
    """
    global _CACHE_TABLA_COLUMNAS
    
    if tabla in _CACHE_TABLA_COLUMNAS:
        return _CACHE_TABLA_COLUMNAS[tabla]
    
    # Primera vez: consultar BD y guardar en caché
    # ✅ FIX 2: Usar get_db_connection() para usar el pool
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
            """, (tabla,))
            cols = {r[0] for r in cur.fetchall()}
            cur.close()
            
            _CACHE_TABLA_COLUMNAS[tabla] = cols
            return cols
    except Exception as e:
        debug_log(f"⚠️ Error caching columnas para {tabla}: {e}")
        return set()

def precarga_codigo_base():
    """Precarga toda la tabla CodigoBase al startup.
    Se ejecuta UNA sola vez al iniciar la app.
    Impacto: -50-100ms × N búsquedas posteriores.
    """
    global _CACHE_CODIGO_BASE
    # La precarga sólo aplica si existen columnas (producto, terminacion, codigo).
    # En el esquema actual, CodigoBase guarda columnas por producto/terminación (p. ej. tath, flat, satin...),
    # por lo que esta precarga no es aplicable y debe omitirse para evitar errores.
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='codigobase'
                    """
                )
                cols = {r[0] for r in cur.fetchall()}
                if {'producto', 'terminacion', 'codigo'}.issubset(cols):
                    # Sólo si el esquema tiene esas columnas, realizar la precarga
                    cur.execute(
                        """
                        SELECT base, producto, terminacion, codigo
                        FROM CodigoBase
                        WHERE activo = TRUE
                        """
                    )
                    for base, prod, term, cod in cur.fetchall():
                        key = (
                            (base or "").lower().strip(),
                            (prod or "").lower().strip(),
                            (term or "").lower().strip()
                        )
                        _CACHE_CODIGO_BASE[key] = cod
                    debug_log(f"✅ Precargados {len(_CACHE_CODIGO_BASE)} códigos base")
                else:
                    # Omitir silenciosamente: el flujo usa _CACHE_ROW_CODIGO_BASE por base y lógica en tiempo de uso
                    debug_log("ℹ️ Precarga CodigoBase omitida: esquema sin columnas producto/terminacion/codigo")
    except Exception as e:
        debug_log(f"⚠️ Precarga CodigoBase omitida por error: {e}")

# === DB: carga productos desde ProductSW ===
def obtener_productos_desde_db():
    # ✅ FIX 2: Usar get_db_connection() para usar el pool
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            # Solo obtener productos activos
            cur.execute("SELECT codigo, nombre, base, ubicacion FROM ProductSW WHERE activo = TRUE;")
            datos = cur.fetchall()
            cur.close()
            return datos
    except Exception as e:
        return []
    

# === Consulta a la base de datos ===
def obtener_datos_por_pintura(pintura_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        p.id AS codigo,
                        p.base,
                        c.nombre AS colorante,
                        pr.tipo,
                        pr.oz,
                        pr._32s,
                        pr._64s,
                        pr._128s
                    FROM presentacion pr
                    JOIN pintura p ON pr.id_pintura = p.id
                    JOIN colorante c ON pr.id_colorante = c.id
                    WHERE p.id = %s;
                """, (pintura_id,))
                return cur.fetchall()
    except Exception as e:
        return []

def obtener_datos_por_tinte(tinte_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        t.id AS codigo,
                        t.nombre_color,
                        c.nombre AS colorante,
                        p.tipo,
                        p.cantidad
                    FROM presentacion_tintes p
                    JOIN tintes t ON p.id_tinte = t.id
                    JOIN colorantes_tintes c ON p.id_colorante_tinte = c.codigo
                    WHERE t.id = %s;
                """, (tinte_id,))
                return cur.fetchall()
    except Exception as e:
        return []




def obtener_sufijo_presentacion(presentacion, producto=None, base=None):
    """Devuelve el sufijo según presentación y producto.
    - Para "Cuarto": ciertos productos usan "QT"; el resto usa "1/4".
    - Otras presentaciones mantienen el mapeo existente.
    """
    pr = (presentacion or "").strip()
    if not pr:
        return ""

    prod = (producto or "").strip().lower()
    b = (base or "").strip().lower()

    # Resolver sufijo base por presentación
    suf_base = ""
    if pr == "Cuarto":
        # Lista de productos que usan QT
        productos_qt = [
            'esmalte multiuso', 'excello premium', 'excello voc', 'master paint',
            'super paint', 'excello pastel', 'texturizado', 'water blocking', 'kem aqua',
            'emerald', 'airpuretec', 'kem pro', 'sanitizing', 'scuff tuff',
            'armoseal tread-plex', 'armoseal 1000 hs', 'pro industrial dtm',
            'promar® 200 voc', 'promar® 400 voc', 'h&c heavy shield water-based',
            'h&c silicone acrylic', 'conversion varnish'
        ]
        alias_adicionales = [
            'excello voc', 'h&c heavy-shield', 'h&c silicone-acrylic',
            'armoseal 1000hs', 'armoseal t-p', 'scuff tuff-wb'
        ]
        usa_qt = any(k in prod for k in (productos_qt + alias_adicionales))
        suf_base = 'QT' if usa_qt else '1/4'
    elif pr == "Medio Galón":
        suf_base = "1/2"
    elif pr == "Galón":
        suf_base = "1"
    elif pr == "Cubeta":
        suf_base = "5"
    elif pr.lower() in ("4 galones", "4galones", "4 galon", "4galon"):
        # SHER-LOXANE 800 usa sufijo '4m'
        suf_base = '4m' if 'sher-loxane 800' in prod else '4'
    elif pr.lower() in ("2.38 galones", "2,38 galones", "2.38galones"):
        suf_base = '3'
    elif pr == "1/8":
        suf_base = "1/8"
    else:
        suf_base = ""

    # Sufijo complementario para laca/esmalte
    suf_extra = ""
    # Normalización y regla: solo 'extra white' o 'deep' (NO 'ultra deep' ni variantes) usan sufijo c/n
    import re
    b_low = b
    b_space = re.sub(r"\s+", " ", b_low).strip()              # collapse spaces
    b_flat = re.sub(r"[^a-z0-9]+", "", b_low)                  # remove separators
    tokens = re.findall(r"[a-z0-9]+", b_low)

    # Detectar Ultra Deep y variantes comunes: 'ultra deep', 'ultra-deep', 'ultra dep', 'ultra deep 2', 'ultradeep2', 'ud', 'ud2'
    es_ultra = (
        ('ultra' in b_low and 'deep' in b_low)
        or ('ultradeep' in b_flat)
        or ('ultradeep2' in b_flat)
        or ('ud' in tokens) or ('ud2' in tokens)
    )

    is_extra_white = ('extra white' in b_space) or ('extrawhite' in b_flat)
    is_deep = ('deep' in b_low) and not es_ultra
    es_base_c = is_extra_white or is_deep
    if 'laca' in prod:
        suf_extra = 'c' if es_base_c else 'e'
    elif 'esmalte' in prod and 'esmalte multiuso' not in prod:
        suf_extra = 'n' if es_base_c else 'e'
    # esmalte multiuso no lleva sufijo

    return f"{suf_base}{suf_extra}" if suf_base or suf_extra else ""




timer_id = None  # Variable global para almacenar el ID del temporizador actual

def limpiar_mensaje_despues(milisegundos):
    """Función centralizada para limpiar mensajes después de un tiempo"""
    global timer_id
    
    # Cancelar temporizador anterior si existe
    if timer_id is not None:
        try:
            app.after_cancel(timer_id)
        except:
            pass  # Ignorar errores si el temporizador ya no existe
    
    # Crear nuevo temporizador
    def limpiar():
        global timer_id
        aviso_var.set("")
        timer_id = None
    
    timer_id = app.after(milisegundos, limpiar)

# === Rutas y configuración ===
def obtener_ruta_absoluta(rel_path):
    """Obtiene la ruta correcta de un archivo tanto para scripts como para ejecutables"""
    try:
        # Si es un ejecutable (PyInstaller) con recursos embebidos
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # PyInstaller crea una carpeta temporal con los recursos
            base_path = sys._MEIPASS
            ruta_recurso = os.path.join(base_path, rel_path)
            if os.path.exists(ruta_recurso):
                return ruta_recurso
        
        # Si es un ejecutable sin _MEIPASS, buscar en directorio del ejecutable
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
            ruta_recurso = os.path.join(base_path, rel_path)
            if os.path.exists(ruta_recurso):
                return ruta_recurso
        
        # Si es un script de Python normal
        base_path = os.path.dirname(os.path.abspath(__file__))
        ruta_recurso = os.path.join(base_path, rel_path)
        if os.path.exists(ruta_recurso):
            return ruta_recurso
        
        # Buscar en directorio de usuario como respaldo
        user_path = os.path.expanduser("~/.etiquetas_app")
        os.makedirs(user_path, exist_ok=True)
        return os.path.join(user_path, rel_path)
        
    except Exception as e:
        # En caso de error, usar directorio de usuario
        user_path = os.path.expanduser("~/.etiquetas_app")
        os.makedirs(user_path, exist_ok=True)
        return os.path.join(user_path, rel_path)

def _nombre_mostrar_sucursal(codigo: str) -> str:
    nombres = {
        "principal": "SUCURSAL PRINCIPAL",
        "alameda": "ALAMEDA",
        "churchill": "CHURCHILL",
        "bavaro": "BÁVARO",
        "bellavista": "BELLA VISTA",
        "tiradentes": "TIRADENTES",
        "la_vega": "LA VEGA",
        "luperon": "LUPERÓN",
        "puertoplata": "PUERTO PLATA",
        "puntacana": "PUNTA CANA",
        "romana": "LA ROMANA",
        "santiago1": "SANTIAGO",
        "sanisidro": "SAN ISIDRO",
        "villamella": "VILLA MELLA",
        "terrenas": "LAS TERRENAS",
        "arroyohondo": "ARROYO HONDO",
        "bani": "BANÍ",
        "rafaelvidal": "RAFAEL VIDAL",
        "sanfrancisco": "SAN FCO. DE MACORÍS",
        "sanmartin": "SAN MARTÍN",
        "zonaoriental": "ZONA ORIENTAL",
    }
    return nombres.get(codigo or "principal", "SUCURSAL PRINCIPAL")

def _inferir_sucursal_por_usuario_guardado() -> str | None:
    """Intenta leer usuario guardado y consultar BD para su sucursal."""
    try:
        cfg_path = obtener_ruta_absoluta("paintflow_login.json")
        if not os.path.exists(cfg_path):
            return None
        with open(cfg_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            username = (data.get('usuario') or '').strip()
            if not username:
                return None
        # Consultar BD de usuarios para obtener sucursal
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COALESCE(s.nombre, 'SUCURSAL PRINCIPAL')
                        FROM usuarios u
                        LEFT JOIN sucursales s ON u.sucursal_id = s.id
                        WHERE u.username = %s AND u.activo = true
                        """,
                        (username,)
                    )
                    row = cur.fetchone()
                    return row[0].strip() if row and row[0] else None
        except Exception:
            return None
    except Exception:
        return None

def cargar_sucursal():
    """Mejor detección de sucursal para ejecución directa (sin login).

    Prioridad:
    1) Parámetro CLI (usuario_id|username|sucursal_nombre)
    2) Variable de entorno (PAINTFLOW_SUCURSAL o SUCURSAL)
    3) Archivo sucursal.txt
    4) Usuario guardado -> consulta BD para sucursal
    5) Heurística por nombre de host/usuario
    6) 'SUCURSAL PRINCIPAL'
    """
    try:
        # 1) Parámetro CLI
        if len(sys.argv) > 1:
            try:
                params = sys.argv[1].split('|')
                if len(params) >= 3 and params[2].strip():
                    s_disp = params[2].strip()
                    return s_disp
            except Exception:
                pass

        # 2) Variables de entorno
        env_suc = os.environ.get('PAINTFLOW_SUCURSAL') or os.environ.get('SUCURSAL')
        if env_suc and env_suc.strip():
            codigo = _sucursal_desde_texto(env_suc)
            return _nombre_mostrar_sucursal(codigo)

        # 3) Archivo local
        config_path = obtener_ruta_absoluta("sucursal.txt")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    s = f.read().strip()
                    if s:
                        codigo = _sucursal_desde_texto(s)
                        return _nombre_mostrar_sucursal(codigo)
            except Exception:
                pass

        # 4) Usuario guardado -> BD
        s_db = _inferir_sucursal_por_usuario_guardado()
        if s_db:
            return s_db

        # 5) Heurística por host/usuario del sistema
        host = os.environ.get('COMPUTERNAME', '')
        user = os.environ.get('USERNAME', '')
        for src in [host, user]:
            codigo = _sucursal_desde_texto(src)
            if codigo != 'principal':
                return _nombre_mostrar_sucursal(codigo)

        # 6) Fallback
        return "SUCURSAL PRINCIPAL"
    except Exception:
        return "SUCURSAL PRINCIPAL"

def obtener_icono_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "icono.ico")
    else:
        return os.path.abspath("icono.ico")

def aplicar_icono(ventana):
    """Aplica el icono a una ventana emergente de forma segura.
    
    Args:
        ventana: Widget tk.Tk o tk.Toplevel
    """
    global ICONO_PATH
    if ICONO_PATH and os.path.exists(ICONO_PATH):
        try:
            ventana.iconbitmap(ICONO_PATH)
        except Exception as e:
            debug_log(f"Aviso: No se pudo aplicar icono a ventana: {e}")



# === Configuración inicial ===
CSV_PATH = obtener_ruta_absoluta("etiquetas_guardadas.csv")
IMPRESORA_CONF_PATH = obtener_ruta_absoluta("config_impresora.txt")
PERSONALIZADOS_PATH = obtener_ruta_absoluta("productos_personalizados.csv")
LOGO_PATH = obtener_ruta_absoluta("logo.png")
SUCURSAL = cargar_sucursal()

# Variables globales para información de usuario desde login integrado
USUARIO_ID = None
USUARIO_USERNAME = None
SUCURSAL_USUARIO = None
USUARIO_ROL = None

# Variables globales para recibir info del usuario desde main_unificado.py
# Estas se leen desde __main__ si vienen de sistema unificado
USUARIO_DESDE_UNIFICADO = getattr(sys.modules.get('__main__', None), 'USUARIO_DESDE_UNIFICADO', None)
SUCURSAL_DESDE_UNIFICADO = getattr(sys.modules.get('__main__', None), 'SUCURSAL_DESDE_UNIFICADO', None)

# Si no hay variables globales, intentar leer de variables de entorno (más confiable cuando se llama desde procesos separados)
if USUARIO_DESDE_UNIFICADO is None:
    paintflow_user_id = os.environ.get('PAINTFLOW_USER_ID')
    if paintflow_user_id:  # Si existe alguna var de entorno, construir el diccionario
        USUARIO_DESDE_UNIFICADO = {
            'id': paintflow_user_id,
            'username': os.environ.get('PAINTFLOW_USERNAME', ''),
            'nombre_completo': os.environ.get('PAINTFLOW_USER_NAME', ''),
            'rol': os.environ.get('PAINTFLOW_USER_ROL', '')
        }
        SUCURSAL_DESDE_UNIFICADO = os.environ.get('PAINTFLOW_SUCURSAL')

# Crear root base y ejecutar login como Toplevel para mantener un único mainloop
app = tb.Window(themename='flatly')
try:
    app.withdraw()
except Exception:
    pass

# Ejecutar sistema de login integrado (solo si no viene de main_unificado.py)
if USUARIO_DESDE_UNIFICADO is None:
    # Login normal
    usuario_info, sucursal_info = ejecutar_login(master=app)

    if usuario_info:
        USUARIO_ID = str(usuario_info['id'])
        USUARIO_USERNAME = usuario_info['username']
        SUCURSAL_USUARIO = sucursal_info
        USUARIO_ROL = usuario_info['rol']
        # Sobreescribir SUCURSAL con la del usuario autenticado
        SUCURSAL = sucursal_info
    else:
        try:
            app.destroy()
        except Exception:
            pass
        sys.exit(1)
else:
    # Información ya provista por main_unificado.py (como diccionario)
    USUARIO_ID = str(USUARIO_DESDE_UNIFICADO.get('id', 'desde_unificado'))
    USUARIO_USERNAME = USUARIO_DESDE_UNIFICADO.get('username', 'usuario')
    SUCURSAL_USUARIO = SUCURSAL_DESDE_UNIFICADO
    USUARIO_ROL = USUARIO_DESDE_UNIFICADO.get('rol', 'facturador')
    SUCURSAL = SUCURSAL_DESDE_UNIFICADO

def cargar_productos_personalizados():
    """Carga productos personalizados desde archivo CSV local"""
    try:
        if os.path.exists(PERSONALIZADOS_PATH):
            df = pd.read_csv(PERSONALIZADOS_PATH)
            productos = []
            for _, row in df.iterrows():
                # Convertir todos los valores a string y filtrar NaN
                codigo = str(row['codigo']) if pd.notna(row['codigo']) else ""
                nombre = str(row['nombre']) if pd.notna(row.get('nombre', None)) else ""
                fecha_creacion = str(row['fecha_creacion']) if pd.notna(row.get('fecha_creacion', None)) else ""
                
                if codigo:  # Solo agregar si tiene código
                    productos.append((codigo, nombre, fecha_creacion))
            
            return productos
        return []
    except Exception as e:
        return []

# === Carga de datos ===
datos = obtener_productos_desde_db()
# Filtrar valores None, NaN y convertir a string
codigos = [str(r[0]) for r in datos if r[0] is not None]
nombres = [str(r[1]) for r in datos if r[1] is not None and str(r[1]) != 'nan']

data_por_codigo = {}
data_por_nombre = {}

for r in datos:
    if r[0] is not None and r[1] is not None and str(r[1]) != 'nan':
        codigo = str(r[0])
        nombre = str(r[1])
        base = str(r[2]) if r[2] is not None else ""
        ubicacion = str(r[3]) if r[3] is not None else ""
        
        data_por_codigo[codigo] = {"nombre": nombre, "base": base, "ubicacion": ubicacion}
        data_por_nombre[nombre] = {"codigo": codigo, "base": base, "ubicacion": ubicacion}

# Cargar productos personalizados inmediatamente y combinarlos
productos_personalizados = cargar_productos_personalizados()
for producto in productos_personalizados:
    codigo, nombre, fecha_creacion = producto
    codigo = str(codigo)
    nombre = str(nombre)
    
    if codigo not in data_por_codigo:  # Evitar duplicados
        codigos.append(codigo)
        nombres.append(nombre)
        data_por_codigo[codigo] = {"nombre": nombre, "fecha_creacion": fecha_creacion}
        data_por_nombre[nombre] = {"codigo": codigo, "base": base, "ubicacion": ubicacion}

def recargar_productos():
    """Recarga los productos activos desde la base de datos"""
    global datos, codigos, nombres, data_por_codigo, data_por_nombre
    
    datos = obtener_productos_desde_db()
    # Filtrar valores None, NaN y convertir a string
    codigos = [str(r[0]) for r in datos if r[0] is not None]
    nombres = [str(r[1]) for r in datos if r[1] is not None and str(r[1]) != 'nan']
    
    data_por_codigo = {}
    data_por_nombre = {}
    
    for r in datos:
        if r[0] is not None and r[1] is not None and str(r[1]) != 'nan':
            codigo = str(r[0])
            nombre = str(r[1])
            base = str(r[2]) if r[2] is not None else ""
            ubicacion = str(r[3]) if r[3] is not None else ""
            
            data_por_codigo[codigo] = {"nombre": nombre, "base": base, "ubicacion": ubicacion}
            data_por_nombre[nombre] = {"codigo": codigo, "base": base, "ubicacion": ubicacion}
    
    # Cargar productos personalizados y combinarlos
    productos_personalizados = cargar_productos_personalizados()
    for producto in productos_personalizados:
        codigo, nombre, base, ubicacion = producto
        codigo = str(codigo)
        nombre = str(nombre)
        
        if codigo not in data_por_codigo:  # Evitar duplicados
            codigos.append(codigo)
            nombres.append(nombre)
            data_por_codigo[codigo] = {"nombre": nombre, "base": base, "ubicacion": ubicacion}
            data_por_nombre[nombre] = {"codigo": codigo, "base": base, "ubicacion": ubicacion}
    
    # Actualizar las listas de autocompletado con filtrado seguro solo si existen
    if 'codigo_entry' in globals() and hasattr(codigo_entry, 'lista'):
        codigo_entry.lista = sorted(set([c for c in codigos if c and str(c) != 'nan']))
    if 'descripcion_entry' in globals() and hasattr(descripcion_entry, 'lista'):
        descripcion_entry.lista = sorted(set([n for n in nombres if n and str(n) != 'nan']))

def guardar_producto_personalizado(codigo, nombre):
    """Guarda un nuevo producto personalizado"""
    try:
        # Cargar datos existentes o crear DataFrame vacío
        if os.path.exists(PERSONALIZADOS_PATH):
            df = pd.read_csv(PERSONALIZADOS_PATH)
        else:
            df = pd.DataFrame(columns=['codigo', 'nombre', 'fecha_creacion'])
        
        # Verificar si el código ya existe
        if codigo in df['codigo'].values:
            return False, "El código ya existe en productos personalizados"
        
        # Agregar nuevo producto con fecha actual
        fecha_actual = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        nuevo_producto = pd.DataFrame([{
            'codigo': codigo,
            'nombre': nombre,
            'fecha_creacion': fecha_actual
        }])
        
        df = pd.concat([df, nuevo_producto], ignore_index=True)
        df.to_csv(PERSONALIZADOS_PATH, index=False)
        
        return True, "Producto personalizado guardado exitosamente"
        
    except Exception as e:
        return False, f"Error al guardar producto: {e}"

def eliminar_producto_personalizado(codigo):
    """Elimina un producto personalizado"""
    try:
        if not os.path.exists(PERSONALIZADOS_PATH):
            return False, "No hay productos personalizados"
        
        df = pd.read_csv(PERSONALIZADOS_PATH)
        if codigo not in df['codigo'].values:
            return False, "Código no encontrado en productos personalizados"
        
        df = df[df['codigo'] != codigo]
        df.to_csv(PERSONALIZADOS_PATH, index=False)
        
        return True, "Producto personalizado eliminado"
        
    except Exception as e:
        return False, f"Error al eliminar producto: {e}"

def abrir_vista_gestor():
    """Cambia a la pestaña de Cola de Espera (ahora integrada en la app)"""
    try:
        try:
            notif_gestor_var.set(False)
        except Exception:
            pass
        # Cambiar a la pestaña de cola de espera (índice 2)
        notebook.select(2)
    except Exception:
        pass
        try:
            ventana.state('zoomed')  # Windows maximizado
        except Exception:
            try:
                ventana.attributes('-fullscreen', True)  # Fallback
            except Exception:
                ventana.geometry(adaptar_geometria(1150, 680))
        # Mantener un mínimo por si se sale del modo fullscreen
        ventana.minsize(adaptar_dimension(950), adaptar_dimension(560))
        
        # Aplicar icono
        aplicar_icono(ventana)
        
        # Centrar ventana con dimensiones adaptativas
        centrar_ventana_adaptativa(ventana, 1150, 680)

        # ===== Estilos similares =====
        try:
            style = ttk.Style()
            style.configure("Modern.Treeview", rowheight=30, font=("Segoe UI", 11))
            style.configure("Treeview.Heading", font=("Segoe UI", 12, "bold"))
            style.map("Modern.Treeview", background=[("selected", "#1976D2")], foreground=[("selected", "white")])
        except Exception:
            pass

        # ===== Header =====
        header = ttk.Frame(ventana, style="Card.TFrame")
        header.pack(fill="x", padx=10, pady=8)
        ttk.Label(header, text="🔍 Vista Previa del Gestor (Listas por Factura)", font=("Segoe UI", 18, "bold"), style="Card.TLabel").pack(side="left", padx=8)

        sucursal_actual = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
        right_header = ttk.Frame(header, style="Card.TFrame")
        right_header.pack(side="right")
        ttk.Label(right_header, text=f"🏢 {sucursal_actual}", font=("Segoe UI", 13), style="Card.TLabel", bootstyle="info").pack(side="right", padx=5)
        ttk.Label(right_header, text=f"👤 {USUARIO_USERNAME}", font=("Segoe UI", 11), style="Card.TLabel", bootstyle="success").pack(side="right", padx=5)

        # ===== Barra Filtros =====
        filtros_bar = ttk.Frame(ventana, style="Card.TFrame")
        filtros_bar.pack(fill="x", padx=10, pady=4)
        # Vista simplificada: sin filtros; solo refrescar y finalizar
        # Botón para finalizar seleccionados (pendientes o en proceso)
        # Eliminado botón de finalizar: ahora se usa menú contextual con click derecho
        label_ultima = ttk.Label(filtros_bar, text="🕐 Esperando...", font=("Segoe UI", 10), style="Card.TLabel", bootstyle="secondary")
        label_ultima.pack(side="right", padx=12)
        btn_actualizar = ttk.Button(filtros_bar, text="🔄 Actualizar", bootstyle="primary")
        btn_actualizar.pack(side="left", padx=16)

        # ===== Mensaje inferior temporal =====
        label_msg = ttk.Label(ventana, text="", font=("Segoe UI", 11), style="Card.TLabel", bootstyle="info")
        label_msg.pack(pady=2)

        # ===== Tabla =====
        tree_frame = ttk.Frame(ventana, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=6)
        tree = ttk.Treeview(tree_frame, style="Modern.Treeview")
        # Mostramos solo facturas agrupadas (una fila por factura)
        columnas = ("Factura", "Items", "Operador", "En Proceso", "Finalizados", "Prioridad", "Estado")
        tree["columns"] = columnas
        tree["show"] = "headings"
        anchos = {"Factura": 120, "Items": 80, "Operador": 140, "En Proceso": 95, "Finalizados": 95, "Prioridad": 90, "Estado": 110}
        anchors = {"Factura": "center", "Items": "center", "Operador": "w", "En Proceso": "center", "Finalizados": "center", "Prioridad": "center", "Estado": "center"}
        for col in columnas:
            tree.heading(col, text=col, anchor=anchors.get(col, "w"))
            tree.column(col, width=anchos.get(col, 100), anchor=anchors.get(col, "w"))
        sv = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        sh = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=sv.set, xscrollcommand=sh.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sv.grid(row=0, column=1, sticky="ns")
        sh.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        tree.tag_configure('alta', background='#ffebee')
        tree.tag_configure('media', background='#fff3e0')
        tree.tag_configure('baja', background='#e8f5e8')
        tree.tag_configure('proceso', background='#e3f2fd')
        tree.tag_configure('finalizado', background='#f3e5f5')

        # ===== Lógica de carga =====
        def cargar_datos():
            for i in tree.get_children():
                tree.delete(i)
            inicio = time.time()
            sucursal = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
            tabla = f"pedidos_pendientes_{sucursal}"
            try:
                with get_db_connection() as conn:
                    if not conn:
                        label_ultima.configure(text="❌ Sin conexión", bootstyle="danger")
                        return
                    cur = conn.cursor()
                    # Agrupar por factura y calcular conteos por estado
                    query_base = f"""
                        SELECT id_factura,
                               COUNT(*) AS total,
                               SUM(CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado') THEN 1 ELSE 0 END) AS cnt_final,
                               SUM(CASE WHEN TRIM(COALESCE(estado,'')) = 'En Proceso' THEN 1 ELSE 0 END) AS cnt_proc,
                               MAX(CASE TRIM(COALESCE(prioridad,'')) WHEN 'Alta' THEN 3 WHEN 'Media' THEN 2 WHEN 'Baja' THEN 1 ELSE 0 END) AS pr_rank,
                               MAX(COALESCE(operador, '—')) AS operador
                        FROM {tabla}
                        WHERE TRIM(COALESCE(estado,'')) <> 'Cancelado'
                        GROUP BY id_factura
                    """
                    # Mostrar solo facturas con pendientes o en proceso (no completamente finalizadas)
                    query_base += " HAVING SUM(CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado') THEN 1 ELSE 0 END) < COUNT(*)"
                    # Ordenar por mayor prioridad primero
                    query_base += " ORDER BY pr_rank DESC, id_factura DESC LIMIT 250"

                    cur.execute(query_base)
                    rows = cur.fetchall()
                    if not rows:
                        tree.insert("", "end", values=("—", 0, "—", 0, 0, "—", f"Sin facturas en {tabla}"))
                    else:
                        for (factura, total, cnt_final, cnt_proc, pr_rank, operador) in rows:
                            # Calcular prioridad texto
                            prioridad_txt = 'Alta' if pr_rank == 3 else ('Media' if pr_rank == 2 else ('Baja' if pr_rank == 1 else '—'))
                            # Estado de la factura
                            if cnt_final == total and total > 0:
                                estado_txt = 'Finalizado'
                                tag = 'finalizado'
                            elif cnt_proc > 0:
                                estado_txt = 'En Proceso'
                                tag = 'proceso'
                            else:
                                estado_txt = 'Pendiente'
                                tag = None
                            tree.insert("", "end", values=(
                                factura or '—', int(total or 0), operador or '—', int(cnt_proc or 0), int(cnt_final or 0), prioridad_txt, estado_txt
                            ), tags=(tag,) if tag else ())
                    dur = (time.time() - inicio)
                    label_ultima.configure(text=f"✅ {len(rows)} filas • {dur:.2f}s", bootstyle="success")
                    label_msg.configure(text=f"Tabla: {tabla}")
            except Exception as e:
                label_ultima.configure(text="❌ Error", bootstyle="danger")
                tree.insert("", "end", values=("ERROR", 0, 0, 0, 0, "—", str(e)[:60]))
                debug_log(f"Error cargando vista previa gestor: {e}")

        # Track last cargar_datos() call to debounce rapid reloads
        _cargar_datos_last_time = [0]  # [timestamp]
        _cargar_datos_debounce_ms = 500  # Min 0.5s entre llamadas (evita saturación)
        
        def cargar_datos_debounced():
            """Cargar datos con debouncing para evitar spam"""
            import time
            now = time.time()
            if now - _cargar_datos_last_time[0] >= _cargar_datos_debounce_ms / 1000:
                _cargar_datos_last_time[0] = now
                cargar_datos()
        
        def refrescar_programado():
            try:
                pass  # ✅ OPTIMIZACIÓN: Recarga SOLO con NOTIFY, no automática (reduce contención)
            finally:
                ventana.after(60000, refrescar_programado)  # Fallback cada 60s por seguridad

        btn_actualizar.configure(command=cargar_datos)

        # ==== Acción: Finalizar seleccionados ====
        def finalizar_seleccionados():
            try:
                seleccion = tree.selection()
                if not seleccion:
                    messagebox.showwarning("Selección", "Selecciona una o más facturas para finalizar.")
                    return
                # Extraer facturas y estados de las filas seleccionadas
                facturas = []
                for iid in seleccion:
                    vals = tree.item(iid).get('values') or []
                    if not vals:
                        continue
                    factura = vals[0]
                    estado = vals[6] if len(vals) > 6 else ''
                    if not factura or str(factura).strip() in ('—', ''):
                        continue
                    # Permitir finalizar facturas Pendiente o En Proceso
                    if str(estado).strip() in ("Pendiente", "En Proceso", "", None):
                        facturas.append(str(factura).strip())
                if not facturas:
                    messagebox.showinfo("Información", "No hay facturas pendientes/en proceso en la selección.")
                    return

                # Confirmación breve
                if not messagebox.askyesno("Confirmar", f"¿Finalizar {len(facturas)} factura(s) seleccionada(s)?"):
                    return

                sucursal = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
                tabla = f"pedidos_pendientes_{sucursal}"

                def _do_update(facts):
                    try:
                        # Usar conexión del pool en este hilo
                        with get_db_connection() as conn_upd:
                            with conn_upd.cursor() as cur:
                                # Finalizar facturas (todos los pedidos de cada factura)
                                cur.execute(
                                    f"""
                                    UPDATE {tabla}
                                    SET estado='Finalizado', fecha_completado=%s
                                    WHERE id_factura = ANY(%s)
                                      AND (estado IS NULL OR TRIM(estado) NOT IN ('Finalizado','Completado'))
                                    """,
                                    (datetime.now(), facts)
                                )
                                afectados = cur.rowcount
                                conn_upd.commit()
                        # Refrescar y avisar en hilo principal
                        ventana.after(0, cargar_datos)
                        ventana.after(0, lambda: label_msg.configure(text=f"✅ {afectados} pedido(s) finalizado(s) en {len(facts)} factura(s)", bootstyle="success"))
                    except Exception as e:
                        ventana.after(0, lambda: messagebox.showerror("Error", f"No se pudo finalizar: {e}"))

                # Hacer en hilo para no bloquear UI
                threading.Thread(target=_do_update, args=(facturas,), daemon=True).start()
            except Exception as e:
                messagebox.showerror("Error", f"Error en finalizar seleccionados: {e}")

        # Menú contextual para acciones (Actualizar / Finalizar)
        menu_ctx = tk.Menu(ventana, tearoff=0)
        menu_ctx.add_command(label="🔄 Actualizar", command=cargar_datos)
        menu_ctx.add_command(label="✅ Finalizar Seleccionados", command=finalizar_seleccionados)

        def mostrar_menu_ctx(event):
            try:
                # Seleccionar fila bajo el cursor (permite finalizar una única factura rápido)
                rowid = tree.identify_row(event.y)
                if rowid:
                    if rowid not in tree.selection():
                        tree.selection_set(rowid)
                    tree.focus(rowid)
            except Exception:
                pass
            try:
                menu_ctx.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    menu_ctx.grab_release()
                except Exception:
                    pass

        tree.bind('<Button-3>', mostrar_menu_ctx)

        cargar_datos()
        ventana.after(15000, refrescar_programado)

        def on_close():
            try:
                ventana.destroy()
            except Exception:
                pass
        ventana.protocol("WM_DELETE_WINDOW", on_close)
    except Exception as e:
        debug_log(f"Error abriendo vista gestor (preview): {e}")
        messagebox.showerror("Error", f"No se pudo abrir vista previa del gestor:\n{e}")

def crear_interfaz_cola_espera():
    """Crea la interfaz de cola de espera dentro de tab_cola_espera"""
    # Estilos para el Treeview
    try:
        style = ttk.Style()
        style.configure("Modern.Treeview", rowheight=30, font=("Segoe UI", 11))
        style.configure("Treeview.Heading", font=("Segoe UI", 12, "bold"))
        style.map("Modern.Treeview", background=[("selected", "#1976D2")], foreground=[("selected", "white")])
    except Exception:
        pass

    # Header
    header = ttk.Frame(tab_cola_espera, style="Card.TFrame")
    header.pack(fill="x", padx=10, pady=8)
    ttk.Label(header, text="📋 Cola de Espera - Facturas en Proceso", font=("Segoe UI", 18, "bold"), style="Card.TLabel").pack(side="left", padx=8)

    sucursal_actual = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
    right_header = ttk.Frame(header, style="Card.TFrame")
    right_header.pack(side="right")
    ttk.Label(right_header, text=f"🏢 {sucursal_actual}", font=("Segoe UI", 13), style="Card.TLabel", bootstyle="info").pack(side="right", padx=5)
    ttk.Label(right_header, text=f"👤 {USUARIO_USERNAME}", font=("Segoe UI", 11), style="Card.TLabel", bootstyle="success").pack(side="right", padx=5)

    # Barra de filtros y actualización
    filtros_bar = ttk.Frame(tab_cola_espera, style="Card.TFrame")
    filtros_bar.pack(fill="x", padx=10, pady=4)
    
    label_ultima = ttk.Label(filtros_bar, text="🕐 Esperando...", font=("Segoe UI", 10), style="Card.TLabel", bootstyle="secondary")
    label_ultima.pack(side="right", padx=12)
    btn_actualizar = ttk.Button(filtros_bar, text="🔄 Actualizar", bootstyle="primary")
    btn_actualizar.pack(side="left", padx=16)

    # Mensaje inferior
    label_msg = ttk.Label(tab_cola_espera, text="", font=("Segoe UI", 11), style="Card.TLabel", bootstyle="info")
    label_msg.pack(pady=2)

    # Tabla
    tree_frame = ttk.Frame(tab_cola_espera, style="Card.TFrame")
    tree_frame.pack(fill="both", expand=True, padx=10, pady=6)
    tree = ttk.Treeview(tree_frame, style="Modern.Treeview")
    
    columnas = ("Factura", "Items", "Operador", "En Proceso", "Finalizados", "Prioridad", "Estado")
    tree["columns"] = columnas
    tree["show"] = "headings"
    anchos = {"Factura": 120, "Items": 80, "Operador": 140, "En Proceso": 95, "Finalizados": 95, "Prioridad": 90, "Estado": 110}
    anchors = {"Factura": "center", "Items": "center", "Operador": "w", "En Proceso": "center", "Finalizados": "center", "Prioridad": "center", "Estado": "center"}
    
    for col in columnas:
        tree.heading(col, text=col, anchor=anchors.get(col, "w"))
        tree.column(col, width=anchos.get(col, 100), anchor=anchors.get(col, "w"))
    
    sv = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
    sh = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=sv.set, xscrollcommand=sh.set)
    tree.grid(row=0, column=0, sticky="nsew")
    sv.grid(row=0, column=1, sticky="ns")
    sh.grid(row=1, column=0, sticky="ew")
    tree_frame.grid_rowconfigure(0, weight=1)
    tree_frame.grid_columnconfigure(0, weight=1)
    
    # Configurar colores por estado
    tree.tag_configure('alta', background='#ffebee')           # Rojo claro
    tree.tag_configure('media', background='#fff3e0')          # Naranja claro
    tree.tag_configure('baja', background='#e8f5e8')           # Verde claro
    tree.tag_configure('proceso', background='#e3f2fd')        # Azul claro (EN PROCESO)
    tree.tag_configure('finalizado', background='#f3e5f5')     # Púrpura claro (FINALIZADO)
    tree.tag_configure('cancelado', background='#efebe9')      # Gris claro (CANCELADO)

    # Función para cargar datos
    def cargar_datos():
        for i in tree.get_children():
            tree.delete(i)
        inicio = time.time()
        sucursal = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
        tabla = f"pedidos_pendientes_{sucursal}"
        try:
            with get_db_connection() as conn:
                if not conn:
                    label_ultima.configure(text="❌ Sin conexión", bootstyle="danger")
                    return
                cur = conn.cursor()
                # Agrupar por factura y calcular conteos por estado
                # ✅ NUEVO: Mostrar TODO del día EXCEPTO cancelados
                query_base = f"""
                    SELECT id_factura,
                           COUNT(*) AS total,
                           SUM(CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado') THEN 1 ELSE 0 END) AS cnt_final,
                           SUM(CASE WHEN TRIM(COALESCE(estado,'')) = 'En Proceso' THEN 1 ELSE 0 END) AS cnt_proc,
                           MAX(CASE TRIM(COALESCE(prioridad,'')) WHEN 'Alta' THEN 3 WHEN 'Media' THEN 2 WHEN 'Baja' THEN 1 ELSE 0 END) AS pr_rank,
                           MAX(COALESCE(operador, '—')) AS operador
                    FROM {tabla}
                    WHERE TRIM(COALESCE(estado,'')) NOT IN ('Cancelado', 'Finalizado', 'Completado')
                    GROUP BY id_factura
                """
                # Mostrar todas las facturas del día (incluyendo finalizadas)
                query_base += " ORDER BY pr_rank DESC, id_factura DESC LIMIT 250"

                cur.execute(query_base)
                rows = cur.fetchall()
                if not rows:
                    # Cola vacía: no mostrar nada
                    pass
                else:
                    for (factura, total, cnt_final, cnt_proc, pr_rank, operador) in rows:
                        # Calcular prioridad texto
                        prioridad_txt = 'Alta' if pr_rank == 3 else ('Media' if pr_rank == 2 else ('Baja' if pr_rank == 1 else '—'))
                        # Estado de la factura
                        if cnt_final == total and total > 0:
                            estado_txt = 'Finalizado'
                            tag = 'finalizado'
                        elif cnt_proc > 0:
                            estado_txt = 'En Proceso'
                            tag = 'proceso'
                        else:
                            estado_txt = 'Pendiente'
                            tag = None
                        tree.insert("", "end", values=(
                            factura or '—', int(total or 0), operador or '—', int(cnt_proc or 0), int(cnt_final or 0), prioridad_txt, estado_txt
                        ), tags=(tag,) if tag else ())
                
                # ✅ NUEVO: Agregar pedidos finalizados en la lista de 5 minutos (para mostrar visualmente aunque hayan desaparecido de BD)
                global _pedidos_finalizados_5min
                if _pedidos_finalizados_5min:
                    print(f"[CARGAR_DATOS] Agregando {len(_pedidos_finalizados_5min)} pedidos finalizados a la vista (5min)")
                    escribir_log(f"[CARGAR_DATOS] Agregando {len(_pedidos_finalizados_5min)} pedidos finalizados")
                    for id_pedido, info in _pedidos_finalizados_5min.items():
                        factura = info.get('factura', f'ID:{id_pedido}')
                        # Insertar como "Finalizado" con tag
                        tree.insert("", "end", values=(
                            factura, 0, '—', 0, 0, '—', 'Finalizado'
                        ), tags=('finalizado',))
                
                dur = (time.time() - inicio)
                label_ultima.configure(text=f"✅ {len(rows)} filas • {dur:.2f}s", bootstyle="success")
                label_msg.configure(text="")
        except Exception as e:
            label_ultima.configure(text="❌ Error", bootstyle="danger")
            tree.insert("", "end", values=("ERROR", 0, 0, 0, 0, "—", str(e)[:60]))
            debug_log(f"Error cargando cola de espera: {e}")


    # Menú de contexto (click derecho)
    def crear_menu_contexto(event):
        """Crea y muestra el menú contextual con debounce"""
        global _last_menu_ctx
        
        # Debounce: no ejecutar si fue hace menos de 400ms
        ahora = time.time()
        if ahora - _last_menu_ctx < 0.4:
            return
        _last_menu_ctx = ahora
        
        seleccion = tree.selection()
        if not seleccion:
            return
        
        item = tree.item(seleccion[0])
        factura = item['values'][0]
        
        # Crear menú
        menu_ctx = tk.Menu(tab_cola_espera, tearoff=False)
        menu_ctx.add_command(label="❌ Cancelar Factura", command=lambda: cancelar_factura(factura))
        menu_ctx.add_separator()
        menu_ctx.add_command(label="🔴 Prioridad Alta", command=lambda: cambiar_prioridad(factura, "Alta"))
        menu_ctx.add_command(label="🟡 Prioridad Media", command=lambda: cambiar_prioridad(factura, "Media"))
        menu_ctx.add_command(label="🟢 Prioridad Baja", command=lambda: cambiar_prioridad(factura, "Baja"))
        menu_ctx.add_separator()
        menu_ctx.add_command(label="🎨 Copiar Códigos Color", command=lambda: copiar_codigos_color(factura))
        
        # Mostrar menú
        try:
            menu_ctx.tk_popup(event.x_root, event.y_root)
        finally:
            menu_ctx.grab_release()

    # Función eliminada: marcar_como_entregado (ya no se usa - Gestor notifica cuando finaliza)
    
    def cancelar_factura(factura):
        """Cancela una factura"""
        if messagebox.askyesno("Confirmación", f"¿Deseas cancelar la factura {factura}?"):
            try:
                sucursal = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
                tabla = f"pedidos_pendientes_{sucursal}"
                with get_db_connection() as conn:
                    if not conn:
                        messagebox.showerror("Error", "No hay conexión a la base de datos")
                        return
                    cur = conn.cursor()
                    cur.execute(f"UPDATE {tabla} SET estado = 'Cancelado' WHERE id_factura = %s", (factura,))
                    conn.commit()
                    # Enviar notificación
                    cur.execute(f"NOTIFY pedidos_actualizados, '{tabla}:cancelado:{factura}'")
                    conn.commit()
                    messagebox.showinfo("Éxito", f"Factura {factura} cancelada")
                    cargar_datos()
            except Exception as e:
                messagebox.showerror("Error", f"Error al cancelar: {e}")
                debug_log(f"Error cancelando factura: {e}")

    def cambiar_prioridad(factura, prioridad):
        """Cambia la prioridad de una factura"""
        try:
            sucursal = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
            tabla = f"pedidos_pendientes_{sucursal}"
            with get_db_connection() as conn:
                if not conn:
                    messagebox.showerror("Error", "No hay conexión a la base de datos")
                    return
                cur = conn.cursor()
                cur.execute(f"UPDATE {tabla} SET prioridad = %s WHERE id_factura = %s AND estado != 'Cancelado'", (prioridad, factura))
                conn.commit()
                # Enviar notificación
                cur.execute(f"NOTIFY pedidos_actualizados, '{tabla}:prioridad:{factura}:{prioridad}'")
                conn.commit()
                messagebox.showinfo("Éxito", f"Prioridad de {factura} cambiada a {prioridad}")
                cargar_datos()
        except Exception as e:
            messagebox.showerror("Error", f"Error al cambiar prioridad: {e}")
            debug_log(f"Error cambiando prioridad: {e}")

    def copiar_codigos_color(factura):
        """Copia los códigos de color de todos los productos de una factura"""
        try:
            sucursal = obtener_sucursal_usuario(USUARIO_USERNAME) if USUARIO_USERNAME else 'principal'
            tabla = f"pedidos_pendientes_{sucursal}"
            with get_db_connection() as conn:
                if not conn:
                    messagebox.showerror("Error", "No hay conexión a la base de datos")
                    return
                cur = conn.cursor()
                # Obtener todos los códigos de esta factura
                cur.execute(f"SELECT codigo FROM {tabla} WHERE id_factura = %s ORDER BY codigo", (factura,))
                rows = cur.fetchall()
                
                if not rows:
                    messagebox.showinfo("Sin datos", f"No hay productos en la factura {factura}")
                    return
                
                # Copiar códigos al portapapeles
                codigos = "\n".join([row[0] for row in rows if row[0]])
                app.clipboard_clear()
                app.clipboard_append(codigos)
                app.update()
                
                messagebox.showinfo("Códigos Color", f"Se copiaron {len(rows)} códigos al portapapeles:\n\n{codigos[:200]}{'...' if len(codigos) > 200 else ''}")
        except Exception as e:
            messagebox.showerror("Error", f"Error al copiar códigos: {e}")
            debug_log(f"Error copiando códigos: {e}")

    # Vincular evento de click derecho
    tree.bind("<Button-3>", crear_menu_contexto)

    # ✅ FUNCIÓN GLOBAL: Detener parpadeo (disponible para llamadas desde cualquier lugar)
    def detener_parpadeo_pestaña_global():
        """Detiene el parpadeo y vuelve a gris - Versión Global"""
        global _parpadeando, _timer_parpadeo
        print("[PARPADEO] ⏹️ Deteniendo parpadeo")
        _parpadeando = False
        
        if _timer_parpadeo:
            try:
                tab_cola_espera.after_cancel(_timer_parpadeo)
            except:
                pass
            _timer_parpadeo = None
        
        try:
            tab_index = notebook.index(tab_cola_espera)
            notebook.tab(tab_index, text="🔘 Lista de Espera")
            print("[PARPADEO] ✅ Pestaña volvió a gris")
        except Exception as e:
            print(f"[PARPADEO-ERROR] {e}")

    # Sistema de LISTEN/NOTIFY para actualización en tiempo real
    def escuchar_notificaciones():
        """Escucha notificaciones de cambios en la base de datos en un hilo dedicado"""
        print("[INIT] Iniciando escuchar_notificaciones...")
        
        # Variables para indicador visual
        try:
            indicador_frame = ttk.Frame(tab_cola_espera)
            indicador_frame.pack(anchor="ne", padx=10, pady=5)
            indicador_label = ttk.Label(indicador_frame, text="●", font=("Arial", 20), foreground="gray")
            indicador_label.pack(side="left")
            print("[INIT] Indicador visual creado")
        except Exception as e:
            print(f"[ERROR] Error creando indicador: {e}")
            return
        
        def procesar_alertas_acumuladas():
            """✅ NUEVO: Procesa todos los pedidos acumulados en una sola alerta"""
            global _pedidos_pendientes_alerta, _timer_procesamiento_alertas
            
            if not _pedidos_pendientes_alerta:
                return
            
            pedidos = _pedidos_pendientes_alerta.copy()
            _pedidos_pendientes_alerta.clear()
            cantidad = len(pedidos)
            
            print(f"[ALERTAS] 📋 Procesando {cantidad} pedidos acumulados: {pedidos}")
            escribir_log(f"[ALERTAS] 📋 Procesando {cantidad} pedidos acumulados: {pedidos}")
            
            # Mostrar una única alerta para todos
            mostrar_alerta_producto(cantidad, pedidos)
            _timer_procesamiento_alertas = None
        
        def mostrar_alerta_producto(cantidad=1, pedidos_ids=None):
            """Muestra alerta visual (punto blanco) de producto(s) finalizado(s)"""
            try:
                if pedidos_ids is None:
                    pedidos_ids = []
                
                msg = f"✅✅✅ MOSTRAR_ALERTA_PRODUCTO EJECUTADO - {cantidad} producto(s) ✅✅✅"
                print(f"[ALERTA] {msg}")
                escribir_log(f"[ALERTA] {msg}")
                print(f"[ALERTA] 🔔 INICIANDO ALERTA - Pedidos: {pedidos_ids}")
                print("[ALERTA] 🔔 Cambiando indicador a BLANCO...")
                indicador_label.configure(foreground="white")
                print("[ALERTA] ✅ Indicador puesto en BLANCO")
                
                # Reproducir sonido en hilo separado
                print(f"[ALERTA] 🔊 Iniciando reproducción de sonido ({cantidad}x)...")
                for _ in range(cantidad):
                    threading.Thread(target=reproducir_sonido_sistema, daemon=True).start()
                print("[ALERTA] 🔊 Thread(s) de sonido iniciado(s)")
                
                # ✅ NUEVO: Iniciar parpadeo de pestaña (BLANCO/GRIS por 15 segundos)
                iniciar_parpadeo_pestaña()
                
            except Exception as e:
                print(f"[ALERTA-ERROR] {e}")
                import traceback
                traceback.print_exc()
        
        def iniciar_parpadeo_pestaña():
            """Inicia el parpadeo de la pestaña (blanco/gris) POR 15 SEGUNDOS solamente"""
            global _parpadeando, _timer_parpadeo
            
            # Cancelar si ya hay uno en progreso
            if _timer_parpadeo:
                tab_cola_espera.after_cancel(_timer_parpadeo)
            
            _parpadeando = True
            _estado_parpadeo = [True]  # True = mostrar blanco primero
            _tiempo_inicio_parpadeo = [time.time()]  # Marca del tiempo inicial
            print("[PARPADEO] ⚪ INICIANDO PARPADEO (15 segundos) - Comenzando en BLANCO")
            
            def parpadear():
                """Alterna entre blanco y gris durante 15 segundos"""
                global _parpadeando, _timer_parpadeo
                
                # Verificar si ya pasaron 15 segundos
                tiempo_transcurrido = time.time() - _tiempo_inicio_parpadeo[0]
                if tiempo_transcurrido > 15:
                    # Detener parpadeo automáticamente
                    return
                
                if not _parpadeando:
                    print("[PARPADEO] ⏹️ Parpadeo detenido")
                    return
                
                try:
                    tab_index = notebook.index(tab_cola_espera)
                    if _estado_parpadeo[0]:
                        # Mostrar BLANCO (⚪)
                        print("[PARPADEO] ⚪ Mostrando BLANCO")
                        notebook.tab(tab_index, text="⚪ Lista de Espera")
                        _estado_parpadeo[0] = False  # Siguiente será gris
                    else:
                        # Mostrar GRIS (🔘)
                        print("[PARPADEO] 🔘 Mostrando GRIS")
                        notebook.tab(tab_index, text="🔘 Lista de Espera")
                        _estado_parpadeo[0] = True  # Siguiente será blanco
                    
                    # Siguiente parpadeo en 500ms
                    _timer_parpadeo = tab_cola_espera.after(500, parpadear)
                except Exception as e:
                    print(f"[PARPADEO-ERROR] {e}")
            
            # Empezar el parpadeo
            parpadear()
        
        def agregar_pedido_finalizado(id_pedido, factura=None):
            """Agrega pedido a la lista de 5 minutos"""
            global _pedidos_finalizados_5min
            _pedidos_finalizados_5min[id_pedido] = {
                'timestamp': time.time(),
                'factura': factura,
                'tiempo_restante': TIEMPO_MUESTRA_FINALIZADO_MIN * 60  # segundos
            }
            escribir_log(f"[5MIN] ✅ Pedido {id_pedido} agregado a lista de 5 minutos")
        
        def limpiar_pedidos_viejos():
            """Remueve pedidos finalizados hace más de 5 minutos"""
            global _pedidos_finalizados_5min, _pedidos_sonido_reproducido
            ahora = time.time()
            tiempo_max = TIEMPO_MUESTRA_FINALIZADO_MIN * 60
            
            ids_a_remover = []
            for id_pedido, data in _pedidos_finalizados_5min.items():
                edad = ahora - data['timestamp']
                if edad > tiempo_max:
                    ids_a_remover.append(id_pedido)
            
            for id_pedido in ids_a_remover:
                del _pedidos_finalizados_5min[id_pedido]
                # ✅ NUEVO: También limpiar del set de sonidos para evitar acumulación
                _pedidos_sonido_reproducido.discard(id_pedido)
                escribir_log(f"[5MIN] ⏰ Pedido {id_pedido} removido después de 5 minutos")
            
            # Reschedule cleanup cada 10 segundos
            try:
                tab_cola_espera.after(10000, limpiar_pedidos_viejos)
            except:
                pass
        
        def thread_listener():
            """Hilo dedicado para escuchar LISTEN/NOTIFY"""
            escribir_log("[LISTENER] 🟢 thread_listener() INICIADO")
            conn_listener = None
            reintentos = 0
            max_reintentos = 5
            listen_inicializado = False  # ✅ Bandera de éxito
            
            try:
                escribir_log("[LISTENER] Iniciando thread_listener...")
                
                # Loop de reintentos para inicializar conexión dedicada (NO del pool)
                while reintentos < max_reintentos and not listen_inicializado:
                    try:
                        # ✅ CRÍTICO: Usar conexión DEDICADA para LISTEN (NO del pool)
                        import psycopg2
                        from db_pool import get_db_pool, DB_CONFIG
                        
                        # Obtener DSN del pool para usar misma configuración
                        try:
                            pool = get_db_pool()
                            tmp_conn = pool.pool.getconn()
                            try:
                                dsn = tmp_conn.dsn
                            finally:
                                pool.pool.putconn(tmp_conn)
                        except Exception:
                            dsn = None
                        
                        # Crear conexión DEDICADA (fuera del pool)
                        conn_listener = psycopg2.connect(dsn) if dsn else psycopg2.connect(
                            host=DB_CONFIG.get('host'),
                            port=DB_CONFIG.get('port'),
                            database=DB_CONFIG.get('database'),
                            user=DB_CONFIG.get('user'),
                            password=DB_CONFIG.get('password'),
                            sslmode=DB_CONFIG.get('sslmode','require')
                        )
                        print(f"[LISTENER] ✅ Conexión DEDICADA creada (NO del pool)")
                        
                        # Configurar autocommit
                        conn_listener.set_isolation_level(0)
                        print("[LISTENER] ✅ Autocommit configurado")
                        
                        # Crear cursor y ejecutar LISTEN
                        cur = conn_listener.cursor()
                        cur.execute("LISTEN pedidos_actualizados")
                        cur.close()
                        print("[LISTENER] ✅ LISTEN pedidos_actualizados ejecutado")
                        
                        # ✅ Éxito - marcar bandera y salir del loop
                        listen_inicializado = True
                        break
                        
                    except Exception as e:
                        print(f"[LISTENER-INIT-ERROR] Intento {reintentos + 1}/{max_reintentos}: {e}")
                        if conn_listener:
                            try:
                                pool.pool.putconn(conn_listener)
                            except:
                                pass
                            conn_listener = None
                        reintentos += 1
                        import time
                        time.sleep(1)
                
                if not listen_inicializado:
                    print("[LISTENER-FATAL] ❌ No se pudo inicializar LISTEN después de 5 reintentos")
                    return
                
                print("[LISTENER-OK] ✅✅✅ LISTENER CORRECTAMENTE INICIALIZADO - ESCUCHANDO NOTIFICACIONES ✅✅✅")
                
                # Iniciar limpieza automática de pedidos viejos (5 minutos)
                limpiar_pedidos_viejos()
                
                # Loop de polling
                import time
                ciclo = 0
                while True:
                    try:
                        time.sleep(0.1)
                        ciclo += 1
                        
                        # ✅ Verificar si la conexión se cerró - si es así, salir del loop para reintentar
                        if conn_listener.closed:
                            print("[LISTENER-WARN] ⚠️ Conexión cerrada - reiniciando listener...")
                            escribir_log("[LISTENER-WARN] ⚠️ Conexión cerrada - reiniciando listener...")
                            break  # Salir del loop para reintentar
                        
                        # ✅ Hacer una consulta ficticia para FORZAR que psycopg2 procese notificaciones
                        try:
                            with conn_listener.cursor() as poll_cur:
                                poll_cur.execute("SELECT 1")  # Consulta vacía para activar polling
                                poll_cur.fetchone()
                        except Exception as e:
                            # Si la conexión está cerrada, reiniciar
                            if "closed" in str(e).lower():
                                print(f"[LISTENER-POLL-ERROR] Conexión cerrada durante polling: {e}")
                                escribir_log(f"[LISTENER-POLL-ERROR] Conexión cerrada durante polling: {e}")
                                break  # Salir del loop para reintentar
                            else:
                                print(f"[LISTENER-POLL-ERROR] Error en polling: {e}")
                                escribir_log(f"[LISTENER-POLL-ERROR] Error en polling: {e}")
                        
                        # Check notificaciones
                        if conn_listener.notifies:
                            escribir_log(f"[NOTIF] ¡NOTIFICACIONES RECIBIDAS! Count: {len(conn_listener.notifies)}")
                            print(f"[NOTIF] ¡NOTIFICACIONES RECIBIDAS! Count: {len(conn_listener.notifies)}")
                            for notif in conn_listener.notifies:
                                payload = notif.payload if notif.payload else ""
                                canal = notif.channel if hasattr(notif, 'channel') else "?"
                                print(f"[NOTIF] Canal: {canal}, Payload: '{payload}', Type: {type(payload)}, len: {len(payload)}")
                                escribir_log(f"[NOTIF] Canal: {canal}, Payload: '{payload}', Type: {type(payload)}, len: {len(payload)}")
                                
                                # Detectar finalizado (desde Gestor)
                                if "finalizado" in payload.lower():
                                    print(f"[NOTIF] ✅ DETECTADO 'finalizado' en payload - Pedido finalizado en Gestor")
                                    escribir_log(f"[NOTIF] ✅ DETECTADO 'finalizado' en payload")
                                    
                                    # Extraer ID del pedido y sucursal del payload (formato: "gestor:finalizado:ID:SUCURSAL")
                                    try:
                                        partes = payload.split(':')
                                        print(f"[NOTIF] DEBUG - Partes del payload: {partes}")
                                        
                                        if len(partes) >= 3:
                                            id_pedido = partes[2].strip()
                                            sucursal_pedido = partes[3].strip() if len(partes) >= 4 else None
                                            print(f"[NOTIF] DEBUG - id_pedido extraído: '{id_pedido}', sucursal: '{sucursal_pedido}'")
                                            print(f"[NOTIF] DEBUG - SUCURSAL actual: '{SUCURSAL}', sucursal_pedido: '{sucursal_pedido}'")
                                            
                                            # ✅ NUEVO: Filtrar por sucursal - solo reproducir sonido si la sucursal coincide
                                            if sucursal_pedido and sucursal_pedido.lower() != SUCURSAL.lower():
                                                print(f"[NOTIF] 🔇 Notificación de otra sucursal ('{sucursal_pedido}' != '{SUCURSAL}') - ignorando sonido")
                                                escribir_log(f"[NOTIF] 🔇 Notificación de otra sucursal - ignorada")
                                                continue  # Saltar al siguiente NOTIFY sin reproducir sonido
                                            
                                            print(f"[NOTIF] DEBUG - Set actual de sonidos: {_pedidos_sonido_reproducido}")
                                            
                                            # ✅ NUEVO: Evitar sonido en bucle - reproducir sonido SOLO UNA VEZ
                                            if id_pedido not in _pedidos_sonido_reproducido:
                                                print(f"[NOTIF] 🔔 PRIMERA VEZ para pedido '{id_pedido}' - agregando a cola de alertas")
                                                _pedidos_sonido_reproducido.add(id_pedido)
                                                _pedidos_pendientes_alerta.append(id_pedido)
                                                print(f"[NOTIF] DEBUG - Pedidos pendientes: {_pedidos_pendientes_alerta}")
                                            else:
                                                print(f"[NOTIF] 🔕 Pedido '{id_pedido}' ya procesado - omitiendo")
                                            
                                            agregar_pedido_finalizado(id_pedido)  # Agregar a lista de 5 minutos
                                    except Exception as e:
                                        print(f"[NOTIF-ERROR] Error extrayendo id_pedido y sucursal: {e}")
                                        escribir_log(f"[NOTIF-ERROR] Error extrayendo id_pedido y sucursal: {e}")
                                        import traceback
                                        traceback.print_exc()
                                    
                                    # Calcular tiempo transcurrido desde INSERT
                                    global _tiempo_inicio_envio
                                    if _tiempo_inicio_envio:
                                        tiempo_transcurrido = (time.time() - _tiempo_inicio_envio) * 1000  # ms
                                        escribir_log(f"[TIMING] ⏱️  Tiempo desde INSERT hasta finalizado: {tiempo_transcurrido:.1f}ms")
                                        print(f"[TIMING] ⏱️  Tiempo desde INSERT hasta finalizado: {tiempo_transcurrido:.1f}ms")
                                        _tiempo_inicio_envio = None
                            
                            # ✅ NUEVO: Programar procesamiento de alertas acumuladas (300ms después)
                            global _timer_procesamiento_alertas
                            if _timer_procesamiento_alertas:
                                tab_cola_espera.after_cancel(_timer_procesamiento_alertas)
                            _timer_procesamiento_alertas = tab_cola_espera.after(300, procesar_alertas_acumuladas)
                             
                            # Recarga para mostrar nuevos pedidos en la tabla (si existe cargar_datos_debounced)
                            try:
                                tab_cola_espera.after(0, cargar_datos_debounced)
                            except:
                                pass  # Si no existe, ignorar (no es crítico)
                            
                            conn_listener.notifies.clear()
                        elif ciclo % 150 == 0:  # Log cada 150 ciclos (15 segundos)
                            print(f"[LISTENER-VIVO] Ciclo {ciclo}, esperando notificaciones...")
                            escribir_log(f"[LISTENER-VIVO] Ciclo {ciclo}, esperando notificaciones...")
                        
                    except Exception as e:
                        print(f"[LISTENER-LOOP-ERROR] {e}")
                        escribir_log(f"[LISTENER-LOOP-ERROR] {e}")
                        time.sleep(1)
                
            except Exception as e:
                print(f"[LISTENER-FATAL] {e}")
                import traceback
                traceback.print_exc()
            finally:
                # ✅ Cerrar conexión DEDICADA (no devolver al pool)
                if conn_listener:
                    try:
                        if not conn_listener.closed:
                            conn_listener.close()
                            print("[LISTENER] ✅ Conexión DEDICADA cerrada correctamente")
                    except Exception as e:
                        print(f"[LISTENER-CLEANUP] Error cerrando conexión: {e}")
        
        # Iniciar thread
        try:
            import threading
            listener_thread = threading.Thread(target=thread_listener, daemon=True, name="ListenerThread")
            listener_thread.start()
            escribir_log("[INIT] ✅ Thread listener iniciado")
        except Exception as e:
            escribir_log(f"[INIT-ERROR] {e}")
            debug_log("✅ Thread listener iniciado en LabelsApp")
        except Exception as e:
            debug_log(f"Error iniciando thread listener: {e}")
    # Iniciar listener de notificaciones
    try:
        escuchar_notificaciones()
        # ✅ NOTA: limpiar_pedidos_viejos() se inicia automáticamente dentro de escuchar_notificaciones()
    except Exception as e:
        debug_log(f"Error iniciando listener: {e}")

    def refrescar_programado():
        try:
            cargar_datos()
        except Exception:
            pass
        finally:
            try:
                tab_cola_espera.after(15000, refrescar_programado)  # cada 15s
            except Exception:
                pass

    btn_actualizar.configure(command=cargar_datos)

    # Cargar datos iniciales
    cargar_datos()
    
    # Iniciar actualización programada
    try:
        tab_cola_espera.after(15000, refrescar_programado)
    except Exception:
        pass

def crear_interfaz_personalizados():
    """Crea la interfaz de productos personalizados dentro de tab_personalizados"""
    global app  # Para acceder a las variables globales
    
    # Frame principal con padding
    main_frame = ttk.Frame(tab_personalizados, padding=20)
    main_frame.pack(fill="both", expand=True)
    
    # Logo opcional
    try:
        if LOGO_PATH and os.path.exists(LOGO_PATH):
            from PIL import Image, ImageTk
            _img = Image.open(LOGO_PATH)
            _img.thumbnail((180, 70))
            if not hasattr(app, 'logo_photo_custom'):
                app.logo_photo_custom = ImageTk.PhotoImage(_img)
            ttk.Label(main_frame, image=app.logo_photo_custom).pack(pady=(0, 10))
    except Exception:
        pass

    # Título
    ttk.Label(main_frame, text="Gestión de Productos Personalizados", 
              font=("Segoe UI", 18, "bold")).pack(pady=(10, 20))
    
    # Frame para agregar nuevo producto
    add_frame = tk.LabelFrame(main_frame, text="Agregar Nuevo Producto", padx=20, pady=15, font=("Segoe UI", 11, "bold"))
    add_frame.pack(fill="x", pady=(0, 20))
    
    # Variables para los campos
    codigo_pers_var = tk.StringVar()
    nombre_pers_var = tk.StringVar()
    
    # Campos de entrada con mejor alineación - usando frames para centrado
    codigo_label = ttk.Label(add_frame, text="Código:", font=("Segoe UI", 10))
    codigo_label.grid(row=0, column=0, sticky="w", pady=12, padx=(0, 10))
    codigo_entry = ttk.Entry(add_frame, textvariable=codigo_pers_var, width=18, font=("Segoe UI", 10))
    codigo_entry.grid(row=0, column=1, pady=12, padx=5, sticky="w")
    
    desc_label = ttk.Label(add_frame, text="Descripción:", font=("Segoe UI", 10))
    desc_label.grid(row=0, column=2, sticky="w", pady=12, padx=(25, 10))
    desc_entry = ttk.Entry(add_frame, textvariable=nombre_pers_var, width=45, font=("Segoe UI", 10))
    desc_entry.grid(row=0, column=3, pady=12, padx=5, sticky="ew")
    
    # Configurar pesos de columnas
    add_frame.columnconfigure(1, weight=0)
    add_frame.columnconfigure(3, weight=1)
    
    def agregar_producto():
        codigo = codigo_pers_var.get().strip().upper()
        nombre = nombre_pers_var.get().strip()
        
        if not all([codigo, nombre]):
            messagebox.showwarning("Campos incompletos", "Por favor complete código y descripción")
            return
        
        # Verificar si el código ya existe en la base de datos principal
        if codigo in data_por_codigo:
            messagebox.showwarning("Código existente", "Este código ya existe en la base de datos principal")
            return
        
        exito, mensaje = guardar_producto_personalizado(codigo, nombre)
        
        if exito:
            messagebox.showinfo("Éxito", mensaje)
            # Limpiar campos
            codigo_pers_var.set("")
            nombre_pers_var.set("")
            # Actualizar lista
            actualizar_lista_personalizados()
            # Recargar productos en la aplicación principal
            recargar_productos()
        else:
            messagebox.showerror("Error", mensaje)
    
    # Botón agregar al lado de descripción
    # Botón agregar alineado con los campos
    ttk.Button(add_frame, text="➕ Agregar", command=agregar_producto,
               style="BotonImprimir.TButton", width=12).grid(row=0, column=4, pady=12, padx=(20, 5), sticky="w")
    
    # Frame para lista de productos existentes
    list_frame = tk.LabelFrame(main_frame, text="Productos Personalizados Existentes", padx=12, pady=12, font=("Segoe UI", 11, "bold"))
    list_frame.pack(fill="both", expand=True, pady=(0, 10))
    
    # Treeview para mostrar productos
    columns = ("Código", "Descripción", "Fecha Creación")
    tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=14)
    
    # Ajustar el ancho de las columnas para mejor distribución
    tree.heading("Código", text="Código")
    tree.column("Código", width=100, anchor="w")
    
    tree.heading("Descripción", text="Descripción")
    tree.column("Descripción", width=250, anchor="w")
    
    tree.heading("Fecha Creación", text="Fecha Creación")
    tree.column("Fecha Creación", width=150, anchor="center")
    
    tree.pack(side="left", fill="both", expand=True)
    
    # Scrollbar para el treeview
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
    scrollbar.pack(side="right", fill="y")
    tree.configure(yscrollcommand=scrollbar.set)
    
    def actualizar_lista_personalizados():
        """Actualiza la lista de productos personalizados"""
        for item in tree.get_children():
            tree.delete(item)
        
        productos = cargar_productos_personalizados()
        for producto in productos:
            tree.insert("", "end", values=producto)
    
    def eliminar_seleccionado():
        """Elimina el producto seleccionado"""
        seleccion = tree.selection()
        if not seleccion:
            messagebox.showwarning("Sin selección", "Por favor seleccione un producto para eliminar")
            return
        
        item = tree.item(seleccion[0])
        codigo = item['values'][0]
        
        if messagebox.askyesno("Confirmar eliminación", 
                              f"¿Está seguro de eliminar el producto {codigo}?"):
            exito, mensaje = eliminar_producto_personalizado(codigo)
            
            if exito:
                messagebox.showinfo("Éxito", mensaje)
                # Limpiar campos del formulario
                codigo_pers_var.set("")
                nombre_pers_var.set("")
                # Actualizar lista
                actualizar_lista_personalizados()
                # Recargar productos en la aplicación principal
                recargar_productos()
            else:
                messagebox.showerror("Error", mensaje)
    
    def editar_seleccionado():
        """Edita el código y descripción del producto seleccionado"""
        seleccion = tree.selection()
        if not seleccion:
            messagebox.showwarning("Sin selección", "Por favor seleccione un producto para editar")
            return
        
        item = tree.item(seleccion[0])
        valores = item['values']
        codigo = valores[0]
        nombre = valores[1]
        
        # Cargar en los campos
        codigo_pers_var.set(codigo)
        nombre_pers_var.set(nombre)
        
        messagebox.showinfo("Edición", f"Producto {codigo} cargado. Modifique y presione 'Agregar' para actualizar.")
    
    def desactivar_seleccionado():
        """Marca el producto como desactivado en el CSV"""
        seleccion = tree.selection()
        if not seleccion:
            messagebox.showwarning("Sin selección", "Por favor seleccione un producto para desactivar")
            return
        
        item = tree.item(seleccion[0])
        codigo = item['values'][0]
        nombre = item['values'][1]
        
        try:
            if not os.path.exists(PERSONALIZADOS_PATH):
                messagebox.showerror("Error", "No hay productos personalizados")
                return
            
            df = pd.read_csv(PERSONALIZADOS_PATH)
            if codigo not in df['codigo'].values:
                messagebox.showerror("Error", "Código no encontrado")
                return
            
            # Agregar columna 'activo' si no existe
            if 'activo' not in df.columns:
                df['activo'] = True
            
            # Desactivar el producto
            df.loc[df['codigo'] == codigo, 'activo'] = False
            df.to_csv(PERSONALIZADOS_PATH, index=False)
            
            messagebox.showinfo("Éxito", f"Producto {codigo} desactivado")
            actualizar_lista_personalizados()
            recargar_productos()
        except Exception as e:
            messagebox.showerror("Error", f"Error al desactivar: {e}")
    
    # Menú contextual (click derecho)
    menu_ctx = tk.Menu(tree, tearoff=False)
    menu_ctx.add_command(label="✏️ Editar", command=editar_seleccionado)
    menu_ctx.add_command(label="🚫 Desactivar", command=desactivar_seleccionado)
    menu_ctx.add_separator()
    menu_ctx.add_command(label="🗑️ Eliminar", command=eliminar_seleccionado)
    
    def mostrar_menu_contextual(event):
        """Muestra el menú contextual en click derecho con debounce"""
        global _last_menu_contextual
        
        # Debounce: no ejecutar si fue hace menos de 400ms
        ahora = time.time()
        if ahora - _last_menu_contextual < 0.4:
            return
        _last_menu_contextual = ahora
        
        try:
            rowid = tree.identify_row(event.y)
            if rowid:
                tree.selection_set(rowid)
                tree.focus(rowid)
                menu_ctx.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass
        finally:
            try:
                menu_ctx.grab_release()
            except Exception:
                pass
    
    tree.bind('<Button-3>', mostrar_menu_contextual)
    
    # Cargar lista inicial
    actualizar_lista_personalizados()

def abrir_ventana_personalizados():
    """Cambia a la pestaña de productos personalizados (ahora integrada en la app)"""
    # Cambiar a la pestaña de personalizados (índice 1)
    notebook.select(1)

# === Autocomplete personalizado ===
class AutoCompleteEntry(tk.Entry):
    def __init__(self, master, lista, callback=None, next_widget=None, **kwargs):
        super().__init__(master, **kwargs)
        self.lista = sorted({str(i).strip() for i in lista if i is not None and str(i).strip()})
        self.callback = callback
        self.next_widget = next_widget  # Widget siguiente para Tab
        self.listbox = None
        self._filter_after_id = None
        self._last_on_select = 0  # Timestamp para debounce de on_select
        # Bindings seguros que no generan eventos problemáticos
        self.bind('<KeyRelease>', self.check_input)
        self.bind('<Down>', self.focus_listbox)
        self.bind('<Return>', self.select_listbox)
        self.bind('<Tab>', self.tab_complete)  # Restaurado: sin event_generate()

    def _get_matches(self, txt):
        q = (txt or '').strip().lower()
        if not q:
            return []
        # Prioriza coincidencias por prefijo, luego por contenido.
        pref = [i for i in self.lista if i.lower().startswith(q)]
        contains = [i for i in self.lista if q in i.lower() and i not in pref]
        return (pref + contains)[:50]

    def _position_listbox(self):
        if not self.listbox:
            return
        top = self.winfo_toplevel()
        x = self.winfo_rootx() - top.winfo_rootx()
        y = (self.winfo_rooty() - top.winfo_rooty()) + self.winfo_height()
        self.listbox.place(x=x, y=y, width=self.winfo_width())

    def tab_complete(self, event=None):
        """Al presionar Tab, completa con la primera opción que coincide y pasa al siguiente widget"""
        txt = self.get().lower()
        if not txt:
            if self.next_widget:
                self.next_widget.focus()
            return
        
        matches = self._get_matches(txt)
        if matches:
            # Seleccionar la primera opción que coincide
            self.delete(0, 'end')
            self.insert(0, matches[0])
            self.close_listbox()
            if self.callback:
                self.callback()
            
            # Mover al siguiente widget si existe
            if self.next_widget:
                self.next_widget.focus()
            return 'break'  # Prevenir que Tab cambie el foco
        else:
            if self.next_widget:
                self.next_widget.focus()

    def focus_and_show_matches(self):
        """Abre el listbox de sugerencias del campo actual"""
        txt = self.get().lower()
        if txt:
            matches = self._get_matches(txt)
            if matches:
                self.show_listbox(matches)

    def check_input(self, event=None):
        # Ignorar KeyRelease de Tab para evitar que la lista se vuelva a abrir
        if event and event.keysym == 'Tab':
            return

        # Debounce corto por after() para evitar perder teclas al escribir rápido.
        if self._filter_after_id:
            try:
                self.after_cancel(self._filter_after_id)
            except Exception:
                pass

        def _apply_filter():
            txt = self.get().lower()
            if not txt:
                self.close_listbox()
                return
            matches = self._get_matches(txt)
            if matches:
                self.show_listbox(matches)
            else:
                self.close_listbox()

        self._filter_after_id = self.after(60, _apply_filter)

    def show_listbox(self, matches):
        if self.listbox:
            self.listbox.destroy()

        lb = tk.Listbox(
            self.winfo_toplevel(),
            height=min(8, max(3, len(matches))),
            bg="#ffffff",
            fg="#1f2937",
            selectbackground="#1976D2",
            selectforeground="#ffffff",
            activestyle="none",
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#cbd5e1",
            highlightcolor="#1976D2"
        )
        self.listbox = lb
        self._position_listbox()
        try:
            lb.lift()
        except Exception:
            pass

        for m in matches:
            lb.insert('end', m)
        lb.bind('<<ListboxSelect>>', self.on_select)
        # Capturar click: usar after(0) para permitir que se procese la selección
        lb.bind('<Button-1>', lambda e: lb.after(0, self.on_select))
        lb.bind('<Return>', self.on_select_keyboard)
        lb.bind('<Escape>', lambda e: self.close_listbox())

        # Reposicionar el popup si cambia geometría del padre.
        self.bind('<Configure>', lambda e: self._position_listbox(), add='+')
        self.winfo_toplevel().bind('<Configure>', lambda e: self._position_listbox(), add='+')

    def on_select(self, e=None):
        # Debounce: no ejecutar si fue hace menos de 100ms
        ahora = time.time()
        if ahora - self._last_on_select < 0.1:
            return
        self._last_on_select = ahora
        
        try:
            if not self.listbox:
                return
            sel = self.listbox.get(self.listbox.curselection())
            self.delete(0, 'end')
            self.insert(0, sel)
            self.close_listbox()
            if self.callback:
                self.callback()
            self.focus_set()
        except Exception:
            pass

    def on_select_keyboard(self, e=None):
        self.on_select(e)

    def focus_listbox(self, event=None):
        if self.listbox:
            self.listbox.focus()
            if self.listbox.size() > 0:
                self.listbox.select_set(0)

    def select_listbox(self, event=None):
        if self.listbox:
            self.on_select()
            return 'break'  # Prevenir que el evento se propague
        # Si no hay listbox, permitir que el evento se propague a otros bindings

    def close_listbox(self):
        if self.listbox:
            self.listbox.destroy()
            self.listbox = None
            

# === ZPL + impresión ===
def generar_zpl(codigo, descripcion, producto, terminacion, presentacion, cantidad=1):
    w, h = 406, 203  # 2x1 pulgadas a 203 dpi
    
    # Ajuste dinámico de fuentes según longitud del contenido
    font_codigo = 70 if len(codigo) <= 6 else 70 if len(codigo) <= 8 else 30
    font_desc = 24 if len(descripcion) > 25 else 28
    
    # Construir texto del producto sin presentación
    producto_completo = '/'.join([x for x in [producto, terminacion] if x])
    font_producto = 20 if len(producto_completo) > 25 else 22 if len(producto_completo) > 20 else 26
    
    # Posiciones optimizadas - movido más a la derecha
    margin = 65  # Incrementado de 55 a 65
    y_cod = 25  # Bajado de 15 a 25
    y_desc = y_cod + font_codigo + 5  # Reducido de 8 a 5
    
    # Calcular posición de producto/terminación dinámicamente
    desc_lines = 1 if len(descripcion) <= 32 else 2
    y_producto = y_desc + (font_desc * desc_lines) + 12  # Reducido de 18 a 12
    
    # === Borde decorativo ===
    border_thickness = 2
    
    # === Sucursal lateral vertical optimizada ===
    sucursal_font_size = 16  # Reducido de 20 a 16
    x_sucursal = 18  # Movido de 8 a 18
    y_sucursal_start = 3
    
    # === Base/Ubicación en la parte inferior ===
    base = base_var.get() if base_var.get() else ""
    ubicacion = ubicacion_var.get() if ubicacion_var.get() else ""
    
    # Productos que no deben mostrar la base
    productos_sin_base = ['laca', 'industrial', 'uretano', 'esmalte kem', 'esmalte multiuso', 'monocapa']
    mostrar_base = not any(prod.lower() in producto.lower() for prod in productos_sin_base)

    # Información adicional: base y/o ubicación según producto
    if mostrar_base:
        info_adicional = f"{base} | {ubicacion}" if base and ubicacion else base or ubicacion
    else:
        # Solo mostrar ubicación para productos sin base
        info_adicional = ubicacion if ubicacion else ""

    font_info = 16
    y_info = h - 25

    zpl = (
        "^XA\n"
        "^CI28\n"  # Codificación UTF-8
        f"^PW{w}\n^LL{h}\n^LH0,0\n"
        
        # === BORDE DECORATIVO ===
        f"^FO0,0^GB{w},{border_thickness},B^FS\n"  # Borde superior
        f"^FO0,{h-border_thickness}^GB{w},{border_thickness},B^FS\n"  # Borde inferior
        f"^FO{w-border_thickness},0^GB{border_thickness},{h},B^FS\n"  # Borde derecho
        f"^FO15,0^GB{border_thickness},{h},B^FS\n"  # Borde izquierdo movido hacia la izquierda
        
        # === LÍNEA DECORATIVA SUPERIOR ===
        f"^FO15,15^GB{w-30},1,B^FS\n"  # Línea arriba del código que toca los bordes
        
        # === CÓDIGO PRINCIPAL (Destacado y centrado) ===
        f"^CF0,{font_codigo}\n"
        f"^FO{margin},{y_cod}^FB{w-margin*2-5},1,0,C,0^FD{codigo}^FS\n"
        
        # === DESCRIPCIÓN (Centrada, máximo 2 líneas) ===
        f"^CF0,{font_desc}\n"
        f"^FO{margin},{y_desc}^FB{w-margin*2-5},{desc_lines},0,C,0^FD{descripcion}^FS\n"
        
        # === PRODUCTO/TERMINACIÓN/PRESENTACIÓN (Destacado y centrado) ===
        f"^CF0,{font_producto}\n"
        f"^FO{margin-10},{y_producto}^FB{w-margin*2+15},1,0,C,0^FD{'/'.join([x.upper() for x in [producto, terminacion, presentacion] if x])}^FS\n"
    )
    
    # === INFORMACIÓN ADICIONAL (Base/Ubicación) ===
    if info_adicional:
        zpl += (
            f"^CF0,{font_info}\n"
            f"^FO{margin},{y_info}^FB{w-margin*2-5},1,0,C,0^FD{info_adicional}^FS\n"
        )
    
    # === LÍNEA SEPARADORA ENTRE PRODUCTO Y BASE ===
    y_linea_separadora = y_producto + font_producto + 5  # Subido 5 píxeles
    zpl += f"^FO{margin+20},{y_linea_separadora}^GB{w-margin*2-50},1,B^FS\n"  # Línea más pequeña
    
    # === SUCURSAL LATERAL (Rotada 90°) ===
    if SUCURSAL:
        # Calcular el centro vertical real de la etiqueta
        centro_etiqueta = h // 2  # Centro absoluto de la etiqueta (203/2 = 101.5)
        
        # Calcular la longitud del texto para centrarlo perfectamente
        longitud_texto = len(SUCURSAL) * (sucursal_font_size * 0.5)  # Ajustado de 0.6 a 0.5
        y_inicio_centrado = centro_etiqueta - (longitud_texto // 2)  # Cambiado + por -
        
        zpl += (
            f"^A0R,{sucursal_font_size},{sucursal_font_size}\n"
            f"^FO{x_sucursal},{y_inicio_centrado}^FD{SUCURSAL.upper()}^FS\n"
        )
    
    # === LÍNEA SEPARADORA DECORATIVA ===
    y_linea = y_producto + font_producto + 8
    # Esta línea ya no es necesaria porque agregamos la línea separadora arriba
    # if not info_adicional or y_linea < y_info - 10:
    #     zpl += f"^FO{margin + 10},{y_linea}^GB{w-margin*2-55},1,B^FS\n"
    
    zpl += "^XZ\n"
    
    return zpl * int(cantidad)


# === Generar PDF ===
def generar_pdf_ficha(data, filename="ficha_pintura.pdf"):
    if not data:
        return

    codigo = data[0][0]
    base = data[0][1]

    c = canvas.Canvas(filename, pagesize=landscape(A4))
    width, height = landscape(A4)

    # Título
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, height - 40, f"Fórmula - Código: {codigo}")
    c.setFont("Helvetica", 14)
    c.drawString(40, height - 65, f"Base: {base}")

    # Encabezado de tabla
    encabezado = [
        ["COLORANTE", "CUARTOS", "", "", "", "GALONES", "", "", "", "CUBETAS", "", "", ""],
        ["", "oz", "32s", "64s", "128s", "oz", "32s", "64s", "128s", "oz", "32s", "64s", "128s"]
    ]

    # Datos organizados por colorante y tipo
    filas = {}
    for _, _, colorante, tipo, oz, _32s, _64s, _128s in data:
        if colorante not in filas:
            filas[colorante] = {
                "cuarto": ["", "", "", ""],
                "galon": ["", "", "", ""],
                "cubeta": ["", "", "", ""]
            }
        filas[colorante][tipo] = [oz, _32s, _64s, _128s]

    # Cuerpo de la tabla
    cuerpo = []
    for colorante, valores in filas.items():
        fila = [colorante]
        for tipo in ["cuarto", "galon", "cubeta"]:
            for i in range(4):
                val = valores[tipo][i]
                if val is None or str(val).lower() == "nan" or (isinstance(val, float) and math.isnan(val)):
                    fila.append("")
                else:
                    try:
                        num = float(val)
                        if num.is_integer():
                            fila.append(str(int(num)))
                        else:
                            fila.append(str(num))
                    except:
                        fila.append(str(val))
        cuerpo.append(fila)

    tabla = encabezado + cuerpo

    # Estilos
    t = Table(tabla, colWidths=[80] + [40]*12)
    t.setStyle(TableStyle([
     ("BACKGROUND", (0, 0), (-1, 0), colors.darkblue),
     ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
     ("BACKGROUND", (1, 1), (4, 1), colors.orange),
     ("BACKGROUND", (5, 1), (8, 1), colors.lightblue),
     ("BACKGROUND", (9, 1), (12, 1), colors.gold),
     ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
     ("ALIGN", (0, 0), (-1, -1), "CENTER"),
     ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
     ("FONTNAME", (0, 0), (-1, 1), "Helvetica-Bold"),
 
       # Span y centrado para CUARTOS, GALONES, CUBETAS
      ("SPAN", (1, 0), (4, 0)),
      ("SPAN", (5, 0), (8, 0)),
      ("SPAN", (9, 0), (12, 0)),
      ("ALIGN", (1, 0), (12, 0), "CENTER"),
      ("VALIGN", (1, 0), (12, 0), "MIDDLE"),
    ]))


    t.wrapOn(c, width, height)
    t.drawOn(c, 40, height - 150 - 25 * len(cuerpo))

    c.save()

def generar_pdf_tinte(data, filename="ficha_tinte.pdf"):
    if not data:
        return

    codigo = data[0][0]
    nombre_color = data[0][1]

    c = canvas.Canvas(filename, pagesize=landscape(A4))
    width, height = landscape(A4)

    # Título
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, height - 40, f"Tinte - Código: {codigo}")
    c.setFont("Helvetica", 14)
    c.drawString(40, height - 65, f"Nombre del color: {nombre_color}")

    # Orden deseado de las unidades
    orden_tipos = ["1/8", "QT", "1/2", "GALON"]

    # Construimos estructura: {colorante: {tipo: cantidad}}
    estructura = defaultdict(dict)
    tipos_encontrados = set()
    for _, _, colorante, tipo, cantidad in data:
        tipos_encontrados.add(tipo)
        try:
            num = float(cantidad)
            cantidad_str = str(int(num)) if num.is_integer() else str(num)
        except:
            cantidad_str = str(cantidad)
        estructura[colorante][tipo] = cantidad_str

    # Usar solo los tipos en el orden deseado que existan en los datos
    tipos = [t for t in orden_tipos if t in tipos_encontrados]
    colorantes = sorted(estructura.keys())

    # Encabezado: COLORANTE | 1/8 | QT | 1/2 | GALON
    encabezado = ["COLORANTE"] + tipos
    cuerpo = []

    for colorante in colorantes:
        fila = [colorante]
        for tipo in tipos:
            fila.append(estructura[colorante].get(tipo, ""))
        cuerpo.append(fila)

    tabla = [encabezado] + cuerpo

    # Tamaño de columnas dinámico   
    col_widths = [130] + [80] * (len(encabezado) - 1)

    t = Table(tabla, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.darkblue),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))

    t.wrapOn(c, width, height)
    t.drawOn(c, 40, height - 150 - 25 * len(cuerpo))

    c.save()

def generar_pdf_por_cada_tinte():
    try:
        with get_db_connection() as conn:
            if not conn:
                return []
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM tintes;")
                ids = cur.fetchall()

            for (tinte_id,) in ids:
                data = obtener_datos_por_tinte(tinte_id)
                generar_pdf_tinte(data, filename=f"tinte_{tinte_id}.pdf")

    except Exception as e:
        pass


def imprimir_zebra_zpl(zpl_code):
    if not WIN32_AVAILABLE:
        messagebox.showerror("Error de impresión",
                           "Módulos de impresión no disponibles.\n"
                           "Instala pywin32: pip install pywin32")
        return

    try:
        pr = printer_var.get()
        guardar_impresora(pr)

        # Verificar lista de impresoras disponibles
        try:
            available = [p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
        except Exception:
            available = []

        if not pr:
            messagebox.showwarning("Impresora no seleccionada", "Por favor selecciona una impresora antes de imprimir.")
            return

        if available and pr not in available:
            messagebox.showwarning("Impresora no encontrada", f"La impresora seleccionada ('{pr}') no está entre las impresoras detectadas.\nLista detectada: {available}")

        # Intentar enviar ZPL en RAW
        h = win32print.OpenPrinter(pr)
        try:
            win32print.StartDocPrinter(h, 1, ("Etiqueta", None, "RAW"))
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, zpl_code.encode())
            win32print.EndPagePrinter(h)
            win32print.EndDocPrinter(h)
        finally:
            try:
                win32print.ClosePrinter(h)
            except:
                pass
    except Exception as e:
        # Mostrar error detallado y ofrecer escribir ZPL a archivo como fallback
        try:
            # Guardar ZPL en archivo temporal para envío manual
            import tempfile
            tmp = tempfile.mktemp(suffix='.zpl')
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(zpl_code)
            messagebox.showerror("Error impresión", f"No se pudo imprimir:\n{e}\nSe guardó el ZPL en: {tmp}")
        except Exception as e2:
            messagebox.showerror("Error impresión", f"No se pudo imprimir:\n{e}\nAdemás, no se pudo crear archivo de fallback: {e2}")

def guardar_impresora(nombre):
    try:
        with open(IMPRESORA_CONF_PATH,'w',encoding='utf-8') as f:
            f.write(nombre)
    except: pass

def cargar_impresora_guardada():
    if not WIN32_AVAILABLE:
        return ''
        
    if os.path.exists(IMPRESORA_CONF_PATH):
        try:
            n = open(IMPRESORA_CONF_PATH,'r',encoding='utf-8').read().strip()
            printers = [p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL|win32print.PRINTER_ENUM_CONNECTIONS)]
            if n in printers:
                return n
        except: pass
    return ''

def imprimir_ficha_pintura(codigo_pintura):
    """Genera e imprime la ficha PDF de una pintura o tinte según el producto seleccionado"""
    producto = producto_var.get().strip().lower()

    try:
        # Productos que deben usar ficha de pintura (tabla pintura/presentacion)
        productos_con_ficha_pintura = [
            'esmalte multiuso', 'excello premium', 'excello voc', 'master paint',
            'super paint', 'excello pastel', 'texturizado', 'water blocking', 'kem aqua', 'emerald',
            'airpuretec', 'kem pro', 'sanitizing', 'scuff tuff', 'armoseal tread-plex',
            'armoseal 1000 hs', 'pro industrial dtm', 'promar® 200 voc', 'promar® 400 voc',
            'h&c heavy shield water-based', 'h&c silicone acrylic'
        ]
        # Alias comunes y variantes de nombres
        alias_productos = [
            'armoseal 1000hs', 'armoseal t-p', 'scuff tuff-wb', 'h&c heavy-shield', 'h&c silicone-acrylic',
            'promar 200 voc', 'promar 400 voc', 'h&c heavy shield', 'airpurtec'
        ]

        claves = productos_con_ficha_pintura + alias_productos

        if any(key in producto for key in claves):
            datos = obtener_datos_por_pintura(codigo_pintura)
            if datos:
                temp_pdf = tempfile.mktemp(".pdf")
                generar_pdf_ficha(datos, temp_pdf)
                os.startfile(temp_pdf)
            else:
                messagebox.showwarning("No encontrado", f"No hay fórmula disponible para el producto: {producto}")

        elif producto == "tinte al thinner":
            datos = obtener_datos_por_tinte(codigo_pintura)
            if datos:
                temp_pdf = tempfile.mktemp(".pdf")
                generar_pdf_tinte(datos, temp_pdf)
                os.startfile(temp_pdf)
            else:
                messagebox.showwarning("No encontrado", f"No hay fórmula disponible para el producto: {producto}")

        else:
            messagebox.showwarning("Producto no soportado", f"Producto no reconocido: {producto}")

    except Exception as e:
        messagebox.showerror("Error", f"Ocurrió un error al imprimir: {e}")

def on_btn_imprimir_click():
    codigo = codigo_entry.get()
    if codigo:
        imprimir_ficha_pintura(codigo)
    else:
        messagebox.showinfo("Campo vacío", "Por favor ingrese un código de pintura.")


# === UI ===
# Reutilizar el root existente creado antes del login (app)
app.title(f"PaintFlow {APP_VERSION} - {SUCURSAL}")
# Usar tamaño adaptativo que funcione en cualquier resolución
# Para 1366x768 usa ~1200x680, para 1920x1080 usa ~1700x950, etc.
try:
    pantalla_ancho = app.winfo_screenwidth()
    pantalla_alto = app.winfo_screenheight()
    # Usar 85% de la pantalla disponible, pero mínimo 1200x680
    ancho_ventana = max(int(pantalla_ancho * 0.85), 1200)
    alto_ventana = max(int(pantalla_alto * 0.85), 680)
    app.geometry(f"{ancho_ventana}x{alto_ventana}")
except Exception:
    # Fallback si hay error obteniendo dimensiones
    app.geometry("1200x680")
app.state('zoomed')  # Maximizar ventana en Windows
app.resizable(True, True)

# Configurar icono de la aplicación
ICONO_PATH = obtener_icono_path()
try:
    app.iconbitmap(ICONO_PATH)
except Exception as e:
    pass
    
aviso_var = tk.StringVar()

printer_var = tk.StringVar(value=cargar_impresora_guardada())


# Obtener lista de impresoras con manejo de errores
if WIN32_AVAILABLE:
    try:
        printers = [p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
    except Exception as e:
        printers = []
else:
    printers = ["Sin impresoras disponibles (instalar pywin32)"]

if not printers:
    messagebox.showwarning("Sin impresoras", "No se detectaron impresoras")

# Variables

descripcion_var = tk.StringVar()
producto_var = tk.StringVar()
terminacion_var = tk.StringVar()
presentacion_var = tk.StringVar()
spin = tk.IntVar(value=1)
base_var = tk.StringVar()
ubicacion_var = tk.StringVar()
codigo_base_var = tk.StringVar()

# Lista temporal de productos para factura múltiple
lista_productos_factura = []

# Actualiza la vista previa al cambiar producto o terminación
producto_var.trace_add('write', lambda *args: actualizar_vista())
terminacion_var.trace_add('write', lambda *args: actualizar_vista())

# Diccionario de terminaciones válidas por producto
# Mapeo de Fórmulas y Presentaciones por Producto
FORMULAS_Y_PRESENTACIONES_POR_PRODUCTO = {
    # Productos CCE/BAC existentes
    'airpuretec': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'armoseal t-p': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'armoseal 1000hs': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'emerald': {'formulas': ['CCE'], 'presentaciones': ['Cuarto', 'Galón']},
    'esmalte multiuso': {'formulas': ['BAC'], 'presentaciones': ['Cuarto', 'Galón']},
    'texturizado': {'formulas': ['BAC'], 'presentaciones': ['Cubeta']},
    'excello pastel': {'formulas': ['BAC'], 'presentaciones': ['Cuarto', 'Galón', 'Cubeta']},
    'excello premium': {'formulas': ['BAC'], 'presentaciones': ['Cuarto', 'Galón', 'Cubeta']},
    'excello voc': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'h&c heavy-shield': {'formulas': ['CCE'], 'presentaciones': ['Galón']},
    'h&c silicone-acrylic': {'formulas': ['CCE'], 'presentaciones': ['Galón']},
    'industrial': {'formulas': ['CCE'], 'presentaciones': ['Galón']},
    'kem aqua': {'formulas': ['CCE'], 'presentaciones': ['Cuarto', 'Galón', 'Cubeta']},
    'kem pro': {'formulas': ['BAC'], 'presentaciones': ['Galón', 'Cubeta']},
    'master paint': {'formulas': ['BAC'], 'presentaciones': ['Galón', 'Cubeta']},
    'pro industrial dtm': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'promar® 200 voc': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'promar® 400 voc': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'sanitizing': {'formulas': ['CCE'], 'presentaciones': ['Galón']},
    'scuff tuff-wb': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'super paint': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'tile clad': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'water blocking': {'formulas': ['CCE'], 'presentaciones': ['Galón', 'Cubeta']},
    'water-base catalyzed': {'formulas': ['CCE'], 'presentaciones': ['Galón']},
    'laca': {'formulas': ['BAC'], 'presentaciones': ['Galón']},
    'esmalte kem': {'formulas': ['BAC'], 'presentaciones': ['Cuarto', 'Galón']},
    'monocapa': {'formulas': ['BAC'], 'presentaciones': ['Galón', 'Cubeta']},
    'uretano': {'formulas': ['BAC'], 'presentaciones': ['Galón', 'Cubeta']},
    
    # Productos GIS/MAXITONER (sin fórmula actualmente)
    'acrolon 218': {'presentaciones': ['Galón', 'Cubeta']},
    'acrolon 7300': {'presentaciones': ['Galón', 'Cubeta']},
    'armorseal hs-pt': {'presentaciones': ['Galón', 'Cubeta']},
    'armorseal rexthane': {'presentaciones': ['Galón']},
    'dura-plate 235': {'presentaciones': ['Galón']},
    'dura-plate 235 pw': {'presentaciones': ['Galón']},
    'hi-solids 250': {'presentaciones': ['Galón', 'Cubeta']},
    'hi-solids pt': {'presentaciones': ['Galón', 'Cubeta']},
    'macropoxy 4600': {'presentaciones': ['Galón', 'Cubeta']},
    'macropoxy 646': {'presentaciones': ['Galón', 'Cubeta']},
    'sher-loxane 800': {'presentaciones': ['Galón', 'Cubeta']},
    'sherplate 600': {'presentaciones': ['Galón']},
    'urethane alkyd': {'presentaciones': ['Galón']},
}


TERMINACIONES_POR_PRODUCTO = {
    'laca': ['Mate', 'Semimate', 'Brillo'],
    'industrial': ['Mate', 'Semimate', 'Brillo'],
    'esmalte multiuso': ['Mate', 'Satin', 'Gloss'],
    'excello premium': ['Mate', 'Satin', 'Semigloss', 'Semisatin'],
    'excello voc': ['Mate', 'Satin'],
    'master paint': ['Mate'],
    'tinte al thinner': ['Claro', 'Intermedio', 'Especial'],
    'super paint': ['Mate', 'Satin', 'Gloss'],
    'esmalte kem': ['Mate', 'Semimate', 'Brillo'],
    'excello pastel': ['Mate'],
    'texturizado': ['Mate'],
    'water blocking': ['Mate'],
    'kem aqua': ['Satin'],
    'emerald': ['Satin', 'SemiGloss'],
    'monocapa': ['Mate', 'Semimate', 'Brillo'],
    'uretano': ['Mate', 'Semimate', 'Brillo'],
    'airpuretec': ['Mate', 'Satin'],
    'kem pro': ['Mate'],
    'sanitizing': ['Satin'],
    'scuff tuff-wb' : ['Mate', 'Satin',],
    'armoseal t-p' : ['Semigloss'],
    'armoseal 1000hs' : ['Gloss'],
    'pro industrial dtm' : ['Gloss'],
    'promar® 400 voc' : ['Satin'],
    'h&c heavy-shield' : ['Gloss'],
    'h&c silicone-acrylic' : ['Mate'],
    'promar® 200 voc' : ['Satin', 'Mate'],
    # Nuevos productos industriales
    'macropoxy 646': ['Semigloss'],
    'tile clad': ['Gloss', 'Brillo'],
    'sher-loxane 800': ['Gloss', 'Brillo'],
    'acrolon 7300': ['Gloss', 'Brillo'],
    'dura-plate 235 pw': ['Gloss', 'Brillo'],
    'macropoxy 4600': ['Semigloss'],
    'sherplate 600': ['Gloss', 'Brillo'],
    'acrolon 218': ['Gloss', 'Brillo', 'Semigloss'],
    'armorseal hs-pt': ['Gloss', 'Brillo'],
    'hi-solids pt': ['Gloss', 'Brillo'],
    'hi-solids 250': ['Gloss', 'Brillo'],
    'armorseal rexthane': ['Gloss', 'Brillo'],
    'water-base catalyzed': ['Gloss', 'Brillo'],

    
    
}


# ============================================================================
# SUCURSALES EXCLUIDAS DEL BLOQUEO DE QT (pigmento 128)
# En estas sucursales SI se permite Cuarto/QT para productos con pigmento 128
# ============================================================================
SUCURSALES_EXCLUIDAS_BLOQUEO_QT = {
    'rafaelvidal', 'churchill', 'bellavista', 'puntacana', 
    'bavaro', 'romana', 'tiradentes'
}


def actualizar_terminaciones(*args):
    """Actualiza las terminaciones disponibles según el producto seleccionado"""
    producto = producto_var.get().lower()
    base = base_var.get().lower()
    
    # Buscar terminaciones válidas para el producto
    terminaciones_validas = []
    for key, terminaciones in TERMINACIONES_POR_PRODUCTO.items():
        if key in producto:
            terminaciones_validas = terminaciones
            break
    
    # Si no se encuentra el producto, usar todas las terminaciones
    if not terminaciones_validas:
        terminaciones_validas = ['Mate', 'Satin', 'Semigloss', 'Semimate', 'Gloss', 'Brillo', 
                                "N/A", "ESPECIAL", "CLARO", "INTERMEDIO", "MADERA", "PERLADO", "METALICO", "SEMISATIN"]
    
    # Lógica especial para Excello Premium con bases Ultra Deep / Ultra Deep II
    if 'excello premium' in producto:
        # Detectar Ultra Deep y Ultra Deep II (distintas variantes)
        es_ultra_deep_ii = any(k in base for k in ['ultra deep ii', 'ultradeep ii', 'ultra-deep ii', 'ultra deep 2'])
        es_ultra_deep = ('ultra deep' in base)
        if es_ultra_deep or es_ultra_deep_ii:
            # Solo permitir Semisatin para ambas bases
            terminaciones_validas = ['Semisatin']

    # Actualizar el combobox
    terminaciones_combobox['values'] = terminaciones_validas
    
    # Limpiar la selección actual si no es válida
    terminacion_actual = terminacion_var.get()
    if terminacion_actual and terminacion_actual not in terminaciones_validas:
        terminacion_var.set('')
    
    # Si solo hay una terminación válida, seleccionarla automáticamente
    if len(terminaciones_validas) == 1:
        terminacion_var.set(terminaciones_validas[0])
    
    # Actualizar vista previa
    actualizar_vista()


def _producto_tiene_pigmento_128(producto_codigo: str) -> bool:
    """
    Verifica si un producto tiene el pigmento SW 7009 (128) en su formula de galon.
    Solo se bloquea QT si el producto contiene este pigmento.
    
    Args:
        producto_codigo: Codigo del producto (ej: "SW 9001", "SW-6165")
        
    Returns:
        True si contiene pigmento 128, False en caso contrario
    """
    try:
        # Normalizar el codigo (intentar ambos formatos)
        codigo_space = producto_codigo.replace('-', ' ').strip().upper()
        codigo_dash = producto_codigo.replace(' ', '-').strip().upper()
        
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Buscar en tabla presentacion para galon
                cur.execute("""
                    SELECT COALESCE(c.nombre, p.id_colorante::text) AS colorante
                    FROM presentacion p
                    LEFT JOIN colorante c ON p.id_colorante = c.id
                    WHERE (UPPER(p.id_pintura) = %s OR UPPER(p.id_pintura) = %s)
                      AND p.tipo = 'galon'
                """, (codigo_space, codigo_dash))
                
                colorantes = [row[0] for row in cur.fetchall()]
                
                # Verificar si alguno contiene "SW 7009" o "128"
                for colorante in colorantes:
                    if colorante:
                        colorante_norm = colorante.strip().upper()
                        # Buscar coincidencias
                        if ('SW 7009' in colorante_norm or 
                            'SW-7009' in colorante_norm or
                            'SW7009' in colorante_norm or
                            colorante_norm == '128'):
                            return True
                
                return False
    except Exception as e:
        debug_log(f"Error verificando pigmento 128: {e}")
        return False


def actualizar_presentaciones(*args):
    """Actualiza las presentaciones disponibles según el producto seleccionado"""
    producto = producto_var.get().lower()
    
    # Buscar en el diccionario de fórmulas y presentaciones
    presentaciones_disponibles = []
    for prod_key, config in FORMULAS_Y_PRESENTACIONES_POR_PRODUCTO.items():
        if prod_key in producto:
            presentaciones_disponibles = config.get('presentaciones', [])
            break
    
    # Si no se encuentra en el diccionario, usar las reglas por defecto
    if not presentaciones_disponibles:
        # Presentaciones por defecto (incluye Medio Galón)
        presentaciones_disponibles = ['Cuarto', 'Medio Galón', 'Galón', 'Cubeta']
        
        # Si es laca, industrial, monocapa o uretano, agregar octavos (1/8)
        if any(palabra in producto for palabra in ['laca', 'industrial', 'monocapa', 'uretano']):
            presentaciones_disponibles = ['1/8', 'Cuarto', 'Medio Galón', 'Galón']

        # Regla solicitada: para Esmalte Kem quitar 'Cubeta' y permitir '1/8'
        if 'esmalte kem' in producto:
            presentaciones_disponibles = ['1/8', 'Cuarto', 'Medio Galón', 'Galón']

        # Regla: para Tinte al Thinner quitar 'Cubeta' y permitir '1/8'
        if 'tinte al thinner' in producto:
            presentaciones_disponibles = ['1/8', 'Cuarto', 'Medio Galón', 'Galón']

        # Regla para todos los productos Excello: quitar Medio Galón
        if 'excello' in producto:
            presentaciones_disponibles = ['Cuarto', 'Galón', 'Cubeta']
        
        # NOTA: Nuevos productos GIS/MAXITONER usan el diccionario FORMULAS_Y_PRESENTACIONES_POR_PRODUCTO
    
    # Actualizar el combobox
    presentacion_combobox['values'] = presentaciones_disponibles
    
    # Limpiar la selección actual si no es válida
    presentacion_actual = presentacion_var.get()
    if presentacion_actual and presentacion_actual not in presentaciones_disponibles:
        presentacion_var.set('')


# Actualiza la vista previa al cambiar producto o terminación
producto_var.trace_add('write', actualizar_terminaciones)
producto_var.trace_add('write', actualizar_presentaciones)
terminacion_var.trace_add('write', lambda *args: actualizar_vista())
presentacion_var.trace_add('write', lambda *args: actualizar_vista())
# Agregar listener para cuando cambie la base (importante para Excello Premium)
base_var.trace_add('write', actualizar_terminaciones)

def es_producto_texturizado(producto):
    """Detecta variantes conocidas de Texturizado."""
    p = (producto or '').strip().lower()
    return any(alias in p for alias in [
        'texturizado', 'exc. texturizado', 'exc texturizado',
        'exc. texdturizado', 'exc texdturizado'
    ])

# Sugerencia dinámica de Código Base (sin copiar al portapapeles)
def actualizar_codigo_base_sugerido(*args):
    try:
        base = (base_var.get() or '').strip()
        producto = (producto_var.get() or '').strip()
        terminacion = (terminacion_var.get() or '').strip()
        presentacion = (presentacion_var.get() or '').strip()

        # Requisitos mínimos
        requiere_terminacion = not es_producto_texturizado(producto)
        if not base or not producto or (requiere_terminacion and not terminacion):
            codigo_base_var.set("")
            return

        resultado = obtener_codigo_base(base, producto, terminacion or "")
        if not resultado or resultado in ("No encontrado", "No Aplica", "Error"):
            codigo_base_var.set("")
            return

        if presentacion:
            suf = obtener_sufijo_presentacion(presentacion, producto, base)
            if suf:
                resultado = f"{resultado}{suf}"

        codigo_base_var.set(resultado)
    except Exception:
        # En caso de error no bloquear la UI
        codigo_base_var.set("")

# Enlazar cambios para mantener el código sugerido actualizado
producto_var.trace_add('write', actualizar_codigo_base_sugerido)
terminacion_var.trace_add('write', actualizar_codigo_base_sugerido)
presentacion_var.trace_add('write', actualizar_codigo_base_sugerido)
base_var.trace_add('write', actualizar_codigo_base_sugerido)

# Layout
labels = ["Código", "Descripción", "Producto", "Terminación", "Presentación", "Ubicación", "Base", "Codigo Base", "Cantidad"]

# ============================================================================
# CREAR NOTEBOOK CON 2 PESTAÑAS
# ============================================================================

# Crear estilos personalizados para las pestañas
style = ttk.Style()

# Estilo normal (gris) - para las pestañas inactivas
style.configure('Normal.TNotebook.Tab', foreground='black')

# Estilo activo (AZUL con indicador) - para la pestaña seleccionada
style.configure('Active.TNotebook.Tab', foreground='#0078D4')

# Estilo alerta (ROJO) - para alertas
style.configure('Alert.TNotebook.Tab', foreground='red')

notebook = ttk.Notebook(app)
notebook.pack(fill="both", expand=True, padx=10, pady=10)

# PESTAÑA 1: Facturación (interfaz principal)
tab_gestion = ttk.Frame(notebook)
notebook.add(tab_gestion, text="🧾 Facturación")

# PESTAÑA 2: Productos Personalizados
tab_personalizados = ttk.Frame(notebook)
notebook.add(tab_personalizados, text="🔧 Productos Personalizados")

# PESTAÑA 3: Cola de Espera
tab_cola_espera = ttk.Frame(notebook)
notebook.add(tab_cola_espera, text="🔘 Lista de Espera")

# ✅ NUEVO: Detectar cuando el usuario hace click en la pestaña para cambiar color
# Guardar los textos originales de las pestañas
tab_texts = {
    0: "🧾 Facturación",
    1: "🔧 Productos Personalizados",
    2: "🔘 Lista de Espera"
}

def on_tab_changed(event):
    """Se ejecuta cuando cambia de pestaña - actualiza el indicador visual"""
    selected_tab_index = notebook.index(notebook.select())
    tab_cola_espera_index = notebook.index(tab_cola_espera)
    
    # Actualizar textos de todas las pestañas para mostrar indicador en la activa
    for i in range(len(notebook.tabs())):
        original_text = tab_texts.get(i, notebook.tab(i)['text'])
        if i == selected_tab_index:
            # Pestaña activa: agregar indicador azul ► 
            new_text = f"► {original_text}"
            notebook.tab(i, text=new_text)
        else:
            # Pestañas inactivas: solo el texto original
            notebook.tab(i, text=original_text)
    
    if selected_tab_index == tab_cola_espera_index:
        # El usuario entró a la pestaña de Cola de Espera
        print("[TAB-CLICK] ✅ Usuario entró a pestaña Cola de Espera - deteniendo parpadeo")
        # detener_parpadeo_pestaña_global()
        pass

notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

# Aplicar indicador a la primera pestaña (la activa por defecto)
notebook.tab(0, text=f"► {tab_texts[0]}")

# CREAR ESTRUCTURA PRINCIPAL: 2 COLUMNAS (dentro de tab_gestion)
# Columna izquierda: Formulario + Botones
# Columna derecha: Gestor de lista

main_container = ttk.Frame(tab_gestion)
main_container.pack(fill="both", expand=True, padx=10, pady=10)

# Configurar pesos para que se expandan
main_container.columnconfigure(0, weight=0, minsize=400)  # Formulario (fijo)
main_container.columnconfigure(1, weight=1)  # Gestor (expandible)
main_container.rowconfigure(0, weight=1)

# ============================================================================
# PANEL IZQUIERDO: FORMULARIO + BOTONES
# ============================================================================
left_panel = ttk.Frame(main_container)
left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
left_panel.columnconfigure(1, weight=1)

# Detectar si es una pantalla pequeña
pantalla_ancho = app.winfo_screenwidth()
pantalla_alto = app.winfo_screenheight()
es_pantalla_pequena = (pantalla_ancho <= 1366 and pantalla_alto <= 768)

if not es_pantalla_pequena:
    left_panel.rowconfigure(0, weight=0)  # Logo (altura fija)
    left_panel.rowconfigure(1, weight=1)  # Formulario se expande
    left_panel.rowconfigure(2, weight=0)  # Botones mantienen altura fija
else:
    # En pantallas pequeñas, sin logo, solo formulario y botones
    left_panel.rowconfigure(0, weight=1)  # Formulario se expande
    left_panel.rowconfigure(1, weight=0)  # Botones mantienen altura fija

# Logo/Encabezado en la parte superior (solo si NO es pantalla pequeña)
if not es_pantalla_pequena:
    logo_frame = ttk.Frame(left_panel, height=140)
    logo_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=0, pady=(0, 5))
    logo_frame.grid_propagate(False)  # Mantener altura fija

    logo_path = obtener_ruta_absoluta("logo.png")
    if os.path.exists(logo_path):
        try:
            logo_img = Image.open(logo_path)
            logo_img = logo_img.resize((130, 130), Image.Resampling.LANCZOS)
            app.logo_photo_main = ImageTk.PhotoImage(logo_img)
            tk.Label(logo_frame, image=app.logo_photo_main, bg=app.cget('background')).pack(expand=True, padx=5, pady=5)
        except Exception as e:
            debug_log(f"Error cargando logo principal: {e}")
            tk.Label(logo_frame, text="PAINTFLOW", font=("Segoe UI", 20, "bold"), fg="#1976D2", bg=app.cget('background')).pack(expand=True, padx=5)
    else:
        tk.Label(logo_frame, text="PAINTFLOW", font=("Segoe UI", 20, "bold"), fg="#1976D2", bg=app.cget('background')).pack(expand=True, padx=5)
    form_row = 1
else:
    form_row = 0

# Subpanel: Formulario (SIN scroll)
form_frame = ttk.LabelFrame(left_panel, text="Formulario de Producto", padding=10)
form_frame.grid(row=form_row, column=0, columnspan=2, sticky="nsew", pady=(0, 5))
form_frame.columnconfigure(0, weight=1)
form_frame.rowconfigure(0, weight=1)

# Frame para el formulario sin scrollbar
form_scrollable = ttk.Frame(form_frame)

# Crear campos en el frame scrollable
for i, l in enumerate(labels):
    ttk.Label(form_scrollable, text=f"{l}:").grid(row=i, column=0, sticky="w", padx=8, pady=5)

# Crear codigo_entry primero sin next_widget
codigo_entry = AutoCompleteEntry(form_scrollable, sorted(set([c for c in codigos if c and str(c) != 'nan'])), callback=lambda: completar_datos())
codigo_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=5)

# Crear descripcion_entry con codigo_entry como next_widget
descripcion_entry = AutoCompleteEntry(form_scrollable, sorted(set([n for n in nombres if n and str(n) != 'nan'])), callback=lambda: completar_datos(), textvariable=descripcion_var, next_widget=None)
descripcion_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=5)

# Guarda las referencias de los combobox
producto_combobox = ttk.Combobox(form_scrollable, textvariable=producto_var,
    values=['Excello Premium', 'Laca', 'Esmalte Kem', 'Excello VOC', 'Master Paint', 'Tinte al Thinner', 'Super Paint', 'Esmalte Multiuso', 'Excello Pastel', 'Texturizado', 'Water Blocking', 'Kem Aqua', 'Emerald', 'Monocapa', 'Uretano', 'Airpuretec', 'Kem Pro', 'Sanitizing', 'Industrial', 'h&c silicone-acrylic', 'h&c heavy-shield', 'promar® 200 voc', 'promar® 400 voc', 'pro industrial dtm', 'armoseal 1000hs', 'armoseal t-p', 'scuff tuff-wb', 'Macropoxy 646', 'Tile Clad', 'Sher-loxane 800', 'Acrolon 7300', 'Dura-plate 235', 'Dura-plate 235 PW', 'Acrolon 218', 'Armorseal HS-PT', 'Hi-Solids PT', 'Hi-Solids 250', 'Armorseal Rexthane', 'Sherplate 600', 'Macropoxy 4600', 'Urethane Alkyd', 'Water-Base Catalyzed'],
    state='readonly')
producto_combobox.grid(row=2, column=1, sticky="ew", padx=8, pady=5)

# Establecer next_widget de codigo_entry a producto_combobox (saltando descripcion que se llena automáticamente)
codigo_entry.next_widget = producto_combobox

# Función para desplegar combobox automáticamente (DESHABILITADA: causa recursión)
def open_combobox(event):
    pass  # Deshabilitado para prevenir event_generate() recursivo

# Función para manejar selección en combobox (solo cambiar foco)
def on_combobox_selected(event, next_widget=None):
    if next_widget:
        # Solo hacer focus al siguiente widget, sin abrir dropdown
        next_widget.focus()
    return 'break'

# Función para manejar Tab en combobox (DESHABILITADA: causa recursión)
def on_combobox_tab(event, next_widget=None):
    if next_widget:
        next_widget.focus()
    return 'break'

# Asociar Tab a combobox - DESHABILITADO
# producto_combobox.bind('<Tab>', lambda e: on_combobox_tab(e, terminaciones_combobox))

terminaciones_combobox = ttk.Combobox(form_scrollable, textvariable=terminacion_var,
    values=['Mate', 'Satin', 'Semigloss', 'Semimate', 'Gloss', 'Brillo', "N/A", "ESPECIAL", "CLARO", "INTERMEDIO", "ESPECIAL", "MADERA", "PERLADO", "METALICO"],
    state='readonly')
terminaciones_combobox.grid(row=3, column=1, sticky="ew", padx=8, pady=5)
# Deshabilitado: causa recursión
# terminaciones_combobox.bind('<Tab>', lambda e: on_combobox_tab(e, presentacion_combobox))
# Al seleccionar terminación, hacer focus a presentación (sin abrir dropdown)
def on_terminacion_selected(event):
    presentacion_combobox.focus()
    return 'break'
terminaciones_combobox.bind('<<ComboboxSelected>>', on_terminacion_selected)

# Combobox de presentación
presentacion_combobox = ttk.Combobox(form_scrollable, textvariable=presentacion_var,
    values=['1/8', 'Cuarto', 'Medio Galón', 'Galón', 'Cubeta'],
    state='readonly')
presentacion_combobox.grid(row=4, column=1, sticky="ew", padx=8, pady=5)
# Deshabilitado: causa recursión
# presentacion_combobox.bind('<Tab>', lambda e: on_combobox_tab(e))

# Eliminado el selector de impresora (se conserva la lectura/guardado silencioso)

# Campos de solo lectura
ttk.Entry(form_scrollable, textvariable=ubicacion_var, state='readonly').grid(row=5, column=1, sticky="ew", padx=8, pady=5)
ttk.Entry(form_scrollable, textvariable=base_var, state='readonly').grid(row=6, column=1, sticky="ew", padx=8, pady=5)
ttk.Entry(form_scrollable, textvariable=codigo_base_var, state='readonly').grid(row=7, column=1, sticky="ew", padx=8, pady=5)

# Cantidad con spinbox pequeño
spin_widget = tk.Spinbox(form_scrollable, from_=1, to=100, textvariable=spin, font=("Segoe UI", 10), justify="center", width=6)
spin_widget.grid(row=8, column=1, sticky="w", padx=8, pady=5)

# Configurar peso de columnas del formulario
form_scrollable.columnconfigure(1, weight=1)

# Empacar el formulario sin scroll en el frame
form_scrollable.pack(fill="both", expand=True)

# Función para soporte de teclado en combobox (DESHABILITADA: causa stack overflow)
def combobox_keydown(event, combobox):
    pass  # Bindings deshabilitados para prevenir recursión infinita

# Bind a todos los combobox - DESHABILITADOS TEMPORALMENTE
# producto_combobox.bind('<Key>', lambda e: combobox_keydown(e, producto_combobox))
# terminaciones_combobox.bind('<Key>', lambda e: combobox_keydown(e, terminaciones_combobox))

# Bind de Enter para agregar rápido con teclado
def on_entry_return(event):
    """Si hay código: agregar a lista. Si campo vacío y hay lista: enviar lista"""
    codigo = codigo_entry.get().strip()
    if codigo:
        # Si hay código, agregar a la lista
        agregar_producto_a_lista()
    elif lista_productos_factura:
        # Si no hay código pero hay productos en lista, enviar la lista
        enviar_todos_a_lista_espera()

codigo_entry.bind('<Return>', on_entry_return)
descripcion_entry.bind('<Return>', on_entry_return)
spin_widget.bind('<Return>', on_entry_return)

# Función para recalcular código base automáticamente
def recalcular_codigo_base(event=None):
    """Recalcula el código base cuando cambian producto, terminación o presentación"""
    try:
        base_sel = base_var.get().strip()
        p = producto_var.get().strip()
        t = terminacion_var.get().strip()
        pr = presentacion_var.get().strip()
        
        codigo_base_calc = ""
        if base_sel and p and t:
            codigo_base_calc = obtener_codigo_base(base_sel, p, t)
            if codigo_base_calc not in ("No encontrado", "No Aplica", "Error", None, ""):
                suf = obtener_sufijo_presentacion(pr, p, base_sel)
                if suf:
                    codigo_base_calc = f"{codigo_base_calc}{suf}"
            else:
                codigo_base_calc = ""
        
        codigo_base_var.set(codigo_base_calc)
    except Exception as e:
        debug_log(f"Error recalculando código base: {e}")

# Agregar bindings para recalcular automáticamente
producto_combobox.bind('<<ComboboxSelected>>', recalcular_codigo_base)
terminaciones_combobox.bind('<<ComboboxSelected>>', recalcular_codigo_base)
presentacion_combobox.bind('<<ComboboxSelected>>', recalcular_codigo_base)

# También permitir Enter en productos_entry (si existe) y otros campos
presentacion_combobox.bind('<Return>', on_entry_return)
producto_combobox.bind('<Return>', on_entry_return)
terminaciones_combobox.bind('<Return>', on_entry_return)

# # Indicador visual para notificaciones del Gestor (ahora integrado - no necesario)
# notif_gestor_var = tk.BooleanVar(value=False)
# _notif_gestor_dot = tk.Label(app, text="●", fg="red", bg=app.cget('background'))
notif_gestor_var = tk.BooleanVar(value=False)  # Mantener para compatibilidad

# def _actualizar_indicador_gestor(*_):
#     try:
#         if notif_gestor_var.get():
#             _notif_gestor_dot.place(x=240, y=410)
#         else:
#             _notif_gestor_dot.place_forget()
#     except Exception:
#         pass

# notif_gestor_var.trace_add("write", _actualizar_indicador_gestor)
# _actualizar_indicador_gestor()

# # Tooltip para el botón gestor (ya no existe)
# def crear_tooltip(widget, texto):
#     def mostrar_tooltip(event):
#         tooltip = tk.Toplevel()
#         tooltip.wm_overrideredirect(True)
#         tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
#         tooltip.configure(bg="#ffffcc")
#         ttk.Label(tooltip, text=texto, background="#ffffcc", 
#                  font=("Segoe UI", 8)).pack()
#         widget.tooltip_window = tooltip
#     
#     def ocultar_tooltip(event):
#         if hasattr(widget, 'tooltip_window'):
#             widget.tooltip_window.destroy()
#             del widget.tooltip_window
#     
#     widget.bind('<Enter>', mostrar_tooltip)
#     widget.bind('<Leave>', ocultar_tooltip)

# crear_tooltip(btn_gestor, "Ver estado del gestor")

# Listener de finalizaciones desde Gestor para feedback visual/auditivo
def _iniciar_listen_finalizados():
    try:
        import psycopg2, threading, os, datetime
        dsn = None
        try:
            tmp_conn = get_db_pool().pool.getconn()
            try:
                dsn = tmp_conn.dsn
            finally:
                get_db_pool().pool.putconn(tmp_conn)
        except Exception:
            pass
        if dsn:
            conn = psycopg2.connect(dsn)
        else:
            try:
                from db_pool import ConnectionPool as _CP
                _cfg = getattr(_CP, 'DB_CONFIG', {})
            except Exception:
                _cfg = {}
            conn = psycopg2.connect(
                host=_cfg.get('host'),
                port=_cfg.get('port', 5432),
                database=_cfg.get('database'),
                user=_cfg.get('user'),
                password=_cfg.get('password'),
                sslmode=_cfg.get('sslmode','require')
            )
        try:
            conn.set_session(autocommit=True)
        except Exception:
            pass
        canales = ["gestor_pedidos_finalizados"]
        # Fallback adicional: usar la sucursal del título si está disponible
        try:
            suc_titulo = _sucursal_desde_texto(str(globals().get('SUCURSAL', '') or ''))
            if suc_titulo:
                ch_titulo = f"gestor_pedidos_finalizados_{suc_titulo}"
                if ch_titulo not in canales:
                    canales.append(ch_titulo)
        except Exception:
            pass
        try:
            # Obtener sucursal del usuario actual si disponible
            suc = None
            try:
                usuario = globals().get('sistema_login', None)
                if usuario and getattr(usuario, 'usuario_actual', None):
                    suc = obtener_sucursal_usuario(usuario.usuario_actual)
            except Exception:
                pass
            # Fallback: usar USUARIO_USERNAME si está definido
            if not suc:
                try:
                    uname = globals().get('USUARIO_USERNAME', None)
                    if uname:
                        suc = obtener_sucursal_usuario(uname)
                except Exception:
                    pass
            if suc:
                suc = str(suc).strip().lower()
                ch_user = f"gestor_pedidos_finalizados_{suc}"
                if ch_user not in canales:
                    canales.append(ch_user)
        except Exception:
            pass

        # Súper fallback: suscribirse a todos los sufijos existentes pedidos_pendientes_*
        try:
            with db_connection() as _c:
                with _c.cursor() as _cur:
                    _cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'pedidos_pendientes_%'")
                    for r in _cur.fetchall():
                        suf = r[0].replace('pedidos_pendientes_', '').strip().lower()
                        ch_all = f"gestor_pedidos_finalizados_{suf}"
                        if ch_all and ch_all not in canales:
                            canales.append(ch_all)
        except Exception:
            pass
        # Log util para diagnosticar LISTEN
        def _log_listen(msg: str):
            try:
                ruta = os.path.join(os.getcwd(), "labels_listen.log")
                with open(ruta, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        with conn.cursor() as cur:
            _log_listen(f"LISTEN iniciando. Canales: {canales}")
            for ch in canales:
                try:
                    cur.execute(f"LISTEN {ch};")
                    _log_listen(f"LISTEN registrado en: {ch}")
                except Exception as e:
                    _log_listen(f"Error LISTEN {ch}: {e}")
        def _listen_loop_finalizados():
            import select
            try:
                while True:
                    if select.select([conn], [], [], 5) != ([], [], []):
                        conn.poll()
                        while conn.notifies:
                            _ = conn.notifies.pop(0)
                            try:
                                _log_listen(f"NOTIFY recibido: channel={getattr(_, 'channel', '?')} payload={getattr(_, 'payload', '')}")
                            except Exception:
                                pass
                            try:
                                debug_log("NOTIFY recibido:", _)
                            except Exception:
                                pass
                            # Actualizar UI en hilo principal (Tkinter no es thread-safe)
                            try:
                                app.after(0, lambda: notif_gestor_var.set(True))
                            except Exception:
                                try:
                                    notif_gestor_var.set(True)
                                except Exception:
                                    pass
                            # Reproducir sonido (ligero) sin bloquear
                            try:
                                import winsound
                                app.after(0, lambda: winsound.MessageBeep())
                            except Exception:
                                try:
                                    app.after(0, lambda: __import__('winsound').Beep(800, 150))
                                except Exception:
                                    pass
                            # Mensaje visual corto
                            try:
                                app.after(0, lambda: aviso_var.set("✅ Finalización recibida"))
                                app.after(3000, lambda: aviso_var.set(""))
                            except Exception:
                                pass
            except Exception as e_loop:
                _log_listen(f"Error en loop LISTEN: {e_loop}")
            finally:
                try:
                    conn.close()
                    _log_listen("Conexión LISTEN cerrada")
                except Exception:
                    pass
        threading.Thread(target=_listen_loop_finalizados, daemon=True).start()
    except Exception:
        # No silenciar completamente fallas de arranque del listener
        try:
            ruta = os.path.join(os.getcwd(), "labels_listen.log")
            with open(ruta, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.datetime.now().isoformat()}] Error iniciando LISTEN en LabelsApp\n")
        except Exception:
            pass

_iniciar_listen_finalizados()






# GESTOR DE LISTA INTEGRADO EN LA VENTANA PRINCIPAL
# Variables para la lista de productos en factura
# ✅ NOTA: lista_productos_factura ya fue definida en línea ~3626, NO redefinir aquí
# lista_productos_factura = []  # ❌ ELIMINADO: causaba duplicación de variable
id_factura_var = tk.StringVar()
prioridad_var = tk.StringVar(value="Media")

# ============================================================================
# PANEL DERECHO: GESTOR DE LISTA
# ============================================================================
frame_gestor = ttk.LabelFrame(main_container, text="📦 Gestor de Lista de Factura", padding=10)
frame_gestor.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
frame_gestor.columnconfigure(0, weight=1)
frame_gestor.rowconfigure(0, weight=0)  # Estado (altura fija)
frame_gestor.rowconfigure(1, weight=1)  # Lista (expandible)
frame_gestor.rowconfigure(2, weight=0)  # Botones (altura fija)

# Label para estado (arriba)
label_estado_lista = ttk.Label(frame_gestor, text="Productos en lista: 0", font=("Segoe UI", 10), justify="center")
label_estado_lista.grid(row=0, column=0, sticky="ew", pady=(0, 5))

# Frame para la tabla de productos (al centro, expandible)
frame_lista_prod = ttk.LabelFrame(frame_gestor, text="Productos en la Lista", padding=8)
frame_lista_prod.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
frame_lista_prod.columnconfigure(0, weight=1)
frame_lista_prod.rowconfigure(0, weight=1)

# Configurar estilo para cabecera azul
style = ttk.Style()
style.configure("ListaProductos.Treeview.Heading", background="#1976D2", foreground="white", font=("Segoe UI", 11, "bold"), relief="flat", padding=8)

# Treeview para mostrar productos
columns_gestor = ("Código Base", "Descripción", "Color", "Cantidad")
tree_productos_main = ttk.Treeview(frame_lista_prod, columns=columns_gestor, show="headings", style="ListaProductos.Treeview")

# Configurar columnas con pesos proporcionales
for col in columns_gestor:
    tree_productos_main.heading(col, text=col)
    if col == "Código Base":
        tree_productos_main.column(col, width=120, anchor="center")
    elif col == "Descripción":
        tree_productos_main.column(col, width=250, anchor="w")
    elif col == "Color":
        tree_productos_main.column(col, width=110, anchor="center")
    elif col == "Cantidad":
        tree_productos_main.column(col, width=80, anchor="center")

# Scrollbars
vs_tree = ttk.Scrollbar(frame_lista_prod, orient='vertical', command=tree_productos_main.yview)
hs_tree = ttk.Scrollbar(frame_lista_prod, orient='horizontal', command=tree_productos_main.xview)
tree_productos_main.configure(yscrollcommand=vs_tree.set, xscrollcommand=hs_tree.set)

tree_productos_main.grid(row=0, column=0, sticky='nsew')
vs_tree.grid(row=0, column=1, sticky='ns')
hs_tree.grid(row=1, column=0, sticky='ew')
frame_lista_prod.grid_rowconfigure(0, weight=1)
frame_lista_prod.grid_columnconfigure(0, weight=1)

# Función para editar producto en la lista
def editar_producto_en_lista():
    """Editar producto de la lista con debounce"""
    global lista_productos_factura, _last_editar_producto_en_lista
    
    # Debounce: no ejecutar si fue hace menos de 400ms
    ahora = time.time()
    if ahora - _last_editar_producto_en_lista < 0.4:
        return
    _last_editar_producto_en_lista = ahora
    
    sel = tree_productos_main.selection()
    if not sel:
        messagebox.showwarning("Seleccionar", "Debe seleccionar un producto para editar.")
        return
    
    idx = tree_productos_main.index(sel[0])
    prod = lista_productos_factura[idx]
    
    # Cargar datos en el formulario principal
    codigo_entry.delete(0, tk.END)
    codigo_entry.insert(0, prod.get('codigo', ''))
    
    descripcion_var.set(prod.get('descripcion', ''))
    producto_var.set(prod.get('producto', ''))
    terminacion_var.set(prod.get('terminacion', ''))
    presentacion_var.set(prod.get('presentacion', ''))
    ubicacion_var.set(prod.get('ubicacion', ''))
    base_var.set(prod.get('base', ''))
    codigo_base_var.set(prod.get('codigo_base', ''))
    spin.set(prod.get('cantidad', 1))
    
    # Eliminar de la lista (se agregará de nuevo al guardar)
    lista_productos_factura.pop(idx)
    tree_productos_main.delete(sel[0])
    label_estado_lista.config(text=f"Productos en lista: {len(lista_productos_factura)}")
    
    # Scroll al formulario
    codigo_entry.focus()

# Wrapper con debounce para el doble click
def editar_producto_en_lista_debounced(event):
    """Ejecuta editar con debounce automático"""
    editar_producto_en_lista()

# Bind para doble click en el treeview (editar automáticamente)
tree_productos_main.bind('<Double-1>', editar_producto_en_lista_debounced)

# Función para copiar códigos base de todos los productos en la lista
def copiar_fila():
    """Copia los códigos base de todos los productos en la lista"""
    global lista_productos_factura
    
    if not lista_productos_factura:
        messagebox.showwarning("Lista vacía", "No hay productos en la lista")
        return
    
    # Extraer códigos base de la lista global
    codigos_base = [p.get('codigo_base', '') for p in lista_productos_factura if p.get('codigo_base', '').strip()]
    
    if not codigos_base:
        messagebox.showwarning("Sin códigos base", "No se encontraron códigos base en la lista")
        return
    
    # Copiar al portapapeles
    contenido = "\n".join(codigos_base)
    app.clipboard_clear()
    app.clipboard_append(contenido)
    app.update()
    
    messagebox.showinfo("Copiado", f"Se copiaron {len(codigos_base)} códigos base al portapapeles.")

# Función para copiar código color del producto seleccionado
def copiar_codigo_color_seleccionado():
    """Copia el código color del producto seleccionado en la lista"""
    global lista_productos_factura
    
    sel = tree_productos_main.selection()
    if not sel:
        messagebox.showwarning("No seleccionado", "Seleccione un producto de la lista")
        return
    
    # Obtener índice del producto seleccionado
    try:
        idx = tree_productos_main.index(sel[0])
    except Exception:
        messagebox.showwarning("Error", "No se pudo obtener el producto")
        return
    
    if idx < 0 or idx >= len(lista_productos_factura):
        messagebox.showwarning("Error", "Producto no encontrado")
        return
    
    # Obtener el código del producto
    producto = lista_productos_factura[idx]
    codigo = producto.get('codigo', '').strip()
    
    if not codigo:
        messagebox.showwarning("Sin código", "El producto seleccionado no tiene código color")
        return
    
    # Copiar al portapapeles
    app.clipboard_clear()
    app.clipboard_append(codigo)
    app.update()
    
    messagebox.showinfo("Copiado", f"Código color copiado: {codigo}")

# Menú contextual para click derecho
def mostrar_menu_contextual(event):
    """Muestra menú contextual con debounce"""
    global _last_menu_contextual
    
    # Debounce: no ejecutar si fue hace menos de 400ms
    ahora = time.time()
    if ahora - _last_menu_contextual < 0.4:
        return
    _last_menu_contextual = ahora
    
    menu_contextual = tk.Menu(tree_productos_main, tearoff=False)
    menu_contextual.add_command(label="✏️  Editar", command=editar_producto_en_lista)
    menu_contextual.add_command(label="📋 Copiar Código Base", command=copiar_fila)
    menu_contextual.add_command(label="🎨 Copiar Código Color", command=copiar_codigo_color_seleccionado)
    menu_contextual.add_separator()
    menu_contextual.add_command(label="❌ Eliminar", command=_eliminar_producto_gestor)
    
    try:
        menu_contextual.tk_popup(event.x_root, event.y_root)
    finally:
        menu_contextual.grab_release()

# Bind para click derecho
tree_productos_main.bind('<Button-3>', mostrar_menu_contextual)

# Frame para botones del gestor (abajo)
frame_botones_gestor = ttk.Frame(frame_gestor)
frame_botones_gestor.grid(row=2, column=0, sticky="ew", pady=(10, 0))

def _eliminar_producto_gestor():
    """Elimina el producto seleccionado de la lista"""
    global lista_productos_factura
    sel = tree_productos_main.selection()
    if not sel:
        messagebox.showwarning("Selección", "Selecciona un producto para eliminar")
        return
    try:
        idx = tree_productos_main.index(sel[0])
        lista_productos_factura.pop(idx)
        tree_productos_main.delete(sel[0])
        label_estado_lista.config(text=f"Productos en lista: {len(lista_productos_factura)}")
    except Exception as e:
        messagebox.showerror("Error", f"Error al eliminar: {e}")

def _limpiar_lista_gestor():
    """Limpia toda la lista de productos"""
    global lista_productos_factura
    if messagebox.askyesno("Confirmar", "¿Limpiar toda la lista de productos?"):
        lista_productos_factura = []
        tree_productos_main.delete(*tree_productos_main.get_children())
        label_estado_lista.config(text="Productos en lista: 0")

btn_eliminar_gestor = ttk.Button(frame_botones_gestor, text="🗑️ Eliminar", command=_eliminar_producto_gestor)
btn_eliminar_gestor.pack(side="left", padx=5)

btn_limpiar_gestor = ttk.Button(frame_botones_gestor, text="🧹 Limpiar", command=_limpiar_lista_gestor)
btn_limpiar_gestor.pack(side="left", padx=5)

# ✅ NUEVO: Botón para enviar desde el gestor integrado
btn_enviar_gestor = ttk.Button(frame_botones_gestor, text="📤 Enviar", 
                               command=lambda: enviar_todos_a_lista_espera(), bootstyle="success", width=30)
btn_enviar_gestor.pack(side="right", padx=5, pady=5)

# ============================================================================
# PANEL INFERIOR: BOTONES DE ACCIONES
# vista_canvas.pack(anchor='center')
_vista_imgs_cache = {}

# Label para avisos debajo de la vista previa
# aviso_label = ttk.Label(app, textvariable=aviso_var, font=('Segoe UI', 10), foreground="#1976d2", background="#fff")
# aviso_label.place(x=500, y=270, width=380)  # Subido más para dejar espacio al cuadro de acciones

codigo_base_actual = ""

def actualizar_vista():
    """Función vacía - vista previa fue removida para integrar gestor de lista"""
    # Vista previa ya no se muestra; ahora se usa el gestor integrado en la ventana principal
    pass


def completar_datos():
    c = codigo_entry.get().strip()
    d = descripcion_entry.get().strip()

    base_encontrada = ""
    ubicacion_encontrada = ""

    if c in data_por_codigo:
        info = data_por_codigo[c]
        descripcion_var.set(info["nombre"])
        base_encontrada = info.get("base", "")
        ubicacion_encontrada = info.get("ubicacion", "")
    elif d in data_por_nombre:
        info = data_por_nombre[d]
        codigo_entry.delete(0, 'end')
        codigo_entry.insert(0, info["codigo"])
        descripcion_var.set(d)
        base_encontrada = info.get("base", "")
        ubicacion_encontrada = info.get("ubicacion", "")
    
    # Si base sigue vacía, obtenerla desde ProductSW
    if not base_encontrada or base_encontrada.strip() == "":
        conn = None  # ✅ Inicializar para evitar NameError en except
        try:
            conn = get_db_pool().pool.getconn()
            cur = conn.cursor()
            cur.execute("SELECT base FROM ProductSW WHERE codigo = %s AND activo = TRUE", (c,))
            resultado = cur.fetchone()
            cur.close()
            if resultado:
                base_encontrada = resultado[0] or ""
                debug_log(f"✅ Base obtenida desde ProductSW para {c}: {base_encontrada}")
            get_db_pool().pool.putconn(conn)
        except Exception as e:
            debug_log(f"⚠️ No se pudo obtener base de ProductSW: {e}")
            if conn is not None:  # ✅ Verificar que conn exista antes de devolver
                try:
                    get_db_pool().pool.putconn(conn)
                except Exception:
                    pass
    
    base_var.set(base_encontrada)
    ubicacion_var.set(ubicacion_encontrada)
    
    # Limpiar código base para que solo aparezca al presionar el botón
    codigo_base_var.set("")
    
    # Actualizar terminaciones después de completar datos
    actualizar_terminaciones()
    actualizar_vista()

def mostrar_ventana_factura():
    """Muestra ventana emergente para ingresar ID de factura y prioridad"""
    ventana_factura = tk.Toplevel()
    ventana_factura.title("PaintFlow — Información de Factura")
    ventana_factura.geometry(adaptar_geometria(400, 300))
    ventana_factura.resizable(False, False)
    ventana_factura.grab_set()  # Hacer modal
    
    # Centrar la ventana respecto a la ventana principal
    ventana_factura.transient(app)
    
    # Aplicar icono
    aplicar_icono(ventana_factura)
    
    # Centrar la ventana con dimensiones adaptativas
    centrar_ventana_adaptativa(ventana_factura, 400, 250)
    
    # Variables para almacenar los valores
    id_factura_var = tk.StringVar()
    prioridad_var = tk.StringVar(value="Media")
    resultado = {"continuar": False, "id_factura": "", "prioridad": ""}
    
    # Logo (opcional)
    try:
        if LOGO_PATH and os.path.exists(LOGO_PATH):
            from PIL import Image, ImageTk
            _img = Image.open(LOGO_PATH)
            _img.thumbnail((150, 56))
            ventana_factura.logo_photo = ImageTk.PhotoImage(_img)
            ttk.Label(ventana_factura, image=ventana_factura.logo_photo).pack(pady=(12, 0))
    except Exception:
        pass

    # Título
    ttk.Label(ventana_factura, text="Información del Pedido", 
             font=("Segoe UI", 14, "bold")).pack(pady=15)
    
    # Frame para los campos
    frame_campos = ttk.Frame(ventana_factura)
    frame_campos.pack(pady=10, padx=20, fill="x")
    
    # Campo ID Factura
    ttk.Label(frame_campos, text="ID Factura:", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", pady=5)
    entry_factura = ttk.Entry(frame_campos, textvariable=id_factura_var, font=("Segoe UI", 10), width=25)
    entry_factura.grid(row=0, column=1, pady=5, padx=(10, 0))
    entry_factura.focus()
    
    # Campo Prioridad
    ttk.Label(frame_campos, text="Prioridad:", font=("Segoe UI", 10)).grid(row=1, column=0, sticky="w", pady=5)
    combo_prioridad = ttk.Combobox(frame_campos, textvariable=prioridad_var, 
                                  values=["Alta", "Media", "Baja"], 
                                  state="readonly", font=("Segoe UI", 10), width=22)
    combo_prioridad.grid(row=1, column=1, pady=5, padx=(10, 0))
    
    # Frame para botones
    frame_botones = ttk.Frame(ventana_factura)
    frame_botones.pack(pady=20)
    
    def aceptar():
        if not id_factura_var.get().strip():
            messagebox.showwarning("Campo requerido", "Debe ingresar el ID de la factura.")
            entry_factura.focus()
            return
        
        resultado["continuar"] = True
        resultado["id_factura"] = id_factura_var.get().strip()
        resultado["prioridad"] = prioridad_var.get()
        ventana_factura.destroy()
    
    def cancelar():
        resultado["continuar"] = False
        ventana_factura.destroy()
    
    # Botones
    ttk.Button(frame_botones, text="Aceptar", command=aceptar, 
              bootstyle="success").pack(side="left", padx=10)
    ttk.Button(frame_botones, text="Cancelar", command=cancelar, 
              bootstyle="secondary").pack(side="left", padx=10)
    
    # Bind Enter para aceptar
    ventana_factura.bind('<Return>', lambda e: aceptar())
    # Bind Escape para cancelar
    ventana_factura.bind('<Escape>', lambda e: cancelar())
    
    # Esperar hasta que se cierre la ventana
    ventana_factura.wait_window()
    
    return resultado

def imprimir_guardar():
    """Envía tanto el producto individual como toda la lista de productos acumulados"""
    global lista_productos_factura
    
    try:
        # Primero verificar si hay algo que enviar
        c = codigo_entry.get()
        
        # Si no hay producto individual y la lista está vacía, salir silenciosamente
        if not c and not lista_productos_factura:
            return
        
        # VALIDACIÓN OBLIGATORIA: Si hay producto individual, debe tener presentación
        if c:
            pr = presentacion_var.get()
            if not pr:
                messagebox.showwarning("Presentación requerida", 
                                      "Debe seleccionar una presentación antes de enviar el producto.\n\n" +
                                      "Las presentaciones disponibles son:\n" +
                                      "• 1/8 (para lacas)\n" +
                                      "• Cuarto\n" +
                                      "• Medio Galón\n" +
                                      "• Galón\n" +
                                      "• Cubeta")
                return
        
        # Mostrar ventana para ID factura y prioridad (usar valores del frame integrado si están disponibles)
        # Crear una ventana si no la hay o usar los valores existentes
        datos_factura = mostrar_ventana_factura()
        
        # Si el usuario canceló, no continuar
        if not datos_factura["continuar"]:
            return
        
        # ID ingresado por el usuario
        id_factura_input = datos_factura["id_factura"]
        # Si la factura previa está totalmente cerrada (todos sus renglones finalizados), generar un nuevo ID único.
        try:
            id_factura = obtener_id_factura_para_nuevo_envio(id_factura_input, usuario_id=USUARIO_ID)
        except Exception:
            id_factura = id_factura_input
        prioridad = datos_factura["prioridad"]
        
        # Preparar lista de items para enviar en LOTE (incluyendo producto individual si existe)
        items_lote = []
        
        # Agregar producto individual si existe
        if c:
            d = descripcion_var.get()
            p = producto_var.get()
            t = terminacion_var.get()
            pr = presentacion_var.get()
            q = spin.get()
            
            # Guardar en CSV (para compatibilidad)
            reg = {'Codigo': c, 'Descripcion': d, 'Producto': p, 'Terminacion': t, 'Presentacion': pr, 'ID_Factura': id_factura, 'Prioridad': prioridad}
            df = pd.read_csv(CSV_PATH) if os.path.exists(CSV_PATH) else pd.DataFrame()
            df = pd.concat([df, pd.DataFrame([reg])], ignore_index=True)
            df.to_csv(CSV_PATH, index=False)
            
            # Agregar a lista de lote
            items_lote.append({
                'codigo': c,
                'producto': p,
                'terminacion': t,
                'presentacion': pr,
                'cantidad': int(q),
                'base': base_var.get(),
                'ubicacion': ubicacion_var.get()
            })
        
        # Agregar productos de la lista si existen
        if lista_productos_factura:
            items_lote.extend([p.copy() for p in lista_productos_factura])
        
        # Enviar TODO en LOTE de forma síncrona
        if items_lote:
            import threading
            def operaciones_bd_lote():
                try:
                    agregar_lista_a_espera_bulk(items_lote, id_factura, prioridad)
                except Exception as e:
                    debug_log(f"Error en envío en lote: {e}")
            
            threading.Thread(target=operaciones_bd_lote, daemon=True).start()
        
        # Calcular total de productos a enviar
        total_enviados = len(items_lote)
        
        # Esperar a que los threads terminen (máx 2 segundos) para evitar race condition
        import time
        time.sleep(0.5)  # Dar tiempo a que los threads inicien y copien datos
        
        # Limpiar campos e lista
        limpiar_campos()
        
        # Limpiar lista de manera explícita
        del lista_productos_factura[:]  # Elimina todos los elementos de la lista
        
        # Limpiar UI del treeview
        for item in tree_productos_main.get_children():
            tree_productos_main.delete(item)
        label_estado_lista.config(text="Productos en lista: 0")
        
        # Forzar actualización de la UI
        app.update()
        
        # Mostrar confirmación de envío
        messagebox.showinfo("Envío completado", f"Se enviaron {total_enviados} producto(s) a la factura {id_factura}")
        
    except Exception as e:
        debug_log(f"Error en imprimir_guardar: {e}")
        messagebox.showerror("Error", f"Error al enviar: {e}")

def generar_id_factura_unico(id_factura: str, usuario_id: str = None) -> str:
    """Devuelve un ID de factura único para la tabla de pedidos pendientes de la sucursal.
    Si ya existe ese id_factura, agrega un sufijo incremental "-2", "-3", ... hasta encontrar uno libre.
    No modifica registros existentes; sólo propone un nombre disponible.
    """
    try:
        base = str(id_factura).strip()
        if not base:
            return id_factura

        # Quitar sufijo numérico final si ya fue provisto (p.ej., FAC-123-2 -> FAC-123)
        try:
            import re
            m = re.match(r"^(.*?)(?:-(\d+))?$", base)
            base_name = m.group(1).strip() if m else base
        except Exception:
            base_name = base

        usuario_para_deteccion = usuario_id or (USUARIO_USERNAME if 'USUARIO_USERNAME' in globals() and USUARIO_USERNAME else USUARIO_ID)
        sucursal = obtener_sucursal_usuario(usuario_para_deteccion)
        tabla_pendientes = f"pedidos_pendientes_{sucursal}"

        def existe_factura(nombre: str) -> bool:
            try:
                with get_db_connection() as conn:
                    if not conn:
                        return False
                    cur = conn.cursor()
                    cur.execute(f"SELECT 1 FROM {tabla_pendientes} WHERE id_factura = %s LIMIT 1", (nombre,))
                    row = cur.fetchone()
                    return row is not None
            except Exception:
                # Si no podemos verificar, consideramos que no existe para no bloquear el flujo
                return False

        # Si el nombre base no existe, usarlo tal cual
        if not existe_factura(base_name):
            return base_name

        # Buscar el siguiente sufijo disponible
        for n in range(2, 501):  # límite razonable
            candidato = f"{base_name}-{n}"
            if not existe_factura(candidato):
                return candidato

        # Fallback si excede el límite
        return f"{base_name}-{int(time.time()) % 1000}"
    except Exception:
        return id_factura

def factura_esta_cerrada(id_factura: str, usuario_id: str = None) -> bool:
    """Retorna True si la factura tiene al menos un renglón y ninguno está activo.
    Activo = estado NO dentro de ('Finalizado','Completado','Cancelado').
    Cubre variantes de mayúsculas/minúsculas y estados mal tipeados comunes.
    Si falta columna estado => se considera nunca cerrada (False)."""
    try:
        if not id_factura:
            return False
        usuario_para_deteccion = usuario_id or (USUARIO_USERNAME if 'USUARIO_USERNAME' in globals() and USUARIO_USERNAME else USUARIO_ID)
        sucursal = obtener_sucursal_usuario(usuario_para_deteccion)
        tabla = f"pedidos_pendientes_{sucursal}"
        with get_db_connection() as conn:
            if not conn:
                return False
            cur = conn.cursor()
            # Verificar existencia de columna estado
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s AND column_name='estado'
                """,
                (tabla,)
            )
            if not cur.fetchone():
                return False

            # Contar líneas totales y líneas activas (no cerradas)
            cur.execute(f"""
                SELECT 
                    COUNT(*) AS total,
                    SUM(CASE WHEN TRIM(LOWER(COALESCE(estado,''))) NOT IN ('finalizado','completado','cancelado') THEN 1 ELSE 0 END) AS activos
                FROM {tabla}
                WHERE id_factura = %s
            """, (id_factura,))
            row = cur.fetchone()
            if not row:
                return False
            total, activos = row
            # Cerrada si hay registros y activos == 0
            return total > 0 and activos == 0
    except Exception:
        return False

def obtener_id_factura_para_nuevo_envio(id_factura: str, usuario_id: str = None) -> str:
    """Devuelve el mismo id_factura si aún tiene renglones activos.
    Si está cerrada, genera uno nuevo con sufijo incremental."""
    try:
        return generar_id_factura_unico(id_factura, usuario_id=usuario_id) if factura_esta_cerrada(id_factura, usuario_id=usuario_id) else id_factura
    except Exception:
        return id_factura


def limpiar_campos():
    global codigo_base_actual
    codigo_entry.delete(0, 'end')
    descripcion_var.set('')
    producto_var.set('')
    terminacion_var.set('')
    presentacion_var.set('')
    spin.set(1)
    base_var.set('')
    ubicacion_var.set('')
    # vista_canvas.delete('all')  # Comentado: vista_canvas ya no existe
    codigo_base_actual = ''
    codigo_base_var.set('')
    

def obtener_tiempo_acumulado_sucursal(sucursal):
    """Obtiene el tiempo acumulado de todas las órdenes pendientes y en proceso de una sucursal"""
    try:
        with get_db_connection() as conn:
            if not conn:
                return 0
            cur = conn.cursor()
            
            # Obtener la suma de tiempos estimados de órdenes pendientes y en proceso
            cur.execute("""
                SELECT COALESCE(SUM(tiempo_estimado), 0)
                FROM lista_espera 
                WHERE sucursal = %s AND estado IN ('Pendiente', 'En Proceso')
            """, (sucursal,))
            
            tiempo_acumulado = cur.fetchone()[0]
            return tiempo_acumulado
        
    except Exception as e:
        print(f"Error al obtener tiempo acumulado: {e}")
        return 0



def generar_id_profesional(id_factura=None):
    """Genera un ID profesional basado en factura: todos los productos de la misma factura tendrán el mismo ID"""
    from datetime import datetime
    import random
    import time
    
    # Si no hay ID de factura, usar método anterior como fallback
    if not id_factura:
        # Obtener fecha actual
        ahora = datetime.now()
        
        # Iniciales de días de la semana
        dias_semana = {
            0: 'L',  # Lunes
            1: 'M',  # Martes  
            2: 'X',  # Miércoles
            3: 'J',  # Jueves
            4: 'V',  # Viernes
            5: 'S',  # Sábado
            6: 'D'   # Domingo
        }
        
        inicial_dia = dias_semana[ahora.weekday()]
    else:
        # Usar ID de factura como base
        # Extraer los últimos 4 dígitos de la factura
        import re
        numeros = re.findall(r'\d+', str(id_factura))
        if numeros:
            factura_num = int(numeros[-1])  # Tomar el último número encontrado
            # Usar F + últimos 4 dígitos de la factura
            inicial_dia = 'F'
            # Si el número es muy grande, tomar solo los últimos 4 dígitos
            base_numero = factura_num % 10000
        else:
            # Fallback si no hay números en la factura
            inicial_dia = 'F'
            base_numero = abs(hash(str(id_factura))) % 10000
    
    try:
        # Conectar a base de datos
        with get_db_connection() as conn:
            if not conn:
                return f"{inicial_dia}1001"
            cur = conn.cursor()
            
            # Obtener todas las tablas de pedidos pendientes
            cur.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name LIKE 'pedidos_pendientes_%' AND table_schema = 'public'
            """)
            tablas = [row[0] for row in cur.fetchall()]
            
            # También incluir lista_espera si existe
            cur.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'lista_espera' AND table_schema = 'public'
            """)
            if cur.fetchone():
                tablas.append('lista_espera')
            
            # Si tenemos ID de factura, generar ID secuencial basado en la factura
            if id_factura:
                # Obtener el prefijo base para esta factura
                id_base = f"F{base_numero:03d}"
                
                # Buscar cuántos productos ya existen para esta factura
                total_productos_factura = 0
                for tabla in tablas:
                    cur.execute(f"""
                        SELECT COUNT(*) FROM {tabla}
                        WHERE id_factura = %s
                    """, (id_factura,))
                    
                    count = cur.fetchone()[0]
                    total_productos_factura += count
                
                # Generar el siguiente número secuencial para esta factura
                siguiente_secuencia = total_productos_factura + 1
                
                # Intentar generar IDs secuenciales hasta encontrar uno libre
                max_intentos = 10
                for intento in range(max_intentos):
                    # Formato: F001A, F001B, F001C... (base + letra secuencial)
                    if siguiente_secuencia <= 26:
                        sufijo = chr(64 + siguiente_secuencia)  # A=65, B=66, etc.
                    else:
                        # Si hay más de 26 productos, usar números
                        sufijo = str(siguiente_secuencia - 26).zfill(2)
                    
                    id_propuesto = f"{id_base}{sufijo}"
                    
                    # Verificar que no exceda 5 caracteres
                    if len(id_propuesto) > 5:
                        # Reducir el número base si es muy largo
                        base_reducido = base_numero % 100  # Solo 2 dígitos
                        id_base = f"F{base_reducido:02d}"
                        id_propuesto = f"{id_base}{sufijo}"
                    
                    # Verificar si ya existe este ID
                    existe = False
                    for tabla in tablas:
                        cur.execute(f"""
                            SELECT COUNT(*) FROM {tabla}
                            WHERE id_orden_profesional = %s
                        """, (id_propuesto,))
                        
                        count = cur.fetchone()[0]
                        if count > 0:
                            existe = True
                            break
                    
                    if not existe:
                        print(f"✅ Nuevo ID generado para factura {id_factura}: {id_propuesto}")
                        return id_propuesto
                    
                    # Ya existe, probar con el siguiente
                    siguiente_secuencia += 1
            
            # Fallback: usar el método anterior (sin factura o si hay conflicto)
            patron_busqueda = f"{inicial_dia}%"
            ultimo_numero_encontrado = 0
            
            # Buscar el mayor número usado en todas las tablas
            for tabla in tablas:
                cur.execute(f"""
                    SELECT id_orden_profesional FROM {tabla}
                    WHERE id_orden_profesional LIKE %s
                    AND LENGTH(id_orden_profesional) <= 5
                    ORDER BY CAST(SUBSTRING(id_orden_profesional FROM '\\d+') AS INTEGER) DESC 
                    LIMIT 1
                """, (patron_busqueda,))
                
                resultado = cur.fetchone()
                if resultado and resultado[0]:
                    try:
                        import re
                        numeros = re.findall(r'\d+', resultado[0])
                        if numeros:
                            numero = int(numeros[0])
                            ultimo_numero_encontrado = max(ultimo_numero_encontrado, numero)
                    except (ValueError, IndexError):
                        pass
            
            if ultimo_numero_encontrado > 0:
                siguiente_numero = ultimo_numero_encontrado + 1
            else:
                siguiente_numero = 1001  # Empezar desde 1001 para tener 4 dígitos
            
            # Intentar generar un ID único ultra corto (máximo 5 caracteres)
            max_intentos = 50
            for intento in range(max_intentos):
                # Formato: L1234 (1 letra + 4 números = 5 caracteres máximo)
                id_propuesto = f"{inicial_dia}{siguiente_numero}"
                
                # Verificar que no exceda 5 caracteres
                if len(id_propuesto) > 5:
                    siguiente_numero = 1001  # Reset si se hace muy largo
                    id_propuesto = f"{inicial_dia}{siguiente_numero}"
                
                # Verificar si ya existe en cualquier tabla
                existe = False
                for tabla in tablas:
                    cur.execute(f"""
                        SELECT COUNT(*) FROM {tabla}
                        WHERE id_orden_profesional = %s
                    """, (id_propuesto,))
                    
                    count = cur.fetchone()[0]
                    if count > 0:
                        existe = True
                        break
                
                if not existe:
                    # No existe, podemos usarlo
                    return id_propuesto
                
                # Ya existe, incrementar
                siguiente_numero += 1
            
            # Si llegamos aquí, usar timestamp como fallback ultra corto
            timestamp = int(time.time())
            return f"{inicial_dia}{timestamp % 9999}"
        
    except Exception as e:
        debug_log(f"Error generando ID profesional: {e}")
        # Fallback con timestamp ultra corto (máximo 5 caracteres)
        timestamp = int(time.time())
        return f"{inicial_dia}{timestamp % 9999}"

def agregar_a_lista_espera(codigo, producto, terminacion, id_factura, prioridad, cantidad, base=None, presentacion=None, ubicacion=None):
    """Agrega el pedido a la tabla específica de la sucursal del usuario"""
    try:
        # Si la factura original está completamente cerrada, generar un nuevo ID únicamente para este envío
        try:
            if factura_esta_cerrada(id_factura, usuario_id=USUARIO_ID):
                id_factura = generar_id_factura_unico(id_factura, usuario_id=USUARIO_ID)
        except Exception:
            pass

        # Detectar sucursal automáticamente basándose en el USERNAME (no ID numérico)
        usuario_para_deteccion = USUARIO_USERNAME if 'USUARIO_USERNAME' in globals() and USUARIO_USERNAME else USUARIO_ID
        sucursal = obtener_sucursal_usuario(usuario_para_deteccion)
        tabla_pendientes = f'pedidos_pendientes_{sucursal}'

        # Normalizar prioridad
        prioridad_limpia = (prioridad or 'Media').strip().title()
        if prioridad_limpia not in ['Alta', 'Media', 'Baja']:
            prioridad_limpia = 'Media'

        debug_log(f"🏢 Usuario: {usuario_para_deteccion} → Sucursal: {sucursal} → Tabla: {tabla_pendientes} → Prioridad: {prioridad_limpia}")

        # Preparar datos previos (indentación corregida)
        base_a_usar = (base if base not in [None, ""] else base_var.get())
        # Si base sigue vacía, obtenerla desde ProductSW
        if not base_a_usar or base_a_usar.strip() == "":
            conn = None  # ✅ Inicializar para evitar NameError en except
            try:
                conn = get_db_pool().pool.getconn()
                cur = conn.cursor()
                cur.execute("SELECT base FROM ProductSW WHERE codigo = %s AND activo = TRUE", (codigo,))
                resultado = cur.fetchone()
                cur.close()
                if resultado:
                    base_a_usar = resultado[0] or ""
                    debug_log(f"✅ Base obtenida desde ProductSW para {codigo}: {base_a_usar}")
                else:
                    debug_log(f"⚠️ No se encontró base en ProductSW para {codigo}")
                get_db_pool().pool.putconn(conn)
            except Exception as e:
                debug_log(f"❌ Error obteniendo base desde ProductSW: {e}")
                if conn is not None:  # ✅ Verificar que conn exista antes de devolver
                    try:
                        get_db_pool().pool.putconn(conn)
                    except Exception:
                        pass
        
        # Si viene vacía desde el diccionario del producto, tomar la selección actual de la UI
        _pres_tmp = presentacion if presentacion not in [None, ""] else presentacion_var.get()
        presentacion_a_usar = (_pres_tmp or "").strip()
        ubicacion_a_usar = (ubicacion if ubicacion not in [None, ""] else ubicacion_var.get())
        terminacion_a_usar = (terminacion or "").strip()

        # Calcular código base completo (con sufijo presentación si aplica)
        codigo_base_calculado = ""
        if base_a_usar and producto and (terminacion_a_usar or es_producto_texturizado(producto)):
            codigo_base_calculado = obtener_codigo_base(base_a_usar, producto, terminacion_a_usar or "")
            if presentacion_a_usar and codigo_base_calculado not in ["No encontrado", "No Aplica"]:
                sufijo = obtener_sufijo_presentacion(presentacion_a_usar, producto, base or base_var.get())
                if sufijo:
                    codigo_base_calculado += sufijo

        # ✅ Generar ID profesional rápidamente (UUID5 determinístico basado en factura)
        id_profesional = generar_id_orden_rapido(id_factura, 0)

        # INTENTO CON REINTENTOS Y VERIFICACIÓN
        backoffs = [0.15, 0.4, 0.9]
        last_error = None

        for intento, espera in enumerate(backoffs, start=1):
            try:
                with get_db_connection() as conn:
                    if not conn:
                        return []
                    cur = conn.cursor()

                # Obtener columnas desde caché (evita consultar information_schema)
                    # Obtener columnas desde caché (evita consultar information_schema)
                    cols_disponibles = get_table_columns_cached(tabla_pendientes)

                    # verificar si ya existe misma línea según columnas disponibles
                    # Incluir producto y base para evitar acumulaciones cruzadas
                    where_parts = [
                        "id_factura = %s",
                        "codigo = %s",
                        "TRIM(LOWER(producto)) = TRIM(LOWER(%s))",
                        "TRIM(LOWER(base)) = TRIM(LOWER(%s))"
                    ]
                    where_params = [id_factura, codigo, producto, (base_a_usar or '')]
                    if 'presentacion' in cols_disponibles:
                        where_parts.append("TRIM(COALESCE(presentacion,'')) = %s")
                        where_params.append(presentacion_a_usar)
                    if 'terminacion' in cols_disponibles:
                        where_parts.append("TRIM(COALESCE(terminacion,'')) = %s")
                        where_params.append(terminacion_a_usar)
                    where_sql = " AND ".join(where_parts)
                    cur.execute(f"SELECT id FROM {tabla_pendientes} WHERE {where_sql}", tuple(where_params))
                    row = cur.fetchone()
                    if row:
                        # Ya existe una línea con mismo código+factura+presentación
                        # En lugar de descartar, acumulamos cantidad y tiempo estimado
                        try:
                            # Sumar cantidad y actualizar prioridad si es superior
                            # Además, si existe 'estado' y la línea estaba Finalizada/Completada/Cancelada, reabrir a 'Pendiente'.
                            set_partes = [
                                "cantidad = COALESCE(cantidad,0) + %s",
                                "prioridad = CASE WHEN %s = 'Alta' OR prioridad = 'Alta' THEN 'Alta' WHEN %s = 'Media' AND prioridad = 'Baja' THEN 'Media' ELSE prioridad END"
                            ]
                            params_update = [cantidad, prioridad_limpia, prioridad_limpia]
                            if 'estado' in cols_disponibles:
                                set_partes.append("estado = CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado','Cancelado') THEN 'Pendiente' ELSE estado END")
                            if 'fecha_asignacion' in cols_disponibles:
                                set_partes.append("fecha_asignacion = CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado','Cancelado') THEN NULL ELSE fecha_asignacion END")
                            if 'fecha_completado' in cols_disponibles:
                                set_partes.append("fecha_completado = CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado','Cancelado') THEN NULL ELSE fecha_completado END")
                            sql_update = f"UPDATE {tabla_pendientes} SET " + ", ".join(set_partes) + f" WHERE {where_sql}"
                            cur.execute(sql_update, tuple(params_update + where_params))
                            conn.commit()
                            debug_log(f"✅ Acumulado en {tabla_pendientes}: {codigo} x+{cantidad} ({presentacion_a_usar or ''})")
                            return True
                        except Exception as e2:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            # Si falla el UPDATE por cualquier razón, seguimos al flujo de inserción como fallback
                            debug_log(f"⚠️ Falló acumulación; intentaremos insertar nueva línea. Motivo: {e2}")

                    # cols_disponibles ya obtenido arriba

                    # ✅ id_orden_profesional = UUID determinístico, id_factura = lo que digita el usuario
                    cols = [
                        'id_orden_profesional','codigo','producto','terminacion','id_factura',
                        'prioridad','cantidad','base','ubicacion','sucursal'
                    ]
                    vals = [
                        id_profesional, codigo, producto, terminacion_a_usar, id_factura,
                        prioridad_limpia, cantidad, base_a_usar, ubicacion_a_usar, sucursal.title()
                    ]

                    # opcionales
                    if 'presentacion' in cols_disponibles:
                        cols.append('presentacion'); vals.append(presentacion_a_usar)
                    if 'codigo_base' in cols_disponibles:
                        cols.append('codigo_base'); vals.append(codigo_base_calculado)
                    if 'estado' in cols_disponibles:
                        cols.append('estado'); vals.append('Pendiente')

                    placeholders = ", ".join(["%s"] * len(cols))
                    sql = f"INSERT INTO {tabla_pendientes} (" + ", ".join(cols) + ") VALUES (" + placeholders + ")"

                    cur.execute(sql, tuple(vals))
                    # Emitir NOTIFY para actualizar Gestor inmediatamente
                    try:
                        cur.execute(f"NOTIFY pedidos_actualizados, 'labels:factura:{id_factura}'")
                    except Exception:
                        pass
                    try:
                        cur.execute(f"NOTIFY pedidos_actualizados_{sucursal.lower()}, 'labels:factura:{id_factura}'")
                    except Exception:
                        pass
                    conn.commit()

                    # Si el INSERT fue exitoso, simplemente registrar y retornar
                    # La verificación post-inserción es innecesaria (si INSERT commitió, existe)
                    global _tiempo_inicio_envio
                    _tiempo_inicio_envio = time.time()  # Marcar inicio para medir
                    debug_log(f"✅ Insert completado en {tabla_pendientes}: {id_profesional} [{prioridad_limpia}] [T=0ms]")
                    return True

            except Exception as e:
                last_error = e
                try:
                    conn.rollback()
                except Exception:
                    pass
                debug_log(f"❌ Error intento {intento} insertando en {tabla_pendientes}: {e}")
                time.sleep(espera)
                continue

        # Si llegamos aquí, todos los intentos fallaron
        debug_log(f"🚫 Falló el envío tras varios intentos para factura {id_factura}, código {codigo} ({prioridad_limpia}). Último error: {last_error}")
        return False

    except Exception as e:
        debug_log(f"❌ Error general en agregar_a_lista_espera: {e}")
        return False

def agregar_lista_a_espera_bulk(items, id_factura, prioridad_global):
    """Inserta/actualiza en lote todos los productos de una factura en una sola transacción.
    - items: lista de dicts con claves: codigo, descripcion, producto, terminacion, presentacion, cantidad, base, ubicacion
    - id_factura: string de la factura (se usa tal cual, sin sufijos)
    - prioridad_global: 'Alta' | 'Media' | 'Baja'
    Efecto: todos los renglones aparecen juntos en el Gestor (commit único).
    """
    import time
    t_bulk_inicio = time.time()
    
    try:
        debug_log(f"🔷 [BULK] Iniciando agregar_lista_a_espera_bulk con {len(items)} items")
        t_conn = time.time()
        
        usuario_para_deteccion = USUARIO_USERNAME if 'USUARIO_USERNAME' in globals() and USUARIO_USERNAME else USUARIO_ID
        sucursal = obtener_sucursal_usuario(usuario_para_deteccion)
        tabla_pendientes = f"pedidos_pendientes_{sucursal}"
        debug_log(f"🔷 [BULK] Tabla destino: {tabla_pendientes}, sucursal: {sucursal}")

        # Normalizar prioridad
        prio_base = (prioridad_global or 'Media').strip().title()
        if prio_base not in ['Alta', 'Media', 'Baja']:
            prio_base = 'Media'

        # Si la factura está cerrada, crear una nueva para el lote completo
        try:
            if factura_esta_cerrada(id_factura, usuario_id=USUARIO_ID):
                id_factura = generar_id_factura_unico(id_factura, usuario_id=USUARIO_ID)
        except Exception:
            pass

        # Pre-agrupación de items iguales (codigo+producto+base+presentacion+terminacion)
        agrupados = {}
        for it in items:
            codigo = (it.get('codigo') or '').strip()
            producto = (it.get('producto') or '').strip()
            terminacion = (it.get('terminacion') or '').strip()
            presentacion = (it.get('presentacion') or '').strip()
            base_norm = (it.get('base') or '').strip()
            ubicacion = it.get('ubicacion')
            cantidad = int(it.get('cantidad') or 0)
            if not codigo or not producto or cantidad <= 0:
                continue
            # Incluir producto y base para evitar mezcla entre familias o bases diferentes
            key = (codigo, producto, base_norm, presentacion, terminacion)
            if key not in agrupados:
                agrupados[key] = {
                    'codigo': codigo,
                    'producto': producto,
                    'terminacion': terminacion,
                    'presentacion': presentacion,
                    'base': base_norm,
                    'ubicacion': ubicacion,
                    'cantidad': 0
                }
            agrupados[key]['cantidad'] += cantidad

        if not agrupados:
            debug_log(f"🔷 [BULK] Lista vacía después de agrupar (sin items válidos)")
            return True

        debug_log(f"🔷 [BULK] Agrupados: {len(agrupados)} grupos únicos")
        
        t_agrupar = time.time()
        debug_log(f"⏱️ [BULK] Tiempo agrupación: {t_agrupar - t_conn:.3f}s")

        # ✅ SOLUCIÓN 1: Usar pool connection en lugar de hardcodeada
        with get_db_connection() as conn:
            if not conn:
                debug_log(f"❌ [BULK] No se pudo obtener conexión del pool")
                return False
            
            t_conn_obtenida = time.time()
            debug_log(f"⏱️ [BULK] Tiempo obtener conexión: {t_conn_obtenida - t_agrupar:.3f}s")
            
            cur = conn.cursor()

            # ✅ SOLUCIÓN 2: Usar caché de columnas para evitar query redundante
            cols_disponibles = obtener_cols_disponibles_cached(tabla_pendientes, cur)
            debug_log(f"🔷 [BULK] Columnas disponibles: {len(cols_disponibles)}")
            
            t_cols = time.time()
            debug_log(f"⏱️ [BULK] Tiempo obtener columnas: {t_cols - t_conn_obtenida:.3f}s")

            # ✅ OPTIMIZACIÓN: Usar generador simple de IDs (UUID5 determinístico)
            contador_id = 0
            ids_generados = []  # ✅ NUEVO: Guardar IDs para NOTIFYs después del commit
            
            def _gen_id():
                nonlocal contador_id
                id_nuevo = generar_id_orden_rapido(id_factura, contador_id)
                contador_id += 1
                return id_nuevo

            # Para cada item único: intentar acumular si existe, si no, insertar
            items_procesados = 0
            for (codigo, producto, base_key, presentacion, terminacion) in agrupados:
                it = agrupados[(codigo, producto, base_key, presentacion, terminacion)]
                base_a_usar = (base_key if base_key not in [None, ""] else base_var.get())
                presentacion_a_usar = (presentacion or presentacion_var.get() or '').strip()
                ubicacion_a_usar = (it['ubicacion'] if it['ubicacion'] not in [None, ""] else ubicacion_var.get())
                terminacion_a_usar = (terminacion or '').strip()
                cantidad_total = int(it['cantidad'] or 0)

                # Calcular código_base si la tabla lo soporta
                codigo_base_calculado = ""
                try:
                    if 'codigo_base' in cols_disponibles and base_a_usar and producto and (terminacion_a_usar or es_producto_texturizado(producto)):
                        cb = obtener_codigo_base(base_a_usar, producto, terminacion_a_usar or "")
                        if cb not in ["No encontrado", "No Aplica", None, ""]:
                            suf = obtener_sufijo_presentacion(presentacion_a_usar, producto, base_a_usar)
                            codigo_base_calculado = f"{cb}{suf}" if suf else cb
                except Exception:
                    codigo_base_calculado = ""

                # ¿Existe renglón igual? (misma factura + codigo + producto + base + presentacion + terminacion)
                where_parts = ["id_factura = %s", "codigo = %s", "TRIM(LOWER(producto)) = TRIM(LOWER(%s))"]
                where_params = [id_factura, codigo, producto]
                if 'base' in cols_disponibles:
                    where_parts.append("TRIM(COALESCE(base,'')) = %s")
                    where_params.append(base_a_usar)
                if 'presentacion' in cols_disponibles:
                    where_parts.append("TRIM(COALESCE(presentacion,'')) = %s")
                    where_params.append(presentacion_a_usar)
                if 'terminacion' in cols_disponibles:
                    where_parts.append("TRIM(COALESCE(terminacion,'')) = %s")
                    where_params.append(terminacion_a_usar)
                where_sql = " AND ".join(where_parts)
                cur.execute(f"SELECT id FROM {tabla_pendientes} WHERE {where_sql} LIMIT 1", tuple(where_params))
                row = cur.fetchone()
                if row:
                    debug_log(f"🔷 [BULK] Actualizando renglón existente: {codigo} - {producto}")
                    # Acumular cantidad y tiempo; elevar prioridad si aplica; reabrir estado si estaba finalizado
                    set_partes = [
                        "cantidad = COALESCE(cantidad,0) + %s",
                        "prioridad = CASE WHEN %s = 'Alta' OR prioridad = 'Alta' THEN 'Alta' WHEN %s = 'Media' AND prioridad = 'Baja' THEN 'Media' ELSE prioridad END"
                    ]
                    params_update = [cantidad_total, prio_base, prio_base]
                    if 'estado' in cols_disponibles:
                        set_partes.append("estado = CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado','Cancelado') THEN 'Pendiente' ELSE estado END")
                    if 'fecha_asignacion' in cols_disponibles:
                        set_partes.append("fecha_asignacion = CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado','Cancelado') THEN NULL ELSE fecha_asignacion END")
                    if 'fecha_completado' in cols_disponibles:
                        set_partes.append("fecha_completado = CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado','Cancelado') THEN NULL ELSE fecha_completado END")
                    sql_update = f"UPDATE {tabla_pendientes} SET " + ", ".join(set_partes) + f" WHERE {where_sql}"
                    cur.execute(sql_update, tuple(params_update + where_params))
                    debug_log(f"🔷 [BULK] ✓ Renglón actualizado")
                    items_procesados += 1
                else:
                    debug_log(f"🔷 [BULK] Insertando renglón nuevo: {codigo} - {producto}")
                    # Insertar nuevo renglón con ID generado rápidamente
                    id_profesional = _gen_id()
                    ids_generados.append(id_profesional)  # ✅ NUEVO: Guardar para NOTIFY después

                    # ✅ id_orden_profesional = UUID determinístico, id_factura = lo que digita el usuario
                    cols = [
                        'id_orden_profesional','codigo','producto','terminacion','id_factura',
                        'prioridad','cantidad','base','ubicacion','sucursal'
                    ]
                    vals = [
                        id_profesional, codigo, producto, terminacion_a_usar, id_factura,
                        prio_base, cantidad_total, base_a_usar, ubicacion_a_usar, sucursal.title()
                    ]
                    if 'presentacion' in cols_disponibles:
                        cols.append('presentacion'); vals.append(presentacion_a_usar)
                    if 'codigo_base' in cols_disponibles:
                        cols.append('codigo_base'); vals.append(codigo_base_calculado)
                    if 'estado' in cols_disponibles:
                        cols.append('estado'); vals.append('Pendiente')

                    placeholders = ", ".join(["%s"] * len(cols))
                    # UPSERT: Si existe por id_orden_profesional, actualizar. Si no, insertar.
                    update_parts = [f"{col} = EXCLUDED.{col}" for col in cols[1:]]  # Sin id_orden_profesional
                    sql = (
                        f"INSERT INTO {tabla_pendientes} (" + ", ".join(cols) + ") VALUES (" + placeholders + ") "
                        f"ON CONFLICT (id_orden_profesional) DO UPDATE SET " + ", ".join(update_parts)
                    )
                    cur.execute(sql, tuple(vals))

            # ✅ COMMIT primero (ANTES de los NOTIFYs)
            t_before_commit = time.time()
            debug_log(f"⏱️ [BULK] Tiempo procesamiento items: {t_before_commit - t_cols:.3f}s")
            debug_log(f"🔷 [BULK] Ejecutando commit de transacción...")
            conn.commit()
            
            t_after_commit = time.time()
            debug_log(f"⏱️ [BULK] Tiempo commit: {t_after_commit - t_before_commit:.3f}s")
            
            # ✅ NUEVO: Enviar UN ÚNICO NOTIFY por factura completa (no uno por producto)
            # ✅ IMPORTANTE: Enviar SIEMPRE, incluso si solo hay UPDATEs (no solo si hay INSERTs)
            # ✅ IMPORTANTE: Enviar SIEMPRE, incluso si hay solo 1 producto (envío obligatorio en lote)
            if items_procesados > 0:  # ← Cambié: ahora es items_procesados en lugar de ids_generados
                debug_log(f"🔷 [BULK] Enviando NOTIFY único para factura '{id_factura}' ({len(ids_generados)} inserts + {items_procesados} updates)...")
                try:
                    # Crear un cursor NUEVO para el NOTIFY (después del commit)
                    cur_notify = conn.cursor()
                    payload = f"labels:factura:{id_factura}"
                    
                    # ✅ FIX: Usar NOTIFY directo (no SELECT pg_notify)
                    # NOTIFY funciona mejor con sintaxis directa
                    debug_log(f"[NOTIFY-DEBUG] Intentando enviar NOTIFY con payload: '{payload}'")
                    # Escapar el payload de forma segura: reemplazar comillas simples por comillas dobles
                    payload_escaped = payload.replace("'", "''")
                    notify_sql = f"NOTIFY pedidos_actualizados, '{payload_escaped}'"
                    debug_log(f"[NOTIFY-DEBUG] SQL: {notify_sql}")
                    cur_notify.execute(notify_sql)
                    cur_notify.close()
                    
                    debug_log(f"[NOTIFY-DEBUG] ✅ NOTIFY ejecutado correctamente")
                    debug_log(f"🔷 [BULK] ✅ NOTIFY ÚNICO enviado con payload: '{payload}'")
                except Exception as ex_notify:
                    debug_log(f"[NOTIFY-DEBUG] ❌ Error en NOTIFY: {ex_notify}")
                    debug_log(f"⚠️ [BULK] Error en NOTIFY: {ex_notify}")
                    
                    # Fallback: Intentar NOTIFY directo
                    try:
                        cur_notify = conn.cursor()
                        cur_notify.execute(f"NOTIFY pedidos_actualizados, 'labels:factura:{id_factura}'")
                        cur_notify.close()
                        debug_log(f"[NOTIFY-DEBUG] ✅ NOTIFY directo ejecutado (fallback)")
                        debug_log(f"🔷 [BULK] ✅ NOTIFY directo ejecutado (fallback)")
                    except Exception as ex_notify2:
                        debug_log(f"[NOTIFY-DEBUG] ❌ Error con NOTIFY directo: {ex_notify2}")
                        debug_log(f"⚠️ [BULK] Error en NOTIFY directo: {ex_notify2}")
                            
            debug_log(f"🔷 [BULK] ✅ Commit completado y NOTIFYs enviados, cerrando conexión")
            
        debug_log(f"✅ Envío en lote completado: {len(agrupados)} renglones para factura {id_factura}")
        
        t_bulk_total = time.time() - t_bulk_inicio
        debug_log(f"⏱️ [BULK] TIEMPO TOTAL agregar_lista_a_espera_bulk: {t_bulk_total:.3f}s")
        
        return True
    except Exception as e:
        debug_log(f"❌ [BULK] EXCEPCIÓN: {e}")
        try:
            debug_log(f"❌ [BULK] Ejecutando rollback...")
            conn.rollback()
            cur.close(); conn.close()
            debug_log(f"❌ [BULK] Rollback completado")
        except Exception:
            pass
        debug_log(f"❌ Error en envío en lote: {e}")
        return False

# === HISTORIAL DE ENVÍOS ===
# Mantener referencia única para evitar múltiples ventanas abiertas
ventana_historial = None
def obtener_historial_impresiones(limit=200, sucursal=None, factura=None):
    """Obtiene historial de productos enviados limitándolo SIEMPRE a últimas 24 horas.
    Devuelve (columnas, filas) usando sólo columnas existentes para tolerar cambios de esquema.
    ORDENAMIENTO MEJORADO: agrupa por presentacion y terminacion para evitar desorden.
    Selecciona una única columna de fecha (preferencia: created_at, fecha, fecha_impresion, timestamp)."""
    try:
        with get_db_connection() as conn:
            if not conn:
                return [], []
            cur = conn.cursor()

            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'historial_impresiones'
                """
            )
            cols_db = [r[0] for r in cur.fetchall()]
            if not cols_db:
                return [], []

            # Detectar columna de fecha preferida
            preferencia_fechas = ['created_at','fecha','fecha_impresion','timestamp']
            col_fecha = next((c for c in preferencia_fechas if c in cols_db), None)

            select_parts = []
            columnas = []
            if col_fecha:
                select_parts.append(f"{col_fecha} AS fecha")
                columnas.append('fecha')

            def add(colname, alias=None):
                if colname in cols_db:
                    select_parts.append(f"{colname} AS {alias or colname}")
                    columnas.append(alias or colname)

            add('codigo')
            add('descripcion')
            add('producto')
            add('terminacion')
            add('presentacion')
            add('cantidad')
            # Factura puede existir como id_factura o factura
            if 'id_factura' in cols_db:
                add('id_factura','factura')
            elif 'factura' in cols_db:
                add('factura','factura')
            add('prioridad')
            add('sucursal')
            # usuario / operador
            if 'usuario_id' in cols_db:
                add('usuario_id','operador')
            elif 'operador' in cols_db:
                add('operador','operador')

            if not select_parts:
                return [], []

            base_q = f"SELECT {', '.join(select_parts)} FROM historial_impresiones"
            conds = []
            params = []
            if sucursal and 'sucursal' in cols_db:
                conds.append("TRIM(LOWER(sucursal)) = TRIM(LOWER(%s))")
                params.append(sucursal)
            if factura and ('id_factura' in cols_db or 'factura' in cols_db):
                col_fact = 'id_factura' if 'id_factura' in cols_db else 'factura'
                conds.append(f"{col_fact} = %s")
                params.append(factura)
            if col_fecha:
                # Siempre limitar a últimas 24 horas
                conds.append(f"{col_fecha} >= NOW() - INTERVAL '24 HOURS'")
            if conds:
                base_q += " WHERE " + " AND ".join(conds)
            # ✅ ORDENAMIENTO MEJORADO: primero presentacion, luego terminacion, luego fecha DESC
            # Esto agrupa productos similares juntos para una mejor visualización
            order_parts = []
            if 'presentacion' in cols_db:
                order_parts.append("COALESCE(presentacion, '') ASC")
            if 'terminacion' in cols_db:
                order_parts.append("COALESCE(terminacion, '') ASC")
            if col_fecha:
                order_parts.append(f"{col_fecha} DESC")
            else:
                order_parts.append("1 DESC")
            if order_parts:
                base_q += " ORDER BY " + ", ".join(order_parts)
            base_q += " LIMIT %s"; params.append(limit)
            cur.execute(base_q, params)
            filas = cur.fetchall()
            return columnas, filas
    except Exception as e:
        debug_log(f"❌ Error obteniendo historial impresiones: {e}")
        return [], []


def abrir_historial_impresiones():
    """Abre ventana de historial agrupada por FACTURAS, igual que el gestor de pendientes."""
    global ventana_historial
    try:
        # Reutilizar ventana si existe
        if ventana_historial is not None:
            try:
                if ventana_historial.winfo_exists():
                    ventana_historial.deiconify(); ventana_historial.lift(); ventana_historial.focus_force()
                    return
            except Exception:
                pass

        ventana = tk.Toplevel(app)
        ventana.title("PaintFlow — Historial de Envíos (Agrupado por Factura)")
        try:
            ventana.state('zoomed')
        except Exception:
            pass
        ventana.minsize(adaptar_dimension(950), adaptar_dimension(560))
        
        # Aplicar icono
        aplicar_icono(ventana)
        ventana_historial = ventana

        # ===== Estilos =====
        try:
            style = ttk.Style()
            style.configure("Hist.Treeview", rowheight=30, font=("Segoe UI",11))
            style.configure("Treeview.Heading", font=("Segoe UI",12,"bold"))
            style.map("Hist.Treeview", background=[("selected","#1976D2")], foreground=[("selected","white")])
        except Exception:
            pass

        # ===== Header =====
        header = ttk.Frame(ventana, style="Card.TFrame")
        header.pack(fill="x", padx=10, pady=8)
        ttk.Label(header, text="📦 Historial de Envíos (24h) - Agrupado por Factura", font=("Segoe UI",18,"bold"), style="Card.TLabel").pack(side="left", padx=8)
        right_header = ttk.Frame(header, style="Card.TFrame")
        right_header.pack(side="right")
        ttk.Label(right_header, text=f"👤 {USUARIO_USERNAME}", font=("Segoe UI",11), style="Card.TLabel", bootstyle="success").pack(side="right", padx=5)

        # ===== Filtros =====
        filtros_bar = ttk.Frame(ventana, style="Card.TFrame")
        filtros_bar.pack(fill="x", padx=10, pady=4)
        
        # ✅ Mostrar solo sucursal del usuario (SIN cambiar)
        sucursal_sel = SUCURSAL  # Bloqueado a sucursal del usuario
        ttk.Label(filtros_bar, text=f"🏢 Sucursal: {sucursal_sel}", style="Card.TLabel", bootstyle="info").pack(side="left", padx=(10,4))
        
        label_info = ttk.Label(filtros_bar, text="⏳ Cargando...", style="Card.TLabel", bootstyle="secondary")
        label_info.pack(side="right", padx=8)
        btn_actualizar = ttk.Button(filtros_bar, text="🔄 Actualizar")
        btn_actualizar.pack(side="right", padx=4)
        btn_exportar = ttk.Button(filtros_bar, text="📊 Exportar CSV")
        btn_exportar.pack(side="right", padx=4)

        # ===== Tabla (Agrupada por Factura) =====
        tree_frame = ttk.Frame(ventana, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=6)
        tree = ttk.Treeview(tree_frame, style="Hist.Treeview")
        
        # Columnas: agrupamiento por factura
        columnas = ("Factura", "Productos", "Presentaciones", "Terminaciones", "Total Qty", "Operador", "Primera", "Última")
        tree["columns"] = columnas
        tree["show"] = "headings"
        
        anchos = {
            "Factura": 130, "Productos": 150, "Presentaciones": 130, "Terminaciones": 130,
            "Total Qty": 90, "Operador": 140, "Primera": 130, "Última": 130
        }
        
        for col in columnas:
            tree.heading(col, text=col)
            tree.column(col, width=anchos.get(col, 120), anchor='center')
        
        sv = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        sh = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=sv.set, xscrollcommand=sh.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sv.grid(row=0, column=1, sticky="ns")
        sh.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        # Tags para colores
        tree.tag_configure('agrupado', background='#f5f5f5', foreground='#333')

        # ===== Lógica de carga =====
        def cargar_datos():
            tree.delete(*tree.get_children())
            inicio = time.time()
            
            try:
                with get_db_connection() as conn:
                    if not conn:
                        label_info.configure(text="❌ Sin conexión", bootstyle="danger")
                        return
                    cur = conn.cursor()
                    
                    # Obtener TODAS las tablas pedidos_pendientes_*
                    cur.execute("""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema='public' AND table_name LIKE 'pedidos_pendientes_%'
                        ORDER BY table_name
                    """)
                    todas_tablas = [r[0] for r in cur.fetchall()]
                    
                    if not todas_tablas:
                        label_info.configure(text="❌ Sin tablas de pedidos", bootstyle="danger")
                        return
                    
                    # Buscar tabla para la sucursal actual
                    sufijo_sucursal = sucursal_sel.lower().replace(' ', '_').replace('á', 'a').replace('é', 'e').replace('í', 'i').replace('ó', 'o').replace('ú', 'u')
                    tabla_pedidos = None
                    
                    # Primero: buscar match exacto
                    tabla_buscada = f"pedidos_pendientes_{sufijo_sucursal}"
                    if tabla_buscada in todas_tablas:
                        tabla_pedidos = tabla_buscada
                    else:
                        # Segundo: buscar que contenga el sufijo
                        coincidencias = [t for t in todas_tablas if sufijo_sucursal in t.lower()]
                        if coincidencias:
                            tabla_pedidos = coincidencias[0]
                    
                    if not tabla_pedidos:
                        # Fallback: usar la primera tabla disponible (por defecto principal)
                        tabla_pedidos = next((t for t in todas_tablas if 'principal' in t), todas_tablas[0])
                        print(f"[HISTORIAL] Usando tabla fallback: {tabla_pedidos}")
                    
                    print(f"[HISTORIAL] Usando tabla: {tabla_pedidos}")
                    
                    # Detectar columnas disponibles
                    cur.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=%s
                        ORDER BY ordinal_position
                    """, (tabla_pedidos,))
                    cols_db = [r[0] for r in cur.fetchall()]
                    cols_lower = {c.lower(): c for c in cols_db}
                    
                    if not cols_db:
                        label_info.configure(text=f"❌ Tabla {tabla_pedidos} vacía", bootstyle="danger")
                        return
                    
                    # Mapear columnas flexible
                    def get_col(opciones):
                        for opt in opciones:
                            if opt.lower() in cols_lower:
                                return cols_lower[opt.lower()]
                        return None
                    
                    # Columnas obligatorias
                    col_factura = get_col(['id_factura', 'factura', 'numero'])
                    col_fecha_creacion = get_col(['fecha_creacion', 'created_at', 'timestamp', 'fecha', 'fecha_creado', 'date_created', 'creado', 'fecha_ingreso', 'fecha_ini'])
                    
                    if not col_factura:
                        label_info.configure(text="❌ Falta id_factura", bootstyle="danger")
                        print(f"[HISTORIAL] Columnas disponibles: {cols_db}")
                        return
                    if not col_fecha_creacion:
                        label_info.configure(text="❌ Falta fecha", bootstyle="danger")
                        print(f"[HISTORIAL ERROR] No se encontró columna de fecha")
                        print(f"[HISTORIAL] Tabla: {tabla_pedidos}")
                        print(f"[HISTORIAL] Columnas disponibles: {cols_db}")
                        return
                    
                    # Columnas opcionales
                    col_producto = get_col(['producto', 'descripcion', 'name', 'product'])
                    col_presentacion = get_col(['presentacion', 'size', 'presentation'])
                    col_terminacion = get_col(['terminacion', 'finish', 'state'])
                    col_cantidad = get_col(['cantidad', 'qty', 'quantity'])
                    col_operador = get_col(['operador', 'usuario', 'user', 'operator'])
                    col_fecha_completado = get_col(['fecha_completado', 'completed_at', 'updated_at'])
                    col_estado = get_col(['estado', 'status', 'state', 'status_column'])
                    
                    # ✅ QUERY AGRUPADA - CON FALLBACKS PARA COLUMNAS OPCIONALES
                    # Manejar None en col_fecha_completado
                    col_fecha_final = f"COALESCE({col_fecha_completado}, {col_fecha_creacion})" if col_fecha_completado else col_fecha_creacion
                    
                    # Solo incluir agregaciones si la columna existe
                    parts = [
                        f"{col_factura}",
                        "COUNT(*) AS total_items",
                    ]
                    
                    if col_producto:
                        parts.append(f"COUNT(DISTINCT TRIM(LOWER(COALESCE({col_producto}, '')))) AS cnt_productos")
                    if col_presentacion:
                        parts.append(f"STRING_AGG(DISTINCT TRIM(COALESCE({col_presentacion}, '')), ', ') AS presentaciones")
                    if col_terminacion:
                        parts.append(f"STRING_AGG(DISTINCT TRIM(COALESCE({col_terminacion}, '')), ', ') AS terminaciones")
                    if col_cantidad:
                        parts.append(f"SUM(CAST(COALESCE({col_cantidad}, 1) AS INTEGER)) AS total_qty")
                    if col_operador:
                        parts.append(f"MAX({col_operador}) AS operador")
                    
                    parts.append(f"MIN({col_fecha_creacion}) AS primera_fecha")
                    parts.append(f"MAX({col_fecha_final}) AS ultima_fecha")
                    
                    if col_estado:
                        parts.append(f"STRING_AGG(DISTINCT {col_estado}, ', ') AS estados_distintos")
                    
                    query = f"""
                        SELECT {', '.join(parts)}
                        FROM {tabla_pedidos}
                        WHERE {col_fecha_creacion} >= NOW() - INTERVAL '24 HOURS'
                        GROUP BY {col_factura}
                        ORDER BY MAX({col_fecha_final}) DESC
                        LIMIT 250
                    """
                    
                    cur.execute(query)
                    rows = cur.fetchall()
                    
                    # Función para formatear fechas (una sola vez)
                    def fmt_fecha(dt):
                        if dt is None:
                            return '—'
                        if isinstance(dt, str):
                            return dt[:16]
                        try:
                            from datetime import timezone
                            try:
                                from zoneinfo import ZoneInfo
                                tz = ZoneInfo('America/Santo_Domingo')
                            except:
                                tz = timezone(timedelta(hours=-4))
                            if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            dt = dt.astimezone(tz) if hasattr(dt, 'astimezone') else dt
                            return dt.strftime('%Y-%m-%d %H:%M')
                        except:
                            return str(dt)[:16]
                    
                    if not rows:
                        tree.insert("", "end", values=("—", 0, "—", "—", 0, "—", "—", "—"), tags=('agrupado',))
                    else:
                        for row in rows:
                            # Desempacar 10 columnas (incluyendo estados_distintos)
                            (factura, total_items, cnt_prod, presentaciones, terminaciones, 
                             total_qty, operador, primera, ultima, estados) = row
                            
                            tree.insert("", "end", values=(
                                factura or '—',
                                int(total_items or 0),
                                (presentaciones or '—')[:50],
                                (terminaciones or '—')[:50],
                                int(total_qty or 0),
                                (operador or '—')[:40],
                                fmt_fecha(primera),
                                fmt_fecha(ultima)
                            ), tags=('agrupado',))
                    
                    dur = time.time() - inicio
                    label_info.configure(text=f"✅ {len(rows)} facturas • {dur:.2f}s", bootstyle="success")
                    
            except Exception as e:
                import traceback
                error_msg = f"{type(e).__name__}: {str(e)}"
                print(f"[HISTORIAL ERROR] {error_msg}")
                print(f"[TRACEBACK]\n{traceback.format_exc()}")
                debug_log(f"Error cargando historial: {error_msg}")
                label_info.configure(text=f"❌ {error_msg[:50]}", bootstyle="danger")

        def refrescar():
            cargar_datos()

        def exportar():
            try:
                import csv
                from tkinter import filedialog
                items = tree.get_children()
                if not items:
                    messagebox.showinfo('Exportar CSV', 'No hay filas para exportar.')
                    return
                filename = filedialog.asksaveasfilename(
                    title='Guardar historial',
                    defaultextension='.csv',
                    filetypes=[('CSV','*.csv')],
                    initialfile=f'historial_facturas_{SUCURSAL}_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
                )
                if not filename:
                    return
                with open(filename, 'w', newline='', encoding='utf-8') as f:
                    w = csv.writer(f)
                    w.writerow(columnas)
                    for iid in items:
                        w.writerow(tree.item(iid, 'values'))
                messagebox.showinfo('Exportar CSV', f'Se exportó a:\n{filename}')
            except Exception as e:
                messagebox.showerror('Exportar CSV', f'Error: {e}')

        btn_actualizar.configure(command=refrescar)
        btn_exportar.configure(command=exportar)

        cargar_datos()
        
        # Actualizar cada 20 segundos
        def refrescar_auto():
            try:
                cargar_datos()
            finally:
                if ventana_historial and ventana_historial.winfo_exists():
                    ventana_historial.after(20000, refrescar_auto)

        ventana.after(20000, refrescar_auto)

    except Exception as e:
        debug_log(f"Error abriendo historial impresiones: {e}")
        messagebox.showerror("Error", f"No se pudo abrir historial:\n{e}")

# === FUNCIONES PARA LISTA DE PRODUCTOS DE FACTURA ===

def agregar_producto_a_lista():
    """Agrega el producto actual a la lista temporal de la factura"""
    global lista_productos_factura
    
    # Validar campos necesarios
    c = codigo_entry.get().strip()
    d = descripcion_var.get().strip()
    p = producto_var.get().strip()
    t = terminacion_var.get().strip()
    pr = presentacion_var.get().strip()
    q = spin.get()
    
    if not c:
        messagebox.showwarning("Campo requerido", "Debe ingresar un código.")
        return
    
    if not p:
        messagebox.showwarning("Campo requerido", "Debe seleccionar un producto.")
        return
    
    if not pr:
        messagebox.showwarning("Presentación requerida", 
                              "Debe seleccionar una presentación antes de agregar el producto a la lista.\n\n" +
                              "Las presentaciones disponibles son:\n" +
                              "• 1/8 (para lacas)\n" +
                              "• Cuarto\n" +
                              "• Medio Galón\n" +
                              "• Galón\n" +
                              "• Cubeta")
        return
    
    # Crear producto para la lista
    producto_item = {
        'codigo': c,
        'descripcion': d,
        'producto': p,
        'terminacion': t,
        'presentacion': pr,
        'cantidad': q,
        'base': base_var.get(),
        'ubicacion': ubicacion_var.get()
    }
    
    # Calcular y guardar código base en el momento de agregar
    try:
        base_sel = producto_item.get('base')
        codigo_base_calc = ""
        if base_sel and p and t:
            codigo_base_calc = obtener_codigo_base(base_sel, p, t)
            if codigo_base_calc not in ("No encontrado", "No Aplica", "Error", None, ""):
                suf = obtener_sufijo_presentacion(pr, p, base_sel)
                if suf:
                    codigo_base_calc = f"{codigo_base_calc}{suf}"
            else:
                codigo_base_calc = ""
        producto_item['codigo_base'] = codigo_base_calc
    except Exception:
        producto_item['codigo_base'] = ""
    
    # Agregar a la lista directamente (sin validación de duplicados)
    
    # Agregar a la lista
    debug_log(f"[AGREGAR-LISTA] Antes de append: lista_productos_factura tiene {len(lista_productos_factura)} items")
    lista_productos_factura.append(producto_item)
    debug_log(f"[AGREGAR-LISTA] Después de append: lista_productos_factura tiene {len(lista_productos_factura)} items")
    debug_log(f"[AGREGAR-LISTA] Producto agregado: Código={c}, Producto={p}, Presentación={pr}")
    
    # Actualizar treeview integrado
    try:
        codigo_base_disp = producto_item.get('codigo_base', '')
        descripcion_disp = f"{p} / {t}"
        color_disp = c
        cantidad_disp = q
        
        tree_productos_main.insert('', 'end', values=(codigo_base_disp, descripcion_disp, color_disp, cantidad_disp))
        label_estado_lista.config(text=f"Productos en lista: {len(lista_productos_factura)}")
        debug_log(f"[AGREGAR-LISTA] TreeView actualizado. Total productos en lista: {len(lista_productos_factura)}")
    except Exception as e:
        debug_log(f"Error actualizando treeview: {e}")
    
    # Limpiar campos para el siguiente producto
    limpiar_campos()

def enviar_todos_a_lista_espera():
    """FUNCIÓN GLOBAL: Envía todos los productos en lista como un lote unitario"""
    global lista_productos_factura
    
    # Validar que haya lista
    if not lista_productos_factura:
        messagebox.showwarning("Lista vacía", "No hay productos para enviar")
        return
    
    # Mostrar ventana para ID factura y prioridad
    datos_factura = mostrar_ventana_factura()
    
    # Si el usuario canceló, no continuar
    if not datos_factura["continuar"]:
        return
    
    id_factura = datos_factura["id_factura"]
    prioridad = datos_factura["prioridad"]
    
    # Crear snapshot de items para evitar race condition
    items_a_enviar = [p.copy() for p in lista_productos_factura]
    
    debug_log(f"\n{'='*70}")
    debug_log(f"[ENVIO-GLOBAL] Enviando {len(items_a_enviar)} items de lista_productos_factura")
    for idx, item in enumerate(items_a_enviar):
        debug_log(f"[ENVIO-GLOBAL] [{idx}] Código: {item.get('codigo')}, Producto: {item.get('producto')}, Cantidad: {item.get('cantidad')}")
    debug_log(f"{'='*70}\n")
    
    # Enviar en lote (SÍNCRONO para garantizar que se envíe)
    try:
        debug_log(f"📋 [GLOBAL] Iniciando envío en lote de {len(items_a_enviar)} items para factura {id_factura}")
        ok = agregar_lista_a_espera_bulk(items_a_enviar, id_factura, prioridad)
        
        if ok:
            debug_log(f"✅ [GLOBAL] Lote enviado exitosamente: {len(items_a_enviar)} items")
            
            # Limpiar lista
            del lista_productos_factura[:]
            
            # Limpiar UI
            for item in tree_productos_main.get_children():
                tree_productos_main.delete(item)
            label_estado_lista.config(text="Productos en lista: 0")
            
            # Sin messagebox para no interrumpir
            debug_log(f"✅ [GLOBAL] Se enviaron {len(items_a_enviar)} producto(s) a la factura {id_factura}")
        else:
            debug_log(f"⚠️ [GLOBAL] Lote no se envió correctamente")
            messagebox.showerror("Envío no completado", "No se pudo enviar el lote. Revise la conexión e intente nuevamente.")
    except Exception as e:
        error_msg = str(e)
        debug_log(f"❌ [GLOBAL] Error enviando lote: {error_msg}")
        messagebox.showerror("Error al enviar", f"Error: {error_msg}\n\nIntente nuevamente o contacte al soporte.")

def abrir_gestor_lista_factura():
    """Abre la ventana para gestionar la lista de productos de la factura"""
    global lista_productos_factura
    
    if not lista_productos_factura:
        messagebox.showinfo("Lista vacía", "No hay productos en la lista.\nAgregue productos usando 'Agregar a Lista'.")
        return
    
    # Crear ventana
    ventana_lista = tk.Toplevel(app)
    ventana_lista.title("PaintFlow — Gestionar Lista de Factura")
    # Ampliar ventana para que quepan botones cómodamente
    ventana_lista.geometry(adaptar_geometria(1040, 660))
    ventana_lista.resizable(True, True)
    ventana_lista.grab_set()
    ventana_lista.transient(app)
    
    # Aplicar icono
    aplicar_icono(ventana_lista)
    
    # Centrar ventana con dimensiones adaptativas
    centrar_ventana_adaptativa(ventana_lista, 1040, 660)
    
    # Logo opcional
    try:
        if LOGO_PATH and os.path.exists(LOGO_PATH):
            from PIL import Image, ImageTk
            _img = Image.open(LOGO_PATH)
            _img.thumbnail((180, 68))
            ventana_lista.logo_photo = ImageTk.PhotoImage(_img)
            ttk.Label(ventana_lista, image=ventana_lista.logo_photo).pack(pady=(8, 2))
    except Exception:
        pass

    # Título
    ttk.Label(ventana_lista, text="Lista de Productos para Factura", 
             font=("Segoe UI", 14, "bold")).pack(pady=10)
    
    # Frame para información de factura
    frame_factura = tk.LabelFrame(ventana_lista, text="Información de Factura", padx=10, pady=10)
    frame_factura.pack(fill="x", padx=20, pady=5)
    
    # Variables para factura
    id_factura_var = tk.StringVar()
    prioridad_var = tk.StringVar(value="Media")
    
    # Campos de factura
    ttk.Label(frame_factura, text="ID Factura:").grid(row=0, column=0, sticky="w", padx=5)
    entry_factura = ttk.Entry(frame_factura, textvariable=id_factura_var, width=25)
    entry_factura.grid(row=0, column=1, padx=5, sticky="w")
    
    ttk.Label(frame_factura, text="Prioridad:").grid(row=0, column=2, sticky="w", padx=5)
    combo_prioridad = ttk.Combobox(frame_factura, textvariable=prioridad_var, 
                                  values=["Alta", "Media", "Baja"], state="readonly", width=15)
    combo_prioridad.grid(row=0, column=3, padx=5, sticky="w")
    
    # Frame para lista de productos
    frame_lista = tk.LabelFrame(ventana_lista, text="Productos en la Lista", padx=10, pady=10)
    frame_lista.pack(fill="both", expand=True, padx=20, pady=10)
    
    # Treeview para mostrar productos con el nuevo orden
    # 1) Código Base, 2) Descripción (Producto + Terminación), 3) Color (código), 4) Cantidad
    columns = ("Código Base", "Descripción", "Color", "Cantidad")
    tree_productos = ttk.Treeview(frame_lista, columns=columns, show="headings", height=12)

    # Configurar columnas
    for col in columns:
        tree_productos.heading(col, text=col)
        if col == "Código Base":
            tree_productos.column(col, width=140, anchor="center")
        elif col == "Descripción":
            tree_productos.column(col, width=240, anchor="w")
        elif col == "Color":
            tree_productos.column(col, width=110, anchor="center")
        elif col == "Cantidad":
            tree_productos.column(col, width=90, anchor="center")
    
    # Scrollbar
    scrollbar = ttk.Scrollbar(frame_lista, orient="vertical", command=tree_productos.yview)
    tree_productos.configure(yscrollcommand=scrollbar.set)
    
    tree_productos.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    
    def _descripcion_pt(producto, terminacion):
        try:
            p = (producto or "").strip()
            t = (terminacion or "").strip()
            if not p and not t:
                return ""
            # Combinar como "Excello Premium Mate"
            return (f"{p} {t}").strip().title()
        except Exception:
            return f"{producto} {terminacion}".strip()

    def _codigo_base_producto(p):
        try:
            base_color = p.get('base')
            prod = p.get('producto')
            term = p.get('terminacion')
            pres = p.get('presentacion')
            # Cache combinado para evitar recomputar: base+producto+terminación+presentación
            key = ((base_color or '').strip().lower(), (prod or '').strip().lower(), (term or '').strip().lower(), (pres or '').strip())
            if key in _CACHE_CODIGO_BASE_RESULT:
                return _CACHE_CODIGO_BASE_RESULT[key]
            cod_base = obtener_codigo_base(base_color, prod, term)
            if cod_base in ("No encontrado", "No Aplica", None, ""):
                resultado = ""
            else:
                sufijo = obtener_sufijo_presentacion(pres, prod, base_color)
                resultado = f"{cod_base}{sufijo}" if sufijo else cod_base
            _CACHE_CODIGO_BASE_RESULT[key] = resultado
            return resultado
        except Exception:
            return ""

    def cargar_productos_en_tree():
        """Carga los productos de la lista en el treeview con el nuevo formato.
        No calcula códigos base para abrir la ventana más rápido."""
        for item in tree_productos.get_children():
            tree_productos.delete(item)

        snapshot = [p.copy() for p in lista_productos_factura]
        for idx, p in enumerate(snapshot):
            try:
                desc = _descripcion_pt(p.get('producto'), p.get('terminacion'))
                color = p.get('codigo')
                cant = p.get('cantidad')
                cb = p.get('codigo_base') or ""
                tree_productos.insert("", "end", iid=str(idx), values=(cb, desc, color, cant))
            except Exception:
                tree_productos.insert("", "end", iid=str(idx), values=("", _descripcion_pt(p.get('producto'), p.get('terminacion')), p.get('codigo'), p.get('cantidad')))
        # Códigos base ya vienen precalculados al agregar
    
    def eliminar_producto():
        """Elimina el producto seleccionado de la lista (usando índice del tree)"""
        global lista_productos_factura
        selection = tree_productos.selection()
        if not selection:
            messagebox.showwarning("Selección", "Seleccione un producto para eliminar.")
            return

        iid = selection[0]
        try:
            idx = int(iid)
        except Exception:
            idx = None

        if idx is None or idx < 0 or idx >= len(lista_productos_factura):
            messagebox.showerror("Error", "No se pudo identificar el elemento seleccionado.")
            return

        p = lista_productos_factura[idx]
        desc = _descripcion_pt(p.get('producto'), p.get('terminacion'))
        pres = p.get('presentacion')

        if messagebox.askyesno("Confirmar", f"¿Eliminar '{desc}' ({pres}) de la lista?"):
            del lista_productos_factura[idx]
            cargar_productos_en_tree()
            messagebox.showinfo("Eliminado", f"Producto eliminado.")

    def editar_producto():
        """Editar con autocompletado y combobox: código, producto, terminación, presentación y cantidad"""
        global lista_productos_factura, _last_editar_producto
        
        # Debounce: no ejecutar si fue hace menos de 400ms
        ahora = time.time()
        if ahora - _last_editar_producto < 0.4:
            return
        _last_editar_producto = ahora
        
        selection = tree_productos.selection()
        if not selection:
            messagebox.showwarning("Editar", "Seleccione un producto para editar.")
            return
        try:
            idx = int(selection[0])
        except Exception:
            messagebox.showerror("Editar", "No se pudo identificar el elemento seleccionado.")
            return
        if idx < 0 or idx >= len(lista_productos_factura):
            messagebox.showerror("Editar", "Índice fuera de rango.")
            return
            datos = lista_productos_factura[idx]

            # Crear ventana de edición
            win_edit = tk.Toplevel(ventana_lista)
            win_edit.title("Editar Producto de Factura")
            win_edit.grab_set()
            win_edit.transient(ventana_lista)
            
            # Aplicar icono
            aplicar_icono(win_edit)
            
            try:
                centrar_ventana_adaptativa(win_edit, 560, 420)
            except Exception:
                win_edit.geometry("560x420")

            frm = ttk.Frame(win_edit, padding=15)
            frm.pack(fill="both", expand=True)

            # Variables locales
            var_codigo = tk.StringVar(value=str(datos.get('codigo', '')))
            var_producto = tk.StringVar(value=str(datos.get('producto', '')))
            var_terminacion = tk.StringVar(value=str(datos.get('terminacion', '')))
            var_presentacion = tk.StringVar(value=str(datos.get('presentacion', '')))
            var_cantidad = tk.IntVar(value=int(str(datos.get('cantidad', '1')) or 1))

            # Filas de campos
            ttk.Label(frm, text="Código:").grid(row=0, column=0, sticky="w", pady=4, padx=(0,8))
            try:
                lista_codigos = sorted(set([c for c in codigos if c and str(c) != 'nan']))
            except Exception:
                lista_codigos = []
            e_codigo = AutoCompleteEntry(frm, lista_codigos, callback=lambda: on_codigo_autocomplete())
            e_codigo.insert(0, var_codigo.get())
            e_codigo.grid(row=0, column=1, sticky="we", pady=4)

            ttk.Label(frm, text="Producto:").grid(row=1, column=0, sticky="w", pady=4, padx=(0,8))
            producto_vals = ['Excello Premium', 'Laca', 'Esmalte Kem', 'Excello VOC', 'Master Paint', 'Tinte al Thinner', 'Super Paint', 'Esmalte Multiuso', 'Excello Pastel', 'Texturizado', 'Water Blocking', 'Kem Aqua', 'Emerald', 'Monocapa', 'Uretano', 'Airpuretec', 'Kem Pro', 'Sanitizing', 'Industrial', 'h&c silicone-acrylic', 'h&c heavy-shield', 'promar® 200 voc', 'promar® 400 voc', 'pro industrial dtm', 'armoseal 1000hs', 'armoseal t-p', 'scuff tuff-wb', 'Macropoxy 646', 'Tile Clad', 'Sher-loxane 800', 'Acrolon 7300', 'Dura-plate 235', 'Dura-plate 235 PW', 'Acrolon 218', 'Armorseal HS-PT', 'Hi-Solids PT', 'Hi-Solids 250', 'Armorseal Rexthane', 'Sherplate 600', 'Macropoxy 4600', 'Urethane Alkyd', 'Water-Base Catalyzed']
            cb_producto = ttk.Combobox(frm, textvariable=var_producto, values=producto_vals, state='readonly')
            cb_producto.grid(row=1, column=1, sticky="we", pady=4)

            ttk.Label(frm, text="Terminación:").grid(row=2, column=0, sticky="w", pady=4, padx=(0,8))
            cb_terminacion = ttk.Combobox(frm, textvariable=var_terminacion, state='readonly')
            cb_terminacion.grid(row=2, column=1, sticky="we", pady=4)

            ttk.Label(frm, text="Presentación:").grid(row=3, column=0, sticky="w", pady=4, padx=(0,8))
            cb_presentacion = ttk.Combobox(frm, textvariable=var_presentacion, state='readonly')
            cb_presentacion.grid(row=3, column=1, sticky="we", pady=4)

            ttk.Label(frm, text="Cantidad:").grid(row=4, column=0, sticky="w", pady=4, padx=(0,8))
            sp_cantidad = ttk.Spinbox(frm, from_=1, to=100, textvariable=var_cantidad, width=10)
            sp_cantidad.grid(row=4, column=1, sticky="w", pady=4)

            # Expandir columna de entradas
            frm.columnconfigure(1, weight=1)

            aviso_lbl = ttk.Label(frm, text="" , foreground="#d32f2f")
            aviso_lbl.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6,2))

            # Reglas locales de terminaciones/presentaciones
            def actualizar_terminaciones_local(*a):
                try:
                    producto = (var_producto.get() or '').lower()
                    base_local = (datos.get('base') or '').lower()
                    terminaciones_validas = []
                    for key, terminaciones in TERMINACIONES_POR_PRODUCTO.items():
                        if key in producto:
                            terminaciones_validas = terminaciones
                            break
                    if not terminaciones_validas:
                        terminaciones_validas = ['Mate', 'Satin', 'Semigloss', 'Semimate', 'Gloss', 'Brillo', 
                                                "N/A", "ESPECIAL", "CLARO", "INTERMEDIO", "MADERA", "PERLADO", "METALICO", "SEMISATIN"]
                    if 'excello premium' in producto:
                        es_ultra_deep_ii = any(k in base_local for k in ['ultra deep ii', 'ultradeep ii', 'ultra-deep ii', 'ultra deep 2'])
                        es_ultra_deep = ('ultra deep' in base_local)
                        if es_ultra_deep or es_ultra_deep_ii:
                            terminaciones_validas = ['Semisatin']
                    cb_terminacion['values'] = terminaciones_validas
                    if var_terminacion.get() not in terminaciones_validas:
                        var_terminacion.set('')
                    if len(terminaciones_validas) == 1:
                        var_terminacion.set(terminaciones_validas[0])
                except Exception:
                    pass

            def actualizar_presentaciones_local(*a):
                try:
                    producto = (var_producto.get() or '').lower()
                    presentaciones_disponibles = ['Cuarto', 'Medio Galón', 'Galón', 'Cubeta']
                    if any(palabra in producto for palabra in ['laca', 'industrial', 'monocapa', 'uretano']):
                        presentaciones_disponibles = ['1/8', 'Cuarto', 'Medio Galón', 'Galón']
                    if 'esmalte kem' in producto:
                        presentaciones_disponibles = ['1/8', 'Cuarto', 'Medio Galón', 'Galón']
                    if 'tinte al thinner' in producto:
                        presentaciones_disponibles = ['1/8', 'Cuarto', 'Medio Galón', 'Galón']
                    p = producto
                    if 'macropoxy 646' in p or 'tile clad' in p:
                        presentaciones_disponibles = ['Galón', 'Cubeta']
                    elif 'sher-loxane 800' in p:
                        presentaciones_disponibles = ['Galón', '4 Galones']
                    elif 'acrolon 7300' in p:
                        presentaciones_disponibles = ['Galón', '2.38 Galones']
                    elif 'dura-plate 235 pw' in p:
                        presentaciones_disponibles = ['Galón', '4 Galones']
                    elif any(k in p for k in [
                        'acrolon 218','armorseal rexthane','armorseal hs-pt','hi-solids pt','hi-solids 250','sherplate 600','macropoxy 4600','water-base catalyzed'
                    ]):
                        presentaciones_disponibles = ['Galón']
                    cb_presentacion['values'] = presentaciones_disponibles
                    if var_presentacion.get() and var_presentacion.get() not in presentaciones_disponibles:
                        var_presentacion.set('')
                except Exception:
                    pass

            # Autocompletado de código rellena nombre/base/ubicación si existe
            def on_codigo_autocomplete():
                try:
                    cval = e_codigo.get().strip()
                    if cval in data_por_codigo:
                        info = data_por_codigo[cval]
                        # Descripción al editar no es campo explícito; mantenemos la existente
                        datos['base'] = info.get('base', datos.get('base'))
                        # Cambiar producto a partir del nombre detectado puede ser confuso; conservar producto actual
                except Exception:
                    pass

            # Enlazar cambios
            cb_producto.bind('<<ComboboxSelected>>', lambda e: (actualizar_terminaciones_local(), actualizar_presentaciones_local()))
            actualizar_terminaciones_local(); actualizar_presentaciones_local()

            def guardar_cambios():
                codigo_n = e_codigo.get().strip()
                producto_n = var_producto.get().strip()
                terminacion_n = var_terminacion.get().strip()
                presentacion_n = var_presentacion.get().strip()
                cantidad_n = str(var_cantidad.get()).strip()
                if not codigo_n:
                    aviso_lbl.config(text="Código requerido")
                    return
                if not producto_n:
                    aviso_lbl.config(text="Producto requerido")
                    return
                if not cantidad_n.isdigit():
                    aviso_lbl.config(text="Cantidad debe ser numérica")
                    return
                # Actualizar estructura
                datos['codigo'] = codigo_n
                datos['producto'] = producto_n
                datos['terminacion'] = terminacion_n
                datos['presentacion'] = presentacion_n
                datos['cantidad'] = int(cantidad_n)
                # Recalcular código base si procede
                try:
                    base_sel = datos.get('base')
                    pres_sel = datos.get('presentacion')
                    cb = ""
                    if base_sel and producto_n and terminacion_n:
                        cb_raw = obtener_codigo_base(base_sel, producto_n, terminacion_n)
                        if cb_raw not in ("No encontrado", "No Aplica", "Error", None, ""):
                            suf = obtener_sufijo_presentacion(pres_sel, producto_n, base_sel)
                            if suf:
                                cb_raw = f"{cb_raw}{suf}"
                            cb = cb_raw
                    datos['codigo_base'] = cb
                except Exception:
                    pass
                cargar_productos_en_tree()
                win_edit.destroy()

            ttk.Button(frm, text="💾 Guardar", command=guardar_cambios, bootstyle="success").grid(row=6, column=0, pady=12, sticky="w")
            ttk.Button(frm, text="❌ Cancelar", command=win_edit.destroy, bootstyle="secondary").grid(row=6, column=1, pady=12, sticky="e")

            e_codigo.focus()
            win_edit.bind('<Return>', lambda e: guardar_cambios())
            win_edit.bind('<Escape>', lambda e: win_edit.destroy())

    # Doble clic para editar
    tree_productos.bind('<Double-1>', lambda e: editar_producto())

    def copiar_codigos_base_columna():
        """Copia al portapapeles solo la columna de código base (una por línea)."""
        try:
            filas = []
            for p in lista_productos_factura:
                cb = p.get('codigo_base') or _codigo_base_producto(p) or ''
                if cb:
                    filas.append(cb)
            if not filas:
                messagebox.showinfo("Copiar Códigos", "No hay datos para copiar.")
                return
            texto = "\n".join(filas)
            ventana_lista.clipboard_clear()
            ventana_lista.clipboard_append(texto)
        except Exception as e:
            messagebox.showerror("Copiar Códigos", f"No se pudieron copiar los datos: {e}")
    
    def enviar_todos_a_lista_espera():
        """Envía todos los productos a la lista de espera"""
        global lista_productos_factura
        
        import time
        t_inicio = time.time()
        debug_log(f"⏱️ [ENVIO] Iniciando envío de {len(lista_productos_factura)} items...")
        
        # Validar ID de factura silenciosamente
        id_factura = id_factura_var.get().strip()
        if not id_factura:
            return  # Salir silenciosamente
        
        prioridad = prioridad_var.get()
        
        # Envío directo sin confirmación para velocidad
        # Tomar un snapshot inmutable de la lista ANTES de limpiarla para evitar carrera
        items_a_enviar = [p.copy() for p in lista_productos_factura]
        
        # 🔍 DEBUG: Mostrar contenido exacto de lista_productos_factura
        debug_log(f"\n{'='*70}")
        debug_log(f"[ENVIO-DEBUG] lista_productos_factura tiene {len(lista_productos_factura)} items")
        for idx, item in enumerate(lista_productos_factura):
            debug_log(f"[ENVIO-DEBUG] [{idx}] Código: {item.get('codigo')}, Producto: {item.get('producto')}, Cantidad: {item.get('cantidad')}")
        debug_log(f"[ENVIO-DEBUG] items_a_enviar tiene {len(items_a_enviar)} items")
        for idx, item in enumerate(items_a_enviar):
            debug_log(f"[ENVIO-DEBUG] [{idx}] Código: {item.get('codigo')}, Producto: {item.get('producto')}, Cantidad: {item.get('cantidad')}")
        debug_log(f"{'='*70}\n")
        
        # Enviar todo en un solo commit a la tabla de pedidos (SÍNCRONO para garantizar)
        try:
            debug_log(f"📋 Iniciando envío en lote de {len(items_a_enviar)} items para factura {id_factura}")
            t_before_bulk = time.time()
            debug_log(f"⏱️ [ENVIO] Antes de agregar_lista_a_espera_bulk: {t_before_bulk - t_inicio:.3f}s")
            
            ok = agregar_lista_a_espera_bulk(items_a_enviar, id_factura, prioridad)
            
            t_after_bulk = time.time()
            debug_log(f"⏱️ [ENVIO] Después de agregar_lista_a_espera_bulk: {t_after_bulk - t_before_bulk:.3f}s")
            
            if ok:
                debug_log(f"✅ Lote enviado exitosamente: {len(items_a_enviar)} items")
            else:
                debug_log(f"⚠️ Lote no se envió correctamente")
        except Exception as e:
            debug_log(f"❌ Error enviando lote: {e}")

        # Limpiar inmediatamente la lista global y la UI
        lista_productos_factura = []
        try:
            cargar_productos_en_tree()
        except Exception:
            pass

        t_before_destroy = time.time()
        # Cerrar ventana
        ventana_lista.destroy()
        t_after_destroy = time.time()
        
        t_total = time.time() - t_inicio
        debug_log(f"⏱️ [ENVIO] TOTAL: {t_total:.3f}s | Destroy: {t_after_destroy - t_before_destroy:.3f}s")
    
    # Cargar productos iniciales
    cargar_productos_en_tree()
    
    # Menú contextual en el Treeview
    menu_ctx = tk.Menu(ventana_lista, tearoff=0)
    menu_ctx.add_command(label="✏️ Editar", command=editar_producto)
    menu_ctx.add_command(label="❌ Eliminar", command=eliminar_producto)
    menu_ctx.add_separator()
    menu_ctx.add_command(label="📋 Copiar Códigos Base (columna)", command=copiar_codigos_base_columna)
    menu_ctx.add_separator()
    menu_ctx.add_command(label="🔄 Limpiar Lista", command=lambda: limpiar_lista_factura())
    menu_ctx.add_command(label="🚀 Enviar Todos", command=enviar_todos_a_lista_espera)
    menu_ctx.add_separator()
    menu_ctx.add_command(label="Cerrar", command=ventana_lista.destroy)

    def on_right_click(event):
        try:
            rowid = tree_productos.identify_row(event.y)
            if rowid:
                tree_productos.selection_set(rowid)
            tree_productos.focus(rowid)
        except Exception:
            pass
        menu_ctx.tk_popup(event.x_root, event.y_root)

    tree_productos.bind('<Button-3>', on_right_click)

    # Frame para botones (solo Enviar y Limpiar, como solicitado)
    frame_botones = ttk.Frame(ventana_lista)
    frame_botones.pack(fill="x", padx=20, pady=15)

    def limpiar_lista_factura():
        """Limpia todos los productos de la lista y refresca la vista"""
        global lista_productos_factura
        if not lista_productos_factura:
            cargar_productos_en_tree()
            return
        lista_productos_factura = []  # Asignar nueva lista vacía
        cargar_productos_en_tree()
        try:
            messagebox.showinfo("Lista", "Lista de productos vaciada.")
        except Exception:
            pass
    
    ttk.Button(frame_botones, text="🔄 Limpiar Lista", 
              command=limpiar_lista_factura, 
              bootstyle="warning").pack(side="left", padx=8, pady=5)
    
    ttk.Button(frame_botones, text="📋 Enviar Todos a Lista de Espera", 
              command=enviar_todos_a_lista_espera, 
              bootstyle="success").pack(side="right", padx=8, pady=5)
    
    # Bind Enter para enviar rápido con teclado en ventana de gestión
    ventana_lista.bind('<Return>', lambda e: enviar_todos_a_lista_espera())

# === Código Base desde tabla CodigoBase y ReglasCodigo ===
# Caches para acelerar consultas y cómputos repetidos
_CACHE_ROW_CODIGO_BASE = {}
_CACHE_CODIGO_BASE_RESULT = {}
_CACHE_REGLAS_CODIGO = {}

def _buscar_regla_en_bd(producto_norm, terminacion_norm, base_color_norm, prioridad=False):
    """
    Busca una regla en la tabla ReglasCodigo.
    Si prioridad=True, busca por orden de prioridad (mayor primero)
    """
    try:
        cache_key = (producto_norm, terminacion_norm, base_color_norm)
        if cache_key in _CACHE_REGLAS_CODIGO:
            return _CACHE_REGLAS_CODIGO[cache_key]
        
        with get_db_connection() as conn:
            cur = conn.cursor()
            
            if prioridad:
                # Buscar primero exacta con prioridad alta
                cur.execute("""
                    SELECT codigo FROM ReglasCodigo 
                    WHERE producto ILIKE %s AND terminacion ILIKE %s AND base_color ILIKE %s
                    AND activo = TRUE
                    ORDER BY prioridad DESC, id DESC
                    LIMIT 1
                """, (producto_norm, terminacion_norm, base_color_norm))
            else:
                # Búsqueda exacta
                cur.execute("""
                    SELECT codigo FROM ReglasCodigo 
                    WHERE producto ILIKE %s AND terminacion ILIKE %s 
                    AND (base_color ILIKE %s OR base_color IS NULL)
                    AND activo = TRUE
                    ORDER BY base_color DESC NULLS LAST, id DESC
                    LIMIT 1
                """, (producto_norm, terminacion_norm, base_color_norm if base_color_norm else ''))
            
            resultado = cur.fetchone()
            if resultado:
                codigo = resultado[0]
                _CACHE_REGLAS_CODIGO[cache_key] = codigo
                return codigo
        
        _CACHE_REGLAS_CODIGO[cache_key] = None
        return None
        
    except Exception as e:
        debug_log(f"[REGLAS-BD] Error buscando regla: {e}")
        return None

def obtener_codigo_base(base, producto, terminacion):
    try:
        producto_norm_check = (producto or "").strip().lower()
        if any(alias in producto_norm_check for alias in ["texturizado", "exc. texturizado", "exc texturizado", "exc. texdturizado", "exc texdturizado"]):
            return "A44WGBX01-"

        # Buscar fila en caché para evitar abrir conexión por cada consulta
        base_key = (base or "").strip().lower()
        row = _CACHE_ROW_CODIGO_BASE.get(base_key)
        if row is None:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT base, tath, tath2, tath3 , flat, satin, sgi , flat2, satin3, sg4, satinkq, flatkp, flatmp, flatcov, flatpas, satinem, sgem, flatsp, satinsp, glossp, flatap, satinap, satinsan   FROM CodigoBase WHERE base ILIKE %s", (base,))
                row = cur.fetchone()
            # Cachear tanto si existe como si no, para evitar múltiples consultas fallidas
            _CACHE_ROW_CODIGO_BASE[base_key] = row

        if not row:
            return "No encontrado"

        _, tath, tath2, tath3 ,flat, satin, sgi , flat2, satin3, sg4, satinkq, flatkp, flatmp, flatcov, flatpas, satinem, sgem, flatsp, satinsp, glossp, flatap, satinap, satinsan = row
        producto = producto.lower()
        terminacion = terminacion.lower()

        es_esmalte = any(p in producto for p in ["esmalte multiuso"])
        es_kempro = any(p in producto for p in ["kem pro"])
        es_kemaqua = any(p in producto for p in ["kem aqua"])
        es_masterpaint = any(p in producto for p in ["master paint"])
        es_pastel = any(p in producto for p in ["excello pastel"])
        es_emerald = any(p in producto for p in ["emerald"])
        es_superpaint = any(p in producto for p in ["super paint"])
        es_superpaintAP = any(p in producto for p in ["airpurtec"])
        es_sanitizing= any(p in producto for p in ["sanitizing"])
        es_laca= any(p in producto for p in ["laca"])
        es_EsmalteIndustrial= any(p in producto for p in ["esmalte kem"])
        es_uretano= any(p in producto for p in ["uretano"])
        es_tintealthinner= any(p in producto for p in ["tinte al thinner"])
        es_monocapa= any(p in producto for p in ["monocapa"])
        es_excellocov= any(p in producto for p in ["excello voc"])
        es_excellopremium= any(p in producto for p in ["excello premium"])
        es_waterblocking= any(p in producto for p in ["water blocking"])
        es_airpuretec= any(p in producto for p in ["airpuretec"])
        es_hcsiloconeacr= any(p in producto for p in ["h&c silicone-acrylic"])
        es_hcheavyshield= any(p in producto for p in ["h&c heavy-shield"]) 
        es_ProMarEgShel= any(p in producto for p in ["promar® 200 voc"])
        es_ProMarEgShel400= any(p in producto for p in ["promar® 400 voc"]) 
        es_proindustrialDTM= any(p in producto for p in ["pro industrial dtm"])                     
        es_armoseal= any(p in producto for p in ["armoseal 1000hs"])                     
        es_armosealtp= any(p in producto for p in ["armoseal t-p"]) 
        es_scufftuff= any(p in producto for p in ["scuff tuff-wb"]) 
        es_UrethaneAlkyd= any(p in producto for p in ["urethane alkyd"])
        es_industrialenamels= any(p in producto for p in ["industrial enamel"])
        es_macropoxy646= any(p in producto for p in ["macropoxy 646"]) 
        es_sherplate600= any(p in producto for p in ["sherplate 600"])
        es_macropoxy4600= any(p in producto for p in ["macropoxy 4600"]) 
        es_tileclad= any(p in producto for p in ["tile clad"])
        es_acrolon7300= any(p in producto for p in ["acrolon 7300"])
        es_acrolon218= any(p in producto for p in ["acrolon 218"])
        es_ARMORSEAL_HS_PT= any(p in producto for p in ["ARMORSEAL HS-PT"])
        es_HISOLIDS_EP= any(p in producto for p in ["hi-solids PT"])
        es_HISOLIDS_EP250= any(p in producto for p in ["hi-solids 250"])
        es_ARMORSEAL_REXTHANE= any(p in producto for p in ["armorseal rexthane"])
        es_duraplate= any(p in producto for p in ["dura-plate 235 "])
        es_duraplatePW= any(p in producto for p in ["dura-plate PW "])
        es_WATER_BASECATALYZED= any(p in producto for p in ["water-base catalyzed"])
        es_SHER_LOXANE_800 = any(p in producto for p in ["sher-loxane 800"])                


   



        base_color = base_var.get().lower()

        # ============================================================================
        # ESTRATEGIA 1: Consultar tabla ReglasCodigo (nueva)
        # ============================================================================
        codigo_regla = _buscar_regla_en_bd(producto, terminacion, base_color, prioridad=True)
        if codigo_regla:
            debug_log(f"[CODIGO-BASE] Regla encontrada en BD: {producto} | {terminacion} | {base_color} -> {codigo_regla}")
            return codigo_regla

        # ============================================================================
        # ESTRATEGIA 2: Intenta producto_code_generator (si existe)
        # ============================================================================
        try:
            try:
                from product_code_generator import get_product_code, PRODUCT_CODES, SPECIAL_PRODUCTS, MULTI_FINISH_PRODUCTS  # type: ignore
            except Exception:
                get_product_code = None
            product_flags = {
                'es_SHER_LOXANE_800': es_SHER_LOXANE_800,
                'es_WATER_BASECATALYZED': es_WATER_BASECATALYZED,
                'es_duraplatePW': es_duraplatePW,
                'es_duraplate': es_duraplate,
                'es_ARMORSEAL_REXTHANE': es_ARMORSEAL_REXTHANE,
                'es_HISOLIDS_EP250': es_HISOLIDS_EP250,
                'es_HISOLIDS_EP': es_HISOLIDS_EP,
                'es_ARMORSEAL_HS_PT': es_ARMORSEAL_HS_PT,
                'es_acrolon218': es_acrolon218,
                'es_acrolon7300': es_acrolon7300,
                'es_tileclad': es_tileclad,
                'es_macropoxy4600': es_macropoxy4600,
                'es_macropoxy646': es_macropoxy646,
                'es_sherplate600': es_sherplate600,
                'es_airpuretec': es_airpuretec,
                'es_waterblocking': es_waterblocking,
                'es_proindustrialDTM': es_proindustrialDTM,
                'es_scufftuff': es_scufftuff,
                'es_UrethaneAlkyd': es_UrethaneAlkyd,
                'es_industrialenamels': es_industrialenamels,
                'es_hcheavyshield': es_hcheavyshield,
                'es_ProMarEgShel': es_ProMarEgShel,
                'es_ProMarEgShel400': es_ProMarEgShel400,
                'es_armoseal': es_armoseal,
                'es_armosealtp': es_armosealtp,
                'es_hcsiloconeacr': es_hcsiloconeacr,
                'es_kemaqua': es_kemaqua,
                'es_excellocov': es_excellocov,
                'es_laca': es_laca,
                'es_EsmalteIndustrial': es_EsmalteIndustrial,
                'es_tintealthinner': es_tintealthinner,
                'es_esmalte': es_esmalte,
                'es_kempro': es_kempro,
                'es_masterpaint': es_masterpaint,
                'es_pastel': es_pastel,
                'es_emerald': es_emerald,
                'es_superpaint': es_superpaint,
                'es_superpaintAP': es_superpaintAP,
                'es_sanitizing': es_sanitizing,
                'es_uretano': es_uretano,
                'es_monocapa': es_monocapa,
                'es_excellopremium': es_excellopremium,
            }
            variables_map = {
                'tath': tath,
                'tath2': tath2,
                'tath3': tath3,
                'flat': flat,
                'satin': satin,
                'sgi': sgi,
                'flat2': flat2,
                'satin3': satin3,
                'sg4': sg4,
                'satinkq': satinkq,
                'flatkp': flatkp,
                'flatmp': flatmp,
                'flatcov': flatcov,
                'flatpas': flatpas,
                'satinem': satinem,
                'sgem': sgem,
                'flatsp': flatsp,
                'satinsp': satinsp,
                'glossp': glossp,
                'flatap': flatap,
                'satinap': satinap,
                'satinsan': satinsan,
            }
            # Normalizar terminación
            term_norm = (terminacion or '').strip().lower()
            codigo_gen = get_product_code(product_flags, term_norm, base_color, variables=variables_map)
            if isinstance(codigo_gen, str) and codigo_gen not in ("No Aplica", "Error", "No encontrado") and codigo_gen:
                debug_log(f"[CODIGO-BASE] Regla generada por product_code_generator: {codigo_gen}")
                return codigo_gen
        except Exception:
            pass

        # ============================================================================
        # ESTRATEGIA 3: Fallback a lógica hardcodeada (mantener para compatibilidad)
        # ============================================================================
        # Nota: Este bloque es un FALLBACK. Las nuevas reglas deben agregarse a
        # la tabla ReglasCodigo en lugar de modificar este código.

        if es_SHER_LOXANE_800:

            if  terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B80W501-" 
                
                elif base_color == "ultra deep":
                    return "B80T504-"
                    
            else:
                return "No Aplica"        

        if es_WATER_BASECATALYZED:

            if  terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B73W311-" 
                    
            else:
                return "No Aplica"        

        if es_duraplatePW:

            if  terminacion ==  "Semigloss":

                if base_color == "extra white":
                 return "B67WX235-" 
                    
            else:
                return "No Aplica"
          

        if es_duraplate:

             if terminacion ==  "semigloss":

                if base_color == "extra white":
                 return "B67W235-"
                                   
             else:
                return "No Aplica" 

        if es_ARMORSEAL_REXTHANE:

             if terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B65W60-"
                                   
             else:
                return "No Aplica" 

        if es_HISOLIDS_EP250:

             if terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B65WJ311-"
                                   
             else:
                return "No Aplica"      

        if es_HISOLIDS_EP:

            if  terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B65W311-" 
                
                elif base_color == "ultra deep":
                    return "B65T304-"
                    
            else:
                return "No Aplica"



        if es_ARMORSEAL_HS_PT:

            if  terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B65W220-" 
                
                elif base_color == "ultra deep":
                    return "B65T220-"
                    
            else:
                return "No Aplica"



        if es_acrolon218:

            if  terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B65W611-" 
                
                elif base_color == "ultra deep":
                    return "B65T604-"
                    
            elif terminacion == "semigloss":

                if base_color== "extra white":
                  
                  return "B65W651-" 
                
                elif base_color == "ultra deep":
                    return "B65T654-"
            else:
                return "No Aplica"
            

        if es_acrolon7300:

             if terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B65W01301-"
                
                elif base_color == "ultra deep":
                 return "B65T01304-3"
                                                       
             else:
                return "No Aplica" 

        if es_tileclad:

             if terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B62WZ111-"
                
                elif base_color == "ultra deep":
                 return "B62TZ104-"
                                                       
             else:
                return "No Aplica"  
             

        if es_macropoxy4600:

             if terminacion == "semigloss":

                if base_color == "extra white":
                 return "B58WW730-"
                                   
             else:
                return "No Aplica"  
             
        if es_macropoxy646:

             if terminacion == "semigloss":

                if base_color == "extra white":
                 return "B58W610-"
                
                elif base_color == "ultra deep":
                 return "B58T604-"
                                                       
             else:
                return "No Aplica"  

        if es_sherplate600:

             if terminacion == "brillo" or terminacion == "gloss":

                if base_color == "extra white":
                 return "B58W681-"
                               
             else:
                return "No Aplica"             

        if es_kemaqua:

            if terminacion == "satin":
                return satinkq
            else:
                return "No Aplica"

        if es_airpuretec:

            if  terminacion == "mate":

                if base_color == "extra white":
                 return "A86W00061-" 
                
                elif base_color == "deep":
                    return "A86W00063-"
                    
            elif terminacion == "satin":

                if base_color== "extra white":
                  
                  return "A87W00061-" 
                
                elif base_color == "deep":
                    return "A87W00063-"
            else:
                return "No Aplica"
                
        if es_waterblocking:

            if terminacion == "mate":
                return "LX12WDR50-"
            else:
                return "No Aplica"    
            
        if es_excellocov:

            if terminacion == "mate":

                return "A30WDR2651"
            
            elif terminacion == "satin":

                return "A20WDR2651-"
            
            else:
                return "No Aplica"    

        if es_laca:

            if terminacion == "mate":
                return "L15-" 
            elif terminacion == "semimate":
                return "L15-" 
            elif terminacion == "brillo":
                return "L15-" 
            else:
                return "No Aplica"
            

        if es_EsmalteIndustrial:

            if terminacion == "mate":
                return "F300-"
                
            elif terminacion == "semimate":
                return "F300-"
            
            elif terminacion == "brillo":
                return "F300-"
            else:
                return "No Aplica"

        if es_hcsiloconeacr:

            if terminacion == "mate" or terminacion == "satin":

                if base_color == "extra white":
                 return "20.101214-" 
                
                elif base_color == "deep":
                 return "20.102214-" 

                elif base_color == "ultra deep":
                 return "20.103214-"
                                 
                
            else:
                    return "No aplica"
                                 


        if es_proindustrialDTM:

                 if terminacion == "brillo" or terminacion == "gloss":

                     if base_color == "extra white":
                      return "B66W1051-" 
                
                     elif base_color == "ultra deep":
                      return "B66T1054-" 
               
                
                     else:
                          return "No aplica"
                                 
                 else:
                     return "No Aplica"

        if es_scufftuff:

             if terminacion == "mate":

                if base_color == "extra white":
                 return "S23W00051-" 
                
                elif base_color == "ultra deep":
                 return "S23T00154-" 
                
                elif base_color == "deep":
                 return "S23W00153-" 
                
                else:
                    return "No aplica"
                
             elif terminacion == "satin" :
                 return "S24W00051-" 
             
               
             elif terminacion == "semigloss" :
                 return "S26W00051-"                                                
             else:
                return "No Aplica"

        if es_UrethaneAlkyd:

             if terminacion == "brillo":

                if base_color == "extra white":
                 return "B54W00151-"

                
                elif base_color == "ultra deep":
                 return "B54T00154-" 
                                                             
             else:
                return "No Aplica"

        if es_industrialenamels:

             if terminacion == "brillo":

                if base_color == "extra white":
                 return "B54W101-"
                
                elif base_color == "ultra deep":
                 return "B54T101-"
                                                       
             else:
                return "No Aplica"


        if es_hcheavyshield:

                 if terminacion == "brillo" or terminacion == "gloss":

                     if base_color == "extra white":
                      return "35.100214-"  
                
                     elif base_color == "deep":
                      return "35.100314-" 

                     elif base_color == "ultra deep":
                      return "35.100414-"                 
                
                     else:
                          return "No aplica"
                                 
                 else:
                     return "No Aplica"
             
        if  es_ProMarEgShel:

             if terminacion == "satin":

                if base_color == "deep":
                 return "B20W02653-" 
                
                elif base_color == "extra white":
                 return "B20W12651-"                
                
                else:
                    return "No aplica"
                                 
             elif terminacion== "mate":

                if base_color == "ultra deep":
                 return "B30T02654-" 
                
                elif base_color == "extra white":
                 return "B30W02651-"                
                
                elif base_color == "deep":
                 return "B30W02653-"   

                else:
                    return "No Aplica"

             elif terminacion== "semigloss":

                if base_color == "extra white":
                 return "B31W02651-"                
                                                 
             else:

                return "No aplica"

        if  es_ProMarEgShel400:

             if terminacion == "satin":

                if base_color == "extra white":
                 return "B20W04651-" 
 
                else:
                    return "No aplica"


        
        if es_armoseal:

                 if terminacion == "brillo" or terminacion == "gloss":

                     if base_color == "extra white":
                      return "B67W2001-" 
                
                     elif base_color == "ultra deep":
                      return "B67T2004-" 
               
                
                     else:
                          return "No aplica"
                                 
                 else:
                     return "No Aplica"

        if es_armosealtp:

             if terminacion == "semigloss":

                if base_color == "extra white":
                 return "B90T104-" 
                
                elif base_color == "ultra deep":
                 return "B90W111-" 
               
                
                else:
                    return "No aplica"
                                 
             else:
                return "No Aplica"



        if es_uretano:

             if terminacion == "mate":

                if base_color == "extra white":
                 return "ASPPA-" 
                
                elif base_color == "deep" or base_color == "ultra deep":
                 return "ASPPB-" 
                
                else:
                    return "ASPPD-"
                
             elif terminacion == "semimate":

                if base_color == "extra white":
                 return "ASPPA-" 
                
                elif base_color == "deep" or base_color == "ultra deep":
                 return "ASPPB-" 
                
                else:
                    return "ASPPD-"
                
             elif terminacion == "brillo":

                if base_color == "extra white":
                 return "ASPPA-" 
                
                elif base_color == "deep" or base_color == "ultra deep":
                 return "ASPPB-" 
                
                else:
                    return "ASPPD-"                    
             else:
                return "No Aplica"
    
        if es_tintealthinner:

            if terminacion == "claro":
                return tath 
            
            elif terminacion == "intermedio":
                return tath2
            
            elif terminacion== "especial":
                return tath3
            else:
                return "No Aplica"
            
        if es_monocapa:

            if terminacion == "mate":

                if base_color == "extra white":
                 return "ASMCA-" 
                
                elif base_color == "deep" or base_color == "ultra deep":
                 return "ASMCB-" 
                
                else:
                    return "ASMCD-" 
                
            elif terminacion == "semimate":

                if base_color == "extra white":
                 return "ASMCA-" 
                
                elif base_color == "deep" or base_color == "ultra deep":
                 return "ASMCB-" 
                
                else:
                    return "ASMCD-" 
                
            elif terminacion == "brillo":

                if base_color == "extra white":
                 return "ASMCA-" 
                
                elif base_color == "deep" or base_color == "ultra deep":
                 return "ASMCB-" 
                
                else:
                    return "ASMCD-" 
            else:
                return "No Aplica"

        if es_esmalte:

            if terminacion == "mate":
                return flat2
            elif terminacion == "satin":
                return satin3
            elif terminacion == "brillo" or terminacion == "gloss":
                return sg4
            else:
                return "No Aplica"
            
        elif es_kempro:
                
            if terminacion == "mate":
                 return flatkp
            else:
                return "No Aplica"
            
        elif es_masterpaint:
                
            if terminacion == "mate":
                 return flatmp
            else:
                return "No Aplica"
            
        elif es_pastel:
                
            if terminacion == "mate":
                 return flatpas
            else:
                return "No Aplica"   
            
        elif es_emerald: 
            # Códigos específicos para Esmerald según base y terminación
            if terminacion == "satin":
                return "K37W02751-"
            elif terminacion == "semigloss":
                # Códigos específicos según la base
                if "extra white" in base_color:
                    return "K38W02751-"
                elif "ultradeep" in base_color or "ultra deep" in base_color or "ultra-deep" in base_color:
                    return "K38T01754-"
                elif "deep" in base_color and "ultra" not in base_color:
                    return "K38W01753-"
                else:
                    return "K38W02751-"  # Default
            else:
                return "No Aplica"
            
        elif es_superpaint: 
                    
            if terminacion == "mate":
                return flatsp
            
            elif terminacion == "satin":
                return satinsp    

            elif  terminacion == "brillo" or terminacion == "gloss":
                return glossp
            else:
                return "No Aplica"           
            
        elif es_superpaintAP: 
                    
            if terminacion == "mate":
                return flatap
            elif terminacion == "satin":
                return satinap
            else:
                return "No Aplica" 
            
        elif es_sanitizing:
                
            if terminacion == "satin":
                 return satinsan
            else:
                return "No Aplica"
            
        elif es_excellopremium: 
            # Reglas especiales Excello Premium
            # 1) Ultra Deep II -> código PP4-
            es_ultra_deep_ii = any(k in base_color for k in ["ultra deep ii", "ultradeep ii", "ultra-deep ii", "ultra deep 2"])
            if es_ultra_deep_ii:
                return "PP4-"

            # 2) Ultra Deep (no II) -> solo Semisatin con código A27WDR03-
            es_ultra_deep = ("ultra deep" in base_color) and not es_ultra_deep_ii
            if es_ultra_deep:
                if terminacion == "semisatin":
                    return "A27WDR03-"
                else:
                    return "No Aplica"

            # 3) Resto de mapeos Excello Premium
            if terminacion == "mate":
                return flat
            elif terminacion == "satin":
                return satin
            elif terminacion == "semigloss":
                return sgi
            else:
                return "No Aplica"   
                                  
        # Fallback: retornar códigos base según terminación si ningún producto específico coincidió
        if terminacion == "mate" or terminacion == "flat":
            return flat if flat else "No Aplica"
        elif terminacion == "satin" or terminacion == "satín":
            return satin if satin else "No Aplica"
        elif terminacion == "semigloss" or terminacion == "sgi":
            return sgi if sgi else "No Aplica"
        else:
            return "No Aplica"
        
    except Exception as e:
        return "Error"
    
     

def mostrar_codigo_base():
    global codigo_base_actual

    base = base_var.get()
    producto = producto_var.get()
    terminacion = terminacion_var.get()
    presentacion = presentacion_var.get()

    requiere_terminacion = not es_producto_texturizado(producto)
    if not base or not producto or (requiere_terminacion and not terminacion):
        aviso_var.set("Completa todos los campos")
        return

    # Obtener código base
    resultado = obtener_codigo_base(base, producto, terminacion or "")
    
    # Agregar sufijo de presentación si está seleccionada
    if presentacion and resultado != "No encontrado" and resultado != "No Aplica":
        sufijo_presentacion = obtener_sufijo_presentacion(presentacion, producto, base)
        if sufijo_presentacion:
            resultado += sufijo_presentacion

    app.clipboard_clear()
    app.clipboard_append(resultado)
    aviso_var.set("Código facturación copiado en el portapapeles")

    # Guardamos solo para vista previa
    codigo_base_actual = resultado
    codigo_base_var.set(resultado)

    actualizar_vista()
    limpiar_mensaje_despues(3000)

def actualizar_terminaciones(*args):
    """Actualiza las terminaciones disponibles según el producto seleccionado"""
    producto = producto_var.get().lower()
    base = base_var.get().lower()
    
    # Buscar terminaciones válidas para el producto
    terminaciones_validas = []
    for key, terminaciones in TERMINACIONES_POR_PRODUCTO.items():
        if key in producto:
            terminaciones_validas = terminaciones
            break
    
    # Si no se encuentra el producto, usar todas las terminaciones
    if not terminaciones_validas:
        terminaciones_validas = ['Mate', 'Satin', 'Semigloss', 'Semimate', 'Gloss', 'Brillo', 
                                "N/A", "ESPECIAL", "CLARO", "INTERMEDIO", "MADERA", "PERLADO", "METALICO"]
    
    # Lógica especial para Excello Premium con bases Ultra Deep / Ultra Deep II
    if 'excello premium' in producto:
        es_ultra_deep_ii = any(k in base for k in ['ultra deep ii', 'ultradeep ii', 'ultra-deep ii', 'ultra deep 2'])
        es_ultra_deep = ('ultra deep' in base)
        if es_ultra_deep or es_ultra_deep_ii:
            # Solo permitir Semisatin en ambas bases
            terminaciones_validas = ['Semisatin']

    # Actualizar el combobox
    terminaciones_combobox['values'] = terminaciones_validas
    
    # Limpiar la selección actual si no es válida
    terminacion_actual = terminacion_var.get()
    if terminacion_actual and terminacion_actual not in terminaciones_validas:
        terminacion_var.set('')
    
    # Si solo hay una terminación válida, seleccionarla automáticamente
    if len(terminaciones_validas) == 1:
        terminacion_var.set(terminaciones_validas[0])
    
    # Actualizar vista previa
    actualizar_vista()    


# Agrupa los botones en un LabelFrame moderno


# Crear estilo personalizado para botones y LabelFrame
style = ttk.Style()
# Botones azul claro, texto/iconos blancos
style.configure("BotonGrande.TButton", font=("Segoe UI", 11, "bold"), foreground="#ffffff", background="#64B5F6", padding=8, borderwidth=1)
style.map("BotonGrande.TButton", background=[("active", "#42A5F5"), ("pressed", "#2196F3")])
# Botón Enviar con azul claro consistente
style.configure("BotonImprimir.TButton", font=("Segoe UI", 11, "bold"), foreground="#ffffff", background="#64B5F6", padding=8, borderwidth=1)
style.map("BotonImprimir.TButton", background=[("active", "#6FBBF9"), ("pressed", "#60B0F1")])
# Botones especiales para lista (verde)
style.configure("BotonEspecial.TButton", font=("Segoe UI", 10, "bold"), foreground="#ffffff", background="#28a745", padding=6, borderwidth=1)
# LabelFrame y título fondo blanco, título azul oscuro
style.configure("Acciones.TLabelframe", background="#fff", borderwidth=2, relief="groove")
style.configure("Acciones.TLabelframe.Label", font=("Segoe UI", 12, "bold"), foreground="#222c3c", background="#fff")

# Panel de botones debajo del formulario en left_panel
frame_botones = tk.LabelFrame(left_panel, text="Acciones", padx=16, pady=16)
botones_row = 2 if not es_pantalla_pequena else 1
frame_botones.grid(row=botones_row, column=0, columnspan=2, sticky="ew", padx=0, pady=(10, 0))

btn_style = {"width": 14, "style": "BotonGrande.TButton"}

# Primera fila de botones
btn_limpiar = ttk.Button(frame_botones, text="🧹 Limpiar", command=limpiar_campos, bootstyle="info", **btn_style)
btn_limpiar.grid(row=0, column=0, padx=8, pady=10, sticky='ew')

btn_codigo = ttk.Button(frame_botones, text="🔧 Custom", command=abrir_ventana_personalizados, bootstyle="info", **btn_style)
btn_codigo.grid(row=0, column=1, padx=8, pady=10, sticky='ew')

btn_ficha = ttk.Button(frame_botones, text="📜 Historial", command=abrir_historial_impresiones, bootstyle="info", **btn_style)
btn_ficha.grid(row=0, column=2, padx=8, pady=10, sticky='ew')

btn_personalizar = ttk.Button(frame_botones, text="📋 Código", command=mostrar_codigo_base, bootstyle="info", **btn_style)
btn_personalizar.grid(row=0, column=3, padx=8, pady=10, sticky='ew')

# Segunda fila de botones
btn_agregar_lista = ttk.Button(frame_botones, text="➕ Agregar a Lista", command=lambda: agregar_producto_a_lista(), bootstyle="success", width=20)
btn_agregar_lista.grid(row=1, column=0, columnspan=4, padx=5, pady=5, sticky='ew')

for i in range(4):
    frame_botones.columnconfigure(i, weight=1)

# Actualizar vista (ahora es un pass)
actualizar_vista()

# Inicializar sugerencia de código base si hay datos cargados
try:
    actualizar_codigo_base_sugerido()
except Exception:
    pass

# Inicializar presentaciones
actualizar_presentaciones()

# Atajo de teclado: Ctrl+H para abrir historial
try:
    app.bind('<Control-h>', lambda e: abrir_historial_impresiones())
except Exception:
    pass

# Crear la interfaz de productos personalizados en la pestaña
crear_interfaz_personalizados()

# Crear la interfaz de cola de espera en la pestaña
crear_interfaz_cola_espera()

def main_toplevel(parent=None, on_close_callback=None):
    """
    Abre LabelsApp como una ventana Toplevel subordinada del Dashboard.
    Permite que el Dashboard mantenga su mainloop sin conflictos.
    """
    global app
    
    try:
        # Precarga de CodigoBase al startup (evita consultas posteriores)
        threading.Thread(target=precarga_codigo_base, daemon=True).start()
        
        # Si se proporciona un parent, trabajar con Toplevel
        if parent:
            try:
                # La interfaz ya está creada en app global, solo mostrarla
                app.deiconify()
                
                # Configurar callback de cierre
                def on_close():
                    try:
                        app.withdraw()
                    except:
                        pass
                    if on_close_callback:
                        on_close_callback()
                
                # Registrar el callback de cierre en la ventana
                app.protocol("WM_DELETE_WINDOW", on_close)
                
                # NO ejecutar mainloop si viene como Toplevel del Dashboard
                # El Dashboard mantendrá su propio mainloop
                
            except Exception as e:
                print(f"Error en main_toplevel: {e}")
                if on_close_callback:
                    on_close_callback()
                raise
        else:
            # Modo independiente (fallback)
            try:
                app.deiconify()
            except:
                pass
            app.mainloop()
            
    except Exception as e:
        messagebox.showerror("Error", f"Error inesperado al ejecutar LabelsApp:\n{e}")
        if on_close_callback:
            on_close_callback()

def main():
    """Función principal de la aplicación"""
    try:
        # Precarga de CodigoBase al startup (evita consultas posteriores)
        threading.Thread(target=precarga_codigo_base, daemon=True).start()
        
        # Asegurar que el root esté visible tras el login
        try:
            app.deiconify()
        except Exception:
            pass
        app.mainloop()
    except Exception as e:
        messagebox.showerror("Error", f"Error inesperado al ejecutar la aplicación:\n{e}")

if __name__ == "__main__":
    main()

