# FIX 1.3.3 - teste final
# FIX 1.3.3 - versao de teste
# FIX 1.3.3 - versao de teste
# -*- coding: utf-8 -*-
"""Módulo services: regras de negócio e utilitários do SGE."""
import re
import secrets
import math
from datetime import datetime, timedelta
from database import get_conn, get_config, registrar_auditoria
from werkzeug.security import generate_password_hash, check_password_hash

class RegraNegocioError(Exception):
    pass

UNIDADES_VALIDAS = ("Caixa", "Embalagem", "Unidade", "kg")
UNIDADES_DECIMAIS = ("kg",)

 
# ---------------------------------------------------------------------------
# Utilitários de arredondamento
# ---------------------------------------------------------------------------
def round5(valor, casas=2):
    try:
        casas = int(casas)
    except (TypeError, ValueError):
        casas = 2
    if valor is None:
        return 0.0 if casas > 0 else 0
    try:
        valor_float = float(valor)
    except (TypeError, ValueError):
        return 0.0 if casas > 0 else 0
    if not math.isfinite(valor_float):
        return 0.0 if casas > 0 else 0
    if casas <= 0:
        return int(round(valor_float + 1e-12))
    return round(valor_float + 1e-12, casas)


def ceil5(valor, casas=5):
    try:
        casas = int(casas)
    except (TypeError, ValueError):
        casas = 5
    if valor is None:
        return 0.0
    try:
        valor_float = float(valor)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(valor_float):
        return 0.0
    if casas <= 0:
        return int(math.ceil(valor_float - 1e-12))
    fator = 10 ** casas
    return math.ceil(valor_float * fator - 1e-12) / fator


def arredondar_moeda(valor):
    return round5(valor, 2)


# ---------------------------------------------------------------------------
# Validações
# ---------------------------------------------------------------------------
def validar_senha(senha):
    if not senha or len(senha) < 8:
        return False
    if not re.search(r"[A-Za-z]", senha):
        return False
    if not re.search(r"\d", senha):
        return False
    if not re.search(r"[^A-Za-z0-9]", senha):
        return False
    return True


def validar_produto_cadastrado(conn, id_produto):
    if id_produto is None:
        return False
    try:
        id_produto = int(id_produto)
    except (TypeError, ValueError):
        return False
    row = conn.execute("SELECT 1 FROM produtos WHERE id_produto=? AND ativo=1", (id_produto,)).fetchone()
    return bool(row)


def produto_nome_existe(conn, nome, id_produto_excluir=None):
    nome_norm = (nome or "").strip().lower()
    if not nome_norm:
        return False
    sql = "SELECT id_produto FROM produtos WHERE ativo=1 AND LOWER(nome)=?"
    params = [nome_norm]
    if id_produto_excluir:
        sql += " AND id_produto!=?"
        params.append(id_produto_excluir)
    row = conn.execute(sql, params).fetchone()
    return bool(row)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def registrar_tentativa_login(conn, login, sucesso, ip_address=None):
    conn.execute(
        "INSERT INTO login_attempts (login, ip_address, data_hora, sucesso) VALUES (?,?,?,?)",
        (login, ip_address, datetime.now().isoformat(timespec="seconds"), 1 if sucesso else 0),
    )
    conn.commit()


def login_bloqueado(conn, login):
    max_tent = int(get_config("max_tentativas_login", "5"))
    bloqueio_min = int(get_config("tempo_bloqueio_login_min", "5"))
    desde = (datetime.now() - timedelta(minutes=bloqueio_min)).isoformat(timespec="seconds")
    falhas = conn.execute(
        "SELECT COUNT(*) c FROM login_attempts WHERE login=? AND sucesso=0 AND data_hora >= ?",
        (login, desde),
    ).fetchone()["c"]
    ultimas_falhas = conn.execute(
        "SELECT data_hora FROM login_attempts WHERE login=? AND sucesso=0 ORDER BY data_hora DESC LIMIT ?",
        (login, max_tent),
    ).fetchall()
    if falhas >= max_tent and len(ultimas_falhas) >= max_tent:
        ultima = ultimas_falhas[-1]["data_hora"]
        try:
            dt = datetime.fromisoformat(ultima)
        except Exception:
            dt = datetime.now()
        libera = dt + timedelta(minutes=bloqueio_min)
        restante = (libera - datetime.now()).total_seconds()
        if restante > 0:
            return True, int(restante)
    return False, 0


def gerar_token_recuperacao(conn, login):
    user = conn.execute("SELECT * FROM usuarios WHERE login=? AND ativo=1", (login,)).fetchone()
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    expira = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE usuarios SET token_recuperacao=?, token_expira=? WHERE id_usuario=?",
        (token, expira, user["id_usuario"]),
    )
    conn.commit()
    return token, user


def validar_token_recuperacao(conn, token):
    if not token:
        return None
    user = conn.execute("SELECT * FROM usuarios WHERE token_recuperacao=? AND ativo=1", (token,)).fetchone()
    if not user:
        return None
    if not user["token_expira"]:
        return None
    try:
        expira = datetime.fromisoformat(user["token_expira"])
    except Exception:
        return None
    if expira < datetime.now():
        return None
    return user


def consumir_token_recuperacao(conn, id_usuario):
    conn.execute(
        "UPDATE usuarios SET token_recuperacao=NULL, token_expira=NULL WHERE id_usuario=?",
        (id_usuario,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Código de produto — SEQUENCIAL
# ---------------------------------------------------------------------------
def _proximo_numero_codigo(conn):
    """Devolve o próximo número inteiro disponível para um código PRD#####."""
    try:
        m1 = conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(codigo, 4) AS INTEGER)), 0) AS m "
            "FROM produtos WHERE codigo LIKE 'PRD%' "
            "AND CAST(SUBSTR(codigo, 4) AS INTEGER) > 0"
        ).fetchone()["m"] or 0
    except Exception:
        m1 = 0
    try:
        m2 = conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(codigo, 4) AS INTEGER)), 0) AS m "
            "FROM codigos_provisorios WHERE codigo LIKE 'PRD%' "
            "AND CAST(SUBSTR(codigo, 4) AS INTEGER) > 0"
        ).fetchone()["m"] or 0
    except Exception:
        m2 = 0
    return max(int(m1), int(m2)) + 1

def gerar_codigo_produto(conn):
    """Gera o próximo código PRD##### sequencial (nunca aleatório)."""
    proximo = _proximo_numero_codigo(conn)
    while True:
        codigo = f"PRD{proximo:05d}"
        existe = conn.execute(
            "SELECT 1 FROM produtos WHERE codigo=?", (codigo,)
        ).fetchone()
        existe_prov = conn.execute(
            "SELECT 1 FROM codigos_provisorios WHERE codigo=?", (codigo,)
        ).fetchone()
        if not existe and not existe_prov:
            return codigo
        proximo += 1

