# -*- coding: utf-8 -*-
"""comparador_estoque.py — lê ficheiros de stock (Excel/PDF) e cruza com a BD.

Não escreve na base de dados. Devolve listas de resultados para o template.
"""
import io
import re
import unicodedata
from difflib import SequenceMatcher

import openpyxl


def normalizar(texto):
    """Remove acentos, baixa para minúsculas, junta espaços."""
    if not texto:
        return ""
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def similaridade(a, b):
    return SequenceMatcher(None, normalizar(a), normalizar(b)).ratio()


def melhor_match(nome, produtos_bd, limiar=0.65):
    """Procura o produto da BD mais parecido com `nome`.
    Devolve (produto_dict|None, score_0_a_1)."""
    melhor = None
    melhor_score = 0.0
    for p in produtos_bd:
        s = similaridade(nome, p["nome"])
        if s > melhor_score:
            melhor_score = s
            melhor = p
        # também tenta por código
        if p.get("codigo"):
            s2 = similaridade(nome, p["codigo"])
            if s2 > melhor_score:
                melhor_score = s2
                melhor = p
    if melhor_score >= limiar:
        return melhor, melhor_score
    return None, melhor_score


def _tentar_float(valor):
    if valor is None:
        return None
    try:
        from services import parse_numero_pt
    except ImportError:          # uso isolado, fora da aplicação
        parse_numero_pt = None
    if parse_numero_pt is not None:
        return parse_numero_pt(valor, milhares=True)
    s = str(valor).strip().replace(" ", "")
    s = s.replace(".", "").replace(",", ".") if ("," in s and "." in s) else s.replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def ler_excel(ficheiro):
    wb = openpyxl.load_workbook(ficheiro, data_only=True)
    ws = wb.active
    linhas = []
    header = None
    idx_nome = idx_qtd = None

    for row_idx, row in enumerate(ws.iter_rows(values_only=True), 1):
        if row_idx == 1:
            header = [normalizar(c) for c in row]
            for i, h in enumerate(header):
                if any(k in h for k in ("produto", "nome", "descri", "artigo")):
                    idx_nome = i
                if any(k in h for k in ("quant", "qtd", "saldo", "stock", "estoque")):
                    idx_qtd = i
            if idx_nome is not None and idx_qtd is not None:
                continue  # linha de cabeçalho, saltar
            # Se não encontrou cabeçalho, assume colunas 0 e 1
            idx_nome = 0
            idx_qtd = 1
            continue
        nome = row[idx_nome] if idx_nome is not None and idx_nome < len(row) else None
        qtd = row[idx_qtd] if idx_qtd is not None and idx_qtd < len(row) else None
        qtd_num = _tentar_float(qtd)
        if nome and qtd_num is not None:
            linhas.append({"nome": str(nome).strip(), "quantidade": qtd_num})
    return linhas


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def ler_pdf(ficheiro):
    import pdfplumber
    linhas = []
    with pdfplumber.open(io.BytesIO(ficheiro.read() if hasattr(ficheiro, "read") else ficheiro)) as pdf:
        for pagina in pdf.pages:
            tabelas = pagina.extract_tables() or []
            for tabela in tabelas:
                if not tabela or len(tabela) < 2:
                    continue
                header = [normalizar(c or "") for c in tabela[0]]
                idx_nome = idx_qtd = None
                for i, h in enumerate(header):
                    if any(k in h for k in ("produto", "nome", "descri", "artigo")):
                        idx_nome = i
                    if any(k in h for k in ("quant", "qtd", "saldo", "stock", "estoque")):
                        idx_qtd = i
                if idx_nome is None or idx_qtd is None:
                    # tenta estrutura simples: primeira coluna texto, segunda número
                    idx_nome, idx_qtd = 0, 1
                for row in tabela[1:]:
                    nome = row[idx_nome] if idx_nome < len(row) else None
                    qtd = row[idx_qtd] if idx_qtd < len(row) else None
                    qtd_num = _tentar_float(qtd)
                    if nome and qtd_num is not None:
                        linhas.append({"nome": str(nome).strip(), "quantidade": qtd_num})
    return linhas


# ---------------------------------------------------------------------------
# Comparação
# ---------------------------------------------------------------------------
def comparar(linhas_ficheiro, produtos_bd, limiar=0.65):
    encontrados = []
    nao_encontrados = []
    for linha in linhas_ficheiro:
        prod, score = melhor_match(linha["nome"], produtos_bd, limiar=limiar)
        if prod:
            encontrados.append({
                "nome_ficheiro": linha["nome"],
                "quantidade_ficheiro": linha["quantidade"],
                "codigo": prod["codigo"],
                "nome_sistema": prod["nome"],
                "unidade": prod.get("unidade", "Unidade"),
                "saldo_sistema": float(prod.get("saldo", 0) or 0),
                "diferenca": round(linha["quantidade"] - float(prod.get("saldo", 0) or 0), 3),
                "similaridade": round(score * 100, 1),
            })
        else:
            nao_encontrados.append({
                "nome_ficheiro": linha["nome"],
                "quantidade_ficheiro": linha["quantidade"],
                "melhor_score": round(score * 100, 1),
            })
    return encontrados, nao_encontrados