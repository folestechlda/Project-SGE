# -*- coding: utf-8 -*-
"""Cálculo dos relatórios (Balancete, Estoque Geral, Posição, Vendas, Extratos,
Perdas e Validade, Divergências de inventário).

Todos partem do MESMO razão (`carregar_razao`): os movimentos de cada produto, por
ordem, com a quantidade e o valor acumulados. Assim os totais de um relatório batem
com os dos outros, e cada conta fecha por construção:

    valor inicial + compras - custo das vendas - perdas e ajustes = valor final

Convenções:
  * quantidades guardadas em unidade-base; aqui convertem-se para a unidade que o
    utilizador vê (caixa, kg, un) com `qtd_vista`;
  * o valor de um movimento é sempre a CUSTO (a venda entra pelo custo; o preço de
    venda aparece só como receita);
  * nada é lido de "estornos" ou "lotes": o sistema não os guarda.
"""
from datetime import datetime, timedelta, date

ROTULO_UN = {"Unidade": "un", "kg": "kg", "Caixa": "cx", "Embalagem": "emb"}
EPS = 1e-9


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def r2(v):
    return round(float(v or 0) + 0.0, 2)


def rotulo_un(p):
    return ROTULO_UN.get(p["unidade"], p["unidade"])


def fator(p):
    """Quantas unidades-base cabem na unidade que o utilizador vê."""
    qpu = float(p["qtd_por_unidade"] or 1)
    return qpu if (p["unidade"] in ("Caixa", "Embalagem") and qpu > 1) else 1.0


class Qtd(float):
    """Quantidade na unidade vista; guarda o fator para caixas/embalagens."""
    fat = 1.0
    un = ""
    base = None