def reservar_codigo_provisorio(conn, sessao_id):
    """Reserva um código provisório para a sessão."""
    # Limpa órfãos antigos (>24h) — antes eram só 1 min, causando troca
    # de código inesperada entre abas abertas.
    limite_limpeza = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        conn.execute(
            "UPDATE codigos_provisorios SET usado=1 WHERE usado=0 AND data_criacao < ?",
            (limite_limpeza,),
        )
        conn.commit()
    except Exception:
        pass

    existente = conn.execute(
        "SELECT codigo FROM codigos_provisorios "
        "WHERE sessao_id = ? AND usado = 0 "
        "ORDER BY id_codigo DESC LIMIT 1",
        (sessao_id,),
    ).fetchone()
    if existente:
        return existente["codigo"]

    codigo = gerar_codigo_produto(conn)
    conn.execute(
        "INSERT INTO codigos_provisorios (codigo, sessao_id, usado, data_criacao) "
        "VALUES (?, ?, 0, ?)",
        (codigo, sessao_id, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return codigo


def liberar_codigo_provisorio(conn, sessao_id, codigo=None):
    if codigo:
        conn.execute(
            "UPDATE codigos_provisorios SET usado=1 WHERE sessao_id=? AND codigo=?",
            (sessao_id, codigo),
        )
    else:
        conn.execute(
            "UPDATE codigos_provisorios SET usado=1 WHERE sessao_id=? AND usado=0", (sessao_id,)
        )
    conn.commit()


def usar_codigo_provisorio(conn, sessao_id, codigo):
    conn.execute(
        "UPDATE codigos_provisorios SET usado=1 WHERE sessao_id=? AND codigo=?",
        (sessao_id, codigo),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Conversão de unidades
# ---------------------------------------------------------------------------
def converter_para_unidade_base(quantidade_original, qtd_por_unidade, unidade_original="Unidade"):
    """Converte para unidade base. Aceita decimais apenas para kg."""
    if unidade_original not in UNIDADES_VALIDAS:
        raise RegraNegocioError("Unidade inválida.")

    try:
        qtd_float = float(quantidade_original)
    except (TypeError, ValueError):
        raise RegraNegocioError("Quantidade inválida.")
    if not math.isfinite(qtd_float) or qtd_float <= 0:
        raise RegraNegocioError("Quantidade deve ser maior que zero.")

    if unidade_original in UNIDADES_DECIMAIS:
        return round(qtd_float, 3)

    if not qtd_float.is_integer():
        raise RegraNegocioError(
            f"Quantidade inválida para '{unidade_original}': deve ser um número inteiro."
        )
    qtd = int(qtd_float)

    if unidade_original == "Unidade":
        return qtd

    try:
        fator = int(qtd_por_unidade)
    except (TypeError, ValueError):
        raise RegraNegocioError("Quantidade por unidade inválida.")
    if fator <= 0:
        raise RegraNegocioError("Quantidade por unidade deve ser positiva.")
    return qtd * fator


def saldo_atual(conn, id_produto):
    """Devolve o saldo actual (sempre float — suporta kg)."""
    row = conn.execute("SELECT saldo_atual FROM estoque WHERE id_produto=?", (id_produto,)).fetchone()
    if not row:
        return 0.0
    try:
        return float(row["saldo_atual"] or 0)
    except (TypeError, ValueError):
        return 0.0


def _garantir_linha_estoque(conn, id_produto):
    row = conn.execute("SELECT 1 FROM estoque WHERE id_produto=?", (id_produto,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO estoque (id_produto, saldo_atual, ultima_atualizacao) VALUES (?,0,?)",
            (id_produto, datetime.now().isoformat(timespec="seconds")),
        )


# ---------------------------------------------------------------------------
# Atualização de produto → refletir no estoque
# ---------------------------------------------------------------------------
def atualizar_produto_refletir_estoque(conn, id_produto, dados_antigos, dados_novos):
    _garantir_linha_estoque(conn, id_produto)
    agora = datetime.now().isoformat(timespec="seconds")
    campos_alterados = {}

    old_qpu = int(dados_antigos.get("qtd_por_unidade") or 1)
    new_qpu = int(dados_novos.get("qtd_por_unidade") or 1)
    new_unidade = dados_novos.get("unidade", "Unidade")
    if new_unidade == "Unidade":
        new_qpu = 1

    if old_qpu != new_qpu:
        campos_alterados["qtd_por_unidade"] = (old_qpu, new_qpu)

    for campo in ["preco_custo", "preco_venda", "estoque_minimo", "estoque_maximo"]:
        old_v = dados_antigos.get(campo)
        new_v = dados_novos.get(campo)
        if old_v != new_v:
            campos_alterados[campo] = (old_v, new_v)

    if "preco_custo" in campos_alterados:
        conn.execute("UPDATE estoque SET ultima_atualizacao=? WHERE id_produto=?", (agora, id_produto))
    return campos_alterados


# ---------------------------------------------------------------------------
# Código de segurança
# ---------------------------------------------------------------------------
def validar_codigo_alteracao_produto(codigo_fornecido):
    codigo_cadastrado = get_config("codigo_alteracao_produto", "")
    if not codigo_cadastrado:
        return True
    try:
        return check_password_hash(codigo_cadastrado, codigo_fornecido or "")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Registrar entrada (compra) — usa float (kg)
# ---------------------------------------------------------------------------
def registrar_entrada(conn, id_produto, quantidade_original, unidade_original,
                       preco_unitario, id_usuario, nota_fiscal=None,
                       fornecedor=None, observacoes=None, data_hora=None,
                       ip_address=None):
    if not validar_produto_cadastrado(conn, id_produto):
        raise RegraNegocioError("Selecione um produto cadastrado e ativo.")
    if unidade_original not in UNIDADES_VALIDAS:
        raise RegraNegocioError("Unidade inválida.")

    produto = conn.execute("SELECT * FROM produtos WHERE id_produto=?", (id_produto,)).fetchone()
    if not produto:
        raise RegraNegocioError("Produto não encontrado.")

    # FIX: aceita também o caso em que o produto é 'kg' e a unidade escolhida
    # é 'kg', mas rejeita produtos NÃO-kg a serem movimentados em kg.
    if unidade_original == "kg" and produto["unidade"] != "kg":
        raise RegraNegocioError(
            f"Produto em '{produto['unidade']}' não pode ser movimentado em kg."
        )
    if produto["unidade"] in ("Unidade", "kg") and unidade_original != produto["unidade"]:
        raise RegraNegocioError(
            f"Este produto só pode ser movimentado em '{produto['unidade']}'."
        )

    qtd_base = converter_para_unidade_base(
        quantidade_original, produto["qtd_por_unidade"], unidade_original)
    if qtd_base <= 0:
        raise RegraNegocioError("Quantidade de entrada deve ser maior que zero.")

    try:
        preco_float = float(preco_unitario)
    except (TypeError, ValueError):
        raise RegraNegocioError("Preço unitário inválido.")
    if preco_float < 0:
        raise RegraNegocioError("Preço unitário não pode ser negativo.")

    qtd_original_num = float(quantidade_original)
    valor_total = arredondar_moeda(qtd_original_num * preco_float)
    preco_unitario_base = arredondar_moeda(valor_total / qtd_base) if qtd_base else 0.0

    _garantir_linha_estoque(conn, id_produto)
    saldo = saldo_atual(conn, id_produto)

    metodo = get_config("metodo_custeio", "media_ponderada")
    custo_atual = float(produto["preco_custo"] or 0)

    if metodo == "manual":
        novo_custo = custo_atual          # O cadastro NÃO muda
        atualizar_custo = False
        # Guardamos o preço real desta entrada no novo campo
        conn.execute(
            "UPDATE produtos SET ultimo_preco_entrada=? WHERE id_produto=?",
            (preco_unitario_base, id_produto)
        )
    elif metodo == "media_ponderada" and (saldo + qtd_base) > 0:
        novo_custo = (saldo * custo_atual + valor_total) / (saldo + qtd_base)
        novo_custo = arredondar_moeda(novo_custo)
        atualizar_custo = True
        # Actualiza ambos no modo CMP
        conn.execute(
            "UPDATE produtos SET ultimo_preco_entrada=? WHERE id_produto=?",
            (novo_custo, id_produto)
        )
    else:
        novo_custo = preco_unitario_base
        atualizar_custo = True
        conn.execute(
            "UPDATE produtos SET ultimo_preco_entrada=? WHERE id_produto=?",
            (preco_unitario_base, id_produto)
        )

    data_hora = data_hora or datetime.now().isoformat(timespec="seconds")

    cur = conn.execute(
        """INSERT INTO movimentos
           (tipo, id_produto, data_hora, quantidade, quantidade_original, unidade_original,
            preco_unitario, valor_total, id_usuario, nota_fiscal, fornecedor, observacoes, lucro)
           VALUES ('Entrada',?,?,?,?,?,?,?,?,?,?,?,0)""",
        (id_produto, data_hora, qtd_base, qtd_original_num, unidade_original,
         preco_unitario_base, valor_total, id_usuario, nota_fiscal,
         fornecedor or produto["fornecedor"], observacoes),
    )
    movimento_id = cur.lastrowid

    conn.execute(
        "UPDATE estoque SET saldo_atual = saldo_atual + ?, ultima_atualizacao=? WHERE id_produto=?",
        (qtd_base, data_hora, id_produto),
    )
    if atualizar_custo:
        conn.execute("UPDATE produtos SET preco_custo=? WHERE id_produto=?", (novo_custo, id_produto))

    registrar_auditoria(conn, id_usuario, "movimentos", movimento_id, "INSERT",
                        None, {"tipo": "Entrada", "id_produto": id_produto,
                               "quantidade": qtd_base, "valor_total": valor_total},
                        ip_address)
    conn.commit()
    return qtd_base


# ---------------------------------------------------------------------------
# Registrar saída (venda)
# ---------------------------------------------------------------------------
def registrar_saida(conn, id_produto, quantidade_original, unidade_original,
                     preco_unitario, id_usuario, cliente=None, forma_pagamento=None,
                     observacoes=None, data_hora=None, ip_address=None, commit=True,
                     codigo_venda=None):
    """Regista uma saída. Com commit=False a transacção fica aberta para o
    chamador (usado em vendas com vários itens, que têm de ser atómicas)."""
    if not validar_produto_cadastrado(conn, id_produto):
        raise RegraNegocioError("Selecione um produto cadastrado e ativo.")
    if unidade_original not in UNIDADES_VALIDAS:
        raise RegraNegocioError("Unidade inválida.")

    produto = conn.execute("SELECT * FROM produtos WHERE id_produto=?", (id_produto,)).fetchone()
    if not produto:
        raise RegraNegocioError("Produto não encontrado.")

    if unidade_original == "kg" and produto["unidade"] != "kg":
        raise RegraNegocioError(
            f"Produto em '{produto['unidade']}' não pode ser movimentado em kg."
        )
    if produto["unidade"] in ("Unidade", "kg") and unidade_original != produto["unidade"]:
        raise RegraNegocioError(
            f"Este produto só pode ser movimentado em '{produto['unidade']}'."
        )

    qtd_base = converter_para_unidade_base(
        quantidade_original, produto["qtd_por_unidade"], unidade_original)
    if qtd_base <= 0:
        raise RegraNegocioError("Quantidade de saída deve ser maior que zero.")

    try:
        preco_float = float(preco_unitario)
    except (TypeError, ValueError):
        raise RegraNegocioError("Preço unitário inválido.")
    if preco_float < 0:
        raise RegraNegocioError("Preço unitário não pode ser negativo.")

    qtd_original_num = float(quantidade_original)
    valor_total = arredondar_moeda(qtd_original_num * preco_float)
    preco_unitario_base = arredondar_moeda(valor_total / qtd_base) if qtd_base else 0.0

    _garantir_linha_estoque(conn, id_produto)
    saldo = saldo_atual(conn, id_produto)

    permitir_negativo = get_config("permitir_estoque_negativo", "0") == "1"

    # Usa o último preço de entrada se existir, senão o do cadastro
    custo_real = float(produto["ultimo_preco_entrada"] or produto["preco_custo"] or 0)
    custo_total_venda = arredondar_moeda(custo_real * qtd_base)
    lucro = arredondar_moeda(valor_total - custo_total_venda)

    data_hora = data_hora or datetime.now().isoformat(timespec="seconds")

    if permitir_negativo:
        conn.execute(
            "UPDATE estoque SET saldo_atual = saldo_atual - ?, ultima_atualizacao=? WHERE id_produto=?",
            (qtd_base, data_hora, id_produto),
        )
    else:
        cur_update = conn.execute(
            "UPDATE estoque SET saldo_atual = saldo_atual - ?, ultima_atualizacao=? "
            "WHERE id_produto=? AND saldo_atual >= ?",
            (qtd_base, data_hora, id_produto, qtd_base),
        )
        if cur_update.rowcount == 0:
            if commit:
                conn.rollback()
            raise RegraNegocioError(
                f"Estoque insuficiente para '{produto['nome']}'. "
                f"Disponível: {_descrever_qtd(produto, unidade_original, saldo)}; "
                f"pedido: {_descrever_qtd(produto, unidade_original, qtd_base)}."
            )

    cur = conn.execute(
        """INSERT INTO movimentos
           (tipo, id_produto, data_hora, quantidade, quantidade_original, unidade_original,
            preco_unitario, valor_total, id_usuario, cliente, forma_pagamento, observacoes, lucro,
            codigo_venda)
           VALUES ('Saida',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (id_produto, data_hora, qtd_base, qtd_original_num, unidade_original,
         preco_unitario_base, valor_total, id_usuario, cliente, forma_pagamento, observacoes, lucro,
         codigo_venda),
    )
    movimento_id = cur.lastrowid

    registrar_auditoria(conn, id_usuario, "movimentos", movimento_id, "INSERT",
                        None, {"tipo": "Saida", "id_produto": id_produto,
                               "quantidade": qtd_base, "valor_total": valor_total,
                               "lucro": lucro},
                        ip_address)
    if commit:
        conn.commit()

    novo_saldo = saldo - qtd_base
    alerta_minimo = novo_saldo < produto["estoque_minimo"]
    return {"qtd_base": qtd_base, "novo_saldo": novo_saldo, "alerta_minimo": alerta_minimo,
            "id_movimento": movimento_id, "codigo_venda": codigo_venda}


# ---------------------------------------------------------------------------
# Validação prévia de vendas (sem gravar) e gravação atómica
# ---------------------------------------------------------------------------
_ROTULO_UNIDADE = {"Unidade": "un", "kg": "kg", "Caixa": "cx", "Embalagem": "emb"}


def _descrever_qtd(produto, unidade, qtd_base):
    """'2 cx (48 un)', '2,5 kg', '45 un': a quantidade na unidade que o utilizador usou."""
    rotulo = _ROTULO_UNIDADE.get(unidade, unidade)
    qpu = float(produto["qtd_por_unidade"] or 1)
    base = round(float(qtd_base), 3)
    if unidade in ("Caixa", "Embalagem") and qpu > 1:
        return f"{round(base / qpu, 3):g} {rotulo} ({base:g} un)"
    return f"{base:g} {rotulo}"


def validar_itens_venda(conn, itens):
    """Valida TODAS as linhas de uma venda sem gravar nada.

    itens: lista de dicts {linha, id_produto, quantidade, unidade, preco_unitario}.
    Devolve (erros, avisos). Cada entrada tem linha, produto, codigo, campo e
    mensagem. Erros bloqueiam a venda; avisos pedem confirmação (margem negativa,
    preço zero). As linhas do mesmo produto somam-se ao verificar o estoque.
    """
    erros, avisos = [], []
    consumo = {}
    permitir_negativo = get_config("permitir_estoque_negativo", "0") == "1"

    for it in itens:
        linha = it.get("linha")
        produto = None

        def erro(campo, mensagem):
            erros.append({
                "linha": linha, "campo": campo, "mensagem": mensagem,
                "id_produto": it.get("id_produto"),
                "produto": produto["nome"] if produto else None,
                "codigo": produto["codigo"] if produto else None,
            })

        if not validar_produto_cadastrado(conn, it.get("id_produto")):
            erro("produto", "Produto inválido ou inativo.")
            continue
        produto = conn.execute("SELECT * FROM produtos WHERE id_produto=?",
                               (int(it["id_produto"]),)).fetchone()
        unidade = it.get("unidade")
        if unidade not in UNIDADES_VALIDAS:
            erro("unidade", "Unidade inválida.")
            continue
        if unidade == "kg" and produto["unidade"] != "kg":
            erro("unidade", f"Produto em '{produto['unidade']}' não pode ser vendido em kg.")
            continue
        if produto["unidade"] in ("Unidade", "kg") and unidade != produto["unidade"]:
            erro("unidade", f"Este produto só pode ser vendido em '{produto['unidade']}'.")
            continue
        try:
            qtd_base = converter_para_unidade_base(
                it.get("quantidade"), produto["qtd_por_unidade"], unidade)
        except RegraNegocioError as e:
            erro("quantidade", str(e))
            continue
        if qtd_base <= 0:
            erro("quantidade", "A quantidade deve ser maior que zero.")
            continue
        try:
            preco = float(it.get("preco_unitario"))
        except (TypeError, ValueError):
            erro("preco", "Preço unitário inválido.")
            continue
        if preco < 0:
            erro("preco", "O preço unitário não pode ser negativo.")
            continue

        if not permitir_negativo:
            row = conn.execute("SELECT saldo_atual FROM estoque WHERE id_produto=?",
                               (produto["id_produto"],)).fetchone()
            saldo = float(row["saldo_atual"] or 0) if row else 0.0
            ja_pedido = consumo.get(produto["id_produto"], 0.0)
            if ja_pedido + qtd_base > saldo + 1e-9:
                msg = (f"Estoque insuficiente. Disponível: {_descrever_qtd(produto, unidade, saldo)}; "
                       f"pedido nesta linha: {_descrever_qtd(produto, unidade, qtd_base)}")
                if ja_pedido:
                    msg += f", mais {_descrever_qtd(produto, unidade, ja_pedido)} noutras linhas do mesmo produto"
                erro("quantidade", msg + ".")
                continue
        consumo[produto["id_produto"]] = consumo.get(produto["id_produto"], 0.0) + qtd_base

        valor_total = arredondar_moeda(float(it["quantidade"]) * preco)
        custo_real = float(produto["ultimo_preco_entrada"] or produto["preco_custo"] or 0)
        lucro = arredondar_moeda(valor_total - arredondar_moeda(custo_real * qtd_base))
        base_aviso = {"linha": linha, "id_produto": produto["id_produto"],
                      "produto": produto["nome"], "codigo": produto["codigo"], "campo": "preco"}
        if preco == 0:
            avisos.append({**base_aviso, "mensagem": "Preço 0,00: a venda fica sem receita."})
        elif custo_real > 0 and lucro < 0:
            avisos.append({**base_aviso, "mensagem":
                           f"Margem negativa: prejuízo de {abs(lucro):.2f}. "
                           f"Confirme se o preço corresponde à unidade vendida ({unidade})."})
    return erros, avisos


def registrar_tentativa_falhada(conn, id_usuario, tipo, erros, ip_address=None):
    """Deixa rasto na auditoria de uma venda que não foi registada."""
    resumo = [{"linha": e.get("linha"), "produto": e.get("produto"), "mensagem": e.get("mensagem")}
              for e in erros][:20]
    registrar_auditoria(conn, id_usuario, "venda_falhada", None, "INSERT",
                        None, {"tipo": tipo, "erros": resumo}, ip_address)
    conn.commit()


def processar_venda_vista(conn, itens, id_usuario, erros_previos=None, cliente=None,
                          forma_pagamento=None, observacoes=None, data_hora=None,
                          ip_address=None, confirmar_avisos=False, apenas_validar=False):
    """Valida e (se tudo estiver certo) regista uma venda à vista, numa só transacção.

    estado: 'erros' (nada gravado), 'confirmacao' (há avisos por confirmar, nada
    gravado), 'validada' (apenas_validar) ou 'ok'. Se uma linha falhar durante a
    gravação (ex.: outro utilizador vendeu o mesmo produto entretanto), a transacção
    é desfeita e a linha é devolvida em 'erros', para o utilizador corrigir.
    """
    erros, avisos = validar_itens_venda(conn, itens)
    erros = list(erros_previos or []) + erros
    erros.sort(key=lambda e: (e.get("linha") is None, e.get("linha") or 0))
    if erros:
        if not apenas_validar:
            registrar_tentativa_falhada(conn, id_usuario, "venda_vista", erros, ip_address)
        return {"estado": "erros", "erros": erros, "avisos": avisos}
    if apenas_validar:
        return {"estado": "validada", "erros": [], "avisos": avisos}
    if avisos and not confirmar_avisos:
        return {"estado": "confirmacao", "erros": [], "avisos": avisos}

    alertas_minimo, margens_negativas, linha_atual = [], [], None
    codigo_venda = proximo_codigo_venda(conn)
    try:
        for it in itens:
            linha_atual = it
            res = registrar_saida(
                conn, id_produto=int(it["id_produto"]), quantidade_original=it["quantidade"],
                unidade_original=it["unidade"], preco_unitario=it["preco_unitario"],
                id_usuario=id_usuario, cliente=cliente, forma_pagamento=forma_pagamento,
                observacoes=observacoes, data_hora=data_hora, ip_address=ip_address,
                commit=False, codigo_venda=codigo_venda)
            if res["alerta_minimo"]:
                alertas_minimo.append(int(it["id_produto"]))
            mov = conn.execute(
                "SELECT m.lucro, p.nome FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto "
                "WHERE m.id_movimento=?", (res["id_movimento"],)).fetchone()
            if mov and float(mov["lucro"] or 0) < 0:
                margens_negativas.append((mov["nome"], float(mov["lucro"])))
        conn.commit()
    except RegraNegocioError as e:
        conn.rollback()
        prod = conn.execute("SELECT nome, codigo FROM produtos WHERE id_produto=?",
                            (linha_atual["id_produto"],)).fetchone() if linha_atual else None
        erros = [{"linha": linha_atual.get("linha") if linha_atual else None, "campo": "quantidade",
                  "mensagem": str(e), "id_produto": linha_atual.get("id_produto") if linha_atual else None,
                  "produto": prod["nome"] if prod else None, "codigo": prod["codigo"] if prod else None}]
        registrar_tentativa_falhada(conn, id_usuario, "venda_vista", erros, ip_address)
        return {"estado": "erros", "erros": erros, "avisos": avisos}
    except Exception:
        conn.rollback()
        raise
    return {"estado": "ok", "erros": [], "avisos": avisos, "sucessos": len(itens),
            "codigo_venda": codigo_venda,
            "alertas_minimo": alertas_minimo, "margens_negativas": margens_negativas}


# ---------------------------------------------------------------------------
# Situação do estoque / alertas
# ---------------------------------------------------------------------------
def situacao_estoque(saldo, minimo, maximo):
    if saldo < (minimo or 0):
        return "abaixo_minimo"
    if maximo and saldo > maximo:
        return "acima_maximo"
    return "normal"


def produtos_proximos_validade(conn):
    dias = int(get_config("dias_alerta_validade", "7"))
    limite = (datetime.now() + timedelta(days=dias)).date().isoformat()
    hoje = datetime.now().date().isoformat()
    rows = conn.execute(
        """SELECT p.*, e.saldo_atual FROM produtos p
           LEFT JOIN estoque e ON e.id_produto = p.id_produto
           WHERE p.ativo=1 AND p.validade IS NOT NULL AND p.validade != ''
             AND p.validade <= ? ORDER BY p.validade ASC""",
        (limite,),
    ).fetchall()
    resultado = []
    for r in rows:
        resultado.append({**dict(r), "vencido": r["validade"] < hoje})
    return resultado


def produtos_abaixo_minimo(conn):
    rows = conn.execute(
        """SELECT p.*, COALESCE(e.saldo_atual,0) AS saldo_atual FROM produtos p
           LEFT JOIN estoque e ON e.id_produto = p.id_produto
           WHERE p.ativo=1 AND COALESCE(e.saldo_atual,0) < p.estoque_minimo
           ORDER BY (p.estoque_minimo - COALESCE(e.saldo_atual,0)) DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def sugestoes_precos(conn, dias=30):
    desde = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")
    vendas = conn.execute(
        """SELECT id_produto, SUM(quantidade) AS total_vendido
           FROM movimentos WHERE tipo='Saida' AND data_hora >= ?
           GROUP BY id_produto""",
        (desde,),
    ).fetchall()
    if not vendas:
        return []
    medias = {v["id_produto"]: v["total_vendido"] for v in vendas}
    media_geral = sum(medias.values()) / len(medias)

    sugestoes = []
    for id_produto, total in medias.items():
        produto = conn.execute("SELECT * FROM produtos WHERE id_produto=?", (id_produto,)).fetchone()
        if not produto or not produto["ativo"]:
            continue
        if total > media_geral * 1.2:
            sugestoes.append({
                "produto": dict(produto), "total_vendido": total,
                "tipo": "aumento", "percentual": 5,
                "preco_sugerido": arredondar_moeda(produto["preco_venda"] * 1.05),
            })
        elif total < media_geral * 0.5:
            sugestoes.append({
                "produto": dict(produto), "total_vendido": total,
                "tipo": "reducao", "percentual": 10,
                "preco_sugerido": arredondar_moeda(produto["preco_venda"] * 0.90),
            })
    return sugestoes


# ---------------------------------------------------------------------------
# Balancete — usa float (kg)
# ---------------------------------------------------------------------------
def calcular_balancete(conn, periodo, data_inicio, data_fim, id_categoria=None, id_produto=None):
    dt_ini = data_inicio + "T00:00:00"
    dt_fim = data_fim + "T23:59:59"

    sql_prod = """
        SELECT p.*, c.nome AS categoria_nome
        FROM produtos p
        LEFT JOIN categorias c ON c.id_categoria = p.id_categoria
        WHERE (
            p.ativo = 1
            OR EXISTS (
                SELECT 1 FROM movimentos m
                WHERE m.id_produto = p.id_produto
                  AND m.data_hora <= ?
            )
        )
    """
    params = [dt_fim]
    if id_categoria:
        sql_prod += " AND p.id_categoria = ?"
        params.append(id_categoria)
    if id_produto:
        sql_prod += " AND p.id_produto = ?"
        params.append(id_produto)
    sql_prod += " ORDER BY p.nome"
    produtos = conn.execute(sql_prod, params).fetchall()

    linhas = []
    total_saldo_ini_qtd = 0.0
    total_entradas_qtd = 0.0
    total_saidas_qtd = 0.0
    total_saldo_fin_qtd = 0.0
    total_entradas_val = 0.0
    total_saidas_val = 0.0
    total_lucro = 0.0
    total_saldo_ini_valor_custo = 0.0
    total_saldo_ini_valor_venda = 0.0
    total_saldo_fin_valor_custo = 0.0

    for p in produtos:
        idp = p["id_produto"]

        row_ini = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN tipo='Entrada' THEN quantidade ELSE -quantidade END),0) AS s
               FROM movimentos WHERE id_produto=? AND data_hora < ?""",
            (idp, dt_ini),
        ).fetchone()
        saldo_ini = round(float(row_ini["s"] or 0), 3)

        ent_q = conn.execute(
            """SELECT COALESCE(SUM(quantidade),0) AS s,
                      COALESCE(SUM(valor_total),0) AS v
               FROM movimentos
               WHERE id_produto=? AND tipo='Entrada' AND data_hora BETWEEN ? AND ?""",
            (idp, dt_ini, dt_fim),
        ).fetchone()
        sai_q = conn.execute(
            """SELECT COALESCE(SUM(quantidade),0) AS s,
                      COALESCE(SUM(valor_total),0) AS v,
                      COALESCE(SUM(lucro),0) AS l
               FROM movimentos
               WHERE id_produto=? AND tipo='Saida' AND data_hora BETWEEN ? AND ?""",
            (idp, dt_ini, dt_fim),
        ).fetchone()

        entradas_qtd = round(float(ent_q["s"] or 0), 3)
        entradas_val = arredondar_moeda(ent_q["v"])
        saidas_qtd = round(float(sai_q["s"] or 0), 3)
        saidas_val = arredondar_moeda(sai_q["v"])
        lucro = arredondar_moeda(sai_q["l"])
        saldo_fin = round(saldo_ini + entradas_qtd - saidas_qtd, 3)

        custo_atual = float(p["preco_custo"] or 0)
        venda_atual = float(p["preco_venda"] or 0)
        saldo_ini_valor_custo = arredondar_moeda(saldo_ini * custo_atual)
        saldo_ini_valor_venda = arredondar_moeda(saldo_ini * venda_atual)
        saldo_fin_valor_custo = arredondar_moeda(saldo_fin * custo_atual)

        total_saldo_ini_qtd += saldo_ini
        total_entradas_qtd += entradas_qtd
        total_saidas_qtd += saidas_qtd
        total_saldo_fin_qtd += saldo_fin
        total_entradas_val += entradas_val
        total_saidas_val += saidas_val
        total_lucro += lucro
        total_saldo_ini_valor_custo += saldo_ini_valor_custo
        total_saldo_ini_valor_venda += saldo_ini_valor_venda
        total_saldo_fin_valor_custo += saldo_fin_valor_custo

        linhas.append({
            "id_produto": idp,
            "codigo": p["codigo"],
            "produto": p["nome"],
            "categoria": p["categoria_nome"] or "—",
            "unidade": p["unidade"],
            "saldo_inicial": saldo_ini,
            "saldo_inicial_qtd": saldo_ini,
            "saldo_inicial_valor_custo": saldo_ini_valor_custo,
            "saldo_inicial_valor_venda": saldo_ini_valor_venda,
            "entradas_qtd": entradas_qtd,
            "entradas_val": entradas_val,
            "entradas_valor": entradas_val,
            "entradas_valor_venda": entradas_val,
            "saidas_qtd": saidas_qtd,
            "saidas_val": saidas_val,
            "saidas_valor": saidas_val,
            "saidas_valor_venda": saidas_val,
            "saldo_final": saldo_fin,
            "saldo_final_qtd": saldo_fin,
            "saldo_final_valor_custo": saldo_fin_valor_custo,
            "lucro": lucro,
            "debito": entradas_val,
            "credito": saidas_val,
            "nome_produto": p["nome"],
            "codigo_produto": p["codigo"],
        })

    totais = {
        "saldo_inicial": round(total_saldo_ini_qtd, 3),
        "saldo_inicial_qtd": round(total_saldo_ini_qtd, 3),
        "saldo_inicial_valor_custo": arredondar_moeda(total_saldo_ini_valor_custo),
        "saldo_inicial_valor_venda": arredondar_moeda(total_saldo_ini_valor_venda),
        "entradas_qtd": round(total_entradas_qtd, 3),
        "entradas_val": arredondar_moeda(total_entradas_val),
        "entradas_valor": arredondar_moeda(total_entradas_val),
        "entradas_valor_venda": arredondar_moeda(total_entradas_val),
        "saidas_qtd": round(total_saidas_qtd, 3),
        "saidas_val": arredondar_moeda(total_saidas_val),
        "saidas_valor": arredondar_moeda(total_saidas_val),
        "saidas_valor_venda": arredondar_moeda(total_saidas_val),
        "saldo_final": round(total_saldo_fin_qtd, 3),
        "saldo_final_qtd": round(total_saldo_fin_qtd, 3),
        "saldo_final_valor_custo": arredondar_moeda(total_saldo_fin_valor_custo),
        "lucro": arredondar_moeda(total_lucro),
    }
    return linhas, totais


def calcular_extrato_categoria(conn, id_categoria, data_inicio, data_fim):
    dt_ini = data_inicio + "T00:00:00"
    dt_fim = data_fim + "T23:59:59"

    movs = conn.execute(
        """SELECT m.*, p.nome AS produto_nome, p.codigo AS produto_codigo,
                  c.nome AS categoria_nome
           FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
           LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
           WHERE p.id_categoria=? AND m.data_hora BETWEEN ? AND ?
           ORDER BY m.data_hora ASC, m.id_movimento ASC""",
        (id_categoria, dt_ini, dt_fim),
    ).fetchall()

    linhas = []
    # O saldo é por produto (kg, caixas e unidades não se somam) e parte do saldo
    # acumulado até ao dia anterior ao período, nunca de zero.
    saldos = {
        r["id_produto"]: round(float(r["s"] or 0), 3)
        for r in conn.execute(
            """SELECT m.id_produto,
                      SUM(CASE WHEN m.tipo='Entrada' THEN m.quantidade ELSE -m.quantidade END) AS s
               FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
               WHERE p.id_categoria=? AND m.data_hora < ?
               GROUP BY m.id_produto""",
            (id_categoria, dt_ini),
        ).fetchall()
    }
    entradas_acum = 0.0
    saidas_acum = 0.0
    for m in movs:
        sinal = 1 if m["tipo"] == "Entrada" else -1
        qtd = round(float(m["quantidade"] or 0), 3)
        saldo_prod = round(saldos.get(m["id_produto"], 0.0) + sinal * qtd, 3)
        saldos[m["id_produto"]] = saldo_prod
        if m["tipo"] == "Entrada":
            entradas_acum += float(m["valor_total"] or 0)
        else:
            saidas_acum += float(m["valor_total"] or 0)
        linhas.append({
            "data_hora": m["data_hora"],
            "tipo": m["tipo"],
            "codigo": m["produto_codigo"],
            "produto": m["produto_nome"],
            "quantidade": qtd,
            "unidade": m["unidade_original"],
            "preco_unitario": arredondar_moeda(m["preco_unitario"]),
            "valor_total": arredondar_moeda(m["valor_total"]),
            "saldo_acumulado": saldo_prod,
            "entradas_acum_valor": arredondar_moeda(entradas_acum),
            "saidas_acum_valor": arredondar_moeda(saidas_acum),
        })
    # O 2.º valor (saldo em quantidade da categoria) deixou de existir: seria uma
    # soma de unidades diferentes. Mantém-se None para não partir os chamadores.
    return linhas, None, arredondar_moeda(entradas_acum), arredondar_moeda(saidas_acum)


# ---------------------------------------------------------------------------
# Extrato por produto (cronológico)
# ---------------------------------------------------------------------------
def calcular_extrato_produto(conn, id_produto, data_inicio, data_fim):
    """Extrato cronológico de um produto."""
    produto = conn.execute(
        "SELECT * FROM produtos WHERE id_produto=?", (id_produto,)
    ).fetchone()
    if not produto:
        return None, [], {}

    dt_ini = (data_inicio or "1900-01-01") + "T00:00:00"
    dt_fim = (data_fim or "2999-12-31") + "T23:59:59"

    row_ini = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN tipo='Entrada' THEN quantidade ELSE -quantidade END),0) AS s
           FROM movimentos
           WHERE id_produto=? AND data_hora < ?""",
        (id_produto, dt_ini),
    ).fetchone()
    saldo_ini = round(float(row_ini["s"] or 0), 3)

    movs = conn.execute(
        """SELECT m.*, u.nome AS usuario_nome
           FROM movimentos m
           LEFT JOIN usuarios u ON u.id_usuario = m.id_usuario
           WHERE m.id_produto = ? AND m.data_hora BETWEEN ? AND ?
           ORDER BY m.data_hora ASC, m.id_movimento ASC""",
        (id_produto, dt_ini, dt_fim),
    ).fetchall()

    linhas = []
    saldo = saldo_ini
    tot_ent_qtd_orig = 0.0
    tot_ent_val = 0.0
    tot_sai_qtd_orig = 0.0
    tot_sai_val = 0.0
    tot_lucro = 0.0

    for m in movs:
        qtd_base = round(float(m["quantidade"] or 0), 3)
        qtd_orig = round(float(m["quantidade_original"] or 0), 3)
        sinal = 1 if m["tipo"] == "Entrada" else -1
        saldo = round(saldo + sinal * qtd_base, 3)

        valor = float(m["valor_total"] or 0)
        if m["tipo"] == "Entrada":
            tot_ent_qtd_orig += qtd_orig
            tot_ent_val += valor
        else:
            tot_sai_qtd_orig += qtd_orig
            tot_sai_val += valor
            tot_lucro += float(m["lucro"] or 0)

        linhas.append({
            "data_hora": m["data_hora"],
            "tipo": m["tipo"],
            "documento": m["nota_fiscal"] or "—",
            "quantidade_original": qtd_orig,
            "unidade_original": m["unidade_original"],
            "quantidade_base": qtd_base,
            "preco_unitario": arredondar_moeda(m["preco_unitario"]),
            "valor_total": arredondar_moeda(valor),
            "lucro": arredondar_moeda(m["lucro"] or 0),
            "saldo_acumulado": saldo,
            "terceiro": m["fornecedor"] or m["cliente"] or "—",
            "usuario": m["usuario_nome"] or "—",
        })

    resumo = {
        "qtd_comprada": round(tot_ent_qtd_orig, 3),
        "valor_comprado": arredondar_moeda(tot_ent_val),
        "qtd_vendida": round(tot_sai_qtd_orig, 3),
        "valor_vendido": arredondar_moeda(tot_sai_val),
        "lucro_periodo": arredondar_moeda(tot_lucro),
        "saldo_inicial": saldo_ini,
        "saldo_final": saldo,
        "n_movimentos": len(linhas),
    }

    return dict(produto), linhas, resumo


# ---------------------------------------------------------------------------
# Cotações
# ---------------------------------------------------------------------------
def parse_numero_pt(valor, milhares=True):
    """Converte texto em número aceitando o formato português ('1.500,50').

    milhares=True  (dinheiro): '1.500' = 1500  (1 a 3 dígitos, ponto, exactamente 3 dígitos).
    milhares=False (quantidades, taxas): um único ponto é decimal, '2.500' = 2,5 (balança).
    A vírgula é sempre decimal. Devolve None se não for um número.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        return None
    tem_v, tem_p = "," in s, "." in s
    if tem_v and tem_p:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")      # 1.500,50
        else:
            s = s.replace(",", "")                        # 1,500.50
    elif tem_v:
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif tem_p:
        inteiro, _, frac = s.rpartition(".")
        so_milhares = s.count(".") > 1 or (
            len(frac) == 3 and inteiro.lstrip("+-").isdigit()
            and 1 <= len(inteiro.lstrip("+-")) <= 3 and inteiro.lstrip("+-").lstrip("0") != ""
        )
        if milhares and so_milhares:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def qtd_relatorio(valor):
    """Quantidade para relatórios e exports: inteira quando possível, sem truncar
    produtos vendidos ao peso (2,5 kg não pode aparecer como 2)."""
    try:
        q = float(valor or 0)
    except (TypeError, ValueError):
        return 0
    return int(q) if q.is_integer() else round(q, 3)


def _qtd_cotacao(valor):
    """Quantidade de um item de cotação: inteira quando possível, mas sem
    truncar produtos vendidos ao peso (2,5 kg não pode virar 2)."""
    qtd = float(valor)
    return int(qtd) if qtd.is_integer() else qtd


def _maior_sufixo_numerico(conn, tabela, coluna):
    """Maior número no fim de códigos como 'COT-0007' (0 se não houver)."""
    maior = 0
    for (valor,) in conn.execute(f"SELECT {coluna} FROM {tabela}").fetchall():
        m = re.search(r"(\d+)\s*$", str(valor or ""))
        if m:
            maior = max(maior, int(m.group(1)))
    return maior


def gerar_numero_cotacao(conn):
    prox = _maior_sufixo_numerico(conn, "cotacoes", "numero") + 1
    return f"COT-{prox:04d}"


def _calcular_total_itens(itens):
    total = 0.0
    for item in itens:
        qtd = _qtd_cotacao(item["quantidade"])
        preco = float(item["preco_unitario"])
        desconto = float(item.get("desconto_percentual", 0) or 0)
        total += qtd * preco * (1 - desconto / 100)
    return arredondar_moeda(total)


def criar_cotacao(conn, id_usuario, cliente_nome, cliente_email=None, cliente_telefone=None,
                  data_validade=None, observacoes=None, itens=None):
    if not itens:
        raise RegraNegocioError("A cotação deve ter pelo menos um item.")

    numero = gerar_numero_cotacao(conn)
    data_emissao = datetime.now().isoformat(timespec="seconds")
    if not data_validade:
        data_validade = (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")

    valor_total = _calcular_total_itens(itens)

    cur = conn.execute(
        """INSERT INTO cotacoes
           (numero, id_cliente, nome_cliente, email_cliente, telefone_cliente,
            data_emissao, data_validade, observacoes, status, valor_total, id_usuario)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (numero, None, cliente_nome, cliente_email, cliente_telefone,
         data_emissao, data_validade, observacoes, "Aberta", valor_total, id_usuario)
    )
    id_cotacao = cur.lastrowid

    for item in itens:
        qtd = _qtd_cotacao(item["quantidade"])
        preco = float(item["preco_unitario"])
        desconto = float(item.get("desconto_percentual", 0) or 0)
        subtotal = arredondar_moeda(qtd * preco * (1 - desconto / 100))
        conn.execute(
            """INSERT INTO itens_cotacao
               (id_cotacao, id_produto, quantidade, preco_unitario, desconto_percentual, valor_total)
               VALUES (?,?,?,?,?,?)""",
            (id_cotacao, item["id_produto"], qtd, arredondar_moeda(preco),
             desconto, subtotal)
        )

    conn.commit()
    registrar_auditoria(conn, id_usuario, "cotacoes", id_cotacao, "INSERT", None,
                        {"numero": numero, "cliente": cliente_nome, "valor_total": valor_total},
                        None)
    conn.commit()
    return id_cotacao


def obter_cotacao(conn, id_cotacao):
    cotacao = conn.execute("SELECT * FROM cotacoes WHERE id_cotacao=?", (id_cotacao,)).fetchone()
    if not cotacao:
        return None
    itens = conn.execute(
        """SELECT i.*, p.codigo, p.nome AS produto_nome
           FROM itens_cotacao i JOIN produtos p ON p.id_produto=i.id_produto
           WHERE i.id_cotacao=?""",
        (id_cotacao,)
    ).fetchall()
    return dict(cotacao), [dict(item) for item in itens]


def atualizar_cotacao(conn, id_cotacao, id_usuario, cliente_nome,
                      cliente_email=None, cliente_telefone=None,
                      data_validade=None, observacoes=None, itens=None):
    cotacao = conn.execute("SELECT * FROM cotacoes WHERE id_cotacao=?", (id_cotacao,)).fetchone()
    if not cotacao:
        raise RegraNegocioError("Cotação não encontrada.")
    if cotacao["status"] != "Aberta":
        raise RegraNegocioError("Apenas cotações abertas podem ser editadas.")
    if not itens:
        raise RegraNegocioError("A cotação deve ter pelo menos um item.")

    if not data_validade:
        data_validade = cotacao["data_validade"]

    valor_total = _calcular_total_itens(itens)

    conn.execute(
        """UPDATE cotacoes
           SET nome_cliente=?, email_cliente=?, telefone_cliente=?,
               data_validade=?, observacoes=?, valor_total=?
           WHERE id_cotacao=?""",
        (cliente_nome, cliente_email, cliente_telefone,
         data_validade, observacoes, valor_total, id_cotacao),
    )

    conn.execute("DELETE FROM itens_cotacao WHERE id_cotacao=?", (id_cotacao,))
    for item in itens:
        qtd = _qtd_cotacao(item["quantidade"])
        preco = float(item["preco_unitario"])
        desconto = float(item.get("desconto_percentual", 0) or 0)
        subtotal = arredondar_moeda(qtd * preco * (1 - desconto / 100))
        conn.execute(
            """INSERT INTO itens_cotacao
               (id_cotacao, id_produto, quantidade, preco_unitario, desconto_percentual, valor_total)
               VALUES (?,?,?,?,?,?)""",
            (id_cotacao, item["id_produto"], qtd, arredondar_moeda(preco),
             desconto, subtotal),
        )

    conn.commit()
    registrar_auditoria(conn, id_usuario, "cotacoes", id_cotacao, "UPDATE",
                        {"valor_total": cotacao["valor_total"]},
                        {"valor_total": valor_total, "n_itens": len(itens)},
                        None)
    conn.commit()
    return id_cotacao

def atualizar_status_cotacao(conn, id_cotacao, novo_status, id_usuario):
    cotacao = conn.execute("SELECT * FROM cotacoes WHERE id_cotacao=?", (id_cotacao,)).fetchone()
    if not cotacao:
        raise RegraNegocioError("Cotação não encontrada.")
    if cotacao["status"] in ("Aprovada", "Rejeitada", "Convertida"):
        raise RegraNegocioError("Cotação já está finalizada.")
    conn.execute("UPDATE cotacoes SET status=? WHERE id_cotacao=?", (novo_status, id_cotacao))
    registrar_auditoria(conn, id_usuario, "cotacoes", id_cotacao, "UPDATE",
                        {"status": cotacao["status"]}, {"status": novo_status}, None)
    conn.commit()


def converter_cotacao_em_venda(conn, id_cotacao, id_usuario):
    """Converte uma cotação APROVADA em vendas (saídas), de forma atómica."""
    res = obter_cotacao(conn, id_cotacao)
    if not res:
        raise RegraNegocioError("Cotação não encontrada.")
    cotacao, itens = res
    if cotacao["status"] != "Aprovada":
        raise RegraNegocioError("Apenas cotações aprovadas podem ser convertidas em venda.")

    vendas = _itens_cotacao_para_venda(conn, itens)
    codigo_venda = proximo_codigo_venda(conn)
    try:
        for item in vendas:
            registrar_saida(
                conn,
                id_produto=item["id_produto"],
                quantidade_original=item["quantidade"],
                unidade_original=item["unidade"],
                preco_unitario=item["preco_unitario"],
                id_usuario=id_usuario,
                cliente=cotacao["nome_cliente"],
                observacoes=f"Venda gerada da cotação {cotacao['numero']}",
                ip_address=None,
                commit=False,
                codigo_venda=codigo_venda,
            )
        conn.execute("UPDATE cotacoes SET status='Convertida' WHERE id_cotacao=?",
                     (id_cotacao,))
        registrar_auditoria(conn, id_usuario, "cotacoes", id_cotacao, "UPDATE",
                            {"status": cotacao["status"]}, {"status": "Convertida"}, None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Reset / formatação da base de dados
# ---------------------------------------------------------------------------
NIVEIS_RESET = {
    "movimentos": "Apagar apenas movimentos (mantém catálogo e usuários)",
    "catalogo":   "Apagar movimentos + catálogo (mantém usuários e configurações)",
    "total":      "Reset total — volta ao estado de fábrica",
}


def _checkpoint_wal(conn):
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    except Exception:
        pass


def fazer_backup_automatico(motivo="pre_reset"):
    """Cópia consistente da base de dados (API de backup do SQLite)."""
    import os
    from database import get_base_dir, copiar_base_dados
    try:
        backup_dir = os.path.join(get_base_dir(), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        destino = os.path.join(backup_dir, f"{motivo}_{ts}.db")
        copiar_base_dados(destino)
        return destino
    except Exception:
        return None


_TABELAS_DERIVADAS_DE_MOVIMENTOS = (
    "recibos", "contas_receber_itens", "contas_receber",
    "fechos_mensais_itens", "fechos_mensais",
    "auditoria_ajustes", "auditoria_contagens", "auditoria_divergencias", "auditorias",
    "itens_cotacao", "cotacoes", "inventarios", "movimentos",
    "codigos_provisorios", "login_attempts", "auditoria",
)


def _tabelas_existentes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _apagar_tabelas(conn, tabelas):
    existentes = _tabelas_existentes(conn)
    apagadas = [t for t in tabelas if t in existentes]
    for t in apagadas:
        conn.execute(f"DELETE FROM {t}")
    _reset_sequence(conn, apagadas)


def _reset_sequence(conn, tabelas):
    try:
        placeholders = ",".join("?" * len(tabelas))
        conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tabelas)
    except Exception:
        pass


def _apagar_movimentos(conn):
    _apagar_tabelas(conn, _TABELAS_DERIVADAS_DE_MOVIMENTOS)
    conn.execute("UPDATE estoque SET saldo_atual=0, ultima_atualizacao=?",
                 (datetime.now().isoformat(timespec="seconds"),))


def _apagar_catalogo(conn):
    _apagar_tabelas(conn, _TABELAS_DERIVADAS_DE_MOVIMENTOS
                    + ("estoque", "produtos", "categorias"))


def _reset_total(conn):
    from database import DEFAULT_CATEGORIAS, DEFAULT_CONFIG
    _apagar_tabelas(conn, _TABELAS_DERIVADAS_DE_MOVIMENTOS
                    + ("estoque", "produtos", "categorias", "usuarios", "configuracoes"))

    conn.execute(
        "INSERT INTO usuarios (nome, login, senha_hash, perfil, ativo) VALUES (?,?,?,?,1)",
        ("Administrador", "admin", generate_password_hash("admin123"), "Admin"),
    )
    conn.executemany(
        "INSERT INTO categorias (nome, descricao, ativo) VALUES (?,?,1)",
        DEFAULT_CATEGORIAS,
    )
    for chave, valor in DEFAULT_CONFIG.items():
        conn.execute("INSERT INTO configuracoes (chave, valor) VALUES (?,?)", (chave, valor))


def formatar_base_dados(conn, nivel, id_usuario_admin, ip_address=None):
    if nivel not in NIVEIS_RESET:
        raise RegraNegocioError("Nível de reset inválido.")

    backup_path = fazer_backup_automatico(motivo=f"pre_reset_{nivel}")
    if not backup_path:
        raise RegraNegocioError(
            "Não foi possível criar o backup de segurança. Reset cancelado — "
            "nada foi apagado.")

    try:
        if nivel == "movimentos":
            _apagar_movimentos(conn)
        elif nivel == "catalogo":
            _apagar_catalogo(conn)
        else:
            _reset_total(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "nivel": nivel,
        "backup_path": backup_path,
        "mensagem": NIVEIS_RESET[nivel],
    }


# ---------------------------------------------------------------------------
# Vendas a Crédito / Contas a Receber / Recibos
# ---------------------------------------------------------------------------
def gerar_numero_recibo(conn):
    prox = _maior_sufixo_numerico(conn, "recibos", "numero") + 1
    return f"REC-{prox:05d}"


def proximo_codigo_venda(conn):
    """Código curto e sequencial de uma venda à vista: V-000123."""
    n = 0
    for sql in ("SELECT MAX(CAST(SUBSTR(codigo_venda,3) AS INTEGER)) m FROM movimentos WHERE codigo_venda LIKE 'V-%'",
                "SELECT MAX(CAST(SUBSTR(codigo_venda,3) AS INTEGER)) m FROM vendas_anuladas WHERE codigo_venda LIKE 'V-%'"):
        r = conn.execute(sql).fetchone()
        n = max(n, int(r["m"] or 0))
    return f"V-{n + 1:06d}"


def obter_venda_por_codigo(conn, codigo):
    """Itens de uma venda (à vista ou fatura) a partir do código. None se não existir."""
    codigo = (codigo or "").strip()
    if not codigo:
        return None
    rows = conn.execute(
        """SELECT m.*, p.nome AS produto_nome, p.codigo AS produto_codigo, p.preco_custo,
                  u.nome AS usuario_nome
           FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
           LEFT JOIN usuarios u ON u.id_usuario=m.id_usuario
           WHERE m.tipo='Saida' AND UPPER(m.codigo_venda)=UPPER(?) ORDER BY m.id_movimento""",
        (codigo,)).fetchall()
    if not rows:
        return None
    conta = conn.execute(
        """SELECT * FROM contas_receber WHERE UPPER(numero_fatura)=UPPER(?)""", (codigo,)).fetchone()
    return {"codigo": rows[0]["codigo_venda"], "itens": [dict(r) for r in rows],
            "total": round(sum(float(r["valor_total"] or 0) for r in rows), 2),
            "data": rows[0]["data_hora"], "cliente": rows[0]["cliente"],
            "forma": rows[0]["forma_pagamento"], "id_usuario": rows[0]["id_usuario"],
            "usuario": rows[0]["usuario_nome"], "conta": dict(conta) if conta else None}


def anular_venda(conn, codigo, motivo, id_usuario, perfil, ip_address=None):
    """Anula uma venda pelo código: repõe o estoque e guarda o registo da anulação.

    Operador: só vendas próprias e do próprio dia. Gerente/Admin: qualquer venda.
    Faturas a crédito com recibos não podem ser anuladas."""
    from database import registrar_auditoria
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise RegraNegocioError("Indique o motivo da anulação (mínimo 5 caracteres).")
    venda = obter_venda_por_codigo(conn, codigo)
    if not venda:
        raise RegraNegocioError(f"Não existe nenhuma venda com o código '{(codigo or '').strip()}'.")
    if perfil not in ("Admin", "Gerente"):
        if venda["id_usuario"] != id_usuario:
            raise RegraNegocioError("Só pode anular vendas registadas por si. Peça a um Gerente.")
        if (venda["data"] or "")[:10] != datetime.now().date().isoformat():
            raise RegraNegocioError("Só pode anular vendas do próprio dia. Peça a um Gerente.")

    conta = venda["conta"]
    if conta:
        n_rec = conn.execute("SELECT COUNT(*) c FROM recibos WHERE id_conta=?",
                             (conta["id_conta"],)).fetchone()["c"]
        if n_rec or float(conta["valor_pago"] or 0) > 0.001:
            raise RegraNegocioError("Esta fatura já tem pagamentos registados e não pode ser anulada.")

    agora = datetime.now().isoformat(timespec="seconds")
    itens_snap = [{"produto": i["produto_nome"], "codigo": i["produto_codigo"],
                   "quantidade": i["quantidade_original"], "unidade": i["unidade_original"],
                   "preco": i["preco_unitario"], "valor": i["valor_total"], "lucro": i["lucro"]}
                  for i in venda["itens"]]
    custo_total = round(sum(float(i["valor_total"] or 0) - float(i["lucro"] or 0) for i in venda["itens"]), 2)
    try:
        for i in venda["itens"]:
            _garantir_linha_estoque(conn, i["id_produto"])
            conn.execute("UPDATE estoque SET saldo_atual = saldo_atual + ?, ultima_atualizacao=? WHERE id_produto=?",
                         (i["quantidade"], agora, i["id_produto"]))
        ids = [i["id_movimento"] for i in venda["itens"]]
        marcas = ",".join("?" * len(ids))
        if conta:
            conn.execute("DELETE FROM contas_receber_itens WHERE id_conta=?", (conta["id_conta"],))
            conn.execute("DELETE FROM contas_receber WHERE id_conta=?", (conta["id_conta"],))
        conn.execute(f"DELETE FROM movimentos WHERE id_movimento IN ({marcas})", ids)
        cur = conn.execute(
            """INSERT INTO vendas_anuladas (codigo_venda, tipo, id_conta, cliente, forma_pagamento, data_venda,
                   valor_total, custo_total, itens_json, motivo, id_usuario_venda, id_usuario_anulou, data_anulacao)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venda["codigo"], "Credito" if conta else "Vista", conta["id_conta"] if conta else None,
             venda["cliente"], venda["forma"], venda["data"], venda["total"], custo_total,
             __import__("json").dumps(itens_snap, ensure_ascii=False), motivo,
             venda["id_usuario"], id_usuario, agora))
        registrar_auditoria(conn, id_usuario, "vendas_anuladas", cur.lastrowid, "INSERT", None,
                            {"codigo": venda["codigo"], "valor": venda["total"], "motivo": motivo,
                             "itens": itens_snap}, ip_address)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"codigo": venda["codigo"], "valor": venda["total"], "itens": len(venda["itens"])}


def gerar_numero_fatura(conn):
    row = conn.execute("SELECT COUNT(*) AS c FROM contas_receber").fetchone()
    return f"FT-{datetime.now().year}-{row['c']+1:05d}"


def registrar_venda_credito(conn, itens, id_usuario, nome_cliente,
                             nif_cliente=None, endereco_cliente=None,
                             localidade_cliente=None, pais_cliente=None,
                             email_cliente=None, telefone_cliente=None,
                             data_vencimento=None, condicao_pagamento=None,
                             isento_iva=False, taxa_iva=None,
                             observacoes=None, data_venda=None,
                             ip_address=None, commit=True):
    """Cria UMA fatura (conta_receber) com vários itens e devolve o resumo.

    É atómica: ou grava tudo (saídas de stock, fatura, itens, número e
    auditoria) ou não grava nada."""
    from database import registrar_auditoria, get_config

    if not itens:
        raise RegraNegocioError("A fatura deve ter pelo menos um item.")
    if not nome_cliente or not nome_cliente.strip():
        raise RegraNegocioError("Nome do cliente é obrigatório.")

    if isento_iva:
        taxa_iva_efetiva = 0.0
    else:
        if taxa_iva is None:
            try:
                taxa_iva_efetiva = float(get_config("taxa_iva_padrao", "16"))
            except (TypeError, ValueError):
                taxa_iva_efetiva = 16.0
        else:
            try:
                taxa_iva_efetiva = float(taxa_iva)
            except (TypeError, ValueError):
                raise RegraNegocioError("Taxa de IVA inválida.")
        if not 0 <= taxa_iva_efetiva <= 100:
            raise RegraNegocioError("Taxa de IVA deve estar entre 0 e 100.")

    try:
        movs_criados = []
        valor_total = 0.0
        data_emissao_final = data_venda
        for it in itens:
            res = registrar_saida(
                conn, id_produto=it["id_produto"],
                quantidade_original=it["quantidade"],
                unidade_original=it["unidade"],
                preco_unitario=it["preco_unitario"],
                id_usuario=id_usuario,
                cliente=nome_cliente,
                forma_pagamento="Credito",
                observacoes=observacoes,
                data_hora=data_venda,
                ip_address=ip_address,
                commit=False,
            )
            mov = conn.execute(
                "SELECT id_movimento, id_produto, valor_total, data_hora, "
                "       quantidade, quantidade_original, unidade_original, preco_unitario "
                "FROM movimentos WHERE id_movimento=?",
                (res["id_movimento"],),
            ).fetchone()
            movs_criados.append(dict(mov))
            valor_total += float(mov["valor_total"] or 0)
            data_emissao_final = mov["data_hora"]

        valor_total = arredondar_moeda(valor_total)
        data_emissao_final = data_emissao_final or datetime.now().isoformat(timespec="seconds")

        numero_fatura = _proximo_numero_fatura(conn)
        cur = conn.execute(
            """INSERT INTO contas_receber
               (id_movimento, nome_cliente, nif_cliente, endereco_cliente,
                localidade_cliente, pais_cliente, email_cliente, telefone_cliente,
                valor_total, valor_pago, saldo, data_emissao, data_vencimento,
                status, observacoes, id_usuario, numero_fatura,
                isento_iva, taxa_iva, condicao_pagamento)
               VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?, 'Aberta', ?,?,?,?,?,?)""",
            (movs_criados[0]["id_movimento"], nome_cliente, nif_cliente,
             endereco_cliente, localidade_cliente, pais_cliente,
             email_cliente, telefone_cliente,
             valor_total, valor_total, data_emissao_final, data_vencimento,
             observacoes, id_usuario, numero_fatura,
             1 if isento_iva else 0, taxa_iva_efetiva, condicao_pagamento),
        )
        id_conta = cur.lastrowid
        for m in movs_criados:
            conn.execute("UPDATE movimentos SET codigo_venda=? WHERE id_movimento=?",
                         (numero_fatura, m["id_movimento"]))

        for m in movs_criados:
            conn.execute(
                """INSERT INTO contas_receber_itens
                   (id_conta, id_movimento, id_produto, quantidade_original,
                    unidade_original, quantidade_base, preco_unitario, valor_total)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (id_conta, m["id_movimento"], m["id_produto"],
                 m["quantidade_original"], m["unidade_original"],
                 m["quantidade"], m["preco_unitario"], m["valor_total"]),
            )

        registrar_auditoria(conn, id_usuario, "contas_receber", id_conta,
                            "INSERT", None,
                            {"cliente": nome_cliente, "valor": valor_total,
                             "numero_fatura": numero_fatura,
                             "n_itens": len(movs_criados)},
                            ip_address)
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"id_conta": id_conta, "valor": valor_total,
            "n_itens": len(movs_criados), "numero_fatura": numero_fatura}


