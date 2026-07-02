# -*- coding: utf-8 -*-
import asyncio
import select

from fastapi import FastAPI, HTTPException, Depends, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import json
import logging
from datetime import date, datetime, timedelta
import csv
import time
import random
import string
import os
import re
import unicodedata
from threading import Lock
from collections import defaultdict
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from urllib.parse import urlencode, quote
from urllib.request import Request as UrlRequest, urlopen

class FormulaNormalCreate(BaseModel):
    codigo_color: str
    id_colorante: str
    oz: float = 0
    x32s: float = 0
    x64s: float = 0
    x128s: float = 0
    tipo: str = "galon"

    class Config:
        # Permitir alias para campos que empiezan con números
        allow_population_by_field_name = True
        
        schema_extra = {
            "properties": {
                "_32s": {"type": "number", "default": 0},
                "_64s": {"type": "number", "default": 0}, 
                "_128s": {"type": "number", "default": 0}
            }
        }

# Modelos Pydantic
class SucursalUpdate(BaseModel):
    nombre: str = ""
    direccion: str = ""
    telefono: str = ""

class EmpleadoUpdate(BaseModel):
    nombre_completo: str = None
    email: str = None
    rol: str = None
    sucursal_id: int = None
    telefono: str = None
    codigo_empleado: str = None
    activo: bool = True


class MaintenanceAssetCreate(BaseModel):
    codigo: str
    nombre: str
    categoria: str = "General"
    sucursal_id: Optional[int] = None
    ubicacion_detalle: Optional[str] = ""
    descripcion: Optional[str] = ""
    estado_operativo: str = "Operativo"
    frecuencia_dias: int = 90
    ultimo_mantenimiento: Optional[date] = None
    proximo_mantenimiento: Optional[date] = None
    responsable_usuario_id: Optional[int] = None
    activo: bool = True


class MaintenanceAssetUpdate(BaseModel):
    nombre: Optional[str] = None
    categoria: Optional[str] = None
    sucursal_id: Optional[int] = None
    ubicacion_detalle: Optional[str] = None
    descripcion: Optional[str] = None
    estado_operativo: Optional[str] = None
    frecuencia_dias: Optional[int] = None
    ultimo_mantenimiento: Optional[date] = None
    proximo_mantenimiento: Optional[date] = None
    responsable_usuario_id: Optional[int] = None
    activo: Optional[bool] = None


class MaintenanceWorkOrderCreate(BaseModel):
    activo_id: int
    tipo: str = "Preventivo"
    titulo: str
    descripcion: Optional[str] = ""
    prioridad: str = "Media"
    estado: str = "Pendiente"
    programado_para: Optional[date] = None
    asignado_usuario_id: Optional[int] = None
    creado_por_usuario_id: Optional[int] = None


class MaintenanceWorkOrderUpdate(BaseModel):
    tipo: Optional[str] = None
    titulo: Optional[str] = None
    descripcion: Optional[str] = None
    prioridad: Optional[str] = None
    estado: Optional[str] = None
    programado_para: Optional[date] = None
    asignado_usuario_id: Optional[int] = None


class LabelsAppItem(BaseModel):
    codigo: str
    descripcion: str = ""
    producto: str
    terminacion: str = ""
    presentacion: str = ""
    cantidad: int = 1
    base: str = ""
    ubicacion: str = ""
    codigo_base: str = ""
    prioridad: Optional[str] = None


class LabelsAppSendRequest(BaseModel):
    id_factura: str
    id_cliente: Optional[str] = None
    prioridad: str = "Media"
    username: Optional[str] = None
    usuario_id: Optional[int] = None
    sucursal: Optional[str] = None
    operador: Optional[str] = None
    items: List[LabelsAppItem]


FORMULA_SOURCE_TABLES = [
    "presentacion",
    "formulas_cce_g",
    "formulas_cce_c",
    "formulas_cce_qt",
    "formulas_bacc_g",
    "formulas_bacc_c",
    "formulas_bacc_qt",
]

QUEUE_CACHE_TTL_SEC = 2.0
_labelsapp_queue_cache = {
    "live": {},
    "pending": {},
}
_labelsapp_queue_cache_lock = Lock()
USAGE_METRICS_CACHE_TTL_SEC = 90.0
_labels_usage_metrics_cache: Dict[tuple, Dict[str, Any]] = {}
_labels_usage_metrics_cache_lock = Lock()
_labels_usage_metrics_fetch_locks: Dict[tuple, Lock] = {}
_labels_usage_metrics_fetch_locks_guard = Lock()
LABELSAPP_PRODUCTS_CACHE_TTL_SEC = 6.0
_labelsapp_products_cache: Dict[tuple, Dict[str, Any]] = {}
_labelsapp_products_cache_lock = Lock()
_labelsapp_products_fetch_locks: Dict[tuple, Lock] = {}
_labelsapp_products_fetch_locks_guard = Lock()
# Locks por tabla: si dos requests piden la misma sucursal en paralelo solo uno
# pega a la DB, pero requests para sucursales distintas no se serializan entre si.
# Un solo Lock global hace que el dashboard de analistas (fanout a 13 sucursales)
# encole los requests y el cliente abandone con 499 a los ~4.5s.
_labelsapp_live_fetch_locks: Dict[str, Lock] = {}
_labelsapp_pending_fetch_locks: Dict[str, Lock] = {}
_labelsapp_fetch_locks_guard = Lock()


def _get_table_lock(registry: Dict[str, Lock], table_name: str) -> Lock:
    lock = registry.get(table_name)
    if lock is not None:
        return lock
    with _labelsapp_fetch_locks_guard:
        lock = registry.get(table_name)
        if lock is None:
            lock = Lock()
            registry[table_name] = lock
        return lock


def _queue_cache_get(kind: str, key):
    now = time.time()
    with _labelsapp_queue_cache_lock:
        bucket = _labelsapp_queue_cache.get(kind, {})
        entry = bucket.get(key)
        if not entry:
            return None
        if (now - float(entry.get("ts", 0))) > QUEUE_CACHE_TTL_SEC:
            try:
                del bucket[key]
            except Exception:
                pass
            return None
        return entry.get("items")


def _queue_cache_set(kind: str, key, items):
    with _labelsapp_queue_cache_lock:
        bucket = _labelsapp_queue_cache.setdefault(kind, {})
        bucket[key] = {
            "ts": time.time(),
            "items": items,
        }


def _usage_metrics_cache_get(key: tuple):
    now = time.time()
    with _labels_usage_metrics_cache_lock:
        entry = _labels_usage_metrics_cache.get(key)
        if not entry:
            return None
        if (now - float(entry.get("ts", 0))) > USAGE_METRICS_CACHE_TTL_SEC:
            try:
                del _labels_usage_metrics_cache[key]
            except Exception:
                pass
            return None
        return entry.get("data")


def _usage_metrics_cache_set(key: tuple, data: dict) -> None:
    with _labels_usage_metrics_cache_lock:
        _labels_usage_metrics_cache[key] = {
            "ts": time.time(),
            "data": data,
        }


def _get_usage_metrics_lock(key: tuple) -> Lock:
    lock = _labels_usage_metrics_fetch_locks.get(key)
    if lock is not None:
        return lock
    with _labels_usage_metrics_fetch_locks_guard:
        lock = _labels_usage_metrics_fetch_locks.get(key)
        if lock is None:
            lock = Lock()
            _labels_usage_metrics_fetch_locks[key] = lock
        return lock


def _products_cache_get(key: tuple):
    now = time.time()
    with _labelsapp_products_cache_lock:
        entry = _labelsapp_products_cache.get(key)
        if not entry:
            return None
        if (now - float(entry.get("ts", 0))) > LABELSAPP_PRODUCTS_CACHE_TTL_SEC:
            try:
                del _labelsapp_products_cache[key]
            except Exception:
                pass
            return None
        return entry.get("data")


def _products_cache_set(key: tuple, data: dict) -> None:
    with _labelsapp_products_cache_lock:
        _labelsapp_products_cache[key] = {
            "ts": time.time(),
            "data": data,
        }


def _products_cache_invalidate_sucursal(sucursal_slug: str) -> None:
    target = _normalize_sucursal_slug(sucursal_slug or "principal")
    with _labelsapp_products_cache_lock:
        stale_keys = [k for k in _labelsapp_products_cache.keys() if k and k[0] == target]
        for key in stale_keys:
            try:
                del _labelsapp_products_cache[key]
            except Exception:
                pass


def _get_products_fetch_lock(key: tuple) -> Lock:
    lock = _labelsapp_products_fetch_locks.get(key)
    if lock is not None:
        return lock
    with _labelsapp_products_fetch_locks_guard:
        lock = _labelsapp_products_fetch_locks.get(key)
        if lock is None:
            lock = Lock()
            _labelsapp_products_fetch_locks[key] = lock
        return lock


class LabelsAppCodigoBaseRequest(BaseModel):
    base: str
    producto: str
    terminacion: str


class LabelsAppFacturaPriorityRequest(BaseModel):
    prioridad: str


class LabelsAppFacturaItemsUpdateRequest(BaseModel):
    prioridad: Optional[str] = None
    id_cliente: Optional[str] = None
    items: List[LabelsAppItem]


class SherwinLab(BaseModel):
    L: float
    A: float
    B: float


class SherwinCoordinatingColors(BaseModel):
    coord1ColorId: str
    coord2ColorId: str
    whiteColorId: Optional[str] = None


class SherwinColorResult(BaseModel):
    colorNumber: str
    coordinatingColors: Optional[SherwinCoordinatingColors] = None
    description: List[str] = []
    id: str
    isExterior: bool
    isInterior: bool
    name: str
    lrv: float
    brandedCollectionNames: List[str] = []
    colorFamilyNames: List[str] = []
    brandKey: str
    red: int
    green: int
    blue: int
    hue: float
    saturation: float
    lightness: float
    hex: str
    isDark: bool
    storeStripLocator: Optional[str] = None
    similarColors: List[str] = []
    ignore: bool
    archived: bool
    lab: SherwinLab


class SherwinColorSearchResponse(BaseModel):
    count: int
    results: List[SherwinColorResult]


class SherwinSwatchItem(BaseModel):
    code: str
    name: str
    hex: str
    found: bool
    cached: bool


class SherwinSwatchBatchResponse(BaseModel):
    total: int
    items: List[SherwinSwatchItem]

from config import settings
from database import DatabasePool, get_db
import bcrypt
import hashlib
import uvicorn

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL))
logger = logging.getLogger(__name__)

# Sesiones activas: {usuario_id: {"username": "", "nombre": "", "rol": "", "departamento": "", "ultima_actividad": timestamp}}
ACTIVE_SESSIONS = {}
SHERWIN_SWATCH_CACHE: Dict[str, Dict[str, Any]] = {}
SHERWIN_SWATCH_CACHE_TTL_SECONDS = 3600
SHERWIN_SWATCH_CACHE_LOCK = Lock()
SHERWIN_UPSTREAM_MAX_CONCURRENCY = 6
SHERWIN_UPSTREAM_SEMAPHORE = asyncio.Semaphore(SHERWIN_UPSTREAM_MAX_CONCURRENCY)
SHERWIN_CIRCUIT_LOCK = Lock()
SHERWIN_CIRCUIT_FAILURE_THRESHOLD = 5
SHERWIN_CIRCUIT_OPEN_SECONDS = 20
SHERWIN_CIRCUIT_STATE: Dict[str, Any] = {
    "failures": 0,
    "open_until": 0.0,
}

SHERWIN_LOCAL_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sherwin_colors_cache.json")
SHERWIN_LOCAL_CATALOG_LOCK = Lock()
SHERWIN_LOCAL_CATALOG_STATE: Dict[str, Any] = {
    "mtime": 0.0,
    "payload": None,
    "by_code": {},
}
MAINTENANCE_SCHEMA_READY = False


def _load_local_sherwin_catalog() -> Dict[str, Any]:
    """Lee sherwin_colors_cache.json (lazy, con cache + invalidacion por mtime)."""
    path = SHERWIN_LOCAL_CATALOG_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        with SHERWIN_LOCAL_CATALOG_LOCK:
            SHERWIN_LOCAL_CATALOG_STATE.update({"mtime": 0.0, "payload": None, "by_code": {}})
        return {"fetched_at": 0, "total": 0, "colors": []}

    with SHERWIN_LOCAL_CATALOG_LOCK:
        if SHERWIN_LOCAL_CATALOG_STATE["payload"] and SHERWIN_LOCAL_CATALOG_STATE["mtime"] == mtime:
            return SHERWIN_LOCAL_CATALOG_STATE["payload"]

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"No se pudo leer catalogo Sherwin local: {e}")
        return {"fetched_at": 0, "total": 0, "colors": []}

    colors = data.get("colors") or []
    by_code: Dict[str, Dict[str, Any]] = {}
    for c in colors:
        if not isinstance(c, dict):
            continue
        code = str(c.get("code") or "").upper().replace(" ", "")
        if code:
            by_code[code] = c

    payload = {
        "fetched_at": int(data.get("fetched_at") or 0),
        "total": len(colors),
        "colors": colors,
    }
    with SHERWIN_LOCAL_CATALOG_LOCK:
        SHERWIN_LOCAL_CATALOG_STATE.update({"mtime": mtime, "payload": payload, "by_code": by_code})
    return payload


def _lookup_local_sherwin(code: str) -> Optional[Dict[str, Any]]:
    if not code:
        return None
    _load_local_sherwin_catalog()
    key = re.sub(r"[^0-9A-Z]", "", str(code).upper().replace("SW", ""))
    if not key:
        return None
    with SHERWIN_LOCAL_CATALOG_LOCK:
        return SHERWIN_LOCAL_CATALOG_STATE["by_code"].get(key)

app = FastAPI(title=settings.API_TITLE, version=settings.API_VERSION, description="API PaintFlow")

# Dynamic CORS configuration for development and production
cors_origins = [
    "http://127.0.0.1:8001",
    "http://localhost:8001",
    "https://paintflow.onrender.com",  # Production Render URL
    "https://paintflow.onrender.com/",
]

# Add production URL if available via environment variable
render_url = os.getenv('RENDER_EXTERNAL_URL')
if render_url and render_url not in cors_origins:
    cors_origins.append(render_url)
    if not render_url.endswith('/'):
        cors_origins.append(render_url + '/')

app.add_middleware(CORSMiddleware, 
    allow_origins=cors_origins,
    allow_credentials=True, 
    allow_methods=["*"], 
    allow_headers=["*"]
)

# Middleware para logging de peticiones
@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.method == "POST" and "formulas-normales" in str(request.url):
        try:
            body = await request.body()
            logger.info(f"POST formulas-normales - Raw body: {body}")
            logger.info(f"POST formulas-normales - Headers: {dict(request.headers)}")
        except Exception as e:
            logger.error(f"Error reading request body: {e}")
    
    response = await call_next(request)
    return response

# Servir archivos estáticos con ruta absoluta
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
else:
    logger.warning(f"Static directory not found at: {static_dir}")

pngs_dir = os.path.join(os.path.dirname(__file__), "pngs")
if os.path.exists(pngs_dir):
    app.mount("/pngs", StaticFiles(directory=pngs_dir), name="pngs")
else:
    logger.warning(f"PNGs directory not found at: {pngs_dir}")

PRODUCTOS_MEDIA_DIR = ""
for _candidate in (
    os.path.join(os.path.dirname(__file__), "Productos"),
    os.path.join(os.path.dirname(__file__), "productos"),
):
    if os.path.exists(_candidate):
        PRODUCTOS_MEDIA_DIR = _candidate
        break

if PRODUCTOS_MEDIA_DIR:
    app.mount("/productos", StaticFiles(directory=PRODUCTOS_MEDIA_DIR), name="productos")
else:
    logger.warning("Productos media directory not found (Productos/productos)")


def _humanize_producto_name(filename: str) -> str:
    name = os.path.splitext(os.path.basename(filename or ""))[0]
    name = re.sub(r"[-_]+", " ", name).strip()
    if not name:
        return "Producto"
    return " ".join(piece.capitalize() if piece.islower() else piece for piece in name.split())


@app.get("/api/v1/productos/catalog")
async def productos_catalog():
    """Lista imágenes del catálogo de productos para pantalla touch."""
    if not PRODUCTOS_MEDIA_DIR:
        return {"total": 0, "productos": []}

    allowed_ext = {".png", ".jpg", ".jpeg", ".webp"}
    files = []
    try:
        for entry in os.listdir(PRODUCTOS_MEDIA_DIR):
            ext = os.path.splitext(entry)[1].lower()
            if ext in allowed_ext:
                files.append(entry)
    except Exception as e:
        logger.warning(f"Error listing productos catalog: {e}")
        return {"total": 0, "productos": []}

    files.sort(key=lambda x: x.lower())
    items = []
    for filename in files:
        display_name = _humanize_producto_name(filename)
        items.append({
            "name": display_name,
            "image": f"/productos/{quote(filename)}",
            "description": f"Acabado {display_name}. Combina tu color seleccionado y configura terminacion, presentacion y cantidad.",
        })

    return {"total": len(items), "productos": items}

def _run_startup_schema_setup():
    """Schema bootstrap ejecutado en background para no bloquear el bind del puerto.

    Cualquier DDL que tome locks (DROP/CREATE TRIGGER, ALTER) debe correr aqui con
    lock_timeout/statement_timeout para que un deploy nunca quede colgado esperando
    un lock que tenga la instancia anterior durante el rollover de Render.
    """
    try:
        db = DatabasePool.get_connection()
    except Exception as e:
        logger.error(f"[startup-setup] No se pudo obtener conexion: {e}")
        return

    try:
        global MAINTENANCE_SCHEMA_READY
        try:
            with db.cursor() as cur:
                # Fallar rapido si hay contencion en vez de colgar el deploy.
                cur.execute("SET lock_timeout = '3s'")
                cur.execute("SET statement_timeout = '30s'")
        except Exception as e:
            logger.warning(f"[startup-setup] No se pudo aplicar timeouts: {e}")

        for step_name, fn in (
            ("labelsapp_history_table", _ensure_labelsapp_history_table),
            ("usuarios_cliente_role_constraint", _ensure_usuarios_cliente_role_constraint),
            ("formula_backup", _ensure_formula_backup),
            ("productsw_search_index", _ensure_productsw_search_index),
            ("maintenance_schema", _ensure_maintenance_schema),
        ):
            try:
                fn(db)
                db.commit()
                logger.info(f"[startup-setup] OK: {step_name}")
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning(f"[startup-setup] Skip {step_name}: {e}")
        try:
            _seed_maintenance_demo_data(db)
            db.commit()
            MAINTENANCE_SCHEMA_READY = True
            logger.info("[startup-setup] OK: maintenance_demo_seed")
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(f"[startup-setup] Skip maintenance_demo_seed: {e}")
    finally:
        try:
            DatabasePool.return_connection(db)
        except Exception:
            pass


@app.on_event("startup")
async def startup_event():
    logger.info("Starting PaintFlow API...")
    DatabasePool.init_pool()
    # No bloquear el startup con DDL: si alguna tabla esta con lock por la instancia
    # vieja durante un rollover, el bind del puerto debe ocurrir igual.
    try:
        asyncio.get_event_loop().run_in_executor(None, _run_startup_schema_setup)
    except Exception as e:
        logger.warning(f"No se pudo programar schema setup en background: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down PaintFlow API...")
    DatabasePool.close_pool()

@app.get("/")
async def root():
    """Servir interfaz HTML"""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Index not found")

@app.get("/employees")
async def employees_page():
    """Servir interfaz de gestión de empleados para analistas"""
    html_path = os.path.join(os.path.dirname(__file__), "employees.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Employees page not found")

@app.get("/employees.html")
async def employees_html():
    """Alias para acceso directo a employees.html"""
    html_path = os.path.join(os.path.dirname(__file__), "employees.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Employees page not found")


@app.get("/health")
async def health_check():
    """Estado de la API"""
    return {"status": "healthy", "version": settings.API_VERSION, "timestamp": datetime.now().isoformat()}

