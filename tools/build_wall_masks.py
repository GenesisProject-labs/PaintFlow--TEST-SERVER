# -*- coding: utf-8 -*-
"""
build_wall_masks.py
-------------------
Genera máscaras de "pared" para la feature "Ver en pared" del kiosko cliente.

Flujo:
    1. Drop fotos (JPG/PNG) en static/escenas/raw/
    2. Ejecutar:  python tools/build_wall_masks.py
    3. Por cada foto se abre una ventana, el operador hace UN click sobre la
       pared principal. Se envía la imagen + el punto a Replicate (modelo
       Segment Anything 2) y se descarga la máscara segmentada.
    4. Se guarda:
         - static/escenas/<id>.jpg          (copia de la foto)
         - static/escenas/<id>_paredes.png  (máscara: blanco=pared, transparente=resto)
         - static/escenas/manifest.json     (índice que consume el frontend)

Requisitos:
    pip install replicate pillow

Variables de entorno:
    REPLICATE_API_TOKEN   (obligatoria — https://replicate.com/account/api-tokens)
    SAM_MODEL             (opcional, default: meta/sam-2)
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import re
import unicodedata
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "static" / "escenas" / "raw"
OUT_DIR = ROOT / "static" / "escenas"
MANIFEST_PATH = OUT_DIR / "manifest.json"

SAM_MODEL = os.environ.get("SAM_MODEL", "meta/sam-2")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return value or "escena"


def humanize(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", " ").split())


# ---------------------------------------------------------------------------
# Selección de punto (tkinter)
# ---------------------------------------------------------------------------

def pick_wall_point(image_path: Path) -> Optional[Tuple[int, int]]:
    """Abre la foto en una ventana y devuelve (x, y) del click en coords originales.

    Retorna None si el usuario cierra sin hacer click.
    """
    try:
        import tkinter as tk
        from PIL import Image, ImageTk
    except ImportError as e:
        raise RuntimeError(
            "Faltan dependencias. Instala con:\n    pip install pillow\n"
            f"Detalle: {e}"
        )

    img = Image.open(image_path).convert("RGB")
    iw, ih = img.size
    max_side = 900
    scale = min(1.0, max_side / max(iw, ih))
    if scale < 1.0:
        disp = img.resize((int(iw * scale), int(ih * scale)))
    else:
        disp = img

    root = tk.Tk()
    root.title(f"Click sobre la PARED — {image_path.name}")
    root.attributes("-topmost", True)

    photo = ImageTk.PhotoImage(disp)
    canvas = tk.Canvas(root, width=disp.width, height=disp.height, highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)

    instructions = tk.Label(
        root,
        text=("Click sobre la PARED principal (zona pintable).  "
              "Esc = saltar esta foto."),
        font=("Segoe UI", 11),
        pady=6,
    )
    instructions.pack(fill="x")

    coords = {"x": None, "y": None}

    def on_click(ev):
        coords["x"] = int(ev.x / scale)
        coords["y"] = int(ev.y / scale)
        # marcar visualmente
        canvas.create_oval(ev.x - 8, ev.y - 8, ev.x + 8, ev.y + 8,
                           outline="#ff3366", width=3)
        root.after(150, root.destroy)

    def on_escape(_ev):
        root.destroy()

    canvas.bind("<Button-1>", on_click)
    root.bind("<Escape>", on_escape)

    root.mainloop()

    if coords["x"] is None:
        return None
    return coords["x"], coords["y"]


# ---------------------------------------------------------------------------
# Llamada a Replicate
# ---------------------------------------------------------------------------

def segment_wall(image_path: Path, point: Tuple[int, int]) -> bytes:
    """Devuelve los bytes PNG de la máscara (modo L o RGBA) producida por SAM."""
    try:
        import replicate
    except ImportError:
        raise RuntimeError("Falta el paquete 'replicate'. Instala con:\n    pip install replicate")

    if not os.environ.get("REPLICATE_API_TOKEN"):
        raise RuntimeError(
            "Falta REPLICATE_API_TOKEN. Crea un token en\n"
            "    https://replicate.com/account/api-tokens\n"
            "y exporta:    setx REPLICATE_API_TOKEN <tu_token>   (luego reiniciar terminal)"
        )

    x, y = point

    # El input exacto depende del modelo. meta/sam-2 acepta point_coords/point_labels
    # como strings JSON. Si Replicate cambia el schema, ajustar este dict.
    with open(image_path, "rb") as f:
        output = replicate.run(
            SAM_MODEL,
            input={
                "image": f,
                "point_coords": json.dumps([[x, y]]),
                "point_labels": json.dumps([1]),
                "multimask_output": False,
            },
        )

    # El output puede venir como: URL string, lista de URLs, FileOutput, o dict.
    mask_url = None
    if isinstance(output, str):
        mask_url = output
    elif isinstance(output, list) and output:
        first = output[0]
        mask_url = first if isinstance(first, str) else getattr(first, "url", None)
    elif isinstance(output, dict):
        # algunos modelos devuelven {"individual_masks": [...], "combined_mask": "..."}
        mask_url = (output.get("combined_mask")
                    or (output.get("individual_masks") or [None])[0])
        if hasattr(mask_url, "url"):
            mask_url = mask_url.url
    else:
        mask_url = getattr(output, "url", None)

    if not mask_url:
        raise RuntimeError(f"No pude extraer la URL de la máscara del output de Replicate: {output!r}")

    import urllib.request
    with urllib.request.urlopen(mask_url) as resp:  # noqa: S310 (URL viene de Replicate)
        return resp.read()


# ---------------------------------------------------------------------------
# Post-procesamiento de la máscara
# ---------------------------------------------------------------------------

def normalize_mask(mask_bytes: bytes, target_size: Tuple[int, int]) -> bytes:
    """Convierte la máscara a PNG con alpha: blanco opaco donde está la pared,
    transparente fuera. Suaviza bordes con un blur leve."""
    from PIL import Image, ImageFilter

    raw = Image.open(BytesIO(mask_bytes)).convert("RGBA")
    if raw.size != target_size:
        raw = raw.resize(target_size, Image.LANCZOS)

    # Tomar el canal alpha o, si viene como blanco/negro, el luminance.
    r, g, b, a = raw.split()
    luminance = Image.merge("RGB", (r, g, b)).convert("L")
    # Usamos max(alpha, luminance) por si el modelo devuelve la máscara en RGB sólido.
    base = Image.eval(luminance, lambda v: v).point(lambda v: 255 if v > 32 else 0)
    if a.getextrema() != (0, 0):
        base = Image.eval(a, lambda v: 255 if v > 32 else 0)

    # Suavizar borde para que la pintura no quede pixelada.
    smooth = base.filter(ImageFilter.GaussianBlur(radius=1.5))

    out = Image.new("RGBA", target_size, (255, 255, 255, 0))
    out.putalpha(smooth)
    # canal RGB = blanco sólido (el frontend solo usa el alpha).
    white = Image.new("RGB", target_size, (255, 255, 255))
    out_rgba = Image.merge("RGBA", (*white.split(), smooth))

    buf = BytesIO()
    out_rgba.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def discover_raw_images() -> List[Path]:
    if not RAW_DIR.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    return sorted(p for p in RAW_DIR.iterdir() if p.suffix.lower() in exts)


def build_manifest(entries: List[dict]) -> None:
    manifest = {"version": 1, "scenes": entries}
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  -> manifest actualizado: {MANIFEST_PATH.relative_to(ROOT)}")


def load_existing_manifest() -> List[dict]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return list(data.get("scenes") or [])
    except Exception:
        return []


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    raw_images = discover_raw_images()
    if not raw_images:
        print(f"No hay imágenes en {RAW_DIR.relative_to(ROOT)}")
        print("Coloca fotos (JPG/PNG) ahí y vuelve a ejecutar.")
        sys.exit(1)

    from PIL import Image

    existing = {entry.get("id"): entry for entry in load_existing_manifest()}

    for img_path in raw_images:
        slug = slugify(img_path.stem)
        out_jpg = OUT_DIR / f"{slug}.jpg"
        out_mask = OUT_DIR / f"{slug}_paredes.png"

        if out_jpg.exists() and out_mask.exists() and slug in existing:
            print(f"[skip] {slug} ya tiene máscara generada. Borra los archivos para regenerar.")
            continue

        print(f"\n[{slug}] abriendo selector de punto…")
        point = pick_wall_point(img_path)
        if point is None:
            print(f"  -> saltado por el usuario")
            continue

        # Copiar/convertir a JPG estándar (1600px máximo).
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            max_side = 1600
            if max(im.size) > max_side:
                ratio = max_side / max(im.size)
                im = im.resize((int(im.width * ratio), int(im.height * ratio)), Image.LANCZOS)
                # Reescalar el click al nuevo tamaño.
                point = (int(point[0] * ratio), int(point[1] * ratio))
            im.save(out_jpg, format="JPEG", quality=86, optimize=True)
            target_size = im.size

        print(f"  -> punto: {point}, llamando a Replicate ({SAM_MODEL})…")
        try:
            mask_bytes = segment_wall(out_jpg, point)
        except Exception as e:
            print(f"  !! Falló segmentación: {e}")
            continue

        out_mask.write_bytes(normalize_mask(mask_bytes, target_size))
        print(f"  -> guardado: {out_mask.relative_to(ROOT)}")

        existing[slug] = {
            "id": slug,
            "label": humanize(slug),
            "base": f"/static/escenas/{slug}.jpg",
            "mask_paredes": f"/static/escenas/{slug}_paredes.png",
            "width": target_size[0],
            "height": target_size[1],
        }

    build_manifest(list(existing.values()))
    print("\nListo. Recarga /cliente en el navegador para ver las nuevas escenas.")


if __name__ == "__main__":
    main()
