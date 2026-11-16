# -*- coding: utf-8 -*-
"""criar_sgeu.py — gera um pacote .sgeu para o SGE.

Uso:
    python criar_sgeu.py 1.3.4

Produz: instalador/SGE_update_1.3.4.sgeu
"""
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime

# Ficheiros a incluir no pacote (caminhos relativos à raiz do projecto)
FICHEIROS = [
    "app.py",
    "atualizador.py",
    "comparador_estoque.py",
    "database.py",
    "diagnostico.py",
    "reports.py",
    "services.py",
    "templates/base.html",
    "templates/movimento_form.html",
    "templates/movimentos.html",
    "templates/comparar_estoque.html",
    "templates/comparar_estoque_resultado.html",
]


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def criar_pacote(versao_nova):
    raiz = os.path.dirname(os.path.abspath(__file__))

    # Ler versão actual (para mostrar)
    vj_path = os.path.join(raiz, "version.json")
    if os.path.exists(vj_path):
        with open(vj_path, "r", encoding="utf-8") as f:
            versao_actual = json.load(f).get("version", "0.0.0")
    else:
        versao_actual = "0.0.0"

    print(f"Versao actual: {versao_actual}")
    print(f"Versao nova:   {versao_nova}")
    print()

    faltam = [r for r in FICHEIROS if not os.path.exists(os.path.join(raiz, r))]
    if faltam:
        print("[ERRO] Faltam ficheiros:")
        for f in faltam:
            print(f"  - {f}")
        sys.exit(1)

    # Manifest dos ficheiros
    files_manifest = []
    for rel in FICHEIROS:
        path = os.path.join(raiz, rel)
        files_manifest.append({
            "path": rel,
            "sha256": _sha256_file(path),
            "size": os.path.getsize(path),
        })

    # version.json que será instalado no cliente
    version_json_bytes = json.dumps(
        {"version": versao_nova, "format_version": 1},
        indent=2, ensure_ascii=False,
    ).encode("utf-8")

    # Manifest principal
    manifest = {
        "format": "sge-update",
        "app_version": versao_nova,
        # IMPORTANTE: aceitar qualquer versão (0.0.0 incluído),
        # porque o cliente pode não ter version.json.
        "min_app_version": "0.0.0",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "requires_restart": True,
        "files": files_manifest,
    }

    pasta_saida = os.path.join(raiz, "instalador")
    os.makedirs(pasta_saida, exist_ok=True)
    nome_sgeu = f"SGE_update_{versao_nova}.sgeu"
    caminho_sgeu = os.path.join(pasta_saida, nome_sgeu)

    with zipfile.ZipFile(caminho_sgeu, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )
        for rel in FICHEIROS:
            src = os.path.join(raiz, rel)
            arcname = f"files/{rel.replace(os.sep, '/')}"
            zf.write(src, arcname)
            print(f"  + {rel}")

        # version.json gerado (não existe como ficheiro, é virtual)
        zf.writestr("files/version.json", version_json_bytes)
        print("  + version.json (gerado, v" + versao_nova + ")")

    tamanho = os.path.getsize(caminho_sgeu)
    print()
    print(f"[OK] Pacote criado: {caminho_sgeu}")
    print(f"     Tamanho: {tamanho / 1024:.1f} KB")
    print(f"     Ficheiros: {len(FICHEIROS) + 1}")
    print()
    print("No cliente:")
    print("  1. Abre o SGE -> Administracao -> Actualizacoes")
    print(f"  2. Escolhe o ficheiro: {nome_sgeu}")
    print("  3. Clica Aplicar -> Fecha o SGE -> Volta a abrir")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python criar_sgeu.py <versao>")
        print("Exemplo: python criar_sgeu.py 1.3.4")
        sys.exit(1)
    criar_pacote(sys.argv[1])