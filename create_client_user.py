#!/usr/bin/env python3
"""
Crear o actualizar un usuario cliente vinculado a una sucursal.
Uso:
  python create_client_user.py --username cliente --password cliente123 --sucursal "San Martin"
"""

import argparse
import hashlib
from database import DatabasePool


def resolve_sucursal_id(cur, sucursal_name: str):
    cur.execute(
        """
        SELECT id, nombre
        FROM sucursales
        WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(%s))
        LIMIT 1
        """,
        (sucursal_name,)
    )
    row = cur.fetchone()
    if row:
        return int(row[0]), str(row[1])

    cur.execute(
        """
        SELECT id, nombre
        FROM sucursales
        WHERE LOWER(nombre) LIKE LOWER(%s)
        ORDER BY id
        LIMIT 1
        """,
        (f"%{sucursal_name.strip()}%",)
    )
    row = cur.fetchone()
    if row:
        return int(row[0]), str(row[1])

    return None, None


def create_or_update_client(username: str, password: str, sucursal_name: str):
    conn = None
    try:
        conn = DatabasePool.get_connection()
        cur = conn.cursor()

        sucursal_id, sucursal_real = resolve_sucursal_id(cur, sucursal_name)
        if not sucursal_id:
            raise RuntimeError(f"No se encontro sucursal para: {sucursal_name}")

        password_hash = hashlib.sha256(password.encode()).hexdigest()
        nombre_completo = f"Cliente {sucursal_real}"
        email = f"{username}@paintflow.local"

        cur.execute("SELECT id FROM usuarios WHERE LOWER(TRIM(username)) = LOWER(TRIM(%s)) LIMIT 1", (username,))
        row = cur.fetchone()

        if row:
            user_id = int(row[0])
            cur.execute(
                """
                UPDATE usuarios
                SET password_hash = %s,
                    nombre_completo = %s,
                    email = %s,
                    rol = 'cliente',
                    sucursal_id = %s,
                    activo = true,
                    fecha_modificacion = NOW()
                WHERE id = %s
                """,
                (password_hash, nombre_completo, email, sucursal_id, user_id)
            )
            action = "actualizado"
        else:
            cur.execute(
                """
                INSERT INTO usuarios (username, password_hash, nombre_completo, email, rol, sucursal_id, activo, fecha_creacion)
                VALUES (%s, %s, %s, %s, 'cliente', %s, true, NOW())
                RETURNING id
                """,
                (username, password_hash, nombre_completo, email, sucursal_id)
            )
            user_id = int(cur.fetchone()[0])
            action = "creado"

        conn.commit()

        print(f"✅ Usuario cliente {action}: {username}")
        print(f"🪪 ID: {user_id}")
        print(f"🏬 Sucursal: {sucursal_real} (id={sucursal_id})")
        print(f"🔐 Password: {password}")
        print("➡️  Inicia sesion en el portal y se comportara como usuario de tienda (LabelsApp Web).")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if conn:
            DatabasePool.return_connection(conn)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="cliente")
    parser.add_argument("--password", default="cliente123")
    parser.add_argument("--sucursal", default="San Martin")
    args = parser.parse_args()
    create_or_update_client(args.username, args.password, args.sucursal)