@app.get("/logo.png")
async def serve_logo():
    """Servir logo"""
    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")
    if os.path.exists(logo_path):
        return FileResponse(logo_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")


@app.get("/labelsapp-web")
async def labelsapp_web_page():
    """Servir interfaz web de LabelsApp"""
    html_path = os.path.join(os.path.dirname(__file__), "labelsapp_web.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="LabelsApp Web page not found")


@app.get("/labelsapp-feedback")
async def labelsapp_feedback_page():
    """Servir formulario de encuesta para LabelsApp Web"""
    html_path = os.path.join(os.path.dirname(__file__), "labelsapp_feedback.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="LabelsApp Feedback page not found")


@app.get("/kiosk-touch")
async def kiosk_touch_page():
    """Alias legacy: redirige a la ruta canonica de clientes."""
    return RedirectResponse(url="/cliente", status_code=307)


@app.get("/kiosk-touch.html")
async def kiosk_touch_html():
    """Alias legacy HTML: redirige a la ruta canonica de clientes."""
    return RedirectResponse(url="/cliente", status_code=307)


@app.get("/cliente")
async def cliente_page():
    """Alias comercial para la vista touch de clientes"""
    html_path = os.path.join(os.path.dirname(__file__), "kiosk_touch.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Cliente page not found")


@app.get("/cliente.html")
async def cliente_html():
    """Alias HTML para la vista de clientes"""
    html_path = os.path.join(os.path.dirname(__file__), "kiosk_touch.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Cliente page not found")


@app.get("/maintenance")
async def maintenance_page():
    """Modulo de gestion de mantenimiento integrado en PaintFlow."""
    html_path = os.path.join(os.path.dirname(__file__), "maintenance_management.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Maintenance page not found")


@app.get("/maintenance-management")
async def maintenance_management_page():
    """Alias comercial para el modulo de mantenimiento."""
    return RedirectResponse(url="/maintenance", status_code=307)


@app.get("/clientes")
async def clientes_page():
    """Alias plural hacia la ruta canonica de cliente."""
    return RedirectResponse(url="/cliente", status_code=307)


@app.get("/clientes.html")
async def clientes_html():
    """Alias plural HTML hacia la ruta canonica de cliente."""
    return RedirectResponse(url="/cliente", status_code=307)


def _normalize_sucursal_slug(text: str) -> str:
    raw = (text or "").strip().lower()
    if not raw:
        return "principal"
    norm = unicodedata.normalize("NFD", raw)
    norm = "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")
    norm = re.sub(r"[^a-z0-9]+", "_", norm).strip("_")
    return norm or "principal"


def _safe_table_for_sucursal(sucursal_slug: str) -> str:
    slug = _normalize_sucursal_slug(sucursal_slug)
    if not re.match(r"^[a-z0-9_]+$", slug):
        slug = "principal"
    return f"pedidos_pendientes_{slug}"


def _normalize_zona_key(text: str) -> str:
    raw = (text or "").strip().lower()
    if not raw:
        return "Sin_Zona"
    norm = unicodedata.normalize("NFD", raw)
    norm = "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")
    norm = re.sub(r"[^a-z0-9]+", "_", norm).strip("_")
    if norm in {"santo_domingo", "santo_dom", "sto_domingo"}:
        return "Santo_Domingo"
    if norm in {"zona_este", "este"}:
        return "Zona_Este"
    if norm in {"zona_norte", "norte"}:
        return "Zona_Norte"
    if norm in {"zona_sur", "sur"}:
        return "Zona_Sur"
    if not norm:
        return "Sin_Zona"
    return "_".join(p.capitalize() for p in norm.split("_"))


PERSONALIZADOS_CSV_PATH = os.path.join(os.path.dirname(__file__), "productos_personalizados.csv")
LABELSAPP_FEEDBACK_CSV_PATH = os.path.join(os.path.dirname(__file__), "labelsapp_feedback.csv")
PERSONALIZADO_CODIGO_MAX = 7
PERSONALIZADO_NOMBRE_MAX = 20
_personalizados_migration_done: Dict[str, bool] = {}
_personalizados_migration_lock = Lock()


class LabelsAppPersonalizedProductRequest(BaseModel):
    codigo: str
    nombre: str


class LabelsAppFeedbackRequest(BaseModel):
    rendimiento: int
    facilidad_uso: int
    estabilidad: int
    implementacion_web: int
    satisfaccion_general: int
    recomendaria: Optional[str] = ""
    comentario_general: Optional[str] = ""
    mejoras_sugeridas: Optional[str] = ""
    errores_reportados: Optional[str] = ""
    username: Optional[str] = None
    usuario_id: Optional[int] = None
    sucursal: Optional[str] = None
    modulo: Optional[str] = "labelsapp-web"


def _load_personalized_products_csv() -> List[dict]:
    items = []
    if not os.path.exists(PERSONALIZADOS_CSV_PATH):
        return items

    try:
        with open(PERSONALIZADOS_CSV_PATH, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                codigo = (row.get("codigo") or "").strip()
                nombre = (row.get("nombre") or "").strip()
                if not codigo:
                    continue
                items.append({
                    "codigo": codigo,
                    "nombre": nombre,
                    "base": row.get("base", "") or "",
                    "ubicacion": row.get("ubicacion", "") or "",
                    "fecha_creacion": row.get("fecha_creacion", "") or "",
                    "sucursal_slug": (row.get("sucursal_slug") or "").strip(),
                    "personalizado": True,
                })
    except Exception as e:
        logger.warning(f"Error loading personalized products: {e}")
    return items


def _save_personalized_products(items: List[dict]) -> None:
    fieldnames = ["codigo", "nombre", "base", "ubicacion", "fecha_creacion", "sucursal_slug"]
    os.makedirs(os.path.dirname(PERSONALIZADOS_CSV_PATH), exist_ok=True)
    with open(PERSONALIZADOS_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow({
                "codigo": (item.get("codigo") or "").strip(),
                "nombre": (item.get("nombre") or "").strip(),
                "base": (item.get("base") or "").strip(),
                "ubicacion": (item.get("ubicacion") or "").strip(),
                "fecha_creacion": (item.get("fecha_creacion") or "").strip(),
                "sucursal_slug": (item.get("sucursal_slug") or "").strip(),
            })


def _filter_personalized_products_by_sucursal(items: List[dict], sucursal_slug: str) -> List[dict]:
    slug = (sucursal_slug or "principal").strip() or "principal"
    return [
        item for item in (items or [])
        if (item.get("sucursal_slug") or "").strip() == slug
    ]


def _find_personalized_product_by_code(codigo: str) -> Optional[dict]:
    codigo_norm = (codigo or "").strip().lower()
    for item in _load_personalized_products_csv():
        if (item.get("codigo") or "").strip().lower() == codigo_norm:
            return item
    return None


def _personalizados_table_for_sucursal(sucursal_slug: str) -> str:
    slug = _normalize_sucursal_slug(sucursal_slug or "principal")
    if not re.match(r"^[a-z0-9_]+$", slug):
        slug = "principal"
    return f"personalizado_{slug}"


def _ensure_personalizados_table(db, table_name: str) -> None:
    cur = db.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            codigo VARCHAR(32) PRIMARY KEY,
            nombre VARCHAR(120) NOT NULL,
            base VARCHAR(80) NOT NULL DEFAULT '',
            ubicacion VARCHAR(120) NOT NULL DEFAULT '',
            fecha_creacion TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_nombre ON {table_name}(LOWER(nombre))")


def _migrate_personalizados_csv_for_sucursal(db, sucursal_slug: str, table_name: str) -> None:
    slug = _normalize_sucursal_slug(sucursal_slug or "principal")
    with _personalizados_migration_lock:
        if _personalizados_migration_done.get(slug):
            return

    cur = db.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table_name}")
    table_count = int((cur.fetchone() or [0])[0] or 0)
    if table_count > 0:
        with _personalizados_migration_lock:
            _personalizados_migration_done[slug] = True
        return

    legacy_items = _filter_personalized_products_by_sucursal(_load_personalized_products_csv(), slug)
    inserted = 0
    for item in legacy_items:
        codigo = (item.get("codigo") or "").strip()
        nombre = (item.get("nombre") or "").strip()
        if not codigo or not nombre:
            continue
        if len(codigo) > PERSONALIZADO_CODIGO_MAX:
            continue
        if len(nombre) > PERSONALIZADO_NOMBRE_MAX:
            continue
        cur.execute(
            f"""
            INSERT INTO {table_name} (codigo, nombre, base, ubicacion, fecha_creacion)
            VALUES (%s, %s, %s, %s, COALESCE(NULLIF(%s, '')::timestamp, NOW()))
            ON CONFLICT (codigo) DO NOTHING
            """,
            (
                codigo,
                nombre,
                (item.get("base") or "").strip(),
                (item.get("ubicacion") or "").strip(),
                (item.get("fecha_creacion") or "").strip(),
            ),
        )
        inserted += 1

    if inserted:
        logger.info(f"Migrated {inserted} personalizados legacy rows into {table_name}")

    with _personalizados_migration_lock:
        _personalizados_migration_done[slug] = True


def _list_personalizados_for_sucursal(db, sucursal_slug: str) -> List[dict]:
    table_name = _personalizados_table_for_sucursal(sucursal_slug)
    _ensure_personalizados_table(db, table_name)
    _migrate_personalizados_csv_for_sucursal(db, sucursal_slug, table_name)
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT codigo, nombre, COALESCE(base, ''), COALESCE(ubicacion, ''),
               TO_CHAR(fecha_creacion, 'YYYY-MM-DD HH24:MI:SS')
        FROM {table_name}
        ORDER BY LOWER(nombre), LOWER(codigo)
        """
    )
    return [
        {
            "codigo": r[0],
            "nombre": r[1],
            "base": r[2],
            "ubicacion": r[3],
            "fecha_creacion": r[4] or "",
            "sucursal_slug": _normalize_sucursal_slug(sucursal_slug or "principal"),
            "personalizado": True,
        }
        for r in (cur.fetchall() or [])
    ]


def _get_personalizado_by_codigo(db, sucursal_slug: str, codigo: str) -> Optional[dict]:
    table_name = _personalizados_table_for_sucursal(sucursal_slug)
    _ensure_personalizados_table(db, table_name)
    _migrate_personalizados_csv_for_sucursal(db, sucursal_slug, table_name)
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT codigo, nombre, COALESCE(base, ''), COALESCE(ubicacion, ''),
               TO_CHAR(fecha_creacion, 'YYYY-MM-DD HH24:MI:SS')
        FROM {table_name}
        WHERE LOWER(TRIM(codigo)) = LOWER(TRIM(%s))
        LIMIT 1
        """,
        (codigo,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "codigo": r[0],
        "nombre": r[1],
        "base": r[2],
        "ubicacion": r[3],
        "fecha_creacion": r[4] or "",
        "sucursal_slug": _normalize_sucursal_slug(sucursal_slug or "principal"),
        "personalizado": True,
    }


def _insert_personalizado(db, sucursal_slug: str, codigo: str, nombre: str) -> None:
    table_name = _personalizados_table_for_sucursal(sucursal_slug)
    _ensure_personalizados_table(db, table_name)
    _migrate_personalizados_csv_for_sucursal(db, sucursal_slug, table_name)
    cur = db.cursor()
    cur.execute(
        f"""
        INSERT INTO {table_name} (codigo, nombre, base, ubicacion, fecha_creacion, updated_at)
        VALUES (%s, %s, '', '', NOW(), NOW())
        """,
        (codigo, nombre),
    )


def _delete_personalizado(db, sucursal_slug: str, codigo: str) -> int:
    table_name = _personalizados_table_for_sucursal(sucursal_slug)
    _ensure_personalizados_table(db, table_name)
    _migrate_personalizados_csv_for_sucursal(db, sucursal_slug, table_name)
    cur = db.cursor()
    cur.execute(
        f"DELETE FROM {table_name} WHERE LOWER(TRIM(codigo)) = LOWER(TRIM(%s))",
        (codigo,),
    )
    return int(cur.rowcount or 0)


def _resolve_sucursal_slug(db, username: Optional[str] = None, usuario_id: Optional[int] = None, sucursal_text: Optional[str] = None) -> str:
    if sucursal_text:
        return _normalize_sucursal_slug(sucursal_text)

    try:
        if usuario_id is not None and usuario_id in ACTIVE_SESSIONS:
            session_data = ACTIVE_SESSIONS.get(usuario_id) or {}
            session_slug = (
                session_data.get("sucursal_slug")
                or session_data.get("sucursal")
                or session_data.get("sucursal_nombre")
            )
            if session_slug:
                return _normalize_sucursal_slug(str(session_slug))

        if username:
            username_norm = (username or "").strip().lower()
            for session_data in ACTIVE_SESSIONS.values():
                s_user = str(session_data.get("username") or "").strip().lower()
                if s_user and s_user == username_norm:
                    session_slug = (
                        session_data.get("sucursal_slug")
                        or session_data.get("sucursal")
                        or session_data.get("sucursal_nombre")
                    )
                    if session_slug:
                        return _normalize_sucursal_slug(str(session_slug))
    except Exception:
        pass

    try:
        cur = db.cursor()
        if username:
            cur.execute(
                """
                SELECT COALESCE(s.codigo, s.nombre, '')
                FROM usuarios u
                LEFT JOIN sucursales s ON u.sucursal_id = s.id
                WHERE LOWER(u.username) = LOWER(%s)
                LIMIT 1
                """,
                (username,)
            )
            row = cur.fetchone()
            if row and row[0]:
                return _normalize_sucursal_slug(str(row[0]))

        if usuario_id:
            cur.execute(
                """
                SELECT COALESCE(s.codigo, s.nombre, '')
                FROM usuarios u
                LEFT JOIN sucursales s ON u.sucursal_id = s.id
                WHERE u.id = %s
                LIMIT 1
                """,
                (usuario_id,)
            )
            row = cur.fetchone()
            if row and row[0]:
                return _normalize_sucursal_slug(str(row[0]))
    except Exception:
        pass

    return "principal"


def _resolve_operador_label(db, username: Optional[str] = None, usuario_id: Optional[int] = None, operador: Optional[str] = None) -> str:
    explicit = (operador or "").strip()
    if explicit:
        return explicit

    # El operador real lo decide el gestor; desde web no lo inventamos.
    return ""


def _normalize_role_key(role: Optional[str]) -> str:
    role_key = (role or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "admin": "administrador",
        "tecnico": "tecnicos",
        "tecnico_mantenimiento": "tecnicos",
        "activo_fijo": "activos_fijos",
        "activofijo": "activos_fijos",
        "activos_fijo": "activos_fijos",
        "activosfijos": "activos_fijos",
        "activosfijo": "activos_fijos",
        "activos_fijos": "activos_fijos",
    }
    return aliases.get(role_key, role_key)


def _can_view_all_labelsapp_history(role: Optional[str]) -> bool:
    return _normalize_role_key(role) in {"administrador", "admin", "manager", "gerente"}


def _resolve_requester_role(db, username: Optional[str] = None, usuario_id: Optional[int] = None, role: Optional[str] = None) -> str:
    if usuario_id is not None and usuario_id in ACTIVE_SESSIONS:
        return _normalize_role_key(ACTIVE_SESSIONS[usuario_id].get("rol"))

    if username:
        for session_data in ACTIVE_SESSIONS.values():
            if _normalize_role_key(session_data.get("username")) == _normalize_role_key(username):
                return _normalize_role_key(session_data.get("rol"))

    try:
        cur = db.cursor()
        if usuario_id is not None:
            cur.execute("SELECT rol FROM usuarios WHERE id = %s LIMIT 1", (usuario_id,))
            row = cur.fetchone()
            if row and row[0]:
                return _normalize_role_key(str(row[0]))
        if username:
            cur.execute("SELECT rol FROM usuarios WHERE LOWER(TRIM(username)) = LOWER(TRIM(%s)) LIMIT 1", (username,))
            row = cur.fetchone()
            if row and row[0]:
                return _normalize_role_key(str(row[0]))
    except Exception:
        pass

    return _normalize_role_key(role)


def _require_maintenance_access(db, username: Optional[str] = None, usuario_id: Optional[int] = None, role: Optional[str] = None) -> str:
    resolved_role = _resolve_requester_role(db, username=username, usuario_id=usuario_id, role=role)
    if resolved_role not in {"administrador", "activos_fijos", "tecnicos"}:
        raise HTTPException(status_code=403, detail="Acceso restringido al modulo de mantenimiento")
    return resolved_role


def _get_labelsapp_live_queue(db, table_name: str, limit: int = 50):
    cols = _get_table_columns(db, table_name)
    id_cliente_expr = "MAX(COALESCE(id_cliente, ''))" if "id_cliente" in cols else "''"
    operador_expr = "MAX(COALESCE(operador, '—'))" if "operador" in cols else "'—'"
    pr_rank_expr = (
        "MAX(CASE TRIM(COALESCE(prioridad,'')) WHEN 'Alta' THEN 3 WHEN 'Media' THEN 2 WHEN 'Baja' THEN 1 ELSE 0 END)"
        if "prioridad" in cols
        else "0"
    )
    estado_expr = "TRIM(COALESCE(estado,''))" if "estado" in cols else "'Pendiente'"

    cur = db.cursor()
    cur.execute(
        f"""
        SELECT id_factura,
               {id_cliente_expr} AS id_cliente,
               COUNT(*) AS total,
               SUM(CASE WHEN {estado_expr} IN ('Finalizado','Completado') THEN 1 ELSE 0 END) AS cnt_final,
               SUM(CASE WHEN {estado_expr} = 'En Proceso' THEN 1 ELSE 0 END) AS cnt_proc,
               {pr_rank_expr} AS pr_rank,
               {operador_expr} AS operador
        FROM {table_name}
        WHERE {estado_expr} <> 'Cancelado'
        GROUP BY id_factura
        HAVING SUM(CASE WHEN {estado_expr} IN ('Finalizado','Completado') THEN 1 ELSE 0 END) < COUNT(*)
        ORDER BY pr_rank DESC, id_factura DESC
        LIMIT %s
        """,
        (limit,)
    )
    rows = cur.fetchall()
    items = []
    for factura, id_cliente, total, cnt_final, cnt_proc, pr_rank, operador in rows:
        prioridad_txt = 'Alta' if pr_rank == 3 else ('Media' if pr_rank == 2 else ('Baja' if pr_rank == 1 else '—'))
        if cnt_final == total and total > 0:
            estado_txt = 'Finalizado'
        elif cnt_proc > 0:
            estado_txt = 'En Proceso'
        else:
            estado_txt = 'Pendiente'
        items.append({
            "factura": factura or '—',
            "cliente": (id_cliente or '').strip() or '—',
            "items": int(total or 0),
            "operador": operador or '—',
            "en_proceso": int(cnt_proc or 0),
            "finalizados": int(cnt_final or 0),
            "prioridad": prioridad_txt,
            "estado": estado_txt,
        })
    return items


def _get_labelsapp_pending_queue(db, table_name: str, limit: int = 80):
    cols = _get_table_columns(db, table_name)
    id_cliente_expr = "MAX(COALESCE(id_cliente, ''))" if "id_cliente" in cols else "''"
    operador_expr = "MAX(COALESCE(operador, '—'))" if "operador" in cols else "'—'"
    pr_rank_expr = (
        "MAX(CASE TRIM(COALESCE(prioridad,'')) WHEN 'Alta' THEN 3 WHEN 'Media' THEN 2 WHEN 'Baja' THEN 1 ELSE 0 END)"
        if "prioridad" in cols
        else "0"
    )
    estado_expr = "TRIM(COALESCE(estado,''))" if "estado" in cols else "'Pendiente'"

    if "fecha_creacion" in cols:
        created_expr = "MAX(fecha_creacion)"
    elif "fecha_creado" in cols:
        created_expr = "MAX(fecha_creado)"
    else:
        created_expr = "NULL"

    cur = db.cursor()
    cur.execute(
        f"""
        SELECT id_factura,
               {id_cliente_expr} AS id_cliente,
               COUNT(*) AS total,
               {pr_rank_expr} AS pr_rank,
               {operador_expr} AS operador,
               {created_expr} AS created_at
        FROM {table_name}
        WHERE {estado_expr} <> 'Cancelado'
        GROUP BY id_factura
        HAVING SUM(CASE WHEN {estado_expr} IN ('Finalizado','Completado') THEN 1 ELSE 0 END) = 0
           AND SUM(CASE WHEN {estado_expr} = 'En Proceso' THEN 1 ELSE 0 END) = 0
        ORDER BY pr_rank DESC, {created_expr} DESC, id_factura DESC
        LIMIT %s
        """,
        (limit,)
    )
    rows = cur.fetchall()
    items = []
    for factura, id_cliente, total, pr_rank, operador, created_at in rows:
        prioridad_txt = 'Alta' if pr_rank == 3 else ('Media' if pr_rank == 2 else ('Baja' if pr_rank == 1 else '—'))
        items.append({
            "factura": factura or '—',
            "cliente": (id_cliente or '').strip() or '—',
            "items": int(total or 0),
            "operador": operador or '—',
            "prioridad": prioridad_txt,
            "estado": 'Pendiente',
            "created_at": created_at.isoformat() if created_at else None,
        })
    return items


def _make_labelsapp_notify_payload(action: str, sucursal_slug: str, id_factura: str, **extra) -> str:
    payload = {
        "action": action,
        "sucursal": (sucursal_slug or "principal").strip() or "principal",
        "id_factura": (id_factura or "").strip(),
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)


def _notify_labelsapp_update(cur, action: str, sucursal_slug: str, id_factura: str, **extra) -> None:
    cur.execute(
        "SELECT pg_notify('pedidos_actualizados', %s)",
        (_make_labelsapp_notify_payload(action, sucursal_slug, id_factura, **extra),),
    )


def _get_labelsapp_factura_items(db, table_name: str, factura: str, limit: int = 300):
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT COALESCE(id_orden_profesional, ''),
               COALESCE(codigo, ''),
               COALESCE(producto, ''),
               COALESCE(terminacion, ''),
               COALESCE(presentacion, ''),
               COALESCE(cantidad, 1),
               COALESCE(base, ''),
               COALESCE(ubicacion, ''),
               COALESCE(codigo_base, ''),
               COALESCE(prioridad, 'Media'),
               COALESCE(estado, 'Pendiente')
        FROM {table_name}
        WHERE id_factura = %s
          AND TRIM(COALESCE(estado, '')) <> 'Cancelado'
        ORDER BY id ASC
        LIMIT %s
        """,
        (factura, limit)
    )
    rows = cur.fetchall()
    return [
        {
            "id_orden_profesional": r[0],
            "codigo": r[1],
            "producto": r[2],
            "terminacion": r[3],
            "presentacion": r[4],
            "cantidad": int(r[5] or 1),
            "base": r[6],
            "ubicacion": r[7],
            "codigo_base": r[8],
            "prioridad": r[9],
            "estado": r[10],
        }
        for r in rows
    ]


def _get_table_columns(db, table_name: str) -> set:
    cur = db.cursor()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table_name,)
    )
    return {r[0] for r in cur.fetchall()}


def _ensure_pedidos_table(db, table_name: str) -> None:
    cur = db.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id SERIAL PRIMARY KEY,
            id_orden_profesional VARCHAR(20) UNIQUE,
            id_factura VARCHAR(120) NOT NULL,
            id_cliente VARCHAR(120),
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
        )
    """)
    cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS id_cliente VARCHAR(120)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_factura ON {table_name}(id_factura)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_estado ON {table_name}(estado)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_prioridad ON {table_name}(prioridad)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_sucursal_estado_factura ON {table_name}(sucursal, estado, id_factura)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_sucursal_prioridad_factura ON {table_name}(sucursal, prioridad, id_factura)")


def _pedidos_table_exists(db, table_name: str) -> bool:
    cur = db.cursor()
    cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    row = cur.fetchone()
    return bool(row and row[0])


def _set_local_pg_timeouts(db, statement_ms: int = 8000, lock_ms: int = 1200) -> None:
    cur = db.cursor()
    cur.execute("SET LOCAL lock_timeout = %s", (f"{int(lock_ms)}ms",))
    cur.execute("SET LOCAL statement_timeout = %s", (f"{int(statement_ms)}ms",))


def _ensure_labelsapp_history_table(db) -> None:
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS labelsapp_historial (
            id BIGSERIAL PRIMARY KEY,
            id_factura VARCHAR(120) NOT NULL,
            id_cliente VARCHAR(120),
            sucursal VARCHAR(120) NOT NULL,
            username VARCHAR(120),
            usuario_id INTEGER,
            operador VARCHAR(120),
            prioridad VARCHAR(10) DEFAULT 'Media',
            total_items INTEGER DEFAULT 0,
            total_unidades INTEGER DEFAULT 0,
            productos_json JSONB NOT NULL,
            estado_envio VARCHAR(20) DEFAULT 'Enviado',
            origen VARCHAR(20) DEFAULT 'web',
            fecha_envio TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_labelsapp_historial_fecha ON labelsapp_historial(fecha_envio DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_labelsapp_historial_factura ON labelsapp_historial(id_factura)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_labelsapp_historial_sucursal ON labelsapp_historial(sucursal)")


def _ensure_usuarios_cliente_role_constraint(db) -> None:
    """Asegura que la restriccion de rol de usuarios permita roles del portal."""
    cur = db.cursor()
    cur.execute(
        """
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'usuarios'::regclass
          AND contype = 'c'
          AND conname = 'usuarios_rol_check'
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        return

    definition = str(row[1] or "")
    definition_lower = definition.lower()
    if all(role_name in definition_lower for role_name in ("cliente", "activos_fijos", "tecnicos")):
        return

    # Reusar valores del CHECK actual y agregar roles del portal sin duplicados.
    existing_roles = re.findall(r"'([^']+)'", definition)
    if not existing_roles:
        return

    merged_roles = []
    seen = set()
    for role_value in existing_roles + ["cliente", "activos_fijos", "tecnicos"]:
        key = role_value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged_roles.append(role_value)

    in_clause = ", ".join("'" + value.replace("'", "''") + "'" for value in merged_roles)
    cur.execute("ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_rol_check")
    cur.execute(
        f"ALTER TABLE usuarios ADD CONSTRAINT usuarios_rol_check CHECK (rol::text = ANY (ARRAY[{in_clause}]::text[]))"
    )


def _normalize_maintenance_priority(value: Optional[str]) -> str:
    normalized = (value or "Media").strip().lower()
    mapping = {
        "alta": "Alta",
        "media": "Media",
        "baja": "Baja",
    }
    return mapping.get(normalized, "Media")


def _normalize_maintenance_status(value: Optional[str]) -> str:
    normalized = (value or "Pendiente").strip().lower()
    mapping = {
        "pendiente": "Pendiente",
        "en proceso": "En proceso",
        "en_proceso": "En proceso",
        "completado": "Completado",
        "vencido": "Vencido",
    }
    return mapping.get(normalized, "Pendiente")


def _normalize_maintenance_type(value: Optional[str]) -> str:
    normalized = (value or "Preventivo").strip().lower()
    mapping = {
        "preventivo": "Preventivo",
        "correctivo": "Correctivo",
        "inspeccion": "Inspeccion",
        "inspección": "Inspeccion",
    }
    return mapping.get(normalized, "Preventivo")


def _compute_next_maintenance(last_date: Optional[date], frequency_days: int, explicit_next: Optional[date] = None) -> Optional[date]:
    if explicit_next is not None:
        return explicit_next
    if last_date is None:
        return None
    return last_date + timedelta(days=max(1, int(frequency_days or 1)))


def _ensure_maintenance_schema(db) -> None:
    cur = db.cursor()
    try:
        cur.execute("SET LOCAL lock_timeout = '2s'")
        cur.execute("SET LOCAL statement_timeout = '15s'")
    except Exception:
        pass
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_assets (
            id SERIAL PRIMARY KEY,
            codigo VARCHAR(60) NOT NULL UNIQUE,
            nombre VARCHAR(160) NOT NULL,
            categoria VARCHAR(80) NOT NULL DEFAULT 'General',
            sucursal_id INTEGER REFERENCES sucursales(id),
            ubicacion_detalle VARCHAR(160),
            descripcion TEXT,
            estado_operativo VARCHAR(40) NOT NULL DEFAULT 'Operativo',
            frecuencia_dias INTEGER NOT NULL DEFAULT 90,
            ultimo_mantenimiento DATE NULL,
            proximo_mantenimiento DATE NULL,
            responsable_usuario_id INTEGER NULL REFERENCES usuarios(id),
            activo BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_work_orders (
            id SERIAL PRIMARY KEY,
            activo_id INTEGER NOT NULL REFERENCES maintenance_assets(id) ON DELETE CASCADE,
            tipo VARCHAR(40) NOT NULL DEFAULT 'Preventivo',
            titulo VARCHAR(160) NOT NULL,
            descripcion TEXT,
            prioridad VARCHAR(20) NOT NULL DEFAULT 'Media',
            estado VARCHAR(30) NOT NULL DEFAULT 'Pendiente',
            programado_para DATE NULL,
            completado_en TIMESTAMP NULL,
            asignado_usuario_id INTEGER NULL REFERENCES usuarios(id),
            creado_por_usuario_id INTEGER NULL REFERENCES usuarios(id),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for stmt in (
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS ubicacion_detalle VARCHAR(160)",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS descripcion TEXT",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS responsable_usuario_id INTEGER NULL REFERENCES usuarios(id)",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS estado_operativo VARCHAR(40) NOT NULL DEFAULT 'Operativo'",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS frecuencia_dias INTEGER NOT NULL DEFAULT 90",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS ultimo_mantenimiento DATE NULL",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS proximo_mantenimiento DATE NULL",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE maintenance_assets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE maintenance_work_orders ADD COLUMN IF NOT EXISTS descripcion TEXT",
        "ALTER TABLE maintenance_work_orders ADD COLUMN IF NOT EXISTS completado_en TIMESTAMP NULL",
        "ALTER TABLE maintenance_work_orders ADD COLUMN IF NOT EXISTS asignado_usuario_id INTEGER NULL REFERENCES usuarios(id)",
        "ALTER TABLE maintenance_work_orders ADD COLUMN IF NOT EXISTS creado_por_usuario_id INTEGER NULL REFERENCES usuarios(id)",
        "ALTER TABLE maintenance_work_orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE maintenance_work_orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    ):
        cur.execute(stmt)
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_maintenance_assets_due ON maintenance_assets(proximo_mantenimiento)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_assets_sucursal ON maintenance_assets(sucursal_id)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_assets_responsable ON maintenance_assets(responsable_usuario_id)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_work_orders_estado ON maintenance_work_orders(estado)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_work_orders_programado ON maintenance_work_orders(programado_para)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_work_orders_activo ON maintenance_work_orders(activo_id)",
    ):
        cur.execute(stmt)


def _seed_maintenance_demo_data(db) -> None:
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM maintenance_assets")
    existing_assets = int((cur.fetchone() or [0])[0] or 0)
    if existing_assets > 0:
        return

    cur.execute(
        "SELECT id, nombre FROM sucursales WHERE activo = TRUE ORDER BY id LIMIT 6"
    )
    sucursales = cur.fetchall() or []
    default_locations = [
        ("Planta Principal", "Compresores"),
        ("Sistema Hidraulico", "Bombas"),
        ("Area de Produccion", "HVAC"),
        ("Cuarto de Maquinas", "Generadores"),
        ("Almacen", "Montacargas"),
        ("Nave Industrial", "Ventilacion"),
    ]
    cur.execute(
        """
        SELECT id, COALESCE(nombre_completo, username, 'Usuario')
        FROM usuarios
        WHERE activo = TRUE
                    AND LOWER(TRIM(COALESCE(rol, ''))) IN ('administrador', 'activos_fijos', 'tecnicos', 'analista', 'operador', 'colorista')
        ORDER BY id
        LIMIT 6
        """
    )
    responsables = cur.fetchall() or []

    today = datetime.now().date()
    demo_assets = [
        {"codigo": "COMP-001", "nombre": "Compresor de Aire 30HP", "categoria": "Compresores", "dias": -3, "frecuencia": 30, "estado": "En revision"},
        {"codigo": "BOMB-002", "nombre": "Bomba de Agua 5HP", "categoria": "Bombas", "dias": 4, "frecuencia": 45, "estado": "Operativo"},
        {"codigo": "AC-003", "nombre": "Aire Acondicionado LG 24K", "categoria": "HVAC", "dias": 5, "frecuencia": 60, "estado": "Operativo"},
        {"codigo": "GEN-004", "nombre": "Planta Electrica 150kVA", "categoria": "Generadores", "dias": 9, "frecuencia": 90, "estado": "Operativo"},
        {"codigo": "MONT-005", "nombre": "Montacargas Toyota 2.5T", "categoria": "Montacargas", "dias": 12, "frecuencia": 30, "estado": "Operativo"},
        {"codigo": "EXT-006", "nombre": "Extractor de Aire Industrial", "categoria": "Ventilacion", "dias": 14, "frecuencia": 60, "estado": "Operativo"},
    ]

    asset_ids = []
    for idx, asset in enumerate(demo_assets):
        sucursal_id = sucursales[idx][0] if idx < len(sucursales) else (sucursales[0][0] if sucursales else None)
        sucursal_nombre = sucursales[idx][1] if idx < len(sucursales) else (sucursales[0][1] if sucursales else "Sucursal Principal")
        responsable_id = responsables[idx % len(responsables)][0] if responsables else None
        ultimo_mantenimiento = today - timedelta(days=max(7, asset["frecuencia"] // 2))
        proximo_mantenimiento = today + timedelta(days=asset["dias"])
        area, _ = default_locations[idx % len(default_locations)]
        cur.execute(
            """
            INSERT INTO maintenance_assets (
                codigo, nombre, categoria, sucursal_id, ubicacion_detalle, descripcion,
                estado_operativo, frecuencia_dias, ultimo_mantenimiento, proximo_mantenimiento,
                responsable_usuario_id, activo, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW(), NOW())
            RETURNING id
            """,
            (
                asset["codigo"],
                asset["nombre"],
                asset["categoria"],
                sucursal_id,
                area,
                f"Equipo demo para {sucursal_nombre}. Datos sembrados para pruebas visuales del modulo.",
                asset["estado"],
                asset["frecuencia"],
                ultimo_mantenimiento,
                proximo_mantenimiento,
                responsable_id,
            )
        )
        asset_ids.append((cur.fetchone() or [None])[0])

    demo_orders = [
        {"asset_idx": 0, "tipo": "Correctivo", "titulo": "Mantenimiento vencido", "prioridad": "Alta", "estado": "Vencido", "dias": -3},
        {"asset_idx": 1, "tipo": "Preventivo", "titulo": "Cambio de sellos y lubricacion", "prioridad": "Media", "estado": "Pendiente", "dias": 4},
        {"asset_idx": 2, "tipo": "Preventivo", "titulo": "Revision de filtros y gas refrigerante", "prioridad": "Media", "estado": "Pendiente", "dias": 5},
        {"asset_idx": 3, "tipo": "Preventivo", "titulo": "Cambio de aceite y filtros", "prioridad": "Baja", "estado": "Completado", "dias": -22},
        {"asset_idx": 4, "tipo": "Preventivo", "titulo": "Inspeccion hidraulica", "prioridad": "Baja", "estado": "Completado", "dias": -35},
        {"asset_idx": 5, "tipo": "Inspeccion", "titulo": "Balanceo de aspas y limpieza", "prioridad": "Media", "estado": "En proceso", "dias": 1},
    ]

    for idx, order in enumerate(demo_orders):
        activo_id = asset_ids[order["asset_idx"]] if order["asset_idx"] < len(asset_ids) else None
        if not activo_id:
            continue
        asignado_id = responsables[idx % len(responsables)][0] if responsables else None
        programado_para = today + timedelta(days=order["dias"])
        completado_en = None
        if order["estado"] == "Completado":
            completado_en = datetime.combine(programado_para, datetime.min.time())
        cur.execute(
            """
            INSERT INTO maintenance_work_orders (
                activo_id, tipo, titulo, descripcion, prioridad, estado,
                programado_para, completado_en, asignado_usuario_id, creado_por_usuario_id,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (
                activo_id,
                order["tipo"],
                order["titulo"],
                f"Orden demo sembrada para pruebas visuales del dashboard de mantenimiento.",
                order["prioridad"],
                order["estado"],
                programado_para,
                completado_en,
                asignado_id,
                asignado_id,
            )
        )


def _serialize_maintenance_asset_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "codigo": row[1],
        "nombre": row[2],
        "categoria": row[3],
        "sucursal_id": row[4],
        "sucursal_nombre": row[5],
        "ubicacion_detalle": row[6] or "",
        "descripcion": row[7] or "",
        "estado_operativo": row[8],
        "frecuencia_dias": int(row[9] or 0),
        "ultimo_mantenimiento": row[10].isoformat() if row[10] else None,
        "proximo_mantenimiento": row[11].isoformat() if row[11] else None,
        "responsable_usuario_id": row[12],
        "responsable_nombre": row[13],
        "activo": bool(row[14]),
        "ordenes_abiertas": int(row[15] or 0),
    }


def _serialize_maintenance_work_order_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "activo_id": row[1],
        "activo_codigo": row[2],
        "activo_nombre": row[3],
        "tipo": row[4],
        "titulo": row[5],
        "descripcion": row[6] or "",
        "prioridad": row[7],
        "estado": row[8],
        "programado_para": row[9].isoformat() if row[9] else None,
        "completado_en": row[10].isoformat() if row[10] else None,
        "asignado_usuario_id": row[11],
        "asignado_nombre": row[12],
        "creado_por_usuario_id": row[13],
        "created_at": row[14].isoformat() if row[14] else None,
    }


def _refresh_asset_dates_from_work_order(db, activo_id: int, status: str, scheduled_for: Optional[date]) -> None:
    if status != "Completado":
        return
    completed_date = scheduled_for or datetime.now().date()
    cur = db.cursor()
    cur.execute(
        "SELECT frecuencia_dias FROM maintenance_assets WHERE id = %s LIMIT 1",
        (activo_id,)
    )
    row = cur.fetchone()
    if not row:
        return
    frequency_days = int(row[0] or 90)
    next_due = _compute_next_maintenance(completed_date, frequency_days)
    cur.execute(
        """
        UPDATE maintenance_assets
        SET ultimo_mantenimiento = %s,
            proximo_mantenimiento = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (completed_date, next_due, activo_id)
    )


def _list_maintenance_assets(db, limit: int = 200, include_inactive: bool = False) -> List[Dict[str, Any]]:
    cur = db.cursor()
    filters = []
    params: List[Any] = []
    if not include_inactive:
        filters.append("a.activo = TRUE")
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    cur.execute(
        f"""
        SELECT a.id,
               a.codigo,
               a.nombre,
               a.categoria,
               a.sucursal_id,
               s.nombre AS sucursal_nombre,
               a.ubicacion_detalle,
               a.descripcion,
               a.estado_operativo,
               a.frecuencia_dias,
               a.ultimo_mantenimiento,
               a.proximo_mantenimiento,
               a.responsable_usuario_id,
               COALESCE(u.nombre_completo, u.username, 'Sin asignar') AS responsable_nombre,
               a.activo,
               COALESCE(open_orders.total_abiertas, 0) AS ordenes_abiertas
        FROM maintenance_assets a
        LEFT JOIN sucursales s ON s.id = a.sucursal_id
        LEFT JOIN usuarios u ON u.id = a.responsable_usuario_id
        LEFT JOIN (
            SELECT activo_id, COUNT(*) AS total_abiertas
            FROM maintenance_work_orders
            WHERE estado IN ('Pendiente', 'En proceso', 'Vencido')
            GROUP BY activo_id
        ) open_orders ON open_orders.activo_id = a.id
        {where_clause}
        ORDER BY a.proximo_mantenimiento NULLS LAST, a.nombre ASC
        LIMIT %s
        """,
        params + [limit]
    )
    return [_serialize_maintenance_asset_row(row) for row in (cur.fetchall() or [])]


def _list_maintenance_work_orders(db, limit: int = 200) -> List[Dict[str, Any]]:
    cur = db.cursor()
    cur.execute(
        """
        SELECT wo.id,
               wo.activo_id,
               a.codigo,
               a.nombre,
               wo.tipo,
               wo.titulo,
               wo.descripcion,
               wo.prioridad,
               wo.estado,
               wo.programado_para,
               wo.completado_en,
               wo.asignado_usuario_id,
               COALESCE(u.nombre_completo, u.username, 'Sin asignar') AS asignado_nombre,
               wo.creado_por_usuario_id,
               wo.created_at
        FROM maintenance_work_orders wo
        JOIN maintenance_assets a ON a.id = wo.activo_id
        LEFT JOIN usuarios u ON u.id = wo.asignado_usuario_id
        ORDER BY
            CASE wo.estado
                WHEN 'Vencido' THEN 0
                WHEN 'Pendiente' THEN 1
                WHEN 'En proceso' THEN 2
                WHEN 'Completado' THEN 3
                ELSE 4
            END,
            wo.programado_para NULLS LAST,
            wo.created_at DESC
        LIMIT %s
        """,
        (limit,)
    )
    return [_serialize_maintenance_work_order_row(row) for row in (cur.fetchall() or [])]


@app.get("/api/v1/maintenance/lookups")
async def maintenance_lookups(
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    cur = db.cursor()
    cur.execute(
        "SELECT id, nombre, zona FROM sucursales WHERE activo = TRUE ORDER BY nombre"
    )
    sucursales = [
        {"id": row[0], "nombre": row[1], "zona": row[2]}
        for row in (cur.fetchall() or [])
    ]
    cur.execute(
        """
        SELECT id, username, nombre_completo, rol
        FROM usuarios
        WHERE activo = TRUE
        ORDER BY COALESCE(nombre_completo, username), username
        LIMIT 300
        """
    )
    usuarios = [
        {
            "id": row[0],
            "username": row[1],
            "nombre_completo": row[2],
            "rol": row[3],
        }
        for row in (cur.fetchall() or [])
    ]
    return {"sucursales": sucursales, "usuarios": usuarios}


@app.get("/api/v1/maintenance/dashboard")
async def maintenance_dashboard(
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM maintenance_assets WHERE activo = TRUE")
    total_assets = int((cur.fetchone() or [0])[0] or 0)
    cur.execute(
        "SELECT COUNT(*) FROM maintenance_assets WHERE activo = TRUE AND proximo_mantenimiento BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '7 days')"
    )
    due_soon = int((cur.fetchone() or [0])[0] or 0)
    cur.execute(
        "SELECT COUNT(*) FROM maintenance_assets WHERE activo = TRUE AND proximo_mantenimiento < CURRENT_DATE"
    )
    overdue = int((cur.fetchone() or [0])[0] or 0)
    cur.execute(
        """
        SELECT COUNT(*)
        FROM maintenance_work_orders
        WHERE estado = 'Completado'
          AND date_trunc('month', completado_en) = date_trunc('month', CURRENT_DATE)
        """
    )
    completed_this_month = int((cur.fetchone() or [0])[0] or 0)
    cur.execute(
        "SELECT COUNT(*) FROM maintenance_work_orders WHERE estado = 'En proceso'"
    )
    in_progress = int((cur.fetchone() or [0])[0] or 0)
    cur.execute(
        """
        SELECT a.id, a.codigo, a.nombre, a.proximo_mantenimiento, a.estado_operativo, s.nombre
        FROM maintenance_assets a
        LEFT JOIN sucursales s ON s.id = a.sucursal_id
        WHERE a.activo = TRUE
          AND a.proximo_mantenimiento IS NOT NULL
        ORDER BY
            CASE WHEN a.proximo_mantenimiento < CURRENT_DATE THEN 0 ELSE 1 END,
            a.proximo_mantenimiento ASC
        LIMIT 6
        """
    )
    alerts = [
        {
            "id": row[0],
            "codigo": row[1],
            "nombre": row[2],
            "proximo_mantenimiento": row[3].isoformat() if row[3] else None,
            "estado_operativo": row[4],
            "sucursal_nombre": row[5],
        }
        for row in (cur.fetchall() or [])
    ]
    return {
        "summary": {
            "total_assets": total_assets,
            "due_soon": due_soon,
            "overdue": overdue,
            "completed_this_month": completed_this_month,
            "in_progress": in_progress,
        },
        "alerts": alerts,
        "assets": _list_maintenance_assets(db, limit=12),
        "work_orders": _list_maintenance_work_orders(db, limit=12),
    }


@app.get("/api/v1/maintenance/assets")
async def maintenance_assets_list(
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    include_inactive: bool = False,
    limit: int = 200,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    assets = _list_maintenance_assets(db, limit=min(max(limit, 1), 500), include_inactive=include_inactive)
    return {
        "total": len(assets),
        "assets": assets,
    }


@app.post("/api/v1/maintenance/assets")
async def maintenance_assets_create(
    payload: MaintenanceAssetCreate,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    codigo = (payload.codigo or "").strip().upper()
    nombre = (payload.nombre or "").strip()
    if not codigo or not nombre:
        raise HTTPException(status_code=400, detail="Codigo y nombre son obligatorios")
    frecuencia_dias = max(1, int(payload.frecuencia_dias or 1))
    proximo_mantenimiento = _compute_next_maintenance(payload.ultimo_mantenimiento, frecuencia_dias, payload.proximo_mantenimiento)
    cur = db.cursor()
    cur.execute(
        "SELECT 1 FROM maintenance_assets WHERE codigo = %s LIMIT 1",
        (codigo,)
    )
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="Ya existe un activo con ese codigo")
    cur.execute(
        """
        INSERT INTO maintenance_assets (
            codigo, nombre, categoria, sucursal_id, ubicacion_detalle, descripcion,
            estado_operativo, frecuencia_dias, ultimo_mantenimiento, proximo_mantenimiento,
            responsable_usuario_id, activo, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        RETURNING id
        """,
        (
            codigo,
            nombre,
            (payload.categoria or "General").strip() or "General",
            payload.sucursal_id,
            (payload.ubicacion_detalle or "").strip() or None,
            (payload.descripcion or "").strip() or None,
            (payload.estado_operativo or "Operativo").strip() or "Operativo",
            frecuencia_dias,
            payload.ultimo_mantenimiento,
            proximo_mantenimiento,
            payload.responsable_usuario_id,
            bool(payload.activo),
        )
    )
    asset_id = (cur.fetchone() or [None])[0]
    db.commit()
    return {"ok": True, "asset_id": asset_id}


@app.patch("/api/v1/maintenance/assets/{asset_id}")
async def maintenance_assets_update(
    asset_id: int,
    payload: MaintenanceAssetUpdate,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    updates = []
    params: List[Any] = []
    if payload.nombre is not None:
        updates.append("nombre = %s")
        params.append(payload.nombre.strip())
    if payload.categoria is not None:
        updates.append("categoria = %s")
        params.append(payload.categoria.strip() or "General")
    if payload.sucursal_id is not None:
        updates.append("sucursal_id = %s")
        params.append(payload.sucursal_id)
    if payload.ubicacion_detalle is not None:
        updates.append("ubicacion_detalle = %s")
        params.append(payload.ubicacion_detalle.strip() or None)
    if payload.descripcion is not None:
        updates.append("descripcion = %s")
        params.append(payload.descripcion.strip() or None)
    if payload.estado_operativo is not None:
        updates.append("estado_operativo = %s")
        params.append(payload.estado_operativo.strip() or "Operativo")
    if payload.frecuencia_dias is not None:
        updates.append("frecuencia_dias = %s")
        params.append(max(1, int(payload.frecuencia_dias or 1)))
    if payload.ultimo_mantenimiento is not None:
        updates.append("ultimo_mantenimiento = %s")
        params.append(payload.ultimo_mantenimiento)
    if payload.proximo_mantenimiento is not None:
        updates.append("proximo_mantenimiento = %s")
        params.append(payload.proximo_mantenimiento)
    if payload.responsable_usuario_id is not None:
        updates.append("responsable_usuario_id = %s")
        params.append(payload.responsable_usuario_id)
    if payload.activo is not None:
        updates.append("activo = %s")
        params.append(bool(payload.activo))
    if not updates:
        raise HTTPException(status_code=400, detail="No hay cambios para aplicar")
    updates.append("updated_at = NOW()")
    params.append(asset_id)
    cur = db.cursor()
    cur.execute(
        f"UPDATE maintenance_assets SET {', '.join(updates)} WHERE id = %s",
        params,
    )
    db.commit()
    return {"ok": True}


@app.get("/api/v1/maintenance/work-orders")
async def maintenance_work_orders_list(
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    limit: int = 200,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    orders = _list_maintenance_work_orders(db, limit=min(max(limit, 1), 500))
    return {"total": len(orders), "work_orders": orders}


@app.post("/api/v1/maintenance/work-orders")
async def maintenance_work_orders_create(
    payload: MaintenanceWorkOrderCreate,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    titulo = (payload.titulo or "").strip()
    if not titulo:
        raise HTTPException(status_code=400, detail="El titulo de la orden es obligatorio")
    status = _normalize_maintenance_status(payload.estado)
    scheduled_for = payload.programado_para
    cur = db.cursor()
    cur.execute(
        "SELECT 1 FROM maintenance_assets WHERE id = %s LIMIT 1",
        (payload.activo_id,)
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Activo no encontrado")
    cur.execute(
        """
        INSERT INTO maintenance_work_orders (
            activo_id, tipo, titulo, descripcion, prioridad, estado,
            programado_para, completado_en, asignado_usuario_id, creado_por_usuario_id,
            created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        RETURNING id
        """,
        (
            payload.activo_id,
            _normalize_maintenance_type(payload.tipo),
            titulo,
            (payload.descripcion or "").strip() or None,
            _normalize_maintenance_priority(payload.prioridad),
            status,
            scheduled_for,
            datetime.now() if status == "Completado" else None,
            payload.asignado_usuario_id,
            payload.creado_por_usuario_id or usuario_id,
        )
    )
    work_order_id = (cur.fetchone() or [None])[0]
    _refresh_asset_dates_from_work_order(db, payload.activo_id, status, scheduled_for)
    db.commit()
    return {"ok": True, "work_order_id": work_order_id}


@app.patch("/api/v1/maintenance/work-orders/{work_order_id}")
async def maintenance_work_orders_update(
    work_order_id: int,
    payload: MaintenanceWorkOrderUpdate,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    db=Depends(get_db),
):
    _require_maintenance_access(db, username=username, usuario_id=usuario_id, role=role)
    updates = []
    params: List[Any] = []
    status = None
    scheduled_for = payload.programado_para
    if payload.tipo is not None:
        updates.append("tipo = %s")
        params.append(_normalize_maintenance_type(payload.tipo))
    if payload.titulo is not None:
        updates.append("titulo = %s")
        params.append(payload.titulo.strip())
    if payload.descripcion is not None:
        updates.append("descripcion = %s")
        params.append(payload.descripcion.strip() or None)
    if payload.prioridad is not None:
        updates.append("prioridad = %s")
        params.append(_normalize_maintenance_priority(payload.prioridad))
    if payload.estado is not None:
        status = _normalize_maintenance_status(payload.estado)
        updates.append("estado = %s")
        params.append(status)
        updates.append("completado_en = %s")
        params.append(datetime.now() if status == "Completado" else None)
    if payload.programado_para is not None:
        updates.append("programado_para = %s")
        params.append(payload.programado_para)
    if payload.asignado_usuario_id is not None:
        updates.append("asignado_usuario_id = %s")
        params.append(payload.asignado_usuario_id)
    if not updates:
        raise HTTPException(status_code=400, detail="No hay cambios para aplicar")
    updates.append("updated_at = NOW()")
    params.append(work_order_id)
    cur = db.cursor()
    cur.execute(
        "SELECT activo_id, programado_para FROM maintenance_work_orders WHERE id = %s LIMIT 1",
        (work_order_id,)
    )
    existing = cur.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    activo_id = existing[0]
    if scheduled_for is None:
        scheduled_for = existing[1]
    cur.execute(
        f"UPDATE maintenance_work_orders SET {', '.join(updates)} WHERE id = %s",
        params,
    )
    if status is not None:
        _refresh_asset_dates_from_work_order(db, activo_id, status, scheduled_for)
    db.commit()
    return {"ok": True}


def _store_labelsapp_history(db, payload, sucursal_slug: str, operador_label: Optional[str], estado_envio: str = "Enviado") -> None:
    _ensure_labelsapp_history_table(db)
    cur = db.cursor()
    total_items = len(payload.items or [])
    total_unidades = sum(max(1, int(item.cantidad or 1)) for item in (payload.items or []))
    productos_json = json.dumps([item.dict() for item in (payload.items or [])], ensure_ascii=False)
    cur.execute(
        """
        INSERT INTO labelsapp_historial (
            id_factura,
            id_cliente,
            sucursal,
            username,
            usuario_id,
            operador,
            prioridad,
            total_items,
            total_unidades,
            productos_json,
            estado_envio,
            origen,
            fecha_envio
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        """,
        (
            payload.id_factura,
            (payload.id_cliente or None),
            sucursal_slug,
            payload.username,
            payload.usuario_id,
            operador_label,
            (payload.prioridad or "Media").strip().title(),
            total_items,
            total_unidades,
            productos_json,
            estado_envio,
            "web",
            datetime.now(),
        )
    )


def _generate_order_id(id_factura: str, codigo: str, idx: int) -> str:
    seed = f"{id_factura}|{codigo}|{idx}|{time.time_ns()}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:9].upper()
    return f"O{digest}"


# Cache TTL para _calculate_codigo_base: el frontend (labelsapp_web.html)
# llama POST /labelsapp/codigo-base por cada cambio de linea en el carrito,
# y cada llamada hace hasta 8 SELECT a ReglasCodigo. Con 20 lineas y varios
# operadores, esto saturaba el pool. El resultado solo depende de
# (base, producto, terminacion) + datos relativamente estaticos en BD, asi
# que cachear 5 min en RAM es seguro.
_CODIGO_BASE_CACHE_TTL = 300.0
_codigo_base_cache: Dict[tuple, tuple] = {}  # key -> (codigo, expires_at)
_codigo_base_cache_lock = Lock()


def _codigo_base_cache_get(key: tuple) -> Optional[str]:
    now = time.time()
    with _codigo_base_cache_lock:
        entry = _codigo_base_cache.get(key)
        if not entry:
            return None
        codigo, expires_at = entry
        if expires_at < now:
            _codigo_base_cache.pop(key, None)
            return None
        return codigo


def _codigo_base_cache_set(key: tuple, codigo: str) -> None:
    with _codigo_base_cache_lock:
        # Limite duro para evitar growth ilimitado.
        if len(_codigo_base_cache) > 5000:
            _codigo_base_cache.clear()
        _codigo_base_cache[key] = (codigo, time.time() + _CODIGO_BASE_CACHE_TTL)


def _calculate_codigo_base(db, base: str, producto: str, terminacion: str) -> str:
    if not base or not producto or not terminacion:
        return ""

    cache_key = (
        (base or "").strip().lower(),
        (producto or "").strip().lower(),
        (terminacion or "").strip().lower(),
    )
    cached = _codigo_base_cache_get(cache_key)
    if cached is not None:
        return cached

    result = _calculate_codigo_base_uncached(db, base, producto, terminacion)
    _codigo_base_cache_set(cache_key, result)
    return result


def _calculate_codigo_base_uncached(db, base: str, producto: str, terminacion: str) -> str:

    def _norm(s: str) -> str:
        text = (s or "").strip().lower()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"\s+", " ", text)
        return text

    prod = _norm(producto)
    term = _norm(terminacion)
    base_clean = (base or "").strip()
    base_norm = _norm(base_clean)

    # Regla fija de negocio: Texturizado (incluyendo alias mal escritos) usa este código base.
    if any(alias in prod for alias in ["texturizado", "exc. texturizado", "exc texturizado", "exc. texdturizado", "exc texdturizado"]):
        return "A44WGBX01-"

    # Alias de terminaciones comunes (alineado al escritorio).
    term_alias = {
        "flat": "mate",
        "mate": "mate",
        "satin": "satin",
        "satinado": "satin",
        "semi gloss": "semigloss",
        "semigloss": "semigloss",
        "sgi": "semigloss",
        "semi mate": "semimate",
        "semimate": "semimate",
        "gloss": "gloss",
        "brillo": "brillo",
        "semisatin": "semisatin",
    }
    term_canon = term_alias.get(term, term)

    # Overrides de negocio solicitados: deben ganar a reglas antiguas en BD.
    if "industrial enamel" in prod and term_canon in ("brillo", "gloss"):
        if base_norm == "extra white":
            return "B54W101-"
        if base_norm == "ultra deep":
            return "B54T104-"

    if (("water-base pre catalyzed" in prod) or ("water base pre catalyzed" in prod)) and term_canon in ("brillo", "gloss"):
        if base_norm == "extra white":
            return "K45W01151-"
        if base_norm == "ultra deep":
            return "K45T02154-"

    if "kem kromik 150" in prod and term_canon in ("brillo", "gloss"):
        if base_norm == "extra white":
            return "N41W651-"
        if base_norm == "ultra deep":
            return "N41T654-"

    def _expand_template(template: str, row_map: dict) -> str:
        def repl(match):
            token = (match.group(1) or "").strip().lower()
            value = row_map.get(token)
            return "" if value is None else str(value).strip()

        return re.sub(r"\{([a-zA-Z0-9_]+)\}", repl, template)

    def _is_excello_premium() -> bool:
        return "excello premium" in prod

    def _special_excello_from_desktop(row_map: dict) -> str | None:
        # Lógica tomada del escritorio para Excello Premium + bases Ultra Deep.
        if not _is_excello_premium():
            return None

        es_ultra_deep_ii = any(k in base_norm for k in ["ultra deep ii", "ultradeep ii", "ultra-deep ii", "ultra deep 2"])
        if es_ultra_deep_ii:
            return "PP4-"

        es_ultra_deep = ("ultra deep" in base_norm) and (not es_ultra_deep_ii)
        if es_ultra_deep:
            if term_canon == "semisatin":
                return "A27WDR03-"
            return "No Aplica"

        if not row_map:
            return None
        if term_canon == "mate":
            return str(row_map.get("flat") or "").strip() or "No Aplica"
        if term_canon == "satin":
            return str(row_map.get("satin") or "").strip() or "No Aplica"
        if term_canon == "semigloss":
            return str(row_map.get("sgi") or "").strip() or "No Aplica"
        return "No Aplica"

    row_map = {}
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM CodigoBase WHERE base ILIKE %s LIMIT 1", (base_clean,))
        row = cur.fetchone()
        if row:
            cols = [desc[0].lower() for desc in cur.description]
            row_map = {cols[i]: row[i] for i in range(len(cols))}
    except Exception:
        row_map = {}

    try:
        cur = db.cursor()

        # Estrategia de búsqueda más flexible para ReglasCodigo (alineada al escritorio):
        # 1) exacta por producto + terminación + base
        # 2) exacta por producto + terminación sin base fija
        # 3) contiene producto + terminación + base
        # 4) contiene producto + terminación sin base fija
        search_pairs = [(prod, term_canon)]
        if term_canon in ("brillo", "gloss"):
            search_pairs.append((prod, "gloss" if term_canon == "brillo" else "brillo"))

        query_patterns = []
        for pval, tval in search_pairs:
            query_patterns.extend([
                (pval, tval, base_clean, base_clean),
                (pval, tval, "", ""),
                (f"%{pval}%", tval, base_clean, base_clean),
                (f"%{pval}%", tval, "", ""),
            ])

        codigo = ""
        for p_q, t_q, b_q, b_order in query_patterns:
            cur.execute(
                """
                SELECT codigo
                FROM ReglasCodigo
                WHERE activo = TRUE
                  AND producto ILIKE %s
                  AND terminacion ILIKE %s
                  AND (base_color ILIKE %s OR base_color IS NULL)
                ORDER BY CASE WHEN base_color ILIKE %s THEN 0 ELSE 1 END, prioridad DESC NULLS LAST, id DESC
                LIMIT 1
                """,
                (p_q, t_q, b_q, b_order)
            )
            rule = cur.fetchone()
            if rule and rule[0]:
                codigo = str(rule[0]).strip()
                break

        if codigo:
            if "{" in codigo and "}" in codigo and row_map:
                codigo = _expand_template(codigo, row_map)
            if codigo:
                return codigo

        # Sin regla en tabla: aplicar lógica hardcodeada del escritorio.
        if not row_map:
            return "No encontrado"

        tath = str(row_map.get("tath") or "").strip()
        tath2 = str(row_map.get("tath2") or "").strip()
        tath3 = str(row_map.get("tath3") or "").strip()
        flat = str(row_map.get("flat") or "").strip()
        satin = str(row_map.get("satin") or "").strip()
        sgi = str(row_map.get("sgi") or "").strip()
        flat2 = str(row_map.get("flat2") or "").strip()
        satin3 = str(row_map.get("satin3") or "").strip()
        sg4 = str(row_map.get("sg4") or "").strip()
        satinkq = str(row_map.get("satinkq") or "").strip()
        flatkp = str(row_map.get("flatkp") or "").strip()
        flatmp = str(row_map.get("flatmp") or "").strip()
        flatcov = str(row_map.get("flatcov") or "").strip()
        flatpas = str(row_map.get("flatpas") or "").strip()
        satinem = str(row_map.get("satinem") or "").strip()
        sgem = str(row_map.get("sgem") or "").strip()
        flatsp = str(row_map.get("flatsp") or "").strip()
        satinsp = str(row_map.get("satinsp") or "").strip()
        glossp = str(row_map.get("glossp") or "").strip()
        flatap = str(row_map.get("flatap") or "").strip()
        satinap = str(row_map.get("satinap") or "").strip()
        satinsan = str(row_map.get("satinsan") or "").strip()

        term_cmp = term_canon
        base_color = base_norm

        es_esmalte = "esmalte multiuso" in prod
        es_kempro = "kem pro" in prod
        es_kemaqua = "kem aqua" in prod
        es_masterpaint = "master paint" in prod
        es_pastel = "excello pastel" in prod
        es_emerald = "emerald" in prod
        es_superpaint = "super paint" in prod
        es_superpaintAP = "airpurtec" in prod
        es_sanitizing = "sanitizing" in prod
        es_laca = "laca" in prod
        es_EsmalteIndustrial = "esmalte kem" in prod
        es_uretano = "uretano" in prod
        es_tintealthinner = "tinte al thinner" in prod
        es_monocapa = "monocapa" in prod
        es_excellocov = "excello voc" in prod
        es_excellopremium = "excello premium" in prod
        es_waterblocking = "water blocking" in prod
        es_airpuretec = "airpuretec" in prod
        es_hcsiloconeacr = "h&c silicone-acrylic" in prod
        es_hcheavyshield = "h&c heavy-shield" in prod
        es_ProMarEgShel = "promar® 200 voc" in prod
        es_ProMarEgShel400 = "promar® 400 voc" in prod
        es_proindustrialDTM = "pro industrial dtm" in prod
        es_armoseal = "armoseal 1000hs" in prod
        es_armosealtp = "armoseal t-p" in prod
        es_scufftuff = "scuff tuff-wb" in prod
        es_UrethaneAlkyd = "urethane alkyd" in prod
        es_industrialenamels = "industrial enamel" in prod
        es_waterbase_precat = ("water-base pre catalyzed" in prod) or ("water base pre catalyzed" in prod)
        es_kem_kromik_150 = "kem kromik 150" in prod
        es_macropoxy646 = "macropoxy 646" in prod
        es_sherplate600 = "sherplate 600" in prod
        es_macropoxy4600 = "macropoxy 4600" in prod
        es_tileclad = "tile clad" in prod
        es_acrolon7300 = "acrolon 7300" in prod
        es_acrolon218 = "acrolon 218" in prod
        es_ARMORSEAL_HS_PT = "armorseal hs-pt" in prod
        es_HISOLIDS_EP = "hi-solids pt" in prod
        es_HISOLIDS_EP250 = "hi-solids 250" in prod
        es_ARMORSEAL_REXTHANE = "armorseal rexthane" in prod
        es_duraplate = "dura-plate 235" in prod
        es_duraplatePW = "dura-plate pw" in prod
        es_WATER_BASECATALYZED = "water-base catalyzed" in prod
        es_SHER_LOXANE_800 = "sher-loxane 800" in prod

        # Estrategia 2 del escritorio: product_code_generator (si existe).
        try:
            try:
                from product_code_generator import get_product_code  # type: ignore
            except Exception:
                get_product_code = None
            if get_product_code:
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
                    'es_waterbase_precat': es_waterbase_precat,
                    'es_kem_kromik_150': es_kem_kromik_150,
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
                codigo_gen = get_product_code(product_flags, term_cmp, base_color, variables=variables_map)
                if isinstance(codigo_gen, str) and codigo_gen not in ("No Aplica", "Error", "No encontrado") and codigo_gen:
                    return codigo_gen
        except Exception:
            pass

        if es_SHER_LOXANE_800:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B80W501-"
                if base_color == "ultra deep":
                    return "B80T504-"
            return "No Aplica"

        if es_WATER_BASECATALYZED:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B73W311-"
            return "No Aplica"

        if es_duraplatePW:
            if term_cmp == "semigloss" and base_color == "extra white":
                return "B67WX235-"
            return "No Aplica"

        if es_duraplate:
            if term_cmp == "semigloss" and base_color == "extra white":
                return "B67W235-"
            return "No Aplica"

        if es_ARMORSEAL_REXTHANE:
            if term_cmp in ("brillo", "gloss") and base_color == "extra white":
                return "B65W60-"
            return "No Aplica"

        if es_HISOLIDS_EP250:
            if term_cmp in ("brillo", "gloss") and base_color == "extra white":
                return "B65WJ311-"
            return "No Aplica"

        if es_HISOLIDS_EP:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B65W311-"
                if base_color == "ultra deep":
                    return "B65T304-"
            return "No Aplica"

        if es_ARMORSEAL_HS_PT:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B65W220-"
                if base_color == "ultra deep":
                    return "B65T220-"
            return "No Aplica"

        if es_acrolon218:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B65W611-"
                if base_color == "ultra deep":
                    return "B65T604-"
            elif term_cmp == "semigloss":
                if base_color == "extra white":
                    return "B65W651-"
                if base_color == "ultra deep":
                    return "B65T654-"
            return "No Aplica"

        if es_acrolon7300:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B65W01301-"
                if base_color == "ultra deep":
                    return "B65T01304-3"
            return "No Aplica"

        if es_tileclad:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B62WZ111-"
                if base_color == "ultra deep":
                    return "B62TZ104-"
            return "No Aplica"

        if es_macropoxy4600:
            if term_cmp == "semigloss" and base_color == "extra white":
                return "B58WW730-"
            return "No Aplica"

        if es_macropoxy646:
            if term_cmp == "semigloss":
                if base_color == "extra white":
                    return "B58W610-"
                if base_color == "ultra deep":
                    return "B58T604-"
            return "No Aplica"

        if es_sherplate600:
            if term_cmp in ("brillo", "gloss") and base_color == "extra white":
                return "B58W681-"
            return "No Aplica"

        if es_kemaqua:
            return satinkq if term_cmp == "satin" else "No Aplica"

        if es_airpuretec:
            if term_cmp == "mate":
                if base_color == "extra white":
                    return "A86W00061-"
                if base_color == "deep":
                    return "A86W00063-"
            elif term_cmp == "satin":
                if base_color == "extra white":
                    return "A87W00061-"
                if base_color == "deep":
                    return "A87W00063-"
            return "No Aplica"

        if es_waterblocking:
            return "LX12WDR50-" if term_cmp == "mate" else "No Aplica"

        if es_excellocov:
            if term_cmp == "mate":
                return "A30WDR2651"
            if term_cmp == "satin":
                return "A20WDR2651-"
            return "No Aplica"

        if es_laca:
            return "L15-" if term_cmp in ("mate", "semimate", "brillo") else "No Aplica"

        if es_EsmalteIndustrial:
            return "F300-" if term_cmp in ("mate", "semimate", "brillo") else "No Aplica"

        if es_hcsiloconeacr:
            if term_cmp in ("mate", "satin"):
                if base_color == "extra white":
                    return "20.101214-"
                if base_color == "deep":
                    return "20.102214-"
                if base_color == "ultra deep":
                    return "20.103214-"
            return "No Aplica"

        if es_proindustrialDTM:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B66W1051-"
                if base_color == "ultra deep":
                    return "B66T1054-"
            return "No Aplica"

        if es_scufftuff:
            if term_cmp == "mate":
                if base_color == "extra white":
                    return "S23W00051-"
                if base_color == "ultra deep":
                    return "S23T00154-"
                if base_color == "deep":
                    return "S23W00153-"
            elif term_cmp == "satin":
                return "S24W00051-"
            elif term_cmp == "semigloss":
                return "S26W00051-"
            return "No Aplica"

        if es_UrethaneAlkyd:
            if term_cmp in ("brillo", "gloss", "eggshell"):
                if base_color == "extra white":
                    return "B54W00151-"
                if base_color == "ultra deep":
                    return "B54T00154-"
            return "No Aplica"

        if es_industrialenamels:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B54W101-"
                if base_color == "ultra deep":
                    return "B54T104-"
            return "No Aplica"

        if es_waterbase_precat:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "K45W01151-"
                if base_color == "ultra deep":
                    return "K45T02154-"
            return "No Aplica"

        if es_kem_kromik_150:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "N41W651-"
                if base_color == "ultra deep":
                    return "N41T654-"
            return "No Aplica"

        if es_hcheavyshield:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "35.100214-"
                if base_color == "deep":
                    return "35.100314-"
                if base_color == "ultra deep":
                    return "35.100414-"
            return "No Aplica"

        if es_ProMarEgShel:
            if term_cmp == "satin":
                if base_color == "deep":
                    return "B20W02653-"
                if base_color == "extra white":
                    return "B20W12651-"
            elif term_cmp == "mate":
                if base_color == "ultra deep":
                    return "B30T02654-"
                if base_color == "extra white":
                    return "B30W02651-"
                if base_color == "deep":
                    return "B30W02653-"
            elif term_cmp == "semigloss":
                if base_color == "extra white":
                    return "B31W02651-"
            return "No Aplica"

        if es_ProMarEgShel400:
            if term_cmp == "satin" and base_color == "extra white":
                return "B20W04651-"
            return "No Aplica"

        if es_armoseal:
            if term_cmp in ("brillo", "gloss"):
                if base_color == "extra white":
                    return "B67W2001-"
                if base_color == "ultra deep":
                    return "B67T2004-"
            return "No Aplica"

        if es_armosealtp:
            if term_cmp == "semigloss":
                if base_color == "extra white":
                    return "B90T104-"
                if base_color == "ultra deep":
                    return "B90W111-"
            return "No Aplica"

        if es_uretano:
            if term_cmp in ("mate", "semimate", "brillo"):
                if base_color == "extra white":
                    return "ASPPA-"
                if base_color in ("deep", "ultra deep"):
                    return "ASPPB-"
                return "ASPPD-"
            return "No Aplica"

        if es_tintealthinner:
            if term_cmp == "claro":
                return tath
            if term_cmp == "intermedio":
                return tath2
            if term_cmp == "especial":
                return tath3
            return "No Aplica"

        if es_monocapa:
            if term_cmp in ("mate", "semimate", "brillo"):
                if base_color == "extra white":
                    return "ASMCA-"
                if base_color in ("deep", "ultra deep"):
                    return "ASMCB-"
                return "ASMCD-"
            return "No Aplica"

        if es_esmalte:
            if term_cmp == "mate":
                return flat2
            if term_cmp == "satin":
                return satin3
            if term_cmp in ("brillo", "gloss"):
                return sg4
            return "No Aplica"

        if es_kempro:
            return flatkp if term_cmp == "mate" else "No Aplica"

        if es_masterpaint:
            return flatmp if term_cmp == "mate" else "No Aplica"

        if es_pastel:
            return flatpas if term_cmp == "mate" else "No Aplica"

        if es_emerald:
            if term_cmp == "satin":
                return "K37W02751-"
            if term_cmp == "semigloss":
                if "extra white" in base_color:
                    return "K38W02751-"
                if "ultradeep" in base_color or "ultra deep" in base_color or "ultra-deep" in base_color:
                    return "K38T01754-"
                if "deep" in base_color and "ultra" not in base_color:
                    return "K38W01753-"
                return "K38W02751-"
            return "No Aplica"

        if es_superpaint:
            if term_cmp == "mate":
                return flatsp
            if term_cmp == "satin":
                return satinsp
            if term_cmp in ("brillo", "gloss"):
                return glossp
            return "No Aplica"

        if es_superpaintAP:
            if term_cmp == "mate":
                return flatap
            if term_cmp == "satin":
                return satinap
            return "No Aplica"

        if es_sanitizing:
            return satinsan if term_cmp == "satin" else "No Aplica"

        if es_excellopremium:
            es_ultra_deep_ii = any(k in base_color for k in ["ultra deep ii", "ultradeep ii", "ultra-deep ii", "ultra deep 2"])
            if es_ultra_deep_ii:
                return "PP4-"

            es_ultra_deep = ("ultra deep" in base_color) and not es_ultra_deep_ii
            if es_ultra_deep:
                return "A27WDR03-" if term_cmp == "semisatin" else "No Aplica"

            if term_cmp == "mate":
                return flat
            if term_cmp == "satin":
                return satin
            if term_cmp == "semigloss":
                return sgi
            return "No Aplica"

        # Fallback final del escritorio.
        if term_cmp in ("mate", "flat"):
            return flat if flat else "No Aplica"
        if term_cmp in ("satin", "satin"):
            return satin if satin else "No Aplica"
        if term_cmp in ("semigloss", "sgi"):
            return sgi if sgi else "No Aplica"
        return "No Aplica"

    except Exception:
        pass
    return "Error"


@app.get("/api/v1/labelsapp/products")
async def labelsapp_products(
    q: str = "",
    limit: int = 250,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    """Buscar productos para LabelsApp Web"""
    try:
        # Cap defensivo: limit absurdamente alto (ej. 50000) hace que un solo cliente
        # mate el pool. 2000 es mas que suficiente para autocomplete.
        safe_limit = None if limit <= 0 else max(1, min(limit, 2000))
        query = (q or "").strip()
        requester_sucursal_slug = _resolve_sucursal_slug(
            db,
            username=username,
            usuario_id=usuario_id,
            sucursal_text=sucursal,
        )
        cache_key = (
            requester_sucursal_slug,
            query.lower(),
            int(safe_limit or 0),
        )
        cached = _products_cache_get(cache_key)
        if cached is not None:
            return cached

        with _get_products_fetch_lock(cache_key):
            cached = _products_cache_get(cache_key)
            if cached is not None:
                return cached

            cur = db.cursor()
            # Time-budget por request: si por alguna razon falta el indice trigram, evita
            # que un ILIKE secuencial se coma 7 minutos y bloquee al resto.
            try:
                cur.execute("SET LOCAL statement_timeout = '8s'")
            except Exception:
                pass
            if query:
                params = (f"%{query}%", f"%{query}%")
                sql = """
                    SELECT codigo, nombre, COALESCE(base, ''), COALESCE(ubicacion, '')
                    FROM ProductSW
                    WHERE (activo = TRUE OR activo IS NULL)
                      AND (codigo ILIKE %s OR nombre ILIKE %s)
                    ORDER BY nombre
                """
                if safe_limit is not None:
                    sql += "\n                    LIMIT %s"
                    params = params + (safe_limit,)
                cur.execute(sql, params)
            else:
                sql = """
                    SELECT codigo, nombre, COALESCE(base, ''), COALESCE(ubicacion, '')
                    FROM ProductSW
                    WHERE (activo = TRUE OR activo IS NULL)
                    ORDER BY nombre
                """
                params = ()
                if safe_limit is not None:
                    sql += "\n                    LIMIT %s"
                    params = (safe_limit,)
                cur.execute(sql, params)

            rows = cur.fetchall()
            products = [
                {
                    "codigo": r[0],
                    "nombre": r[1],
                    "base": r[2],
                    "ubicacion": r[3],
                }
                for r in rows
            ]

            personalized = _list_personalizados_for_sucursal(db, requester_sucursal_slug)
            existing = {str(p.get("codigo") or "").strip().lower() for p in products}
            for item in personalized:
                code = (item.get("codigo") or "").strip()
                if not code or code.lower() in existing:
                    continue
                products.append({
                    "codigo": code,
                    "nombre": (item.get("nombre") or "").strip(),
                    "base": (item.get("base") or "").strip(),
                    "ubicacion": (item.get("ubicacion") or "").strip(),
                    "personalizado": True,
                })
                existing.add(code.lower())

            if query:
                qn = query.lower()
                products = [
                    p for p in products
                    if qn in str(p.get("codigo") or "").lower() or qn in str(p.get("nombre") or "").lower()
                ]

            products.sort(key=lambda p: (str(p.get("nombre") or "").lower(), str(p.get("codigo") or "").lower()))

            if safe_limit is not None:
                products = products[:safe_limit]
            response_data = {"total": len(products), "productos": products}
            _products_cache_set(cache_key, response_data)
            return response_data
    except Exception as e:
        logger.error(f"Error listing labelsapp products: {e}")
        raise HTTPException(status_code=500, detail="Error listando productos")


@app.get("/api/v1/labelsapp/product/{codigo}")
async def labelsapp_product_by_code(
    codigo: str,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    """Obtener producto por código para autocompletar formulario"""
    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT codigo, nombre, COALESCE(base, ''), COALESCE(ubicacion, '')
            FROM ProductSW
            WHERE codigo = %s
            LIMIT 1
            """,
            (codigo,)
        )
        row = cur.fetchone()
        if not row:
            requester_sucursal_slug = _resolve_sucursal_slug(
                db,
                username=username,
                usuario_id=usuario_id,
                sucursal_text=sucursal,
            )
            personalized = _get_personalizado_by_codigo(db, requester_sucursal_slug, codigo)
            if personalized:
                return personalized
            raise HTTPException(status_code=404, detail="Producto no encontrado")
        return {
            "codigo": row[0],
            "nombre": row[1],
            "base": row[2],
            "ubicacion": row[3],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting labelsapp product: {e}")
        raise HTTPException(status_code=500, detail="Error obteniendo producto")


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "si", "y"}
    return False


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _normalize_sherwin_result(item: Dict[str, Any]) -> Dict[str, Any]:
    descriptions = item.get("description")
    if isinstance(descriptions, str):
        descriptions = [descriptions]
    elif not isinstance(descriptions, list):
        descriptions = []

    similar_colors = item.get("similarColors")
    if isinstance(similar_colors, str):
        similar_colors = [similar_colors]
    elif not isinstance(similar_colors, list):
        similar_colors = []

    branded_collections = item.get("brandedCollectionNames")
    if isinstance(branded_collections, str):
        branded_collections = [branded_collections]
    elif not isinstance(branded_collections, list):
        branded_collections = []

    family_names = item.get("colorFamilyNames")
    if isinstance(family_names, str):
        family_names = [family_names]
    elif not isinstance(family_names, list):
        family_names = []

    lab = item.get("lab") if isinstance(item.get("lab"), dict) else {}
    coord = item.get("coordinatingColors") if isinstance(item.get("coordinatingColors"), dict) else None

    normalized = {
        "colorNumber": str(item.get("colorNumber") or ""),
        "description": [str(x) for x in descriptions],
        "id": str(item.get("id") or ""),
        "isExterior": _to_bool(item.get("isExterior")),
        "isInterior": _to_bool(item.get("isInterior")),
        "name": str(item.get("name") or ""),
        "lrv": _to_float(item.get("lrv"), 0.0),
        "brandedCollectionNames": [str(x) for x in branded_collections],
        "colorFamilyNames": [str(x) for x in family_names],
        "brandKey": str(item.get("brandKey") or ""),
        "red": _to_int(item.get("red"), 0),
        "green": _to_int(item.get("green"), 0),
        "blue": _to_int(item.get("blue"), 0),
        "hue": _to_float(item.get("hue"), 0.0),
        "saturation": _to_float(item.get("saturation"), 0.0),
        "lightness": _to_float(item.get("lightness"), 0.0),
        "hex": str(item.get("hex") or ""),
        "isDark": _to_bool(item.get("isDark")),
        "storeStripLocator": (str(item.get("storeStripLocator")) if item.get("storeStripLocator") is not None else None),
        "similarColors": [str(x) for x in similar_colors],
        "ignore": _to_bool(item.get("ignore")),
        "archived": _to_bool(item.get("archived")),
        "lab": {
            "L": _to_float(lab.get("L"), 0.0),
            "A": _to_float(lab.get("A"), 0.0),
            "B": _to_float(lab.get("B"), 0.0),
        },
    }

    if coord:
        normalized["coordinatingColors"] = {
            "coord1ColorId": str(coord.get("coord1ColorId") or ""),
            "coord2ColorId": str(coord.get("coord2ColorId") or ""),
            "whiteColorId": (str(coord.get("whiteColorId")) if coord.get("whiteColorId") is not None else None),
        }

    return normalized


def _pick_best_sherwin_result(results: List[Dict[str, Any]], sw_code: str) -> Optional[Dict[str, Any]]:
    if not results:
        return None
    compact = (sw_code or "").replace("SW", "").strip().upper()
    exact = next((r for r in results if str(r.get("colorNumber") or "").replace(" ", "").upper() == compact), None)
    if exact:
        return exact
    ends = next((r for r in results if str(r.get("colorNumber") or "").replace(" ", "").upper().endswith(compact)), None)
    if ends:
        return ends
    return results[0]


def _cached_sherwin_swatch_get(sw_code: str) -> Optional[Dict[str, Any]]:
    key = (sw_code or "").strip().upper()
    if not key:
        return None
    with SHERWIN_SWATCH_CACHE_LOCK:
        entry = SHERWIN_SWATCH_CACHE.get(key)
        if not entry:
            return None
        if (time.time() - float(entry.get("ts") or 0)) > SHERWIN_SWATCH_CACHE_TTL_SECONDS:
            SHERWIN_SWATCH_CACHE.pop(key, None)
            return None
        return dict(entry)


def _cached_sherwin_swatch_set(sw_code: str, name: str, hex_value: str, found: bool) -> None:
    key = (sw_code or "").strip().upper()
    if not key:
        return
    with SHERWIN_SWATCH_CACHE_LOCK:
        SHERWIN_SWATCH_CACHE[key] = {
            "ts": time.time(),
            "name": (name or "").strip() or f"SW {key}",
            "hex": (hex_value or "").strip(),
            "found": bool(found),
        }


def _execute_sherwin_search_request(req: UrlRequest, timeout_seconds: int) -> Dict[str, Any]:
    with urlopen(req, timeout=timeout_seconds) as resp:
        return {
            "status_code": int(getattr(resp, "status", 200) or 200),
            "body": resp.read().decode("utf-8", errors="replace"),
        }


def _sherwin_circuit_is_open() -> bool:
    with SHERWIN_CIRCUIT_LOCK:
        return float(SHERWIN_CIRCUIT_STATE.get("open_until") or 0) > time.time()


def _sherwin_circuit_register_success() -> None:
    with SHERWIN_CIRCUIT_LOCK:
        SHERWIN_CIRCUIT_STATE["failures"] = 0
        SHERWIN_CIRCUIT_STATE["open_until"] = 0.0


def _sherwin_circuit_register_failure() -> None:
    with SHERWIN_CIRCUIT_LOCK:
        failures = int(SHERWIN_CIRCUIT_STATE.get("failures") or 0) + 1
        SHERWIN_CIRCUIT_STATE["failures"] = failures
        if failures >= SHERWIN_CIRCUIT_FAILURE_THRESHOLD:
            SHERWIN_CIRCUIT_STATE["open_until"] = time.time() + SHERWIN_CIRCUIT_OPEN_SECONDS


async def _fetch_sherwin_swatch_async(sw_code: str, lng: str, corev: str, timeout_seconds: int) -> Dict[str, Any]:
    if _sherwin_circuit_is_open():
        raise RuntimeError("Sherwin circuit open")

    async with SHERWIN_UPSTREAM_SEMAPHORE:
        try:
            data = await asyncio.to_thread(
                _fetch_sherwin_swatch,
                sw_code,
                lng,
                corev,
                timeout_seconds,
            )
            _sherwin_circuit_register_success()
            return data
        except Exception:
            _sherwin_circuit_register_failure()
            raise


def _fetch_sherwin_swatch(sw_code: str, lng: str = "en-US", corev: str = "7.16.0", timeout_seconds: int = 8) -> Dict[str, Any]:
    code = re.sub(r"[^0-9A-Z]", "", str(sw_code or "").upper().replace("SW", "").strip())
    if not code:
        return {"code": "", "name": "", "hex": "", "found": False, "cached": False}

    cached = _cached_sherwin_swatch_get(code)
    if cached:
        return {
            "code": code,
            "name": str(cached.get("name") or f"SW {code}"),
            "hex": str(cached.get("hex") or ""),
            "found": bool(cached.get("found")),
            "cached": True,
        }

    params = {
        "query": code,
        "lng": lng or "en-US",
        "_corev": corev or "7.16.0",
    }
    upstream_url = f"https://api.sherwin-williams.com/prism/v1/search/sherwin?{urlencode(params)}"
    req = UrlRequest(
        upstream_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "PaintFlow/1.0",
        },
        method="GET",
    )

    with urlopen(req, timeout=max(3, min(int(timeout_seconds or 8), 20))) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    raw = json.loads(body or "{}")
    raw_results = raw.get("results") if isinstance(raw, dict) else []
    raw_results = raw_results if isinstance(raw_results, list) else []

    normalized_results: List[Dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        try:
            normalized_results.append(_normalize_sherwin_result(item))
        except Exception:
            continue

    best = _pick_best_sherwin_result(normalized_results, code)
    if not best:
        _cached_sherwin_swatch_set(code, f"SW {code}", "", False)
        return {"code": code, "name": f"SW {code}", "hex": "", "found": False, "cached": False}

    name = str(best.get("name") or f"SW {code}")
    hex_value = str(best.get("hex") or "").strip()
    _cached_sherwin_swatch_set(code, name, hex_value, True)
    return {"code": code, "name": name, "hex": hex_value, "found": True, "cached": False}


@app.get("/api/v1/sherwin/search", response_model=SherwinColorSearchResponse)
async def sherwin_color_search(
    query: str,
    lng: str = "en-US",
    corev: str = "7.16.0",
    timeout_seconds: int = 12,
):
    """Proxy tipado para busqueda de colores Sherwin en Prism."""
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query es requerido")

    timeout_seconds = max(3, min(int(timeout_seconds or 12), 20))

    if _sherwin_circuit_is_open():
        return SherwinColorSearchResponse(count=0, results=[])

    params = {
        "query": query,
        "lng": lng or "en-US",
        "_corev": corev or "7.16.0",
    }
    upstream_url = f"https://api.sherwin-williams.com/prism/v1/search/sherwin?{urlencode(params)}"

    req = UrlRequest(
        upstream_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "PaintFlow/1.0",
        },
        method="GET",
    )

    try:
        async with SHERWIN_UPSTREAM_SEMAPHORE:
            upstream_response = await asyncio.to_thread(_execute_sherwin_search_request, req, timeout_seconds)
        status_code = int(upstream_response.get("status_code") or 200)
        body = str(upstream_response.get("body") or "")

        if status_code >= 400:
            _sherwin_circuit_register_failure()
            raise HTTPException(status_code=502, detail="Sherwin API devolvio error")

        raw = json.loads(body or "{}")
        raw_results = raw.get("results") if isinstance(raw, dict) else []
        if not isinstance(raw_results, list):
            raw_results = []

        typed_results = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            try:
                typed_results.append(SherwinColorResult(**_normalize_sherwin_result(item)))
            except Exception:
                continue

        _sherwin_circuit_register_success()

        return SherwinColorSearchResponse(
            count=_to_int(raw.get("count") if isinstance(raw, dict) else len(typed_results), len(typed_results)),
            results=typed_results,
        )
    except HTTPException:
        raise
    except Exception as e:
        _sherwin_circuit_register_failure()
        logger.error(f"Error querying Sherwin Prism API: {e}")
        raise HTTPException(status_code=502, detail="No se pudo consultar Sherwin API")


@app.get("/api/v1/sherwin/swatches", response_model=SherwinSwatchBatchResponse)
async def sherwin_swatches(
    codes: str,
    lng: str = "en-US",
    corev: str = "7.16.0",
    timeout_seconds: int = 8,
):
    """Obtiene muestras de color (nombre + hex) para varios codigos SW."""
    timeout_seconds = max(3, min(int(timeout_seconds or 8), 20))
    parts = [p.strip() for p in (codes or "").split(",") if p.strip()]
    if not parts:
        raise HTTPException(status_code=400, detail="codes es requerido")

    clean_codes = []
    seen = set()
    for raw in parts:
        compact = re.sub(r"[^0-9A-Z]", "", raw.upper().replace("SW", "").strip())
        if not compact or compact in seen:
            continue
        seen.add(compact)
        clean_codes.append(compact)
        if len(clean_codes) >= 40:
            break

    if _sherwin_circuit_is_open():
        items: List[SherwinSwatchItem] = []
        for code in clean_codes:
            local = _lookup_local_sherwin(code)
            if local and local.get("hex"):
                items.append(SherwinSwatchItem(
                    code=code,
                    name=str(local.get("name") or f"SW {code}"),
                    hex=str(local.get("hex") or ""),
                    found=True,
                    cached=True,
                ))
                continue
            cached = _cached_sherwin_swatch_get(code)
            if cached:
                items.append(SherwinSwatchItem(
                    code=code,
                    name=str(cached.get("name") or f"SW {code}"),
                    hex=str(cached.get("hex") or ""),
                    found=bool(cached.get("found")),
                    cached=True,
                ))
            else:
                items.append(SherwinSwatchItem(code=code, name=f"SW {code}", hex="", found=False, cached=False))
        return SherwinSwatchBatchResponse(total=len(items), items=items)

    items: List[SherwinSwatchItem] = []
    missing_codes: List[str] = []
    for code in clean_codes:
        local = _lookup_local_sherwin(code)
        if local and local.get("hex"):
            items.append(SherwinSwatchItem(
                code=code,
                name=str(local.get("name") or f"SW {code}"),
                hex=str(local.get("hex") or ""),
                found=True,
                cached=True,
            ))
        else:
            missing_codes.append(code)

    if not missing_codes:
        return SherwinSwatchBatchResponse(total=len(items), items=items)

    tasks = [
        _fetch_sherwin_swatch_async(code, lng=lng, corev=corev, timeout_seconds=timeout_seconds)
        for code in missing_codes
    ]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)

    for code, data in zip(missing_codes, fetched):
        try:
            if isinstance(data, Exception):
                raise data
            items.append(SherwinSwatchItem(**data))
        except Exception:
            items.append(SherwinSwatchItem(code=code, name=f"SW {code}", hex="", found=False, cached=False))

    return SherwinSwatchBatchResponse(total=len(items), items=items)


@app.get("/api/v1/sherwin/catalog")
async def sherwin_local_catalog(limit: int = 0, offset: int = 0):
    """Catalogo Sherwin pre-descargado y servido desde disco (sin tocar upstream)."""
    payload = _load_local_sherwin_catalog()
    colors = payload.get("colors") or []
    total = len(colors)
    if limit and limit > 0:
        start = max(0, int(offset or 0))
        end = start + int(limit)
        colors = colors[start:end]
    return {
        "fetched_at": payload.get("fetched_at"),
        "total": total,
        "returned": len(colors),
        "offset": int(offset or 0),
        "colors": colors,
    }


@app.get("/api/v1/sherwin/local/search")
async def sherwin_local_search(query: str = "", limit: int = 50):
    """Busqueda en memoria sobre el catalogo local (code, name, families)."""
    payload = _load_local_sherwin_catalog()
    colors = payload.get("colors") or []
    q = (query or "").strip().lower()
    if not q:
        return {"total": 0, "results": []}

    compact = re.sub(r"[^0-9a-z]", "", q)
    matches: List[Dict[str, Any]] = []
    for c in colors:
        code = str(c.get("code") or "").lower()
        name = str(c.get("name") or "").lower()
        if compact and compact in code:
            matches.append(c)
            continue
        if q in name:
            matches.append(c)
            continue
        families = [str(f or "").lower() for f in (c.get("families") or [])]
        if any(q in f for f in families):
            matches.append(c)

    if limit and limit > 0:
        matches = matches[: int(limit)]
    return {"total": len(matches), "results": matches}


@app.get("/api/v1/labelsapp/personalizados")
async def labelsapp_personalized_products(
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    requester_sucursal_slug = _resolve_sucursal_slug(
        db,
        username=username,
        usuario_id=usuario_id,
        sucursal_text=sucursal,
    )
    items = _list_personalizados_for_sucursal(db, requester_sucursal_slug)
    return {"items": items}


@app.post("/api/v1/labelsapp/personalizados")
async def labelsapp_create_personalized_product(
    payload: LabelsAppPersonalizedProductRequest,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    codigo = (payload.codigo or "").strip()
    nombre = (payload.nombre or "").strip()
    if not codigo or not nombre:
        raise HTTPException(status_code=400, detail="Debe indicar código y nombre")

    if len(codigo) > PERSONALIZADO_CODIGO_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"El código no puede exceder {PERSONALIZADO_CODIGO_MAX} caracteres (contando espacios)",
        )

    if len(nombre) > PERSONALIZADO_NOMBRE_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"La descripción no puede exceder {PERSONALIZADO_NOMBRE_MAX} caracteres",
        )

    requester_sucursal_slug = _resolve_sucursal_slug(
        db,
        username=username,
        usuario_id=usuario_id,
        sucursal_text=sucursal,
    )

    existing = _get_personalizado_by_codigo(db, requester_sucursal_slug, codigo)
    if existing:
        raise HTTPException(status_code=409, detail="El código ya existe en productos personalizados")

    _insert_personalizado(db, requester_sucursal_slug, codigo, nombre)
    _products_cache_invalidate_sucursal(requester_sucursal_slug)
    db.commit()
    return {"message": "Producto personalizado guardado", "codigo": codigo, "nombre": nombre}


@app.delete("/api/v1/labelsapp/personalizados/{codigo}")
async def labelsapp_delete_personalized_product(
    codigo: str,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    requester_sucursal_slug = _resolve_sucursal_slug(
        db,
        username=username,
        usuario_id=usuario_id,
        sucursal_text=sucursal,
    )

    deleted = _delete_personalizado(db, requester_sucursal_slug, codigo)
    if deleted <= 0:
        raise HTTPException(status_code=404, detail="Código no encontrado en productos personalizados")
    _products_cache_invalidate_sucursal(requester_sucursal_slug)
    db.commit()
    return {"message": "Producto personalizado eliminado", "codigo": codigo}


def _append_labelsapp_feedback_row(row: dict) -> None:
    fieldnames = [
        "fecha_registro",
        "modulo",
        "username",
        "usuario_id",
        "sucursal",
        "rendimiento",
        "facilidad_uso",
        "estabilidad",
        "implementacion_web",
        "satisfaccion_general",
        "recomendaria",
        "comentario_general",
        "mejoras_sugeridas",
        "errores_reportados",
        "ip_origen",
    ]

    file_exists = os.path.exists(LABELSAPP_FEEDBACK_CSV_PATH)
    with open(LABELSAPP_FEEDBACK_CSV_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})


@app.post("/api/v1/labelsapp/feedback")
async def labelsapp_feedback_submit(payload: LabelsAppFeedbackRequest, request: Request):
    """Guardar encuesta de percepcion sobre LabelsApp Web."""
    try:
        ratings = [
            int(payload.rendimiento),
            int(payload.facilidad_uso),
            int(payload.estabilidad),
            int(payload.implementacion_web),
            int(payload.satisfaccion_general),
        ]
    except Exception:
        raise HTTPException(status_code=400, detail="Las calificaciones deben ser numericas")

    if any(v < 1 or v > 5 for v in ratings):
        raise HTTPException(status_code=400, detail="Las calificaciones deben estar entre 1 y 5")

    def _clean_text(value: Optional[str], max_len: int = 1200) -> str:
        return (value or "").strip()[:max_len]

    row = {
        "fecha_registro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "modulo": _clean_text(payload.modulo or "labelsapp-web", 64),
        "username": _clean_text(payload.username, 120),
        "usuario_id": str(payload.usuario_id or ""),
        "sucursal": _clean_text(payload.sucursal, 120),
        "rendimiento": str(ratings[0]),
        "facilidad_uso": str(ratings[1]),
        "estabilidad": str(ratings[2]),
        "implementacion_web": str(ratings[3]),
        "satisfaccion_general": str(ratings[4]),
        "recomendaria": _clean_text(payload.recomendaria, 64),
        "comentario_general": _clean_text(payload.comentario_general),
        "mejoras_sugeridas": _clean_text(payload.mejoras_sugeridas),
        "errores_reportados": _clean_text(payload.errores_reportados),
        "ip_origen": _clean_text(getattr(request.client, "host", ""), 120),
    }

    try:
        _append_labelsapp_feedback_row(row)
    except Exception as e:
        logger.error(f"Error saving labelsapp feedback: {e}")
        raise HTTPException(status_code=500, detail="No se pudo guardar la encuesta")

    return {"message": "Gracias por tu retroalimentacion", "saved": True}


@app.post("/api/v1/labelsapp/codigo-base")
async def labelsapp_codigo_base(payload: LabelsAppCodigoBaseRequest, db=Depends(get_db)):
    """Calcular código base aproximado para LabelsApp Web"""
    try:
        codigo = _calculate_codigo_base(db, payload.base, payload.producto, payload.terminacion)
        return {"codigo_base": codigo}
    except Exception as e:
        logger.error(f"Error calculating codigo base: {e}")
        raise HTTPException(status_code=500, detail="Error calculando código base")


@app.post("/api/v1/labelsapp/send")
async def labelsapp_send(payload: LabelsAppSendRequest, db=Depends(get_db)):
    """Enviar lote de productos de LabelsApp Web a la cola de espera"""
    if not payload.items:
        raise HTTPException(status_code=400, detail="Debe enviar al menos un producto")

    try:
        prioridad = (payload.prioridad or "Media").strip().title()
        if prioridad not in ["Alta", "Media", "Baja"]:
            prioridad = "Media"

        sucursal_slug = _resolve_sucursal_slug(
            db,
            username=payload.username,
            usuario_id=payload.usuario_id,
            sucursal_text=payload.sucursal,
        )
        table_name = _safe_table_for_sucursal(sucursal_slug)

        _ensure_pedidos_table(db, table_name)
        columns = _get_table_columns(db, table_name)

        inserted = 0
        cur = db.cursor()
        operador_label = _resolve_operador_label(db, username=payload.username, usuario_id=payload.usuario_id, operador=payload.operador)
        operador_hint = ((payload.operador or operador_label or "").strip().lower())
        factura_hint = (payload.id_factura or "").strip().upper()
        is_kiosk_or_client = (
            operador_hint in {"kiosk_touch", "kiosk", "cliente"}
            or factura_hint.startswith("CLI-")
            or factura_hint.startswith("KIOSK-")
            or factura_hint.startswith("BORRADOR-")
        )
        # Las órdenes siempre entran como "Pendiente" para que el colorista
        # pueda asignarse antes de pasar a "En Proceso".
        estado_inicial = "Pendiente"

        for idx, item in enumerate(payload.items):
            cantidad = max(1, int(item.cantidad or 1))
            item_priority = (item.prioridad or prioridad or "Media").strip().title()
            if item_priority not in ["Alta", "Media", "Baja"]:
                item_priority = prioridad
            codigo_base = (item.codigo_base or "").strip() or _calculate_codigo_base(db, item.base, item.producto, item.terminacion)

            data = {
                "id_orden_profesional": _generate_order_id(payload.id_factura, item.codigo, idx),
                "id_factura": payload.id_factura,
                "id_cliente": payload.id_cliente,
                "codigo": item.codigo,
                "producto": item.producto,
                "terminacion": item.terminacion,
                "presentacion": item.presentacion,
                "cantidad": cantidad,
                "prioridad": item_priority,
                "estado": estado_inicial,
                "base": item.base,
                "ubicacion": item.ubicacion,
                "sucursal": sucursal_slug,
                "codigo_base": codigo_base,
                "fecha_creacion": datetime.now(),
            }

            if operador_label:
                data["operador"] = operador_label

            insert_cols = [c for c in data.keys() if c in columns]
            values = [data[c] for c in insert_cols]

            if not insert_cols:
                continue

            placeholders = ", ".join(["%s"] * len(insert_cols))
            sql = f"INSERT INTO {table_name} ({', '.join(insert_cols)}) VALUES ({placeholders})"
            cur.execute(sql, values)
            inserted += 1

        try:
            _notify_labelsapp_update(cur, "labels:web", sucursal_slug, payload.id_factura, inserted=inserted)
        except Exception:
            pass

        _store_labelsapp_history(db, payload, sucursal_slug, operador_label, estado_envio="Enviado")

        db.commit()

        return {
            "message": "Lote enviado a cola correctamente",
            "id_factura": payload.id_factura,
            "sucursal": sucursal_slug,
            "tabla": table_name,
            "inserted": inserted,
        }
    except HTTPException:
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error sending labelsapp batch: {e}")
        raise HTTPException(status_code=500, detail="Error enviando lote a cola")


@app.get("/api/v1/labelsapp/live-queue")
async def labelsapp_live_queue(
    limit: int = 50,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
):
    """Obtener la cola agrupada en tiempo real para la vista web."""
    sucursal_slug = "principal"
    table_name = _safe_table_for_sucursal(sucursal_slug)
    db = None
    try:
        limit = max(1, min(int(limit or 50), 250))

        # Evita tocar DB si la sucursal ya viene en el request.
        if (sucursal or "").strip():
            sucursal_slug = _normalize_sucursal_slug(str(sucursal))
        elif username or usuario_id:
            db = DatabasePool.get_connection()
            sucursal_slug = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)

        table_name = _safe_table_for_sucursal(sucursal_slug)

        cache_key = (table_name, limit)
        items = _queue_cache_get("live", cache_key)
        if items is None:
            if db is None:
                db = DatabasePool.get_connection()

            if not _pedidos_table_exists(db, table_name):
                return {
                    "sucursal": sucursal_slug,
                    "tabla": table_name,
                    "total": 0,
                    "items": [],
                }

            with _get_table_lock(_labelsapp_live_fetch_locks, table_name):
                items = _queue_cache_get("live", cache_key)
                if items is None:
                    _set_local_pg_timeouts(db, statement_ms=2500, lock_ms=500)
                    items = _get_labelsapp_live_queue(db, table_name, limit=limit)
                    _queue_cache_set("live", cache_key, items)

        return {
            "sucursal": sucursal_slug,
            "tabla": table_name,
            "total": len(items),
            "items": items,
        }
    except HTTPException:
        raise
    except Exception as e:
        err_text = str(e).lower()
        if getattr(e, "pgcode", "") in {"57014", "55P03"}:
            logger.warning(f"Live queue timeout/lock for {table_name}: {e}")
            return {
                "sucursal": sucursal_slug,
                "tabla": table_name,
                "total": 0,
                "items": [],
            }
        if (
            isinstance(e, RuntimeError)
            or "database unavailable" in err_text
            or "ssl connection has been closed unexpectedly" in err_text
            or "could not connect to server" in err_text
        ):
            logger.warning(f"Live queue DB unavailable for {table_name}: {e}")
            return {
                "sucursal": sucursal_slug,
                "tabla": table_name,
                "total": 0,
                "items": [],
            }
        logger.error(f"Error fetching labelsapp live queue: {e}")
        raise HTTPException(status_code=500, detail="Error consultando la cola en tiempo real")
    finally:
        if db is not None:
            try:
                DatabasePool.return_connection(db)
            except Exception:
                pass


@app.get("/api/v1/labelsapp/pending-queue")
async def labelsapp_pending_queue(
    limit: int = 80,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
):
    """Obtener la cola pendiente (en espera) por sucursal para facturador."""
    sucursal_slug = "principal"
    table_name = _safe_table_for_sucursal(sucursal_slug)
    db = None
    try:
        limit = max(1, min(int(limit or 80), 250))

        # Evita tocar DB si la sucursal ya viene en el request.
        if (sucursal or "").strip():
            sucursal_slug = _normalize_sucursal_slug(str(sucursal))
        elif username or usuario_id:
            db = DatabasePool.get_connection()
            sucursal_slug = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)

        table_name = _safe_table_for_sucursal(sucursal_slug)

        cache_key = (table_name, limit)
        items = _queue_cache_get("pending", cache_key)
        if items is None:
            if db is None:
                db = DatabasePool.get_connection()

            if not _pedidos_table_exists(db, table_name):
                return {
                    "sucursal": sucursal_slug,
                    "tabla": table_name,
                    "total": 0,
                    "items": [],
                }

            with _get_table_lock(_labelsapp_pending_fetch_locks, table_name):
                items = _queue_cache_get("pending", cache_key)
                if items is None:
                    _set_local_pg_timeouts(db, statement_ms=2500, lock_ms=500)
                    items = _get_labelsapp_pending_queue(db, table_name, limit=limit)
                    _queue_cache_set("pending", cache_key, items)

        return {
            "sucursal": sucursal_slug,
            "tabla": table_name,
            "total": len(items),
            "items": items,
        }
    except HTTPException:
        raise
    except Exception as e:
        err_text = str(e).lower()
        if getattr(e, "pgcode", "") in {"57014", "55P03"}:
            logger.warning(f"Pending queue timeout/lock for {table_name}: {e}")
            return {
                "sucursal": sucursal_slug,
                "tabla": table_name,
                "total": 0,
                "items": [],
            }
        if (
            isinstance(e, RuntimeError)
            or "database unavailable" in err_text
            or "ssl connection has been closed unexpectedly" in err_text
            or "could not connect to server" in err_text
        ):
            logger.warning(f"Pending queue DB unavailable for {table_name}: {e}")
            return {
                "sucursal": sucursal_slug,
                "tabla": table_name,
                "total": 0,
                "items": [],
            }
        logger.error(f"Error fetching labelsapp pending queue: {e}")
        raise HTTPException(status_code=500, detail="Error consultando la cola en espera")
    finally:
        if db is not None:
            try:
                DatabasePool.return_connection(db)
            except Exception:
                pass


@app.get("/api/v1/labelsapp/factura/{id_factura}/items")
async def labelsapp_factura_items(
    id_factura: str,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    try:
        sucursal_slug = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
        table_name = _safe_table_for_sucursal(sucursal_slug)
        _ensure_pedidos_table(db, table_name)
        items = _get_labelsapp_factura_items(db, table_name, id_factura)
        return {
            "id_factura": id_factura,
            "sucursal": sucursal_slug,
            "tabla": table_name,
            "items": items,
        }
    except Exception as e:
        logger.error(f"Error fetching factura items: {e}")
        raise HTTPException(status_code=500, detail="Error consultando items de factura")


@app.patch("/api/v1/labelsapp/factura/{id_factura}/prioridad")
async def labelsapp_factura_priority(
    id_factura: str,
    payload: LabelsAppFacturaPriorityRequest,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    prioridad = (payload.prioridad or "Media").strip().title()
    if prioridad not in ["Alta", "Media", "Baja"]:
        raise HTTPException(status_code=400, detail="Prioridad inválida")

    try:
        sucursal_slug = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
        table_name = _safe_table_for_sucursal(sucursal_slug)
        _ensure_pedidos_table(db, table_name)
        cur = db.cursor()
        cur.execute(
            f"""
            UPDATE {table_name}
            SET prioridad = %s
            WHERE id_factura = %s
              AND TRIM(COALESCE(estado,'')) <> 'Cancelado'
            """,
            (prioridad, id_factura)
        )
        updated = int(cur.rowcount or 0)
        try:
            _notify_labelsapp_update(cur, "labels:prioridad", sucursal_slug, id_factura, prioridad=prioridad, updated=updated)
        except Exception:
            pass
        db.commit()
        return {"id_factura": id_factura, "prioridad": prioridad, "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error updating factura priority: {e}")
        raise HTTPException(status_code=500, detail="Error actualizando prioridad")


@app.patch("/api/v1/labelsapp/factura/{id_factura}/cancel")
async def labelsapp_factura_cancel(
    id_factura: str,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    try:
        sucursal_slug = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
        table_name = _safe_table_for_sucursal(sucursal_slug)
        _ensure_pedidos_table(db, table_name)
        cur = db.cursor()
        cur.execute(
            f"""
            UPDATE {table_name}
            SET estado = 'Cancelado'
            WHERE id_factura = %s
              AND TRIM(COALESCE(estado,'')) <> 'Cancelado'
            """,
            (id_factura,)
        )
        updated = int(cur.rowcount or 0)
        try:
            _notify_labelsapp_update(cur, "labels:cancelado", sucursal_slug, id_factura, cancelados=updated)
        except Exception:
            pass
        db.commit()
        return {"id_factura": id_factura, "cancelados": updated}
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error canceling factura: {e}")
        raise HTTPException(status_code=500, detail="Error cancelando factura")


@app.put("/api/v1/labelsapp/factura/{id_factura}/items")
async def labelsapp_factura_replace_items(
    id_factura: str,
    payload: LabelsAppFacturaItemsUpdateRequest,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    db=Depends(get_db)
):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Debe enviar items para actualizar")

    try:
        prioridad_global = (payload.prioridad or "Media").strip().title()
        if prioridad_global not in ["Alta", "Media", "Baja"]:
            prioridad_global = "Media"

        sucursal_slug = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
        table_name = _safe_table_for_sucursal(sucursal_slug)
        _ensure_pedidos_table(db, table_name)
        columns = _get_table_columns(db, table_name)
        cur = db.cursor()

        cur.execute(
            f"""
            DELETE FROM {table_name}
            WHERE id_factura = %s
              AND TRIM(COALESCE(estado,'')) <> 'Cancelado'
            """,
            (id_factura,)
        )

        inserted = 0
        for idx, item in enumerate(payload.items):
            cantidad = max(1, int(item.cantidad or 1))
            item_priority = (item.prioridad or prioridad_global or "Media").strip().title()
            if item_priority not in ["Alta", "Media", "Baja"]:
                item_priority = prioridad_global

            codigo_base = (item.codigo_base or "").strip() or _calculate_codigo_base(db, item.base, item.producto, item.terminacion)
            data = {
                "id_orden_profesional": _generate_order_id(id_factura, item.codigo, idx),
                "id_factura": id_factura,
                "codigo": item.codigo,
                "producto": item.producto,
                "terminacion": item.terminacion,
                "presentacion": item.presentacion,
                "cantidad": cantidad,
                "prioridad": item_priority,
                "estado": "Pendiente",
                "base": item.base,
                "ubicacion": item.ubicacion,
                "sucursal": sucursal_slug,
                "codigo_base": codigo_base,
                "fecha_creacion": datetime.now(),
            }

            insert_cols = [c for c in data.keys() if c in columns]
            if not insert_cols:
                continue
            values = [data[c] for c in insert_cols]
            placeholders = ", ".join(["%s"] * len(insert_cols))
            sql = f"INSERT INTO {table_name} ({', '.join(insert_cols)}) VALUES ({placeholders})"
            cur.execute(sql, values)
            inserted += 1

        try:
            _notify_labelsapp_update(cur, "labels:editada", sucursal_slug, id_factura, inserted=inserted)
        except Exception:
            pass

        _store_labelsapp_history(db, payload, sucursal_slug, None, estado_envio="Actualizada")
        db.commit()
        return {"id_factura": id_factura, "inserted": inserted, "tabla": table_name}
    except HTTPException:
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error replacing factura items: {e}")
        raise HTTPException(status_code=500, detail="Error editando factura")


@app.get("/api/v1/labelsapp/history")
async def labelsapp_history(
    limit: int = 100,
    date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    factura: Optional[str] = None,
    cliente: Optional[str] = None,
    operador: Optional[str] = None,
    sucursal: Optional[str] = None,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    role: Optional[str] = None,
    db=Depends(get_db)
):
    """Obtener el historial de envíos de LabelsApp por día."""
    try:
        _ensure_labelsapp_history_table(db)
        safe_limit = max(1, min(int(limit or 100), 500))
        cur = db.cursor()
        filters = []
        params = []
        requester_role = _resolve_requester_role(db, username=username, usuario_id=usuario_id, role=role)
        can_view_all = _can_view_all_labelsapp_history(requester_role)

        if date:
            filters.append("fecha_envio::date = %s::date")
            params.append(date)
        if date_from:
            filters.append("fecha_envio::date >= %s::date")
            params.append(date_from)
        if date_to:
            filters.append("fecha_envio::date <= %s::date")
            params.append(date_to)

        search_terms = [term for term in [q, factura, cliente, operador] if (term or "").strip()]
        if search_terms:
            search_term = (search_terms[0] or "").strip()
            filters.append(
                "(" \
                "LOWER(COALESCE(id_factura, '')) LIKE LOWER(%s) OR " \
                "LOWER(COALESCE(id_cliente, '')) LIKE LOWER(%s) OR " \
                "LOWER(COALESCE(operador, '')) LIKE LOWER(%s)" \
                ")"
            )
            params.extend([f"%{search_term}%", f"%{search_term}%", f"%{search_term}%"])

        if not can_view_all:
            requester_sucursal = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
            filters.append("TRIM(COALESCE(sucursal, '')) = TRIM(COALESCE(%s, ''))")
            params.append(requester_sucursal)

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(safe_limit)

        cur.execute(
            f"""
            SELECT
                id,
                id_factura,
                id_cliente,
                sucursal,
                username,
                usuario_id,
                operador,
                prioridad,
                total_items,
                total_unidades,
                productos_json,
                estado_envio,
                origen,
                fecha_envio
            FROM labelsapp_historial
            {where_clause}
            ORDER BY fecha_envio DESC, id DESC
            LIMIT %s
            """,
            params,
        )

        items = []
        for row in cur.fetchall() or []:
            productos_raw = row[10] or []
            if isinstance(productos_raw, str):
                try:
                    productos_raw = json.loads(productos_raw)
                except Exception:
                    productos_raw = []
            items.append({
                "id": row[0],
                "id_factura": row[1],
                "id_cliente": row[2],
                "sucursal": row[3],
                "username": row[4],
                "usuario_id": row[5],
                "operador": row[6],
                "prioridad": row[7],
                "total_items": int(row[8] or 0),
                "total_unidades": int(row[9] or 0),
                "productos": productos_raw,
                "estado_envio": row[11],
                "origen": row[12],
                "fecha_envio": row[13].isoformat() if row[13] else None,
            })

        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.error(f"Error getting labelsapp history: {e}")
        raise HTTPException(status_code=500, detail="Error obteniendo historial de LabelsApp")


@app.patch("/api/v1/labelsapp/history/{history_id}/transfer")
async def labelsapp_mark_history_transfer(
    history_id: int,
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
    role: Optional[str] = None,
    db=Depends(get_db),
):
    """Marcar un registro de historial como transferido desde Clientes/Kiosko."""
    try:
        _ensure_labelsapp_history_table(db)
        cur = db.cursor()

        requester_role = _resolve_requester_role(db, username=username, usuario_id=usuario_id, role=role)
        can_view_all = _can_view_all_labelsapp_history(requester_role)

        params = [int(history_id)]
        where_extra = ""
        if not can_view_all:
            requester_sucursal = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
            where_extra = " AND TRIM(COALESCE(sucursal, '')) = TRIM(COALESCE(%s, ''))"
            params.append(requester_sucursal)

        cur.execute(
            f"""
            UPDATE labelsapp_historial
            SET estado_envio = 'Transferido'
            WHERE id = %s
              AND (
                          UPPER(COALESCE(id_factura, '')) LIKE 'CLI-%%'
                      OR UPPER(COALESCE(id_factura, '')) LIKE 'KIOSK-%%'
                 OR LOWER(COALESCE(origen, '')) = 'kiosk'
                      OR LOWER(COALESCE(operador, '')) LIKE '%%kiosk%%'
              )
              {where_extra}
            RETURNING id, id_factura, sucursal
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Registro de kiosko no encontrado")

        # Defensive parsing: some deployments may return fewer columns than expected.
        row_id = row[0] if len(row) > 0 else int(history_id)
        row_factura = row[1] if len(row) > 1 else None
        row_sucursal = row[2] if len(row) > 2 else None

        db.commit()
        return {
            "ok": True,
            "id": row_id,
            "id_factura": row_factura,
            "sucursal": row_sucursal,
            "estado_envio": "Transferido",
        }
    except HTTPException:
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception(f"Error marking kiosk transfer history row {history_id}: {e}")
        raise HTTPException(status_code=500, detail="Error marcando pedido como transferido")


async def _labelsapp_live_events_stream(username: Optional[str], usuario_id: Optional[int], sucursal: Optional[str]):
    db = DatabasePool.get_connection()
    try:
        db.autocommit = True
        cur = db.cursor()
        cur.execute("LISTEN pedidos_actualizados")

        requested_sucursal = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
        yield "retry: 3000\n\n"

        while True:
            ready = await asyncio.to_thread(select.select, [db], [], [], 20)
            if ready and ready[0]:
                db.poll()
                while db.notifies:
                    notification = db.notifies.pop(0)
                    raw_payload = notification.payload or ""
                    parsed_payload = None
                    try:
                        parsed_payload = json.loads(raw_payload)
                    except Exception:
                        parsed_payload = {"raw": raw_payload}

                    payload_sucursal = (parsed_payload.get("sucursal") or "").strip() if isinstance(parsed_payload, dict) else ""
                    if payload_sucursal and payload_sucursal != requested_sucursal:
                        continue

                    yield f"event: pedidos_actualizados\ndata: {json.dumps(parsed_payload, ensure_ascii=False)}\n\n"
            else:
                yield "event: ping\ndata: {}\n\n"
    except asyncio.CancelledError:
        return
    finally:
        DatabasePool.return_connection(db, close=True)


@app.get("/api/v1/labelsapp/live-events")
async def labelsapp_live_events(
    username: Optional[str] = None,
    usuario_id: Optional[int] = None,
    sucursal: Optional[str] = None,
):
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _labelsapp_live_events_stream(username=username, usuario_id=usuario_id, sucursal=sucursal),
        media_type="text/event-stream",
        headers=headers,
    )


# ============================================================
# LOGIN ENDPOINT
# ============================================================

def get_departamento(rol):
    """Mapear rol a departamento"""
    if rol and rol.lower() == 'administrador':
        return "Departamento TI"
    elif rol and rol.lower() == 'activos_fijos':
        return "Activos Fijos"
    elif rol and rol.lower() == 'tecnicos':
        return "Mantenimiento"
    elif rol and rol.lower() in ['gerente', 'contabilidad']:
        return "Finanzas"
    elif rol and rol.lower() in ['facturador', 'cajero', 'cliente', 'colorista', 'analista']:
        return "Tienda"
    return "Otros"

@app.post("/api/v1/login")
async def login(username: str, password: str, db=Depends(get_db)):
    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id, username, nombre_completo, email, password_hash, rol, sucursal_id, telefono, activo
            FROM usuarios
            WHERE LOWER(TRIM(username)) = LOWER(TRIM(%s))
            ORDER BY id DESC
            LIMIT 1
            """,
            (username,)
        )
        usuario = cur.fetchone()
        
        if not usuario:
            raise HTTPException(status_code=401, detail="Usuario o contraseña inválida")
        
        usuario_id, db_username, nombre_completo, email, password_hash, rol, sucursal_id, telefono, activo = usuario
        rol_normalizado = (rol or "").strip().lower()
        
        # Verificar contraseña con SHA256
        password_check = hashlib.sha256(password.encode()).hexdigest()
        if password_check != password_hash:
            raise HTTPException(status_code=401, detail="Usuario o contraseña inválida")
        
        # Verificar que esté activo
        if not activo:
            raise HTTPException(status_code=403, detail="Esta cuenta está inactiva")
        
        # Verificar que tenga un rol permitido en el portal
        allowed_roles = ['administrador', 'analista', 'facturador', 'cajero', 'cliente', 'activos_fijos', 'tecnicos']
        if not rol_normalizado or rol_normalizado not in allowed_roles:
            raise HTTPException(status_code=403, detail="Acceso restringido para este rol")
        
        # Registrar login en login_audits
        try:
            # Obtener nombre de sucursal
            cur2 = db.cursor()
            cur2.execute("SELECT nombre FROM sucursales WHERE id = %s", (sucursal_id,))
            sucursal_row = cur2.fetchone()
            sucursal_nombre = sucursal_row[0] if sucursal_row else ""
            
            # Registrar en login_audits
            cur.execute(
                "INSERT INTO login_audits (usuario_id, username, nombre_completo, rol, sucursal, fecha_hora_login, created_at) VALUES (%s, %s, %s, %s, %s, NOW(), NOW())",
                (usuario_id, db_username, nombre_completo, rol, sucursal_nombre)
            )
            db.commit()
        except Exception as log_error:
            logger.warning(f"Error registering login audit: {log_error}")
        
        # Registrar sesión activa
        ACTIVE_SESSIONS[usuario_id] = {
            "username": db_username,
            "nombre_completo": nombre_completo,
            "email": email,
            "rol": rol_normalizado,
            "sucursal_id": sucursal_id,
            "sucursal_nombre": sucursal_nombre,
            "sucursal": sucursal_nombre,
            "sucursal_slug": _normalize_sucursal_slug(sucursal_nombre),
            "departamento": get_departamento(rol_normalizado),
            "ultima_actividad": time.time()
        }
        
        return {
            "id": usuario_id,
            "username": db_username,
            "nombre_completo": nombre_completo,
            "email": email,
            "rol": rol_normalizado,
            "sucursal_id": sucursal_id,
            "sucursal": sucursal_nombre,
            "telefono": telefono,
            "activo": activo
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in login: {e}")
        raise HTTPException(status_code=500, detail="Error de autenticacion")

# ============================================================
# CHANGE PASSWORD ENDPOINT
# ============================================================

class ChangePasswordRequest(BaseModel):
    user_id: int
    username: str
    current_password: str
    new_password: str

@app.post("/api/v1/change-password")
async def change_password(request: ChangePasswordRequest, db=Depends(get_db)):
    try:
        cur = db.cursor()
        
        # Verificar que el usuario existe y obtener la contraseña actual
        cur.execute(
            "SELECT id, password_hash, activo FROM usuarios WHERE id = %s AND username = %s",
            (request.user_id, request.username)
        )
        usuario = cur.fetchone()
        
        if not usuario:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        
        usuario_id, current_password_hash, activo = usuario
        
        if not activo:
            raise HTTPException(status_code=403, detail="Esta cuenta está inactiva")
        
        # Verificar contraseña actual con SHA256
        current_password_check = hashlib.sha256(request.current_password.encode()).hexdigest()
        if current_password_check != current_password_hash:
            raise HTTPException(status_code=401, detail="Contraseña actual incorrecta")
        
        # Validar nueva contraseña
        if len(request.new_password) < 6:
            raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 6 caracteres")
        
        # Hash de la nueva contraseña
        new_password_hash = hashlib.sha256(request.new_password.encode()).hexdigest()
        
        # Actualizar contraseña en la base de datos
        cur.execute(
            "UPDATE usuarios SET password_hash = %s, updated_at = NOW() WHERE id = %s",
            (new_password_hash, usuario_id)
        )
        db.commit()
        
        logger.info(f"Password changed successfully for user: {request.username}")
        
        return {"message": "Contraseña cambiada exitosamente"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error changing password: {e}")
        raise HTTPException(status_code=500, detail="Error al cambiar contraseña")

# ============================================================
# ROLES ENDPOINTS
# ============================================================

@app.get("/api/v1/roles")
async def list_roles(db=Depends(get_db)):
    """Listar roles disponibles"""
    try:
        cur = db.cursor()
        cur.execute("SELECT id, nombre, descripcion FROM roles ORDER BY nombre")
        roles = cur.fetchall()
        return {
            "total": len(roles),
            "roles": [{"id": r[0], "nombre": r[1], "descripcion": r[2]} for r in roles]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        logger.error(f"Error listing roles: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# EMPLEADOS ENDPOINTS
# ============================================================

@app.get("/api/v1/empleados")
async def list_empleados(skip: int = 0, limit: int = 200, mostrar_inactivos: bool = False, db=Depends(get_db)):
    """Listar empleados desde tablas coloristas y encargados"""
    try:
        cur = db.cursor()
        
        # Debug: Log del parámetro recibido
        logger.info(f"[GET EMPLEADOS DEBUG] mostrar_inactivos={mostrar_inactivos}, type={type(mostrar_inactivos)}")
        
        # Mapeo de sucursales
        mapeo_sucursal = {
            'Arroyohondo': 'Arroyo Hondo',
            'Bellavista': 'Bella Vista',
            'Puertoplata': 'Puerto Plata',
            'Puntacana': 'Punta Cana',
            'Rafaelvidal': 'Rafael Vidal',
            'Sanfrancisco': 'San Francisco',
            'Sanmartin': 'San Martin',
            'Santiago1': 'Santiago Bartolome Colon',
            'Test': 'test',
            'Villamella': 'Villa Mella',
            'Zonaoriental': 'Zona Oriental',
            'Bavaro': 'Bavaro'
        }
        
        # Consultar coloristas
        activo_condition = not mostrar_inactivos  # Si mostrar_inactivos=True, queremos activo=False
        logger.info(f"[GET EMPLEADOS DEBUG] activo_condition={activo_condition}")
        cur.execute("""
            SELECT 
                c.id, 
                c.nombre, 
                c.sucursal, 
                c.activo,
                c.rol,
                u.email,
                u.telefono,
                c.codigo_empleado
            FROM coloristas c
            LEFT JOIN usuarios u ON u.id = c.id
            WHERE c.activo = %s
            ORDER BY c.nombre
        """, (activo_condition,))
        coloristas = cur.fetchall()
        
        # Consultar encargados
        cur.execute("""
            SELECT 
                e.id, 
                e.nombre, 
                e.sucursal, 
                e.activo,
                e.rol,
                NULL as email,
                NULL as telefono,
                NULL as codigo_empleado
            FROM encargados e
            WHERE e.activo = %s
            ORDER BY e.nombre
        """, (activo_condition,))
        encargados = cur.fetchall()
        
        # Combinar resultados
        todos = list(coloristas) + list(encargados)
        
        # Ordenar por nombre
        todos.sort(key=lambda x: x[1] if x[1] else "")
        
        # Aplicar LIMIT y OFFSET después de combinar
        todos_paginados = todos[skip:skip + limit]

        # OPTIMIZACIÓN: Cargar todas las sucursales UNA SOLA VEZ
        cur.execute("SELECT id, nombre FROM sucursales")
        all_sucursales = cur.fetchall()
        sucursal_dict = {s[1]: s[0] for s in all_sucursales}
        
        result = []
        for e in todos_paginados:
            sucursal_nombre = e[2] or ""
            # Mapear nombre de sucursal
            sucursal_mapped = mapeo_sucursal.get(sucursal_nombre, sucursal_nombre)
            
            # OPTIMIZACIÓN: Lookup en diccionario
            sucursal_id = sucursal_dict.get(sucursal_mapped, None)
            
            result.append({
                "id": e[0],
                "nombre_completo": e[1] or "",
                "email": e[5] or "",
                "posicion": e[4] or "colorista",
                "sucursal_id": sucursal_id,
                "sucursal_nombre": sucursal_mapped,
                "telefono": e[6] or "",
                "codigo_empleado": e[7] or "",
                "activo": e[3] if e[3] is not None else True
            })
        
        logger.info(f"[GET EMPLEADOS] Retrieved {len(result)} empleados from coloristas and encargados tables")
        
        return {
            "total": len(result),
            "empleados": result
        }
    except Exception as e:
        logger.error(f"Error listing empleados: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/sucursales")
async def list_sucursales(skip: int = 0, limit: int = 200, activo: Optional[bool] = None, db=Depends(get_db)):
    """Listar sucursales"""
    try:
        cur = db.cursor()
        if activo is not None:
            cur.execute("SELECT id, nombre, direccion, telefono, codigo, extension, activo, zona FROM sucursales WHERE activo = %s LIMIT %s OFFSET %s", (activo, limit, skip))
        else:
            cur.execute("SELECT id, nombre, direccion, telefono, codigo, extension, activo, zona FROM sucursales LIMIT %s OFFSET %s", (limit, skip))
        
        sucursales = cur.fetchall()
        return {
            "total": len(sucursales),
            "sucursales": [
                {
                    "id": s[0],
                    "nombre": s[1],
                    "direccion": s[2] or "",
                    "telefono": s[3] or "",
                    "codigo": s[4] or "",
                    "extension": s[5] or "",
                    "estado": "activa" if s[6] else "inactiva",
                    "zona": s[7] or "Santo_Domingo"
                }
                for s in sucursales
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        logger.error(f"Error listing sucursales: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/sucursales")
async def create_sucursal(nombre: str, direccion: str = "", telefono: str = "", zona: str = "Santo_Domingo", db=Depends(get_db)):
    """Crear sucursal"""
    try:
        cur = db.cursor()
        cur.execute("INSERT INTO sucursales (nombre, direccion, telefono, zona, activo, fecha_creacion) VALUES (%s, %s, %s, %s, true, NOW()) RETURNING id", 
                   (nombre, direccion if direccion else None, telefono if telefono else None, zona))
        sucursal_id = cur.fetchone()[0]
        db.commit()
        return {"id": sucursal_id, "nombre": nombre, "direccion": direccion, "telefono": telefono, "zona": zona, "estado": "activa"}
    except Exception as e:
        logger.error(f"Error creating sucursal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/sucursales/{sucursal_id}")
async def update_sucursal(sucursal_id: int, nombre: str, direccion: str = "", telefono: str = "", zona: str = "", db=Depends(get_db)):
    """Actualizar sucursal"""
    try:
        cur = db.cursor()
        if zona:
            cur.execute("UPDATE sucursales SET nombre = %s, direccion = %s, telefono = %s, zona = %s WHERE id = %s", 
                       (nombre, direccion if direccion else None, telefono if telefono else None, zona, sucursal_id))
        else:
            cur.execute("UPDATE sucursales SET nombre = %s, direccion = %s, telefono = %s WHERE id = %s", 
                       (nombre, direccion if direccion else None, telefono if telefono else None, sucursal_id))
        db.commit()
        return {"id": sucursal_id, "nombre": nombre, "direccion": direccion, "telefono": telefono, "zona": zona, "message": "Sucursal actualizada"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        logger.error(f"Error updating sucursal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/v1/sucursales/{sucursal_id}/estado")
async def update_sucursal_estado(sucursal_id: int, activo: bool, db=Depends(get_db)):
    """Cambiar estado de sucursal"""
    try:
        cur = db.cursor()
        cur.execute("UPDATE sucursales SET activo = %s WHERE id = %s", (activo, sucursal_id))
        db.commit()
        return {"id": sucursal_id, "activo": activo, "message": "Estado actualizado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        logger.error(f"Error updating sucursal estado: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# USUARIOS ENDPOINTS (Organized by Sucursal)
# ============================================================

@app.get("/api/v1/usuarios")
async def list_usuarios(sucursal_id: int = None, skip: int = 0, limit: int = 200, db=Depends(get_db)):
    """Listar usuarios, opcionalmente filtrados por sucursal"""
    try:
        cur = db.cursor()
        if sucursal_id:
            cur.execute("SELECT id, username, nombre_completo, email, rol, sucursal_id, telefono, activo FROM usuarios WHERE sucursal_id = %s LIMIT %s OFFSET %s", (sucursal_id, limit, skip))
        else:
            cur.execute("SELECT id, username, nombre_completo, email, rol, sucursal_id, telefono, activo FROM usuarios LIMIT %s OFFSET %s", (limit, skip))
        
        usuarios = cur.fetchall()
        return {
            "total": len(usuarios),
            "usuarios": [
                {
                    "id": u[0],
                    "username": u[1],
                    "nombre_completo": u[2],
                    "email": u[3],
                    "rol": u[4],
                    "sucursal_id": u[5],
                    "telefono": u[6],
                    "activo": u[7]
                }
                for u in usuarios
            ]
        }
    except Exception as e:
        logger.error(f"Error listing usuarios: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/usuarios")
async def create_usuario(nombre_completo: str, email: str, password: str = None, username: str = None, rol: str = "Empleado", sucursal_id: int = None, telefono: str = "", db=Depends(get_db)):
    """Crear usuario con contraseña hasheada (genera temporal si no se proporciona)"""
    try:
        # Normalizar rol
        if rol and rol.lower() == 'contabilidad':
            rol = 'Contabilidad'
        
        # Generar contraseña temporal si no se proporciona
        if not password:
            password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        
        # Construir username robusto: evita n/a y colisiones por duplicado.
        username_was_provided = bool((username or "").strip())
        raw_username = (username or "").strip()
        if not raw_username:
            email_value = (email or "").strip()
            if email_value and "@" in email_value and email_value.lower() not in {"n/a", "na", "none", "null"}:
                raw_username = email_value.split("@")[0]
            elif nombre_completo:
                raw_username = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", nombre_completo).encode("ascii", "ignore").decode("ascii").lower()).strip("_")
            else:
                raw_username = f"user_{sucursal_id or 'x'}"

        username = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw_username).strip("._-")
        if not username or username.lower() in {"n/a", "na", "none", "null", "n_a"}:
            username = f"user_{int(time.time())}"
        
        cur = db.cursor()
        _ensure_usuarios_cliente_role_constraint(db)

        if username_was_provided:
            cur.execute("SELECT 1 FROM usuarios WHERE LOWER(TRIM(username)) = LOWER(TRIM(%s)) LIMIT 1", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="El usuario (login) ya existe")
        else:
            candidate = username
            suffix = 1
            while True:
                cur.execute("SELECT 1 FROM usuarios WHERE LOWER(TRIM(username)) = LOWER(TRIM(%s)) LIMIT 1", (candidate,))
                if not cur.fetchone():
                    username = candidate
                    break
                suffix += 1
                candidate = f"{username}_{suffix}"
        
        # Hash usando SHA256 (consistente con BD)
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        cur.execute(
            "INSERT INTO usuarios (username, password_hash, nombre_completo, email, telefono, rol, sucursal_id, activo, fecha_creacion) VALUES (%s, %s, %s, %s, %s, %s, %s, true, NOW()) RETURNING id",
            (username, password_hash, nombre_completo, email if email else None, telefono if telefono else None, rol or "Empleado", sucursal_id)
        )
        usuario_id = cur.fetchone()[0]
        db.commit()
        return {"id": usuario_id, "nombre_completo": nombre_completo, "email": email, "rol": rol or "Empleado", "sucursal_id": sucursal_id, "username": username, "temporal_password": password, "estado": "activo"}
    except HTTPException:
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error creating usuario: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@app.put("/api/v1/usuarios/{usuario_id}")
async def update_usuario(usuario_id: int, nombre_completo: str = "", email: str = "", rol: str = "", telefono: str = "", sucursal_id: int = None, password: str = None, db=Depends(get_db)):
    """Actualizar usuario con soporte para cambio de contrasena"""
    try:
        cur = db.cursor()
        _ensure_usuarios_cliente_role_constraint(db)
        updates = []
        values = []
        
        if nombre_completo:
            updates.append("nombre_completo = %s")
            values.append(nombre_completo)
        if email:
            updates.append("email = %s")
            values.append(email)
        if rol:
            # Normalizar rol: "contabilidad" -> "Contabilidad"
            if rol.lower() == 'contabilidad':
                rol = 'Contabilidad'
            updates.append("rol = %s")
            values.append(rol)
        if telefono:
            updates.append("telefono = %s")
            values.append(telefono)
        if sucursal_id:
            updates.append("sucursal_id = %s")
            values.append(sucursal_id)
        if password:
            # Usar SHA256 (consistente con login y creación)
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            updates.append("password_hash = %s")
            values.append(password_hash)
        
        if updates:
            updates.append("fecha_modificacion = NOW()")
            values.append(usuario_id)
            query = f"UPDATE usuarios SET {', '.join(updates)} WHERE id = %s"
            cur.execute(query, values)
            db.commit()
        
        return {"id": usuario_id, "message": "Usuario actualizado"}
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error updating usuario: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/usuarios/{usuario_id}")
async def delete_usuario(usuario_id: int, db=Depends(get_db)):
    """Eliminar usuario"""
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM usuarios WHERE id = %s", (usuario_id,))
        db.commit()
        return {"id": usuario_id, "message": "Usuario eliminado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        logger.error(f"Error deleting usuario: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/v1/usuarios/{usuario_id}/estado")
async def toggle_usuario_estado(usuario_id: int, activo: bool, db=Depends(get_db)):
    """Cambiar estado de usuario"""
    try:
        cur = db.cursor()
        cur.execute("UPDATE usuarios SET activo = %s, fecha_modificacion = NOW() WHERE id = %s", (activo, usuario_id))
        db.commit()
        return {"id": usuario_id, "activo": activo, "message": "Estado actualizado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/v1/empleados/{empleado_id}")
async def toggle_empleado_estado(empleado_id: int, request: Request, db=Depends(get_db)):
    """Cambiar estado de empleado (activo/inactivo)"""
    try:
        # Obtener datos del body JSON
        body = await request.json()
        activo = body.get("activo")
        
        if activo is None:
            raise HTTPException(status_code=400, detail="Se requiere el campo 'activo' en el body")
        
        cur = db.cursor()
        
        # Actualizar en coloristas
        cur.execute("UPDATE coloristas SET activo = %s WHERE id = %s", (activo, empleado_id))
        
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Empleado no encontrado")
            
        db.commit()
        return {"id": empleado_id, "activo": activo, "message": "Estado del empleado actualizado"}
        
    except Exception as e:
        logger.error(f"Error updating empleado estado: {e}")
        raise HTTPException(status_code=500, detail=str(e))



# ============================================================
# EMPLEADOS ENDPOINTS (Using usuarios table)
# ============================================================

@app.get("/api/v1/debug/empleados")
async def debug_empleados(db=Depends(get_db)):
    """DEBUG: Ver todos los empleados en BD sin filtro"""
    try:
        cur = db.cursor()
        cur.execute("""
            SELECT id, nombre_completo, rol, sucursal_id, email
            FROM usuarios
            WHERE rol IN ('colorista', 'facturador', 'cliente', 'encargado', 'operador', 'auxiliar_almacen', 'administrador', 'analista')
            ORDER BY id
            LIMIT 50
        """)
        empleados = cur.fetchall()
        return {
            "total": len(empleados),
            "empleados": [
                {
                    "id": e[0],
                    "nombre_completo": e[1],
                    "rol": e[2],
                    "sucursal_id": e[3],
                    "email": e[4]
                }
                for e in empleados
            ]
        }
    except Exception as e:
        logger.error(f"Debug error: {e}")
        return {"error": str(e)}

@app.post("/api/v1/empleados")
async def create_empleado(data: EmpleadoUpdate, db=Depends(get_db)):
    """Crear empleado en coloristas"""
    try:
        cur = db.cursor()
        
        mapeo_inverso = {
            "Arroyo Hondo": "Arroyohondo",
            "Bella Vista": "Bellavista",
            "Puerto Plata": "Puertoplata",
            "Punta Cana": "Puntacana",
            "Rafael Vidal": "Rafaelvidal",
            "San Francisco": "Sanfrancisco",
            "San Martin": "Sanmartin",
            "Santiago Bartolome Colon": "Santiago1",
            "test": "Test",
            "Villa Mella": "Villamella",
            "Zona Oriental": "Zonaoriental",
            "Bavaro": "Bavaro"
        }
        
        sucursal_nombre = ""
        if data.sucursal_id:
            cur.execute("SELECT nombre FROM sucursales WHERE id = %s", (data.sucursal_id,))
            sucursal_row = cur.fetchone()
            if sucursal_row:
                sucursal_nombre = sucursal_row[0]
        
        sucursal_code = mapeo_inverso.get(sucursal_nombre, sucursal_nombre)
        
        codigo_empleado = data.codigo_empleado or data.nombre_completo.replace(" ", "_").lower()
        cur.execute(
            "INSERT INTO coloristas (nombre, sucursal, rol, activo, creado_en, codigo_empleado) VALUES (%s, %s, %s, true, NOW(), %s) RETURNING id",
            (data.nombre_completo, sucursal_code, data.rol or "colorista", codigo_empleado)
        )
        empleado_id = cur.fetchone()[0]
        db.commit()
        
        logger.info(f"[CREATE EMPLEADO] ID={empleado_id}, Nombre={data.nombre_completo}")
        
        return {
            "id": empleado_id,
            "nombre_completo": data.nombre_completo,
            "email": data.email or "",
            "posicion": data.rol or "colorista",
            "sucursal_id": data.sucursal_id,
            "sucursal_nombre": sucursal_nombre,
            "telefono": data.telefono or "",
            "activo": True,
            "message": "Empleado creado exitosamente"
        }
    except Exception as e:
        logger.error(f"Error creating empleado: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/empleados/{empleado_id}")
async def update_empleado(empleado_id: int, data: EmpleadoUpdate, db=Depends(get_db)):
    """Actualizar empleado en coloristas"""
    try:
        cur = db.cursor()
        
        # Actualizar coloristas (nombre, sucursal, rol)
        coloristas_updates = []
        coloristas_params = []
        
        if data.nombre_completo:
            coloristas_updates.append("nombre = %s")
            coloristas_params.append(data.nombre_completo)
        
        if data.sucursal_id:
            # Obtener nombre de sucursal
            cur.execute("SELECT nombre FROM sucursales WHERE id = %s", (data.sucursal_id,))
            sucursal_row = cur.fetchone()
            if sucursal_row:
                sucursal_nombre = sucursal_row[0]
                # Aplicar mapeo inverso
                mapeo_inverso = {
                    "Arroyo Hondo": "Arroyohondo",
                    "Bella Vista": "Bellavista",
                    "Puerto Plata": "Puertoplata",
                    "Punta Cana": "Puntacana",
                    "Rafael Vidal": "Rafaelvidal",
                    "San Francisco": "Sanfrancisco",
                    "San Martin": "Sanmartin",
                    "Santiago Bartolome Colon": "Santiago1",
                    "test": "Test",
                    "Villa Mella": "Villamella",
                    "Zona Oriental": "Zonaoriental",
                    "Bavaro": "Bavaro"
                }
                sucursal_code = mapeo_inverso.get(sucursal_nombre, sucursal_nombre)
                coloristas_updates.append("sucursal = %s")
                coloristas_params.append(sucursal_code)
        
        # Agregar rol a coloristas
        if data.rol:
            coloristas_updates.append("rol = %s")
            coloristas_params.append(data.rol)
        
        # Agregar codigo_empleado a coloristas
        if data.codigo_empleado:
            coloristas_updates.append("codigo_empleado = %s")
            coloristas_params.append(data.codigo_empleado)
        
        # Ejecutar actualización en coloristas
        if coloristas_updates:
            coloristas_params.append(empleado_id)
            coloristas_query = "UPDATE coloristas SET " + ", ".join(coloristas_updates) + " WHERE id = %s"
            logger.info(f"[UPDATE COLORISTAS] ID={empleado_id}, Query={coloristas_query}")
            cur.execute(coloristas_query, coloristas_params)
            db.commit()
            logger.info(f"[UPDATE COLORISTAS] OK - {cur.rowcount} filas actualizadas")
        
        # Actualizar usuarios si existe (email, telefono, sucursal_id) - sincronización opcional
        usuarios_updates = []
        usuarios_params = []
        
        if data.email:
            usuarios_updates.append("email = %s")
            usuarios_params.append(data.email)
        if data.telefono is not None:
            usuarios_updates.append("telefono = %s")
            usuarios_params.append(data.telefono)
        if data.sucursal_id:
            usuarios_updates.append("sucursal_id = %s")
            usuarios_params.append(data.sucursal_id)
        
        if usuarios_updates:
            usuarios_params.append(empleado_id)
            usuarios_query = "UPDATE usuarios SET " + ", ".join(usuarios_updates) + " WHERE id = %s"
            logger.info(f"[SYNC USUARIOS] ID={empleado_id}")
            cur.execute(usuarios_query, usuarios_params)
            db.commit()
        
        return {"id": empleado_id, "message": "Empleado actualizado"}
        
        return {"id": empleado_id, "message": "Empleado actualizado"}
    except Exception as e:
        logger.error(f"Error updating empleado: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/empleados/{empleado_id}")
async def delete_empleado(empleado_id: int, db=Depends(get_db)):
    """Eliminar empleado de coloristas o encargados"""
    try:
        cur = db.cursor()
        # Intenta eliminar de coloristas primero
        cur.execute("DELETE FROM coloristas WHERE id = %s", (empleado_id,))
        deleted_from_coloristas = cur.rowcount > 0
        
        # Si no estaba en coloristas, intenta de encargados
        if not deleted_from_coloristas:
            cur.execute("DELETE FROM encargados WHERE id = %s", (empleado_id,))
        
        db.commit()
        logger.info(f"[DELETE EMPLEADO] ID={empleado_id}")
        return {"id": empleado_id, "message": "Empleado eliminado"}
    except Exception as e:
        logger.error(f"Error deleting empleado: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# USUARIOS ONLINE (ACTIVIDAD)
# ============================================================


@app.post("/api/v1/desktop/register")
async def register_desktop_app(nombre_programa: str, version: str = "", maquina: str = "", usuario_so: str = ""):
    """Registrar un programa de escritorio como activo"""
    try:
        # Crear ID único para el programa
        app_id = f"{nombre_programa}_{maquina}_{usuario_so}"
        
        ACTIVE_SESSIONS[app_id] = {
            "tipo": "desktop",
            "nombre_programa": nombre_programa,
            "version": version,
            "maquina": maquina,
            "usuario_so": usuario_so,
            "departamento": "Programas Desktop",
            "ultima_actividad": time.time()
        }
        
        return {"id": app_id, "message": f"Programa {nombre_programa} registrado"}
    except Exception as e:
        logger.error(f"Error registering desktop app: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/desktop/heartbeat/{app_id}")
async def desktop_heartbeat(app_id: str):
    """Actualizar actividad de programa desktop (heartbeat)"""
    try:
        if app_id in ACTIVE_SESSIONS and ACTIVE_SESSIONS[app_id].get("tipo") == "desktop":
            ACTIVE_SESSIONS[app_id]["ultima_actividad"] = time.time()
            return {"message": "Heartbeat registrado"}
        else:
            raise HTTPException(status_code=404, detail="Programa no encontrado")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in desktop heartbeat: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/desktop/unregister/{app_id}")
async def unregister_desktop_app(app_id: str):
    """Desregistrar un programa de escritorio"""
    try:
        if app_id in ACTIVE_SESSIONS:
            del ACTIVE_SESSIONS[app_id]
            return {"message": "Programa desconectado"}
        else:
            raise HTTPException(status_code=404, detail="Programa no encontrado")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unregistering desktop app: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/online")
async def get_online_users():
    """Listar usuarios y programas desktop online"""
    try:
        # Limpiar sesiones antiguas (más de 30 minutos sin actividad)
        current_time = time.time()
        timeout = 30 * 60  # 30 minutos
        
        usuarios_timeout = [uid for uid, data in ACTIVE_SESSIONS.items() if current_time - data["ultima_actividad"] > timeout]
        for uid in usuarios_timeout:
            del ACTIVE_SESSIONS[uid]
        
        # Separar usuarios de programas desktop
        usuarios = {}
        programas = []
        
        por_departamento = {
            "Tienda": [],
            "Departamento TI": [],
            "Finanzas": [],
            "Otros": []
        }
        
        for session_id, datos in ACTIVE_SESSIONS.items():
            if datos.get("tipo") == "desktop":
                # Es un programa de escritorio
                programas.append({
                    "id": session_id,
                    "nombre_programa": datos.get("nombre_programa", "N/A"),
                    "version": datos.get("version", ""),
                    "maquina": datos.get("maquina", ""),
                    "usuario_so": datos.get("usuario_so", "")
                })
            else:
                # Es un usuario web
                depto = datos.get("departamento", "Otros")
                por_departamento[depto].append({
                    "id": session_id,
                    "username": datos.get("username", "N/A"),
                    "nombre_completo": datos.get("nombre_completo", "N/A"),
                    "rol": datos.get("rol", "N/A"),
                    "email": datos.get("email", "")
                })
        
        # Eliminar departamentos vacíos
        usuarios_result = {k: v for k, v in por_departamento.items() if v}
        
        return {
            "total": len(ACTIVE_SESSIONS),
            "usuarios_web": usuarios_result,
            "programas_desktop": programas,
            "total_usuarios": sum(len(v) for v in usuarios_result.values()),
            "total_programas": len(programas)
        }
    except Exception as e:
        logger.error(f"Error getting online users: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/logout/{usuario_id}")
async def logout_user(usuario_id: int):
    """Desloguear a un usuario"""
    try:
        if usuario_id in ACTIVE_SESSIONS:
            del ACTIVE_SESSIONS[usuario_id]
            return {"message": "Usuario deslogeado correctamente"}
        else:
            raise HTTPException(status_code=404, detail="Usuario no encontrado en sesiones activas")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging out user: {e}")
        raise HTTPException(status_code=500, detail=str(e))




@app.get("/api/v1/login-activity")
async def get_login_activity(limit: int = 50, db=Depends(get_db)):
    """Obtener historial de logins recientes"""
    try:
        cur = db.cursor()
        cur.execute("""
            SELECT usuario_id, username, nombre_completo, rol, sucursal, fecha_hora_login 
            FROM login_audits 
            ORDER BY fecha_hora_login DESC 
            LIMIT %s
        """, (limit,))
        
        audits = cur.fetchall()
        
        result = [
            {
                "usuario_id": a[0],
                "username": a[1],
                "nombre_completo": a[2],
                "rol": a[3],
                "sucursal": a[4],
                "fecha_hora_login": a[5].isoformat() if a[5] else None
            }
            for a in audits
        ]
        
        return {
            "total": len(result),
            "logins": result
        }
    except Exception as e:
        logger.error(f"Error getting login activity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/labelsapp/usage-metrics")
async def labelsapp_usage_metrics(
    product_limit: int = 5,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db=Depends(get_db)
):
    """Resumen de uso de LabelsApp para dashboard administrativo."""
    try:
        safe_limit = max(3, min(int(product_limit or 5), 20))
        cache_key = (safe_limit, (date_from or ""), (date_to or ""))
        cached = _usage_metrics_cache_get(cache_key)
        if cached is not None:
            return cached

        from_dt = None
        to_dt_exclusive = None
        if date_from:
            try:
                from_dt = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=400, detail="date_from debe tener formato YYYY-MM-DD")
        if date_to:
            try:
                to_dt_exclusive = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            except ValueError:
                raise HTTPException(status_code=400, detail="date_to debe tener formato YYYY-MM-DD")
        if from_dt and to_dt_exclusive and from_dt >= to_dt_exclusive:
            raise HTTPException(status_code=400, detail="Rango inválido: date_from debe ser menor o igual que date_to")

        with _get_usage_metrics_lock(cache_key):
            cached = _usage_metrics_cache_get(cache_key)
            if cached is not None:
                return cached

            _set_local_pg_timeouts(db, statement_ms=9000, lock_ms=700)
            cur = db.cursor()

            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema='public'
                  AND table_name LIKE 'pedidos_pendientes_%'
                ORDER BY table_name
                """
            )
            tables = [r[0] for r in cur.fetchall() if r and r[0]]

            columns_by_table = defaultdict(set)
            if tables:
                cur.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema='public'
                      AND table_name = ANY(%s)
                    """,
                    (tables,)
                )
                for table, column in cur.fetchall() or []:
                    if table and column:
                        columns_by_table[str(table)].add(str(column))

            product_totals = defaultdict(int)
            product_totals_by_sucursal = defaultdict(lambda: defaultdict(int))
            sucursal_totals = {}
            completion_by_sucursal = defaultdict(lambda: {"total_min": 0.0, "count": 0})
            total_facturas = 0
            total_items = 0

            for table_name in tables:
                if not re.match(r"^pedidos_pendientes_[a-z0-9_]+$", table_name):
                    continue

                sucursal_slug = table_name.replace("pedidos_pendientes_", "").strip() or "principal"

                table_columns = columns_by_table.get(table_name, set())
                factura_col = "id_factura" if "id_factura" in table_columns else None
                cantidad_col = "cantidad" if "cantidad" in table_columns else None
                producto_col = "producto" if "producto" in table_columns else None
                date_col = "fecha_creacion" if "fecha_creacion" in table_columns else None
                if not factura_col and "factura" in table_columns:
                    factura_col = "factura"
                if not producto_col and "nombre_producto" in table_columns:
                    producto_col = "nombre_producto"

                if (from_dt or to_dt_exclusive) and not date_col:
                    # Si no hay columna de fecha, no se puede filtrar por rango en esta tabla.
                    continue

                date_clauses = []
                date_params = []
                if from_dt:
                    date_clauses.append(f"{date_col} >= %s")
                    date_params.append(from_dt)
                if to_dt_exclusive:
                    date_clauses.append(f"{date_col} < %s")
                    date_params.append(to_dt_exclusive)
                where_date_sql = f"WHERE {' AND '.join(date_clauses)}" if date_clauses else ""

                facturas_expr = (
                    f"COUNT(DISTINCT NULLIF(TRIM(COALESCE({factura_col}::text, '')), ''))"
                    if factura_col else
                    "COUNT(*)"
                )
                items_expr = (
                    f"COALESCE(SUM(COALESCE({cantidad_col}, 1)), 0)"
                    if cantidad_col else
                    "COUNT(*)"
                )

                cur.execute(
                    f"""
                    SELECT {facturas_expr} AS facturas,
                           {items_expr} AS items
                    FROM {table_name}
                    {where_date_sql}
                    """,
                    tuple(date_params)
                )
                facts_row = cur.fetchone() or (0, 0)
                facturas_count = int(facts_row[0] or 0)
                items_count = int(facts_row[1] or 0)

                sucursal_totals[sucursal_slug] = {
                    "sucursal_slug": sucursal_slug,
                    "facturas": facturas_count,
                    "items": items_count,
                }
                total_facturas += facturas_count
                total_items += items_count

                if producto_col:
                    product_qty_expr = (
                        f"COALESCE(SUM(COALESCE({cantidad_col}, 1)), 0)"
                        if cantidad_col else
                        "COUNT(*)"
                    )
                    cur.execute(
                        f"""
                        SELECT TRIM(COALESCE({producto_col}::text, '')) AS producto,
                               {product_qty_expr} AS qty
                        FROM {table_name}
                        WHERE TRIM(COALESCE({producto_col}::text, '')) <> ''
                        {'AND ' + ' AND '.join(date_clauses) if date_clauses else ''}
                        GROUP BY TRIM(COALESCE({producto_col}::text, ''))
                        """,
                        tuple(date_params)
                    )
                    for producto, qty in cur.fetchall() or []:
                        product_name = (producto or "").strip()
                        if not product_name:
                            continue
                        qty_i = int(qty or 0)
                        product_totals[product_name] += qty_i
                        product_totals_by_sucursal[sucursal_slug][product_name] += qty_i

                if "fecha_creacion" in table_columns and "fecha_completado" in table_columns:
                    completion_clauses = [
                        "fecha_creacion IS NOT NULL",
                        "fecha_completado IS NOT NULL",
                        "TRIM(COALESCE(estado, '')) IN ('Finalizado', 'Completado')",
                    ]
                    completion_params = []
                    if from_dt:
                        completion_clauses.append("fecha_completado >= %s")
                        completion_params.append(from_dt)
                    if to_dt_exclusive:
                        completion_clauses.append("fecha_completado < %s")
                        completion_params.append(to_dt_exclusive)
                    cur.execute(
                        f"""
                        SELECT
                            COALESCE(SUM(EXTRACT(EPOCH FROM (fecha_completado - fecha_creacion)) / 60.0), 0) AS total_min,
                            COUNT(*) AS cnt
                        FROM {table_name}
                        WHERE {' AND '.join(completion_clauses)}
                        """,
                        tuple(completion_params)
                    )
                    comp_row = cur.fetchone() or (0, 0)
                    completion_by_sucursal[sucursal_slug]["total_min"] += float(comp_row[0] or 0)
                    completion_by_sucursal[sucursal_slug]["count"] += int(comp_row[1] or 0)

        # Mapa de sucursal_slug -> metadatos reales
        slug_to_meta = {}
        all_sucursales_meta = []
        try:
            cur.execute("SELECT nombre, zona FROM sucursales")
            for row in cur.fetchall() or []:
                suc_name = (row[0] or "").strip()
                if not suc_name:
                    continue
                zona = _normalize_zona_key(row[1] or "")
                slug = _normalize_sucursal_slug(suc_name)
                item = {
                    "nombre": suc_name,
                    "zona": zona,
                    "slug": slug,
                }
                slug_to_meta[slug] = item
                all_sucursales_meta.append(item)
        except Exception:
            slug_to_meta = {}
            all_sucursales_meta = []

        sucursal_usage = []
        for slug, data in sucursal_totals.items():
            suc_meta = slug_to_meta.get(slug, {})
            zona = suc_meta.get("zona") or "Sin_Zona"
            comp = completion_by_sucursal.get(slug, {"total_min": 0.0, "count": 0})
            avg_completion = (comp["total_min"] / comp["count"]) if comp["count"] else 0.0
            top_product = None
            top_by_suc = product_totals_by_sucursal.get(slug, {})
            if top_by_suc:
                p_name, p_qty = max(top_by_suc.items(), key=lambda x: (x[1], x[0].lower()))
                top_product = {"producto": p_name, "cantidad": int(p_qty)}
            sucursal_usage.append({
                "sucursal": suc_meta.get("nombre") or slug.replace("_", " ").title(),
                "sucursal_slug": slug,
                "zona": zona,
                "facturas": data["facturas"],
                "items": data["items"],
                "avg_completion_minutes": round(avg_completion, 2),
                "ordenes_finalizadas": int(comp["count"]),
                "top_producto": top_product,
            })
        sucursal_usage.sort(key=lambda x: (-x["facturas"], -x["items"], x["sucursal"]))

        zona_totals = defaultdict(lambda: {"facturas": 0, "items": 0, "sucursales": 0, "total_min": 0.0, "finalizadas": 0})
        zone_products = defaultdict(lambda: defaultdict(int))
        for row in sucursal_usage:
            zona = _normalize_zona_key(row.get("zona") or "")
            zona_totals[zona]["facturas"] += int(row.get("facturas") or 0)
            zona_totals[zona]["items"] += int(row.get("items") or 0)
            zona_totals[zona]["sucursales"] += 1
            zona_totals[zona]["total_min"] += float(row.get("avg_completion_minutes") or 0) * int(row.get("ordenes_finalizadas") or 0)
            zona_totals[zona]["finalizadas"] += int(row.get("ordenes_finalizadas") or 0)

            suc_slug = row.get("sucursal_slug")
            for prod_name, qty in (product_totals_by_sucursal.get(suc_slug, {}) or {}).items():
                zone_products[zona][prod_name] += int(qty or 0)

        zona_usage = [
            {
                "zona": z,
                "facturas": int(v["facturas"]),
                "items": int(v["items"]),
                "sucursales": int(v["sucursales"]),
                "avg_completion_minutes": round((float(v["total_min"]) / int(v["finalizadas"])) if int(v["finalizadas"]) else 0.0, 2),
                "ordenes_finalizadas": int(v["finalizadas"]),
            }
            for z, v in zona_totals.items()
        ]
        zona_usage.sort(key=lambda x: (-x["facturas"], -x["items"], x["zona"]))

        zone_top_products = []
        for zona, products_map in zone_products.items():
            ranked = sorted(
                [{"producto": p, "cantidad": int(q)} for p, q in products_map.items() if int(q or 0) > 0],
                key=lambda x: (-x["cantidad"], x["producto"].lower())
            )
            zone_top_products.append({
                "zona": zona,
                "top_products": ranked[:safe_limit]
            })
        zone_top_products.sort(key=lambda x: x["zona"])

        total_completion_min = sum(float(v.get("total_min") or 0) for v in zona_totals.values())
        total_completed_orders = sum(int(v.get("finalizadas") or 0) for v in zona_totals.values())
        avg_completion_minutes_overall = round((total_completion_min / total_completed_orders) if total_completed_orders else 0.0, 2)

        product_usage = [
            {"producto": k, "cantidad": int(v)}
            for k, v in product_totals.items()
            if int(v or 0) > 0
        ]
        product_usage.sort(key=lambda x: (-x["cantidad"], x["producto"].lower()))
        top_products = product_usage[:safe_limit]
        low_products = sorted(product_usage, key=lambda x: (x["cantidad"], x["producto"].lower()))[:safe_limit]

        last_login = None
        latest_login_by_sucursal = {}
        try:
            cur.execute(
                """
                SELECT username, nombre_completo, rol, sucursal, fecha_hora_login
                FROM login_audits
                ORDER BY fecha_hora_login DESC
                LIMIT 1
                """
            )
            last_login_row = cur.fetchone()
            if last_login_row:
                last_login = {
                    "username": last_login_row[0],
                    "nombre_completo": last_login_row[1],
                    "rol": last_login_row[2],
                    "sucursal": last_login_row[3],
                    "fecha_hora_login": last_login_row[4].isoformat() if last_login_row[4] else None,
                }

            cur.execute(
                """
                SELECT DISTINCT ON (LOWER(TRIM(COALESCE(sucursal, ''))))
                       username, sucursal, fecha_hora_login
                FROM login_audits
                WHERE TRIM(COALESCE(sucursal, '')) <> ''
                ORDER BY LOWER(TRIM(COALESCE(sucursal, ''))), fecha_hora_login DESC
                """
            )
            for u, suc, dt in cur.fetchall() or []:
                key = _normalize_sucursal_slug(suc or "")
                latest_login_by_sucursal[key] = {
                    "username": u,
                    "fecha_hora_login": dt.isoformat() if dt else None,
                }
        except Exception:
            last_login = None
            latest_login_by_sucursal = {}

        sucursal_map_usage = {row.get("sucursal_slug"): row for row in sucursal_usage}
        sucursal_login_by_zone = defaultdict(list)
        all_zone_order = set()
        for meta in all_sucursales_meta:
            slug = meta.get("slug")
            zona = meta.get("zona") or "Sin_Zona"
            all_zone_order.add(zona)
            usage = sucursal_map_usage.get(slug, {})
            login = latest_login_by_sucursal.get(slug, {})
            sucursal_login_by_zone[zona].append({
                "sucursal": meta.get("nombre"),
                "sucursal_slug": slug,
                "zona": zona,
                "ultimo_login": login.get("fecha_hora_login"),
                "ultimo_login_username": login.get("username"),
                "facturas": int(usage.get("facturas") or 0),
                "items": int(usage.get("items") or 0),
                "avg_completion_minutes": round(float(usage.get("avg_completion_minutes") or 0.0), 2),
                "ordenes_finalizadas": int(usage.get("ordenes_finalizadas") or 0),
                "top_producto": usage.get("top_producto"),
            })

        sucursal_login_by_zone_list = []
        for z in sorted(all_zone_order):
            rows = sorted(sucursal_login_by_zone.get(z, []), key=lambda x: (x.get("sucursal") or "").lower())
            sucursal_login_by_zone_list.append({"zona": z, "sucursales": rows})

            response_data = {
                "total_facturas": total_facturas,
                "total_items": total_items,
                "avg_completion_minutes_overall": avg_completion_minutes_overall,
                "total_completed_orders": int(total_completed_orders),
                "last_login": last_login,
                "top_products": top_products,
                "low_products": low_products,
                "sucursal_usage": sucursal_usage,
                "zona_usage": zona_usage,
                "zone_top_products": zone_top_products,
                "sucursal_login_by_zone": sucursal_login_by_zone_list,
                "sucursal_mas_uso": sucursal_usage[0] if sucursal_usage else None,
                "sucursal_menos_uso": sucursal_usage[-1] if sucursal_usage else None,
                "zona_mas_uso": zona_usage[0] if zona_usage else None,
                "zona_menos_uso": zona_usage[-1] if zona_usage else None,
                "filters": {
                    "date_from": date_from or "",
                    "date_to": date_to or "",
                },
            }
            _usage_metrics_cache_set(cache_key, response_data)
            return response_data
    except Exception as e:
        logger.error(f"Error getting labelsapp usage metrics: {e}")
        raise HTTPException(status_code=500, detail="Error obteniendo métricas de LabelsApp")


# ============================================================
# FORMULAS ENDPOINTS
# ============================================================

@app.get("/formulas")
async def formulas_page(request: Request):
    """Servir interfaz de gestión de fórmulas solo para analistas"""
    # Verificar que sea analista a través del header o parámetro
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden acceder a fórmulas")
    
    html_path = os.path.join(os.path.dirname(__file__), "formulas.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Formulas page not found")

@app.get("/api/v1/colorantes")
async def list_colorantes(skip: int = 0, limit: int = 200, db=Depends(get_db)):
    """Listar colorantes disponibles"""
    try:
        cur = db.cursor()
        cur.execute("SELECT id, nombre FROM colorante ORDER BY nombre LIMIT %s OFFSET %s", (limit, skip))
        colorantes = cur.fetchall()
        return {
            "total": len(colorantes),
            "colorantes": [{"id": c[0], "nombre": c[1]} for c in colorantes]
        }
    except Exception as e:
        logger.error(f"Error listing colorantes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _ensure_formula_backup(db) -> None:
    """DESACTIVADO: el trigger fn_formulas_backup crecia formulas_backup sin
    limite y al final del dia el bloat saturaba el pool de Postgres -> 499s
    en cascada en /labelsapp/send, /codigo-base, /products, etc.

    Esta funcion ahora solo DROPEA los triggers existentes. La tabla
    formulas_backup y la funcion fn_formulas_backup se conservan por si hace
    falta revisar historico — pero ya no se escribe nada nuevo en ellas.

    Para reactivar el backup en el futuro, hay que reescribir esto con purga
    periodica (ej. DELETE WHERE changed_at < now() - interval '30 days') y
    preferiblemente publicar a una cola async en vez de un trigger sincrono.
    """
    cur = db.cursor()
    try:
        cur.execute("SET LOCAL lock_timeout = '2s'")
    except Exception:
        pass

    dropped = []
    for table_name in FORMULA_SOURCE_TABLES:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table_name,)
        )
        if not cur.fetchone():
            continue
        trigger_name = f"trg_{table_name}_formula_backup"
        cur.execute(
            """
            SELECT 1
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = %s
              AND t.tgname = %s
              AND NOT t.tgisinternal
            """,
            (table_name, trigger_name),
        )
        if not cur.fetchone():
            continue
        try:
            cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
            dropped.append(trigger_name)
        except Exception as e:
            logger.warning(f"[startup-setup] No se pudo drop trigger {trigger_name}: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            cur = db.cursor()
            try:
                cur.execute("SET LOCAL lock_timeout = '2s'")
            except Exception:
                pass

    if dropped:
        logger.info(f"[startup-setup] Triggers fn_formulas_backup desactivados: {dropped}")


def _ensure_productsw_search_index(db) -> None:
    """Crear indice trigram para acelerar /labelsapp/products (ILIKE %q%)."""
    cur = db.cursor()
    try:
        cur.execute("SET LOCAL lock_timeout = '2s'")
    except Exception:
        pass
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as e:
        logger.warning(f"[startup-setup] pg_trgm no disponible: {e}")
        return
    # IF NOT EXISTS evita el DROP innecesario; CONCURRENTLY no se puede dentro de
    # transaccion, asi que usamos el modo normal pero con lock_timeout corto.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_productsw_codigo_trgm ON ProductSW USING gin (codigo gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_productsw_nombre_trgm ON ProductSW USING gin (nombre gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_productsw_activo_nombre ON ProductSW (activo, nombre)",
    ):
        try:
            cur.execute(stmt)
        except Exception as e:
            logger.warning(f"[startup-setup] No se pudo crear indice: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            cur = db.cursor()
            try:
                cur.execute("SET LOCAL lock_timeout = '2s'")
            except Exception:
                pass



@app.get("/api/v1/formulas-normales")
async def list_formulas_normales(request: Request, codigo: str = None, tipo: str = "galon", skip: int = 0, limit: int = 100, db=Depends(get_db)):
    """Listar fórmulas normales (tabla presentacion) - Solo analistas"""
    # Verificar que sea analista
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden acceder a fórmulas")
    
    try:
        cur = db.cursor()
        
        # Query base con JOIN a colorante (presentacion no tiene columna id)
        base_query = """
            SELECT p.id_pintura, p.id_colorante, c.nombre as colorante_nombre, 
                   p.oz, p._32s, p._64s, p._128s, p.tipo
            FROM presentacion p
            LEFT JOIN colorante c ON p.id_colorante = c.id
            WHERE p.tipo = %s
        """
        params = [tipo]
        
        # Agregar filtro de código si se proporciona
        if codigo:
            codigo_busqueda = codigo.replace('-', ' ').replace('_', ' ')
            base_query += " AND UPPER(p.id_pintura) LIKE UPPER(%s)"
            params.append(f"%{codigo_busqueda}%")
        
        base_query += " ORDER BY p.id_pintura, p.id_colorante LIMIT %s OFFSET %s"
        params.extend([limit, skip])
        
        cur.execute(base_query, params)
        formulas = cur.fetchall()
        
        result = [
            {
                "id": f"{f[0]}|{f[1]}",  # clave compuesta como string
                "codigo_color": f[0],
                "id_colorante": f[1],
                "colorante_nombre": f[2] or f[1],
                "oz": f[3],
                "_32s": f[4], 
                "_64s": f[5],
                "_128s": f[6],
                "tipo": f[7]
            }
            for f in formulas
        ]
        
        return {
            "total": len(result),
            "formulas": result
        }
    except Exception as e:
        logger.error(f"Error listing formulas normales: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/formulas-cce")
async def list_formulas_cce(request: Request, codigo: str = None, tipo: str = "galon", skip: int = 0, limit: int = 100, db=Depends(get_db)):
    """Listar fórmulas CCE según tipo (galon, cubeta, cuarto) - Solo analistas"""
    # Verificar que sea analista
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden acceder a fórmulas")
    
    try:
        cur = db.cursor()
        
        # Mapear tipo a tabla
        table_map = {
            "galon": "formulas_cce_g",
            "cubeta": "formulas_cce_c", 
            "cuarto": "formulas_cce_qt"
        }
        
        if tipo not in table_map:
            raise HTTPException(status_code=400, detail="Tipo debe ser: galon, cubeta, cuarto")
        
        table = table_map[tipo]
        
        # Query con JOIN a colorante
        base_query = f"""
            SELECT f.id, f.id_pintura, f.id_colorante, c.nombre as colorante_nombre,
                   f.oz, f._32s, f._64s, f._128s
            FROM {table} f
            LEFT JOIN colorante c ON f.id_colorante = c.id
        """
        params = []
        
        # Agregar filtro de código si se proporciona
        if codigo:
            codigo_busqueda = codigo.replace('-', ' ').replace('_', ' ')
            base_query += " WHERE UPPER(f.id_pintura) LIKE UPPER(%s)"
            params.append(f"%{codigo_busqueda}%")
        
        base_query += " ORDER BY f.id_pintura, f.id_colorante LIMIT %s OFFSET %s"
        params.extend([limit, skip])
        
        cur.execute(base_query, params)
        formulas = cur.fetchall()
        
        result = [
            {
                "id": f[0],
                "codigo_color": f[1],
                "id_colorante": f[2], 
                "colorante_nombre": f[3] or f[2],
                "oz": f[4],
                "_32s": f[5],
                "_64s": f[6], 
                "_128s": f[7],
                "tipo": tipo
            }
            for f in formulas
        ]
        
        return {
            "total": len(result),
            "formulas": result
        }
    except Exception as e:
        logger.error(f"Error listing formulas CCE: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/formulas-normales")
async def create_formula_normal(request: Request, formula: FormulaNormalCreate, db=Depends(get_db)):
    """Crear fórmula normal en tabla presentacion - Solo analistas"""
    # Verificar que sea analista
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden gestionar fórmulas")
    
    print(f"DEBUG - Received formula data: {formula}")
    print(f"DEBUG - Raw data: codigo_color={formula.codigo_color}, id_colorante={formula.id_colorante}, oz={formula.oz}, x32s={formula.x32s}, x64s={formula.x64s}, x128s={formula.x128s}, tipo={formula.tipo}")
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO presentacion (id_pintura, id_colorante, oz, _32s, _64s, _128s, tipo) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (formula.codigo_color, formula.id_colorante, formula.oz or None, formula.x32s or None, formula.x64s or None, formula.x128s or None, formula.tipo)
        )
        db.commit()
        
        return {
            "id": f"{formula.codigo_color}|{formula.id_colorante}",  # clave compuesta como string
            "codigo_color": formula.codigo_color,
            "id_colorante": formula.id_colorante,
            "oz": formula.oz,
            "_32s": formula.x32s,
            "_64s": formula.x64s, 
            "_128s": formula.x128s,
            "tipo": formula.tipo,
            "message": "Fórmula creada exitosamente"
        }
    except Exception as e:
        logger.error(f"Error creating formula normal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/formulas-cce")
async def create_formula_cce(request: Request, codigo_color: str, id_colorante: str, oz: float = 0, _32s: float = 0, _64s: float = 0, _128s: float = 0, tipo: str = "galon", db=Depends(get_db)):
    """Crear fórmula CCE en tabla correspondiente - Solo analistas"""
    # Verificar que sea analista
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden gestionar fórmulas")
    
    try:
        cur = db.cursor()
        
        # Mapear tipo a tabla
        table_map = {
            "galon": "formulas_cce_g",
            "cubeta": "formulas_cce_c",
            "cuarto": "formulas_cce_qt"
        }
        
        if tipo not in table_map:
            raise HTTPException(status_code=400, detail="Tipo debe ser: galon, cubeta, cuarto")
        
        table = table_map[tipo]
        
        query = f"INSERT INTO {table} (id_pintura, id_colorante, oz, _32s, _64s, _128s) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id"
        cur.execute(query, (codigo_color, id_colorante, oz or None, _32s or None, _64s or None, _128s or None))
        formula_id = cur.fetchone()[0]
        db.commit()
        
        return {
            "id": formula_id,
            "codigo_color": codigo_color,
            "id_colorante": id_colorante,
            "oz": oz,
            "_32s": _32s,
            "_64s": _64s,
            "_128s": _128s, 
            "tipo": tipo,
            "message": "Fórmula CCE creada exitosamente"
        }
    except Exception as e:
        logger.error(f"Error creating formula CCE: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/formulas-normales/{id_pintura}/{id_colorante}")
async def update_formula_normal(request: Request, id_pintura: str, id_colorante: str, codigo_color: str = None, nuevo_colorante: str = None, oz: float = None, _32s: float = None, _64s: float = None, _128s: float = None, db=Depends(get_db)):
    """Actualizar fórmula normal usando clave compuesta - Solo analistas"""
    # Verificar que sea analista
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden gestionar fórmulas")
    
    try:
        cur = db.cursor()
        updates = []
        values = []
        
        if codigo_color is not None:
            updates.append("id_pintura = %s")
            values.append(codigo_color)
        if nuevo_colorante is not None:
            updates.append("id_colorante = %s") 
            values.append(nuevo_colorante)
        if oz is not None:
            updates.append("oz = %s")
            values.append(oz)
        if _32s is not None:
            updates.append("_32s = %s")
            values.append(_32s)
        if _64s is not None:
            updates.append("_64s = %s")
            values.append(_64s)
        if _128s is not None:
            updates.append("_128s = %s")
            values.append(_128s)
        
        if updates:
            values.extend([id_pintura, id_colorante])
            query = f"UPDATE presentacion SET {', '.join(updates)} WHERE id_pintura = %s AND id_colorante = %s"
            cur.execute(query, values)
            db.commit()
        
        return {"id": f"{id_pintura}|{id_colorante}", "message": "Fórmula actualizada"}
    except Exception as e:
        logger.error(f"Error updating formula normal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/formulas-normales/{id_pintura}/{id_colorante}")
async def delete_formula_normal(request: Request, id_pintura: str, id_colorante: str, db=Depends(get_db)):
    """Eliminar fórmula normal usando clave compuesta - Solo analistas"""
    # Verificar que sea analista
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden gestionar fórmulas")
    
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM presentacion WHERE id_pintura = %s AND id_colorante = %s", (id_pintura, id_colorante))
        db.commit()
        return {"id": f"{id_pintura}|{id_colorante}", "message": "Fórmula eliminada"}
    except Exception as e:
        logger.error(f"Error deleting formula normal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/formulas-cce/{formula_id}")
async def delete_formula_cce(request: Request, formula_id: int, tipo: str = "galon", db=Depends(get_db)):
    """Eliminar fórmula CCE - Solo analistas"""
    # Verificar que sea analista
    user_role = request.headers.get("X-User-Role") or request.query_params.get("role")
    if not user_role or user_role.lower() != 'analista':
        raise HTTPException(status_code=403, detail="Acceso restringido: Solo analistas pueden gestionar fórmulas")
    
    try:
        cur = db.cursor()
        
        # Mapear tipo a tabla
        table_map = {
            "galon": "formulas_cce_g",
            "cubeta": "formulas_cce_c",
            "cuarto": "formulas_cce_qt"
        }
        
        if tipo not in table_map:
            raise HTTPException(status_code=400, detail="Tipo debe ser: galon, cubeta, cuarto")
        
        table = table_map[tipo]
        query = f"DELETE FROM {table} WHERE id = %s"
        cur.execute(query, (formula_id,))
        db.commit()
        
        return {"id": formula_id, "message": "Fórmula CCE eliminada"}
    except Exception as e:
        logger.error(f"Error deleting formula CCE: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8002"))
    uvicorn.run(app, host="0.0.0.0", port=port)
