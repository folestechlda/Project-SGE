# -*- coding: utf-8 -*-
"""Repara saídas a crédito "órfãs": movimentos com forma_pagamento='Credito' que
não pertencem a nenhuma fatura (ficaram assim quando uma venda a crédito ou a
facturação de uma cotação falhava a meio — o stock foi descontado, mas a fatura
nunca foi criada).

O que faz (só com --aplicar): faz backup, repõe o stock desses movimentos,
apaga-os e regista a operação na auditoria.
Por omissão (sem --aplicar) apenas LISTA o que faria.

Uso (a partir da pasta do projecto, com a aplicação FECHADA):
    python scripts/reparar_orfaos.py                 # só mostra
    python scripts/reparar_orfaos.py --aplicar       # corrige
    python scripts/reparar_orfaos.py --dados D:\\SGE\\dados --aplicar
"""
import argparse
import os
import sys
from datetime import datetime

ap = argparse.ArgumentParser()
ap.add_argument("--dados", help="pasta que contém estoque.db (por omissão: ./dados)")
ap.add_argument("--aplicar", action="store_true", help="aplica a correcção (senão só lista)")
args = ap.parse_args()
if args.dados:
    os.environ["SGE_DATA_DIR"] = args.dados

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database  # noqa: E402

ORFAOS_SQL = """
SELECT m.id_movimento, m.data_hora, p.codigo, p.nome, m.quantidade, m.valor_total,
       m.cliente, m.observacoes, m.id_produto
FROM movimentos m JOIN produtos p USING(id_produto)
WHERE m.tipo='Saida' AND m.forma_pagamento='Credito'
  AND m.id_movimento NOT IN (SELECT id_movimento FROM contas_receber_itens)
  AND m.id_movimento NOT IN (SELECT id_movimento FROM contas_receber WHERE id_movimento IS NOT NULL)
ORDER BY m.id_movimento
"""

conn = database.get_conn()
orfaos = conn.execute(ORFAOS_SQL).fetchall()
if not orfaos:
    print("Nenhum movimento órfão encontrado. Nada a fazer.")
    sys.exit(0)

print(f"Base de dados: {database.DB_PATH}")
print(f"{len(orfaos)} saída(s) a crédito sem fatura:\n")
total = 0.0
for o in orfaos:
    total += float(o["valor_total"] or 0)
    print(f"  #{o['id_movimento']:<5} {o['data_hora']}  {o['codigo']:<9} {o['nome'][:38]:<38} "
          f"qtd {o['quantidade']:<6g} {o['valor_total']:>10,.2f}  {o['cliente'] or ''}  [{o['observacoes'] or ''}]")
print(f"\nTotal: {total:,.2f}")

if not args.aplicar:
    print("\n(Modo de listagem. Para repor o stock e apagar estes movimentos use --aplicar.)")
    sys.exit(0)

backup = os.path.join(os.path.dirname(database.DB_PATH), "backups_reparacao")
os.makedirs(backup, exist_ok=True)
destino = os.path.join(backup, f"antes_reparar_orfaos_{datetime.now():%Y%m%d_%H%M%S}.db")
database.copiar_base_dados(destino)
print(f"\nBackup criado: {destino}")

try:
    agora = datetime.now().isoformat(timespec="seconds")
    for o in orfaos:
        conn.execute("UPDATE estoque SET saldo_atual = saldo_atual + ?, ultima_atualizacao=? "
                     "WHERE id_produto=?", (o["quantidade"], agora, o["id_produto"]))
        conn.execute("DELETE FROM movimentos WHERE id_movimento=?", (o["id_movimento"],))
        database.registrar_auditoria(
            conn, 1, "movimentos", o["id_movimento"], "DELETE", dict(o),
            {"acao": "REPARACAO_SAIDA_ORFA_STOCK_REPOSTO"}, None)
    conn.commit()
    print(f"Concluído: {len(orfaos)} movimento(s) revertido(s) e stock reposto.")
except Exception as e:
    conn.rollback()
    print(f"ERRO — nada foi alterado: {e}")
    sys.exit(1)
finally:
    conn.close()
