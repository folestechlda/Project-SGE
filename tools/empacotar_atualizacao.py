# -*- coding: utf-8 -*-
"""Gera um pacote .sgeu a partir de ficheiros alterados.

Uso:
    python tools\\empacotar_atualizacao.py ^
        --version 1.3.3 ^
        --min-version 1.3.2 ^
        --changelog "Corrige margem negativa" ^
        --files services.py templates/dashboard.html ^
        --output instalador/SGE_update_1.3.3.sgeu
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(65536), b""):
            h.update(c)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--min-version", default="0.0.0")
    ap.add_argument("--changelog", default="")
    ap.add_argument("--files", nargs="+", required=True,
                    help="Caminhos relativos a raiz do projecto")
    ap.add_argument("--migrations", nargs="*", default=None)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    files_meta = []
    for rel in args.files:
        origem = os.path.join(PROJECT_ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(origem):
            sys.exit(f"Ficheiro nao encontrado: {rel}")
        files_meta.append({
            "path": rel.replace("\\", "/"),
            "sha256": sha256_file(origem),
            "size": os.path.getsize(origem),
        })

    migrations_meta = []
    if args.migrations:
        for rel in args.migrations:
            origem = os.path.join(PROJECT_ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(origem):
                sys.exit(f"Migracao nao encontrada: {rel}")
            migrations_meta.append(rel)

    manifest = {
        "format": "sge-update",
        "format_version": 1,
        "app_version": args.version,
        "min_app_version": args.min_version,
        "release_date": datetime.now().strftime("%Y-%m-%d"),
        "changelog": args.changelog,
        "requires_restart": True,
        "files": files_meta,
        "has_migrations": bool(migrations_meta),
    }

    out = args.output if os.path.isabs(args.output) \
          else os.path.join(PROJECT_ROOT, args.output)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in args.files:
            src = os.path.join(PROJECT_ROOT, rel.replace("/", os.sep))
            zf.write(src, arcname=f"files/{rel}".replace("\\", "/"))
        for rel in migrations_meta:
            src = os.path.join(PROJECT_ROOT, rel.replace("/", os.sep))
            zf.write(src, arcname="migrations.py")

    print(f"Pacote criado: {out}")
    print(f"  {len(files_meta)} ficheiro(s), "
          f"{os.path.getsize(out) // 1024} KB")


if __name__ == "__main__":
    main()