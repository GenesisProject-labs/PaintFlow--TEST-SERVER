# Escenas para "Ver en pared" (vista cliente)

Esta carpeta `tools/` contiene el script que genera las máscaras de pared
que consume el modal "Ver en pared" del kiosko cliente.

## Flujo

```
static/escenas/raw/*.jpg         (fotos originales que ponés acá)
            │
            ▼  python tools/build_wall_masks.py
            │
static/escenas/*.jpg             (copia normalizada, max 1600px)
static/escenas/*_paredes.png     (máscara generada por SAM)
static/escenas/manifest.json     (índice consumido por el front)
```

## Setup (una sola vez)

1. Crear cuenta en https://replicate.com y obtener un token en
   https://replicate.com/account/api-tokens
2. En PowerShell:
   ```powershell
   setx REPLICATE_API_TOKEN "r8_xxx..."
   # cerrar y reabrir la terminal
   pip install replicate pillow
   ```

## Agregar escenas

1. Conseguir 3-5 fotos de cuartos/exteriores con paredes claramente visibles.
   Opciones libres de regalías:
   - https://www.pexels.com/search/empty%20room/
   - https://unsplash.com/s/photos/empty-room
2. Guardar como JPG en `static/escenas/raw/` con nombres descriptivos:
   `sala.jpg`, `dormitorio.jpg`, `exterior.jpg`, etc.
3. Ejecutar:
   ```powershell
   python tools/build_wall_masks.py
   ```
4. Para cada foto se abre una ventana — hacer **un click sobre la pared
   principal** (la zona pintable más grande). El script llama a Replicate
   (~5-10s por foto, ~USD 0.005) y guarda la máscara.
5. Recargar `/cliente` y la nueva escena aparece en el modal.

## Regenerar una escena

Borrá los archivos correspondientes y volvé a correr:
```powershell
Remove-Item static/escenas/sala.jpg, static/escenas/sala_paredes.png
python tools/build_wall_masks.py
```

## Si el modelo de Replicate cambia

El script usa `meta/sam-2` por defecto. Si Replicate retira o renombra el
modelo, exportá otro:
```powershell
$env:SAM_MODEL = "lucataco/sam-2"
python tools/build_wall_masks.py
```