def _proximo_numero_fatura(conn):
    """Gera 'FT 2026/00001' e incrementa o contador na config."""
    from database import get_config
    serie = get_config("serie_fatura", "FT") or "FT"
    ano = datetime.now().year
    row = conn.execute(
        "SELECT valor FROM configuracoes WHERE chave='proximo_num_fatura'"
    ).fetchone()
    try:
        num = int(str(row["valor"]).strip()) if row else 1
    except (TypeError, ValueError):
        num = 1
    num = max(num, _maior_sufixo_numerico(conn, "contas_receber", "numero_fatura") + 1)
    numero = f"{serie} {ano}/{num:05d}"
    conn.execute(
        "INSERT INTO configuracoes (chave, valor) VALUES ('proximo_num_fatura', ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
        (str(num + 1),),
    )
    return numero


def liquidar_conta(conn, id_conta, valor, forma_pagamento, id_usuario,
                    data_pagamento=None, observacoes=None, ip_address=None):
    from database import registrar_auditoria

    conta = conn.execute(
        "SELECT * FROM contas_receber WHERE id_conta=?", (id_conta,)
    ).fetchone()
    if not conta:
        raise RegraNegocioError("Conta a receber não encontrada.")
    if conta["status"] == "Cancelada":
        raise RegraNegocioError("Conta cancelada não pode ser liquidada.")
    if conta["status"] == "Liquidada":
        raise RegraNegocioError("Conta já está liquidada.")

    valor = round(float(valor or 0), 2)
    if valor <= 0:
        raise RegraNegocioError("Valor do pagamento deve ser positivo.")
    if valor > float(conta["saldo"]) + 0.001:
        raise RegraNegocioError(
            f"Valor excede o saldo devedor ({float(conta['saldo']):.2f})."
        )

    data_pagamento = data_pagamento or datetime.now().isoformat(timespec="seconds")
    numero = gerar_numero_recibo(conn)

    cur = conn.execute(
        """INSERT INTO recibos (numero, id_conta, valor, data_pagamento,
                                forma_pagamento, observacoes, id_usuario)
           VALUES (?,?,?,?,?,?,?)""",
        (numero, id_conta, valor, data_pagamento, forma_pagamento,
         observacoes, id_usuario),
    )
    id_recibo = cur.lastrowid

    novo_pago = round(float(conta["valor_pago"]) + valor, 2)
    novo_saldo = round(float(conta["valor_total"]) - novo_pago, 2)
    novo_status = "Liquidada" if novo_saldo <= 0.001 else "Parcial"

    conn.execute(
        "UPDATE contas_receber SET valor_pago=?, saldo=?, status=? WHERE id_conta=?",
        (novo_pago, novo_saldo, novo_status, id_conta),
    )
    conn.commit()
    registrar_auditoria(conn, id_usuario, "recibos", id_recibo, "INSERT",
                        None, {"numero": numero, "valor": valor},
                        ip_address)
    conn.commit()
    return {"id_recibo": id_recibo, "numero": numero,
            "novo_saldo": novo_saldo, "status": novo_status}


