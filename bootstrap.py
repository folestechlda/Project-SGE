# -*- coding: utf-8 -*-
"""bootstrap.py — entry point do executável PyInstaller.

Empacota apenas as dependências (flask, reportlab, etc.). O código da
aplicação vive em _codigo ao lado do .exe e é validado por hash antes
de carregar. Se faltar um ficheiro, tenta restaurar de _internal code_backup.
"""
import hashlib
import json
import os
import shutil
import sys
import traceback


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(65536), b""):
                h.update(c)
        return h.hexdigest()
    except OSError:
        return None


def _restaurar_do_backup(base, rel, destino):
    origem = os.path.join(base, "_internal", "code_backup", rel)
    if not os.path.isfile(origem):
        return False
    try:
        os.makedirs(os.path.dirname(destino) or base, exist_ok=True)
        shutil.copy2(origem, destino)
        return True
    except OSError:
        return False


def _validar_e_reparar(base):
    """Lê version.json, verifica hashes, restaura o que faltar."""
    version_path = os.path.join(base, "_codigo", "version.json")
    try:
        with open(version_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except FileNotFoundError:
        # Sem version.json: só aceitamos em dev (não frozen).
        if getattr(sys, "frozen", False):
            try:
                import logging
                logging.error(
                    f"version.json ausente em {version_path}. "
                    f"Auto-reparo desactivado — reconstrua com build_exe.bat."
                )
            except Exception:
                pass
        return True, [], []
    except json.JSONDecodeError:
        return True, [], []

    ficheiros = meta.get("files", [])
    faltando, restaurados = [], []

    for item in ficheiros:
        rel = item["path"]
        destino = os.path.join(base, "_codigo", rel)
        sha_esperado = item.get("sha256")

        actual = _sha256(destino) if os.path.isfile(destino) else None
        if actual == sha_esperado:
            continue

        if _restaurar_do_backup(base, rel, destino):
            restaurados.append(rel)
            continue

        faltando.append(rel)

    return (len(faltando) == 0), faltando, restaurados


def _mostrar_erro_critico(base, faltando):
    msg = (
        "O SGE nao consegue arrancar porque faltam ficheiros do sistema:\n\n"
        + "\n".join(faltando[:10])
        + ("\n... e mais " + str(len(faltando) - 10) + " ficheiro(s).\n\n"
           if len(faltando) > 10 else "\n\n")
        + "Solucoes:\n"
        "1. Reinstale o SGE a partir do instalador.\n"
        "2. Ou contacte o suporte tecnico.\n\n"
        f"Pasta: {base}"
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "SGE - Erro critico", 0x10)
    except Exception:
        sys.stderr.write(msg + "\n")


def _aplicar_update_pendente(base):
    """Se existe _pending_update\\READY, aplica ANTES de carregar a app.

    Chamado só pelo bootstrap, antes de qualquer `import services`, logo
    não há locks de ficheiros. Se algo falhar, restaura do backup.
    """
    pending = os.path.join(base, "_pending_update")
    ready = os.path.join(pending, "READY")

    if not os.path.isfile(ready):
        return

    log_path = os.path.join(base, "app_errors.log")

    def _log(msg):
        try:
            from datetime import datetime
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass

    try:
        with open(os.path.join(pending, "manifest.json"), "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        _log(f"ERROR manifest pendente invalido: {e}")
        shutil.rmtree(pending, ignore_errors=True)
        return

    codigo_dir = os.path.join(base, "_codigo")
    backup_dir = os.path.join(base, "_internal", "code_backup")
    from datetime import datetime as _dt
    backup_update = os.path.join(
        base, "dados",
        f"update_pre_{_dt.now().strftime('%Y%m%d_%H%M%S')}",
    )
    os.makedirs(backup_update, exist_ok=True)

    aplicados = []
    falhas = []

    for item in manifest.get("files", []):
        rel = item["path"]
        origem = os.path.join(pending, "files", rel)
        destino = os.path.join(codigo_dir, rel)

        # Backup do actual (em _codigo) e do backup_base (para rollback total)
        if os.path.isfile(destino):
            bkp = os.path.join(backup_update, rel)
            os.makedirs(os.path.dirname(bkp) or backup_update, exist_ok=True)
            try:
                shutil.copy2(destino, bkp)
            except OSError:
                pass

        # Também actualizar code_backup\ para futuras validações
        bkp_base = os.path.join(backup_dir, rel)

        # Retry com espera: Windows pode ter handles abertos do processo
        # anterior. Tentar até 30x com 1s entre tentativas (Fix 2).
        sucesso = False
        ultimo_erro = None
        for tentativa in range(30):
            try:
                os.makedirs(os.path.dirname(destino) or codigo_dir, exist_ok=True)
                shutil.copy2(origem, destino)
                sucesso = True
                break
            except PermissionError as e:
                ultimo_erro = str(e)
                import time as _time
                _time.sleep(1)
            except Exception as e:
                ultimo_erro = str(e)
                break

        if sucesso:
            # Sincronizar backup
            if os.path.isfile(destino):
                os.makedirs(os.path.dirname(bkp_base) or backup_dir, exist_ok=True)
                try:
                    shutil.copy2(destino, bkp_base)
                except OSError:
                    pass
            aplicados.append(rel)
        else:
            falhas.append((rel, ultimo_erro or "falha desconhecida"))

    if falhas:
        # Rollback
        for item in manifest.get("files", []):
            rel = item["path"]
            bkp = os.path.join(backup_update, rel)
            if os.path.isfile(bkp):
                destino = os.path.join(codigo_dir, rel)
                try:
                    shutil.copy2(bkp, destino)
                except OSError:
                    pass
        _log(f"ERROR aplicar update pendente: {falhas}")
        shutil.rmtree(pending, ignore_errors=True)
        return

    # Regenerar version.json (com novos hashes)
    try:
        novos_meta_files = []
        for raiz, _, nomes in os.walk(codigo_dir):
            for nome in nomes:
                if nome == "version.json":
                    continue
                path_full = os.path.join(raiz, nome)
                rel = os.path.relpath(path_full, codigo_dir).replace("\\", "/")
                novos_meta_files.append({
                    "path": rel,
                    "sha256": _sha256(path_full),
                    "size": os.path.getsize(path_full),
                })
        meta = {
            "version": manifest["app_version"],
            "format_version": 1,
            "build_date": manifest.get("release_date", ""),
            "files": sorted(novos_meta_files, key=lambda x: x["path"]),
        }
        version_path = os.path.join(codigo_dir, "version.json")
        with open(version_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        # Cópia para a raiz (com retry — Windows pode ter handle aberto)
        raiz_version = os.path.join(base, "version.json")
        for _ in range(5):
            try:
                shutil.copy2(version_path, raiz_version)
                break
            except OSError:
                import time as _time
                _time.sleep(0.3)
    except Exception as e:
        _log(f"WARNING regenerar version.json: {e}")

    # Apagar _pending_update\ com retry (Windows pode ter handles)
    import time as _time
    apagado = False
    for _ in range(3):
        try:
            shutil.rmtree(pending)
            apagado = True
            break
        except OSError:
            _time.sleep(0.5)
    if not apagado:
        # Fallback: apagar READY (impede re-aplicação)
        try:
            os.remove(os.path.join(pending, "READY"))
        except OSError:
            pass
        _log(f"WARNING _pending_update nao apagado, mas READY removido")

    _log(f"INFO Update aplicado: v{manifest['app_version']} ({len(aplicados)} ficheiros)")


def main():
    base = _base_dir()

    # Log de arranque (para diagnosticar relaunch)
    try:
        from datetime import datetime
        with open(os.path.join(base, "app_errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} INFO "
                    f"Bootstrap arrancou (PID={os.getpid()}, argv={sys.argv})\n")
    except Exception:
        pass

    # --- CRÍTICO (Fix 1): se fomos lançados por relaunch, esperar 6s ANTES
    #     de aplicar o update. O processo pai precisa de morrer e o
    #     Windows precisa de libertar handles de services.py.
    if "--wait-start" in sys.argv:
        import time as _t
        _t.sleep(6.0)
        try:
            from datetime import datetime
            with open(os.path.join(base, "app_errors.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} INFO "
                        f"Bootstrap: sleep(6) concluido, a aplicar update\n")
        except Exception:
            pass

    try:
        _aplicar_update_pendente(base)
    except Exception as e:
        try:
            from datetime import datetime
            with open(os.path.join(base, "app_errors.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                        f"ERROR _aplicar_update_pendente: {e}\n")
        except Exception:
            pass

    codigo_dir = os.path.join(base, "_codigo")
    app_path = os.path.join(codigo_dir, "app.py")

    # Fallback: dev mode OU primeira execução sem _codigo
    if not os.path.isdir(codigo_dir):
        codigo_dir = os.path.dirname(os.path.abspath(__file__))
        app_path = os.path.join(codigo_dir, "app.py")

    if not os.path.isfile(app_path):
        _mostrar_erro_critico(base, ["app.py"])
        sys.exit(1)

    ok, faltando, restaurados = _validar_e_reparar(base)
    if restaurados:
        try:
            from datetime import datetime
            with open(os.path.join(base, "app_errors.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                        f"WARNING Auto-reparados: {restaurados}\n")
        except Exception:
            pass

    if not ok:
        _mostrar_erro_critico(base, faltando)
        sys.exit(1)

    # Preparar ambiente e executar app.py como __main__
    os.chdir(base)
    if base not in sys.path:
        sys.path.insert(0, base)
    if codigo_dir not in sys.path:
        sys.path.insert(0, codigo_dir)

    try:
        import runpy
        runpy.run_path(app_path, run_name="__main__")
    except Exception:
        tb = traceback.format_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, tb[:2000], "SGE - Erro ao arrancar", 0x10)
        except Exception:
            sys.stderr.write(tb)
        sys.exit(1)


if __name__ == "__main__":
    main()