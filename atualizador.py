# -*- coding: utf-8 -*-
"""atualizador.py — valida pacotes .sgeu e prepara _pending_update\\.

Um pacote .sgeu é um ZIP com:
    manifest.json           (metadados, versão, hashes)
    files/...               (ficheiros a substituir, caminhos relativos a _codigo\\)
    migrations.py           (opcional — migrações de BD)

A aplicação real (substituição em _codigo\\) é feita pelo bootstrap.py
no arranque seguinte — quando os ficheiros .py NÃO estão em uso.
"""
import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime


class PacoteInvalido(Exception):
    pass


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ler_version_json(base_dir):
    caminho = os.path.join(base_dir, "version.json")
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": "0.0.0", "format_version": 1}


def _version_tuple(v):
    partes = []
    for p in str(v).split("."):
        num = ""
        for c in p:
            if c.isdigit():
                num += c
            else:
                break
        partes.append(int(num) if num else 0)
    while len(partes) < 3:
        partes.append(0)
    return tuple(partes[:3])


# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------
def validar_pacote(caminho_zip, base_dir):
    """Abre o ZIP, valida estrutura + hashes. Devolve o manifest.

    Levanta PacoteInvalido em caso de problema.
    """
    if not zipfile.is_zipfile(caminho_zip):
        raise PacoteInvalido("O ficheiro nao e um ZIP valido.")

    with zipfile.ZipFile(caminho_zip, "r") as zf:
        nomes = set(zf.namelist())
        if "manifest.json" not in nomes:
            raise PacoteInvalido("Falta manifest.json no pacote.")

        with zf.open("manifest.json") as f:
            try:
                manifest = json.load(f)
            except json.JSONDecodeError as e:
                raise PacoteInvalido(f"manifest.json invalido: {e}")

        for chave in ("format", "app_version", "files"):
            if chave not in manifest:
                raise PacoteInvalido(f"manifest.json sem campo '{chave}'.")

        if manifest["format"] != "sge-update":
            raise PacoteInvalido("Formato desconhecido (esperado 'sge-update').")

        if not isinstance(manifest["files"], list) or not manifest["files"]:
            raise PacoteInvalido("manifest.files vazio ou invalido.")

        versao_actual = _ler_version_json(base_dir).get("version", "0.0.0")
        min_v = manifest.get("min_app_version", "0.0.0")
        if _version_tuple(versao_actual) < _version_tuple(min_v):
            raise PacoteInvalido(
                f"Esta versao ({versao_actual}) e demasiado antiga. "
                f"Precisa pelo menos de {min_v}. Faca uma instalacao completa."
            )

        for item in manifest["files"]:
            path = item.get("path")
            if not path or "sha256" not in item:
                raise PacoteInvalido(f"Entrada invalida no manifest: {item}")
            arcname = f"files/{path}"
            if arcname not in nomes:
                raise PacoteInvalido(f"Ficheiro ausente no ZIP: {path}")
            with zf.open(arcname) as f:
                if _sha256_bytes(f.read()) != item["sha256"]:
                    raise PacoteInvalido(f"Hash invalido para: {path}")

        return manifest


# ---------------------------------------------------------------------------
# Preparar update pendente
# ---------------------------------------------------------------------------
def preparar_update(caminho_zip, base_dir, callback_progresso=None):
    """Valida o pacote e extrai para _pending_update\\ com READY no fim.

    Devolve dict:
        ok, versao, n_ficheiros, requires_restart, erro
    """
    try:
        manifest = validar_pacote(caminho_zip, base_dir)
    except PacoteInvalido as e:
        return {"ok": False, "erro": str(e)}
    except Exception as e:
        return {"ok": False, "erro": f"Erro ao validar: {e}"}

    pending = os.path.join(base_dir, "_pending_update")
    if os.path.isdir(pending):
        # Pacote anterior por aplicar — limpar (o utilizador pode cancelar)
        try:
            shutil.rmtree(pending, ignore_errors=True)
        except Exception:
            pass

    try:
        os.makedirs(os.path.join(pending, "files"), exist_ok=True)

        total = len(manifest["files"])
        with zipfile.ZipFile(caminho_zip, "r") as zf:
            for i, item in enumerate(manifest["files"], start=1):
                path = item["path"]
                normalizado = os.path.normpath(path)
                if normalizado.startswith("..") or os.path.isabs(normalizado):
                    raise PacoteInvalido(f"Path invalido: {path}")

                destino = os.path.join(pending, "files", normalizado)
                os.makedirs(os.path.dirname(destino) or os.path.join(pending, "files"),
                            exist_ok=True)

                with zf.open(f"files/{path}") as src, open(destino, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                if callback_progresso:
                    callback_progresso(i, total, path)

        # Copiar migrations.py (se existir)
        if "migrations.py" in [n for n in zipfile.ZipFile(caminho_zip).namelist()]:
            with zipfile.ZipFile(caminho_zip, "r") as zf:
                with zf.open("migrations.py") as src, \
                     open(os.path.join(pending, "migrations.py"), "wb") as dst:
                    shutil.copyfileobj(src, dst)

        # Manifest
        with open(os.path.join(pending, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # READY por último (atomicidade: se faltar algo, não fica marcado)
        with open(os.path.join(pending, "READY"), "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds"))

        return {
            "ok": True,
            "versao": manifest["app_version"],
            "n_ficheiros": total,
            "requires_restart": bool(manifest.get("requires_restart", True)),
        }
    except Exception as e:
        try:
            shutil.rmtree(pending, ignore_errors=True)
        except Exception:
            pass
        return {"ok": False, "erro": f"Falha ao preparar update: {e}"}


def cancelar_update_pendente(base_dir):
    """Remove _pending_update\\ (se existir)."""
    pending = os.path.join(base_dir, "_pending_update")
    if os.path.isdir(pending):
        try:
            shutil.rmtree(pending, ignore_errors=True)
            return True
        except Exception:
            return False
    return True


def tem_update_pendente(base_dir):
    return os.path.isfile(os.path.join(base_dir, "_pending_update", "READY"))