def obter_conta_receber(conn, id_conta):
    """Devolve (conta_dict, itens_list, recibos_list) ou None."""
    conta = conn.execute(
        "SELECT * FROM contas_receber WHERE id_conta=?", (id_conta,)
    ).fetchone()
    if not conta:
        return None

    itens = conn.execute(
        """SELECT cri.*, p.codigo AS produto_codigo, p.nome AS produto_nome
           FROM contas_receber_itens cri
           JOIN produtos p ON p.id_produto=cri.id_produto
           WHERE cri.id_conta=?
           ORDER BY cri.id_item""",
        (id_conta,)
    ).fetchall()

    if not itens and conta["id_movimento"]:
        mov = conn.execute(
            """SELECT m.*, p.codigo AS produto_codigo, p.nome AS produto_nome
               FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
               WHERE m.id_movimento=?""",
            (conta["id_movimento"],)
        ).fetchone()
        if mov:
            itens = [{
                "id_conta": id_conta,
                "id_movimento": mov["id_movimento"],
                "id_produto": mov["id_produto"],
                "produto_codigo": mov["produto_codigo"],
                "produto_nome": mov["produto_nome"],
                "quantidade_original": mov["quantidade_original"],
                "unidade_original": mov["unidade_original"],
                "quantidade_base": mov["quantidade"],
                "preco_unitario": mov["preco_unitario"],
                "valor_total": mov["valor_total"],
            }]

    recibos = conn.execute(
        "SELECT * FROM recibos WHERE id_conta=? ORDER BY data_pagamento",
        (id_conta,)
    ).fetchall()

    return dict(conta), [dict(i) for i in itens], [dict(r) for r in recibos]