def fmt_qtd(value, un=None):
    """200,0 (kg) · 60 (un) · 2 cx 5 un (caixas: inteiros + resto em unidades).

    A unidade é lida do próprio valor (`Qtd.un`) quando não vem por argumento, para
    que o PDF e o HTML mostrem o mesmo: produtos em 'un' nunca aparecem com decimais."""
    try:
        v = round(float(value), 3)
    except (TypeError, ValueError):
        return "—"
    sinal = "\u2212" if v < 0 else ""
    v = abs(v)
    fat = getattr(value, "fat", 1.0)
    un = un or getattr(value, "un", None)
    if fat and fat > 1:
        # trabalha sobre a quantidade-base exacta (evita 22,99 un em vez de 23 un)
        base = getattr(value, "base", None)
        base = round(abs(float(base)), 3) if base is not None else round(v * fat, 3)
        caixas = int(base // fat + 1e-9)
        resto = round(base - caixas * fat, 3)
        if resto >= fat - 0.0005:
            caixas, resto = caixas + 1, 0
        if abs(resto - round(resto)) < 0.0051:
            resto = int(round(resto))
        rot = un or "cx"
        resto_txt = str(resto) if isinstance(resto, int) else f"{resto:.3f}".rstrip("0").replace(".", ",")
        if caixas and resto:
            return f"{sinal}{caixas} {rot} {resto_txt} un"
        if resto:
            return f"{sinal}{resto_txt} un"
        return f"{sinal}{caixas}"
    if un == "un":
        txt = str(int(round(v))) if abs(v - round(v)) < 0.0051 else f"{v:.3f}".rstrip("0").replace(".", ",")
    else:
        txt = f"{v:.3f}".rstrip("0")
        if txt.endswith("."):
            txt += "0"
        if "." not in txt:
            txt += ".0"
        txt = txt.replace(".", ",")
    return sinal + txt


def qtd_vista(p, qtd_base):
    fat = fator(p)
    q = Qtd(round(float(qtd_base or 0) / fat, 3))
    q.fat = fat
    q.base = float(qtd_base or 0)
    q.un = ROTULO_UN.get(p["unidade"], p["unidade"])
    return q


def _dia(txt, padrao):
    txt = (txt or "").strip()
    try:
        return datetime.strptime(txt[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return padrao


def periodo(ini, fim, hoje=None):
    """Normaliza (ini, fim): por omissão, o mês corrente até hoje."""
    hoje = hoje or datetime.now().date()
    fim = _dia(fim, hoje.isoformat())
    ini = _dia(ini, hoje.replace(day=1).isoformat())
    if ini > fim:
        ini, fim = fim, ini
    return ini, fim


def dmy(iso):
    iso = (iso or "")[:10]
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso or "—"


def dm(iso):
    iso = (iso or "")[:10]
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m")
    except ValueError:
        return iso or "—"


def metodo_custeio_texto(conn):
    from database import get_config
    m = get_config("metodo_custeio", "media_ponderada")
    return {"media_ponderada": "Custo médio ponderado",
            "manual": "Custo manual (cadastro do produto)"}.get(m, "Último custo de entrada")


def valorizacao_texto(conn):
    from database import get_config
    if get_config("metodo_custeio", "media_ponderada") == "media_ponderada":
        return "Custo vigente à data de cada movimento"
    return "Custo actual do cadastro, aplicado a todos os movimentos"


def classe_movimento(m):
    st = (m["subtipo"] or "")
    if st == "Ajuste_Inventario":
        return "ajuste"
    if st.startswith("Perda_"):
        return "perda"
    return "compra" if m["tipo"] == "Entrada" else "venda"


def motivo_perda(m):
    return (m["subtipo"] or "").replace("Perda_", "").replace("_", " ") or "Outros"


def justificacao_perda(obs):
    """Texto livre escrito pelo utilizador (retira o prefixo 'Perda — Motivo.')."""
    t = (obs or "").strip()
    if t.startswith("Perda"):
        t = t.split(".", 1)[1].strip() if "." in t else ""
    return t


# ---------------------------------------------------------------------------
# Razão por produto
# ---------------------------------------------------------------------------
def carregar_razao(conn, fim, id_categoria=None, id_produto=None, so_ativos=False):
    """{id_produto: {"p": dict, "movs": [..]}} com os movimentos até `fim` (inclusive).

    Cada movimento traz: cls (compra/venda/perda/ajuste), q (com sinal, base),
    val (valor a CUSTO, com sinal), receita, custo, qtd_apos, val_apos, unit_apos.
    """
    sql = """SELECT p.*, c.nome AS categoria_nome
             FROM produtos p LEFT JOIN categorias c ON c.id_categoria = p.id_categoria
             WHERE 1=1"""
    par = []
    if id_categoria:
        sql += " AND p.id_categoria=?"
        par.append(id_categoria)
    if id_produto:
        sql += " AND p.id_produto=?"
        par.append(id_produto)
    if so_ativos:
        sql += " AND p.ativo=1"
    razao = {r["id_produto"]: {"p": dict(r), "movs": []}
             for r in conn.execute(sql + " ORDER BY c.nome, p.nome", par).fetchall()}
    if not razao:
        return razao

    movs = conn.execute(
        """SELECT m.*, u.nome AS usuario_nome FROM movimentos m
           LEFT JOIN usuarios u ON u.id_usuario = m.id_usuario
           WHERE m.data_hora <= ? ORDER BY m.data_hora ASC, m.id_movimento ASC""",
        (fim + "T23:59:59",)).fetchall()

    from database import get_config
    cmp_ = get_config("metodo_custeio", "media_ponderada") == "media_ponderada"
    # Saldo de abertura: o que o estoque do sistema tem a mais/menos do que os movimentos
    # explicam (ex.: stock carregado sem entrada, ou saldo reposto a zero). Sem isto, produtos
    # com stock anterior ao 1.º movimento apareceriam com saldo negativo.
    liquido = {r["id_produto"]: float(r["l"] or 0) for r in conn.execute(
        "SELECT id_produto, SUM(CASE WHEN tipo='Entrada' THEN quantidade ELSE -quantidade END) AS l "
        "FROM movimentos GROUP BY id_produto").fetchall()}
    saldos = {r["id_produto"]: float(r["saldo_atual"] or 0) for r in conn.execute(
        "SELECT id_produto, saldo_atual FROM estoque").fetchall()}
    estado = {}
    for pid, e in razao.items():
        p = e["p"]
        custo_cad = float(p["preco_custo"] or 0)
        q0 = round(saldos.get(pid, 0.0) - liquido.get(pid, 0.0), 6)
        if abs(q0) < 0.0005:
            q0 = 0.0
        e["ab"] = (q0, q0 * custo_cad, custo_cad if q0 else None)
        e["abertura_q"] = q0
        estado[pid] = {"q": q0, "v": q0 * custo_cad, "u": custo_cad, "cad": custo_cad}
    for m in movs:
        e = razao.get(m["id_produto"])
        if e is None:
            continue
        st = estado[m["id_produto"]]
        cls = classe_movimento(m)
        sinal = 1 if m["tipo"] == "Entrada" else -1
        q = float(m["quantidade"] or 0)
        valor = float(m["valor_total"] or 0)
        receita = custo = 0.0
        if not cmp_:
            # custo manual / último custo: tudo valorizado ao custo do cadastro,
            # como o sistema valoriza o estoque (saldo x custo)
            val = sinal * q * st["cad"]
            if cls == "venda":
                receita = valor
                custo = q * st["cad"]
        elif cls == "venda":
            receita = valor
            custo = valor - float(m["lucro"] or 0)
            val = -custo
        else:
            val = sinal * valor
        st["q"] += sinal * q
        st["v"] += val
        if not cmp_:
            st["u"] = st["cad"]
        elif st["q"] > EPS:
            st["u"] = st["v"] / st["q"]
        e["movs"].append({
            "id": m["id_movimento"], "dt": m["data_hora"], "tipo": m["tipo"], "cls": cls,
            "q": sinal * q, "val": val, "receita": receita, "custo": custo,
            "lucro": (receita - custo) if cls == "venda" else float(m["lucro"] or 0),
            "preco": float(m["preco_unitario"] or 0),
            "doc": m["nota_fiscal"], "obs": m["observacoes"], "subtipo": m["subtipo"],
            "forma": m["forma_pagamento"], "cliente": m["cliente"], "usuario": m["usuario_nome"],
            "id_usuario": m["id_usuario"], "codigo": m["codigo_venda"],
            "qtd_apos": st["q"], "val_apos": st["v"], "unit_apos": st["u"],
        })
    return razao


def _estado_em(entry, antes_de):
    """(qtd, valor, custo_unit) logo antes de `antes_de` (string ISO completa)."""
    ult = None
    for m in entry["movs"]:
        if m["dt"] < antes_de:
            ult = m
        else:
            break
    if ult:
        return (ult["qtd_apos"], ult["val_apos"], ult["unit_apos"])
    return entry.get("ab", (0.0, 0.0, None))


def estado_final(entry):
    """(qtd, valor, custo_unit) depois do último movimento carregado."""
    if entry["movs"]:
        u = entry["movs"][-1]
        return (u["qtd_apos"], u["val_apos"], u["unit_apos"])
    return entry.get("ab", (0.0, 0.0, None))


def resumir_produto(entry, ini, fim):
    """Totais de um produto no período [ini, fim] (strings YYYY-MM-DD)."""
    movs = entry["movs"]
    q0, v0, _ = _estado_em(entry, ini + "T00:00:00")
    d = {"q_ini": q0, "v_ini": v0, "compra_q": 0.0, "compra_v": 0.0, "venda_q": 0.0,
         "venda_receita": 0.0, "venda_custo": 0.0, "perda_q": 0.0, "perda_v": 0.0,
         "aj_q": 0.0, "aj_v": 0.0, "n_mov": 0, "n_venda_neg": 0, "n_venda": 0}
    qf, vf = q0, v0
    unit = None
    for m in movs:
        if m["dt"] < ini + "T00:00:00":
            continue
        if m["dt"] > fim + "T23:59:59":
            break
        d["n_mov"] += 1
        if m["cls"] == "compra":
            d["compra_q"] += m["q"]; d["compra_v"] += m["val"]
        elif m["cls"] == "venda":
            d["venda_q"] += -m["q"]; d["venda_receita"] += m["receita"]; d["venda_custo"] += m["custo"]
            d["n_venda"] += 1
            if m["lucro"] < -0.005:
                d["n_venda_neg"] += 1
        elif m["cls"] == "perda":
            d["perda_q"] += -m["q"]; d["perda_v"] += -m["val"]
        else:
            d["aj_q"] += m["q"]; d["aj_v"] += m["val"]
        qf, vf, unit = m["qtd_apos"], m["val_apos"], m["unit_apos"]
    p = entry["p"]
    if unit is None:
        _, _, unit = _estado_em(entry, fim + "T23:59:59")
        if unit is None:
            unit = float(p["preco_custo"] or 0)
    d["q_fim"], d["v_fim"], d["unit_fim"] = qf, vf, unit
    # "perdas e ajustes" como REDUÇÃO de valor (positivo = perda líquida)
    d["perdas_aj_v"] = d["perda_v"] - d["aj_v"]
    # entradas/saídas em quantidade que fecham com a quantidade final
    d["ent_q"] = d["compra_q"] + max(d["aj_q"], 0.0)
    d["sai_q"] = d["venda_q"] + d["perda_q"] + max(-d["aj_q"], 0.0)
    return d


def _situacao(p, qtd_fim):
    fat = fator(p)
    minimo = float(p["estoque_minimo"] or 0)
    maximo = p.get("estoque_maximo")
    if qtd_fim < -EPS:
        return {"codigo": "negativo", "texto": "Saldo negativo", "classe": "chip-perigo"}
    if qtd_fim < minimo - EPS:
        return {"codigo": "abaixo", "classe": "chip-aviso",
                "texto": f"Abaixo do mínimo ({fmt_qtd(qtd_vista(p, minimo), rotulo_un(p))})"}
    if maximo and qtd_fim > float(maximo) + EPS:
        return {"codigo": "acima", "texto": "Acima do máximo", "classe": "chip-neutro"}
    return {"codigo": "ok", "texto": "OK", "classe": "chip-ok"}


def _n(v):
    v = round(float(v), 3)
    return str(int(v)) if v == int(v) else str(v).replace(".", ",")


def _agrupar_categorias(linhas, chaves_soma):
    """[{nome, linhas, sub}] mantendo a ordem de categoria > produto."""
    grupos, idx = [], {}
    for ln in linhas:
        nome = ln["categoria"]
        if nome not in idx:
            idx[nome] = {"nome": nome, "linhas": [], "sub": {k: 0.0 for k in chaves_soma}}
            grupos.append(idx[nome])
        g = idx[nome]
        g["linhas"].append(ln)
        for k in chaves_soma:
            g["sub"][k] += ln[k]
    for g in grupos:
        g["sub"] = {k: r2(v) for k, v in g["sub"].items()}
    return grupos


def _relevante(entry, resumo):
    p = entry["p"]
    return (p["ativo"] == 1 or resumo["n_mov"] > 0 or abs(resumo["q_ini"]) > EPS
            or abs(resumo["q_fim"]) > EPS)


# ---------------------------------------------------------------------------
# Estado do período e reconciliação com o sistema
# ---------------------------------------------------------------------------
def estado_periodo(conn, fim):
    r = conn.execute("SELECT 1 FROM fechos_mensais WHERE data_fecho >= ? LIMIT 1", (fim,)).fetchone()
    return "FECHADO" if r else "ABERTO"


def reconciliar_com_sistema(conn, fim, entradas_resumo, valor_final_total, filtrado):
    """Compara o valor final do razão com o estoque que o sistema mostra hoje.

    Só é possível para períodos que terminam hoje (ou depois): para datas passadas o
    sistema não guarda a fotografia diária. Devolve {estado, diferenca, texto}.
    """
    hoje = datetime.now().date().isoformat()
    if fim < hoje:
        return {"estado": "nv", "diferenca": 0.0,
                "texto": "Período passado: não há fotografia do sistema para comparar; "
                         "a conta fecha pelo histórico de movimentos."}
    dif_val, divergentes = 0.0, []
    total_sistema = 0.0
    for e, r in entradas_resumo:
        p = e["p"]
        row = conn.execute("SELECT saldo_atual FROM estoque WHERE id_produto=?",
                           (p["id_produto"],)).fetchone()
        saldo = float(row["saldo_atual"] or 0) if row else 0.0
        total_sistema += saldo * float(p["preco_custo"] or 0)
        if abs(saldo - r["q_fim"]) > 0.0005:
            divergentes.append(p["nome"])
    dif_val = round(valor_final_total - total_sistema, 2)
    n_mov = sum(len(e["movs"]) for e, _ in entradas_resumo)
    tol = max(1.0, 0.01 * n_mov)
    if divergentes:
        return {"estado": "atencao", "diferenca": dif_val,
                "texto": "O saldo guardado difere do histórico de movimentos em: "
                         + ", ".join(divergentes[:5]) + ("…" if len(divergentes) > 5 else "")}
    if abs(dif_val) > tol:
        return {"estado": "atencao", "diferenca": dif_val,
                "texto": f"Valor do histórico difere do estoque valorizado ao custo actual em {dif_val:.2f}."}
    return {"estado": "ok", "diferenca": dif_val, "texto": f"diferença {dif_val:.2f}"}


def faturas_anuladas(conn, ini, fim):
    """Vendas anuladas no período (repuseram o estoque)."""
    return [{"data": r["data_anulacao"][:10], "numero": r["codigo_venda"],
             "cliente": r["cliente"] or "—", "valor": r2(r["valor_total"]),
             "motivo": r["motivo"], "usuario": r["usuario"] or "—", "tipo": r["tipo"]}
            for r in conn.execute(
                """SELECT v.*, u.nome AS usuario FROM vendas_anuladas v
                   LEFT JOIN usuarios u ON u.id_usuario=v.id_usuario_anulou
                   WHERE v.data_anulacao BETWEEN ? AND ? ORDER BY v.data_anulacao""",
                (ini + "T00:00:00", fim + "T23:59:59")).fetchall()]


# ---------------------------------------------------------------------------
# 1. Balancete de Estoque
# ---------------------------------------------------------------------------
def balancete(conn, ini, fim, id_categoria=None):
    razao = carregar_razao(conn, fim, id_categoria=id_categoria)
    linhas, pares = [], []
    for e in razao.values():
        r = resumir_produto(e, ini, fim)
        if not _relevante(e, r):
            continue
        p = e["p"]
        fat = fator(p)
        pares.append((e, r))
        linhas.append({
            "id_produto": p["id_produto"], "produto": p["nome"], "categoria": p["categoria_nome"] or "Sem categoria",
            "un": rotulo_un(p), "unidade": p["unidade"], "ativo": p["ativo"],
            "q_ini": qtd_vista(p, r["q_ini"]), "ent": qtd_vista(p, r["ent_q"]),
            "sai": qtd_vista(p, r["sai_q"]), "q_fim": qtd_vista(p, r["q_fim"]),
            "custo_unit": r2(r["unit_fim"] * fat), "valor_final": r2(r["v_fim"]),
            "situacao": _situacao(p, r["q_fim"]), "movimentou": r["n_mov"] > 0,
            "r": r,
        })
    grupos = _agrupar_categorias(linhas, ["valor_final"])

    soma = lambda k: r2(sum(r[k] for _, r in pares))
    tot = {"inicial": soma("v_ini"), "compras": soma("compra_v"), "custo_vendas": soma("venda_custo"),
           "perdas_aj": soma("perdas_aj_v"), "final": soma("v_fim"),
           "vendas": soma("venda_receita")}
    tot["lucro"] = r2(tot["vendas"] - tot["custo_vendas"])
    tot["margem"] = round(tot["lucro"] / tot["vendas"] * 100, 1) if tot["vendas"] else None
    esperado = r2(tot["inicial"] + tot["compras"] - tot["custo_vendas"] - tot["perdas_aj"])
    tot["identidade_ok"] = abs(esperado - tot["final"]) <= 0.02

    rec = reconciliar_com_sistema(conn, fim, pares, tot["final"], bool(id_categoria))
    alertas = {
        "abaixo_minimo": sum(1 for l in linhas if l["ativo"] and l["situacao"]["codigo"] == "abaixo"),
        "negativos": sum(1 for l in linhas if l["situacao"]["codigo"] == "negativo"),
        "margem_negativa": sum(r["n_venda_neg"] for _, r in pares),
        "sem_movimento": sum(1 for l in linhas if l["ativo"] and not l["movimentou"]),
    }
    return {"ini": ini, "fim": fim, "grupos": grupos, "tot": tot, "reconciliacao": rec,
            "alertas": alertas, "anuladas": faturas_anuladas(conn, ini, fim),
            "n_aberturas": sum(1 for e, _ in pares if abs(e.get("abertura_q", 0)) > 0.0005),
            "estado": estado_periodo(conn, fim), "metodo": metodo_custeio_texto(conn), "valorizacao": valorizacao_texto(conn),
            "n_linhas": len(linhas)}


# ---------------------------------------------------------------------------
# 2. Estoque Geral
# ---------------------------------------------------------------------------
def estoque_geral(conn, ini, fim, id_categoria=None):
    razao = carregar_razao(conn, fim, id_categoria=id_categoria)
    linhas, pares = [], []
    for e in razao.values():
        r = resumir_produto(e, ini, fim)
        if not _relevante(e, r):
            continue
        p = e["p"]
        pares.append((e, r))
        linhas.append({
            "id_produto": p["id_produto"], "produto": p["nome"], "categoria": p["categoria_nome"] or "Sem categoria",
            "un": rotulo_un(p), "unidade": p["unidade"], "q_fim": qtd_vista(p, r["q_fim"]),
            "v_ini": r2(r["v_ini"]), "compras": r2(r["compra_v"]), "custo_vendas": r2(r["venda_custo"]),
            "perdas_aj": r2(r["perdas_aj_v"]), "v_fim": r2(r["v_fim"]),
            "fecha": abs(r["v_ini"] + r["compra_v"] - r["venda_custo"] - r["perdas_aj_v"] - r["v_fim"]) <= 0.02,
        })
    total_final = r2(sum(l["v_fim"] for l in linhas))
    for l in linhas:
        l["pct"] = round(l["v_fim"] / total_final * 100, 1) if total_final > 0 else 0.0
    grupos = _agrupar_categorias(linhas, ["v_ini", "compras", "custo_vendas", "perdas_aj", "v_fim"])
    for g in grupos:
        g["sub"]["pct"] = round(g["sub"]["v_fim"] / total_final * 100, 1) if total_final > 0 else 0.0
    tot = {k: r2(sum(l[k] for l in linhas)) for k in ("v_ini", "compras", "custo_vendas", "perdas_aj", "v_fim")}
    rec = reconciliar_com_sistema(conn, fim, pares, tot["v_fim"], bool(id_categoria))
    todas_fecham = all(l["fecha"] for l in linhas)
    pendentes = conn.execute(
        """SELECT a.id_auditoria, a.data_fim, COUNT(*) AS n, COALESCE(SUM(d.diferenca_valor),0) AS valor
           FROM auditoria_divergencias d JOIN auditorias a ON a.id_auditoria=d.id_auditoria
           WHERE d.estado IN ('pendente','justificada') AND a.estado != 'fechada'
           GROUP BY a.id_auditoria ORDER BY a.id_auditoria""").fetchall()
    return {"ini": ini, "fim": fim, "grupos": grupos, "tot": tot, "reconciliacao": rec,
            "todas_fecham": todas_fecham, "pendentes": [dict(x) for x in pendentes],
            "metodo": metodo_custeio_texto(conn), "valorizacao": valorizacao_texto(conn), "n_linhas": len(linhas)}


# ---------------------------------------------------------------------------
# 3. Posição de Estoque
# ---------------------------------------------------------------------------
def posicao_estoque(conn, data, id_categoria=None, so_abaixo=False, so_sem_mov=False):
    razao = carregar_razao(conn, data, id_categoria=id_categoria, so_ativos=True)
    d0 = (datetime.strptime(data, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    linhas, valor_total, n_ativos = [], 0.0, 0
    n_abaixo = n_neg = 0
    for e in razao.values():
        p = e["p"]
        n_ativos += 1
        r = resumir_produto(e, d0, data)
        qf = r["q_fim"]
        sit = _situacao(p, qf)
        fat = fator(p)
        media = r["venda_q"] / 30.0
        cobertura = int(qf / media) if media > EPS and qf > 0 else None
        valor = r2(r["v_fim"])
        valor_total += valor
        minimo = float(p["estoque_minimo"] or 0)
        if sit["codigo"] == "negativo":
            n_neg += 1
            acao = {"texto": "Saldo negativo: rever movimentos", "classe": "chip-perigo"}
        elif sit["codigo"] == "abaixo":
            n_abaixo += 1
            falta = qtd_vista(p, minimo - qf)
            falta_txt = fmt_qtd(falta, rotulo_un(p))
            un_txt = "" if (fat > 1 and (" " in falta_txt)) else " " + rotulo_un(p)
            acao = {"texto": f"Repor: faltam {falta_txt}{un_txt}", "classe": "chip-aviso"}
        elif sit["codigo"] == "acima":
            acao = {"texto": "Acima do máximo", "classe": "chip-neutro"}
        else:
            acao = {"texto": "OK", "classe": "chip-ok"}
        ordem = {"negativo": 0, "abaixo": 1, "acima": 3, "ok": 3}[sit["codigo"]]
        linhas.append({
            "id_produto": p["id_produto"], "produto": p["nome"], "categoria": p["categoria_nome"] or "—",
            "un": rotulo_un(p), "saldo": qtd_vista(p, qf), "minimo": qtd_vista(p, minimo),
            "cobertura": cobertura, "custo_unit": r2(r["unit_fim"] * fat), "valor": valor, "acao": acao,
            "codigo": sit["codigo"], "sem_mov": r["n_mov"] == 0,
            "_ordem": (ordem, cobertura if cobertura is not None else 10 ** 6, p["nome"].lower()),
        })
    linhas.sort(key=lambda l: l["_ordem"])
    if so_abaixo:
        linhas = [l for l in linhas if l["codigo"] in ("abaixo", "negativo")]
    if so_sem_mov:
        linhas = [l for l in linhas if l["sem_mov"]]
    total_listado = r2(sum(l["valor"] for l in linhas))
    return {"data": data, "linhas": linhas, "valor_total": r2(valor_total), "total_listado": total_listado,
            "n_ativos": n_ativos, "n_abaixo": n_abaixo, "n_neg": n_neg,
            "filtrado": bool(so_abaixo or so_sem_mov), "metodo": metodo_custeio_texto(conn), "valorizacao": valorizacao_texto(conn)}


# ---------------------------------------------------------------------------
# 4. Relatório de Vendas
# ---------------------------------------------------------------------------
def _movimentos_a_credito(conn):
    ids = {}
    for r in conn.execute(
            """SELECT i.id_movimento, c.numero_fatura, c.id_conta FROM contas_receber_itens i
               JOIN contas_receber c ON c.id_conta=i.id_conta""").fetchall():
        ids[r["id_movimento"]] = r["numero_fatura"] or f"FT-{r['id_conta']:05d}"
    for r in conn.execute("SELECT id_movimento, numero_fatura, id_conta FROM contas_receber "
                          "WHERE id_movimento IS NOT NULL").fetchall():
        ids.setdefault(r["id_movimento"], r["numero_fatura"] or f"FT-{r['id_conta']:05d}")
    return ids


def vendas(conn, ini, fim, id_categoria=None, id_produto=None, forma=None, id_usuario=None):
    razao = carregar_razao(conn, fim, id_categoria=id_categoria, id_produto=id_produto)
    credito = _movimentos_a_credito(conn)
    linhas, formas = [], {}
    tot_rec = tot_custo = 0.0
    n_neg = 0
    for e in razao.values():
        p = e["p"]
        q = rec = custo = 0.0
        for m in e["movs"]:
            if m["cls"] != "venda" or m["dt"] < ini + "T00:00:00" or m["dt"] > fim + "T23:59:59":
                continue
            eh_credito = m["id"] in credito
            chave = "A crédito" if eh_credito else (m["forma"] or "Forma não indicada")
            if forma:
                if forma == "__credito__" and not eh_credito:
                    continue
                if forma == "__vista__" and eh_credito:
                    continue
                if forma not in ("__credito__", "__vista__") and chave != forma:
                    continue
            if id_usuario and m["id_usuario"] != id_usuario:
                continue
            q += -m["q"]; rec += m["receita"]; custo += m["custo"]
            if m["lucro"] < -0.005:
                n_neg += 1
            f = formas.setdefault(chave, {"credito": eh_credito, "valor": 0.0})
            f["valor"] += m["receita"]
        if q <= EPS and rec <= EPS:
            continue
        tot_rec += rec; tot_custo += custo
        lucro = rec - custo
        linhas.append({
            "id_produto": p["id_produto"], "produto": p["nome"], "un": rotulo_un(p),
            "qtd": qtd_vista(p, q), "preco_medio": r2(rec / qtd_vista(p, q)) if qtd_vista(p, q) else 0.0,
            "receita": r2(rec), "custo": r2(custo), "lucro": r2(lucro),
            "margem": round(lucro / rec * 100, 1) if rec else None,
        })
    linhas.sort(key=lambda l: l["receita"], reverse=True)
    for l in linhas:
        l["pct"] = round(l["receita"] / tot_rec * 100, 1) if tot_rec else 0.0
    tot = {"vendas": r2(tot_rec), "custo": r2(tot_custo), "lucro": r2(tot_rec - tot_custo)}
    tot["margem"] = round(tot["lucro"] / tot["vendas"] * 100, 1) if tot["vendas"] else None
    vista = [(k, r2(v["valor"])) for k, v in sorted(formas.items()) if not v["credito"]]
    return {"ini": ini, "fim": fim, "linhas": linhas, "tot": tot,
            "a_vista_total": r2(sum(v for _, v in vista)), "a_vista": vista,
            "a_credito": r2(formas.get("A crédito", {"valor": 0})["valor"]),
            "margem_negativa": n_neg, "anuladas": faturas_anuladas(conn, ini, fim),
            "metodo": metodo_custeio_texto(conn), "valorizacao": valorizacao_texto(conn)}


# ---------------------------------------------------------------------------
# 5. Extrato de Produto (e variantes por categoria / geral)
# ---------------------------------------------------------------------------
_TIPO_TXT = {"compra": "Compra", "venda": "Venda", "ajuste": "Ajuste de inventário"}


def _extrato_de(entry, ini, fim, credito, incluir_perdas_ajustes=True):
    p = entry["p"]
    fat = fator(p)
    resumo = resumir_produto(entry, ini, fim)
    linhas = []
    for m in entry["movs"]:
        if m["dt"] < ini + "T00:00:00" or m["dt"] > fim + "T23:59:59":
            continue
        if not incluir_perdas_ajustes and m["cls"] in ("perda", "ajuste"):
            continue
        if m["cls"] == "compra":
            doc = m["doc"] or f"CMP-{m['id']:05d}"
        elif m["cls"] == "venda":
            doc = credito.get(m["id"]) or m.get("codigo") or f"V-{m['id']:05d}"
        elif m["cls"] == "perda":
            doc = f"PRD-{m['id']:05d}"
        else:
            doc = f"AJ-{m['id']:05d}"
        if m["cls"] == "perda":
            tipo = "Perda · " + motivo_perda({"subtipo": m["subtipo"]})
        else:
            tipo = _TIPO_TXT[m["cls"]]
        linhas.append({
            "data": dm(m["dt"]), "doc": doc, "tipo": tipo, "cinza": m["cls"] in ("perda", "ajuste"),
            "entrada": qtd_vista(p, m["q"]) if m["q"] > 0 else None,
            "saida": qtd_vista(p, -m["q"]) if m["q"] < 0 else None,
            "saldo": qtd_vista(p, m["qtd_apos"]), "custo_unit": r2(m["unit_apos"] * fat),
            "valor": r2(m["val"]), "saldo_valor": r2(m["val_apos"]),
        })
    r = resumo
    ent_total = qtd_vista(p, r["ent_q"])
    sai_total = qtd_vista(p, r["sai_q"])
    rec = r["venda_receita"]
    lucro = rec - r["venda_custo"]
    return {
        "produto": p, "id_produto": p["id_produto"], "nome": p["nome"], "un": rotulo_un(p),
        "categoria": p["categoria_nome"] or "—",
        "q_ini": qtd_vista(p, r["q_ini"]), "v_ini": r2(r["v_ini"]),
        "linhas": linhas, "ent_total": ent_total, "sai_total": sai_total,
        "q_fim": qtd_vista(p, r["q_fim"]), "v_fim": r2(r["v_fim"]),
        "ent_v": r2(r["compra_v"] + max(r["aj_v"], 0)),
        "sai_v": r2(r["venda_custo"] + r["perda_v"] + max(-r["aj_v"], 0)),
        "rec_q": r["q_ini"] + r["ent_q"] - r["sai_q"], "rec_v": r["v_ini"] + r["compra_v"] - r["venda_custo"] - r["perdas_aj_v"],
        "fecha_q": abs(r["q_ini"] + r["ent_q"] - r["sai_q"] - r["q_fim"]) < 0.0005,
        "fecha_v": abs(r["v_ini"] + r["compra_v"] - r["venda_custo"] - r["perdas_aj_v"] - r["v_fim"]) <= 0.02,
        "vendas": r2(rec), "custo_vendas": r2(r["venda_custo"]), "lucro": r2(lucro),
        "margem": round(lucro / rec * 100, 1) if rec else None,
        "compras_v": r2(r["compra_v"]), "perdas_aj_v": r2(r["perdas_aj_v"]),
        "n_mov": r["n_mov"], "abertura": qtd_vista(p, entry.get("abertura_q", 0.0)),
        "dia_anterior": (datetime.strptime(ini, "%Y-%m-%d") - timedelta(days=1)).strftime("%d/%m"),
    }


def extrato_produto(conn, id_produto, ini, fim):
    razao = carregar_razao(conn, fim, id_produto=id_produto)
    if id_produto not in razao:
        return None
    ex = _extrato_de(razao[id_produto], ini, fim, _movimentos_a_credito(conn))
    ex.update({"modo": "produto", "ini": ini, "fim": fim, "estado": estado_periodo(conn, fim),
               "metodo": metodo_custeio_texto(conn), "valorizacao": valorizacao_texto(conn)})
    return ex


def extrato_categoria(conn, id_categoria, ini, fim):
    razao = carregar_razao(conn, fim, id_categoria=id_categoria)
    credito = _movimentos_a_credito(conn)
    secoes = []
    for e in razao.values():
        r = resumir_produto(e, ini, fim)
        if r["n_mov"] == 0 and abs(r["q_ini"]) < EPS:
            continue
        secoes.append(_extrato_de(e, ini, fim, credito))
    nome = ""
    if razao:
        nome = next(iter(razao.values()))["p"]["categoria_nome"] or ""
    tot = {"v_ini": r2(sum(s["v_ini"] for s in secoes)), "v_fim": r2(sum(s["v_fim"] for s in secoes)),
           "ent_v": r2(sum(s["ent_v"] for s in secoes)), "sai_v": r2(sum(s["sai_v"] for s in secoes))}
    return {"modo": "categoria", "categoria": nome, "secoes": secoes, "tot": tot, "ini": ini, "fim": fim,
            "estado": estado_periodo(conn, fim), "metodo": metodo_custeio_texto(conn), "valorizacao": valorizacao_texto(conn)}


def extrato_geral(conn, ini, fim, id_categoria=None, incluir_perdas_ajustes=True):
    razao = carregar_razao(conn, fim, id_categoria=id_categoria)
    credito = _movimentos_a_credito(conn)
    linhas = []
    for e in razao.values():
        r = resumir_produto(e, ini, fim)
        if r["n_mov"] == 0 and abs(r["q_ini"]) < EPS:
            continue
        x = _extrato_de(e, ini, fim, credito, incluir_perdas_ajustes)
        linhas.append({"id_produto": x["id_produto"], "nome": x["nome"], "categoria": x["categoria"],
                       "v_ini": x["v_ini"], "ent_v": x["ent_v"], "sai_v": x["sai_v"], "v_fim": x["v_fim"]})
    tot = {k: r2(sum(l[k] for l in linhas)) for k in ("v_ini", "ent_v", "sai_v", "v_fim")}
    return {"modo": "geral", "linhas": linhas, "tot": tot, "ini": ini, "fim": fim,
            "estado": estado_periodo(conn, fim), "metodo": metodo_custeio_texto(conn), "valorizacao": valorizacao_texto(conn)}


# ---------------------------------------------------------------------------
# 6. Extrato de Cliente
# ---------------------------------------------------------------------------
def extrato_cliente(conn, nome_cliente, hoje=None):
    hoje = hoje or datetime.now().date()
    faturas = conn.execute(
        "SELECT * FROM contas_receber WHERE nome_cliente=? ORDER BY data_emissao ASC, id_conta ASC",
        (nome_cliente,)).fetchall()
    if not faturas:
        return None
    linhas = []
    tot = {"total": 0.0, "pago": 0.0, "aberto": 0.0, "atraso": 0.0}
    idade = {"a_vencer": 0.0, "d1_30": 0.0, "d31_60": 0.0, "d60_mais": 0.0}
    for f in faturas:
        numero = f["numero_fatura"] or f"FT-{f['id_conta']:05d}"
        total, pago = r2(f["valor_total"]), r2(f["valor_pago"])
        aberto = r2(f["saldo"])
        venc = (f["data_vencimento"] or "")[:10]
        dias_txt, estado, classe = "—", "", ""
        if f["status"] == "Cancelada":
            linhas.append({"numero": numero, "emissao": dmy(f["data_emissao"]), "venc": dmy(venc) if venc else "—",
                           "total": total, "pago": 0.0, "aberto": 0.0, "dias": "—",
                           "estado": "Anulada", "classe": "chip-neutro", "anulada": True})
            continue
        tot["total"] += total; tot["pago"] += pago; tot["aberto"] += aberto
        if aberto <= 0.005:
            estado, classe = "Paga", "chip-ok"
        else:
            try:
                dv = datetime.strptime(venc, "%Y-%m-%d").date() if venc else None
            except ValueError:
                dv = None
            if dv and dv < hoje:
                atraso = (hoje - dv).days
                dias_txt, estado, classe = str(atraso), "Em atraso", "chip-perigo"
                tot["atraso"] += aberto
                idade["d1_30" if atraso <= 30 else "d31_60" if atraso <= 60 else "d60_mais"] += aberto
            else:
                estado, classe = "A vencer", "chip-aviso"
                if dv:
                    dias_txt = f"faltam {(dv - hoje).days}"
                idade["a_vencer"] += aberto
        linhas.append({"numero": numero, "emissao": dmy(f["data_emissao"]), "venc": dmy(venc) if venc else "—",
                       "total": total, "pago": pago, "aberto": aberto, "dias": dias_txt,
                       "estado": estado, "classe": classe, "anulada": False})
    ids = [f["id_conta"] for f in faturas]
    marcas = ",".join("?" * len(ids))
    num_por_id = {f["id_conta"]: (f["numero_fatura"] or f"FT-{f['id_conta']:05d}") for f in faturas}
    recibos = [{"numero": r["numero"], "data": dm(r["data_pagamento"]), "valor": r2(r["valor"]),
                "fatura": num_por_id.get(r["id_conta"], "—")}
               for r in conn.execute(
                   f"SELECT * FROM recibos WHERE id_conta IN ({marcas}) ORDER BY data_pagamento, id_recibo",
                   ids).fetchall()]
    tot = {k: r2(v) for k, v in tot.items()}
    idade = {k: r2(v) for k, v in idade.items()}
    nif = next((f["nif_cliente"] for f in reversed(faturas) if "nif_cliente" in f.keys() and f["nif_cliente"]), "")
    return {"cliente": nome_cliente, "nif": nif, "hoje": hoje.isoformat(), "linhas": linhas, "tot": tot,
            "idade": idade, "recibos": recibos}


# ---------------------------------------------------------------------------
# 7. Inventário físico: divergências de uma contagem (auditoria)
# ---------------------------------------------------------------------------
def divergencias_inventario(conn, id_auditoria):
    aud = conn.execute(
        """SELECT a.*, u.nome AS responsavel_nome FROM auditorias a
           LEFT JOIN usuarios u ON u.id_usuario=a.responsavel_id WHERE a.id_auditoria=?""",
        (id_auditoria,)).fetchone()
    if not aud:
        return None
    divs = conn.execute(
        """SELECT d.*, p.nome, p.unidade, p.qtd_por_unidade FROM auditoria_divergencias d
           JOIN produtos p ON p.id_produto=d.id_produto WHERE d.id_auditoria=?""",
        (id_auditoria,)).fetchall()
    contados = conn.execute(
        """SELECT c.id_produto, p.nome, p.unidade, p.qtd_por_unidade, p.preco_custo,
                  MAX(c.quantidade_contada) AS qtd, MAX(c.id_usuario_contador) AS uid
           FROM auditoria_contagens c JOIN produtos p ON p.id_produto=c.id_produto
           WHERE c.id_auditoria=? GROUP BY c.id_produto""", (id_auditoria,)).fetchall()
    contador = conn.execute(
        """SELECT u.nome FROM auditoria_contagens c LEFT JOIN usuarios u ON u.id_usuario=c.id_usuario_contador
           WHERE c.id_auditoria=? AND u.nome IS NOT NULL ORDER BY c.id_contagem LIMIT 1""",
        (id_auditoria,)).fetchone()
    por_prod = {d["id_produto"]: d for d in divs}
    linhas = []
    for d in divs:
        p = {"unidade": d["unidade"], "qtd_por_unidade": d["qtd_por_unidade"]}
        dif = float(d["diferenca_qtd"] or 0)
        linhas.append({
            "produto": d["nome"], "un": rotulo_un(p), "sistema": qtd_vista(p, d["estoque_teorico"]),
            "contagem": qtd_vista(p, d["estoque_contado"]), "dif": qtd_vista(p, dif),
            "custo_unit": r2(float(d["custo_unitario"] or 0) * fator(p)),
            "dif_v": r2(d["diferenca_valor"]), "motivo": d["causa"] or "", "obs": d["observacao"] or "",
            "estado": d["estado"], "com_div": abs(dif) > 0.0005 or abs(float(d["diferenca_valor"] or 0)) > 0.004,
        })
    for c in contados:
        if c["id_produto"] in por_prod:
            continue
        p = {"unidade": c["unidade"], "qtd_por_unidade": c["qtd_por_unidade"]}
        linhas.append({"produto": c["nome"], "un": rotulo_un(p), "sistema": qtd_vista(p, c["qtd"]),
                       "contagem": qtd_vista(p, c["qtd"]), "dif": 0.0,
                       "custo_unit": r2(float(c["preco_custo"] or 0) * fator(p)), "dif_v": 0.0,
                       "motivo": "", "estado": "ok", "com_div": False})
    linhas.sort(key=lambda l: (not l["com_div"], -abs(l["dif_v"]), l["produto"].lower()))
    perdas = r2(sum(l["dif_v"] for l in linhas if l["dif_v"] < 0))
    sobras = r2(sum(l["dif_v"] for l in linhas if l["dif_v"] > 0))
    n_pend = sum(1 for l in linhas if l["com_div"] and l["estado"] in ("pendente", "justificada"))
    if aud["estado"] == "fechada":
        estado = {"texto": "FECHADA", "classe": "chip-ok"}
    elif n_pend or aud["estado"] == "em_revisao":
        estado = {"texto": "PENDENTE DE APROVAÇÃO", "classe": "chip-aviso"}
    else:
        estado = {"texto": aud["estado"].replace("_", " ").upper(), "classe": "chip-neutro"}
    return {"aud": dict(aud), "linhas": linhas, "estado": estado,
            "contador": (contador["nome"] if contador else None) or aud["responsavel_nome"] or "—",
            "n_contados": len(linhas), "n_sem": sum(1 for l in linhas if not l["com_div"]),
            "n_com": sum(1 for l in linhas if l["com_div"]),
            "impacto": r2(perdas + sobras), "perdas": perdas, "sobras": sobras}


# ---------------------------------------------------------------------------
# 8. Perdas e Validade
# ---------------------------------------------------------------------------
def perdas_validade(conn, ini, fim, a_partir_de=None, dias=90):
    razao = carregar_razao(conn, fim)
    por_motivo = {}
    ocorrencias = []
    for e in razao.values():
        for m in e["movs"]:
            if m["cls"] != "perda" or m["dt"] < ini + "T00:00:00" or m["dt"] > fim + "T23:59:59":
                continue
            ocorrencias.append({"dt": m["dt"], "data": dmy(m["dt"][:10]), "produto": e["p"]["nome"],
                                "qtd": qtd_vista(e["p"], -m["q"]), "un": rotulo_un(e["p"]),
                                "motivo": motivo_perda({"subtipo": m["subtipo"]}),
                                "justificacao": justificacao_perda(m["obs"]),
                                "valor": r2(-m["val"]), "usuario": m["usuario"] or "—"})
            k = motivo_perda({"subtipo": m["subtipo"]})
            d = por_motivo.setdefault(k, {"motivo": k, "n": 0, "valor": 0.0})
            d["n"] += 1
            d["valor"] += -m["val"]
    ocorrencias.sort(key=lambda x: x["dt"], reverse=True)
    motivos = sorted(({**v, "valor": r2(v["valor"])} for v in por_motivo.values()),
                     key=lambda x: x["valor"], reverse=True)
    base = datetime.strptime(a_partir_de or datetime.now().date().isoformat(), "%Y-%m-%d").date()
    itens = []
    for e in carregar_razao(conn, base.isoformat(), so_ativos=True).values():
        p = e["p"]
        if not p["validade"]:
            continue
        try:
            venc = datetime.strptime(p["validade"][:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        d = (venc - base).days
        if d > dias:
            continue
        saldo_base, valor, _ = estado_final(e)
        if saldo_base <= EPS:
            continue
        faixa = ("Vencido", "chip-perigo") if d < 0 else ("0 a 30 dias", "chip-perigo") if d <= 30 else \
                ("31 a 60 dias", "chip-aviso") if d <= 60 else ("61 a 90 dias", "chip-neutro")
        itens.append({"produto": p["nome"], "validade": dmy(venc.isoformat()), "dias": d,
                      "saldo": qtd_vista(p, saldo_base), "un": rotulo_un(p), "valor": r2(valor),
                      "faixa": faixa[0], "classe": faixa[1]})
    itens.sort(key=lambda x: x["dias"])
    return {"ini": ini, "fim": fim, "motivos": motivos,
            "ocorrencias": ocorrencias,
            "tot_n": sum(m["n"] for m in motivos), "tot_valor": r2(sum(m["valor"] for m in motivos)),
            "itens": itens, "risco": r2(sum(i["valor"] for i in itens)), "base": base.isoformat()}


# ---------------------------------------------------------------------------
# Curva ABC (sobre o valor do estoque)
# ---------------------------------------------------------------------------
def curva_abc(conn, data):
    razao = carregar_razao(conn, data, so_ativos=True)
    itens = []
    for e in razao.values():
        p = e["p"]
        q_, v_, u_ = estado_final(e)
        if v_ <= 0 or not u_:
            continue
        itens.append({"produto": p["nome"], "un": rotulo_un(p), "qtd": qtd_vista(p, q_),
                      "custo_unit": r2(u_ * fator(p)), "valor": r2(v_)})
    itens.sort(key=lambda x: x["valor"], reverse=True)
    total = r2(sum(i["valor"] for i in itens))
    acum = 0.0
    for i in itens:
        antes = acum
        acum += i["valor"]
        i["pct"] = round(i["valor"] / total * 100, 1) if total else 0.0
        i["pct_acum"] = round(acum / total * 100, 1) if total else 0.0
        # a classe depende de onde o produto COMEÇA na curva, para o 1.º ser sempre A
        i["classe"] = "A" if (antes / total * 100 if total else 0) < 80 else \
                      "B" if (antes / total * 100) < 95 else "C"
    return {"data": data, "itens": itens, "total": total}


# ---------------------------------------------------------------------------
# 10. Relatório de Gestão (auditoria do período: vendas, compras, recebimentos, estoque)
# ---------------------------------------------------------------------------
CANAIS = ["Caixa", "Banco", "M-Pesa", "e-Mola"]


def canal_de(forma):
    f = (forma or "").strip().lower()
    if f in ("dinheiro", "numerário", "numerario", "caixa"):
        return "Caixa"
    if f in ("transferência", "transferencia", "cartão", "cartao", "cheque", "banco", "pos", "depósito", "deposito"):
        return "Banco"
    if f in ("m-pesa", "mpesa", "m pesa"):
        return "M-Pesa"
    if f in ("e-mola", "emola", "e mola"):
        return "e-Mola"
    return "Sem forma indicada"


def _meses(ini, fim):
    a, b = datetime.strptime(ini, "%Y-%m-%d"), datetime.strptime(fim, "%Y-%m-%d")
    y, m = a.year, a.month
    out = []
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def gestao(conn, ini, fim):
    t0, t1 = ini + "T00:00:00", fim + "T23:59:59"
    razao = carregar_razao(conn, fim)
    credito = _movimentos_a_credito(conn)
    por_mes = {m: {"vendas": 0.0, "compras": 0.0, "lucro": 0.0, "recebido": 0.0, "perdas": 0.0}
               for m in _meses(ini, fim)}

    vendas_cod = {}          # código -> resumo da venda
    compras = {}             # fornecedor -> valor
    perdas, ajustes, neg = [], [], []
    tot = {"vendas": 0.0, "custo": 0.0, "lucro": 0.0, "compras": 0.0, "n_compras": 0}
    for e in razao.values():
        p = e["p"]
        for m in e["movs"]:
            if not (t0 <= m["dt"] <= t1):
                continue
            mes = m["dt"][:7]
            if m["cls"] == "venda":
                cod = credito.get(m["id"]) or m.get("codigo") or f"V-{m['id']:05d}"
                v = vendas_cod.setdefault(cod, {"codigo": cod, "dt": m["dt"], "valor": 0.0, "lucro": 0.0,
                                               "forma": m["forma"], "credito": m["id"] in credito,
                                               "usuario": m["usuario"] or "—", "cliente": m["cliente"] or "",
                                               "itens": []})
                v["valor"] += m["receita"]; v["lucro"] += m["lucro"]
                v["itens"].append(p["nome"])
                tot["vendas"] += m["receita"]; tot["custo"] += m["custo"]; tot["lucro"] += m["lucro"]
                por_mes[mes]["vendas"] += m["receita"]; por_mes[mes]["lucro"] += m["lucro"]
            elif m["cls"] == "compra":
                tot["compras"] += m["val"]; tot["n_compras"] += 1
                por_mes[mes]["compras"] += m["val"]
                k = (conn.execute("SELECT fornecedor FROM movimentos WHERE id_movimento=?", (m["id"],)).fetchone() or {"fornecedor": None})["fornecedor"] or "Sem fornecedor"
                compras[k] = compras.get(k, 0.0) + m["val"]
            elif m["cls"] == "perda":
                por_mes[mes]["perdas"] += -m["val"]
                perdas.append({"data": dmy(m["dt"][:10]), "produto": p["nome"], "qtd": qtd_vista(p, -m["q"]),
                               "un": rotulo_un(p), "motivo": motivo_perda({"subtipo": m["subtipo"]}),
                               "justificacao": justificacao_perda(m["obs"]), "valor": r2(-m["val"]),
                               "usuario": m["usuario"] or "—"})
            elif m["cls"] == "ajuste":
                ajustes.append({"data": dmy(m["dt"][:10]), "produto": p["nome"], "qtd": qtd_vista(p, m["q"]),
                                "un": rotulo_un(p), "valor": r2(m["val"]), "usuario": m["usuario"] or "—"})
    for e in razao.values():
        if e["p"].get("ativo"):
            row = conn.execute("SELECT saldo_atual FROM estoque WHERE id_produto=?", (e["p"]["id_produto"],)).fetchone()
            if row and float(row["saldo_atual"] or 0) < -0.0005:
                neg.append(e["p"]["nome"])

    # --- recebimentos esperados por canal (à vista + recibos de crédito)
    esperado = {c: {"vista": 0.0, "recibos": 0.0} for c in CANAIS}
    esperado["Sem forma indicada"] = {"vista": 0.0, "recibos": 0.0}
    for v in vendas_cod.values():
        if not v["credito"]:
            esperado[canal_de(v["forma"])]["vista"] += v["valor"]
    for r in conn.execute("SELECT * FROM recibos WHERE data_pagamento BETWEEN ? AND ?", (t0, t1)).fetchall():
        esperado[canal_de(r["forma_pagamento"])]["recibos"] += float(r["valor"] or 0)
        mes = (r["data_pagamento"] or "")[:7]
        if mes in por_mes:
            por_mes[mes]["recebido"] += float(r["valor"] or 0)
    for v in vendas_cod.values():
        if not v["credito"] and v["dt"][:7] in por_mes:
            por_mes[v["dt"][:7]]["recebido"] += v["valor"]

    conf = {r["canal"]: r for r in conn.execute(
        "SELECT * FROM conferencias_recebimento WHERE data_ini=? AND data_fim=?", (ini, fim)).fetchall()}
    canais = []
    for c in CANAIS + ["Sem forma indicada"]:
        ex = esperado[c]
        total = r2(ex["vista"] + ex["recibos"])
        if c == "Sem forma indicada" and abs(total) < 0.005:
            continue
        cf = conf.get(c)
        row = {"canal": c, "vista": r2(ex["vista"]), "recibos": r2(ex["recibos"]), "esperado": total,
               "conferido": r2(cf["valor_conferido"]) if cf else None,
               "obs": (cf["observacao"] if cf else "") or "", "diferenca": None, "texto": ""}
        if c == "Sem forma indicada":
            row["texto"] = "Vendas sem forma de pagamento: não é possível confirmar onde o dinheiro entrou."
        elif cf is None:
            row["texto"] = "Por conferir."
        else:
            dif = r2(cf["valor_conferido"] - total)
            row["diferenca"] = dif
            if abs(dif) < 0.005:
                row["texto"] = "Sem diferença."
            elif dif < 0:
                row["texto"] = f"FALTAM {_mt(-dif)} MT: o sistema regista mais recebimentos do que o contado — possível desfalque ou venda não cobrada."
            else:
                row["texto"] = f"SOBRAM {_mt(dif)} MT: foi contado mais do que o sistema regista — possível venda não registada ou erro de lançamento."
        canais.append(row)

    # --- a receber
    ar = conn.execute("""SELECT COALESCE(SUM(saldo),0) s, COUNT(*) n FROM contas_receber
                         WHERE status IN ('Aberta','Parcial') AND data_emissao <= ?""", (t1,)).fetchone()
    ar_atraso = conn.execute("""SELECT COALESCE(SUM(saldo),0) s, COUNT(*) n FROM contas_receber
                         WHERE status IN ('Aberta','Parcial') AND data_vencimento IS NOT NULL
                           AND data_vencimento < ?""", (fim,)).fetchone()

    vend = sorted(vendas_cod.values(), key=lambda v: v["dt"])
    vista_total = sum(v["valor"] for v in vend if not v["credito"])
    cred_total = sum(v["valor"] for v in vend if v["credito"])
    anuladas = faturas_anuladas(conn, ini, fim)
    sem_forma = [v for v in vend if not v["credito"] and not (v["forma"] or "").strip()]
    margem_neg = [{"codigo": v["codigo"], "data": dmy(v["dt"][:10]), "valor": r2(v["valor"]), "lucro": r2(v["lucro"]),
                   "itens": ", ".join(dict.fromkeys(v["itens"])), "usuario": v["usuario"]}
                  for v in vend if v["lucro"] < -0.005]

    bal = balancete(conn, ini, fim)
    t = bal["tot"]
    perdas_tot = r2(sum(x["valor"] for x in perdas))
    aj_neg = r2(sum(x["valor"] for x in ajustes if x["valor"] < 0))
    aj_pos = r2(sum(x["valor"] for x in ajustes if x["valor"] > 0))
    evolucao = [{"mes": f"{k[5:]}/{k[:4]}", **{a: r2(b) for a, b in v.items()}} for k, v in por_mes.items()]

    # --- pontos de atenção, escritos em linguagem corrente
    pontos = []
    for c in canais:
        if c["diferenca"] is not None and abs(c["diferenca"]) >= 0.005:
            pontos.append({"nivel": "alto" if c["diferenca"] < 0 else "medio", "area": c["canal"], "texto": c["texto"]})
        elif c["diferenca"] is None and c["canal"] != "Sem forma indicada" and c["esperado"] > 0.005:
            pontos.append({"nivel": "info", "area": c["canal"],
                           "texto": f"Ainda não foi conferido o valor recebido em {c['canal']} ({_mt(c['esperado'])} MT esperados)."})
    if sem_forma:
        pontos.append({"nivel": "medio", "area": "Vendas",
                       "texto": f"{len(sem_forma)} venda(s) à vista sem forma de pagamento, no valor de "
                                f"{_mt(sum(v['valor'] for v in sem_forma))} MT: " + ", ".join(v["codigo"] for v in sem_forma[:6]) + "."})
    if anuladas:
        pontos.append({"nivel": "medio", "area": "Anulações",
                       "texto": f"{len(anuladas)} venda(s) anulada(s), no valor de {_mt(sum(a['valor'] for a in anuladas))} MT. Confirme os motivos."})
    if margem_neg:
        pontos.append({"nivel": "medio", "area": "Preços",
                       "texto": f"{len(margem_neg)} venda(s) abaixo do custo (prejuízo de {_mt(-sum(x['lucro'] for x in margem_neg))} MT)."})
    if perdas_tot > 0.004:
        pontos.append({"nivel": "medio", "area": "Perdas", "texto": f"Perdas de estoque no período: {_mt(perdas_tot)} MT ({len(perdas)} ocorrência(s))."})
    if neg:
        pontos.append({"nivel": "alto", "area": "Estoque", "texto": f"{len(neg)} produto(s) com saldo negativo: " + ", ".join(neg[:5]) + ("…" if len(neg) > 5 else "") + "."})
    if not t.get("identidade_ok", True):
        pontos.append({"nivel": "alto", "area": "Estoque", "texto": "A conta do estoque (inicial + compras − custo das vendas − perdas e ajustes = final) não fecha."})
    if float(ar_atraso["s"] or 0) > 0.004:
        pontos.append({"nivel": "medio", "area": "Clientes", "texto": f"{ar_atraso['n']} fatura(s) em atraso, num total de {_mt(ar_atraso['s'])} MT."})
    # --- contagens físicas (inventários) feitas no período e as suas justificações
    contagens = []
    for a in conn.execute("""SELECT a.id_auditoria, a.tipo, a.estado,
                  COALESCE((SELECT SUBSTR(MIN(c.data_contagem),1,10) FROM auditoria_contagens c
                            WHERE c.id_auditoria=a.id_auditoria), a.data_fim) AS dia
           FROM auditorias a ORDER BY dia, a.id_auditoria""").fetchall():
        if not (ini <= a["dia"] <= fim):
            continue
        di = divergencias_inventario(conn, a["id_auditoria"])
        if not di:
            continue
        com = [l for l in di["linhas"] if l["com_div"]]
        contagens.append({"id": a["id_auditoria"], "dia": a["dia"], "data": dmy(a["dia"]), "estado": di["estado"]["texto"],
                          "n_contados": di["n_contados"], "n_com": len(com), "perdas": di["perdas"], "sobras": di["sobras"],
                          "contador": di["contador"],
                          "linhas": [{**l, "justificada": bool((l["motivo"] or l["obs"]).strip())} for l in com]})
    cont_sem_just = [(c["data"], l["produto"]) for c in contagens for l in c["linhas"] if not l["justificada"]]
    cont_perdas = r2(sum(c["perdas"] for c in contagens))
    cont_sobras = r2(sum(c["sobras"] for c in contagens))
    if cont_perdas < -0.004:
        pontos.append({"nivel": "alto", "area": "Contagem física",
                       "texto": f"Nas contagens físicas faltou estoque no valor de {_mt(-cont_perdas)} MT em relação ao sistema."})
    if cont_sem_just:
        pontos.append({"nivel": "alto", "area": "Contagem física",
                       "texto": f"{len(cont_sem_just)} diferença(s) de contagem sem justificação: "
                                + ", ".join(f"{p} ({d})" for d, p in cont_sem_just[:5]) + ("…" if len(cont_sem_just) > 5 else "") + "."})
    if cont_sobras > 0.004:
        pontos.append({"nivel": "info", "area": "Contagem física",
                       "texto": f"Nas contagens físicas sobrou estoque no valor de {_mt(cont_sobras)} MT em relação ao sistema."})

    graves = sum(1 for p in pontos if p["nivel"] == "alto")
    if graves:
        veredicto = {"nivel": "alto", "texto": f"Foram encontrados {graves} problema(s) grave(s) que exigem verificação."}
    elif any(p["nivel"] == "medio" for p in pontos):
        veredicto = {"nivel": "medio", "texto": "Sem desfalques confirmados, mas há pontos a acompanhar."}
    elif any(c["diferenca"] is None and c["canal"] != "Sem forma indicada" and c["esperado"] > 0.005 for c in canais):
        veredicto = {"nivel": "info", "texto": "Sem diferenças encontradas, mas faltam conferir recebimentos."}
    else:
        veredicto = {"nivel": "ok", "texto": "Sem diferenças nem ocorrências relevantes no período."}

    por_forn = sorted(({"fornecedor": k, "valor": r2(v)} for k, v in compras.items()), key=lambda x: -x["valor"])
    return {
        "ini": ini, "fim": fim, "veredicto": veredicto, "pontos": pontos,
        "vendas": {"total": r2(tot["vendas"]), "custo": r2(tot["custo"]), "lucro": r2(tot["lucro"]),
                   "margem": round(tot["lucro"] / tot["vendas"] * 100, 1) if tot["vendas"] else None,
                   "n": len(vend), "vista": r2(vista_total), "credito": r2(cred_total)},
        "compras": {"total": r2(tot["compras"]), "n": tot["n_compras"], "fornecedores": por_forn[:8]},
        "canais": canais,
        "rec_esperado_total": r2(sum(c["esperado"] for c in canais)),
        "rec_conferido_total": r2(sum(c["conferido"] for c in canais if c["conferido"] is not None)),
        "recibos_total": r2(sum(v["recibos"] for v in esperado.values())),
        "a_receber": {"valor": r2(ar["s"]), "n": ar["n"], "atraso": r2(ar_atraso["s"]), "n_atraso": ar_atraso["n"]},
        "estoque": {"inicial": t["inicial"], "compras": t["compras"], "custo_vendas": t["custo_vendas"],
                    "perdas_aj": t["perdas_aj"], "final": t["final"], "fecha": bool(t.get("identidade_ok", True))},
        "perdas": perdas, "perdas_total": perdas_tot, "ajustes": ajustes, "ajustes_neg": aj_neg, "ajustes_pos": aj_pos,
        "negativos": neg, "anuladas": anuladas, "anuladas_total": r2(sum(a["valor"] for a in anuladas)),
        "sem_forma": [{"codigo": v["codigo"], "valor": r2(v["valor"]), "usuario": v["usuario"]} for v in sem_forma],
        "margem_neg": margem_neg, "evolucao": evolucao, "contagens": contagens,
        "cont_perdas": cont_perdas, "cont_sobras": cont_sobras,
    }


# =============================================================================
# RELATÓRIO DE GESTÃO / AUDITORIA COMPLETA
# =============================================================================
def gestao_completa(conn, ini, fim):
    """
    Relatório de gestão completo para auditoria.
    Retorna estrutura com todas as secções necessárias para auditoria plena.
    """
    from datetime import timedelta
    t0, t1 = ini + "T00:00:00", fim + "T23:59:59"

    # --- 1. Reutilizar dados base do gestao() ---
    base = gestao(conn, ini, fim)

    # --- 2. VENDAS DETALHADAS ---
    razao = carregar_razao(conn, fim)
    credito = _movimentos_a_credito(conn)

    vendas_detalhadas = []
    vendas_por_forma = {}
    vendas_abaixo_custo = []
    vendas_acima_tabela = []
    vendas_sem_forma = []
    vendedores_stats = {}

    for e in razao.values():
        p = e["p"]
        for m in e["movs"]:
            if not (t0 <= m["dt"] <= t1):
                continue
            if m["cls"] != "venda":
                continue

            codigo = credito.get(m["id"]) or m.get("codigo") or f"V-{m['id']:05d}"
            eh_credito = m["id"] in credito
            forma = m["forma"] or ("A crédito" if eh_credito else "Sem forma")
            usuario = m["usuario"] or "—"
            cliente = m["cliente"] or "—"
            preco_unit = float(m.get("preco") or m.get("preco_unitario") or 0)
            preco_custo = float(p["preco_custo"] or 0)
            preco_venda_tabela = float(p["preco_venda"] or 0)
            valor = float(m["receita"] or 0)
            custo = float(m["custo"] or 0)
            lucro = valor - custo
            qtd = float(-m["q"])

            # Venda detalhada
            vendas_detalhadas.append({
                "codigo": codigo,
                "data": dmy(m["dt"][:10]),
                "hora": m["dt"][11:16] if len(m["dt"]) > 16 else "",
                "produto": p["nome"],
                "produto_codigo": p["codigo"],
                "categoria": p["categoria_nome"] or "—",
                "qtd": qtd_vista(p, qtd),
                "un": rotulo_un(p),
                "preco_unit": r2(preco_unit),
                "preco_custo": r2(preco_custo),
                "preco_venda_tabela": r2(preco_venda_tabela),
                "valor": r2(valor),
                "custo": r2(custo),
                "lucro": r2(lucro),
                "margem": round(lucro / valor * 100, 1) if valor else None,
                "forma": forma,
                "eh_credito": eh_credito,
                "cliente": cliente,
                "usuario": usuario,
                "id_usuario": m["id_usuario"],
            })

            # Por forma de pagamento
            if forma not in vendas_por_forma:
                vendas_por_forma[forma] = {"valor": 0.0, "qtd": 0, "credito": eh_credito}
            vendas_por_forma[forma]["valor"] += valor
            vendas_por_forma[forma]["qtd"] += 1

            # Abaixo do custo (prejuízo)
            if preco_unit < preco_custo - 0.005:
                vendas_abaixo_custo.append({
                    "codigo": codigo,
                    "data": dmy(m["dt"][:10]),
                    "produto": p["nome"],
                    "preco_unit": r2(preco_unit),
                    "preco_custo": r2(preco_custo),
                    "diferenca": r2(preco_custo - preco_unit),
                    "diferenca_pct": round((preco_custo - preco_unit) / preco_custo * 100, 1) if preco_custo else 0,
                    "valor": r2(valor),
                    "prejuizo": r2(custo - valor),
                    "usuario": usuario,
                })

            # Acima do preço de tabela (catálogo)
            if preco_venda_tabela > 0 and preco_unit > preco_venda_tabela + 0.005:
                vendas_acima_tabela.append({
                    "codigo": codigo,
                    "data": dmy(m["dt"][:10]),
                    "produto": p["nome"],
                    "preco_tabela": r2(preco_venda_tabela),
                    "preco_praticado": r2(preco_unit),
                    "diferenca": r2(preco_unit - preco_venda_tabela),
                    "diferenca_pct": round((preco_unit - preco_venda_tabela) / preco_venda_tabela * 100, 1),
                    "valor": r2(valor),
                    "usuario": usuario,
                })

            # Sem forma de pagamento
            if not eh_credito and not (m["forma"] or "").strip():
                vendas_sem_forma.append({
                    "codigo": codigo,
                    "data": dmy(m["dt"][:10]),
                    "valor": r2(valor),
                    "usuario": usuario,
                })

            # Stats por vendedor
            if usuario not in vendedores_stats:
                vendedores_stats[usuario] = {
                    "usuario": usuario, "id_usuario": m["id_usuario"],
                    "vendas_valor": 0.0, "vendas_qtd": 0, "lucro_total": 0.0,
                    "compras_valor": 0.0, "perdas_valor": 0.0, "ajustes_valor": 0.0,
                    "anulacoes": 0, "perdas_registadas": 0, "ajustes_aprovados": 0,
                    "conferencias": 0,
                }
            vendedores_stats[usuario]["vendas_valor"] += valor
            vendedores_stats[usuario]["vendas_qtd"] += 1
            vendedores_stats[usuario]["lucro_total"] += lucro

    # Ordenar vendas por forma
    vendas_por_forma_lista = sorted(
        [{"forma": k, "valor": r2(v["valor"]), "qtd": v["qtd"], "credito": v["credito"]}
         for k, v in vendas_por_forma.items()],
        key=lambda x: -x["valor"]
    )

    # --- 3. FATURAS A CRÉDITO NO PERÍODO ---
    faturas_credito = []

    faturas_rows = conn.execute(
        """SELECT c.*, u.nome AS usuario_nome FROM contas_receber c
           LEFT JOIN usuarios u ON u.id_usuario=c.id_usuario
           WHERE c.data_emissao BETWEEN ? AND ? ORDER BY c.data_emissao""",
        (t0, t1)).fetchall()

    for f in faturas_rows:
        numero = f["numero_fatura"] or f"FT-{f['id_conta']:05d}"
        venc = (f["data_vencimento"] or "")[:10]
        hoje = datetime.now().date()
        try:
            dv = datetime.strptime(venc, "%Y-%m-%d").date() if venc else None
        except ValueError:
            dv = None
        dias_atraso = (hoje - dv).days if dv and dv < hoje else 0
        status = f["status"]
        if status in ("Aberta", "Parcial") and dv and dv < hoje:
            status_classe = "atraso"
        elif status == "Liquidada":
            status_classe = "ok"
        elif status == "Cancelada":
            status_classe = "cancelada"
        else:
            status_classe = "avencer"

        faturas_credito.append({
            "numero": numero,
            "cliente": f["nome_cliente"],
            "nif": f["nif_cliente"] or "—",
            "emissao": dmy(f["data_emissao"][:10]),
            "vencimento": dmy(venc) if venc else "—",
            "valor": r2(f["valor_total"]),
            "pago": r2(f["valor_pago"]),
            "saldo": r2(f["saldo"]),
            "status": f["status"],
            "status_classe": status_classe,
            "dias_atraso": dias_atraso,
            "usuario": f["usuario_nome"] or "—",
            "observacoes": f["observacoes"] or "",
        })

    # --- 4. RECEBIMENTOS DE CRÉDITO (RECIBOS) ---
    recibos_rows = conn.execute(
        """SELECT r.*, c.nome_cliente, c.numero_fatura, u.nome AS usuario_nome
           FROM recibos r
           JOIN contas_receber c ON c.id_conta=r.id_conta
           LEFT JOIN usuarios u ON u.id_usuario=r.id_usuario
           WHERE r.data_pagamento BETWEEN ? AND ?
           ORDER BY r.data_pagamento""",
        (t0, t1)).fetchall()

    recibos = []
    for r in recibos_rows:
        numero_fat = r["numero_fatura"] or f"FT-{r['id_conta']:05d}"
        recibos.append({
            "numero": r["numero"],
            "fatura": numero_fat,
            "cliente": r["nome_cliente"],
            "data": dmy(r["data_pagamento"][:10]),
            "valor": r2(r["valor"]),
            "forma": r["forma_pagamento"] or "—",
            "usuario": r["usuario_nome"] or "—",
            "observacoes": r["observacoes"] or "",
        })

    # --- 5. CONTAS A RECEBER - ENVELHECIMENTO ---
    hoje = datetime.now().date()
    ar_rows = conn.execute(
        """SELECT c.*, u.nome AS usuario_nome FROM contas_receber c
           LEFT JOIN usuarios u ON u.id_usuario=c.id_usuario
           WHERE c.status IN ('Aberta','Parcial') AND c.data_emissao <= ?""",
        (t1,)).fetchall()

    envelhecimento = {"a_vencer": 0.0, "d1_30": 0.0, "d31_60": 0.0, "d61_90": 0.0, "d90_mais": 0.0}
    envelhecimento_n = {"a_vencer": 0, "d1_30": 0, "d31_60": 0, "d61_90": 0, "d90_mais": 0}
    devedores = []

    for f in ar_rows:
        saldo = float(f["saldo"] or 0)
        if saldo <= 0.005:
            continue
        venc = (f["data_vencimento"] or "")[:10]
        try:
            dv = datetime.strptime(venc, "%Y-%m-%d").date() if venc else None
        except ValueError:
            dv = None

        if not dv or dv >= hoje:
            faixa = "a_vencer"
        else:
            dias = (hoje - dv).days
            if dias <= 30:
                faixa = "d1_30"
            elif dias <= 60:
                faixa = "d31_60"
            elif dias <= 90:
                faixa = "d61_90"
            else:
                faixa = "d90_mais"

        envelhecimento[faixa] += saldo
        envelhecimento_n[faixa] += 1

        devedores.append({
            "cliente": f["nome_cliente"],
            "nif": f["nif_cliente"] or "—",
            "telefone": f["telefone_cliente"] or "—",
            "email": f["email_cliente"] or "—",
            "saldo": r2(saldo),
            "vencimento": dmy(venc) if venc else "—",
            "dias_atraso": (hoje - dv).days if dv and dv < hoje else 0,
            "n_faturas": 1,
            "fatura_mais_antiga": dmy(f["data_emissao"][:10]),
        })

    # Agregar devedores por cliente
    devedores_agreg = {}
    for d in devedores:
        k = d["cliente"]
        if k not in devedores_agreg:
            devedores_agreg[k] = d
        else:
            devedores_agreg[k]["saldo"] = r2(float(devedores_agreg[k]["saldo"]) + float(d["saldo"]))
            devedores_agreg[k]["n_faturas"] += 1
            if d["fatura_mais_antiga"] < devedores_agreg[k]["fatura_mais_antiga"]:
                devedores_agreg[k]["fatura_mais_antiga"] = d["fatura_mais_antiga"]
            if d["dias_atraso"] > devedores_agreg[k]["dias_atraso"]:
                devedores_agreg[k]["dias_atraso"] = d["dias_atraso"]

    devedores_lista = sorted(devedores_agreg.values(), key=lambda x: -float(x["saldo"]))[:20]

    # --- 6. COMPRAS DETALHADAS ---
    compras_detalhadas = []
    compras_acima_ultimo_custo = []
    fornecedores_stats = {}

    for e in razao.values():
        p = e["p"]
        for m in e["movs"]:
            if not (t0 <= m["dt"] <= t1):
                continue
            if m["cls"] != "compra":
                continue

            fornecedor = m.get("doc") or "Sem fornecedor"
            if m.get("doc"):
                fornecedor = m["doc"]
            elif m.get("fornecedor"):
                fornecedor = m["fornecedor"]

            preco_unit = float(m.get("preco") or m.get("preco_unitario") or 0)
            ultimo_custo = float(p.get("ultimo_preco_entrada") or p["preco_custo"] or 0)
            valor = float(m["val"] or 0)
            qtd = float(m["q"])

            compras_detalhadas.append({
                "data": dmy(m["dt"][:10]),
                "produto": p["nome"],
                "produto_codigo": p["codigo"],
                "categoria": p["categoria_nome"] or "—",
                "qtd": qtd_vista(p, qtd),
                "un": rotulo_un(p),
                "preco_unit": r2(preco_unit),
                "ultimo_custo": r2(ultimo_custo),
                "valor": r2(valor),
                "fornecedor": fornecedor,
                "nota_fiscal": m.get("nota_fiscal") or "—",
                "usuario": m["usuario"] or "—",
            })

            # Acima do último custo
            if ultimo_custo > 0 and preco_unit > ultimo_custo + 0.005:
                compras_acima_ultimo_custo.append({
                    "data": dmy(m["dt"][:10]),
                    "produto": p["nome"],
                    "ultimo_custo": r2(ultimo_custo),
                    "preco_praticado": r2(preco_unit),
                    "diferenca": r2(preco_unit - ultimo_custo),
                    "diferenca_pct": round((preco_unit - ultimo_custo) / ultimo_custo * 100, 1),
                    "fornecedor": fornecedor,
                    "usuario": m["usuario"] or "—",
                })

            # Stats por fornecedor
            if fornecedor not in fornecedores_stats:
                fornecedores_stats[fornecedor] = {"valor": 0.0, "qtd": 0, "ultima": ""}
            fornecedores_stats[fornecedor]["valor"] += valor
            fornecedores_stats[fornecedor]["qtd"] += 1
            if m["dt"] > fornecedores_stats[fornecedor]["ultima"]:
                fornecedores_stats[fornecedor]["ultima"] = m["dt"]

    fornecedores_lista = sorted(
        [{"fornecedor": k, "valor": r2(v["valor"]), "qtd": v["qtd"], "ultima": dmy(v["ultima"][:10])}
         for k, v in fornecedores_stats.items()],
        key=lambda x: -x["valor"]
    )

    # --- 7. ESTOQUE - RECONCILIAÇÃO DETALHADA ---
    bal = balancete(conn, ini, fim)
    estoque_diferencas = []
    estoque_negativos = []
    estoque_abaixo_minimo = []
    estoque_acima_maximo = []
    estoque_parado = []

    for e in razao.values():
        p = e["p"]
        r = resumir_produto(e, ini, fim)
        if not _relevante(e, r):
            continue

        row = conn.execute("SELECT saldo_atual FROM estoque WHERE id_produto=?", (p["id_produto"],)).fetchone()
        saldo_sistema = float(row["saldo_atual"] or 0) if row else 0.0
        qtd_movimentos = r["q_fim"]

        diff_qtd = round(saldo_sistema - qtd_movimentos, 3)
        custo_unit = float(p["preco_custo"] or 0)
        diff_valor = r2(diff_qtd * custo_unit)

        if abs(diff_qtd) > 0.0005:
            estoque_diferencas.append({
                "produto": p["nome"],
                "codigo": p["codigo"],
                "categoria": p["categoria_nome"] or "—",
                "saldo_movimentos": qtd_vista(p, qtd_movimentos),
                "saldo_sistema": qtd_vista(p, saldo_sistema),
                "diff_qtd": diff_qtd,
                "diff_valor": diff_valor,
                "un": rotulo_un(p),
            })

        if saldo_sistema < -0.0005:
            ult_mov = conn.execute(
                """SELECT m.data_hora, u.nome AS usuario FROM movimentos m
                   LEFT JOIN usuarios u ON u.id_usuario=m.id_usuario
                   WHERE m.id_produto=? ORDER BY m.data_hora DESC LIMIT 1""",
                (p["id_produto"],)).fetchone()
            estoque_negativos.append({
                "produto": p["nome"],
                "codigo": p["codigo"],
                "categoria": p["categoria_nome"] or "—",
                "saldo": qtd_vista(p, saldo_sistema),
                "un": rotulo_un(p),
                "ultimo_movimento": dmy(ult_mov["data_hora"][:10]) if ult_mov else "—",
                "usuario": ult_mov["usuario"] if ult_mov else "—",
            })

        minimo = float(p["estoque_minimo"] or 0)
        if saldo_sistema > 0 and saldo_sistema < minimo - 0.0005:
            estoque_abaixo_minimo.append({
                "produto": p["nome"],
                "codigo": p["codigo"],
                "categoria": p["categoria_nome"] or "—",
                "saldo": qtd_vista(p, saldo_sistema),
                "minimo": qtd_vista(p, minimo),
                "un": rotulo_un(p),
            })

        maximo = p.get("estoque_maximo")
        if maximo and saldo_sistema > float(maximo) + 0.0005:
            estoque_acima_maximo.append({
                "produto": p["nome"],
                "codigo": p["codigo"],
                "categoria": p["categoria_nome"] or "—",
                "saldo": qtd_vista(p, saldo_sistema),
                "maximo": qtd_vista(p, float(maximo)),
                "un": rotulo_un(p),
            })

        # Parado: sem movimento no período e saldo > 0
        if r["n_mov"] == 0 and saldo_sistema > 0.0005:
            ult_mov = conn.execute(
                "SELECT data_hora FROM movimentos WHERE id_produto=? ORDER BY data_hora DESC LIMIT 1",
                (p["id_produto"],)).fetchone()
            dias_parado = 0
            if ult_mov:
                try:
                    dt_ult = datetime.strptime(ult_mov["data_hora"][:10], "%Y-%m-%d").date()
                    dias_parado = (datetime.now().date() - dt_ult).days
                except:
                    pass
            estoque_parado.append({
                "produto": p["nome"],
                "codigo": p["codigo"],
                "categoria": p["categoria_nome"] or "—",
                "saldo": qtd_vista(p, saldo_sistema),
                "valor": r2(saldo_sistema * custo_unit),
                "un": rotulo_un(p),
                "dias_parado": dias_parado,
                "ultimo_movimento": dmy(ult_mov["data_hora"][:10]) if ult_mov else "Nunca",
            })

    estoque_parado.sort(key=lambda x: -x["dias_parado"])

    # Estoque por categoria (valorização)
    categorias_valor = {}
    for e in razao.values():
        p = e["p"]
        r = resumir_produto(e, ini, fim)
        if not _relevante(e, r):
            continue
        cat = p["categoria_nome"] or "Sem categoria"
        if cat not in categorias_valor:
            categorias_valor[cat] = {"inicial": 0.0, "compras": 0.0, "custo_vendas": 0.0,
                                     "perdas_aj": 0.0, "final": 0.0}
        categorias_valor[cat]["inicial"] += r["v_ini"]
        categorias_valor[cat]["compras"] += r["compra_v"]
        categorias_valor[cat]["custo_vendas"] += r["venda_custo"]
        categorias_valor[cat]["perdas_aj"] += r["perdas_aj_v"]
        categorias_valor[cat]["final"] += r["v_fim"]

    total_final = sum(c["final"] for c in categorias_valor.values())
    estoque_por_categoria = []
    for cat, vals in sorted(categorias_valor.items(), key=lambda x: -x[1]["final"]):
        estoque_por_categoria.append({
            "categoria": cat,
            "inicial": r2(vals["inicial"]),
            "compras": r2(vals["compras"]),
            "custo_vendas": r2(vals["custo_vendas"]),
            "perdas_aj": r2(vals["perdas_aj"]),
            "final": r2(vals["final"]),
            "pct": round(vals["final"] / total_final * 100, 1) if total_final else 0,
        })

    # --- 8. PERDAS DETALHADAS ---
    perdas_por_motivo = {}
    perdas_sem_justificativa = []
    perdas_por_usuario = {}

    for x in base["perdas"]:
        motivo = x["motivo"]
        if motivo not in perdas_por_motivo:
            perdas_por_motivo[motivo] = {"n": 0, "qtd": 0.0, "valor": 0.0}
        perdas_por_motivo[motivo]["n"] += 1
        try:
            qtd_num = float(str(x["qtd"]).replace(",", "").split()[0])
        except:
            qtd_num = 0
        perdas_por_motivo[motivo]["qtd"] += qtd_num
        perdas_por_motivo[motivo]["valor"] += float(x["valor"] or 0)

        # Sem justificativa
        if not (x.get("justificacao") or "").strip() or x.get("justificacao") == "—":
            perdas_sem_justificativa.append(x)

        # Por usuário
        user = x["usuario"]
        if user not in perdas_por_usuario:
            perdas_por_usuario[user] = {"n": 0, "valor": 0.0}
        perdas_por_usuario[user]["n"] += 1
        perdas_por_usuario[user]["valor"] += float(x["valor"] or 0)

        # Stats vendedor
        if user in vendedores_stats:
            vendedores_stats[user]["perdas_valor"] += float(x["valor"] or 0)
            vendedores_stats[user]["perdas_registadas"] += 1

    perdas_por_motivo_lista = sorted(
        [{"motivo": k, "n": v["n"], "qtd": r2(v["qtd"]), "valor": r2(v["valor"]),
          "pct": round(v["valor"] / base["perdas_total"] * 100, 1) if base["perdas_total"] else 0}
         for k, v in perdas_por_motivo.items()],
        key=lambda x: -x["valor"]
    )

    perdas_por_usuario_lista = sorted(
        [{"usuario": k, "n": v["n"], "valor": r2(v["valor"])} for k, v in perdas_por_usuario.items()],
        key=lambda x: -x["valor"]
    )

    # --- 9. AJUSTES ---
    ajustes_pos = [a for a in base["ajustes"] if float(a["valor"] or 0) > 0]
    ajustes_neg = [a for a in base["ajustes"] if float(a["valor"] or 0) < 0]
    ajustes_sem_justificativa = [a for a in base["ajustes"] if not (a.get("motivo") or "").strip()]

    # --- 10. AUDITORIAS / CONTAGENS FÍSICAS ---
    contagens = base["contagens"]
    diffs_nao_justificadas = []
    for c in contagens:
        for l in c.get("linhas", []):
            if not l.get("justificada", False):
                diffs_nao_justificadas.append({
                    "inventario": c["id"],
                    "data": c["data"],
                    "produto": l["produto"],
                    "sistema": l["sistema"],
                    "contado": l["contagem"],
                    "diff_qtd": l["dif"],
                    "diff_valor": l["dif_v"],
                    "motivo": l.get("motivo", ""),
                    "obs": l.get("obs", ""),
                })

    # Recontagens
    recontagens = []
    for c in contagens:
        for l in c.get("linhas", []):
            if l.get("recontagem_solicitada"):
                recontagens.append({
                    "inventario": c["id"],
                    "data": c["data"],
                    "produto": l["produto"],
                    "contador_original": l.get("contador", ""),
                    "contador_recontagem": l.get("recontagem_contador_id", ""),
                    "data_recontagem": l.get("recontagem_data", ""),
                })

    # --- 11. ANOMALIAS AUTOMÁTICAS ---
    anomalias = []

    # 10.1 Estoque negativo
    for item in estoque_negativos:
        anomalias.append({
            "tipo": "Estoque negativo",
            "severidade": "CRITICO",
            "produto": item["produto"],
            "codigo": item["codigo"],
            "detalhe": f"Saldo {item['saldo']} {item['un']} — Último mov: {item['ultimo_movimento']} por {item['usuario']}",
        })

    # 10.2 Custo zerado
    custo_zero = conn.execute(
        """SELECT codigo, nome FROM produtos WHERE ativo=1 AND (preco_custo IS NULL OR preco_custo = 0)""").fetchall()
    for r in custo_zero:
        anomalias.append({
            "tipo": "Custo zerado",
            "severidade": "ALERTA",
            "produto": r["nome"],
            "codigo": r["codigo"],
            "detalhe": "Produto ativo sem custo definido",
        })

    # 10.3 Ajustes de auditoria sem justificativa
    aj_sem_just = conn.execute(
        """SELECT a.id_ajuste, p.codigo, p.nome, a.quantidade_ajuste, a.motivo, a.data_aprovacao, u.nome AS aprovador
           FROM auditoria_ajustes a
           JOIN produtos p ON p.id_produto=a.id_produto
           LEFT JOIN usuarios u ON u.id_usuario=a.id_usuario_aprovador
           WHERE (a.motivo IS NULL OR a.motivo='') AND a.data_aprovacao BETWEEN ? AND ?""",
        (t0, t1)).fetchall()
    for r in aj_sem_just:
        anomalias.append({
            "tipo": "Ajuste sem justificativa",
            "severidade": "CRITICO",
            "produto": r["nome"],
            "codigo": r["codigo"],
            "detalhe": f"{float(r['quantidade_ajuste']):.2f} em {r['data_aprovacao'][:10]} — Aprovador: {r['aprovador'] or '—'}",
        })

    # 10.4 Vendas com preço fora da faixa
    if vendas_abaixo_custo:
        anomalias.append({
            "tipo": "Vendas abaixo do custo",
            "severidade": "ALERTA",
            "produto": f"{len(vendas_abaixo_custo)} ocorrência(s)",
            "codigo": "",
            "detalhe": f"Prejuízo total: {r2(sum(float(v['prejuizo']) for v in vendas_abaixo_custo))} MT",
        })

    if vendas_acima_tabela:
        anomalias.append({
            "tipo": "Vendas acima do preço de tabela",
            "severidade": "ATENCAO",
            "produto": f"{len(vendas_acima_tabela)} ocorrência(s)",
            "codigo": "",
            "detalhe": f"Diferença total vs tabela: {r2(sum(float(v['diferenca']) for v in vendas_acima_tabela))} MT",
        })

    # 10.5 Usuários com muitas anulações
    anulações_por_user = {}
    for a in base["anuladas"]:
        user = a["usuario"]
        anulações_por_user[user] = anulações_por_user.get(user, 0) + 1
    for user, n in sorted(anulações_por_user.items(), key=lambda x: -x[1])[:5]:
        if n >= 3:
            anomalias.append({
                "tipo": "Muitas anulações",
                "severidade": "ATENCAO",
                "produto": user,
                "codigo": "",
                "detalhe": f"{n} anulação(ões) no período",
            })

    # 10.6 Usuários com muitas perdas
    for user, n in sorted([(k, v["n"]) for k, v in perdas_por_usuario.items()], key=lambda x: -x[1])[:5]:
        if n >= 5:
            anomalias.append({
                "tipo": "Muitas perdas registadas",
                "severidade": "ATENCAO",
                "produto": user,
                "codigo": "",
                "detalhe": f"{n} perda(s) registada(s) no período",
            })

    # 10.7 Diferenças de caixa não conferidas
    for c in base["canais"]:
        if c["canal"] != "Sem forma indicada" and c["conferido"] is None and c["esperado"] > 0.005:
            anomalias.append({
                "tipo": "Caixa não conferido",
                "severidade": "ALERTA",
                "produto": c["canal"],
                "codigo": "",
                "detalhe": f"Esperado: {r2(c['esperado'])} MT — Pendente de conferência",
            })

    # 10.8 Faturas vencidas > 90 dias sem cobrança
    faturas_antigas = conn.execute(
        """SELECT c.nome_cliente, COUNT(*) as n, SUM(c.saldo) as total
           FROM contas_receber c
           WHERE c.status IN ('Aberta','Parcial')
             AND c.data_vencimento IS NOT NULL
             AND c.data_vencimento < date(?, '-90 days')
           GROUP BY c.nome_cliente
           HAVING total > 0.005
           ORDER BY total DESC LIMIT 10""",
        (fim,)).fetchall()
    for r in faturas_antigas:
        anomalias.append({
            "tipo": "Faturas > 90 dias sem cobrança",
            "severidade": "ALERTA",
            "produto": r["nome_cliente"],
            "codigo": "",
            "detalhe": f"{r['n']} fatura(s), {r2(r['total'])} MT em atraso > 90 dias",
        })

    # 10.9 Validade
    pv = perdas_validade(conn, ini, fim)
    perdas_valid = pv["ocorrencias"]
    validade = pv["itens"]
    for v in validade:
        if v["dias_restantes"] is not None and v["dias_restantes"] <= 0:
            sev = "CRITICO"
        elif v["dias_restantes"] is not None and v["dias_restantes"] <= 30:
            sev = "ALERTA"
        else:
            sev = "ATENCAO"
        anomalias.append({
            "tipo": "Produto vencido / a vencer",
            "severidade": sev,
            "produto": v["nome"],
            "codigo": v["codigo"],
            "detalhe": f"Validade: {v['validade']} ({v['dias_restantes']} dias) — Saldo: {v['saldo']} {v['unidade']}",
        })

    # 10.10 Gaps em código de venda
    gaps = conn.execute(
        """SELECT CAST(SUBSTR(codigo_venda, 3) AS INTEGER) AS num
           FROM movimentos
           WHERE codigo_venda LIKE 'V-%' AND data_hora BETWEEN ? AND ?
           UNION
           SELECT CAST(SUBSTR(codigo_venda, 3) AS INTEGER) AS num
           FROM vendas_anuladas
           WHERE codigo_venda LIKE 'V-%' AND data_anulacao BETWEEN ? AND ?""",
        (t0, t1, t0, t1)).fetchall()
    nums = sorted([r["num"] for r in gaps if r["num"]])
    if nums:
        expected = set(range(nums[0], nums[-1] + 1))
        missing = sorted(expected - set(nums))
        if missing:
            anomalias.append({
                "tipo": "Gaps em códigos de venda",
                "severidade": "ATENCAO",
                "produto": f"{len(missing)} código(s) faltando",
                "codigo": "",
                "detalhe": f"Faltam: {', '.join(f'V-{n:06d}' for n in missing[:10])}" + ("..." if len(missing) > 10 else ""),
            })

    # --- 12. EVOLUÇÃO MENSAL ---
    evolucao_mensal = base["evolucao"]

    # --- 13. USUÁRIOS - RASTREABILIDADE ---
    for e in razao.values():
        for m in e["movs"]:
            if not (t0 <= m["dt"] <= t1):
                continue
            if m["cls"] == "compra":
                user = m["usuario"] or "—"
                if user in vendedores_stats:
                    vendedores_stats[user]["compras_valor"] += float(m["val"] or 0)

    for a in base["anuladas"]:
        user = a["usuario"]
        if user in vendedores_stats:
            vendedores_stats[user]["anulacoes"] += 1

    for r in recibos_rows:
        user = r["usuario_nome"] or "—"
        if user in vendedores_stats:
            vendedores_stats[user]["conferencias"] += 1

    ajustes_aprov = conn.execute(
        """SELECT u.nome, COUNT(*) as n, SUM(a.quantidade_ajuste * p.preco_custo) as valor
           FROM auditoria_ajustes a
           JOIN produtos p ON p.id_produto=a.id_produto
           JOIN usuarios u ON u.id_usuario=a.id_usuario_aprovador
           WHERE a.data_aprovacao BETWEEN ? AND ?
           GROUP BY u.nome""",
        (t0, t1)).fetchall()
    for r in ajustes_aprov:
        user = r["nome"]
        if user in vendedores_stats:
            vendedores_stats[user]["ajustes_aprovados"] = r["n"]
            vendedores_stats[user]["ajustes_valor"] = float(r["valor"] or 0)

    usuarios_lista = []
    for user, stats in vendedores_stats.items():
        usuarios_lista.append({
            "usuario": user,
            "perfil": conn.execute("SELECT perfil FROM usuarios WHERE nome=?", (user,)).fetchone(),
            "vendas_valor": r2(stats["vendas_valor"]),
            "vendas_qtd": stats["vendas_qtd"],
            "lucro_total": r2(stats["lucro_total"]),
            "compras_valor": r2(stats["compras_valor"]),
            "perdas_valor": r2(stats["perdas_valor"]),
            "perdas_registadas": stats["perdas_registadas"],
            "ajustes_valor": r2(stats["ajustes_valor"]),
            "ajustes_aprovados": stats["ajustes_aprovados"],
            "anulacoes": stats["anulacoes"],
            "recibos_emitidos": stats["conferencias"],
        })

    for u in usuarios_lista:
        if u["perfil"]:
            u["perfil"] = u["perfil"]["perfil"]
        else:
            u["perfil"] = "—"

    usuarios_lista.sort(key=lambda x: -float(x["vendas_valor"]))

    # --- 14. VEREDITO EXECUTIVO ---
    graves = 0
    alertas = 0
    atencoes = 0
    for a in anomalias:
        if a["severidade"] == "CRITICO":
            graves += 1
        elif a["severidade"] == "ALERTA":
            alertas += 1
        elif a["severidade"] == "ATENCAO":
            atencoes += 1

    diff_caixa_total = sum(c["diferenca"] or 0 for c in base["canais"] if c["diferenca"] is not None)

    if graves > 0 or diff_caixa_total < -0.005:
        veredicto_nivel = "CRITICO"
        veredicto_texto = f"{graves} anomalia(s) crítica(s) + diferença de caixa de {r2(diff_caixa_total)} MT — Investigação imediata necessária"
    elif alertas > 0:
        veredicto_nivel = "ALERTA"
        veredicto_texto = f"{alertas} alerta(s) — Verificação prioritária recomendada"
    elif atencoes > 0 or any(c["conferido"] is None and c["esperado"] > 0.005 for c in base["canais"]):
        veredicto_nivel = "ATENCAO"
        veredicto_texto = f"{atencoes} ponto(s) de atenção + conferências pendentes — Acompanhar"
    else:
        veredicto_nivel = "OK"
        veredicto_texto = "Sem anomalias nem diferenças relevantes — Período regular"

    # --- 15. KPIs RESUMO ---
    total_vendas = base["vendas"]["total"]
    total_custo = base["vendas"]["custo"]
    total_lucro = base["vendas"]["lucro"]
    margem = base["vendas"]["margem"]
    total_compras = base["compras"]["total"]
    rec_esperado = base["rec_esperado_total"]
    rec_conferido = base["rec_conferido_total"]
    a_receber = base["a_receber"]["valor"]
    a_receber_atraso = base["a_receber"]["atraso"]
    n_faturas_atraso = base["a_receber"]["n_atraso"]
    estoque_fecha = base["estoque"]["fecha"]
    n_anomalias = len(anomalias)

    # --- RETORNO COMPLETO ---
    return {
        "periodo": {"ini": ini, "fim": fim, "ini_fmt": dmy(ini), "fim_fmt": dmy(fim),
                    "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M")},
        "veredicto": {"nivel": veredicto_nivel, "texto": veredicto_texto,
                      "graves": graves, "alertas": alertas, "atencoes": atencoes},
        "kpis": {
            "vendas_total": r2(total_vendas), "custo_total": r2(total_custo),
            "lucro_total": r2(total_lucro), "margem": margem,
            "compras_total": r2(total_compras),
            "rec_esperado": r2(rec_esperado), "rec_conferido": r2(rec_conferido),
            "diff_caixa": r2(diff_caixa_total),
            "a_receber": r2(a_receber), "a_receber_atraso": r2(a_receber_atraso),
            "n_faturas_atraso": n_faturas_atraso,
            "estoque_fecha": estoque_fecha,
            "n_anomalias": n_anomalias,
        },
        # Secções
        "vendas": {
            "detalhadas": vendas_detalhadas,
            "por_forma": vendas_por_forma_lista,
            "abaixo_custo": vendas_abaixo_custo,
            "acima_tabela": vendas_acima_tabela,
            "sem_forma": vendas_sem_forma,
            "faturas_credito": faturas_credito,
            "recibos": recibos,
        },
        "compras": {
            "detalhadas": compras_detalhadas,
            "por_fornecedor": fornecedores_lista,
            "acima_ultimo_custo": compras_acima_ultimo_custo,
        },
        "recebimentos": {
            "canais": base["canais"],
            "envelhecimento": {k: r2(v) for k, v in envelhecimento.items()},
            "envelhecimento_n": envelhecimento_n,
            "devedores": devedores_lista,
            "recibos": recibos,
        },
        "estoque": {
            "equacao": {
                "inicial": r2(bal["tot"]["inicial"]),
                "compras": r2(bal["tot"]["compras"]),
                "custo_vendas": r2(bal["tot"]["custo_vendas"]),
                "perdas_aj": r2(bal["tot"]["perdas_aj"]),
                "final_calculado": r2(bal["tot"]["inicial"] + bal["tot"]["compras"] - bal["tot"]["custo_vendas"] - bal["tot"]["perdas_aj"]),
                "final_sistema": r2(bal["tot"]["final"]),
                "diferenca": r2(bal["tot"]["inicial"] + bal["tot"]["compras"] - bal["tot"]["custo_vendas"] - bal["tot"]["perdas_aj"] - bal["tot"]["final"]),
                "fecha": bal["tot"].get("identidade_ok", True),
            },
            "diferencas": estoque_diferencas,
            "negativos": estoque_negativos,
            "abaixo_minimo": estoque_abaixo_minimo,
            "acima_maximo": estoque_acima_maximo,
            "parado": estoque_parado[:50],
            "por_categoria": estoque_por_categoria,
        },
        "perdas": {
            "por_motivo": perdas_por_motivo_lista,
            "detalhadas": base["perdas"],
            "sem_justificativa": perdas_sem_justificativa,
            "por_usuario": perdas_por_usuario_lista,
            "total": r2(base["perdas_total"]),
        },
        "ajustes": {
            "positivos": ajustes_pos,
            "negativos": ajustes_neg,
            "sem_justificativa": ajustes_sem_justificativa,
            "total_pos": r2(base["ajustes_pos"]),
            "total_neg": r2(base["ajustes_neg"]),
        },
        "auditorias": {
            "contagens": contagens,
            "diffs_nao_justificadas": diffs_nao_justificadas,
            "recontagens": recontagens,
            "total_perdas": r2(base["cont_perdas"]),
            "total_sobras": r2(base["cont_sobras"]),
        },
        "anomalias": anomalias,
        "evolucao_mensal": evolucao_mensal,
        "usuarios": usuarios_lista,
        "anuladas": base["anuladas"],
        "margem_neg": base["margem_neg"],
    }

def _mt(v):
    s = f"{abs(float(v or 0)):,.2f}".replace(",", " ").replace(".", ",")
    return ("-" if float(v or 0) < 0 else "") + s
