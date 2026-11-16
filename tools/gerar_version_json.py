# -*- coding: utf-8 -*-
"""Gera version.json com hashes SHA-256 de cada ficheiro em _codigo\\."""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(65536), b""):
            h.update(c)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="Pasta que contém _codigo\\")
    ap.add_argument("--version", required=True)
    args = ap.parse_args()

    codigo_dir = os.path.join(args.base, "_codigo")
    if not os.path.isdir(codigo_dir):
        sys.exit(f"Pasta nao encontrada: {codigo_dir}")

    ficheiros = []
    for raiz, _, nomes in os.walk(codigo_dir):
        for nome in nomes:
            if nome == "version.json":
                continue
            path = os.path.join(raiz, nome)
            rel = os.path.relpath(path, codigo_dir).replace("\\", "/")
            ficheiros.append({
                "path": rel,
                "sha256": sha256(path),
                "size": os.path.getsize(path),
            })

    meta = {
        "version": args.version,
        "format_version": 1,
        "build_date": datetime.now().strftime("%Y-%m-%d"),
        "files": sorted(ficheiros, key=lambda x: x["path"]),
    }

    destino = os.path.join(codigo_dir, "version.json")
    with open(destino, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"version.json gerado: {len(ficheiros)} ficheiros")


if __name__ == "__main__":
    main()