def cancelar_conta_receber(conn, id_conta, id_usuario, ip_address=None):
    """Elimina a fatura e REVERTE o stock dos produtos."""
    from database import registrar_auditoria

    conta = conn.execute(
        "SELECT * FROM contas_receber WHERE id_conta=?", (id_conta,)
    ).fetchone()
    if not conta:
        raise RegraNegocioError("Fatura não encontrada.")
    if conta["status"] == "Cancelada":
        raise RegraNegocioError("Fatura já está cancelada.")

    n_recibos = conn.execute(
        "SELECT COUNT(*) c FROM recibos WHERE id_conta=?", (id_conta,)
    ).fetchone()["c"]
    if n_recibos or float(conta["valor_pago"] or 0) > 0.001:
        raise RegraNegocioError(
            "Esta fatura já tem pagamentos (recibos) registados e não pode ser "
            "eliminada."
        )

    itens_snap = [dict(r) for r in conn.execute(
        "SELECT * FROM contas_receber_itens WHERE id_conta=?", (id_conta,)
    ).fetchall()]
    ids_mov = [r["id_movimento"] for r in itens_snap]
    if not ids_mov and conta["id_movimento"]:
        ids_mov = [conta["id_movimento"]]

    try:
        agora = datetime.now().isoformat(timespec="seconds")
        movs = []
        for id_mov in ids_mov:
            mov = conn.execute(
                "SELECT id_movimento, id_produto, quantidade FROM movimentos "
                "WHERE id_movimento=?", (id_mov,)
            ).fetchone()
            if mov:
                movs.append(mov)

        conn.execute("DELETE FROM contas_receber_itens WHERE id_conta=?", (id_conta,))
        conn.execute("DELETE FROM contas_receber WHERE id_conta=?", (id_conta,))

        for mov in movs:
            _garantir_linha_estoque(conn, mov["id_produto"])
            conn.execute(
                "UPDATE estoque SET saldo_atual = saldo_atual + ?, ultima_atualizacao=? "
                "WHERE id_produto=?",
                (mov["quantidade"], agora, mov["id_produto"]),
            )
            conn.execute("DELETE FROM movimentos WHERE id_movimento=?",
                         (mov["id_movimento"],))

        registrar_auditoria(conn, id_usuario, "contas_receber", id_conta,
                            "DELETE", {**dict(conta), "itens": itens_snap},
                            {"acao": "CANCELAMENTO_COM_REVERSAO_STOCK",
                             "movimentos_revertidos": len(movs)},
                            ip_address)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"movimentos_revertidos": len(movs)}


