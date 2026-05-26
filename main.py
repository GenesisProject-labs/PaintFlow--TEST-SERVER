# -*- coding: utf-8 -*-
from fastapi import FastAPI, HTTPException, Depends, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import logging
from datetime import datetime
import csv
import time
import os
import re
import unicodedata
from collections import defaultdict
from typing import Optional, List
from pydantic import BaseModel

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
    prioridad: str = "Media"
    username: Optional[str] = None
    usuario_id: Optional[int] = None
    sucursal: Optional[str] = None
    operador: Optional[str] = None
    items: List[LabelsAppItem]


class LabelsAppCodigoBaseRequest(BaseModel):
    base: str
    producto: str
    terminacion: str


class LabelsAppFacturaPriorityRequest(BaseModel):
    prioridad: str


class LabelsAppFacturaItemsUpdateRequest(BaseModel):
    prioridad: Optional[str] = None
    items: List[LabelsAppItem]

from config import settings
from database import DatabasePool, get_db
import bcrypt
import hashlib
import uvicorn

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL))
logger = logging.getLogger(__name__)

# Sesiones activas: {usuario_id: {"username": "", "nombre": "", "rol": "", "departamento": "", "ultima_actividad": timestamp}}
ACTIVE_SESSIONS = {}

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

@app.on_event("startup")
async def startup_event():
    logger.info("Starting PaintFlow API...")
    DatabasePool.init_pool()

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
PERSONALIZADO_CODIGO_MAX = 7
PERSONALIZADO_NOMBRE_MAX = 20


class LabelsAppPersonalizedProductRequest(BaseModel):
    codigo: str
    nombre: str


def _load_personalized_products() -> List[dict]:
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
                    "personalizado": True,
                })
    except Exception as e:
        logger.warning(f"Error loading personalized products: {e}")
    return items


def _save_personalized_products(items: List[dict]) -> None:
    fieldnames = ["codigo", "nombre", "base", "ubicacion", "fecha_creacion"]
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
            })


def _find_personalized_product_by_code(codigo: str) -> Optional[dict]:
    codigo_norm = (codigo or "").strip().lower()
    for item in _load_personalized_products():
        if (item.get("codigo") or "").strip().lower() == codigo_norm:
            return item
    return None


def _resolve_sucursal_slug(db, username: Optional[str] = None, usuario_id: Optional[int] = None, sucursal_text: Optional[str] = None) -> str:
    if sucursal_text:
        return _normalize_sucursal_slug(sucursal_text)

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


