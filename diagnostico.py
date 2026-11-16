# -*- coding: utf-8 -*-
"""diagnostico.py — verificações automáticas do SGE.

Corre no arranque (chamado do app.py) e sempre que o utilizador abre
/diagnostico. Nunca bloqueia a app: qualquer falha é registada em log
e devolvida como aviso, não como erro fatal.

Verificações:
  1. Integridade da BD (PRAGMA integrity_check)
  2. FKs órfãos (movimentos/produtos/estoque sem referência válida)
  3. Stock negativo
  4. Produtos com margem negativa no cadastro (preco_venda <= preco_custo)
  5. Vendas recentes com lucro negativo
  6. Vendas com preço unitário muito abaixo do preço de catálogo
  7. Produtos com embalagem suspeita (qtd_por_unidade > 1 e preço base
     quase igual ao preço de catálogo — sinal de cadastro mal interpretado)
  8. Faturas a crédito em atraso
"""

import json
import logging
import os
import sqlite3
import traceback
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _base_dir():
    try:
        from database import get_base_dir
        return get_base_dir()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def _db_path():
    try:
        from database import DB_PATH
        return DB_PATH
    except Exception:
        return os.path.join(_base_dir(), "dados", "estoque.db")


def _now():
    return datetime.now()


def _log(msg):
    try:
        logging.warning(f"[diagnostico] {msg}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. Integridade da BD
# ---------------------------------------------------------------------------
def _verificar_integridade(conn):
    resultado = {"ok": True, "erros": []}
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        status = (row[0] if row else "").lower()
        if status != "ok":
            resultado["ok"] = False
            resultado["erros"].append(f"PRAGMA integrity_check: {status}")
    except Exception as e:
        resultado["ok"] = False
        resultado["erros"].append(f"Falha ao executar integrity_check: {e}")

    # FKs órfãos (não há ON DELETE CASCADE em todas as tabelas)
    try:
        orfaos = conn.execute("""
            SELECT COUNT(*) FROM movimentos m
            LEFT JOIN produtos p ON p.id_produto = m.id_produto
            WHERE p.id_produto IS NULL
        """).fetchone()[0]
        if orfaos:
            resultado["ok"] = False
            resultado["erros"].append(
                f"{orfaos} movimento(s) sem produto correspondente."
            )
    except Exception as e:
        resultado["erros"].append(f"Falha ao verificar movimentos órfãos: {e}")

    try:
        orfaos = conn.execute("""
            SELECT COUNT(*) FROM estoque e
            LEFT JOIN produtos p ON p.id_produto = e.id_produto
            WHERE p.id_produto IS NULL
        """).fetchone()[0]
        if orfaos:
            resultado["ok"] = False
            resultado["erros"].append(
                f"{orfaos} linha(s) de estoque sem produto correspondente."
            )
    except Exception as e:
        resultado["erros"].append(f"Falha ao verificar estoque órfão: {e}")

    return resultado


# ---------------------------------------------------------------------------
# 2. Stock negativo
# ---------------------------------------------------------------------------
def _verificar_stock_negativo(conn):
    try:
        rows = conn.execute("""
            SELECT p.codigo, p.nome, p.unidade,
                   ROUND(e.saldo_atual, 3) AS saldo
            FROM estoque e
            JOIN produtos p ON p.id_produto = e.id_produto
            WHERE e.saldo_atual < 0 AND p.ativo = 1
            ORDER BY e.saldo_atual ASC
        """).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log(f"Falha stock negativo: {e}")
        return []


# ---------------------------------------------------------------------------
# 3. Produtos com margem negativa no cadastro
# ---------------------------------------------------------------------------
def _verificar_margens_cadastro(conn):
    try:
        rows = conn.execute("""
            SELECT p.codigo, p.nome, p.unidade, p.qtd_por_unidade,
                   ROUND(p.preco_custo, 2) AS preco_custo,
                   ROUND(p.preco_venda, 2) AS preco_venda,
                   CASE
                     WHEN p.preco_venda > 0
                     THEN ROUND(((p.preco_venda - p.preco_custo)
                                 / p.preco_venda) * 100, 1)
                     ELSE NULL
                   END AS margem_pct
            FROM produtos p
            WHERE p.ativo = 1
              AND p.preco_custo > 0
              AND p.preco_venda <= p.preco_custo
            ORDER BY (p.preco_venda - p.preco_custo) ASC
        """).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log(f"Falha margens cadastro: {e}")
        return []


# ---------------------------------------------------------------------------
# 4. Vendas recentes com lucro negativo
# ---------------------------------------------------------------------------
def _verificar_vendas_lucro_negativo(conn, dias=30):
    desde = (_now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    try:
        rows = conn.execute("""
            SELECT p.codigo, p.nome, p.unidade,
                   COUNT(*) AS n_vendas,
                   ROUND(SUM(m.lucro), 2) AS lucro_total,
                   ROUND(MIN(m.lucro), 2) AS pior_lucro,
                   MAX(m.data_hora) AS ultima_venda
            FROM movimentos m
            JOIN produtos p ON p.id_produto = m.id_produto
            WHERE m.tipo = 'Saida'
              AND m.lucro < 0
              AND m.data_hora >= ?
            GROUP BY p.id_produto
            ORDER BY lucro_total ASC
            LIMIT 50
        """, (desde,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log(f"Falha vendas lucro negativo: {e}")
        return []


# ---------------------------------------------------------------------------
# 5. Vendas com preço unitário muito abaixo do catálogo
# ---------------------------------------------------------------------------
def _verificar_preco_venda_baixo(conn, dias=30, tolerancia_pct=20):
    """Movimentos onde o preço de venda praticado é X% inferior ao cadastrado.

    Isto apanha vendas fracionadas mal interpretadas, por exemplo:
    produto cadastrado com preco_venda=60, mas venderam a 10 MZN."""
    desde = (_now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    try:
        rows = conn.execute("""
            SELECT p.codigo, p.nome, p.unidade, p.qtd_por_unidade,
                   ROUND(p.preco_venda, 2) AS preco_catalogo,
                   ROUND(m.preco_unitario, 2) AS preco_vendido,
                   m.unidade_original,
                   m.data_hora,
                   p.id_produto
            FROM movimentos m
            JOIN produtos p ON p.id_produto = m.id_produto
            WHERE m.tipo = 'Saida'
              AND m.data_hora >= ?
              AND p.preco_venda > 0
              AND (p.preco_venda - m.preco_unitario) / p.preco_venda
                  > (? / 100.0)
            ORDER BY m.data_hora DESC
            LIMIT 100
        """, (desde, tolerancia_pct)).fetchall()

        # Agrupar por produto para não encher a lista
        agrupado = {}
        for r in rows:
            key = r["id_produto"]
            if key not in agrupado:
                agrupado[key] = {
                    "codigo": r["codigo"],
                    "nome": r["nome"],
                    "unidade": r["unidade"],
                    "qtd_por_unidade": r["qtd_por_unidade"],
                    "preco_catalogo": r["preco_catalogo"],
                    "preco_vendido_ultima": r["preco_vendido"],
                    "unidade_original_ultima": r["unidade_original"],
                    "ultima_venda": r["data_hora"],
                    "n_ocorrencias": 0,
                }
            agrupado[key]["n_ocorrencias"] += 1
        return list(agrupado.values())
    except Exception as e:
        _log(f"Falha preço venda baixo: {e}")
        return []


# ---------------------------------------------------------------------------
# 6. Produtos com embalagem suspeita
# ---------------------------------------------------------------------------
def _verificar_embalagem_suspeita(conn):
    """Produtos com qtd_por_unidade > 1 onde o preco_venda cadastrado é
    compatível com preço por embalagem, não por unidade base.

    Regra heurística: se qtd_por_unidade > 1 e o preço base (preco_venda)
    for MAIOR do que vendas reais por 'Unidade', o utilizador provavelmente
    pensou em preço por embalagem quando cadastrou.

    Exemplo: Dragão Limbo, 60 rolos/caixa, preco_venda=60 (por rolo, mas
    deveria ser 1.00 — o utilizador pensou que cadastrava 60 por caixa)."""
    try:
        rows = conn.execute("""
            SELECT p.id_produto, p.codigo, p.nome, p.unidade,
                   p.qtd_por_unidade,
                   ROUND(p.preco_custo, 2) AS preco_custo,
                   ROUND(p.preco_venda, 2) AS preco_venda,
                   (
                     SELECT ROUND(AVG(m.preco_unitario), 2)
                     FROM movimentos m
                     WHERE m.id_produto = p.id_produto
                       AND m.tipo = 'Saida'
                       AND m.unidade_original = 'Unidade'
                   ) AS media_venda_unidade,
                   (
                     SELECT COUNT(*)
                     FROM movimentos m
                     WHERE m.id_produto = p.id_produto
                       AND m.tipo = 'Saida'
                       AND m.unidade_original = 'Unidade'
                   ) AS n_vendas_unidade
            FROM produtos p
            WHERE p.ativo = 1
              AND p.qtd_por_unidade > 1
              AND p.unidade IN ('Caixa', 'Embalagem')
            ORDER BY p.nome
        """).fetchall()

        suspeitos = []
        for r in rows:
            d = dict(r)
            media = d.get("media_venda_unidade")
            pv = d.get("preco_venda") or 0
            qpu = d.get("qtd_por_unidade") or 1
            n_vendas = d.get("n_vendas_unidade") or 0

            if n_vendas == 0 or not media:
                continue

            # Se venderam 'Unidade' a preço MUITO menor do que o preço de
            # catálogo por unidade base, é sinal de cadastro errado.
            # Esperado: preco_venda seria o preço POR ROLO/UNIDADE.
            if media > 0 and pv > 0:
                ratio = media / pv
                if ratio < 0.5:  # venderam a menos de metade do esperado
                    d["media_venda_unidade"] = media
                    d["ratio"] = round(ratio, 2)
                    d["sugestao_preco_base"] = round(media, 2)
                    d["sugestao_preco_embalagem"] = round(media * qpu, 2)
                    suspeitos.append(d)
        return suspeitos
    except Exception as e:
        _log(f"Falha embalagem suspeita: {e}")
        return []


# ---------------------------------------------------------------------------
# 7. Faturas em atraso
# ---------------------------------------------------------------------------
def _verificar_faturas_atraso(conn):
    hoje = _now().strftime("%Y-%m-%d")
    try:
        rows = conn.execute("""
            SELECT id_conta, numero_fatura, nome_cliente,
                   ROUND(valor_total, 2) AS valor_total,
                   ROUND(saldo, 2) AS saldo,
                   data_vencimento,
                   CAST(julianday(?) - julianday(data_vencimento) AS INTEGER)
                     AS dias_atraso
            FROM contas_receber
            WHERE status IN ('Aberta', 'Parcial')
              AND data_vencimento IS NOT NULL
              AND data_vencimento < ?
            ORDER BY dias_atraso DESC
        """, (hoje, hoje)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log(f"Falha faturas em atraso: {e}")
        return []


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------
def executar_diagnostico_completo():
    """Devolve dict com todos os alertas. Nunca levanta excepção."""
    resultado = {
        "corrido_em": _now().isoformat(timespec="seconds"),
        "tem_alertas": False,
        "contagens": {},
        "integridade": {"ok": True, "erros": []},
        "stock_negativo": [],
        "margens_negativas": [],
        "vendas_lucro_negativo": [],
        "preco_venda_baixo": [],
        "embalagem_suspeita": [],
        "faturas_atraso": [],
        "erro_interno": None,
    }

    try:
        from database import get_conn
        conn = get_conn()

        resultado["integridade"] = _verificar_integridade(conn)
        resultado["stock_negativo"] = _verificar_stock_negativo(conn)
        resultado["margens_negativas"] = _verificar_margens_cadastro(conn)
        resultado["vendas_lucro_negativo"] = _verificar_vendas_lucro_negativo(conn)
        resultado["preco_venda_baixo"] = _verificar_preco_venda_baixo(conn)
        resultado["embalagem_suspeita"] = _verificar_embalagem_suspeita(conn)
        resultado["faturas_atraso"] = _verificar_faturas_atraso(conn)

        conn.close()

        resultado["contagens"] = {
            "stock_negativo": len(resultado["stock_negativo"]),
            "margens_negativas": len(resultado["margens_negativas"]),
            "vendas_lucro_negativo": len(resultado["vendas_lucro_negativo"]),
            "preco_venda_baixo": len(resultado["preco_venda_baixo"]),
            "embalagem_suspeita": len(resultado["embalagem_suspeita"]),
            "faturas_atraso": len(resultado["faturas_atraso"]),
            "integridade_erros": len(resultado["integridade"].get("erros", [])),
        }
        resultado["tem_alertas"] = any(
            v > 0 for v in resultado["contagens"].values()
        ) or not resultado["integridade"].get("ok", True)

    except Exception as e:
        resultado["erro_interno"] = f"{e}\n{traceback.format_exc()}"
        _log(f"Erro fatal no diagnóstico: {e}")
        resultado["tem_alertas"] = True

    return resultado


def guardar_relatorio(relatorio):
    """Grava o relatório em JSON (último) e em log (histórico)."""
    base = _base_dir()
    pasta = os.path.join(base, "dados")
    os.makedirs(pasta, exist_ok=True)

    # Último relatório (para o dashboard consultar)
    try:
        caminho_json = os.path.join(pasta, "diagnostico_ultimo.json")
        with open(caminho_json, "w", encoding="utf-8") as f:
            json.dump(relatorio, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        _log(f"Falha ao gravar JSON: {e}")

    # Histórico (log append-only)
    try:
        caminho_log = os.path.join(base, "diagnostico.log")
        with open(caminho_log, "a", encoding="utf-8") as f:
            f.write(
                f"\n=== {relatorio['corrido_em']} "
                f"{'[COM ALERTAS]' if relatorio['tem_alertas'] else '[OK]'} ===\n"
            )
            for k, v in relatorio.get("contagens", {}).items():
                f.write(f"  {k}: {v}\n")
            if relatorio.get("erro_interno"):
                f.write(f"  ERRO INTERNO:\n{relatorio['erro_interno']}\n")
    except Exception as e:
        _log(f"Falha ao gravar log: {e}")


def ler_ultimo_relatorio():
    """Lê o último relatório gravado (usado pelo /diagnostico)."""
    caminho = os.path.join(_base_dir(), "dados", "diagnostico_ultimo.json")
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None