def obter_recibo(conn, id_recibo):
    r = conn.execute("SELECT * FROM recibos WHERE id_recibo=?", (id_recibo,)).fetchone()
    if not r:
        return None
    conta = conn.execute(
        "SELECT * FROM contas_receber WHERE id_conta=?", (r["id_conta"],)
    ).fetchone()
    return dict(r), (dict(conta) if conta else None)

def _itens_cotacao_para_venda(conn, itens_cot):
    """Converte itens de cotação em itens de venda, na unidade do próprio produto."""
    itens = []
    for ic in itens_cot:
        prod = conn.execute(
            "SELECT unidade FROM produtos WHERE id_produto=?", (ic["id_produto"],)
        ).fetchone()
        if not prod:
            raise RegraNegocioError(
                f"Produto {ic.get('codigo', ic['id_produto'])} já não existe.")
        desconto = float(ic.get("desconto_percentual") or 0)
        itens.append({
            "id_produto": ic["id_produto"],
            "quantidade": float(ic["quantidade"]),
            "unidade": prod["unidade"],
            "preco_unitario": float(ic["preco_unitario"]) * (1 - desconto / 100.0),
        })
    return itens


def faturar_cotacao(conn, id_cotacao, id_usuario, ip_address=None,
                    data_vencimento=None, isento_iva=False, taxa_iva=None):
    """Converte uma cotação em fatura a crédito (conta_receber), de forma atómica."""
    from database import registrar_auditoria

    res = obter_cotacao(conn, id_cotacao)
    if not res:
        raise RegraNegocioError("Cotação não encontrada.")
    cotacao, itens_cot = res
    if cotacao["status"] == "Convertida":
        raise RegraNegocioError("Cotação já convertida em venda.")
    if cotacao["status"] in ("Rejeitada",):
        raise RegraNegocioError("Cotação rejeitada não pode ser faturada.")
    if not itens_cot:
        raise RegraNegocioError("Cotação sem itens.")

    itens = _itens_cotacao_para_venda(conn, itens_cot)
    try:
        resultado = registrar_venda_credito(
            conn,
            itens=itens,
            id_usuario=id_usuario,
            nome_cliente=cotacao["nome_cliente"],
            nif_cliente=None,
            email_cliente=cotacao.get("email_cliente"),
            telefone_cliente=cotacao.get("telefone_cliente"),
            data_vencimento=data_vencimento,
            isento_iva=isento_iva,
            taxa_iva=taxa_iva,
            observacoes=f"Gerada da cotação {cotacao['numero']}",
            ip_address=ip_address,
            commit=False,
        )
        conn.execute("UPDATE cotacoes SET status='Convertida' WHERE id_cotacao=?",
                     (id_cotacao,))
        registrar_auditoria(conn, id_usuario, "cotacoes", id_cotacao, "UPDATE",
                            {"status": cotacao["status"]},
                            {"status": "Convertida", "id_conta_gerada": resultado["id_conta"]},
                            ip_address)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return resultado

# ===========================================================================
# AUDITORIA DE ESTOQUE
# ===========================================================================
def estoque_teorico(conn, id_produto, data_limite):
    """Estoque teórico até à data — usa fecho mensal quando disponível."""
    data_iso = (data_limite or "9999-12-31") + "T23:59:59"

    fecho = conn.execute(
        """SELECT f.* FROM fechos_mensais f
           WHERE f.data_fecho < ?
           ORDER BY f.data_fecho DESC LIMIT 1""",
        (data_limite,),
    ).fetchone()

    if fecho:
        item = conn.execute(
            """SELECT quantidade FROM fechos_mensais_itens
               WHERE id_fecho=? AND id_produto=?""",
            (fecho["id_fecho"], id_produto),
        ).fetchone()
        base = float(item["quantidade"] if item else 0)
        ponto_partida = fecho["data_fecho"] + "T23:59:59"
    else:
        base = 0.0
        ponto_partida = "1900-01-01T00:00:00"

    mov = conn.execute(
        """SELECT COALESCE(SUM(
                CASE WHEN tipo='Entrada' THEN quantidade
                     WHEN tipo='Saida'   THEN -quantidade
                     ELSE 0 END), 0) AS s
           FROM movimentos
           WHERE id_produto=? AND data_hora > ? AND data_hora <= ?""",
        (id_produto, ponto_partida, data_iso),
    ).fetchone()
    return round(base + float(mov["s"] or 0), 3)

def justificar_divergencia(conn, id_divergencia, motivo, id_usuario,
                            observacao=None, ip_address=None):
    from database import registrar_auditoria
    div = conn.execute(
        "SELECT * FROM auditoria_divergencias WHERE id_divergencia=?",
        (id_divergencia,),
    ).fetchone()
    if not div:
        raise RegraNegocioError("Divergência não encontrada.")
    if div["estado"] == "ajustada":
        raise RegraNegocioError("Divergência já ajustada — não pode ser justificada.")

    conn.execute(
        """UPDATE auditoria_divergencias
           SET estado='justificada', causa=?, observacao=?, motivo_justificacao=?
           WHERE id_divergencia=?""",
        (motivo, observacao, motivo, id_divergencia),
    )
    conn.commit()
    registrar_auditoria(conn, id_usuario, "auditoria_divergencias",
                        id_divergencia, "UPDATE",
                        {"estado": div["estado"]},
                        {"estado": "justificada", "motivo": motivo},
                        ip_address)
    conn.commit()


def solicitar_recontagem(conn, id_divergencia, id_usuario, ip_address=None):
    from database import registrar_auditoria
    div = conn.execute(
        "SELECT * FROM auditoria_divergencias WHERE id_divergencia=?",
        (id_divergencia,),
    ).fetchone()
    if not div:
        raise RegraNegocioError("Divergência não encontrada.")
    agora = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """UPDATE auditoria_divergencias
           SET recontagem_solicitada=1, recontagem_contador_id=?,
               recontagem_data=?, estado='pendente'
           WHERE id_divergencia=?""",
        (id_usuario, agora, id_divergencia),
    )
    conn.commit()
    registrar_auditoria(conn, id_usuario, "auditoria_divergencias",
                        id_divergencia, "UPDATE", None,
                        {"acao": "recontagem_solicitada"}, ip_address)
    conn.commit()


def marcar_aceite(conn, id_divergencia, id_usuario, ip_address=None):
    from database import registrar_auditoria

    div = conn.execute(
        "SELECT estado FROM auditoria_divergencias WHERE id_divergencia=?",
        (id_divergencia,),
    ).fetchone()
    if not div:
        raise RegraNegocioError("Divergência não encontrada.")
    if div["estado"] != "justificada":
        raise RegraNegocioError(
            "Só é possível aceitar divergências que já foram justificadas."
        )

    conn.execute(
        "UPDATE auditoria_divergencias SET estado='aceita' WHERE id_divergencia=?",
        (id_divergencia,),
    )
    conn.commit()
    registrar_auditoria(conn, id_usuario, "auditoria_divergencias",
                        id_divergencia, "UPDATE", {"estado": div["estado"]},
                        {"estado": "aceita"}, ip_address)
    conn.commit()

    