def _get_labelsapp_live_queue(db, table_name: str, limit: int = 50):
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT id_factura,
               COUNT(*) AS total,
               SUM(CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado') THEN 1 ELSE 0 END) AS cnt_final,
               SUM(CASE WHEN TRIM(COALESCE(estado,'')) = 'En Proceso' THEN 1 ELSE 0 END) AS cnt_proc,
               MAX(CASE TRIM(COALESCE(prioridad,'')) WHEN 'Alta' THEN 3 WHEN 'Media' THEN 2 WHEN 'Baja' THEN 1 ELSE 0 END) AS pr_rank,
               MAX(COALESCE(operador, '—')) AS operador
        FROM {table_name}
        WHERE TRIM(COALESCE(estado,'')) <> 'Cancelado'
        GROUP BY id_factura
        HAVING SUM(CASE WHEN TRIM(COALESCE(estado,'')) IN ('Finalizado','Completado') THEN 1 ELSE 0 END) < COUNT(*)
        ORDER BY pr_rank DESC, id_factura DESC
        LIMIT %s
        """,
        (limit,)
    )
    rows = cur.fetchall()
    items = []
    for factura, total, cnt_final, cnt_proc, pr_rank, operador in rows:
        prioridad_txt = 'Alta' if pr_rank == 3 else ('Media' if pr_rank == 2 else ('Baja' if pr_rank == 1 else '—'))
        if cnt_final == total and total > 0:
            estado_txt = 'Finalizado'
        elif cnt_proc > 0:
            estado_txt = 'En Proceso'
        else:
            estado_txt = 'Pendiente'
        items.append({
            "factura": factura or '—',
            "items": int(total or 0),
            "operador": operador or '—',
            "en_proceso": int(cnt_proc or 0),
            "finalizados": int(cnt_final or 0),
            "prioridad": prioridad_txt,
            "estado": estado_txt,
        })
    return items


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
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_factura ON {table_name}(id_factura)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_estado ON {table_name}(estado)")


def _generate_order_id(id_factura: str, codigo: str, idx: int) -> str:
    seed = f"{id_factura}|{codigo}|{idx}|{time.time_ns()}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:9].upper()
    return f"O{digest}"


def _calculate_codigo_base(db, base: str, producto: str, terminacion: str) -> str:
    if not base or not producto or not terminacion:
        return ""

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
            if term_cmp == "brillo":
                if base_color == "extra white":
                    return "B54W00151-"
                if base_color == "ultra deep":
                    return "B54T00154-"
            return "No Aplica"

        if es_industrialenamels:
            if term_cmp == "brillo":
                if base_color == "extra white":
                    return "B54W101-"
                if base_color == "ultra deep":
                    return "B54T101-"
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
async def labelsapp_products(q: str = "", limit: int = 250, db=Depends(get_db)):
    """Buscar productos para LabelsApp Web"""
    try:
        safe_limit = None if limit <= 0 else max(1, min(limit, 50000))
        query = (q or "").strip()
        cur = db.cursor()
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
                sql += "\n                LIMIT %s"
                params = params + (safe_limit,)
            cur.execute(
                sql,
                params
            )
        else:
            sql = """
                SELECT codigo, nombre, COALESCE(base, ''), COALESCE(ubicacion, '')
                FROM ProductSW
                WHERE (activo = TRUE OR activo IS NULL)
                ORDER BY nombre
            """
            params = ()
            if safe_limit is not None:
                sql += "\n                LIMIT %s"
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

        personalized = _load_personalized_products()
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
            products = [p for p in products if qn in str(p.get("codigo") or "").lower() or qn in str(p.get("nombre") or "").lower()]

        products.sort(key=lambda p: (str(p.get("nombre") or "").lower(), str(p.get("codigo") or "").lower()))

        if safe_limit is not None:
            products = products[:safe_limit]
        return {"total": len(products), "productos": products}
    except Exception as e:
        logger.error(f"Error listing labelsapp products: {e}")
        raise HTTPException(status_code=500, detail="Error listando productos")


@app.get("/api/v1/labelsapp/product/{codigo}")
async def labelsapp_product_by_code(codigo: str, db=Depends(get_db)):
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
            personalized = _find_personalized_product_by_code(codigo)
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


@app.get("/api/v1/labelsapp/personalizados")
async def labelsapp_personalized_products():
    return {"items": _load_personalized_products()}


@app.post("/api/v1/labelsapp/personalizados")
async def labelsapp_create_personalized_product(payload: LabelsAppPersonalizedProductRequest):
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

    items = _load_personalized_products()
    if any((item.get("codigo") or "").strip().lower() == codigo.lower() for item in items):
        raise HTTPException(status_code=409, detail="El código ya existe en productos personalizados")

    items.append({
        "codigo": codigo,
        "nombre": nombre,
        "base": "",
        "ubicacion": "",
        "fecha_creacion": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_personalized_products(items)
    return {"message": "Producto personalizado guardado", "codigo": codigo, "nombre": nombre}


@app.delete("/api/v1/labelsapp/personalizados/{codigo}")
async def labelsapp_delete_personalized_product(codigo: str):
    items = _load_personalized_products()
    filtered = [item for item in items if (item.get("codigo") or "").strip().lower() != (codigo or "").strip().lower()]
    if len(filtered) == len(items):
        raise HTTPException(status_code=404, detail="Código no encontrado en productos personalizados")
    _save_personalized_products(filtered)
    return {"message": "Producto personalizado eliminado", "codigo": codigo}


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

        for idx, item in enumerate(payload.items):
            cantidad = max(1, int(item.cantidad or 1))
            item_priority = (item.prioridad or prioridad or "Media").strip().title()
            if item_priority not in ["Alta", "Media", "Baja"]:
                item_priority = prioridad
            codigo_base = (item.codigo_base or "").strip() or _calculate_codigo_base(db, item.base, item.producto, item.terminacion)

            data = {
                "id_orden_profesional": _generate_order_id(payload.id_factura, item.codigo, idx),
                "id_factura": payload.id_factura,
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
            cur.execute(f"NOTIFY pedidos_actualizados, 'labels:web:{payload.id_factura}'")
        except Exception:
            pass

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
    db=Depends(get_db)
):
    """Obtener la cola agrupada en tiempo real para la vista web."""
    try:
        limit = max(1, min(int(limit or 50), 250))
        sucursal_slug = _resolve_sucursal_slug(db, username=username, usuario_id=usuario_id, sucursal_text=sucursal)
        table_name = _safe_table_for_sucursal(sucursal_slug)

        _ensure_pedidos_table(db, table_name)
        items = _get_labelsapp_live_queue(db, table_name, limit=limit)

        return {
            "sucursal": sucursal_slug,
            "tabla": table_name,
            "total": len(items),
            "items": items,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching labelsapp live queue: {e}")
        raise HTTPException(status_code=500, detail="Error consultando la cola en tiempo real")


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
            cur.execute(f"NOTIFY pedidos_actualizados, 'labels:prioridad:{id_factura}:{prioridad}'")
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
            cur.execute(f"NOTIFY pedidos_actualizados, 'labels:cancelado:{id_factura}'")
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
            cur.execute(f"NOTIFY pedidos_actualizados, 'labels:editada:{id_factura}'")
        except Exception:
            pass
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


# ============================================================
# LOGIN ENDPOINT
# ============================================================

def get_departamento(rol):
    """Mapear rol a departamento"""
    if rol and rol.lower() == 'administrador':
        return "Departamento TI"
    elif rol and rol.lower() in ['gerente', 'contabilidad']:
        return "Finanzas"
    elif rol and rol.lower() in ['facturador', 'cajero', 'colorista', 'analista']:
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
        allowed_roles = ['administrador', 'analista', 'facturador', 'cajero']
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
        
        # Usar username si se proporciona, si no generarlo del email
        if not username:
            username = email.split('@')[0] if email else f"user_{sucursal_id}"
        
        cur = db.cursor()
        
        # Hash usando SHA256 (consistente con BD)
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        cur.execute(
            "INSERT INTO usuarios (username, password_hash, nombre_completo, email, telefono, rol, sucursal_id, activo, fecha_creacion) VALUES (%s, %s, %s, %s, %s, %s, %s, true, NOW()) RETURNING id",
            (username, password_hash, nombre_completo, email if email else None, telefono if telefono else None, rol or "Empleado", sucursal_id)
        )
        usuario_id = cur.fetchone()[0]
        db.commit()
        return {"id": usuario_id, "nombre_completo": nombre_completo, "email": email, "rol": rol or "Empleado", "sucursal_id": sucursal_id, "username": username, "temporal_password": password, "estado": "activo"}
    except Exception as e:
        logger.error(f"Error creating usuario: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@app.put("/api/v1/usuarios/{usuario_id}")
async def update_usuario(usuario_id: int, nombre_completo: str = "", email: str = "", rol: str = "", telefono: str = "", sucursal_id: int = None, password: str = None, db=Depends(get_db)):
    """Actualizar usuario con soporte para cambio de contrasena"""
    try:
        cur = db.cursor()
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
            WHERE rol IN ('colorista', 'facturador', 'encargado', 'operador', 'auxiliar_almacen', 'administrador', 'analista')
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
async def labelsapp_usage_metrics(product_limit: int = 5, db=Depends(get_db)):
    """Resumen de uso de LabelsApp para dashboard administrativo."""
    try:
        safe_limit = max(3, min(int(product_limit or 5), 20))
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

        product_totals = defaultdict(int)
        sucursal_totals = {}
        total_facturas = 0
        total_items = 0

        for table_name in tables:
            if not re.match(r"^pedidos_pendientes_[a-z0-9_]+$", table_name):
                continue

            sucursal_slug = table_name.replace("pedidos_pendientes_", "").strip() or "principal"

            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
                """,
                (table_name,)
            )
            table_columns = {r[0] for r in (cur.fetchall() or []) if r and r[0]}
            factura_col = "id_factura" if "id_factura" in table_columns else None
            cantidad_col = "cantidad" if "cantidad" in table_columns else None
            producto_col = "producto" if "producto" in table_columns else None
            if not factura_col and "factura" in table_columns:
                factura_col = "factura"
            if not producto_col and "nombre_producto" in table_columns:
                producto_col = "nombre_producto"

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
                """
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
                    GROUP BY TRIM(COALESCE({producto_col}::text, ''))
                    """
                )
                for producto, qty in cur.fetchall() or []:
                    product_name = (producto or "").strip()
                    if not product_name:
                        continue
                    product_totals[product_name] += int(qty or 0)

        # Mapa de sucursal_slug -> metadatos reales
        slug_to_meta = {}
        try:
            cur.execute("SELECT nombre, zona FROM sucursales")
            for row in cur.fetchall() or []:
                suc_name = (row[0] or "").strip()
                if not suc_name:
                    continue
                zona = _normalize_zona_key(row[1] or "")
                slug_to_meta[_normalize_sucursal_slug(suc_name)] = {
                    "nombre": suc_name,
                    "zona": zona,
                }
        except Exception:
            slug_to_meta = {}

        sucursal_usage = []
        for slug, data in sucursal_totals.items():
            suc_meta = slug_to_meta.get(slug, {})
            zona = suc_meta.get("zona") or "Sin_Zona"
            sucursal_usage.append({
                "sucursal": suc_meta.get("nombre") or slug.replace("_", " ").title(),
                "sucursal_slug": slug,
                "zona": zona,
                "facturas": data["facturas"],
                "items": data["items"],
            })
        sucursal_usage.sort(key=lambda x: (-x["facturas"], -x["items"], x["sucursal"]))

        zona_totals = defaultdict(lambda: {"facturas": 0, "items": 0, "sucursales": 0})
        for row in sucursal_usage:
            zona = _normalize_zona_key(row.get("zona") or "")
            zona_totals[zona]["facturas"] += int(row.get("facturas") or 0)
            zona_totals[zona]["items"] += int(row.get("items") or 0)
            zona_totals[zona]["sucursales"] += 1

        zona_usage = [
            {
                "zona": z,
                "facturas": int(v["facturas"]),
                "items": int(v["items"]),
                "sucursales": int(v["sucursales"]),
            }
            for z, v in zona_totals.items()
        ]
        zona_usage.sort(key=lambda x: (-x["facturas"], -x["items"], x["zona"]))

        product_usage = [
            {"producto": k, "cantidad": int(v)}
            for k, v in product_totals.items()
            if int(v or 0) > 0
        ]
        product_usage.sort(key=lambda x: (-x["cantidad"], x["producto"].lower()))
        top_products = product_usage[:safe_limit]
        low_products = sorted(product_usage, key=lambda x: (x["cantidad"], x["producto"].lower()))[:safe_limit]

        last_login = None
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
        except Exception:
            last_login = None

        return {
            "total_facturas": total_facturas,
            "total_items": total_items,
            "last_login": last_login,
            "top_products": top_products,
            "low_products": low_products,
            "sucursal_usage": sucursal_usage,
            "zona_usage": zona_usage,
            "sucursal_mas_uso": sucursal_usage[0] if sucursal_usage else None,
            "sucursal_menos_uso": sucursal_usage[-1] if sucursal_usage else None,
            "zona_mas_uso": zona_usage[0] if zona_usage else None,
            "zona_menos_uso": zona_usage[-1] if zona_usage else None,
        }
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
    uvicorn.run(app, host="127.0.0.1", port=8002)
