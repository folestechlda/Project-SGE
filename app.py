# -*- coding: utf-8 -*-
"""Aplicação principal (app.py) — pontos de entrada e rotas Flask.

SGE - Sistema de Gestão de Estoque.
"""

import atexit
import logging
import math
import os
import secrets 
import shutil
import signal
import sqlite3
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timedelta
from functools import wraps
from uuid import uuid4

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file, jsonify, abort, make_response,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from database import (
    init_db, get_conn, get_config, set_config,
    get_base_dir, DB_PATH, get_company_info, registrar_auditoria,
)
import services
import reports as reportmod
import relatorios_dados as rd
import re
import diagnostico as diagmod
import json
import tempfile
import time
import atualizador as _atualizador
import comparador_estoque as compmod

APP_PORT = 5701
APP_NAME = "SGE"

# Modo servidor (hospedagem online): SGE_MODO_SERVIDOR=1. Sem esta variável o SGE comporta-se
# exactamente como a versão desktop.
SERVIDOR = os.environ.get("SGE_MODO_SERVIDOR") == "1"


# ---------------------------------------------------------------------------
# Chave de sessão persistente
# ---------------------------------------------------------------------------
def _get_or_create_secret_key():
    env_key = (os.environ.get("SGE_SECRET_KEY") or "").strip()
    if env_key:
        return env_key
    key_path = os.path.join(get_base_dir(), ".secret_key")
    try:
        with open(key_path, "r") as f:
            key = f.read().strip()
            if key:
                return key
    except (FileNotFoundError, OSError):
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_path, "w") as f:
            f.write(key)
    except OSError:
        pass
    return key


# ---------------------------------------------------------------------------
# Recursos empacotados (PyInstaller)
# ---------------------------------------------------------------------------
def _resource_path(relative_path):
    # Modo modular: templates/ e static/ vivem em _codigo\ ao lado do .exe.
    # Em dev, ficam na pasta do próprio app.py.
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        codigo_dir = os.path.join(base, "_codigo")
        if os.path.isdir(codigo_dir):
            return os.path.join(codigo_dir, relative_path)
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)

_UPDATE_STATE = {
    "state": "idle",       # idle | applying | done | error
    "current": 0,
    "total": 0,
    "file": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
}
# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(
    __name__,
    template_folder=_resource_path("templates"),
    static_folder=_resource_path("static"),
    static_url_path="/static",
)
app.secret_key = _get_or_create_secret_key()
app.config["TEMPLATES_AUTO_RELOAD"] = not SERVIDOR
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # cabe um backup grande

if SERVIDOR:
    # Atrás do proxy do serviço (HTTPS): usa o IP real do cliente e o esquema https
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SGE_COOKIE_SECURE", "1") != "0",
    )
    logging.basicConfig(level=logging.WARNING,       # logs vão para a consola do serviço
                        format="%(asctime)s %(levelname)s %(message)s")
else:
    logging.basicConfig(
        filename=os.path.join(get_base_dir(), "app_errors.log"),
        level=logging.ERROR,
        format="%(asctime)s %(levelname)s %(message)s",
    )


# ---------------------------------------------------------------------------
# Encerramento limpo
# ---------------------------------------------------------------------------
_shutdown_requested = False


def _encerrar_tudo():
    global _shutdown_requested
    if _shutdown_requested:
        return
    _shutdown_requested = True
    try:
        logging.shutdown()
    except Exception:
        pass
    try:
        if getattr(sys, "frozen", False):
            os._exit(0)
        else:
            sys.exit(0)
    except Exception:
        try:
            os._exit(0)
        except Exception:
            pass


if not SERVIDOR:   # num servidor quem gere o ciclo de vida é o gunicorn
    atexit.register(_encerrar_tudo)
    try:
        signal.signal(signal.SIGINT, lambda s, f: (_encerrar_tudo(), sys.exit(0)))
        signal.signal(signal.SIGTERM, lambda s, f: (_encerrar_tudo(), sys.exit(0)))
    except Exception:
        pass


@app.before_request
def _bloquear_funcoes_de_desktop():
    """Em modo servidor não há encerrar/actualizar pacotes .sgeu (o código vem do deploy)."""
    if SERVIDOR and (request.path == "/api/shutdown" or request.path.startswith(
            ("/atualizacoes", "/api/atualizacoes"))):
        abort(404)


@app.route("/healthz")
def healthz():
    """Verificação de saúde para o serviço de hospedagem (sem login)."""
    try:
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception:
        return ("erro", 503)
    return ("ok", 200)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _client_ip():
    try:
        return request.remote_addr or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def safe_parse_float(value, default: "float | None" = 0.0, milhares: bool = True):
    """Aceita '1.500,50' (pt) e '1500.50'. Em dinheiro '1.500' = 1500; em quantidades
    (milhares=False) '2.500' = 2,5. Ver services.parse_numero_pt."""
    if value is None or value == "":
        return default
    v = services.parse_numero_pt(value, milhares=milhares)
    return default if v is None else v


def safe_parse_float_required(value, default: float) -> float:
    """Como safe_parse_float, mas o retorno é sempre float (nunca None)."""
    v = safe_parse_float(value, default)
    return default if v is None else float(v)


def safe_parse_int(value, default: int = 0) -> int:
    """Converte para inteiro aceitando vírgula decimal (ex.: '10,00' -> 10)."""
    v = safe_parse_float(value, None)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@app.errorhandler(Exception)
def handle_exception(e):
    # 404, 403, 405, 413... são respostas HTTP normais, não falhas da aplicação.
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    logging.error(traceback.format_exc())
    msg = str(e) or "Erro interno desconhecido"

    # API/AJAX → JSON para o JS consumir (fetch/atualizacoes.js, etc.)
    if (request.path.startswith("/api/")
            or request.accept_mimetypes.best == "application/json"
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"):
        return jsonify({"ok": False, "erro": msg}), 500

    # Navegação normal → AppModal.erro no frontend, depois redirecciona
    ref = request.referrer or ""
    if not ref.startswith("/") or ref.startswith("//") or "\\" in ref:
        ref = url_for("dashboard")

    payload = json.dumps({
        "msg": msg,
        "ref": ref,
        "detalhes": traceback.format_exc()[:4000],
    })
    modal_src = url_for("static", filename="js/modal.js")
    css_src   = url_for("static", filename="css/style.css")
    return (
        '<!DOCTYPE html><html lang="pt-MZ"><head><meta charset="utf-8">'
        f'<link rel="stylesheet" href="{css_src}">'
        '</head><body>'
        f'<script src="{modal_src}"></script>'
        '<script>'
        f'var __err = {payload};'
        '(function(){'
        '  function mostrar(){'
        '    if (window.AppModal) {'
        '      window.AppModal.erro(__err.msg, __err.detalhes).then(function(){'
        '        window.location.href = __err.ref;'
        '      });'
        '    } else {'
        '      document.body.textContent = "Erro interno: " + __err.msg;'
        '      setTimeout(function(){ window.location.href = __err.ref; }, 5000);'
        '    }'
        '  }'
        '  if (document.readyState === "loading")'
        '    document.addEventListener("DOMContentLoaded", mostrar);'
        '  else mostrar();'
        '})();'
        '</script></body></html>'
    ), 500


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def perfil_required(*perfis):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login", next=request.path))
            if session.get("perfil") not in perfis:
                flash("Você não tem permissão para acessar essa área.", "erro")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Context processor + filtros de formatação
# ---------------------------------------------------------------------------
def _contar_pendentes_menu():
    """Divergências de inventário à espera de decisão (só para Gerente/Admin)."""
    if session.get("perfil") not in ("Admin", "Gerente"):
        return 0
    try:
        c = get_conn()
        try:
            return c.execute(
                "SELECT COUNT(*) FROM auditoria_divergencias d JOIN auditorias a ON a.id_auditoria=d.id_auditoria "
                "WHERE d.estado='pendente' AND a.estado!='fechada'").fetchone()[0]
        finally:
            c.close()
    except Exception:
        return 0


@app.context_processor
def inject_globals():
    emp = get_company_info()
    primeira_install = get_config("primeira_instalacao", "1") == "1"
    empresa_nome = "" if primeira_install else emp.get("nome", "")
    diag = app.config.get("_diagnostico") or {}
    return {
        "current_user": {
            "nome": session.get("nome"),
            "perfil": session.get("perfil"),
        },
        "empresa": {**emp, "nome": empresa_nome,
                    "logo_url": _logo_url_para(emp.get("logo"))},
        "config": {
            "moeda": get_config("moeda", "MZN"),
            "metodo_custeio": get_config("metodo_custeio", "media_ponderada"),
        },
        "ano_atual": datetime.now().year,
        "app_name": APP_NAME,
        "modo_servidor": SERVIDOR,
        "hoje": datetime.now().date().isoformat(),
        "diagnostico": diag,
        "nav_pendentes": _contar_pendentes_menu(),
    }

def _formatar_numero_pt(value, decimals=2):
    try:
        decimals = int(decimals)
    except (TypeError, ValueError):
        decimals = 2
    try:
        val = float(value)
    except (TypeError, ValueError):
        val = 0.0
    if not math.isfinite(val):
        val = 0.0
    formatted = f"{val:,.{decimals}f}"
    parts = formatted.split(".")
    integer_part = parts[0].replace(",", ".")
    decimal_part = parts[1] if len(parts) > 1 else "0" * decimals
    return f"{integer_part},{decimal_part}" if decimals else integer_part


@app.template_filter("format_num")
def format_num(value, decimals=2):
    return _formatar_numero_pt(value, decimals)


@app.template_filter("format_int")
def format_int(value):
    try:
        val = int(float(value))
    except (TypeError, ValueError):
        val = 0
    return _formatar_numero_pt(val, 0)


@app.template_filter("format_currency")
def format_currency(value, decimals=2):
    return _formatar_numero_pt(value, decimals)


@app.template_filter("format_qtd")
def format_qtd(value, unidade=None):
    unidade_norm = (str(unidade) if unidade is not None else "").strip().lower()
    if unidade_norm == "kg":
        return _formatar_numero_pt(value, 3)
    try:
        return _formatar_numero_pt(round(float(value or 0)), 0)
    except (TypeError, ValueError):
        return "0"


@app.template_filter("mt")
def _filtro_mt(value, casas=2):
    """Valor em MT no formato dos relatórios: 10 780,00 (espaço fino nos milhares)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    txt = _formatar_numero_pt(abs(v), casas).replace(".", "\u202f")
    return ("\u2212" + txt) if v < -0.005 else txt


@app.template_filter("mts")
def _filtro_mts(value, casas=2):
    """Valor com sinal explícito (+12 000,00 / −4 800,00)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return ("+" if v > 0.005 else "") + _filtro_mt(v, casas)


@app.template_filter("qt")
def _filtro_qt(value, un=None):
    return rd.fmt_qtd(value, un)


@app.template_filter("sq")
def _filtro_sq(value, unidade=None):
    """Saldo/quantidade para ecrã: inteiro quando possível (4 em vez de 4.0);
    só o kg mostra decimais (vírgula, até 3 casas)."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if unidade != "kg" or abs(v - round(v)) < 0.0005:
        return str(int(round(v)))
    return f"{v:.3f}".rstrip("0").replace(".", ",")


@app.template_filter("pct")
def _filtro_pct(value, casas=1):
    if value is None:
        return "—"
    return f"{float(value):.{casas}f}".replace(".", ",")


@app.template_filter("dmy")
def _filtro_dmy(value):
    return rd.dmy(value)


# ---------------------------------------------------------------------------
# Login / Logout / Recuperação
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_val = request.form.get("login", "").strip()
        senha = request.form.get("senha", "")

        conn = get_conn()
        bloqueado, rest_seg = services.login_bloqueado(conn, login_val)
        if bloqueado:
            mins = math.ceil(rest_seg / 60)
            flash(f"Conta temporariamente bloqueada. Tente novamente em {mins} minuto(s).", "erro")
            conn.close()
            return render_template(
                "login.html",
                bloqueio_info={"bloqueado": bloqueado, "segundos": rest_seg},
                autocomplete="off",
            )

        user = conn.execute(
            "SELECT * FROM usuarios WHERE login=? AND ativo=1", (login_val,)
        ).fetchone()

        sucesso = bool(user and check_password_hash(user["senha_hash"], senha))
        services.registrar_tentativa_login(conn, login_val, sucesso, _client_ip())

        if sucesso:
            registrar_auditoria(conn, user["id_usuario"], "usuarios",
                                user["id_usuario"], "LOGIN", None, None, _client_ip())
            conn.commit()
            conn.close()
            session["user_id"] = user["id_usuario"]
            session["nome"] = user["nome"]
            session["perfil"] = user["perfil"]
            session["_sid"] = str(uuid4())
            if get_config("primeira_instalacao", "1") == "1":
                set_config("primeira_instalacao", "0")
            destino = request.args.get("next") or ""
            # só caminhos internos ("/x"); recusa "//host" e "http://..."
            if not destino.startswith("/") or destino.startswith("//") or "\\" in destino:
                destino = url_for("dashboard")
            return redirect(destino)
        else:
            bloq2, rest2 = services.login_bloqueado(conn, login_val)
            registrar_auditoria(conn, None, "usuarios", None, "LOGIN",
                                {"login": login_val, "sucesso": False}, None, _client_ip())
            conn.commit()
            conn.close()
            if bloq2:
                mins = math.ceil(rest2 / 60)
                flash(f"5 tentativas inválidas. Aguarde {mins} minuto(s).", "erro")
            else:
                flash("Login ou senha inválidos.", "erro")
            return render_template(
                "login.html",
                bloqueio_info={"bloqueado": bloq2, "segundos": rest2},
                autocomplete="off",
            )
    return render_template("login.html", autocomplete="off", bloqueio_info=None)

@app.route("/favicon.ico")
def favicon():
    """Silencia o pedido do browser pelo favicon."""
    from flask import send_from_directory
    pasta = os.path.join(app.static_folder or "", "img")
    if not os.path.exists(os.path.join(pasta, "favicon.ico")):
        return ("", 204)
    return send_from_directory(pasta, "favicon.ico",
                               mimetype="image/vnd.microsoft.icon")

@app.route("/recuperar-senha", methods=["GET", "POST"])
def recuperar_senha_inicio():
    if request.method == "POST":
        login_val = request.form.get("login", "").strip()
        conn = get_conn()
        res = services.gerar_token_recuperacao(conn, login_val)
        conn.close()
        if res:
            token, _user = res
            flash("Token gerado com sucesso.", "sucesso")
            return redirect(url_for("recuperar_senha_token", token=token))
        flash("Utilizador não encontrado ou inativo.", "erro")
    return render_template("recuperar_senha_passo1.html")


def _logo_url_para(nome_logo: "str | None") -> "str | None":
    """URL do logo servido pela rota /media/logo, com cache-busting.

    Em produção o `static_folder` do Flask aponta para `_MEIPASS` (temp.),
    mas o upload escreve em `get_base_dir()/static/img/` (persistente).
    Esta função usa a rota que serve do sítio correto.
    """
    nome = (nome_logo or "").strip()
    if not nome:
        return None
    if nome.startswith("data:image"):
        return nome
    # Sanitização: só aceita nome de ficheiro simples (sem path traversal)
    nome = os.path.basename(nome)
    caminho = os.path.join(get_base_dir(), "static", "img", nome)
    if not os.path.exists(caminho):
        return None
    try:
        v = int(os.path.getmtime(caminho))
    except OSError:
        v = 0
    return url_for("serve_logo", v=v)


@app.route("/media/logo")
def serve_logo():
    """Serve o logo da pasta de dados persistente."""
    from flask import send_from_directory
    nome = (get_config("logo_empresa", "") or "").strip()
    if not nome or nome.startswith("data:"):
        return ("", 404)
    nome = os.path.basename(nome)
    pasta = os.path.join(get_base_dir(), "static", "img")
    caminho = os.path.join(pasta, nome)
    if not os.path.exists(caminho):
        logging.error(f"serve_logo: ficheiro não encontrado — {caminho}")
        return ("", 404)
    return send_from_directory(pasta, nome, max_age=3600)

@app.route("/recuperar-senha/t/<token>", methods=["GET", "POST"])
def recuperar_senha_token(token):
    conn = get_conn()
    user = services.validar_token_recuperacao(conn, token)
    if not user:
        flash("Token inválido ou expirado.", "erro")
        conn.close()
        return redirect(url_for("login"))
    if request.method == "POST":
        nova = request.form.get("nova_senha", "")
        conf = request.form.get("confirmar_senha", "")
        if not services.validar_senha(nova):
            flash("Senha fraca (mín. 8 car., letra, número e símbolo).", "erro")
        elif nova != conf:
            flash("A confirmação da senha não confere.", "erro")
        else:
            conn.execute(
                "UPDATE usuarios SET senha_hash=? WHERE id_usuario=?",
                (generate_password_hash(nova), user["id_usuario"]),
            )
            services.consumir_token_recuperacao(conn, user["id_usuario"])
            registrar_auditoria(conn, user["id_usuario"], "usuarios",
                                user["id_usuario"], "UPDATE",
                                {"senha": "RECUPERADA"}, {"senha": "ALTERADA"}, _client_ip())
            conn.commit()
            conn.close()
            flash("Senha redefinida com sucesso.", "sucesso")
            return redirect(url_for("login"))
    conn.close()
    return render_template("recuperar_senha_passo2.html", token=token,
                           usuario_nome=user["nome"], usuario_login=user["login"])


@app.route("/logout")
def logout():
    conn = get_conn()
    if "user_id" in session:
        registrar_auditoria(conn, session["user_id"], "usuarios",
                            session["user_id"], "LOGOUT", None, None, _client_ip())
        conn.commit()
    conn.close()
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/shutdown", methods=["POST"])
@perfil_required("Admin", "Gerente")
def shutdown_app():
    janela = app.config.get("_janela_webview")
    if janela is not None:
        try:
            janela.destroy()
        except Exception:
            pass
        return jsonify({"ok": True})
    t = threading.Timer(1.5, _encerrar_tudo)
    t.daemon = True
    t.start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_conn()
    categoria_id = request.args.get("categoria_id", type=int)
    periodo_dias = request.args.get("periodo", default=30, type=int)

    filtro_cat_sql = " AND p.id_categoria = ?" if categoria_id else ""
    params_cat = (categoria_id,) if categoria_id else ()

    total_produtos = conn.execute(
        f"SELECT COUNT(*) c FROM produtos p WHERE p.ativo=1{filtro_cat_sql}", params_cat
    ).fetchone()["c"]
    total_categorias = conn.execute(
        "SELECT COUNT(*) c FROM categorias WHERE ativo=1"
    ).fetchone()["c"]

    valor_custo = conn.execute(
        f"""SELECT COALESCE(SUM(e.saldo_atual * COALESCE(NULLIF(p.ultimo_preco_entrada, 0), p.preco_custo)),0) v
            FROM produtos p JOIN estoque e ON e.id_produto=p.id_produto
            WHERE p.ativo=1{filtro_cat_sql}""", params_cat
    ).fetchone()["v"]
    valor_venda = conn.execute(
        f"""SELECT COALESCE(SUM(e.saldo_atual * p.preco_venda),0) v
            FROM produtos p JOIN estoque e ON e.id_produto=p.id_produto
            WHERE p.ativo=1{filtro_cat_sql}""", params_cat
    ).fetchone()["v"]

    inicio_mes = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    lucro_mes = conn.execute(
        f"""SELECT COALESCE(SUM(m.lucro),0) v FROM movimentos m
            JOIN produtos p ON p.id_produto=m.id_produto
            WHERE m.tipo='Saida' AND m.data_hora >= ?{filtro_cat_sql}""",
        (inicio_mes,) + params_cat,
    ).fetchone()["v"]

    abaixo_minimo = services.produtos_abaixo_minimo(conn)
    proximos_venc = services.produtos_proximos_validade(conn)
    num_alertas = len(abaixo_minimo) + len(proximos_venc)

    desde = (datetime.now() - timedelta(days=periodo_dias)).strftime("%Y-%m-%d")
    mov_rows = conn.execute(
        f"""SELECT substr(m.data_hora,1,10) dia, m.tipo, SUM(m.quantidade) qtd
            FROM movimentos m JOIN produtos p ON p.id_produto = m.id_produto
            WHERE substr(m.data_hora,1,10) >= ?{filtro_cat_sql}
            GROUP BY dia, m.tipo ORDER BY dia""",
        (desde,) + params_cat,
    ).fetchall()
    dias_labels = sorted(set(r["dia"] for r in mov_rows))
    entradas_serie = []
    saidas_serie = []
    for d in dias_labels:
        entradas_serie.append(sum(r["qtd"] for r in mov_rows if r["dia"] == d and r["tipo"] == "Entrada"))
        saidas_serie.append(sum(r["qtd"] for r in mov_rows if r["dia"] == d and r["tipo"] == "Saida"))

    valores_rows = conn.execute(
        f"""SELECT substr(m.data_hora,1,10) dia, m.tipo, SUM(m.valor_total) total
            FROM movimentos m JOIN produtos p ON p.id_produto = m.id_produto
            WHERE substr(m.data_hora,1,10) >= ?{filtro_cat_sql}
            GROUP BY dia, m.tipo ORDER BY dia""",
        (desde,) + params_cat,
    ).fetchall()
    compras_serie = [sum(r["total"] for r in valores_rows if r["dia"] == d and r["tipo"] == "Entrada") for d in dias_labels]
    vendas_serie = [sum(r["total"] for r in valores_rows if r["dia"] == d and r["tipo"] == "Saida") for d in dias_labels]

    lucro_rows = conn.execute(
        f"""SELECT substr(m.data_hora,1,10) dia, SUM(m.lucro) lucro
            FROM movimentos m JOIN produtos p ON p.id_produto = m.id_produto
            WHERE m.tipo='Saida' AND substr(m.data_hora,1,10) >= ?{filtro_cat_sql}
            GROUP BY dia ORDER BY dia""",
        (desde,) + params_cat,
    ).fetchall()
    lucro_dict = {r["dia"]: r["lucro"] for r in lucro_rows}
    lucro_serie = [round(lucro_dict.get(d, 0), 2) for d in dias_labels]

    mais_vendidos = conn.execute(
        f"""SELECT p.nome, SUM(m.quantidade) total
            FROM movimentos m JOIN produtos p ON p.id_produto = m.id_produto
            WHERE m.tipo='Saida' AND substr(m.data_hora,1,10) >= ?{filtro_cat_sql}
            GROUP BY p.id_produto ORDER BY total DESC LIMIT 10""",
        (desde,) + params_cat,
    ).fetchall()

    sugestoes = services.sugestoes_precos(conn)
    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()

    # ---- Faturas a crédito por estado (para o gráfico) ----
    faturas_estado = conn.execute("""
        SELECT status,
               COUNT(*) AS n,
               COALESCE(SUM(valor_total), 0) AS total
        FROM contas_receber
        WHERE status != 'Cancelada'
        GROUP BY status
    """).fetchall()
    _lbl = {"Aberta": "Em aberto",
            "Parcial": "Parcialmente pagas",
            "Liquidada": "Pagas"}
    faturas_status_labels = []
    faturas_status_valores = []
    faturas_status_qtds = []
    for _k in ("Aberta", "Parcial", "Liquidada"):
        _r = next((x for x in faturas_estado if x["status"] == _k), None)
        if _r:
            faturas_status_labels.append(_lbl[_k])
            faturas_status_valores.append(float(_r["total"] or 0))
            faturas_status_qtds.append(int(_r["n"] or 0))

    # ---- Top 10 clientes em dívida (para o gráfico) ----
    top_clientes_divida = conn.execute("""
        SELECT nome_cliente,
               COUNT(*) AS n_faturas,
               COALESCE(SUM(saldo), 0) AS saldo
        FROM contas_receber
        WHERE status != 'Cancelada' AND saldo > 0.01
        GROUP BY nome_cliente
        ORDER BY saldo DESC
        LIMIT 10
    """).fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        total_produtos=total_produtos, total_categorias=total_categorias,
        valor_custo=valor_custo, valor_venda=valor_venda, lucro_mes=lucro_mes,
        num_alertas=num_alertas, abaixo_minimo=abaixo_minimo[:8],
        proximos_venc=proximos_venc[:8],
        dias_labels=dias_labels, entradas_serie=entradas_serie, saidas_serie=saidas_serie,
        compras_serie=compras_serie, vendas_serie=vendas_serie, lucro_serie=lucro_serie,
        mais_vendidos=mais_vendidos, sugestoes=sugestoes[:8],
        categorias=categorias, categoria_id=categoria_id, periodo_dias=periodo_dias,
        faturas_status_labels=faturas_status_labels,
        faturas_status_valores=faturas_status_valores,
        faturas_status_qtds=faturas_status_qtds,
        top_clientes_divida=top_clientes_divida,
    )


# ---------------------------------------------------------------------------
# Categorias
# ---------------------------------------------------------------------------
@app.route("/categorias")
@login_required
def categorias_list():
    conn = get_conn()
    categorias = conn.execute("SELECT * FROM categorias ORDER BY nome").fetchall()
    conn.close()
    return render_template("categorias.html", categorias=categorias)


@app.route("/categorias/salvar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def categorias_salvar():
    id_categoria = request.form.get("id_categoria")
    nome = request.form.get("nome", "").strip()
    descricao = request.form.get("descricao", "").strip()
    if not nome:
        flash("Informe o nome da categoria.", "erro")
        return redirect(url_for("categorias_list"))
    conn = get_conn()
    try:
        antigos = None
        if id_categoria:
            antigos = conn.execute(
                "SELECT * FROM categorias WHERE id_categoria=?", (id_categoria,)
            ).fetchone()
            conn.execute("UPDATE categorias SET nome=?, descricao=? WHERE id_categoria=?",
                         (nome, descricao, id_categoria))
            registrar_auditoria(conn, session.get("user_id"), "categorias",
                                int(id_categoria), "UPDATE",
                                dict(antigos) if antigos else None,
                                {"nome": nome, "descricao": descricao}, _client_ip())
        else:
            cur = conn.execute(
                "INSERT INTO categorias (nome, descricao, ativo) VALUES (?,?,1)",
                (nome, descricao))
            registrar_auditoria(conn, session.get("user_id"), "categorias",
                                cur.lastrowid, "INSERT", None,
                                {"nome": nome, "descricao": descricao}, _client_ip())
        conn.commit()
        flash("Categoria salva com sucesso.", "sucesso")
    except Exception as e:
        flash(f"Erro ao salvar categoria: {e}", "erro")
    conn.close()
    return redirect(url_for("categorias_list"))


@app.route("/categorias/<int:id_categoria>/excluir", methods=["POST"])
@perfil_required("Admin")
def categorias_excluir(id_categoria):
    conn = get_conn()
    antigos = conn.execute(
        "SELECT * FROM categorias WHERE id_categoria=?", (id_categoria,)
    ).fetchone()
    em_uso = conn.execute("SELECT COUNT(*) c FROM produtos WHERE id_categoria=?",
                          (id_categoria,)).fetchone()["c"]
    if em_uso:
        conn.execute("UPDATE categorias SET ativo=0 WHERE id_categoria=?", (id_categoria,))
        flash("Categoria em uso por produtos: foi desativada (exclusão lógica).", "info")
    else:
        conn.execute("DELETE FROM categorias WHERE id_categoria=?", (id_categoria,))
        flash("Categoria excluída.", "sucesso")
    registrar_auditoria(conn, session.get("user_id"), "categorias",
                        id_categoria, "DELETE",
                        dict(antigos) if antigos else None, {"ativo": 0 if em_uso else None},
                        _client_ip())
    conn.commit()
    conn.close()
    return redirect(url_for("categorias_list"))


# ---------------------------------------------------------------------------
# Produtos
# ---------------------------------------------------------------------------
@app.route("/produtos")
@login_required
def produtos_list():
    conn = get_conn()
    nome = request.args.get("nome", "").strip()
    categoria_id = request.args.get("categoria_id", type=int)
    codigo = request.args.get("codigo", "").strip()

    sql = """SELECT p.*, c.nome AS categoria_nome, COALESCE(e.saldo_atual,0) AS saldo_atual
              FROM produtos p LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
              LEFT JOIN estoque e ON e.id_produto=p.id_produto
              WHERE p.ativo=1"""
    params = []
    if nome:
        sql += " AND p.nome LIKE ?"
        params.append(f"%{nome}%")
    if codigo:
        sql += " AND p.codigo LIKE ?"
        params.append(f"%{codigo}%")
    if categoria_id:
        sql += " AND p.id_categoria=?"
        params.append(categoria_id)
    sql += " ORDER BY p.nome"

    produtos = conn.execute(sql, params).fetchall()
    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("produtos.html", produtos=produtos, categorias=categorias,
                           filtros={"nome": nome, "categoria_id": categoria_id, "codigo": codigo},
                           situacao=services.situacao_estoque)


@app.route("/produtos/novo")
@perfil_required("Admin", "Gerente")
def produtos_novo():
    if "_sid" not in session:
        session["_sid"] = str(uuid4())
    conn = get_conn()
    sessao_id = session["_sid"]
    codigo_provisorio = services.reservar_codigo_provisorio(conn, sessao_id)
    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("produto_form.html", produto=None, categorias=categorias,
                           codigo_provisorio=codigo_provisorio, sessao_id=sessao_id,
                           modo="novo", necessita_codigo_seguranca=False)


@app.route("/api/produtos/codigo-provisorio")
@login_required
def api_codigo_provisorio():
    if "_sid" not in session:
        session["_sid"] = str(uuid4())
    sessao_id = session["_sid"]
    conn = get_conn()
    codigo = services.reservar_codigo_provisorio(conn, sessao_id)
    conn.close()
    return jsonify({"codigo": codigo, "sessao_id": sessao_id})


@app.route("/produtos/<int:id_produto>/editar")
@perfil_required("Admin", "Gerente")
def produtos_editar(id_produto):
    conn = get_conn()
    produto = conn.execute(
        "SELECT * FROM produtos WHERE id_produto=? AND ativo=1", (id_produto,)
    ).fetchone()
    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    if not produto:
        abort(404)
    codigo_seg = get_config("codigo_alteracao_produto", "")
    return render_template("produto_form.html", produto=produto, categorias=categorias,
                           modo="editar", necessita_codigo_seguranca=bool(codigo_seg))


@app.route("/produtos/salvar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def produtos_salvar():
    f = request.form
    conn = get_conn()
    try:
        id_produto_raw = f.get("id_produto")
        id_produto: "int | None" = int(id_produto_raw) if id_produto_raw else None
        sessao_id = f.get("sessao_id") or session.get("_sid", "")
        modo = "editar" if id_produto is not None else "novo"

        if id_produto is not None:
            codigo_seg_config = get_config("codigo_alteracao_produto", "")
            if codigo_seg_config:
                codigo_seg_forn = f.get("codigo_seguranca", "")
                if not services.validar_codigo_alteracao_produto(codigo_seg_forn):
                    flash("Código de segurança inválido para alterar produto.", "erro")
                    conn.close()
                    return redirect(url_for("produtos_list"))

        nome = f.get("nome", "").strip()
        if not nome:
            flash("Informe o nome do produto.", "erro")
            conn.close()
            return redirect(url_for("produtos_list"))

        if services.produto_nome_existe(conn, nome, id_produto_excluir=id_produto):
            flash("Já existe um produto ativo com este nome.", "erro")
            conn.close()
            return redirect(url_for("produtos_list"))

        id_cat_raw = f.get("id_categoria", "").strip()
        if not id_cat_raw:
            flash("Selecione uma categoria válida.", "erro")
            conn.close()
            return redirect(url_for("produtos_list"))
        try:
            id_categoria = int(id_cat_raw)
        except (TypeError, ValueError):
            flash("Categoria inválida.", "erro")
            conn.close()
            return redirect(url_for("produtos_list"))

        if modo == "novo":
            codigo = (f.get("codigo", "") or "").strip()
            if not codigo:
                codigo = services.gerar_codigo_produto(conn)
            services.usar_codigo_provisorio(conn, sessao_id, codigo)
        else:
            codigo = f.get("codigo", "").strip()
            if not codigo:
                flash("Código do produto ausente.", "erro")
                conn.close()
                return redirect(url_for("produtos_list"))

        def parse_ceil(v):
            try:
                return services.ceil5(safe_parse_float(v, 0.0))
            except Exception:
                return 0.0

        unidade_val = f.get("unidade") or "Unidade"
        if unidade_val not in services.UNIDADES_VALIDAS:
            flash("Unidade de medida inválida.", "erro")
            conn.close()
            return redirect(url_for("produtos_list"))

        if unidade_val in ("Unidade", "kg"):
            qtd_por_unidade_raw = 1
        else:
            qtd_por_unidade_raw = safe_parse_int(f.get("qtd_por_unidade"), 1)
            if qtd_por_unidade_raw < 1:
                qtd_por_unidade_raw = 1

        em_raw = (f.get("estoque_maximo") or "").strip()
        estoque_maximo = None
        if em_raw:
            _em = safe_parse_int(em_raw, 0)
            estoque_maximo = _em if _em > 0 else None

        dados = dict(
            codigo=codigo, nome=nome,
            id_categoria=id_categoria,
            unidade=unidade_val,
            qtd_por_unidade=qtd_por_unidade_raw,
            preco_custo=parse_ceil(f.get("preco_custo")),
            preco_venda=parse_ceil(f.get("preco_venda")),
            estoque_minimo=safe_parse_int(f.get("estoque_minimo"), 0),
            estoque_maximo=estoque_maximo,
            validade=f.get("validade") or None,
            fornecedor=f.get("fornecedor", "").strip(),
            observacoes=f.get("observacoes", "").strip(),
        )

        if id_produto is not None:
            row = conn.execute(
                "SELECT * FROM produtos WHERE id_produto=?", (id_produto,)
            ).fetchone()
            if not row:
                flash("Produto não encontrado para atualização.", "erro")
                conn.close()
                return redirect(url_for("produtos_list"))
            antigos = dict(row)
            conn.execute(
                """UPDATE produtos SET codigo=?, nome=?, id_categoria=?, unidade=?, qtd_por_unidade=?,
                   preco_custo=?, preco_venda=?, estoque_minimo=?, estoque_maximo=?, validade=?,
                   fornecedor=?, observacoes=? WHERE id_produto=?""",
                (dados["codigo"], dados["nome"], dados["id_categoria"], dados["unidade"],
                 dados["qtd_por_unidade"], dados["preco_custo"], dados["preco_venda"],
                 dados["estoque_minimo"], dados["estoque_maximo"], dados["validade"],
                 dados["fornecedor"], dados["observacoes"], id_produto),
            )
            alterados = services.atualizar_produto_refletir_estoque(
                conn, id_produto, antigos, dados)
            registrar_auditoria(conn, session.get("user_id"), "produtos",
                                id_produto, "UPDATE",
                                {**antigos, "campos_alterados": alterados},
                                {**dados, "campos_alterados": alterados},
                                _client_ip())
            flash("Produto atualizado com sucesso.", "sucesso")
        else:
            cur = conn.execute(
                """INSERT INTO produtos (codigo, nome, id_categoria, unidade, qtd_por_unidade,
                   preco_custo, preco_venda, estoque_minimo, estoque_maximo, validade, fornecedor,
                   observacoes, ativo, data_cadastro) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (dados["codigo"], dados["nome"], dados["id_categoria"], dados["unidade"],
                 dados["qtd_por_unidade"], dados["preco_custo"], dados["preco_venda"],
                 dados["estoque_minimo"], dados["estoque_maximo"], dados["validade"],
                 dados["fornecedor"], dados["observacoes"],
                 datetime.now().isoformat(timespec="seconds")),
            )
            conn.execute(
                "INSERT INTO estoque (id_produto, saldo_atual, ultima_atualizacao) VALUES (?,0,?)",
                (cur.lastrowid, datetime.now().isoformat(timespec="seconds")))
            registrar_auditoria(conn, session.get("user_id"), "produtos",
                        cur.lastrowid, "INSERT", None, dados, _client_ip())
            flash("Produto cadastrado com sucesso.", "sucesso")

        # --- 2.6: aviso de margem negativa no cadastro ---
        _pc = float(dados.get("preco_custo") or 0)
        _pv = float(dados.get("preco_venda") or 0)
        if _pc > 0 and _pv > 0 and _pv <= _pc:
            _margem = ((_pv - _pc) / _pv) * 100
            flash(
                f"⚠️ O produto '{dados['nome']}' tem MARGEM NEGATIVA "
                f"({_margem:.1f}%): custo {_pc:.2f}, venda {_pv:.2f}. "
                f"Reveja os preços — as vendas deste produto darão prejuízo.",
                "aviso",
            )

        conn.commit()
    except Exception as e:
        flash(f"Erro ao salvar produto: {e}", "erro")
    conn.close()
    return redirect(url_for("produtos_list"))