def registrar_perda(conn, id_produto, quantidade, motivo, id_usuario,
                     observacao=None, data_hora=None, ip_address=None):
    """Registra uma perda de estoque (vencimento/quebra/furto/consumo)."""
    from database import registrar_auditoria

    motivos_validos = ("Vencimento", "Quebra", "Furto", "Consumo Interno", "Outro")
    if motivo not in motivos_validos:
        raise RegraNegocioError(f"Motivo inválido. Use: {', '.join(motivos_validos)}")
    if motivo == "Outro" and not (observacao or "").strip():
        raise RegraNegocioError("Para o motivo 'Outro' é obrigatório justificar.")

    produto = conn.execute(
        "SELECT * FROM produtos WHERE id_produto=?", (id_produto,)
    ).fetchone()
    if not produto:
        raise RegraNegocioError("Produto não encontrado.")

    qtd = float(quantidade or 0)
    if qtd <= 0:
        raise RegraNegocioError("Quantidade deve ser positiva.")
    # FIX: validação de unidade (kg aceita decimais; restantes exigem inteiro).
    if produto["unidade"] not in UNIDADES_DECIMAIS and not qtd.is_integer():
        raise RegraNegocioError(
            f"Quantidade para '{produto['unidade']}' deve ser inteira."
        )

    saldo = saldo_atual(conn, id_produto)
    if qtd > saldo and get_config("permitir_estoque_negativo", "0") != "1":
        raise RegraNegocioError(
            f"Saldo insuficiente. Actual: {saldo}, solicitado: {qtd}."
        )

    data_hora = data_hora or datetime.now().isoformat(timespec="seconds")
    custo = float(produto["preco_custo"] or 0)
    valor_total = round(qtd * custo, 2)

    conn.execute(
        "UPDATE estoque SET saldo_atual = saldo_atual - ?, ultima_atualizacao=? "
        "WHERE id_produto=?",
        (qtd, data_hora, id_produto),
    )
    cur = conn.execute(
        """INSERT INTO movimentos
           (tipo, id_produto, data_hora, quantidade, quantidade_original,
            unidade_original, preco_unitario, valor_total, id_usuario,
            observacoes, lucro, subtipo)
           VALUES ('Saida',?,?,?,?,?,?,?,?,?,0,?)""",
        (id_produto, data_hora, qtd, qtd,
         produto["unidade"], custo, valor_total, id_usuario,
         f"Perda — {motivo}. {observacao or ''}".strip(),
         f"Perda_{motivo.replace(' ', '_')}"),
    )
    id_mov = cur.lastrowid
    conn.commit()
    registrar_auditoria(conn, id_usuario, "movimentos", id_mov, "INSERT",
                        None, {"tipo": "Perda", "motivo": motivo,
                               "id_produto": id_produto, "qtd": qtd},
                        ip_address)
    conn.commit()
    return id_mov

