# Read the original file
with open('C:/Users/User/Documents/SGE/relatorios_dados.py', 'r', encoding='utf-8') as f:
    content = f.read()

# The new function to insert (without compras_sem_nf)
new_function = '''

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
            preco_unit = float(m["preco_unitario"] or 0)
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
    perdas_valid, validade = perdas_validade(conn, ini, fim)
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

'''

# Insert after the original gestao function
marker = '''    return {
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


def _mt(v):
'''

# Replace the marker with marker + new function
content = content.replace(marker, marker + new_function)

# Write back
with open('C:/Users/User/Documents/SGE/relatorios_dados.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Function added successfully!")