@app.route("/produtos/cancelar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def produtos_cancelar():
    sessao_id = request.form.get("sessao_id") or session.get("_sid", "")
    codigo = request.form.get("codigo") or None
    conn = get_conn()
    services.liberar_codigo_provisorio(conn, sessao_id, codigo)
    conn.close()
    return redirect(url_for("produtos_list"))


@app.route("/produtos/<int:id_produto>/excluir", methods=["POST"])
@perfil_required("Admin")
def produtos_excluir(id_produto):
    conn = get_conn()
    try:
        antigos = conn.execute(
            "SELECT * FROM produtos WHERE id_produto=?", (id_produto,)
        ).fetchone()
        if not antigos:
            flash("Produto não encontrado.", "erro")
            conn.close()
            return redirect(url_for("produtos_list"))

        movimentos_removidos = conn.execute(
            "SELECT COUNT(*) c FROM movimentos WHERE id_produto=?", (id_produto,)
        ).fetchone()["c"]
        inventarios_removidos = conn.execute(
            "SELECT COUNT(*) c FROM inventarios WHERE id_produto=?", (id_produto,)
        ).fetchone()["c"]
        itens_cotacao_removidos = conn.execute(
            "SELECT COUNT(*) c FROM itens_cotacao WHERE id_produto=?", (id_produto,)
        ).fetchone()["c"]

        conn.execute("DELETE FROM itens_cotacao WHERE id_produto=?", (id_produto,))
        conn.execute("DELETE FROM inventarios WHERE id_produto=?", (id_produto,))
        conn.execute("DELETE FROM movimentos WHERE id_produto=?", (id_produto,))
        conn.execute("DELETE FROM estoque WHERE id_produto=?", (id_produto,))
        conn.execute("DELETE FROM codigos_provisorios WHERE codigo=?", (antigos["codigo"],))
        conn.execute("DELETE FROM produtos WHERE id_produto=?", (id_produto,))

        registrar_auditoria(
            conn, session.get("user_id"), "produtos", id_produto, "DELETE",
            dict(antigos),
            {"acao": "EXCLUSAO_CASCATA",
             "movimentos_removidos": movimentos_removidos,
             "inventarios_removidos": inventarios_removidos,
             "itens_cotacao_removidos": itens_cotacao_removidos},
            _client_ip()
        )
        conn.commit()
        flash(
            f"Produto '{antigos['nome']}' excluído permanentemente. "
            f"({movimentos_removidos} movimento(s), "
            f"{inventarios_removidos} inventário(s), "
            f"{itens_cotacao_removidos} item(ns) de cotação removidos).",
            "sucesso"
        )
    except Exception as e:
        conn.rollback()
        flash(f"Erro ao excluir produto: {e}", "erro")
    conn.close()
    return redirect(url_for("produtos_list"))


@app.route("/api/produtos/buscar")
@login_required
def api_produtos_buscar():
    termo = request.args.get("q", "").strip()
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.id_produto, p.codigo, p.nome, p.unidade, p.qtd_por_unidade,
                  p.preco_custo, p.preco_venda, p.fornecedor, COALESCE(e.saldo_atual,0) saldo_atual
           FROM produtos p LEFT JOIN estoque e ON e.id_produto=p.id_produto
           WHERE p.ativo=1 AND (p.nome LIKE ? OR p.codigo LIKE ?) LIMIT 15""",
        (f"%{termo}%", f"%{termo}%"),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Inventário físico
# ---------------------------------------------------------------------------
@app.route("/estoque/inventario", methods=["GET", "POST"])
@login_required
def estoque_inventario():
    conn = get_conn()
    if request.method == "POST":
        exportar = request.form.get("exportar_pdf", "0") == "1"
        inventarios_inseridos = []
        invalid_entries = []

        for key, value in request.form.items():
            if not key.startswith("contagem_"):
                continue
            try:
                id_produto = int(key.split("_", 1)[1])
            except ValueError:
                continue
            texto = (value or "").strip()
            if texto == "":
                contagem = 0.0
            else:
                contagem = safe_parse_float(texto, None, milhares=False)
                if contagem is None:
                    continue
            prod = conn.execute(
                "SELECT nome, unidade FROM produtos WHERE id_produto=?", (id_produto,)
            ).fetchone()
            if (prod
                    and prod["unidade"] not in services.UNIDADES_DECIMAIS
                    and not float(contagem).is_integer()):
                invalid_entries.append(prod["nome"] or str(id_produto))
                continue

            saldo_row = conn.execute(
                "SELECT COALESCE(saldo_atual,0) AS saldo FROM estoque WHERE id_produto=?",
                (id_produto,),
            ).fetchone()
            saldo_registrado = saldo_row["saldo"] if saldo_row else 0
            diferenca = services.round5(contagem - saldo_registrado)

            anterior = conn.execute(
                "SELECT MAX(id_inventario) AS ult FROM inventarios WHERE id_produto=? AND exportado=1",
                (id_produto,),
            ).fetchone()
            valor_anterior = None
            if anterior and anterior["ult"]:
                ult = conn.execute(
                    "SELECT contagem_fisica FROM inventarios WHERE id_inventario=?",
                    (anterior["ult"],),
                ).fetchone()
                if ult and abs(ult["contagem_fisica"] - contagem) > 1e-9:
                    valor_anterior = ult["contagem_fisica"]

            cur = conn.execute(
                """INSERT INTO inventarios
                   (id_produto, contagem_fisica, saldo_registrado, diferenca,
                    data_registro, exportado, valor_anterior)
                   VALUES (?,?,?,?,?,?,?)""",
                (id_produto, contagem, saldo_registrado, diferenca,
                 datetime.now().isoformat(timespec="seconds"),
                 1 if exportar else 0, valor_anterior),
            )
            inventarios_inseridos.append(cur.lastrowid)
        conn.commit()

        if invalid_entries:
            flash("As seguintes contagens foram ignoradas (produtos que não "
                  "são 'kg' exigem valores inteiros): " + ", ".join(invalid_entries), "erro")

        if exportar and inventarios_inseridos:
            placeholders = ",".join("?" * len(inventarios_inseridos))
            linhas_inventario = conn.execute(
                f"""SELECT p.codigo, p.nome, i.saldo_registrado, i.contagem_fisica,
                           i.diferenca, i.valor_anterior
                    FROM inventarios i JOIN produtos p ON p.id_produto=i.id_produto
                    WHERE i.id_inventario IN ({placeholders}) ORDER BY p.nome""",
                inventarios_inseridos,
            ).fetchall()
            conn.close()
            colunas = ["Código", "Produto", "Saldo Regist.", "Contagem Fís.", "Diferença", "Alterado"]
            linhas = []
            for r in linhas_inventario:
                dif = services.qtd_relatorio(r["diferenca"])
                alt = "" 
                if r["valor_anterior"] is not None:
                    delta = services.qtd_relatorio(float(r["contagem_fisica"] or 0) - float(r["valor_anterior"] or 0))
                    alt = f"(antes: {r['valor_anterior']} -> Δ {delta:+g})"
                linhas.append([
                    r["codigo"], r["nome"],
                    services.qtd_relatorio(r["saldo_registrado"]),
                    services.qtd_relatorio(r["contagem_fisica"]),
                    dif, alt,
                ])
            sub = f"Total de {len(linhas)} produtos contados em {datetime.now().strftime('%d/%m/%Y %H:%M')}"
            buf = reportmod.gerar_pdf("Relatório de Inventário Físico", colunas, linhas, subtitulo=sub)
            resp = make_response(buf.getvalue())
            resp.headers["Content-Type"] = "application/pdf"
            resp.headers["Content-Disposition"] = (
                f"attachment; filename=inventario_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
            return resp

        if not invalid_entries:
            flash("Contagem de inventário registrada com sucesso.", "sucesso")
        conn.close()
        return redirect(url_for("estoque_inventario"))

    produtos_rows = conn.execute(
        """SELECT p.id_produto, p.codigo, p.nome, p.unidade, p.qtd_por_unidade,
                  COALESCE(e.saldo_atual,0) AS saldo_atual
           FROM produtos p LEFT JOIN estoque e ON e.id_produto=p.id_produto
           WHERE p.ativo=1 ORDER BY p.nome"""
    ).fetchall()

    produtos = []
    pos_export = set()
    for p in produtos_rows:
        pd = dict(p)
        ult = conn.execute(
            "SELECT valor_anterior FROM inventarios WHERE id_produto=? AND exportado=1 "
            "ORDER BY id_inventario DESC LIMIT 1",
            (pd["id_produto"],),
        ).fetchone()
        pd["valor_anterior"] = ult["valor_anterior"] if ult and "valor_anterior" in ult.keys() else None
        tem = conn.execute(
            "SELECT 1 FROM inventarios WHERE id_produto=? AND exportado=1 LIMIT 1",
            (pd["id_produto"],),
        ).fetchone()
        if tem:
            pos_export.add(pd["id_produto"])
        produtos.append(pd)
    conn.close()
    return render_template("inventario.html", produtos=produtos, ids_exportados=pos_export)


# ---------------------------------------------------------------------------
# Movimentações
# ---------------------------------------------------------------------------
@app.route("/movimentos")
@login_required
def movimentos_list():
    conn = get_conn()
    tipo = request.args.get("tipo", "")
    categoria_id = request.args.get("categoria_id", type=int)
    data_ini = request.args.get("data_ini", "")
    data_fim = request.args.get("data_fim", "")
    exportar = request.args.get("exportar", "")

    sql = """SELECT m.*, p.nome AS produto_nome, p.codigo AS produto_codigo,
                    u.nome AS usuario_nome, c.nome AS categoria_nome
              FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
              LEFT JOIN usuarios u ON u.id_usuario=m.id_usuario
              LEFT JOIN categorias c ON c.id_categoria=p.id_categoria WHERE 1=1"""
    params = []
    if tipo in ("Entrada", "Saida"):
        sql += " AND m.tipo=?"
        params.append(tipo)
    if categoria_id:
        sql += " AND p.id_categoria=?"
        params.append(categoria_id)
    if data_ini:
        sql += " AND m.data_hora >= ?"
        params.append(data_ini + "T00:00:00")
    if data_fim:
        sql += " AND m.data_hora <= ?"
        params.append(data_fim + "T23:59:59")
    sql += " ORDER BY m.data_hora DESC LIMIT 500"
    movimentos = conn.execute(sql, params).fetchall()
    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()

    if exportar in ("pdf", "excel"):
        colunas = ["Data/Hora", "Tipo", "Código", "Produto", "Categoria",
                   "Qtd. Orig.", "Unid.", "Qtd. Base", "Preço Unit.", "Total",
                   "Lucro", "Usuário", "Fornecedor/Cliente"]
        linhas = []
        for m in movimentos:
            linhas.append([
                m["data_hora"], m["tipo"], m["produto_codigo"], m["produto_nome"],
                m["categoria_nome"] or "—",
                m["quantidade_original"], m["unidade_original"], m["quantidade"],
                f"{services.round5(m['preco_unitario']):.5f}",
                f"{services.round5(m['valor_total']):.5f}",
                f"{services.round5(m['lucro']):.5f}",
                m["usuario_nome"] or "—",
                m["fornecedor"] or m["cliente"] or "—",
            ])
        sub = f"Filtros: tipo={tipo or 'Todos'}"
        conn.close()
        if exportar == "pdf":
            buf = reportmod.gerar_pdf("Relatório de Movimentações", colunas, linhas, subtitulo=sub)
            return send_file(buf, mimetype="application/pdf", as_attachment=True,
                             download_name=f"movimentacoes_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        else:
            buf = reportmod.gerar_excel("Relatório de Movimentações", colunas, linhas)
            return send_file(buf,
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True,
                             download_name=f"movimentacoes_{datetime.now():%Y%m%d_%H%M%S}.xlsx")

    conn.close()
    return render_template("movimentos.html", movimentos=movimentos, tipo=tipo,
                           categorias=categorias,
                           filtros={"categoria_id": categoria_id,
                                    "data_ini": data_ini, "data_fim": data_fim})


@app.route("/movimentos/exportar/<formato>")
@login_required
def movimentos_exportar(formato):
    if formato not in ("pdf", "excel"):
        abort(404)
    return redirect(url_for(
        "movimentos_list",
        exportar=formato,
        tipo=request.args.get("tipo", ""),
        categoria_id=request.args.get("categoria_id", ""),
        data_ini=request.args.get("data_ini", ""),
        data_fim=request.args.get("data_fim", ""),
    ))


@app.route("/movimentos/entrada", methods=["GET", "POST"])
@perfil_required("Admin", "Gerente", "Operador")
def movimento_entrada():
    conn = get_conn()
    if request.method == "POST":
        id_produto = request.form.get("id_produto", type=int)
        if not id_produto or not services.validar_produto_cadastrado(conn, id_produto):
            flash("Selecione um produto cadastrado e ativo antes de registrar a entrada.", "erro")
            conn.close()
            return redirect(url_for("movimento_entrada"))

        # FIX: aceita vírgula decimal (pt-PT) e valida antes de chamar o serviço.
        quantidade_val = safe_parse_float(request.form.get("quantidade"), None, milhares=False)
        if quantidade_val is None or quantidade_val <= 0:
            flash("Quantidade inválida (use números, ex.: 10 ou 10,5).", "erro")
            conn.close()
            return redirect(url_for("movimento_entrada"))

        preco_val = safe_parse_float(request.form.get("preco_unitario"), None)
        if preco_val is None or preco_val < 0:
            flash("Preço unitário inválido (use números, ex.: 12,50).", "erro")
            conn.close()
            return redirect(url_for("movimento_entrada"))

        unidade_val = request.form.get("unidade_original") or "Unidade"
        if unidade_val == "Unidade" and not float(quantidade_val).is_integer():
            flash("Quantidade inválida: produtos com unidade 'Unidade' devem ser inteiros.", "erro")
            conn.close()
            return redirect(url_for("movimento_entrada"))

        try:
            data_str = request.form.get("data_entrada") or ""
            data_hora = (data_str + "T" + datetime.now().strftime("%H:%M:%S")) if data_str else None
            services.registrar_entrada(
                conn,
                id_produto=id_produto,
                quantidade_original=quantidade_val,
                unidade_original=unidade_val,
                preco_unitario=preco_val,
                id_usuario=session["user_id"],
                nota_fiscal=request.form.get("nota_fiscal") or None,
                fornecedor=request.form.get("fornecedor") or None,
                observacoes=request.form.get("observacoes") or None,
                data_hora=data_hora,
                ip_address=_client_ip(),
            )
            flash("Entrada registrada com sucesso.", "sucesso")
            conn.close()
            return redirect(url_for("movimento_entrada"))
        except services.RegraNegocioError as e:
            flash(str(e), "erro")
        except Exception as e:
            flash(f"Erro ao registrar entrada: {e}", "erro")
    produtos = conn.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("movimento_form.html", tipo="Entrada", produtos=produtos)


# ---------------------------------------------------------------------------
# Vendas: envio único (anti duplo clique) e respostas JSON
# ---------------------------------------------------------------------------
_TOKENS_ENVIO = {}
_TOKENS_LOCK = threading.Lock()


def _emitir_token_envio():
    tok = secrets.token_urlsafe(16)
    agora = time.time()
    with _TOKENS_LOCK:
        for k in [k for k, t in _TOKENS_ENVIO.items() if agora - t > 43200]:
            del _TOKENS_ENVIO[k]
        _TOKENS_ENVIO[tok] = agora
    return tok


def _reservar_token_envio(tok):
    """True se o token ainda não foi usado; fica reservado (um único registo por token)."""
    with _TOKENS_LOCK:
        return _TOKENS_ENVIO.pop(tok, None) is not None


def _devolver_token_envio(tok):
    if tok:
        with _TOKENS_LOCK:
            _TOKENS_ENVIO[tok] = time.time()


def _pedido_json():
    return request.headers.get("X-Requested-With") == "fetch"


def _texto_erro(e):
    if e.get("linha") is None:
        return e["mensagem"]
    nome = f" · {e['produto']}" if e.get("produto") else ""
    return f"Linha {e['linha']}{nome}: {e['mensagem']}"


def _erro_linha(linha, campo, mensagem):
    return {"linha": linha, "campo": campo, "mensagem": mensagem,
            "id_produto": None, "produto": None, "codigo": None}


@app.route("/movimentos/saida", methods=["GET", "POST"])
@perfil_required("Admin", "Gerente", "Operador")
def movimento_saida():
    conn = get_conn()
    if request.method == "POST":
        json_mode = _pedido_json()
        acao = request.form.get("acao", "gravar")
        token = request.form.get("token_envio") or None
        cliente = request.form.get("cliente") or None
        forma_pagamento = request.form.get("forma_pagamento") or None
        observacoes = request.form.get("observacoes") or None
        data_str = request.form.get("data_venda") or ""
        data_hora_venda = (data_str + "T" + datetime.now().strftime("%H:%M:%S")) if data_str else None

        ids_produto = request.form.getlist("id_produto[]")
        quantidades = request.form.getlist("quantidade[]")
        unidades = request.form.getlist("unidade_original[]")
        precos = request.form.getlist("preco_unitario[]")

        itens, erros_parse = [], []
        for i, id_prod_str in enumerate(ids_produto):
            if not id_prod_str:
                continue
            linha = i + 1
            try:
                id_produto = int(id_prod_str)
            except (TypeError, ValueError):
                erros_parse.append(_erro_linha(linha, "produto", "Produto inválido."))
                continue
            quantidade = safe_parse_float(quantidades[i] if i < len(quantidades) else None, None, milhares=False)
            preco = safe_parse_float(precos[i] if i < len(precos) else None, None)
            if quantidade is None or quantidade <= 0:
                erros_parse.append(_erro_linha(linha, "quantidade", "A quantidade deve ser maior que zero (use vírgula só para decimais)."))
                continue
            if preco is None or preco < 0:
                erros_parse.append(_erro_linha(linha, "preco", "Preço inválido (ex.: 12,50 ou 12.50)."))
                continue
            itens.append({"linha": linha, "id_produto": id_produto, "quantidade": quantidade,
                          "unidade": unidades[i] if i < len(unidades) else "Unidade",
                          "preco_unitario": preco})

        if not itens and not erros_parse:
            erros_parse.append(_erro_linha(None, "itens", "Adicione pelo menos um produto à venda."))

        def _responder_erros(res):
            if json_mode:
                conn.close()
                return jsonify({"ok": False, "estado": "erros", "erros": res["erros"], "avisos": res["avisos"]})
            for e in res["erros"]:
                flash(_texto_erro(e), "erro")
            flash("Nenhuma saída foi registada (transacção cancelada).", "aviso")
            conn.close()
            return redirect(url_for("movimento_saida"))

        try:
            if acao == "validar" and json_mode:
                res = services.processar_venda_vista(
                    conn, itens, session["user_id"], erros_previos=erros_parse, apenas_validar=True)
                conn.close()
                return jsonify({"ok": not res["erros"], "estado": res["estado"],
                                "erros": res["erros"], "avisos": res["avisos"]})

            if token and not _reservar_token_envio(token):
                msg = "Esta venda já foi registada (envio repetido ignorado)."
                if json_mode:
                    conn.close()
                    return jsonify({"ok": False, "estado": "duplicado", "mensagem": msg,
                                    "redirect": url_for("movimento_saida")})
                flash(msg, "aviso")
                conn.close()
                return redirect(url_for("movimento_saida"))

            res = services.processar_venda_vista(
                conn, itens, session["user_id"], erros_previos=erros_parse,
                cliente=cliente, forma_pagamento=forma_pagamento, observacoes=observacoes,
                data_hora=data_hora_venda, ip_address=_client_ip(),
                # sem JavaScript não há passo de confirmação: os avisos não bloqueiam
                confirmar_avisos=(request.form.get("confirmar_avisos") == "1") or not json_mode)
        except Exception:
            _devolver_token_envio(token)
            conn.close()
            raise

        if res["estado"] == "erros":
            _devolver_token_envio(token)
            return _responder_erros(res)
        if res["estado"] == "confirmacao":
            _devolver_token_envio(token)
            conn.close()
            return jsonify({"ok": False, "estado": "confirmacao", "avisos": res["avisos"], "erros": []})

        flash(f"Venda {res['codigo_venda']} registada com sucesso ({res['sucessos']} item(ns)). "
              f"Guarde este código: é com ele que se anula a venda, se foi registada por engano.", "sucesso")
        if res["alertas_minimo"]:
            flash("Atenção: alguns produtos ficaram abaixo do estoque mínimo.", "aviso")
        if res["margens_negativas"]:
            nomes = ", ".join(f"{nome} ({lucro:+.2f})" for nome, lucro in res["margens_negativas"][:3])
            flash(f"⚠️ {len(res['margens_negativas'])} venda(s) com MARGEM NEGATIVA: {nomes}. "
                  f"Verifique se o preço praticado corresponde à unidade vendida "
                  f"(rolo, kg, unidade) ou se o produto está cadastrado com o "
                  f"preço da embalagem em vez do preço unitário.", "aviso")
        conn.close()
        if json_mode:
            return jsonify({"ok": True, "estado": "ok", "redirect": url_for("movimento_saida")})
        return redirect(url_for("movimento_saida"))

    produtos = conn.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("movimento_form.html", tipo="Saida", produtos=produtos,
                           token_envio=_emitir_token_envio())

# ---------------------------------------------------------------------------
# Estoque atual
# ---------------------------------------------------------------------------
@app.route("/estoque")
@login_required
def estoque_list():
    conn = get_conn()
    categoria_id = request.args.get("categoria_id", type=int)
    situacao_filtro = request.args.get("situacao", "")
    exportar = request.args.get("exportar", "")

    sql = """SELECT p.*, c.nome AS categoria_nome, COALESCE(e.saldo_atual,0) AS saldo_atual
              FROM produtos p LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
              LEFT JOIN estoque e ON e.id_produto=p.id_produto WHERE p.ativo=1"""
    params = []
    if categoria_id:
        sql += " AND p.id_categoria=?"
        params.append(categoria_id)
    sql += " ORDER BY p.nome"
    produtos_raw = conn.execute(sql, params).fetchall()

    linhas_processadas = []
    for p in produtos_raw:
        sit = services.situacao_estoque(p["saldo_atual"], p["estoque_minimo"], p["estoque_maximo"])
        if situacao_filtro and sit != situacao_filtro:
            continue
        linhas_processadas.append({**dict(p), "situacao": sit})

    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()

    if exportar in ("pdf", "excel"):
        colunas = ["Código", "Produto", "Categoria", "Unid.",
                   "Saldo Atual", "Mínimo", "Máximo", "P. Custo", "P. Venda",
                   "Valor Estoque (Custo)", "Valor Estoque (Venda)", "Situação"]
        linhas = []
        sit_label = {"abaixo_minimo": "Abaixo do mínimo",
                     "acima_maximo": "Acima do máximo", "normal": "Normal"}
        for p in linhas_processadas:
            custo_unit = float(p["ultimo_preco_entrada"] or p["preco_custo"] or 0)
            val_c = float(p["saldo_atual"] or 0) * custo_unit
            val_v = float(p["saldo_atual"] or 0) * float(p["preco_venda"] or 0)
            linhas.append([
                p["codigo"], p["nome"], p["categoria_nome"] or "—", p["unidade"],
                services.qtd_relatorio(p["saldo_atual"]),
                services.qtd_relatorio(p["estoque_minimo"]),
                services.qtd_relatorio(p["estoque_maximo"]) if p["estoque_maximo"] else None,
                float(p["preco_custo"] or 0),
                float(p["preco_venda"] or 0),
                val_c, val_v,
                sit_label.get(p["situacao"], p["situacao"]),
            ])
        sub = f"Filtros: categoria={categoria_id or 'Todas'} | situação={situacao_filtro or 'Todas'}"
        conn.close()
        if exportar == "pdf":
            buf = reportmod.gerar_pdf("Relatório de Estoque Atual", colunas, linhas, subtitulo=sub)
            return send_file(buf, mimetype="application/pdf", as_attachment=True,
                             download_name=f"estoque_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        else:
            buf = reportmod.gerar_excel("Relatório de Estoque Atual", colunas, linhas)
            return send_file(buf,
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True,
                             download_name=f"estoque_{datetime.now():%Y%m%d_%H%M%S}.xlsx")

    valor_custo_total = 0
    valor_venda_total = 0
    for p in linhas_processadas:
        custo_unit = p.get("ultimo_preco_entrada") or p.get("preco_custo") or 0
        preco_venda = p.get("preco_venda") or 0
        saldo = p.get("saldo_atual") or 0
        valor_custo_total += float(saldo) * float(custo_unit)
        valor_venda_total += float(saldo) * float(preco_venda)
    conn.close()
    return render_template("estoque.html", produtos=linhas_processadas, categorias=categorias,
                           categoria_id=categoria_id, situacao_filtro=situacao_filtro,
                           valor_custo_total=valor_custo_total, valor_venda_total=valor_venda_total)


# ---------------------------------------------------------------------------
# Relatórios
# ---------------------------------------------------------------------------
def _consulta_relatorio(conn, tipo_relatorio, args):
    categoria_id = args.get("categoria_id", type=int)
    id_produto = args.get("id_produto", type=int)
    fornecedor = args.get("fornecedor", "").strip()
    tipo_mov = args.get("tipo_mov", "")
    data_ini = args.get("data_ini", "")
    data_fim = args.get("data_fim", "")

    if tipo_relatorio == "estoque_atual":
        # FIX: agora lê p.estoque_maximo para a classificação ser correcta.
        sql = """SELECT p.codigo, p.nome, c.nome categoria, COALESCE(e.saldo_atual,0) saldo,
                         p.preco_custo, p.preco_venda, p.estoque_minimo, p.estoque_maximo
                  FROM produtos p LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
                  LEFT JOIN estoque e ON e.id_produto=p.id_produto WHERE p.ativo=1"""
        params = []
        if categoria_id:
            sql += " AND p.id_categoria=?"
            params.append(categoria_id)
        if id_produto:
            sql += " AND p.id_produto=?"
            params.append(id_produto)
        sql += " ORDER BY p.nome"
        rows = conn.execute(sql, params).fetchall()
        colunas = ["Código", "Produto", "Categoria", "Saldo", "Custo (mzn)", "Venda (mzn)", "Situação"]
        linhas = []
        for r in rows:
            sit = services.situacao_estoque(r["saldo"], r["estoque_minimo"], r["estoque_maximo"])
            sit_label = {"abaixo_minimo": "Abaixo do mínimo",
                         "acima_maximo": "Acima do máximo",
                         "normal": "Normal"}[sit]
            linhas.append([r["codigo"], r["nome"], r["categoria"] or "—",
                           services.qtd_relatorio(r["saldo"]),
                           float(r["preco_custo"] or 0),
                           float(r["preco_venda"] or 0), sit_label])
        return "Relatório de Estoque Atual", colunas, linhas

    if tipo_relatorio == "movimentacoes":
        sql = """SELECT m.data_hora, m.tipo, p.nome produto, m.quantidade, m.unidade_original,
                         m.preco_unitario, m.valor_total, COALESCE(m.fornecedor, m.cliente,'') terceiro
                  FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
                  LEFT JOIN categorias c ON c.id_categoria=p.id_categoria WHERE 1=1"""
        params = []
        if data_ini:
            sql += " AND m.data_hora >= ?"
            params.append(data_ini)
        if data_fim:
            sql += " AND m.data_hora <= ?"
            params.append(data_fim + "T23:59:59")
        if categoria_id:
            sql += " AND p.id_categoria=?"
            params.append(categoria_id)
        if id_produto:
            sql += " AND p.id_produto=?"
            params.append(id_produto)
        if tipo_mov in ("Entrada", "Saida"):
            sql += " AND m.tipo=?"
            params.append(tipo_mov)
        if fornecedor:
            sql += " AND m.fornecedor LIKE ?"
            params.append(f"%{fornecedor}%")
        sql += " ORDER BY m.data_hora DESC"
        rows = conn.execute(sql, params).fetchall()
        colunas = ["Data/Hora", "Tipo", "Produto", "Qtd.", "Unid.",
                   "Preço Unit.", "Valor Total", "Fornecedor/Cliente"]
        linhas = [[r["data_hora"], r["tipo"], r["produto"],
                   services.qtd_relatorio(r["quantidade"]), r["unidade_original"],
                   float(r["preco_unitario"] or 0),
                   float(r["valor_total"] or 0), r["terceiro"]] for r in rows]
        return "Relatório de Movimentações", colunas, linhas

    if tipo_relatorio == "vendas":
        sql = """SELECT p.nome produto, SUM(m.quantidade) qtd, SUM(m.valor_total) total,
                         SUM(m.lucro) lucro
                  FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
                  WHERE m.tipo='Saida'"""
        params = []
        if data_ini:
            sql += " AND m.data_hora >= ?"
            params.append(data_ini)
        if data_fim:
            sql += " AND m.data_hora <= ?"
            params.append(data_fim + "T23:59:59")
        if categoria_id:
            sql += " AND p.id_categoria=?"
            params.append(categoria_id)
        if id_produto:
            sql += " AND p.id_produto=?"
            params.append(id_produto)
        sql += " GROUP BY p.id_produto ORDER BY total DESC"
        rows = conn.execute(sql, params).fetchall()
        colunas = ["Produto", "Qtd. Vendida", "Receita Total (mzn)",
                   "Lucro Bruto (mzn)", "Margem (%)"]
        linhas = []
        for r in rows:
            total = float(r["total"] or 0)
            lucro = float(r["lucro"] or 0)
            margem = (lucro / total * 100) if total else 0
            linhas.append([r["produto"], services.qtd_relatorio(r["qtd"]),
                           total, lucro, f"{margem:.1f}%"])
        return "Relatório de Vendas", colunas, linhas

    if tipo_relatorio == "estoque_baixo":
        rows = services.produtos_abaixo_minimo(conn)
        colunas = ["Código", "Produto", "Saldo Atual", "Estoque Mínimo", "Déficit"]
        linhas = [[r["codigo"], r["nome"],
                   services.qtd_relatorio(r["saldo_atual"]),
                   services.qtd_relatorio(r["estoque_minimo"]),
                   services.qtd_relatorio(float(r["estoque_minimo"] or 0) - float(r["saldo_atual"] or 0))] for r in rows]
        return "Relatório de Produtos com Estoque Baixo", colunas, linhas

    if tipo_relatorio == "validade":
        rows = services.produtos_proximos_validade(conn)
        colunas = ["Código", "Produto", "Validade", "Saldo Atual", "Situação"]
        linhas = [[r["codigo"], r["nome"], r["validade"],
                   services.qtd_relatorio(r["saldo_atual"]),
                   "VENCIDO" if r["vencido"] else "Próximo ao vencimento"] for r in rows]
        return "Relatório de Validade", colunas, linhas

    if tipo_relatorio == "precos_venda":
        rows = conn.execute(
            """SELECT p.codigo, p.nome, c.nome categoria, p.preco_custo, p.preco_venda,
                      COALESCE(e.saldo_atual,0) saldo
               FROM produtos p LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
               LEFT JOIN estoque e ON e.id_produto=p.id_produto WHERE p.ativo=1 ORDER BY p.nome"""
        ).fetchall()
        colunas = ["Código", "Produto", "Categoria", "Preço Venda (mzn)", "Saldo Atual"]
        linhas = [[r["codigo"], r["nome"], r["categoria"] or "—",
                   float(r["preco_venda"] or 0),
                   services.qtd_relatorio(r["saldo"])] for r in rows]
        return "Relatório de Preços de Venda", colunas, linhas

    if tipo_relatorio == "lucratividade":
        sql = """SELECT p.nome produto, SUM(m.quantidade) qtd, SUM(m.valor_total) receita,
                         SUM(m.valor_total) - SUM(m.lucro) AS custo_total,
                         SUM(m.lucro) lucro
                  FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
                  WHERE m.tipo='Saida'"""
        params = []
        if data_ini:
            sql += " AND m.data_hora >= ?"
            params.append(data_ini)
        if data_fim:
            sql += " AND m.data_hora <= ?"
            params.append(data_fim + "T23:59:59")
        sql += " GROUP BY p.id_produto ORDER BY lucro DESC"
        rows = conn.execute(sql, params).fetchall()
        colunas = ["Produto", "Qtd. Vendida", "Receita (mzn)", "Custo (mzn)",
                   "Lucro Bruto (mzn)", "Margem (%)"]
        linhas = []
        for r in rows:
            receita = float(r["receita"] or 0)
            lucro = float(r["lucro"] or 0)
            custo = float(r["custo_total"] or 0)
            margem = (lucro / receita * 100) if receita else 0
            linhas.append([r["produto"], services.qtd_relatorio(r["qtd"]),
                           receita, custo, lucro, f"{margem:.1f}%"])
        return "Relatório de Lucratividade por Produto", colunas, linhas

    return "Relatório", [], []


@app.route("/relatorios")
@login_required
def relatorios():
    if not request.args.get("tipo"):
        return render_template("relatorios_hub.html")
    conn = get_conn()
    tipo_relatorio = request.args.get("tipo", "")
    titulo, colunas, linhas = ("", [], [])
    if tipo_relatorio:
        titulo, colunas, linhas = _consulta_relatorio(conn, tipo_relatorio, request.args)
    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("relatorios.html", tipo_relatorio=tipo_relatorio, titulo=titulo,
                           colunas=colunas, linhas=linhas, categorias=categorias, args=request.args)


@app.route("/relatorios/exportar/<formato>")
@login_required
def relatorios_exportar(formato):
    conn = get_conn()
    tipo_relatorio = request.args.get("tipo", "")
    titulo, colunas, linhas = _consulta_relatorio(conn, tipo_relatorio, request.args)
    conn.close()
    if not colunas:
        flash("Selecione um relatório antes de exportar.", "erro")
        return redirect(url_for("relatorios"))

    if formato == "pdf":
        buf = reportmod.gerar_pdf(titulo, colunas, linhas)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"{tipo_relatorio}.pdf")
    if formato == "excel":
        buf = reportmod.gerar_excel(titulo, colunas, linhas)
        return send_file(buf,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"{tipo_relatorio}.xlsx")
    abort(404)


# ---------------------------------------------------------------------------
# Relatórios nos modelos (Balancete, Estoque Geral, Posição, Vendas, Extratos,
# Perdas e Validade, Curva ABC, Divergências). Cálculo em relatorios_dados.py.
# ---------------------------------------------------------------------------
def _rel_contexto(**extra):
    """Dados comuns às folhas: quem emitiu e quando, e empresa/NUIT."""
    emp = get_company_info()
    return dict(emitido_por=session.get("nome", ""), emitido_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
                emp_nome=emp.get("nome") or "", emp_nuit=emp.get("nuit") or "", **extra)


def _rel_excel(titulo, colunas, linhas, base):
    titulo = re.sub(r"[\\/*?:\[\]]", "-", titulo)
    if request.args.get("exportar") == "pdf":
        import rel_pdf
        from database import get_company_info
        buf = rel_pdf.gerar(titulo, colunas, linhas, get_company_info(), _filtro_mt, rd.fmt_qtd)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"{base}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
    buf = reportmod.gerar_excel(titulo, colunas, linhas)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"{base}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")


def _categorias_ativas(conn):
    return conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()


@app.route("/relatorios/balancete")
@perfil_required("Admin", "Gerente")
def rel_balancete():
    ini, fim = rd.periodo(request.args.get("ini"), request.args.get("fim"))
    id_cat = request.args.get("categoria_id", type=int)
    conn = get_conn()
    dados = rd.balancete(conn, ini, fim, id_cat)
    cats = _categorias_ativas(conn)
    conn.close()
    if request.args.get("exportar") in ("excel", "pdf"):
        linhas = [[g["nome"], l["produto"], l["un"], l["q_ini"], l["ent"], l["sai"], l["q_fim"],
                   l["custo_unit"], l["valor_final"], l["situacao"]["texto"]]
                  for g in dados["grupos"] for l in g["linhas"]]
        linhas.append(["TOTAL DO ESTOQUE FINAL", "", "", "", "", "", "", "", dados["tot"]["final"], ""])
        return _rel_excel(f"Balancete de Estoque {dmy_(ini)} a {dmy_(fim)}",
                          ["Categoria", "Produto", "Un.", "Qtd inicial", "Entradas", "Saídas", "Qtd final",
                           "Custo unit.", "Valor final", "Situação"], linhas, "balancete")
    return render_template("rel_balancete.html", d=dados, categorias=cats, args=request.args,
                           id_cat=id_cat, **_rel_contexto())


def dmy_(iso):
    return rd.dmy(iso)


@app.route("/relatorios/estoque-geral")
@login_required
def rel_estoque_geral():
    ini, fim = rd.periodo(request.args.get("ini"), request.args.get("fim"))
    id_cat = request.args.get("categoria_id", type=int)
    conn = get_conn()
    dados = rd.estoque_geral(conn, ini, fim, id_cat)
    cats = _categorias_ativas(conn)
    conn.close()
    if request.args.get("exportar") in ("excel", "pdf"):
        linhas = [[g["nome"], l["produto"], l["un"], l["q_fim"], l["v_ini"], l["compras"], l["custo_vendas"],
                   l["perdas_aj"], l["v_fim"], l["pct"]] for g in dados["grupos"] for l in g["linhas"]]
        t = dados["tot"]
        linhas.append(["TOTAL DO ESTOQUE", "", "", "", t["v_ini"], t["compras"], t["custo_vendas"],
                       t["perdas_aj"], t["v_fim"], 100.0])
        return _rel_excel(f"Estoque Geral {dmy_(ini)} a {dmy_(fim)}",
                          ["Categoria", "Produto", "Un.", "Qtd final", "Valor inicial", "Compras",
                           "Custo das vendas", "Perdas e ajustes", "Valor final", "% do estoque"],
                          linhas, "estoque_geral")
    return render_template("rel_estoque_geral.html", d=dados, categorias=cats, args=request.args,
                           id_cat=id_cat, **_rel_contexto())


@app.route("/relatorios/posicao")
@login_required
def rel_posicao():
    data = rd._dia(request.args.get("data"), datetime.now().date().isoformat())
    id_cat = request.args.get("categoria_id", type=int)
    so_abaixo = request.args.get("so_abaixo") == "1"
    so_sem_mov = request.args.get("so_sem_mov") == "1"
    conn = get_conn()
    dados = rd.posicao_estoque(conn, data, id_cat, so_abaixo, so_sem_mov)
    cats = _categorias_ativas(conn)
    conn.close()
    if request.args.get("exportar") in ("excel", "pdf"):
        linhas = [[l["produto"], l["un"], l["saldo"], l["minimo"],
                   ("" if l["cobertura"] is None else l["cobertura"]), l["custo_unit"], l["valor"],
                   l["acao"]["texto"]] for l in dados["linhas"]]
        linhas.append(["TOTAL DO ESTOQUE", "", "", "", "", "", dados["total_listado"], ""])
        return _rel_excel(f"Posição de Estoque a {dmy_(data)}",
                          ["Produto", "Un.", "Saldo", "Mínimo", "Cobertura (dias)", "Custo unit.", "Valor",
                           "Situação"], linhas, "posicao_estoque")
    return render_template("rel_posicao.html", d=dados, categorias=cats, args=request.args,
                           id_cat=id_cat, so_abaixo=so_abaixo, so_sem_mov=so_sem_mov, **_rel_contexto())


@app.route("/relatorios/vendas")
@perfil_required("Admin", "Gerente")
def rel_vendas():
    ini, fim = rd.periodo(request.args.get("ini"), request.args.get("fim"))
    id_cat = request.args.get("categoria_id", type=int)
    id_prod = request.args.get("id_produto", type=int)
    id_user = request.args.get("id_usuario", type=int)
    forma = request.args.get("forma") or None
    conn = get_conn()
    dados = rd.vendas(conn, ini, fim, id_cat, id_prod, forma, id_user)
    cats = _categorias_ativas(conn)
    produtos = conn.execute("SELECT id_produto, nome FROM produtos ORDER BY nome").fetchall()
    usuarios = conn.execute("SELECT id_usuario, nome FROM usuarios ORDER BY nome").fetchall()
    formas = [r[0] for r in conn.execute(
        "SELECT DISTINCT forma_pagamento FROM movimentos WHERE forma_pagamento IS NOT NULL AND forma_pagamento!='' "
        "ORDER BY 1").fetchall()]
    conn.close()
    if request.args.get("exportar") in ("excel", "pdf"):
        linhas = [[l["produto"], l["un"], l["qtd"], l["preco_medio"], l["receita"], l["custo"], l["lucro"],
                   ("" if l["margem"] is None else l["margem"]), l["pct"]] for l in dados["linhas"]]
        t = dados["tot"]
        linhas.append(["TOTAL", "", "", "", t["vendas"], t["custo"], t["lucro"],
                       ("" if t["margem"] is None else t["margem"]), 100.0])
        return _rel_excel(f"Relatório de Vendas {dmy_(ini)} a {dmy_(fim)}",
                          ["Produto", "Un.", "Qtd vendida", "Preço médio", "Receita", "Custo", "Lucro",
                           "Margem %", "% da receita"], linhas, "vendas")
    return render_template("rel_vendas.html", d=dados, categorias=cats, produtos=produtos, usuarios=usuarios,
                           formas=formas, args=request.args, id_cat=id_cat, id_prod=id_prod, id_user=id_user,
                           forma=forma, **_rel_contexto())


@app.route("/relatorios/extrato-produto")
@login_required
def rel_extrato_produto():
    ini, fim = rd.periodo(request.args.get("ini"), request.args.get("fim"))
    modo = request.args.get("modo", "produto")
    if modo not in ("produto", "categoria", "geral"):
        modo = "produto"
    id_prod = request.args.get("id_produto", type=int)
    id_cat = request.args.get("categoria_id", type=int)
    conn = get_conn()
    produtos = conn.execute(
        """SELECT p.id_produto, p.nome, c.nome AS categoria FROM produtos p
           LEFT JOIN categorias c ON c.id_categoria=p.id_categoria ORDER BY p.nome""").fetchall()
    cats = _categorias_ativas(conn)
    d = None
    if modo == "produto" and id_prod:
        d = rd.extrato_produto(conn, id_prod, ini, fim)
    elif modo == "categoria" and id_cat:
        d = rd.extrato_categoria(conn, id_cat, ini, fim)
    elif modo == "geral":
        d = rd.extrato_geral(conn, ini, fim, id_cat)
    conn.close()
    if d and request.args.get("exportar") in ("excel", "pdf"):
        cols = ["Data", "Documento", "Tipo", "Entrada", "Saída", "Saldo", "Custo unit.", "Valor do movimento",
                "Saldo em valor"]
        linhas = []
        secs = [d] if modo == "produto" else (d["secoes"] if modo == "categoria" else [])
        if modo == "geral":
            cols = ["Categoria", "Produto", "Valor inicial", "Entradas", "Saídas", "Valor final"]
            linhas = [[l["categoria"], l["nome"], l["v_ini"], l["ent_v"], l["sai_v"], l["v_fim"]] for l in d["linhas"]]
        for sec in secs:
            linhas.append([f"{sec['nome']} ({sec['un']})", "", "Saldo inicial", "", "", sec["q_ini"], "", "", sec["v_ini"]])
            for l in sec["linhas"]:
                linhas.append([l["data"], l["doc"], l["tipo"], l["entrada"] or "", l["saida"] or "", l["saldo"],
                               l["custo_unit"], l["valor"], l["saldo_valor"]])
        return _rel_excel(f"Extrato {dmy_(ini)} a {dmy_(fim)}", cols, linhas, "extrato")
    return render_template("rel_extrato_produto.html", d=d, modo=modo, produtos=produtos, categorias=cats,
                           args=request.args, id_prod=id_prod, id_cat=id_cat, **_rel_contexto())


@app.route("/relatorios/perdas-validade")
@login_required
def rel_perdas_validade():
    hoje = datetime.now().date()
    ini = rd._dia(request.args.get("ini"), (hoje.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat())
    fim = rd._dia(request.args.get("fim"), (hoje.replace(day=1) - timedelta(days=1)).isoformat())
    base = rd._dia(request.args.get("base"), hoje.isoformat())
    if ini > fim:
        ini, fim = fim, ini
    conn = get_conn()
    dados = rd.perdas_validade(conn, ini, fim, base)
    conn.close()
    if request.args.get("exportar") in ("excel", "pdf"):
        cols = ["Tipo", "Data / Validade", "Produto", "Qtd", "Motivo / Faixa", "Justificação", "Valor"]
        linhas = [["Perda", o["data"], o["produto"], f"{rd.fmt_qtd(o['qtd'], o['un'])} {o['un']}",
                   o["motivo"], o["justificacao"], o["valor"]] for o in dados["ocorrencias"]]
        linhas.append(["TOTAL DAS PERDAS", "", "", "", "", "", dados["tot_valor"]])
        linhas += [["Validade", i["validade"], i["produto"], f"{rd.fmt_qtd(i['saldo'], i['un'])} {i['un']}",
                    i["faixa"], "", i["valor"]] for i in dados["itens"]]
        return _rel_excel("Perdas e Validade", cols, linhas, "perdas_validade")
    return render_template("rel_perdas_validade.html", d=dados, args=request.args, **_rel_contexto())


@app.route("/relatorios/gestao")
@perfil_required("Admin", "Gerente")
def rel_gestao():
    hoje = datetime.now().date()
    p = request.args.get("periodo", "")
    if p == "mes_ant":
        fim_d = hoje.replace(day=1) - timedelta(days=1); ini_d = fim_d.replace(day=1)
    elif p in ("semestre", "semestre_ant"):
        s0 = 1 if hoje.month <= 6 else 7
        ini_d = hoje.replace(month=s0, day=1)
        fim_d = hoje
        if p == "semestre_ant":
            fim_d = ini_d - timedelta(days=1)
            ini_d = fim_d.replace(month=1 if fim_d.month <= 6 else 7, day=1)
    elif p == "mes":
        ini_d, fim_d = hoje.replace(day=1), hoje
    else:
        ini_d = hoje.replace(day=1); fim_d = hoje
    ini = rd._dia(request.args.get("ini"), ini_d.isoformat())
    fim = rd._dia(request.args.get("fim"), fim_d.isoformat())
    if ini > fim:
        ini, fim = fim, ini
    conn = get_conn()
    d = rd.gestao(conn, ini, fim)
    conn.close()
    if request.args.get("exportar") in ("excel", "pdf"):
        v, c, e = d["vendas"], d["compras"], d["estoque"]
        L = []
        sec = lambda t: L.append([t.upper(), "", "", "", ""])
        sec("Conclusão"); L.append([d["veredicto"]["texto"], "", "", "", ""])
        for pt in d["pontos"]:
            L.append([pt["area"], "", "", "", pt["texto"]])
        sec("Vendas")
        L += [["Total vendido", v["total"], "", "", f"{v['n']} venda(s)"], ["Recebido à vista", v["vista"], "", "", ""],
              ["Faturado a crédito", v["credito"], "", "", ""], ["Custo das vendas", v["custo"], "", "", ""],
              ["Lucro bruto", v["lucro"], "", "", f"Margem {v['margem'] if v['margem'] is not None else '—'} %"]]
        sec("Compras"); L.append(["Total comprado", c["total"], "", "", f"{c['n']} entrada(s)"])
        for f in c["fornecedores"]:
            L.append(["  " + f["fornecedor"], f["valor"], "", "", ""])
        sec("Recebimentos: sistema vs conferido")
        for x in d["canais"]:
            L.append([x["canal"], x["esperado"], x["conferido"] if x["conferido"] is not None else "—",
                      x["diferenca"] if x["diferenca"] is not None else "—", x["texto"] + ((" " + x["obs"]) if x["obs"] else "")])
        L.append(["TOTAL", d["rec_esperado_total"], d["rec_conferido_total"], "", ""])
        L.append(["A receber de clientes", d["a_receber"]["valor"], "", "", f"{d['a_receber']['n']} fatura(s); em atraso {_filtro_mt(d['a_receber']['atraso'])}"])
        sec("Estoque")
        L += [["Valor inicial", e["inicial"], "", "", ""], ["+ Compras", e["compras"], "", "", ""],
              ["− Custo das vendas", e["custo_vendas"], "", "", ""], ["− Perdas e ajustes", e["perdas_aj"], "", "", ""],
              ["= Valor final", e["final"], "", "", "A conta fecha." if e["fecha"] else "A CONTA NÃO FECHA."]]
        if d["perdas"]:
            sec("Perdas")
            for x in d["perdas"]:
                L.append([f"{x['data']} · {x['produto']}", x["valor"], "", "", f"{x['motivo']}. {x['justificacao']} ({x['usuario']})"])
        if d["contagens"]:
            sec("Contagens físicas e diferenças de estoque")
            for c in d["contagens"]:
                L.append([f"Contagem de {c['data']} (inventário #{c['id']})", c["perdas"] + c["sobras"], "", "",
                          f"{c['n_com']} diferença(s) em {c['n_contados']} produto(s)"])
                for l in c["linhas"]:
                    just = ((l["motivo"] + (". " if l["motivo"] and l["obs"] else "") + l["obs"]) if l["justificada"] else "SEM JUSTIFICAÇÃO")
                    L.append([f"  {l['produto']}", f"{rd.fmt_qtd(l['sistema'], l['un'])} {l['un']}",
                              f"{rd.fmt_qtd(l['contagem'], l['un'])} {l['un']}", l["dif_v"], just])
        if d["ajustes"]:
            sec("Ajustes de inventário")
            for x in d["ajustes"]:
                L.append([f"{x['data']} · {x['produto']}", x["valor"], "", "", f"{x['usuario']}"])
        if d["anuladas"]:
            sec("Vendas anuladas")
            for x in d["anuladas"]:
                L.append([f"{x['numero']} · {x['cliente']}", x["valor"], "", "", f"{x['motivo']} ({x['usuario']})"])
        if len(d["evolucao"]) > 1:
            sec("Evolução mensal (vendas / compras / lucro / recebido)")
            for x in d["evolucao"]:
                L.append([x["mes"], x["vendas"], x["compras"], x["lucro"], f"Recebido {_filtro_mt(x['recebido'])}"])
        return _rel_excel("Relatório de Gestão", ["Item", "Sistema (MT)", "Conferido (MT)", "Diferença (MT)", "Observação"], L, "relatorio_gestao")
    return render_template("rel_gestao.html", d=d, args=request.args, canais=rd.CANAIS, **_rel_contexto())


@app.route("/relatorios/gestao/conferencia", methods=["POST"])
@perfil_required("Admin", "Gerente")
def rel_gestao_conferencia():
    ini = rd._dia(request.form.get("ini"), None); fim = rd._dia(request.form.get("fim"), None)
    if not ini or not fim:
        abort(400)
    conn = get_conn()
    n = 0
    for canal in rd.CANAIS:
        bruto = (request.form.get("valor_" + canal) or "").strip()
        if bruto == "":
            conn.execute("DELETE FROM conferencias_recebimento WHERE data_ini=? AND data_fim=? AND canal=?", (ini, fim, canal))
            continue
        valor = safe_parse_float(bruto, None)
        if valor is None or valor < 0:
            flash(f"Valor inválido em {canal}.", "erro")
            continue
        conn.execute(
            """INSERT INTO conferencias_recebimento (data_ini,data_fim,canal,valor_conferido,observacao,id_usuario,data_registo)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(data_ini,data_fim,canal) DO UPDATE SET valor_conferido=excluded.valor_conferido,
                 observacao=excluded.observacao, id_usuario=excluded.id_usuario, data_registo=excluded.data_registo""",
            (ini, fim, canal, valor, (request.form.get("obs_" + canal) or "").strip(), session["user_id"],
             datetime.now().isoformat(timespec="seconds")))
        n += 1
    conn.commit(); conn.close()
    flash(f"Conferência guardada ({n} canal/canais).", "sucesso")
    return redirect(url_for("rel_gestao", ini=ini, fim=fim) + "#recebimentos")


@app.route("/relatorios/curva-abc")
@perfil_required("Admin", "Gerente")
def rel_curva_abc():
    data = rd._dia(request.args.get("data"), datetime.now().date().isoformat())
    conn = get_conn()
    dados = rd.curva_abc(conn, data)
    conn.close()
    if request.args.get("exportar") in ("excel", "pdf"):
        linhas = [[i["classe"], i["produto"], i["un"], i["qtd"], i["custo_unit"], i["valor"], i["pct"], i["pct_acum"]]
                  for i in dados["itens"]]
        return _rel_excel(f"Curva ABC a {dmy_(data)}", ["Classe", "Produto", "Un.", "Qtd", "Custo unit.", "Valor",
                                                       "% do estoque", "% acumulada"], linhas, "curva_abc")
    return render_template("rel_curva_abc.html", d=dados, args=request.args, **_rel_contexto())


@app.route("/auditorias/<int:id_auditoria>/divergencias")
@login_required
def rel_divergencias(id_auditoria):
    conn = get_conn()
    dados = rd.divergencias_inventario(conn, id_auditoria)
    conn.close()
    if not dados:
        abort(404)
    if request.args.get("exportar") in ("excel", "pdf"):
        linhas = [[l["produto"], l["un"], l["sistema"], l["contagem"], l["dif"], l["custo_unit"], l["dif_v"],
                   l["motivo"], l["estado"]] for l in dados["linhas"]]
        return _rel_excel(f"Inventário físico: divergências #{id_auditoria}",
                          ["Produto", "Un.", "Sistema", "Contagem", "Diferença", "Custo unit.", "Diferença em MT",
                           "Motivo", "Estado"], linhas, f"divergencias_{id_auditoria}")
    return render_template("rel_divergencias.html", d=dados, **_rel_contexto())


# ---------------------------------------------------------------------------
# Balancetes
# ---------------------------------------------------------------------------
@app.route("/balancetes")
@login_required
def balancetes():
    conn = get_conn()
    tipo = request.args.get("tipo", "mensal")
    data_ini = request.args.get("data_ini", "")
    data_fim = request.args.get("data_fim", "")
    id_categoria = request.args.get("categoria_id", type=int)
    id_produto = request.args.get("id_produto", type=int)
    exportar = request.args.get("exportar", "")

    hoje = datetime.now().date()
    if tipo == "mensal" and not data_ini:
        data_ini = hoje.replace(day=1).isoformat()
    elif tipo == "trimestral" and not data_ini:
        q = (hoje.month - 1) // 3
        data_ini = hoje.replace(month=q * 3 + 1, day=1).isoformat()
    elif tipo == "anual" and not data_ini:
        data_ini = hoje.replace(month=1, day=1).isoformat()
    if not data_fim:
        data_fim = hoje.isoformat()

    linhas = []
    totais = {}
    colunas = []
    titulo = ""
    subtitulo = ""
    saldo_acum_final = 0
    entradas_acum_final = 0
    saidas_acum_final = 0
    categoria_nome = ""
    extrato_produto_info = None
    extrato_produto_resumo = None

    todos_produtos = conn.execute(
        "SELECT id_produto, codigo, nome FROM produtos WHERE ativo=1 ORDER BY nome"
    ).fetchall()

    if tipo == "extrato_cat":
        if id_categoria:
            linhas_ext, saldo_acum_final, entradas_acum_final, saidas_acum_final = (
                services.calcular_extrato_categoria(conn, id_categoria, data_ini, data_fim)
            )
            linhas = linhas_ext
            cat = conn.execute(
                "SELECT nome FROM categorias WHERE id_categoria=?", (id_categoria,)
            ).fetchone()
            categoria_nome = cat["nome"] if cat else "—"
            titulo = f"Extrato de Categoria — {categoria_nome}"
            colunas = ["Data/Hora", "Tipo", "Código", "Produto", "Qtd.", "Unid.",
                       "Preço Unit.", "Valor Total", "Saldo Acumulado"]
            subtitulo = (
                f"Período: {data_ini} a {data_fim}  |  "
                f"Total Entradas: {entradas_acum_final:.2f}  |  "
                f"Total Saídas: {saidas_acum_final:.2f}  |  "
                f"Entradas − Saídas: {entradas_acum_final - saidas_acum_final:.2f}"
            )
            totais = {
                "subtotais": {
                    # quantidades de unidades diferentes (kg, cx, un) não se somam
                    "qtd_entradas": None,
                    "qtd_saidas": None,
                    "qtd_saldo": None,
                    "valor_entradas": services.round5(
                        sum(r["valor_total"] for r in linhas_ext if r["tipo"] == "Entrada")),
                    "valor_saidas": services.round5(
                        sum(r["valor_total"] for r in linhas_ext if r["tipo"] == "Saida")),
                    "valor_saldo": services.round5(
                        sum(r["valor_total"] for r in linhas_ext if r["tipo"] == "Entrada")
                        - sum(r["valor_total"] for r in linhas_ext if r["tipo"] == "Saida")),
                },
            }

    elif tipo == "extrato_prod":
        if id_produto:
            produto_ext, linhas_ext, resumo_ext = services.calcular_extrato_produto(
                conn, id_produto, data_ini, data_fim
            )
            if produto_ext:
                linhas = linhas_ext
                extrato_produto_info = produto_ext
                extrato_produto_resumo = resumo_ext
                titulo = f"Extrato de Produto — {produto_ext['nome']}"
                colunas = ["Data/Hora", "Tipo", "Doc.",
                           "Qtd. Orig.", "Unid.", "Qtd. Base",
                           "Preço Unit.", "Valor Total", "Lucro",
                           "Saldo Acum. (un.)", "Forn./Cli."]
                subtitulo = (
                    f"Período: {data_ini} a {data_fim}  |  "
                    f"Comprado: {resumo_ext['qtd_comprada']} {produto_ext['unidade']} "
                    f"({services.round5(resumo_ext['valor_comprado']):.2f})  |  "
                    f"Vendido: {resumo_ext['qtd_vendida']} {produto_ext['unidade']} "
                    f"({services.round5(resumo_ext['valor_vendido']):.2f})  |  "
                    f"Saldo Final: {resumo_ext['saldo_final']} (unidade base)"
                )
                totais = {
                    "subtotais": {
                        "qtd_entradas": resumo_ext["qtd_comprada"],
                        "qtd_saidas": resumo_ext["qtd_vendida"],
                        "qtd_saldo": resumo_ext["saldo_final"],
                        "valor_entradas": resumo_ext["valor_comprado"],
                        "valor_saidas": resumo_ext["valor_vendido"],
                        "valor_saldo": resumo_ext["lucro_periodo"],
                    },
                }

    elif tipo == "extrato_geral":
        titulo = "Extrato Geral de Produtos"
        colunas = ["Código", "Produto", "Categoria",
                   "Saldo Inicial (Qtd)",
                   "Entradas (Qtd)", "Entradas (Valor)",
                   "Saídas (Qtd)", "Saídas (Valor)",
                   "Saldo Final (Qtd)", "Lucro Bruto"]
        linhas, totais = services.calcular_balancete(
            conn, "extrato_geral", data_ini, data_fim, id_categoria, id_produto)
        subtitulo = (
            f"Período: {data_ini} a {data_fim}  |  "
            f"{len(linhas)} produto(s)"
        )

    else:
        periodo_label = {"mensal": "Mensal",
                         "trimestral": "Trimestral",
                         "anual": "Anual"}.get(tipo, "Personalizado")
        titulo = f"Balancete {periodo_label}"
        colunas = ["Código", "Produto", "Categoria",
                   "Saldo Inicial (Qtd)",
                   "Entradas (Qtd)", "Entradas (Valor)",
                   "Saídas (Qtd)", "Saídas (Valor)",
                   "Saldo Final (Qtd)", "Lucro Bruto"]
        linhas, totais = services.calcular_balancete(
            conn, tipo, data_ini, data_fim, id_categoria, id_produto)
        subtitulo = f"Período: {data_ini} a {data_fim}"

    categorias = conn.execute(
        "SELECT * FROM categorias WHERE ativo=1 ORDER BY nome"
    ).fetchall()

    if exportar in ("pdf", "excel"):
        out_colunas = colunas
        out_linhas = []

        def _fmt_qtd_local(v, un):
            if (un or "").lower() == "kg":
                return f"{float(v or 0):.3f}".replace(".", ",")
            return f"{int(round(float(v or 0)))}"

        if tipo == "extrato_cat":
            for r in linhas:
                out_linhas.append([
                    r["data_hora"], r["tipo"], r["codigo"], r["produto"],
                    r["quantidade"], r["unidade"],
                    r["preco_unitario"], r["valor_total"], r["saldo_acumulado"],
                ])
        elif tipo == "extrato_prod":
            for r in linhas:
                out_linhas.append([
                    r["data_hora"], r["tipo"], r["documento"],
                    _fmt_qtd_local(r["quantidade_original"], r["unidade_original"]),
                    r["unidade_original"],
                    _fmt_qtd_local(r["quantidade_base"], r["unidade_original"]),
                    r["preco_unitario"], r["valor_total"],
                    r["lucro"],
                    _fmt_qtd_local(r["saldo_acumulado"], r["unidade_original"]),
                    r["terceiro"],
                ])
        else:
            for r in linhas:
                out_linhas.append([
                    r["codigo"], r["produto"], r["categoria"],
                    r["saldo_inicial"], r["entradas_qtd"], r["entradas_val"],
                    r["saidas_qtd"], r["saidas_val"], r["saldo_final"], r["lucro"],
                ])
        conn.close()
        if exportar == "pdf":
            buf = reportmod.gerar_pdf(titulo, out_colunas, out_linhas, subtitulo=subtitulo)
            return send_file(buf, mimetype="application/pdf", as_attachment=True,
                             download_name=f"{tipo}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        else:
            buf = reportmod.gerar_excel(titulo, out_colunas, out_linhas)
            return send_file(buf,
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True,
                             download_name=f"{tipo}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")

    conn.close()
    return render_template(
        "balancetes.html",
        tipo=tipo, data_ini=data_ini, data_fim=data_fim,
        titulo=titulo, colunas=colunas, linhas=linhas,
        totais=totais, subtitulo=subtitulo,
        categorias=categorias,
        categoria_nome=categoria_nome,
        todos_produtos=todos_produtos,
        id_produto=id_produto,          
        categoria_id=id_categoria,      
        extrato_produto=extrato_produto_info,
        extrato_resumo=extrato_produto_resumo,
        filtros={"categoria_id": id_categoria, "id_produto": id_produto},
        extrato_sum={"saldo": saldo_acum_final,
                     "entradas": entradas_acum_final,
                     "saidas": saidas_acum_final},
    )


@app.route("/balancetes/imprimir-branco")
@login_required
def balancetes_imprimir_branco():
    conn = get_conn()
    id_categoria = request.args.get("categoria_id", type=int)
    id_produto = request.args.get("id_produto", type=int)

    sql = """SELECT p.codigo, p.nome, p.unidade, c.nome AS categoria_nome
             FROM produtos p
             LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
             WHERE p.ativo=1"""
    params = []
    if id_categoria:
        sql += " AND p.id_categoria=?"
        params.append(id_categoria)
    if id_produto:
        sql += " AND p.id_produto=?"
        params.append(id_produto)
    sql += " ORDER BY p.nome"

    produtos = conn.execute(sql, params).fetchall()
    conn.close()

    colunas = ["Produto", "Quantidade Contada", "Data"]
    linhas = []
    for p in produtos:
        rotulo = f"{p['nome']}  ({p['codigo']})"
        linhas.append([rotulo, "", ""])

    sub = (f"Contagem física manual  |  {len(linhas)} produto(s)  |  "
           f"Emitido em {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    buf = reportmod.gerar_pdf(
        "Folha de Contagem — Preenchimento Manual",
        colunas, linhas, subtitulo=sub, landscape_mode=False,
    )
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"folha_contagem_{datetime.now():%Y%m%d_%H%M%S}.pdf")


@app.route("/relatorios/precos")
@login_required
def relatorio_precos():
    conn = get_conn()
    exportar = request.args.get("exportar", "")
    colunas, linhas = reportmod.gerar_relatorio_precos(conn)
    if exportar == "pdf":
        buf = reportmod.gerar_pdf("Lista de Preços de Venda", colunas, linhas)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name="precos_venda.pdf")
    elif exportar == "excel":
        buf = reportmod.gerar_excel("Lista de Preços de Venda", colunas, linhas)
        return send_file(buf,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name="precos_venda.xlsx")
    conn.close()
    return render_template("relatorio_precos.html", colunas=colunas, linhas=linhas)


# ---------------------------------------------------------------------------
# Cotações
# ---------------------------------------------------------------------------
@app.route("/cotacoes")
@login_required
def cotacoes_list():
    conn = get_conn()
    status = request.args.get("status", "")
    sql = "SELECT * FROM cotacoes"
    params = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY data_emissao DESC"
    cotacoes = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template("cotacoes.html", cotacoes=cotacoes, status_filtro=status)


def _itens_cotacao_do_form():
    """Lê os itens do formulário de cotação (nova ou edição).
    Devolve (itens, erro). Linhas totalmente vazias são ignoradas."""
    ids = request.form.getlist("id_produto[]")
    qtds = request.form.getlist("quantidade[]")
    precos = request.form.getlist("preco_unitario[]")
    descontos = request.form.getlist("desconto[]")

    def _na(lista, i):
        return lista[i].strip() if i < len(lista) and lista[i] is not None else ""

    itens = []
    for i in range(len(ids)):
        id_txt, qtd_txt, preco_txt = ids[i].strip(), _na(qtds, i), _na(precos, i)
        if not (id_txt or qtd_txt or preco_txt):
            continue                                   # linha em branco
        n = len(itens) + 1
        if not (id_txt and qtd_txt and preco_txt):
            return None, f"Item {n}: escolha o produto e preencha quantidade e preço."
        try:
            id_produto = int(id_txt)
        except ValueError:
            return None, f"Item {n}: produto inválido."
        qtd = safe_parse_float(qtd_txt, None, milhares=False)
        preco = safe_parse_float(preco_txt, None)
        desconto = safe_parse_float(_na(descontos, i), 0.0, milhares=False)
        if qtd is None or qtd <= 0:
            return None, f"Item {n}: quantidade inválida."
        if preco is None or preco < 0:
            return None, f"Item {n}: preço inválido."
        if desconto is None or not 0 <= desconto <= 100:
            return None, f"Item {n}: o desconto deve estar entre 0 e 100%."
        itens.append({"id_produto": id_produto, "quantidade": qtd,
                      "preco_unitario": preco, "desconto_percentual": desconto})
    if not itens:
        return None, "Adicione pelo menos um item."
    return itens, None


@app.route("/cotacoes/nova", methods=["GET", "POST"])
@perfil_required("Admin", "Gerente", "Operador")
def cotacao_nova():
    if request.method == "POST":
        cliente_nome = request.form.get("cliente_nome", "").strip()
        if not cliente_nome:
            flash("Nome do cliente é obrigatório.", "erro")
            return redirect(url_for("cotacao_nova"))
        itens, erro = _itens_cotacao_do_form()
        if erro:
            flash(erro, "erro")
            return redirect(url_for("cotacao_nova"))
        conn = get_conn()
        try:
            id_cotacao = services.criar_cotacao(
                conn,
                id_usuario=session["user_id"],
                cliente_nome=cliente_nome,
                cliente_email=request.form.get("cliente_email") or None,
                cliente_telefone=request.form.get("cliente_telefone") or None,
                data_validade=request.form.get("data_validade") or None,
                observacoes=request.form.get("observacoes") or None,
                itens=itens,
            )
            flash("Cotação criada com sucesso.", "sucesso")
            return redirect(url_for("cotacao_detalhe", id_cotacao=id_cotacao))
        except services.RegraNegocioError as e:
            flash(str(e), "erro")
        except Exception as e:
            flash(f"Erro ao criar cotação: {e}", "erro")
        finally:
            conn.close()
        return redirect(url_for("cotacao_nova"))

    conn = get_conn()
    produtos = conn.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    validade_default = (datetime.now() + timedelta(days=30)).date().isoformat()
    return render_template("cotacao_form.html", produtos=produtos, validade_default=validade_default)


@app.route("/cotacoes/<int:id_cotacao>/editar", methods=["GET"])
@perfil_required("Admin", "Gerente", "Operador")
def cotacao_editar(id_cotacao):
    conn = get_conn()
    res = services.obter_cotacao(conn, id_cotacao)
    if not res:
        conn.close()
        abort(404)
    cotacao, itens = res
    # activos + os que já constam na cotação (mesmo que entretanto desactivados)
    produtos = conn.execute(
        "SELECT * FROM produtos WHERE ativo=1 OR id_produto IN "
        "(SELECT id_produto FROM itens_cotacao WHERE id_cotacao=?) ORDER BY nome",
        (id_cotacao,)).fetchall()
    conn.close()
    if cotacao["status"] != "Aberta":
        flash("Apenas cotações com status 'Aberta' podem ser editadas.", "erro")
        return redirect(url_for("cotacao_detalhe", id_cotacao=id_cotacao))
    return render_template("cotacao_form.html", produtos=produtos, cotacao=cotacao,
                           itens=itens, validade_default=cotacao["data_validade"][:10])


@app.route("/cotacoes/<int:id_cotacao>/atualizar", methods=["POST"])
@perfil_required("Admin", "Gerente", "Operador")
def cotacao_atualizar(id_cotacao):
    cliente_nome = request.form.get("cliente_nome", "").strip()
    if not cliente_nome:
        flash("Nome do cliente é obrigatório.", "erro")
        return redirect(url_for("cotacao_editar", id_cotacao=id_cotacao))

    itens, erro = _itens_cotacao_do_form()
    if erro:
        flash(erro, "erro")
        return redirect(url_for("cotacao_editar", id_cotacao=id_cotacao))

    conn = get_conn()
    try:
        services.atualizar_cotacao(
            conn, id_cotacao=id_cotacao, id_usuario=session["user_id"],
            cliente_nome=cliente_nome,
            cliente_email=request.form.get("cliente_email") or None,
            cliente_telefone=request.form.get("cliente_telefone") or None,
            data_validade=request.form.get("data_validade") or None,
            observacoes=request.form.get("observacoes") or None,
            itens=itens,
        )
        flash("Cotação atualizada com sucesso.", "sucesso")
        conn.close()
        return redirect(url_for("cotacao_detalhe", id_cotacao=id_cotacao))
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    except Exception as e:
        flash(f"Erro ao atualizar cotação: {e}", "erro")
    conn.close()
    return redirect(url_for("cotacao_editar", id_cotacao=id_cotacao))


@app.route("/cotacoes/<int:id_cotacao>")
@login_required
def cotacao_detalhe(id_cotacao):
    conn = get_conn()
    res = services.obter_cotacao(conn, id_cotacao)
    conn.close()
    if not res:
        abort(404)
    cotacao, itens = res
    return render_template("cotacao_detalhe.html", cotacao=cotacao, itens=itens)


@app.route("/cotacoes/<int:id_cotacao>/exportar/<formato>")
@login_required
def cotacao_exportar(id_cotacao, formato):
    conn = get_conn()
    res = services.obter_cotacao(conn, id_cotacao)
    conn.close()
    if not res:
        abort(404)
    cotacao, itens = res
    if formato == "pdf":
        buf = reportmod.gerar_pdf_cotacao(cotacao, itens)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"cotacao_{cotacao['numero']}.pdf")
    elif formato == "excel":
        colunas = ["Código", "Produto", "Quantidade", "Preço Unit.", "Desc. %", "Subtotal"]
        linhas = []
        for item in itens:
            linhas.append([
                item["codigo"], item["produto_nome"],
                services._qtd_cotacao(item["quantidade"]),
                services.arredondar_moeda(item["preco_unitario"]),
                f"{item['desconto_percentual']:.0f}%",
                services.arredondar_moeda(item["valor_total"]),
            ])
        buf = reportmod.gerar_excel(f"Cotação {cotacao['numero']}", colunas, linhas)
        return send_file(buf,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"cotacao_{cotacao['numero']}.xlsx")
    abort(404)


@app.route("/cotacoes/<int:id_cotacao>/aprovar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def cotacao_aprovar(id_cotacao):
    conn = get_conn()
    try:
        services.atualizar_status_cotacao(conn, id_cotacao, "Aprovada", session["user_id"])
        flash("Cotação aprovada.", "sucesso")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(url_for("cotacao_detalhe", id_cotacao=id_cotacao))


@app.route("/cotacoes/<int:id_cotacao>/rejeitar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def cotacao_rejeitar(id_cotacao):
    conn = get_conn()
    try:
        services.atualizar_status_cotacao(conn, id_cotacao, "Rejeitada", session["user_id"])
        flash("Cotação rejeitada.", "info")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(url_for("cotacao_detalhe", id_cotacao=id_cotacao))


@app.route("/cotacoes/<int:id_cotacao>/converter", methods=["POST"])
@perfil_required("Admin", "Gerente")
def cotacao_converter(id_cotacao):
    conn = get_conn()
    try:
        services.converter_cotacao_em_venda(conn, id_cotacao, session["user_id"])
        flash("Cotação convertida em venda com sucesso.", "sucesso")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(url_for("cotacao_detalhe", id_cotacao=id_cotacao))


@app.route("/cotacoes/<int:id_cotacao>/faturar", methods=["POST"])
@perfil_required("Admin", "Gerente", "Operador")
def cotacao_faturar(id_cotacao):
    conn = get_conn()
    try:
        data_venc = request.form.get("data_vencimento") or None
        isento = request.form.get("isento_iva") == "1"
        taxa_txt = (request.form.get("taxa_iva") or "").strip()
        taxa = safe_parse_float(taxa_txt, None, milhares=False) if taxa_txt else None
        res = services.faturar_cotacao(
            conn, id_cotacao, session["user_id"],
            ip_address=_client_ip(),
            data_vencimento=data_venc,
            isento_iva=isento,
            taxa_iva=taxa,
        )
        flash(f"Fatura {res['numero_fatura']} emitida a partir da cotação.", "sucesso")
        conn.close()
        return redirect(url_for("conta_receber_detalhe", id_conta=res["id_conta"]))
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    except Exception as e:
        flash(f"Erro: {e}", "erro")
    conn.close()
    return redirect(url_for("cotacao_detalhe", id_cotacao=id_cotacao))


# ---------------------------------------------------------------------------
# Vendas a Crédito / Contas a Receber / Recibos
# ---------------------------------------------------------------------------
@app.route("/vendas/credito", methods=["GET", "POST"])
@perfil_required("Admin", "Gerente", "Operador")
def venda_credito():
    conn = get_conn()
    if request.method == "POST":
        json_mode = _pedido_json()
        acao = request.form.get("acao", "gravar")
        token = request.form.get("token_envio") or None
        nome_cliente = request.form.get("cliente", "").strip()

        ids_produto = request.form.getlist("id_produto[]")
        quantidades = request.form.getlist("quantidade[]")
        unidades = request.form.getlist("unidade_original[]")
        precos = request.form.getlist("preco_unitario[]")

        # Validação item a item: nunca descartar em silêncio.
        erros_parse = []
        itens = []
        if not nome_cliente:
            erros_parse.append(_erro_linha(None, "cliente", "Nome do cliente é obrigatório."))
        for i, id_str in enumerate(ids_produto):
            if not id_str:
                continue
            linha = i + 1
            try:
                id_prod = int(id_str)
            except (TypeError, ValueError):
                erros_parse.append(_erro_linha(linha, "produto", "Produto inválido."))
                continue

            qtd_txt = quantidades[i] if i < len(quantidades) else None
            preco_txt = precos[i] if i < len(precos) else None
            unidade = unidades[i] if i < len(unidades) else "Unidade"

            quantidade = safe_parse_float(qtd_txt, None, milhares=False)
            preco = safe_parse_float(preco_txt, None)

            if quantidade is None or quantidade <= 0:
                erros_parse.append(_erro_linha(linha, "quantidade", "Quantidade inválida (use 10 ou 10,5)."))
                continue
            if preco is None or preco < 0:
                erros_parse.append(_erro_linha(linha, "preco", "Preço inválido (use 12,50 ou 12.50)."))
                continue

            itens.append({
                "linha": linha,
                "id_produto": id_prod,
                "quantidade": quantidade,
                "unidade": unidade,
                "preco_unitario": preco,
            })

        if not itens and not any(e["campo"] in ("produto", "quantidade", "preco") for e in erros_parse):
            erros_parse.append(_erro_linha(None, "itens", "Adicione pelo menos um produto à fatura."))

        erros_val, avisos = services.validar_itens_venda(conn, itens)
        erros = sorted(erros_parse + erros_val,
                       key=lambda e: (e.get("linha") is None, e.get("linha") or 0))

        def _falhar(lista_erros, tentativa=True):
            if tentativa:
                services.registrar_tentativa_falhada(conn, session["user_id"], "venda_credito",
                                                     lista_erros, _client_ip())
            if json_mode:
                conn.close()
                return jsonify({"ok": False, "estado": "erros", "erros": lista_erros, "avisos": avisos})
            for e in lista_erros:
                flash(_texto_erro(e), "erro")
            conn.close()
            return redirect(url_for("venda_credito"))

        if acao == "validar" and json_mode:
            conn.close()
            return jsonify({"ok": not erros, "estado": "erros" if erros else "validada",
                            "erros": erros, "avisos": avisos})
        if erros:
            return _falhar(erros)
        if avisos and json_mode and request.form.get("confirmar_avisos") != "1":
            conn.close()
            return jsonify({"ok": False, "estado": "confirmacao", "avisos": avisos, "erros": []})

        data_venda = request.form.get("data_venda") or ""
        data_venda_iso = (data_venda + "T" + datetime.now().strftime("%H:%M:%S")) if data_venda else None
        data_venc = request.form.get("data_vencimento") or None
        isento = request.form.get("isento_iva") == "1"
        taxa_txt = (request.form.get("taxa_iva") or "").strip()
        taxa = safe_parse_float(taxa_txt, None, milhares=False) if taxa_txt else None

        if token and not _reservar_token_envio(token):
            msg = "Esta fatura já foi registada (envio repetido ignorado)."
            if json_mode:
                conn.close()
                return jsonify({"ok": False, "estado": "duplicado", "mensagem": msg,
                                "redirect": url_for("contas_receber_list")})
            flash(msg, "aviso")
            conn.close()
            return redirect(url_for("contas_receber_list"))

        # --- 1) Criar a fatura ---
        try:
            resultado = services.registrar_venda_credito(
                conn,
                itens=itens,
                id_usuario=session["user_id"],
                nome_cliente=nome_cliente,
                nif_cliente=request.form.get("cliente_nif") or None,
                endereco_cliente=request.form.get("cliente_endereco") or None,
                localidade_cliente=request.form.get("cliente_localidade") or None,
                pais_cliente=request.form.get("cliente_pais") or None,
                email_cliente=request.form.get("cliente_email") or None,
                telefone_cliente=request.form.get("cliente_telefone") or None,
                data_vencimento=data_venc,
                condicao_pagamento=request.form.get("condicao_pagamento") or None,
                isento_iva=isento,
                taxa_iva=taxa,
                observacoes=request.form.get("observacoes") or None,
                data_venda=data_venda_iso,
                ip_address=_client_ip(),
            )
        except services.RegraNegocioError as e:
            _devolver_token_envio(token)
            return _falhar([_erro_linha(None, "itens", str(e))])
        except Exception as e:
            _devolver_token_envio(token)
            logging.exception("Erro ao criar fatura")
            return _falhar([_erro_linha(None, "itens", f"Erro ao criar fatura: {e}")])

        # --- 2) Sucesso: fatura já está commitada ---
        flash(
            f"Fatura {resultado['numero_fatura']} criada com "
            f"{resultado['n_itens']} produto(s) — total {resultado['valor']:.2f} "
            f"{get_config('moeda', 'MZN')}.",
            "sucesso"
        )

        # --- 3) 2.5: avisar margens negativas (cosmético, não bloqueia) ---
        try:
            criticos = conn.execute(
                """SELECT p.nome, m.lucro
                   FROM movimentos m
                   JOIN produtos p ON p.id_produto = m.id_produto
                   WHERE m.tipo = 'Saida'
                     AND m.id_movimento IN (
                         SELECT id_movimento FROM contas_receber_itens
                         WHERE id_conta = ?
                     )
                     AND m.lucro < 0""",
                (resultado["id_conta"],),
            ).fetchall()

            if criticos:
                nomes = ", ".join(
                    f"{c['nome']} ({float(c['lucro']):+.2f})"
                    for c in criticos[:3]
                )
                flash(
                    f"⚠️ {len(criticos)} item(ns) desta fatura ficaram com "
                    f"MARGEM NEGATIVA: {nomes}. Verifique os preços de venda "
                    f"para a unidade praticada.",
                    "aviso",
                )
        except Exception as e:
            logging.warning(f"Falha ao verificar margens da fatura: {e}")

        conn.close()
        destino = url_for("conta_receber_detalhe", id_conta=resultado["id_conta"])
        if json_mode:
            return jsonify({"ok": True, "estado": "ok", "redirect": destino})
        return redirect(destino)

    # GET: mostrar formulário
    produtos = conn.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("venda_credito_form.html", produtos=produtos,
                           token_envio=_emitir_token_envio())

@app.route("/contas-receber")
@login_required
def contas_receber_list():
    conn = get_conn()
    status = request.args.get("status", "")
    sql = "SELECT * FROM contas_receber"
    params = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY data_emissao DESC"
    contas = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template("contas_receber.html", contas=contas, status_filtro=status)


@app.route("/contas-receber/<int:id_conta>")
@login_required
def conta_receber_detalhe(id_conta):
    conn = get_conn()
    res = services.obter_conta_receber(conn, id_conta)
    conn.close()
    if not res:
        abort(404)
    conta, itens, recibos = res
    return render_template("conta_receber_detalhe.html",
                           conta=conta, itens=itens, recibos=recibos)


@app.route("/contas-receber/<int:id_conta>/liquidar", methods=["POST"])
@perfil_required("Admin", "Gerente", "Operador")
def conta_receber_liquidar(id_conta):
    conn = get_conn()
    try:
        # FIX: aceita vírgula decimal em vez de rebentar com ValueError.
        valor = safe_parse_float(request.form.get("valor"), 0)
        if valor is None or valor <= 0:
            flash("Valor de pagamento inválido.", "erro")
            conn.close()
            return redirect(url_for("conta_receber_detalhe", id_conta=id_conta))
        forma = request.form.get("forma_pagamento") or "Dinheiro"
        data_pg = request.form.get("data_pagamento") or ""
        data_pg_iso = (data_pg + "T" + datetime.now().strftime("%H:%M:%S")) if data_pg else None
        res = services.liquidar_conta(
            conn, id_conta, valor, forma, session["user_id"],
            data_pagamento=data_pg_iso,
            observacoes=request.form.get("observacoes") or None,
            ip_address=_client_ip(),
        )
        flash(f"Recibo {res['numero']} emitido. Saldo restante: {res['novo_saldo']:.2f}.", "sucesso")
        conn.close()
        return redirect(url_for("conta_receber_detalhe", id_conta=id_conta))
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    except Exception as e:
        flash(f"Erro: {e}", "erro")
    conn.close()
    return redirect(url_for("conta_receber_detalhe", id_conta=id_conta))


@app.route("/vendas/anular", methods=["GET", "POST"])
@login_required
def venda_anular():
    """Anula uma venda pelo código (V-000123 ou nº da fatura) e repõe o estoque."""
    conn = get_conn()
    codigo = (request.values.get("codigo") or "").strip()
    if request.method == "POST":
        try:
            res = services.anular_venda(conn, codigo, request.form.get("motivo"),
                                        session["user_id"], session.get("perfil"), _client_ip())
            flash(f"Venda {res['codigo']} anulada ({_filtro_mt(res['valor'])} MT). "
                  f"O estoque de {res['itens']} item(ns) foi reposto.", "sucesso")
            conn.close()
            return redirect(url_for("venda_anular"))
        except services.RegraNegocioError as e:
            flash(str(e), "erro")
    venda = services.obter_venda_por_codigo(conn, codigo) if codigo else None
    if codigo and not venda and request.method == "GET":
        flash(f"Não existe nenhuma venda com o código '{codigo}'.", "erro")
    recentes = conn.execute(
        """SELECT codigo_venda, MIN(data_hora) AS data_hora, MAX(cliente) AS cliente,
                  MAX(forma_pagamento) AS forma, SUM(valor_total) AS total, COUNT(*) AS n
           FROM movimentos WHERE tipo='Saida' AND codigo_venda IS NOT NULL
           GROUP BY codigo_venda ORDER BY MIN(data_hora) DESC, codigo_venda DESC LIMIT 15""").fetchall()
    anuladas = conn.execute(
        """SELECT v.*, u.nome AS anulou FROM vendas_anuladas v
           LEFT JOIN usuarios u ON u.id_usuario=v.id_usuario_anulou
           ORDER BY v.id_anulacao DESC LIMIT 15""").fetchall()
    conn.close()
    return render_template("venda_anular.html", codigo=codigo, venda=venda,
                           recentes=recentes, anuladas=anuladas)


@app.route("/contas-receber/<int:id_conta>/eliminar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def conta_receber_eliminar(id_conta):
    conn = get_conn()
    try:
        res = services.cancelar_conta_receber(conn, id_conta, session["user_id"], _client_ip())
        flash(
            f"Fatura eliminada. {res['movimentos_revertidos']} movimento(s) revertido(s) "
            f"e o stock foi reposto.",
            "sucesso"
        )
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    except Exception as e:
        flash(f"Erro: {e}", "erro")
    conn.close()
    return redirect(url_for("contas_receber_list"))


@app.route("/contas-receber/<int:id_conta>/fatura.pdf")
@login_required
def conta_receber_fatura_pdf(id_conta):
    conn = get_conn()
    res = services.obter_conta_receber(conn, id_conta)
    conn.close()
    if not res:
        abort(404)
    conta, itens, _recibos = res
    buf = reportmod.gerar_pdf_fatura(conta, itens=itens)
    nome_arq = (conta.get("numero_fatura") or f"FT-{id_conta:05d}").replace(" ", "_").replace("/", "-")
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{nome_arq}.pdf")

@app.route("/contas-receber/extrato-clientes")
@login_required
def contas_receber_extrato_clientes():
    """Extrato agregado de faturas a crédito por cliente."""
    conn = get_conn()
    filtro_nome = request.args.get("cliente", "").strip()
    exportar = request.args.get("exportar", "")

    sql = """
        SELECT nome_cliente,
               COUNT(*) AS n_faturas,
               COALESCE(SUM(valor_total), 0) AS total,
               COALESCE(SUM(valor_pago), 0) AS pago,
               COALESCE(SUM(saldo), 0) AS saldo,
               SUM(CASE WHEN status = 'Aberta'    THEN 1 ELSE 0 END) AS n_abertas,
               SUM(CASE WHEN status = 'Parcial'   THEN 1 ELSE 0 END) AS n_parciais,
               SUM(CASE WHEN status = 'Liquidada' THEN 1 ELSE 0 END) AS n_liquidadas,
               MAX(data_emissao) AS ultima_emissao
        FROM contas_receber
        WHERE status != 'Cancelada'
    """
    params = []
    if filtro_nome:
        sql += " AND nome_cliente LIKE ?"
        params.append(f"%{filtro_nome}%")
    sql += " GROUP BY nome_cliente ORDER BY saldo DESC, nome_cliente ASC"

    clientes = conn.execute(sql, params).fetchall()

    totais = conn.execute("""
        SELECT COUNT(*) AS n_faturas,
               COUNT(DISTINCT nome_cliente) AS n_clientes,
               COALESCE(SUM(valor_total), 0) AS total,
               COALESCE(SUM(valor_pago), 0) AS pago,
               COALESCE(SUM(saldo), 0) AS saldo
        FROM contas_receber
        WHERE status != 'Cancelada'
    """).fetchone()

    if exportar in ("pdf", "excel"):
        colunas = ["Cliente", "Nº Faturas", "Total", "Pago", "Saldo em Dívida",
                   "Abertas", "Parciais", "Pagas", "Última Fatura"]
        linhas = []
        for c in clientes:
            linhas.append([
                c["nome_cliente"],
                int(c["n_faturas"] or 0),
                float(c["total"] or 0),
                float(c["pago"] or 0),
                float(c["saldo"] or 0),
                int(c["n_abertas"] or 0),
                int(c["n_parciais"] or 0),
                int(c["n_liquidadas"] or 0),
                (c["ultima_emissao"] or "—")[:10],
            ])
        linhas.append([
            "TOTAL",
            int(totais["n_faturas"] or 0),
            float(totais["total"] or 0),
            float(totais["pago"] or 0),
            float(totais["saldo"] or 0),
            "", "", "", "",
        ])
        sub = f"Filtro: {filtro_nome or 'Todos os clientes'}  |  " \
              f"{totais['n_clientes']} cliente(s)  |  {totais['n_faturas']} fatura(s)"
        conn.close()
        if exportar == "pdf":
            buf = reportmod.gerar_pdf("Extrato de Faturas a Crédito por Cliente",
                                      colunas, linhas, subtitulo=sub)
            return send_file(buf, mimetype="application/pdf", as_attachment=True,
                             download_name=f"extrato_clientes_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        else:
            buf = reportmod.gerar_excel("Extrato de Faturas a Crédito por Cliente",
                                        colunas, linhas)
            return send_file(buf,
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True,
                             download_name=f"extrato_clientes_{datetime.now():%Y%m%d_%H%M%S}.xlsx")

    conn.close()
    return render_template("contas_receber_extrato.html",
                           clientes=clientes, totais=totais,
                           filtro_nome=filtro_nome)


@app.route("/contas-receber/extrato-clientes/<path:nome_cliente>")
@login_required
def conta_receber_extrato_cliente(nome_cliente):
    """Extrato detalhado de todas as faturas de um cliente."""
    conn = get_conn()
    exportar = request.args.get("exportar", "")

    faturas = conn.execute("""
        SELECT * FROM contas_receber
        WHERE nome_cliente = ? AND status != 'Cancelada'
        ORDER BY data_emissao DESC
    """, (nome_cliente,)).fetchall()

    if not faturas:
        conn.close()
        abort(404)

    total = sum(float(f["valor_total"] or 0) for f in faturas)
    pago = sum(float(f["valor_pago"] or 0) for f in faturas)
    saldo = sum(float(f["saldo"] or 0) for f in faturas)
    n_abertas = sum(1 for f in faturas if f["status"] == "Aberta")
    n_parciais = sum(1 for f in faturas if f["status"] == "Parcial")
    n_liquidadas = sum(1 for f in faturas if f["status"] == "Liquidada")

    if exportar in ("pdf", "excel"):
        colunas = ["Fatura", "Emissão", "Vencimento", "Status",
                   "Total", "Pago", "Saldo", "Cond. Pagamento"]
        linhas = []
        for f in faturas:
            linhas.append([
                f["numero_fatura"] or f"FT-{f['id_conta']:05d}",
                (f["data_emissao"] or "")[:10],
                (f["data_vencimento"] or "—")[:10] if f["data_vencimento"] else "—",
                f["status"],
                float(f["valor_total"] or 0),
                float(f["valor_pago"] or 0),
                float(f["saldo"] or 0),
                f["condicao_pagamento"] or "—",
            ])
        linhas.append(["TOTAL", "", "", "",
                       float(total), float(pago), float(saldo), ""])
        sub = f"Cliente: {nome_cliente}  |  {len(faturas)} fatura(s)"
        conn.close()
        nome_limpo = "".join(c if c.isalnum() else "_" for c in nome_cliente[:40])
        if exportar == "pdf":
            buf = reportmod.gerar_pdf(f"Extrato de Faturas — {nome_cliente}",
                                      colunas, linhas, subtitulo=sub)
            return send_file(buf, mimetype="application/pdf", as_attachment=True,
                             download_name=f"extrato_{nome_limpo}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        else:
            buf = reportmod.gerar_excel(f"Extrato de Faturas — {nome_cliente}",
                                        colunas, linhas)
            return send_file(buf,
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True,
                             download_name=f"extrato_{nome_limpo}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")

    dados = rd.extrato_cliente(conn, nome_cliente)
    conn.close()
    return render_template("rel_extrato_cliente.html", d=dados, **_rel_contexto())

@app.route("/recibos/<int:id_recibo>.pdf")
@login_required
def recibo_pdf(id_recibo):
    conn = get_conn()
    res = services.obter_recibo(conn, id_recibo)
    conn.close()
    if not res:
        abort(404)
    recibo, conta = res
    buf = reportmod.gerar_pdf_recibo(recibo, conta)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"recibo_{recibo['numero']}.pdf")


# ---------------------------------------------------------------------------
# AUDITORIA DE ESTOQUE
# ---------------------------------------------------------------------------
@app.route("/auditorias")
@login_required
def auditorias_list():
    conn = get_conn()
    auditorias = conn.execute(
        """SELECT a.*, u.nome AS responsavel_nome
           FROM auditorias a
           LEFT JOIN usuarios u ON u.id_usuario=a.responsavel_id
           ORDER BY a.data_abertura DESC"""
    ).fetchall()
    conn.close()
    return render_template("auditorias_list.html", auditorias=auditorias)


@app.route("/auditorias/nova", methods=["GET", "POST"])
@perfil_required("Admin", "Gerente")
def auditoria_nova():
    conn = get_conn()
    if request.method == "POST":
        try:
            id_aud = services.abrir_auditoria(
                conn,
                data_inicio=request.form["data_inicio"],
                data_fim=request.form["data_fim"],
                tipo=request.form.get("tipo", "completa"),
                id_categoria=request.form.get("id_categoria", type=int),
                responsavel_id=session["user_id"],
                tolerancia_percentual=safe_parse_float_required(
                    request.form.get("tolerancia_percentual"), 1.0),
                tolerancia_valor_minimo=safe_parse_float_required(
                    request.form.get("tolerancia_valor_minimo"), 0.0),
                observacoes=request.form.get("observacoes"),
                ip_address=_client_ip(),
            )
            flash("Auditoria aberta. Próximo passo: congelar o estoque teórico.", "sucesso")
            conn.close()
            return redirect(url_for("auditoria_detalhe", id_auditoria=id_aud))
        except Exception as e:
            flash(f"Erro: {e}", "erro")
    categorias = conn.execute("SELECT * FROM categorias WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("auditoria_form.html", categorias=categorias)


@app.route("/auditorias/<int:id_auditoria>")
@login_required
def auditoria_detalhe(id_auditoria):
    conn = get_conn()
    aud = services.obter_auditoria(conn, id_auditoria)
    if not aud:
        conn.close()
        abort(404)
    produtos = services.produtos_da_auditoria(conn, id_auditoria)
    divergencias = services.divergencias_da_auditoria(conn, id_auditoria)
    contagens = conn.execute(
        """SELECT c.*, p.codigo, p.nome, u.nome AS usuario_nome
           FROM auditoria_contagens c
           JOIN produtos p ON p.id_produto=c.id_produto
           LEFT JOIN usuarios u ON u.id_usuario=c.id_usuario_contador
           WHERE c.id_auditoria=?
           ORDER BY c.data_contagem DESC LIMIT 20""",
        (id_auditoria,),
    ).fetchall()
    conn.close()
    return render_template("auditoria_detalhe.html",
                           aud=aud, produtos=produtos,
                           divergencias=divergencias, contagens=contagens)


@app.route("/auditorias/<int:id_auditoria>/congelar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def auditoria_congelar(id_auditoria):
    conn = get_conn()
    try:
        services.congelar_estoque_teorico(conn, id_auditoria, _client_ip())
        flash("Estoque teórico congelado. Inicie a contagem.", "sucesso")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(url_for("auditoria_detalhe", id_auditoria=id_auditoria))


@app.route("/auditorias/<int:id_auditoria>/contar", methods=["POST"])
@perfil_required("Admin", "Gerente", "Operador")
def auditoria_contar(id_auditoria):
    conn = get_conn()
    try:
        id_produto = int(request.form["id_produto"])
        # FIX: aceita vírgula decimal.
        quantidade = safe_parse_float(request.form.get("quantidade"), None, milhares=False)
        if quantidade is None or quantidade <= 0:
            flash("Quantidade inválida.", "erro")
            conn.close()
            return redirect(url_for("auditoria_detalhe", id_auditoria=id_auditoria))
        services.registrar_contagem(
            conn, id_auditoria, id_produto, quantidade,
            id_usuario=session["user_id"],
            local=request.form.get("local"),
            lote=request.form.get("lote"),
            validade=request.form.get("validade"),
            observacao=request.form.get("observacao"),
            contagem_cega=request.form.get("contagem_cega") == "1",
            ip_address=_client_ip(),
        )
        flash("Contagem registada.", "sucesso")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    except Exception as e:
        flash(f"Erro: {e}", "erro")
    conn.close()
    return redirect(url_for("auditoria_detalhe", id_auditoria=id_auditoria))


@app.route("/auditorias/<int:id_auditoria>/fechar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def auditoria_fechar(id_auditoria):
    conn = get_conn()
    try:
        res = services.fechar_auditoria(conn, id_auditoria, session["user_id"], _client_ip())
        flash(
            f"Auditoria fechada. {res['ajustes_gerados']} ajuste(s). "
            f"Divergência: {res['div_qtd']} un. / {res['div_valor']:.2f} MZN.",
            "sucesso"
        )
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(url_for("auditoria_detalhe", id_auditoria=id_auditoria))


@app.route("/auditorias/<int:id_auditoria>/relatorio.pdf")
@login_required
def auditoria_relatorio_pdf(id_auditoria):
    conn = get_conn()
    aud = services.obter_auditoria(conn, id_auditoria)
    if not aud:
        conn.close()
        abort(404)
    divergencias = services.divergencias_da_auditoria(conn, id_auditoria)
    conn.close()

    colunas = ["Código", "Produto", "Categoria", "Unid.",
               "Teórico", "Contado", "Diferença", "Custo Unit.",
               "Diferença Valor", "Estado"]
    linhas = []
    for d in divergencias:
        u = (d["unidade"] or "").lower()
        def fqtd(v):
            return f"{float(v or 0):.3f}".replace(".", ",") if u == "kg" else f"{int(round(float(v or 0)))}"
        linhas.append([
            d["codigo"], d["nome"], d["categoria_nome"] or "—", d["unidade"],
            fqtd(d["estoque_teorico"]),
            fqtd(d["estoque_contado"]),
            fqtd(d["diferenca_qtd"]),
            f"{float(d['custo_unitario'] or 0):.2f}".replace(".", ","),
            f"{float(d['diferenca_valor'] or 0):.2f}".replace(".", ","),
            d["estado"],
        ])

    sub = (f"Período: {aud['data_inicio']} a {aud['data_fim']} | "
           f"Responsável: {aud['responsavel_nome'] or '—'} | "
           f"Estado: {aud['estado']}")
    buf = reportmod.gerar_pdf(
        f"Auditoria de Estoque #{id_auditoria}", colunas, linhas,
        subtitulo=sub, landscape_mode=True)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"auditoria_{id_auditoria}.pdf")


@app.route("/auditorias/divergencia/<int:id_div>/justificar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def auditoria_justificar(id_div):
    conn = get_conn()
    try:
        services.justificar_divergencia(
            conn, id_div,
            motivo=request.form.get("motivo", "").strip() or "Sem motivo",
            id_usuario=session["user_id"],
            observacao=request.form.get("observacao"),
            ip_address=_client_ip(),
        )
        flash("Divergência justificada.", "sucesso")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(request.referrer or url_for("auditorias_list"))


@app.route("/auditorias/divergencia/<int:id_div>/recontar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def auditoria_recontar(id_div):
    conn = get_conn()
    try:
        services.solicitar_recontagem(conn, id_div, session["user_id"], _client_ip())
        flash("Recontagem solicitada.", "sucesso")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(request.referrer or url_for("auditorias_list"))


@app.route("/auditorias/divergencia/<int:id_div>/aceitar", methods=["POST"])
@perfil_required("Admin", "Gerente")
def auditoria_aceitar(id_div):
    conn = get_conn()
    try:
        services.marcar_aceite(conn, id_div, session["user_id"], _client_ip())
        flash("Divergência marcada como aceite.", "sucesso")
    except services.RegraNegocioError as e:
        flash(str(e), "erro")
    conn.close()
    return redirect(request.referrer or url_for("auditorias_list"))


@app.route("/auditorias/<int:id_auditoria>/relatorios/<tipo>")
@login_required
def auditoria_relatorio(id_auditoria, tipo):
    conn = get_conn()
    aud = services.obter_auditoria(conn, id_auditoria)
    if not aud:
        conn.close()
        abort(404)

    contexto = {"aud": aud, "tipo": tipo, "titulo": "", "colunas": [], "linhas": []}

    if tipo == "resumo":
        dados = services.resumo_executivo(conn, id_auditoria)
        contexto.update(dados or {})
        contexto["titulo"] = f"Resumo Executivo — Auditoria #{id_auditoria}"
    elif tipo == "reconciliacao":
        linhas, tot = services.reconciliacao_periodo(
            conn, aud["data_inicio"], aud["data_fim"], aud.get("id_categoria"))
        contexto["linhas"] = linhas
        contexto["totais"] = tot
        contexto["titulo"] = f"Reconciliação — {aud['data_inicio']} a {aud['data_fim']}"
    elif tipo == "abc":
        itens, valor_total = services.curva_abc(conn, id_auditoria)
        contexto["itens"] = itens
        contexto["valor_total"] = valor_total
        contexto["titulo"] = "Curva ABC por Valor de Estoque"
    elif tipo == "perdas":
        perdas, validade = services.perdas_e_validade(conn)
        contexto["perdas"] = perdas
        contexto["validade"] = validade
        contexto["titulo"] = "Perdas e Validade"
    elif tipo == "excecoes":
        lista = services.excecoes(conn)
        contexto["excecoes"] = lista
        contexto["titulo"] = "Exceções"
    elif tipo == "detalhe":
        divs = services.divergencias_da_auditoria(conn, id_auditoria)
        contexto["divergencias"] = divs
        contexto["titulo"] = f"Detalhe por Produto — Auditoria #{id_auditoria}"

    conn.close()
    return render_template("auditoria_relatorios.html", **contexto)


# ---------------------------------------------------------------------------
# FECHOS MENSAIS
# ---------------------------------------------------------------------------
@app.route("/fechos-mensais", methods=["GET", "POST"])
@perfil_required("Admin", "Gerente")
def fechos_mensais_list():
    conn = get_conn()
    if request.method == "POST":
        try:
            ano = int(request.form["ano"])
            mes = int(request.form["mes"])
            sobrescrever = request.form.get("sobrescrever") == "1"
            res = services.gerar_fecho_mensal(
                conn, ano, mes, session["user_id"], _client_ip(),
                sobrescrever=sobrescrever)
            flash(
                f"Fecho {mes:02d}/{ano} gerado: {res['n_itens']} produtos, "
                f"custo total {res['total_custo']:.2f}.",
                "sucesso"
            )
        except services.RegraNegocioError as e:
            flash(str(e), "erro")
        except Exception as e:
            flash(f"Erro: {e}", "erro")
        conn.close()
        return redirect(url_for("fechos_mensais_list"))

    fechos = services.listar_fechos(conn)
    conn.close()
    return render_template("fechos_mensais.html", fechos=fechos,
                           hoje=datetime.now().date().isoformat())


@app.route("/fechos-mensais/<int:id_fecho>")
@perfil_required("Admin", "Gerente")
def fecho_detalhe(id_fecho):
    conn = get_conn()
    fecho = conn.execute(
        "SELECT * FROM fechos_mensais WHERE id_fecho=?", (id_fecho,)
    ).fetchone()
    if not fecho:
        conn.close()
        abort(404)
    itens = conn.execute(
        """SELECT i.*, p.codigo, p.nome, p.unidade
           FROM fechos_mensais_itens i
           JOIN produtos p ON p.id_produto=i.id_produto
           WHERE i.id_fecho=?
           ORDER BY p.nome""",
        (id_fecho,),
    ).fetchall()
    conn.close()
    return render_template("fecho_detalhe.html", fecho=fecho, itens=itens)


# ---------------------------------------------------------------------------
# PERDAS
# ---------------------------------------------------------------------------
@app.route("/perdas", methods=["GET", "POST"])
@perfil_required("Admin", "Gerente", "Operador")
def perdas_list():
    conn = get_conn()
    if request.method == "POST":
        try:
            id_produto = int(request.form["id_produto"])
            # FIX: aceita vírgula decimal.
            quantidade = safe_parse_float(request.form.get("quantidade"), None, milhares=False)
            if quantidade is None or quantidade <= 0:
                flash("Quantidade inválida.", "erro")
                conn.close()
                return redirect(url_for("perdas_list"))
            motivo = request.form.get("motivo", "Outro")
            data_str = request.form.get("data_perda") or ""
            data_hora = (data_str + "T" + datetime.now().strftime("%H:%M:%S")) if data_str else None
            services.registrar_perda(
                conn, id_produto, quantidade, motivo,
                id_usuario=session["user_id"],
                observacao=request.form.get("observacao"),
                data_hora=data_hora,
                ip_address=_client_ip(),
            )
            flash("Perda registada.", "sucesso")
        except services.RegraNegocioError as e:
            flash(str(e), "erro")
        except Exception as e:
            flash(f"Erro: {e}", "erro")
        conn.close()
        return redirect(url_for("perdas_list"))

    perdas = conn.execute(
        """SELECT m.*, p.codigo, p.nome, p.unidade, u.nome AS usuario_nome
           FROM movimentos m
           JOIN produtos p ON p.id_produto=m.id_produto
           LEFT JOIN usuarios u ON u.id_usuario=m.id_usuario
           WHERE m.subtipo LIKE 'Perda_%'
           ORDER BY m.data_hora DESC LIMIT 300"""
    ).fetchall()
    produtos = conn.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("perdas.html", perdas=perdas, produtos=produtos,
                           hoje=datetime.now().date().isoformat())


# ---------------------------------------------------------------------------
# Segurança / perfil
# ---------------------------------------------------------------------------
@app.route("/perfil/senha", methods=["GET", "POST"])
@login_required
def trocar_senha():
    if request.method == "POST":
        senha_atual = request.form.get("senha_atual", "")
        nova_senha = request.form.get("nova_senha", "")
        confirmar_senha = request.form.get("confirmar_senha", "")

        conn = get_conn()
        usuario = conn.execute("SELECT * FROM usuarios WHERE id_usuario=?",
                               (session["user_id"],)).fetchone()
        if not usuario or not check_password_hash(usuario["senha_hash"], senha_atual):
            flash("A senha atual informada está incorreta.", "erro")
        elif not services.validar_senha(nova_senha):
            flash("A nova senha deve ter no mínimo 8 caracteres, incluir letra, número e símbolo.", "erro")
        elif nova_senha != confirmar_senha:
            flash("A confirmação da nova senha não confere.", "erro")
        else:
            conn.execute("UPDATE usuarios SET senha_hash=? WHERE id_usuario=?",
                         (generate_password_hash(nova_senha), session["user_id"]))
            registrar_auditoria(conn, session["user_id"], "usuarios",
                                session["user_id"], "UPDATE",
                                {"senha": "ALTERADA_PELO_USUARIO"}, None, _client_ip())
            conn.commit()
            flash("Sua senha foi alterada com sucesso.", "sucesso")
        conn.close()
    return render_template("trocar_senha.html")


# ---------------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------------
@app.route("/usuarios")
@perfil_required("Admin")
def usuarios_list():
    conn = get_conn()
    usuarios = conn.execute("SELECT * FROM usuarios ORDER BY nome").fetchall()
    conn.close()
    return render_template("usuarios.html", usuarios=usuarios)


@app.route("/usuarios/salvar", methods=["POST"])
@perfil_required("Admin")
def usuarios_salvar():
    f = request.form
    conn = get_conn()
    try:
        id_usuario = f.get("id_usuario")
        senha = f.get("senha", "").strip()
        if id_usuario:
            row = conn.execute(
                "SELECT * FROM usuarios WHERE id_usuario=?", (id_usuario,)
            ).fetchone()
            if not row:
                flash("Usuário não encontrado.", "erro")
                conn.close()
                return redirect(url_for("usuarios_list"))
            antigos = dict(row)
            dados_novos = {"nome": f["nome"], "login": f["login"],
                           "perfil": f["perfil"],
                           "email": f.get("email", "").strip() or None,
                           "telefone": f.get("telefone", "").strip() or None}
            if senha:
                if not services.validar_senha(senha):
                    flash("A nova senha deve ter no mínimo 8 caracteres, incluir letra, número e símbolo.", "erro")
                    conn.close()
                    return redirect(url_for("usuarios_list"))
                conn.execute(
                    "UPDATE usuarios SET nome=?, login=?, perfil=?, senha_hash=?, email=?, telefone=? WHERE id_usuario=?",
                    (f["nome"], f["login"], f["perfil"], generate_password_hash(senha),
                     dados_novos["email"], dados_novos["telefone"], id_usuario))
                dados_novos["senha_hash"] = "***"
            else:
                conn.execute(
                    "UPDATE usuarios SET nome=?, login=?, perfil=?, email=?, telefone=? WHERE id_usuario=?",
                    (f["nome"], f["login"], f["perfil"],
                     dados_novos["email"], dados_novos["telefone"], id_usuario))
            registrar_auditoria(conn, session["user_id"], "usuarios",
                                int(id_usuario), "UPDATE", antigos, dados_novos, _client_ip())
            flash("Usuário atualizado.", "sucesso")
        else:
            if not senha or not services.validar_senha(senha):
                flash("Informe uma senha forte para o novo usuário.", "erro")
                conn.close()
                return redirect(url_for("usuarios_list"))
            dados_novos = {"nome": f["nome"], "login": f["login"],
                           "perfil": f["perfil"], "ativo": 1,
                           "email": f.get("email", "").strip() or None,
                           "telefone": f.get("telefone", "").strip() or None}
            cur = conn.execute(
                "INSERT INTO usuarios (nome, login, senha_hash, perfil, ativo, email, telefone) VALUES (?,?,?,?,1,?,?)",
                (f["nome"], f["login"], generate_password_hash(senha), f["perfil"],
                 dados_novos["email"], dados_novos["telefone"]))
            registrar_auditoria(conn, session["user_id"], "usuarios",
                                cur.lastrowid, "INSERT", None, dados_novos, _client_ip())
            flash("Usuário criado.", "sucesso")
        conn.commit()
    except Exception as e:
        flash(f"Erro ao salvar usuário: {e}", "erro")
    conn.close()
    return redirect(url_for("usuarios_list"))


@app.route("/usuarios/<int:id_usuario>/toggle", methods=["POST"])
@perfil_required("Admin")
def usuarios_toggle(id_usuario):
    conn = get_conn()
    if id_usuario == session["user_id"]:
        flash("Você não pode desativar seu próprio usuário.", "erro")
    else:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE id_usuario=?", (id_usuario,)
        ).fetchone()
        if not row:
            flash("Usuário não encontrado.", "erro")
        else:
            antigos = dict(row)
            novo_ativo = 0 if antigos["ativo"] else 1
            conn.execute("UPDATE usuarios SET ativo = ? WHERE id_usuario=?", (novo_ativo, id_usuario,))
            registrar_auditoria(conn, session["user_id"], "usuarios",
                                id_usuario, "UPDATE", antigos, {"ativo": novo_ativo}, _client_ip())
            conn.commit()
            flash("Status do usuário atualizado.", "sucesso")
    conn.close()
    return redirect(url_for("usuarios_list"))


@app.route("/usuarios/<int:id_usuario>/excluir", methods=["POST"])
@perfil_required("Admin")
def usuarios_excluir(id_usuario):
    conn = get_conn()
    if id_usuario == session["user_id"]:
        flash("Você não pode excluir seu próprio usuário.", "erro")
    else:
        usuario = conn.execute("SELECT * FROM usuarios WHERE id_usuario=?", (id_usuario,)).fetchone()
        if not usuario:
            flash("Usuário não encontrado.", "erro")
        else:
            movimentos = conn.execute(
                "SELECT COUNT(*) c FROM movimentos WHERE id_usuario=?", (id_usuario,)
            ).fetchone()["c"]
            antigos = dict(usuario)
            if movimentos:
                conn.execute("UPDATE usuarios SET ativo=0 WHERE id_usuario=?", (id_usuario,))
                registrar_auditoria(conn, session["user_id"], "usuarios",
                                    id_usuario, "DELETE", antigos,
                                    {"ativo": 0, "motivo": "historico_movimentacoes"},
                                    _client_ip())
                flash("Usuário com histórico foi desativado em vez de excluído.", "info")
            else:
                conn.execute("DELETE FROM usuarios WHERE id_usuario=?", (id_usuario,))
                registrar_auditoria(conn, session["user_id"], "usuarios",
                                    id_usuario, "DELETE", antigos, None, _client_ip())
                flash("Usuário excluído com sucesso.", "sucesso")
            conn.commit()
    conn.close()
    return redirect(url_for("usuarios_list"))


@app.route("/comparar-estoque", methods=["GET", "POST"])
@login_required
def comparar_estoque():
    """Importa Excel/PDF e compara quantidades com o sistema.
    NÃO escreve na base de dados.
    Modos: 'estoque' (saldo actual) ou 'vendas' (período)."""
    if request.method == "POST":
        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            flash("Selecione um ficheiro (.xlsx ou .pdf).", "erro")
            return redirect(url_for("comparar_estoque"))

        modo = request.form.get("modo", "estoque")
        if modo not in ("estoque", "vendas"):
            modo = "estoque"

        data_ini = request.form.get("data_ini", "").strip()
        data_fim = request.form.get("data_fim", "").strip()
        if modo == "vendas":
            if not data_ini:
                data_ini = datetime.now().replace(day=1).strftime("%Y-%m-%d")
            if not data_fim:
                data_fim = datetime.now().strftime("%Y-%m-%d")

        ext = arquivo.filename.rsplit(".", 1)[-1].lower() if "." in arquivo.filename else ""
        try:
            if ext in ("xlsx", "xls"):
                linhas = compmod.ler_excel(arquivo)
            elif ext == "pdf":
                linhas = compmod.ler_pdf(arquivo)
            else:
                flash("Formato não suportado. Use .xlsx ou .pdf.", "erro")
                return redirect(url_for("comparar_estoque"))
        except ImportError as e:
            flash(str(e), "aviso")
            return redirect(url_for("comparar_estoque"))
        except Exception as e:
            logging.error(f"Erro a ler ficheiro: {e}")
            flash(f"Erro ao ler o ficheiro: {e}", "erro")
            return redirect(url_for("comparar_estoque"))

        if not linhas:
            flash("Não foi possível extrair linhas do ficheiro. "
                  "Verifique se tem uma tabela com 'Produto' e 'Quantidade'.", "aviso")
            return redirect(url_for("comparar_estoque"))

        conn = get_conn()
        try:
            if modo == "vendas":
                # Quantidades VENDIDAS no período
                produtos = [dict(r) for r in conn.execute("""
                    SELECT p.id_produto, p.codigo, p.nome, p.unidade,
                           COALESCE(SUM(m.quantidade), 0) AS saldo
                    FROM produtos p
                    LEFT JOIN movimentos m
                        ON m.id_produto = p.id_produto
                       AND m.tipo = 'Saida'
                       AND m.data_hora >= ?
                       AND m.data_hora <= ?
                    WHERE p.ativo = 1
                    GROUP BY p.id_produto
                """, (data_ini + "T00:00:00",
                      data_fim + "T23:59:59")).fetchall()]
            else:
                # Saldo ACTUAL em estoque
                produtos = [dict(r) for r in conn.execute("""
                    SELECT p.id_produto, p.codigo, p.nome, p.unidade,
                           COALESCE(e.saldo_atual, 0) AS saldo
                    FROM produtos p
                    LEFT JOIN estoque e ON e.id_produto = p.id_produto
                    WHERE p.ativo = 1
                """).fetchall()]
        finally:
            conn.close()

        encontrados, nao_encontrados = compmod.comparar(linhas, produtos)

        # Guardar resultado em ficheiro temporário (para exportação)
        comp_id = str(uuid4())
        pasta_comp = os.path.join(get_base_dir(), "dados", "_comparacoes")
        os.makedirs(pasta_comp, exist_ok=True)

        # Limpar ficheiros com mais de 7 dias
        try:
            limite = time.time() - 7 * 86400
            for f in os.listdir(pasta_comp):
                p = os.path.join(pasta_comp, f)
                if os.path.isfile(p) and os.path.getmtime(p) < limite:
                    os.remove(p)
        except Exception:
            pass

        dados_comp = {
            "modo": modo,
            "data_ini": data_ini,
            "data_fim": data_fim,
            "nome_ficheiro": arquivo.filename,
            "total_ficheiro": len(linhas),
            "encontrados": encontrados,
            "nao_encontrados": nao_encontrados,
        }
        with open(os.path.join(pasta_comp, f"{comp_id}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(dados_comp, f, ensure_ascii=False)

        return redirect(url_for("comparar_estoque_resultado", comp_id=comp_id))

    return render_template("comparar_estoque.html")


@app.route("/comparar-estoque/resultado/<comp_id>")
@login_required
def comparar_estoque_resultado(comp_id):
    pasta_comp = os.path.join(get_base_dir(), "dados", "_comparacoes")
    caminho = os.path.join(pasta_comp, f"{comp_id}.json")
    if not os.path.exists(caminho):
        flash("Resultado expirado ou inexistente. Refaça a comparação.", "erro")
        return redirect(url_for("comparar_estoque"))

    with open(caminho, "r", encoding="utf-8") as f:
        dados = json.load(f)

    total_encontrados = len(dados["encontrados"])
    total_nao_encontrados = len(dados["nao_encontrados"])
    total_divergencias = sum(1 for r in dados["encontrados"]
                             if abs(r["diferenca"]) > 0.001)

    return render_template(
        "comparar_estoque_resultado.html",
        comp_id=comp_id,
        modo=dados["modo"],
        data_ini=dados["data_ini"],
        data_fim=dados["data_fim"],
        encontrados=dados["encontrados"],
        nao_encontrados=dados["nao_encontrados"],
        nome_ficheiro=dados["nome_ficheiro"],
        total_ficheiro=dados["total_ficheiro"],
        total_encontrados=total_encontrados,
        total_nao_encontrados=total_nao_encontrados,
        total_divergencias=total_divergencias,
    )


@app.route("/comparar-estoque/resultado/<comp_id>/exportar/<formato>")
@login_required
def comparar_estoque_exportar(comp_id, formato):
    if formato not in ("pdf", "excel"):
        abort(404)

    pasta_comp = os.path.join(get_base_dir(), "dados", "_comparacoes")
    caminho = os.path.join(pasta_comp, f"{comp_id}.json")
    if not os.path.exists(caminho):
        abort(404)

    with open(caminho, "r", encoding="utf-8") as f:
        dados = json.load(f)

    modo_label = "Estoque" if dados["modo"] == "estoque" else "Vendas"
    titulo = f"Comparação de {modo_label} — {dados['nome_ficheiro']}"
    sub = f"Modo: {modo_label}"
    if dados["modo"] == "vendas" and dados["data_ini"]:
        sub += f"  |  Período: {dados['data_ini']} a {dados['data_fim']}"
    sub += (f"  |  {len(dados['encontrados'])} encontrado(s)"
            f"  |  {len(dados['nao_encontrados'])} não encontrado(s)")

    col_qtd_sist = "Qtd. Vendida (Sistema)" if dados["modo"] == "vendas" else "Qtd. Atual (Sistema)"
    colunas = ["Código", "Produto", "Unid.", "Qtd. Ficheiro", col_qtd_sist,
               "Diferença", "Similaridade %"]
    linhas = []
    for r in dados["encontrados"]:
        linhas.append([
            r["codigo"],
            r["nome_sistema"],
            r["unidade"],
            r["quantidade_ficheiro"],
            r["saldo_sistema"],
            r["diferenca"],
            f"{r['similaridade']}%",
        ])

    if dados["nao_encontrados"]:
        linhas.append(["", "", "", "", "", "", ""])
        linhas.append(["PRODUTOS NÃO ENCONTRADOS NO SISTEMA", "", "", "", "", "", ""])
        for r in dados["nao_encontrados"]:
            linhas.append([
                "", r["nome_ficheiro"], "",
                r["quantidade_ficheiro"], "",
                "não encontrado",
                f"{r['melhor_score']}%",
            ])

    buf = None
    if formato == "pdf":
        buf = reportmod.gerar_pdf(titulo, colunas, linhas, subtitulo=sub)
        nome = f"comparacao_{dados['modo']}_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=nome)
    else:
        buf = reportmod.gerar_excel(titulo, colunas, linhas)
        nome = f"comparacao_{dados['modo']}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True, download_name=nome
        )
# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------
@app.route("/configuracoes", methods=["GET", "POST"])
@perfil_required("Admin")
def configuracoes():
    if request.method == "POST":
        # ---- Negócio ----
        set_config("metodo_custeio", request.form.get("metodo_custeio", "media_ponderada"))
        set_config("dias_alerta_validade", request.form.get("dias_alerta_validade", "7"))
        set_config("margem_lucro_desejada", request.form.get("margem_lucro_desejada", "30"))
        set_config("permitir_estoque_negativo",
                   "1" if request.form.get("permitir_estoque_negativo") else "0")
        set_config("estoque_minimo_padrao", request.form.get("estoque_minimo_padrao", "0"))
        set_config("backup_automatico",
                   "1" if request.form.get("backup_automatico") else "0")
        set_config("max_tentativas_login", request.form.get("max_tentativas_login", "5"))
        set_config("tempo_bloqueio_login_min", request.form.get("tempo_bloqueio_login_min", "5"))

        # ---- Empresa ----
        set_config("nome_empresa", (request.form.get("nome_empresa") or "").strip())
        set_config("moeda", (request.form.get("moeda") or "MZN").strip())
        set_config("morada_empresa", (request.form.get("morada_empresa") or "").strip())
        set_config("nuit_empresa", (request.form.get("nuit_empresa") or "").strip())
        set_config("contacto_empresa", (request.form.get("contacto_empresa") or "").strip())
        set_config("email_empresa", (request.form.get("email_empresa") or "").strip())

        # ---- Documentos fiscais ----
        set_config("serie_fatura", (request.form.get("serie_fatura") or "FT").strip())
        set_config("proximo_num_fatura", request.form.get("proximo_num_fatura", "1").strip())
        set_config("tipo_documento", (request.form.get("tipo_documento") or "Factura").strip())
        set_config("condicoes_pagamento_padrao",
                   (request.form.get("condicoes_pagamento_padrao") or "").strip())
        set_config("cambio_padrao", (request.form.get("cambio_padrao") or "0,00").strip())
        set_config("moeda_cambio", (request.form.get("moeda_cambio") or "USD").strip())
        set_config("taxa_iva_padrao", request.form.get("taxa_iva_padrao", "16").strip())
        set_config("regime_iva", (request.form.get("regime_iva") or "Regime Geral").strip())
        set_config("motivo_isencao_iva",
                   (request.form.get("motivo_isencao_iva") or "").strip())
        set_config("texto_rodape_fatura",
                   (request.form.get("texto_rodape_fatura") or "").strip())
        set_config("banco_nome", (request.form.get("banco_nome") or "").strip())
        set_config("iban_empresa", (request.form.get("iban_empresa") or "").strip())
        set_config("numero_conta", (request.form.get("numero_conta") or "").strip())
        set_config("nib_empresa", (request.form.get("nib_empresa") or "").strip())

        # ---- Segurança ----
        codigo_seg_novo = (request.form.get("codigo_alteracao_produto") or "").strip()
        if codigo_seg_novo:
            set_config("codigo_alteracao_produto",
                       generate_password_hash(codigo_seg_novo))
        elif request.form.get("limpar_codigo_alteracao") == "1":
            set_config("codigo_alteracao_produto", "")

        # ---- Upload do logo ----
        logo_arquivo = request.files.get("logo_arquivo")
        if logo_arquivo and logo_arquivo.filename:
            try:
                fname = logo_arquivo.filename
                if "." not in fname:
                    raise ValueError("Ficheiro sem extensão")
                ext = fname.rsplit(".", 1)[-1].lower()
                if ext not in ("png", "jpg", "jpeg", "gif", "bmp", "webp"):
                    raise ValueError(f"Formato de imagem não suportado ({ext})")

                pasta_img = os.path.join(get_base_dir(), "static", "img")
                os.makedirs(pasta_img, exist_ok=True)
                nome_seguro = f"logo_empresa.{ext}"
                caminho_destino = os.path.join(pasta_img, nome_seguro)

                # remove logos antigos de outras extensões
                import glob
                for antigo in glob.glob(os.path.join(pasta_img, "logo_empresa.*")):
                    if os.path.abspath(antigo) != os.path.abspath(caminho_destino):
                        try:
                            os.remove(antigo)
                        except OSError:
                            pass

                logo_arquivo.save(caminho_destino)
                if not os.path.exists(caminho_destino) or os.path.getsize(caminho_destino) == 0:
                    raise IOError("Ficheiro vazio ou inacessível após gravação")
                set_config("logo_empresa", nome_seguro)
                flash("Logo atualizado com sucesso.", "sucesso")
            except Exception as e:
                logging.error(f"Falha ao guardar logo: {e}")
                flash(f"Erro ao carregar logo: {e}", "erro")

        if request.form.get("remover_logo") == "1":
            nome_atual = get_config("logo_empresa", "")
            if nome_atual and not nome_atual.startswith("data:"):
                caminho = os.path.join(get_base_dir(), "static", "img", nome_atual)
                try:
                    if os.path.exists(caminho):
                        os.remove(caminho)
                except OSError:
                    pass
            set_config("logo_empresa", "")
        flash("Configurações salvas com sucesso.", "sucesso")
        return redirect(url_for("configuracoes"))

    config = {
        "metodo_custeio": get_config("metodo_custeio", "media_ponderada"),
        "dias_alerta_validade": get_config("dias_alerta_validade", "7"),
        "margem_lucro_desejada": get_config("margem_lucro_desejada", "30"),
        "permitir_estoque_negativo": get_config("permitir_estoque_negativo", "0"),
        "nome_empresa": get_config("nome_empresa", ""),
        "moeda": get_config("moeda", "MZN"),
        "estoque_minimo_padrao": get_config("estoque_minimo_padrao", "0"),
        "backup_automatico": get_config("backup_automatico", "0"),
        "morada_empresa": get_config("morada_empresa", ""),
        "nuit_empresa": get_config("nuit_empresa", ""),
        "contacto_empresa": get_config("contacto_empresa", ""),
        "email_empresa": get_config("email_empresa", ""),
        "logo_empresa": get_config("logo_empresa", ""),
        "codigo_alteracao_produto": get_config("codigo_alteracao_produto", ""),
        "max_tentativas_login": get_config("max_tentativas_login", "5"),
        "tempo_bloqueio_login_min": get_config("tempo_bloqueio_login_min", "5"),
        "primeira_instalacao": get_config("primeira_instalacao", "1"),
        "serie_fatura": get_config("serie_fatura", "FT"),
        "proximo_num_fatura": get_config("proximo_num_fatura", "1"),
        "tipo_documento": get_config("tipo_documento", "Factura"),
        "condicoes_pagamento_padrao": get_config("condicoes_pagamento_padrao", "30 dias"),
        "cambio_padrao": get_config("cambio_padrao", "0,00"),
        "moeda_cambio": get_config("moeda_cambio", "USD"),
        "taxa_iva_padrao": get_config("taxa_iva_padrao", "16"),
        "regime_iva": get_config("regime_iva", "Regime Geral"),
        "motivo_isencao_iva": get_config("motivo_isencao_iva", ""),
        "texto_rodape_fatura": get_config("texto_rodape_fatura", ""),
        "banco_nome": get_config("banco_nome", ""),
        "iban_empresa": get_config("iban_empresa", ""),
        "numero_conta": get_config("numero_conta", ""),
        "nib_empresa": get_config("nib_empresa", ""),
    }
    logo_url = None
    if config["logo_empresa"]:
        caminho_logo = os.path.join(get_base_dir(), "static", "img", config["logo_empresa"])
        if os.path.exists(caminho_logo):
            logo_url = _logo_url_para(config["logo_empresa"])
    return render_template("configuracoes.html", config=config, logo_url=logo_url)


@app.route("/configuracoes/backup")
@perfil_required("Admin")
def configuracoes_backup():
    backup_dir = os.path.join(get_base_dir(), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"estoque_backup_{ts}.db")
    from database import copiar_base_dados
    copiar_base_dados(backup_path)
    return send_file(backup_path, as_attachment=True,
                     download_name=os.path.basename(backup_path))


@app.route("/configuracoes/restaurar", methods=["POST"])
@perfil_required("Admin")
def configuracoes_restaurar():
    if "arquivo_backup" not in request.files:
        flash("Selecione um arquivo de backup para restaurar.", "erro")
        return redirect(url_for("configuracoes"))

    arquivo = request.files["arquivo_backup"]
    if arquivo.filename == "":
        flash("Selecione um arquivo de backup para restaurar.", "erro")
        return redirect(url_for("configuracoes"))

    nome_seguro = secure_filename(arquivo.filename or "")
    pasta_upload = os.path.join(get_base_dir(), "backups")
    os.makedirs(pasta_upload, exist_ok=True)
    caminho_temp = os.path.join(
        pasta_upload,
        f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{nome_seguro}",
    )
    arquivo.save(caminho_temp)

    tabelas_esperadas = {"produtos", "usuarios", "movimentos", "estoque",
                         "categorias", "configuracoes"}
    try:
        conn = sqlite3.connect(caminho_temp)
        tabelas_existentes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        if not tabelas_esperadas.issubset(tabelas_existentes):
            raise sqlite3.Error("Schema incompatível com o SGE.")

        try:
            c = sqlite3.connect(DB_PATH)
            c.execute("PRAGMA wal_checkpoint(FULL)")
            c.close()
        except Exception:
            pass

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pre_restore = os.path.join(get_base_dir(), "backups", f"pre_restore_{ts}.db")
        os.makedirs(os.path.dirname(pre_restore), exist_ok=True)
        if os.path.exists(DB_PATH):
            try:
                # FIX: usar a API de backup do SQLite em vez de shutil.copy2
                from database import copiar_base_dados
                copiar_base_dados(pre_restore)
            except Exception as e:
                logging.error(f"Falha na cópia pré-restauro: {e}")

        for sufixo in ("-wal", "-shm", "-journal"):
            f = DB_PATH + sufixo
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

        shutil.copy2(caminho_temp, DB_PATH)

        try:
            init_db()
            migracao_ok = True
        except Exception as mig_err:
            migracao_ok = False
            logging.error(f"Falha ao migrar backup restaurado: {mig_err}")
            if os.path.exists(pre_restore):
                try:
                    # Reverter usando API de backup do SQLite
                    origin = sqlite3.connect(pre_restore)
                    dest = sqlite3.connect(DB_PATH)
                    try:
                        origin.backup(dest)
                    finally:
                        origin.close()
                        dest.close()
                    init_db()
                except Exception as rollback_err:
                    logging.error(f"Falha ao reverter restauro: {rollback_err}")

        # FIX: forçar logout após restauro — a sessão pode apontar para um
        # utilizador que já não existe na DB restaurada, causando erros de FK.
        session.clear()
        if migracao_ok:
            flash("Backup restaurado e migrado com sucesso. Faça login novamente.", "sucesso")
        else:
            flash("Backup restaurado, mas a migração falhou. Sistema revertido.", "erro")
    except sqlite3.Error as e:
        flash(f"O arquivo não é um backup válido do sistema. ({e})", "erro")
    except Exception as e:
        flash(f"Erro ao restaurar backup: {e}", "erro")
    finally:
        if os.path.exists(caminho_temp):
            try:
                os.remove(caminho_temp)
            except OSError:
                pass

    return redirect(url_for("login"))


@app.route("/configuracoes/formatar", methods=["GET", "POST"])
@perfil_required("Admin")
def configuracoes_formatar():
    if request.method == "POST":
        nivel = request.form.get("nivel", "").strip()
        confirmacao = request.form.get("confirmacao", "").strip().upper()
        ciencia = request.form.get("ciencia") == "1"
        senha_admin = request.form.get("senha_admin", "")

        conn_auth = get_conn()
        utilizador = conn_auth.execute(
            "SELECT senha_hash FROM usuarios WHERE id_usuario=?", (session["user_id"],)
        ).fetchone()
        conn_auth.close()
        if not utilizador or not check_password_hash(utilizador["senha_hash"], senha_admin):
            flash("Senha incorrecta. Para formatar a base de dados confirme a sua senha.", "erro")
            return redirect(url_for("configuracoes_formatar"))

        if nivel not in services.NIVEIS_RESET:
            flash("Selecione um nível de reset válido.", "erro")
            return redirect(url_for("configuracoes_formatar"))
        if confirmacao != "APAGAR":
            flash("Para confirmar, escreva exactamente APAGAR (maiúsculas).", "erro")
            return redirect(url_for("configuracoes_formatar"))
        if not ciencia:
            flash("Marque a caixa confirmando que compreende o impacto.", "erro")
            return redirect(url_for("configuracoes_formatar"))

        conn = get_conn()
        try:
            resultado = services.formatar_base_dados(
                conn, nivel=nivel,
                id_usuario_admin=session["user_id"],
                ip_address=_client_ip(),
            )
            conn.commit()
            conn.close()

            try:
                log_path = os.path.join(get_base_dir(), "reset_log.txt")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(
                        f"[{datetime.now().isoformat(timespec='seconds')}] "
                        f"user_id={session.get('user_id')} nivel={nivel} "
                        f"backup={resultado.get('backup_path')} ip={_client_ip()}\n"
                    )
            except Exception:
                pass

            session.clear()
            flash(
                f"Reset concluído ({services.NIVEIS_RESET[nivel]}). "
                f"Backup automático guardado em backups/.",
                "sucesso"
            )
            return redirect(url_for("login"))
        except services.RegraNegocioError as e:
            flash(str(e), "erro")
            conn.close()
        except Exception as e:
            flash(f"Erro ao formatar base de dados: {e}", "erro")
            try:
                conn.close()
            except Exception:
                pass
        return redirect(url_for("configuracoes_formatar"))

    return render_template("configuracoes_formatar.html", niveis=services.NIVEIS_RESET)


@app.route("/diagnostico")
@perfil_required("Admin", "Gerente")
def diagnostico_view():
    """Executa o diagnóstico em tempo real e mostra o relatório."""
    rel = diagmod.executar_diagnostico_completo()
    diagmod.guardar_relatorio(rel)
    app.config["_diagnostico"] = rel
    return render_template("diagnostico.html", rel=rel)

@app.route("/api/diagnostico/recheck", methods=["POST"])
@perfil_required("Admin", "Gerente")
def diagnostico_recheck():
    """Força uma nova execução do diagnóstico sem reiniciar a app."""
    try:
        rel = diagmod.executar_diagnostico_completo()
        diagmod.guardar_relatorio(rel)
        app.config["_diagnostico"] = rel
        return jsonify({
            "ok": True,
            "tem_alertas": rel["tem_alertas"],
            "contagens": rel["contagens"],
        })
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500

# ---------------------------------------------------------------------------
# ATUALIZAÇÕES
# ---------------------------------------------------------------------------
def _base_dir_app():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _versao_actual():
    base = _base_dir_app()
    # Fonte de verdade: _codigo\version.json (o que o updater escreve).
    # Fallback: raiz (compatibilidade com builds antigos).
    for p in (os.path.join(base, "_codigo", "version.json"),
              os.path.join(base, "version.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {"version": "0.0.0", "format_version": 1}


@app.route("/atualizacoes")
@perfil_required("Admin")
def atualizacoes_view():
    return render_template("atualizacoes.html", versao=_versao_actual())


@app.route("/api/atualizacoes/validar", methods=["POST"])
@perfil_required("Admin")
def api_atualizacoes_validar():
    if "pacote" not in request.files:
        return jsonify({"ok": False, "erro": "Nenhum ficheiro enviado."}), 400

    f = request.files["pacote"]
    if not f.filename or not f.filename.lower().endswith(".sgeu"):
        return jsonify({"ok": False, "erro": "Envie um ficheiro .sgeu."}), 400

    temp = os.path.join(
        tempfile.gettempdir(),
        f"sge_update_{secrets.token_hex(8)}.sgeu",
    )
    f.save(temp)

    try:
        manifest = _atualizador.validar_pacote(temp, _base_dir_app())
        return jsonify({
            "ok": True,
            "manifest": manifest,
            "temp_path": temp,
            "tamanho": os.path.getsize(temp),
        })
    except _atualizador.PacoteInvalido as e:
        try:
            os.remove(temp)
        except OSError:
            pass
        return jsonify({"ok": False, "erro": str(e)}), 400
    except Exception as e:
        try:
            os.remove(temp)
        except OSError:
            pass
        return jsonify({"ok": False, "erro": f"Erro: {e}"}), 500


@app.route("/api/atualizacoes/aplicar", methods=["POST"])
@perfil_required("Admin")
def api_atualizacoes_aplicar():
    global _UPDATE_STATE
    if _UPDATE_STATE["state"] == "applying":
        return jsonify({"ok": False, "erro": "Ja ha uma atualizacao em curso."}), 409

    data = request.get_json(silent=True) or {}
    temp_path = data.get("temp_path")
    if not temp_path or not os.path.isfile(temp_path):
        return jsonify({"ok": False, "erro": "Pacote nao encontrado."}), 400

    _UPDATE_STATE = {
        "state": "applying",
        "current": 0,
        "total": 0,
        "file": "A preparar...",
        "error": None,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
    }

    def _progresso(current, total, nome):
        _UPDATE_STATE["current"] = current
        _UPDATE_STATE["total"] = total
        _UPDATE_STATE["file"] = nome

    def _runner():
        global _UPDATE_STATE
        try:
            resultado = _atualizador.preparar_update(
                temp_path, _base_dir_app(), callback_progresso=_progresso,
            )
            if resultado["ok"]:
                _UPDATE_STATE.update({
                    "state": "done",
                    "versao_nova": resultado["versao"],
                    "requires_restart": resultado["requires_restart"],
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })
            else:
                _UPDATE_STATE.update({
                    "state": "error",
                    "error": resultado.get("erro"),
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })
        except Exception as e:
            _UPDATE_STATE.update({
                "state": "error",
                "error": str(e),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/atualizacoes/estado")
@perfil_required("Admin")
def api_atualizacoes_estado():
    return jsonify(_UPDATE_STATE)


@app.route("/api/atualizacoes/reiniciar", methods=["POST"])
@perfil_required("Admin")
def api_atualizacoes_reiniciar():
    """Modo seguro: NÃO relança. Pede ao utilizador para fechar e reabrir."""
    return jsonify({
        "ok": True,
        "manual_restart": True,
        "msg": "Feche o SGE completamente e abra novamente.",
    })
# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------
try:
    init_db()
except Exception as _e:
    logging.error(f"Falha ao inicializar a base de dados: {_e}")

# --- Diagnóstico automático no arranque --------------------------------
try:
    _relatorio_diag = diagmod.executar_diagnostico_completo()
    diagmod.guardar_relatorio(_relatorio_diag)
    app.config["_diagnostico"] = _relatorio_diag
    if _relatorio_diag.get("tem_alertas"):
        logging.warning(
            "Diagnóstico no arranque: %s alertas",
            sum(_relatorio_diag.get("contagens", {}).values()),
        )
except Exception as _e:
    logging.error(f"Falha ao correr diagnóstico no arranque: {_e}")
    app.config["_diagnostico"] = None

def abrir_navegador():
    try:
        webbrowser.open(f"http://127.0.0.1:{APP_PORT}/")
    except Exception as e:
        logging.error(f"Falha ao abrir o browser: {e}")


if __name__ == "__main__":
   
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, abrir_navegador).start()

    try:
        app.run(
            host="127.0.0.1",
            port=APP_PORT,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    finally:
        _encerrar_tudo()