def abrir_auditoria(conn, data_inicio, data_fim, tipo, id_categoria,
                    responsavel_id, tolerancia_percentual=1.0,
                    tolerancia_valor_minimo=0.0, observacoes=None,
                    ip_address=None):
    from database import registrar_auditoria
    # FIX: validar datas (início <= fim)
    try:
        d_ini = datetime.strptime(str(data_inicio), "%Y-%m-%d")
        d_fim = datetime.strptime(str(data_fim), "%Y-%m-%d")
    except (TypeError, ValueError):
        raise RegraNegocioError("Datas inválidas.")
    if d_ini > d_fim:
        raise RegraNegocioError("Data de início deve ser anterior ou igual à data de fim.")

    agora = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO auditorias
           (data_inicio, data_fim, data_abertura, tipo, id_categoria,
            responsavel_id, estado, tolerancia_percentual,
            tolerancia_valor_minimo, observacoes)
           VALUES (?,?,?,?,?,?, 'aberta', ?,?,?)""",
        (data_inicio, data_fim, agora, tipo, id_categoria,
         responsavel_id, float(tolerancia_percentual or 1.0),
         float(tolerancia_valor_minimo or 0), observacoes),
    )
    id_aud = cur.lastrowid
    conn.commit()
    registrar_auditoria(conn, responsavel_id, "auditorias", id_aud,
                        "INSERT", None,
                        {"data_inicio": data_inicio, "data_fim": data_fim,
                         "tipo": tipo}, ip_address)
    conn.commit()
    return id_aud


def produtos_da_auditoria(conn, id_auditoria):
    aud = conn.execute(
        "SELECT * FROM auditorias WHERE id_auditoria=?", (id_auditoria,)
    ).fetchone()
    if not aud:
        return []
    sql = """SELECT p.id_produto, p.codigo, p.nome, p.unidade,
                    p.preco_custo, p.preco_venda, c.nome AS categoria_nome,
                    COALESCE(e.saldo_atual, 0) AS saldo_atual
             FROM produtos p
             LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
             LEFT JOIN estoque e   ON e.id_produto=p.id_produto
             WHERE p.ativo=1"""
    params = []
    if aud["tipo"] == "por_categoria" and aud["id_categoria"]:
        sql += " AND p.id_categoria=?"
        params.append(aud["id_categoria"])
    sql += " ORDER BY p.nome"
    return conn.execute(sql, params).fetchall()


def congelar_estoque_teorico(conn, id_auditoria, ip_address=None):
    """Cria linhas de divergência com o teórico congelado (contado=0)."""
    from database import registrar_auditoria
    aud = conn.execute(
        "SELECT * FROM auditorias WHERE id_auditoria=?", (id_auditoria,)
    ).fetchone()
    if not aud:
        raise RegraNegocioError("Auditoria não encontrada.")
    if aud["congelado_em"]:
        return

    produtos = produtos_da_auditoria(conn, id_auditoria)
    agora = datetime.now().isoformat(timespec="seconds")

    for p in produtos:
        teorico = estoque_teorico(conn, p["id_produto"], aud["data_fim"])
        custo = float(p["preco_custo"] or 0)
        conn.execute(
            """INSERT INTO auditoria_divergencias
               (id_auditoria, id_produto, estoque_teorico, estoque_contado,
                diferenca_qtd, custo_unitario, diferenca_valor, estado)
               VALUES (?,?,?,0,?,?,?, 'pendente')""",
            (id_auditoria, p["id_produto"], teorico,
             -teorico, custo, round(-teorico * custo, 2)),
        )

    conn.execute(
        "UPDATE auditorias SET congelado_em=?, estado='em_contagem' WHERE id_auditoria=?",
        (agora, id_auditoria),
    )
    conn.commit()
    registrar_auditoria(conn, aud["responsavel_id"], "auditorias", id_auditoria,
                        "UPDATE", {"estado": aud["estado"]},
                        {"estado": "em_contagem", "congelado_em": agora},
                        ip_address)
    conn.commit()


def registrar_contagem(conn, id_auditoria, id_produto, quantidade, id_usuario,
                       local=None, lote=None, validade=None, observacao=None,
                       contagem_cega=False, ip_address=None):
    from database import registrar_auditoria
    # FIX: validar unidade do produto (kg aceita decimais; restantes exigem inteiro)
    prod = conn.execute(
        "SELECT unidade FROM produtos WHERE id_produto=?", (id_produto,)
    ).fetchone()
    if prod and prod["unidade"] not in UNIDADES_DECIMAIS:
        if not float(quantidade).is_integer():
            raise RegraNegocioError(
                f"Produto em '{prod['unidade']}' exige quantidade inteira."
            )
    agora = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO auditoria_contagens
           (id_auditoria, id_produto, quantidade_contada, data_contagem,
            id_usuario_contador, local_prateleira, lote, validade,
            observacao, contagem_cega)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (id_auditoria, id_produto, float(quantidade), agora,
         id_usuario, local, lote, validade, observacao,
         1 if contagem_cega else 0),
    )
    id_contagem = cur.lastrowid

    div = conn.execute(
        "SELECT * FROM auditoria_divergencias WHERE id_auditoria=? AND id_produto=?",
        (id_auditoria, id_produto),
    ).fetchone()
    if div:
        teorico = float(div["estoque_teorico"] or 0)
        custo = float(div["custo_unitario"] or 0)
        dif_qtd = round(float(quantidade) - teorico, 3)
        dif_valor = round(dif_qtd * custo, 2)
        conn.execute(
            """UPDATE auditoria_divergencias
               SET estoque_contado=?, diferenca_qtd=?, diferenca_valor=?
               WHERE id_divergencia=?""",
            (float(quantidade), dif_qtd, dif_valor, div["id_divergencia"]),
        )
    conn.commit()
    registrar_auditoria(conn, id_usuario, "auditoria_contagens", id_contagem,
                        "INSERT", None,
                        {"id_auditoria": id_auditoria, "id_produto": id_produto,
                         "qtd": quantidade}, ip_address)
    conn.commit()
    return id_contagem


def divergencias_da_auditoria(conn, id_auditoria):
    return conn.execute(
        """SELECT d.*, p.codigo, p.nome, p.unidade, c.nome AS categoria_nome
           FROM auditoria_divergencias d
           JOIN produtos p ON p.id_produto=d.id_produto
           LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
           WHERE d.id_auditoria=?
           ORDER BY ABS(d.diferenca_valor) DESC""",
        (id_auditoria,),
    ).fetchall()

# ---------------------------------------------------------------------------
# RELATÓRIOS DE AUDITORIA
# ---------------------------------------------------------------------------
def resumo_executivo(conn, id_auditoria):
    aud = obter_auditoria(conn, id_auditoria)
    if not aud:
        return None

    produtos = produtos_da_auditoria(conn, id_auditoria)
    valor_custo = 0.0
    valor_venda = 0.0
    for p in produtos:
        qtd = estoque_teorico(conn, p["id_produto"], aud["data_fim"])
        valor_custo += qtd * float(p["preco_custo"] or 0)
        valor_venda += qtd * float(p["preco_venda"] or 0)

    divs = divergencias_da_auditoria(conn, id_auditoria)
    div_qtd = sum(abs(float(d["diferenca_qtd"] or 0)) for d in divs)
    div_valor = sum(abs(float(d["diferenca_valor"] or 0)) for d in divs)
    div_pct = (div_valor / valor_custo * 100) if valor_custo else 0
    pendentes = [d for d in divs if d["estado"] == "pendente"]

    return {
        "aud": aud,
        "n_produtos": len(produtos),
        "valor_custo": round(valor_custo, 2),
        "valor_venda": round(valor_venda, 2),
        "div_qtd": round(div_qtd, 3),
        "div_valor": round(div_valor, 2),
        "div_percentual": round(div_pct, 2),
        "n_divergencias": len(divs),
        "n_pendentes": len(pendentes),
        "top_divergencias": divs[:10],
    }


def reconciliacao_periodo(conn, data_inicio, data_fim, id_categoria=None):
    sql = """SELECT p.id_produto, p.codigo, p.nome, p.unidade,
                    p.preco_custo, c.nome AS categoria_nome
             FROM produtos p
             LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
             WHERE p.ativo=1"""
    params = []
    if id_categoria:
        sql += " AND p.id_categoria=?"
        params.append(id_categoria)
    sql += " ORDER BY p.nome"
    produtos = conn.execute(sql, params).fetchall()

    dt_ini = data_inicio + "T00:00:00"
    dt_fim = data_fim + "T23:59:59"

    # FIX: o saldo inicial deve ser calculado ANTES do primeiro dia do período.
    # Antes usava data_inicio, o que fazia com que os movimentos do dia 1 fossem
    # contados duas vezes (no inicial e nas entradas/saídas do período).
    try:
        dt_ini_obj = datetime.strptime(data_inicio, "%Y-%m-%d")
        dia_antes = (dt_ini_obj - timedelta(days=1)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        dia_antes = data_inicio

    linhas = []
    tot: "dict[str, float]" = {
        "ini": 0.0, "ent": 0.0, "sai": 0.0,
        "per": 0.0, "aj": 0.0, "teo": 0.0,
    }
    for p in produtos:
        ini = estoque_teorico(conn, p["id_produto"], dia_antes)

        movs = conn.execute(
            """SELECT tipo, subtipo, COALESCE(SUM(quantidade),0) AS s,
                      COALESCE(SUM(valor_total),0) AS v
               FROM movimentos
               WHERE id_produto=? AND data_hora BETWEEN ? AND ?
               GROUP BY tipo, subtipo""",
            (p["id_produto"], dt_ini, dt_fim),
        ).fetchall()

        ent = sum(float(m["s"]) for m in movs if m["tipo"] == "Entrada"
                  and (m["subtipo"] or "") != "Ajuste_Inventario")
        sai = sum(float(m["s"]) for m in movs if m["tipo"] == "Saida"
                  and not (m["subtipo"] or "").startswith("Perda_")
                  and (m["subtipo"] or "") != "Ajuste_Inventario")
        per = sum(float(m["s"]) for m in movs if m["tipo"] == "Saida"
                  and (m["subtipo"] or "").startswith("Perda_"))
        aj_ent = sum(float(m["s"]) for m in movs if m["tipo"] == "Entrada"
                     and (m["subtipo"] or "") == "Ajuste_Inventario")
        aj_sai = sum(float(m["s"]) for m in movs if m["tipo"] == "Saida"
                     and (m["subtipo"] or "") == "Ajuste_Inventario")
        aj = aj_ent - aj_sai

        teo = round(ini + ent - sai - per + aj, 3)
        custo = float(p["preco_custo"] or 0)

        linhas.append({
            "codigo": p["codigo"], "nome": p["nome"],
            "categoria": p["categoria_nome"] or "—",
            "unidade": p["unidade"],
            "inicial": ini, "entradas": ent, "saidas": sai,
            "perdas": per, "ajustes": aj, "teorico": teo,
            "custo_unit": custo,
            "valor_teorico": round(teo * custo, 2),
        })
        tot["ini"] += ini; tot["ent"] += ent; tot["sai"] += sai
        tot["per"] += per; tot["aj"] += aj; tot["teo"] += teo

    return linhas, tot

def curva_abc(conn, id_auditoria=None):
    """Classifica produtos em A (80% valor), B (15%), C (5%)."""
    produtos = conn.execute(
        """SELECT p.id_produto, p.codigo, p.nome, p.unidade,
                  p.preco_custo, p.preco_venda, c.nome AS categoria_nome
           FROM produtos p LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
           WHERE p.ativo=1"""
    ).fetchall()

    itens = []
    valor_total = 0.0
    for p in produtos:
        qtd = estoque_teorico(conn, p["id_produto"], datetime.now().date().isoformat())
        valor = round(qtd * float(p["preco_custo"] or 0), 2)
        valor_total += valor
        itens.append({
            "codigo": p["codigo"], "nome": p["nome"],
            "categoria": p["categoria_nome"] or "—",
            "qtd": qtd, "custo_unit": float(p["preco_custo"] or 0),
            "preco_venda": float(p["preco_venda"] or 0),
            "valor": valor,
        })

    itens.sort(key=lambda x: x["valor"], reverse=True)
    acum = 0.0
    for it in itens:
        acum += it["valor"]
        it["pct_acum"] = round(acum / valor_total * 100, 2) if valor_total else 0
        if it["pct_acum"] <= 80:
            it["classe"] = "A"
        elif it["pct_acum"] <= 95:
            it["classe"] = "B"
        else:
            it["classe"] = "C"

    return itens, valor_total


def perdas_e_validade(conn, dias_alerta=(30, 60, 90)):
    """Agrega perdas por motivo + produtos a vencer."""
    from datetime import timedelta
    motivos_rows = conn.execute(
        """SELECT subtipo, COUNT(*) AS n,
                  COALESCE(SUM(quantidade),0) AS qtd,
                  COALESCE(SUM(valor_total),0) AS valor
           FROM movimentos
           WHERE subtipo LIKE 'Perda_%'
           GROUP BY subtipo
           ORDER BY valor DESC"""
    ).fetchall()

    perdas = []
    for r in motivos_rows:
        motivo = (r["subtipo"] or "").replace("Perda_", "").replace("_", " ")
        perdas.append({
            "motivo": motivo, "n": r["n"],
            "qtd": float(r["qtd"] or 0),
            "valor": round(float(r["valor"] or 0), 2),
        })

    hoje = datetime.now().date()
    validade = []
    for dias in dias_alerta:
        limite = (hoje + timedelta(days=dias)).isoformat()
        rows = conn.execute(
            """SELECT p.codigo, p.nome, p.unidade, p.validade,
                      COALESCE(e.saldo_atual,0) AS saldo
               FROM produtos p LEFT JOIN estoque e ON e.id_produto=p.id_produto
               WHERE p.ativo=1 AND p.validade IS NOT NULL
                 AND p.validade <= ?
               ORDER BY p.validade""",
            (limite,),
        ).fetchall()
        for r in rows:
            try:
                venc = datetime.fromisoformat(r["validade"]).date()
                restantes = (venc - hoje).days
            except Exception:
                restantes = None
            validade.append({
                "codigo": r["codigo"], "nome": r["nome"],
                "unidade": r["unidade"], "validade": r["validade"],
                "saldo": float(r["saldo"] or 0),
                "dias_restantes": restantes,
                "limite_dias": dias,
            })

    vistos = set()
    validade_unicos = []
    for v in validade:
        if v["codigo"] in vistos: continue
        vistos.add(v["codigo"])
        validade_unicos.append(v)

    return perdas, validade_unicos


def excecoes(conn):
    """Deteta anomalias: estoque negativo, custo zero, ajustes sem justificação."""
    ex = []

    negativos = conn.execute(
        """SELECT p.codigo, p.nome, p.unidade, e.saldo_atual
           FROM estoque e JOIN produtos p ON p.id_produto=e.id_produto
           WHERE e.saldo_atual < 0 AND p.ativo=1"""
    ).fetchall()
    for r in negativos:
        ex.append({"tipo": "Estoque negativo", "produto": r["nome"],
                   "codigo": r["codigo"],
                   "detalhe": f"{float(r['saldo_atual']):.2f} {r['unidade']}"})

    custo_zero = conn.execute(
        """SELECT codigo, nome FROM produtos
           WHERE ativo=1 AND (preco_custo IS NULL OR preco_custo = 0)"""
    ).fetchall()
    for r in custo_zero:
        ex.append({"tipo": "Custo zerado", "produto": r["nome"],
                   "codigo": r["codigo"], "detalhe": "Sem custo definido"})

    sem_doc = conn.execute(
        """SELECT m.id_movimento, p.codigo, p.nome, m.data_hora, m.tipo
           FROM movimentos m JOIN produtos p ON p.id_produto=m.id_produto
           WHERE m.tipo='Entrada'
             AND (m.nota_fiscal IS NULL OR m.nota_fiscal='')
             AND COALESCE(m.subtipo,'') != 'Ajuste_Inventario'
           ORDER BY m.data_hora DESC LIMIT 50"""
    ).fetchall()
    for r in sem_doc:
        ex.append({"tipo": "Movimento sem documento", "produto": r["nome"],
                   "codigo": r["codigo"],
                   "detalhe": f"{r['tipo']} em {r['data_hora'][:10]} (#{r['id_movimento']})"})

    aj_sem_just = conn.execute(
        """SELECT a.id_ajuste, p.codigo, p.nome, a.quantidade_ajuste,
                  a.motivo, a.data_aprovacao
           FROM auditoria_ajustes a
           JOIN produtos p ON p.id_produto=a.id_produto
           WHERE (a.motivo IS NULL OR a.motivo='')"""
    ).fetchall()
    for r in aj_sem_just:
        ex.append({"tipo": "Ajuste sem justificação", "produto": r["nome"],
                   "codigo": r["codigo"],
                   "detalhe": f"{float(r['quantidade_ajuste']):.2f} em {r['data_aprovacao'][:10]}"})

    return ex

# ---------------------------------------------------------------------------
# FECHO MENSAL
# ---------------------------------------------------------------------------
def obter_fecho_anterior(conn, data_limite):
    """Devolve o fecho mais recente cuja data_fecho < data_limite."""
    return conn.execute(
        """SELECT * FROM fechos_mensais
           WHERE data_fecho < ?
           ORDER BY data_fecho DESC LIMIT 1""",
        (data_limite,),
    ).fetchone()


def gerar_fecho_mensal(conn, ano, mes, id_usuario, ip_address=None,
                        sobrescrever=False):
    """Gera um snapshot do estoque no último instante do mês indicado."""
    from database import registrar_auditoria
    from calendar import monthrange

    if mes < 1 or mes > 12:
        raise RegraNegocioError("Mês inválido (1-12).")

    existente = conn.execute(
        "SELECT * FROM fechos_mensais WHERE ano=? AND mes=?",
        (ano, mes),
    ).fetchone()
    if existente and not sobrescrever:
        raise RegraNegocioError(
            f"Já existe fecho para {mes:02d}/{ano}. Marque 'sobrescrever' se quiser refazer."
        )

    ultimo_dia = monthrange(ano, mes)[1]
    data_fecho = f"{ano}-{mes:02d}-{ultimo_dia}"

    produtos = conn.execute(
        """SELECT p.id_produto, p.codigo, p.nome, p.unidade,
                  p.preco_custo, p.preco_venda
           FROM produtos p WHERE p.ativo=1"""
    ).fetchall()

    agora = datetime.now().isoformat(timespec="seconds")

    if existente:
        conn.execute("DELETE FROM fechos_mensais_itens WHERE id_fecho=?",
                     (existente["id_fecho"],))
        conn.execute("DELETE FROM fechos_mensais WHERE id_fecho=?",
                     (existente["id_fecho"],))

    cur = conn.execute(
        """INSERT INTO fechos_mensais
           (ano, mes, data_fecho, data_geracao, id_usuario, observacoes)
           VALUES (?,?,?,?,?,?)""",
        (ano, mes, data_fecho, agora, id_usuario,
         f"Fecho gerado manualmente"),
    )
    id_fecho = cur.lastrowid

    total_custo = 0.0
    total_venda = 0.0
    n_itens = 0

    for p in produtos:
        qtd = estoque_teorico(conn, p["id_produto"], data_fecho)
        custo = float(p["preco_custo"] or 0)
        venda = float(p["preco_venda"] or 0)
        vc = round(qtd * custo, 2)
        vv = round(qtd * venda, 2)
        total_custo += vc
        total_venda += vv
        n_itens += 1

        conn.execute(
            """INSERT INTO fechos_mensais_itens
               (id_fecho, id_produto, quantidade, custo_unitario,
                preco_venda, valor_custo, valor_venda)
               VALUES (?,?,?,?,?,?,?)""",
            (id_fecho, p["id_produto"], qtd, custo, venda, vc, vv),
        )

    conn.execute(
        """UPDATE fechos_mensais
           SET valor_total_custo=?, valor_total_venda=?, num_produtos=?
           WHERE id_fecho=?""",
        (round(total_custo, 2), round(total_venda, 2), n_itens, id_fecho),
    )
    conn.commit()
    registrar_auditoria(conn, id_usuario, "fechos_mensais", id_fecho,
                        "INSERT", None,
                        {"ano": ano, "mes": mes, "total_custo": total_custo},
                        ip_address)
    conn.commit()
    return {"id_fecho": id_fecho, "total_custo": round(total_custo, 2),
            "total_venda": round(total_venda, 2), "n_itens": n_itens,
            "data_fecho": data_fecho}


def listar_fechos(conn):
    return conn.execute(
        """SELECT f.*, u.nome AS usuario_nome
           FROM fechos_mensais f
           LEFT JOIN usuarios u ON u.id_usuario=f.id_usuario
           ORDER BY f.ano DESC, f.mes DESC"""
    ).fetchall()


def fechar_auditoria(conn, id_auditoria, id_usuario, ip_address=None):
    """Gera ajustes acima da tolerância, aplica no estoque e fecha."""
    from database import registrar_auditoria
    aud = conn.execute(
        "SELECT * FROM auditorias WHERE id_auditoria=?", (id_auditoria,)
    ).fetchone()
    if not aud:
        raise RegraNegocioError("Auditoria não encontrada.")
    if aud["estado"] == "fechada":
        raise RegraNegocioError("Auditoria já fechada.")

    agora = datetime.now().isoformat(timespec="seconds")
    tol_pct = float(aud["tolerancia_percentual"] or 1.0)
    tol_val = float(aud["tolerancia_valor_minimo"] or 0)

    divergencias = divergencias_da_auditoria(conn, id_auditoria)
    total_qtd = 0.0
    total_valor = 0.0
    ajustes_gerados = 0

    for d in divergencias:
        # Produto não contado: não mexe no estoque.
        if d["estoque_contado"] is None or d["estado"] == "ajustada":
            continue
        teorico = float(d["estoque_teorico"] or 0)
        contado = float(d["estoque_contado"] or 0)
        dif_qtd = float(d["diferenca_qtd"] or 0)
        dif_val = float(d["diferenca_valor"] or 0)
        total_qtd += abs(dif_qtd)
        total_valor += abs(dif_val)

        # O estoque passa SEMPRE a refletir a contagem física, mesmo que a
        # divergência tenha sido justificada ou aceite (a justificação explica
        # a diferença, não a elimina).
        if abs(dif_qtd) < 1e-9:
            conn.execute(
                "UPDATE auditoria_divergencias SET estado='aceita' WHERE id_divergencia=?",
                (d["id_divergencia"],),
            )
            continue

        ajuste_qtd = round(contado - teorico, 3)
        custo = float(d["custo_unitario"] or 0)
        if custo <= 0:
            pc = conn.execute("SELECT preco_custo FROM produtos WHERE id_produto=?",
                              (d["id_produto"],)).fetchone()
            custo = float(pc["preco_custo"] or 0) if pc else 0.0

        cur = conn.execute(
            """INSERT INTO auditoria_ajustes
               (id_auditoria, id_produto, quantidade_ajuste, motivo,
                id_usuario_aprovador, data_aprovacao, observacao)
               VALUES (?,?,?,?,?,?,?)""",
            (id_auditoria, d["id_produto"], ajuste_qtd,
             d["causa"] or "Ajuste por auditoria", id_usuario, agora,
             d["observacao"]),
        )
        id_ajuste = cur.lastrowid

        tipo_mov = "Entrada" if ajuste_qtd > 0 else "Saida"
        qtd_mov = abs(ajuste_qtd)
        preco_ref = custo if custo > 0 else 0

        # FIX: usar a unidade do PRODUTO (antes forçava "Unidade").
        prod_unit = conn.execute(
            "SELECT unidade FROM produtos WHERE id_produto=?", (d["id_produto"],)
        ).fetchone()
        unidade_prod = prod_unit["unidade"] if prod_unit else "Unidade"

        conn.execute(
            """INSERT INTO movimentos
               (tipo, id_produto, data_hora, quantidade, quantidade_original,
                unidade_original, preco_unitario, valor_total, id_usuario,
                observacoes, lucro, subtipo)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,'Ajuste_Inventario')""",
            (tipo_mov, d["id_produto"], agora, qtd_mov, qtd_mov,
             unidade_prod, preco_ref, round(qtd_mov * preco_ref, 2),
             id_usuario, f"Ajuste auditoria #{id_auditoria}"),
        )
        id_mov = conn.execute("SELECT last_insert_rowid() AS x").fetchone()["x"]

        sinal = 1 if ajuste_qtd > 0 else -1
        conn.execute(
            """UPDATE estoque SET saldo_atual = saldo_atual + ?,
                   ultima_atualizacao=? WHERE id_produto=?""",
            (sinal * qtd_mov, agora, d["id_produto"]),
        )

        conn.execute(
            """UPDATE auditoria_divergencias
               SET estado='ajustada', id_ajuste=? WHERE id_divergencia=?""",
            (id_ajuste, d["id_divergencia"]),
        )
        ajustes_gerados += 1

    conn.execute(
        """UPDATE auditorias
           SET estado='fechada', valor_div_qtd=?, valor_div_valor=?
           WHERE id_auditoria=?""",
        (round(total_qtd, 3), round(total_valor, 2), id_auditoria),
    )
    conn.commit()
    registrar_auditoria(conn, id_usuario, "auditorias", id_auditoria,
                        "UPDATE", {"estado": aud["estado"]},
                        {"estado": "fechada", "ajustes_gerados": ajustes_gerados,
                         "div_qtd": total_qtd, "div_valor": total_valor},
                        ip_address)
    conn.commit()
    return {"ajustes_gerados": ajustes_gerados,
            "div_qtd": round(total_qtd, 3),
            "div_valor": round(total_valor, 2)}


def obter_auditoria(conn, id_auditoria):
    aud = conn.execute(
        """SELECT a.*, u.nome AS responsavel_nome, c.nome AS categoria_nome
           FROM auditorias a
           LEFT JOIN usuarios u ON u.id_usuario=a.responsavel_id
           LEFT JOIN categorias c ON c.id_categoria=a.id_categoria
           WHERE a.id_auditoria=?""",
        (id_auditoria,),
    ).fetchone()
    return dict(aud) if aud else None