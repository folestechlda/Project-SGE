# -*- coding: utf-8 -*-
"""Testes de regressão dos fluxos que se tinham partido.

Correr a partir da pasta do projecto:   python -m unittest discover -s tests -v
Usam uma base de dados temporária (SGE_DATA_DIR) — nunca tocam em dados/estoque.db.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="sge_test_")
os.environ["SGE_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A            # noqa: E402  (cria a BD temporária em _TMP)
import services            # noqa: E402
from database import get_conn  # noqa: E402

DB = os.path.join(_TMP, "estoque.db")


def q(sql, *a):
    c = sqlite3.connect(DB)
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def um(sql, *a):
    return q(sql, *a)[0][0]


CLIENTE = None
P = {}


def setUpModule():
    global CLIENTE
    CLIENTE = A.app.test_client()
    r = CLIENTE.post("/login", data={"login": "admin", "senha": "admin123"})
    assert r.status_code == 302, "login admin/admin123 falhou"
    conn = get_conn()
    cat = conn.execute("SELECT id_categoria FROM categorias LIMIT 1").fetchone()[0]
    agora = "2026-01-01T00:00:00"

    def prod(cod, un, qpu, venda, saldo):
        cur = conn.execute(
            "INSERT INTO produtos (codigo,nome,id_categoria,unidade,qtd_por_unidade,"
            "preco_custo,preco_venda,data_cadastro) VALUES (?,?,?,?,?,?,?,?)",
            (cod, "Prod " + cod, cat, un, qpu, venda / 2, venda, agora))
        conn.execute("INSERT OR REPLACE INTO estoque (id_produto,saldo_atual,ultima_atualizacao)"
                     " VALUES (?,?,?)", (cur.lastrowid, saldo, agora))
        return cur.lastrowid
    P["un"] = prod("T-UN", "Unidade", 1, 10.0, 2000)
    P["un2"] = prod("T-UN2", "Unidade", 1, 5.0, 3)
    P["cx"] = prod("T-CX", "Caixa", 12, 120.0, 48)       # 4 caixas
    P["kg"] = prod("T-KG", "kg", 1, 30.0, 50)
    conn.commit()
    conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.c = CLIENTE
        self.p_un, self.p_un2, self.p_cx, self.p_kg = P["un"], P["un2"], P["cx"], P["kg"]

    def saldo(self, pid):
        return um("SELECT saldo_atual FROM estoque WHERE id_produto=?", pid)

    def flashes(self, r):
        import html, re
        return [html.unescape(x) for x in re.findall(
            r'class="flash flash-\w+">(.*?)</div>', r.get_data(as_text=True), re.S)]

    def venda(self, itens, **extra):
        data = {"cliente": "Cliente T", "id_produto[]": [str(i[0]) for i in itens],
                "quantidade[]": [str(i[1]) for i in itens],
                "unidade_original[]": [i[2] for i in itens],
                "preco_unitario[]": [str(i[3]) for i in itens]}
        data.update(extra)
        return self.c.post("/vendas/credito", data=data, follow_redirects=True)


class TestRotasEHandler(Base):
    def test_404_nao_vira_500(self):
        self.assertEqual(self.c.get("/rota-que-nao-existe").status_code, 404)
        self.assertEqual(self.c.get("/produtos/99999/editar").status_code in (302, 404), True)

    def test_favicon_e_logo_ausentes_nao_dao_500(self):
        self.assertIn(self.c.get("/favicon.ico").status_code, (200, 204))
        self.assertEqual(self.c.get("/static/img/nao_existe.png").status_code, 404)

    def test_next_externo_e_ignorado(self):
        c = A.app.test_client()
        r = c.post("/login?next=https://evil.example", data={"login": "admin", "senha": "admin123"})
        self.assertNotIn("evil.example", r.headers["Location"])
        c2 = A.app.test_client()
        r = c2.post("/login?next=//evil.example", data={"login": "admin", "senha": "admin123"})
        self.assertNotIn("evil.example", r.headers["Location"])


class TestVendaCredito(Base):
    def test_venda_um_item_guarda_nif_e_iva_padrao(self):
        antes = self.saldo(self.p_un)
        r = self.venda([(self.p_un, 1, "Unidade", 10)], cliente_nif="123456789", taxa_iva="")
        self.assertTrue(any("atura" in f for f in self.flashes(r)), self.flashes(r))
        nif, iva = q("SELECT nif_cliente, taxa_iva FROM contas_receber ORDER BY id_conta DESC LIMIT 1")[0]
        self.assertEqual(nif, "123456789")
        self.assertEqual(iva, 16.0, "IVA vazio deve usar a taxa padrão, não 0%")
        self.assertEqual(self.saldo(self.p_un), antes - 1)

    def test_venda_e_atomica(self):
        antes = (self.saldo(self.p_un), um("SELECT COUNT(*) FROM movimentos"),
                 um("SELECT COUNT(*) FROM contas_receber"),
                 um("SELECT valor FROM configuracoes WHERE chave='proximo_num_fatura'"))
        r = self.venda([(self.p_un, 1, "Unidade", 10), (self.p_un2, 999, "Unidade", 5)])
        self.assertTrue(any("insuficiente" in f.lower() for f in self.flashes(r)), self.flashes(r))
        depois = (self.saldo(self.p_un), um("SELECT COUNT(*) FROM movimentos"),
                  um("SELECT COUNT(*) FROM contas_receber"),
                  um("SELECT valor FROM configuracoes WHERE chave='proximo_num_fatura'"))
        self.assertEqual(antes, depois, "falha no 2.º item não pode deixar rasto do 1.º")

    def test_numeracao_sem_buracos_e_auditoria_gravada(self):
        n_aud = um("SELECT COUNT(*) FROM auditoria WHERE tabela='contas_receber'")
        self.venda([(self.p_un, 1, "Unidade", 10)])
        self.venda([(self.p_un, 1, "Unidade", 10)])
        nums = [r[0] for r in q("SELECT numero_fatura FROM contas_receber ORDER BY id_conta DESC LIMIT 2")]
        a, b = (int(n.split("/")[1]) for n in nums)
        self.assertEqual(a, b + 1)
        self.assertEqual(um("SELECT COUNT(*) FROM auditoria WHERE tabela='contas_receber'"), n_aud + 2)


class TestCotacoes(Base):
    def _cotacao(self, itens):
        conn = get_conn()
        idc = services.criar_cotacao(conn, 1, "Cliente Cot", itens=itens)
        conn.close()
        return idc

    def test_faturar_cotacao_com_caixa_e_kg(self):
        s_cx, s_kg = self.saldo(self.p_cx), self.saldo(self.p_kg)
        idc = self._cotacao([
            {"id_produto": self.p_cx, "quantidade": 1, "preco_unitario": 120},
            {"id_produto": self.p_kg, "quantidade": 2.5, "preco_unitario": 30, "desconto_percentual": 10}])
        r = self.c.post(f"/cotacoes/{idc}/faturar", data={}, follow_redirects=True)
        self.assertFalse(any("só pode ser movimentado" in f for f in self.flashes(r)), self.flashes(r))
        self.assertEqual(self.saldo(self.p_cx), s_cx - 12, "1 caixa = 12 unidades base")
        self.assertAlmostEqual(self.saldo(self.p_kg), s_kg - 2.5)
        self.assertEqual(um("SELECT status FROM cotacoes WHERE id_cotacao=?", idc), "Convertida")
        total = um("SELECT valor_total FROM contas_receber ORDER BY id_conta DESC LIMIT 1")
        self.assertAlmostEqual(total, 120 + 2.5 * 30 * 0.9, places=2)

    def test_faturar_cotacao_falhada_nao_deixa_rasto(self):
        antes = (self.saldo(self.p_un), um("SELECT COUNT(*) FROM movimentos"))
        idc = self._cotacao([
            {"id_produto": self.p_un, "quantidade": 1, "preco_unitario": 10},
            {"id_produto": self.p_un2, "quantidade": 999, "preco_unitario": 5}])
        self.c.post(f"/cotacoes/{idc}/faturar", data={}, follow_redirects=True)
        self.assertEqual(antes, (self.saldo(self.p_un), um("SELECT COUNT(*) FROM movimentos")))
        self.assertEqual(um("SELECT status FROM cotacoes WHERE id_cotacao=?", idc), "Aberta")

    def test_converter_cotacao_aprovada(self):
        s = self.saldo(self.p_un)
        idc = self._cotacao([{"id_produto": self.p_un, "quantidade": 2, "preco_unitario": 10}])
        conn = get_conn()
        conn.execute("UPDATE cotacoes SET status='Aprovada' WHERE id_cotacao=?", (idc,))
        conn.commit()
        services.converter_cotacao_em_venda(conn, idc, 1)
        conn.close()
        self.assertEqual(self.saldo(self.p_un), s - 2)
        self.assertEqual(um("SELECT status FROM cotacoes WHERE id_cotacao=?", idc), "Convertida")

    def test_numero_de_cotacao_nao_colide_apos_apagar(self):
        it = [{"id_produto": self.p_un, "quantidade": 1, "preco_unitario": 10}]
        a, b = self._cotacao(it), self._cotacao(it)
        conn = get_conn()
        conn.execute("DELETE FROM itens_cotacao WHERE id_cotacao=?", (a,))
        conn.execute("DELETE FROM cotacoes WHERE id_cotacao=?", (a,))
        conn.commit(); conn.close()
        c = self._cotacao(it)                     # antes: UNIQUE constraint failed
        self.assertNotEqual(um("SELECT numero FROM cotacoes WHERE id_cotacao=?", b),
                            um("SELECT numero FROM cotacoes WHERE id_cotacao=?", c))


class TestEdicaoCotacao(Base):
    def _nova(self, itens):
        conn = get_conn()
        idc = services.criar_cotacao(conn, 1, "Cliente Ed", cliente_email="a@b.c", observacoes="obs",
                                     itens=itens)
        conn.close()
        return idc

    def test_formulario_de_edicao_vem_preenchido_e_aponta_para_atualizar(self):
        idc = self._nova([{"id_produto": self.p_un, "quantidade": 2, "preco_unitario": 10},
                          {"id_produto": self.p_cx, "quantidade": 1, "preco_unitario": 120}])
        html = self.c.get(f"/cotacoes/{idc}/editar").get_data(as_text=True)
        self.assertIn(f"/cotacoes/{idc}/atualizar", html)
        self.assertIn("Editar Cotação", html)
        self.assertIn('value="Cliente Ed"', html)
        self.assertEqual(html.count('class="cotacao-item-row"'), 2)
        self.assertEqual(html.count(" selected>"), 2, "produtos dos itens devem vir selecionados")

    def test_nova_cotacao_continua_a_apontar_para_nova(self):
        html = self.c.get("/cotacoes/nova").get_data(as_text=True)
        self.assertIn('action="/cotacoes/nova"', html)
        self.assertEqual(html.count('class="cotacao-item-row"'), 1)

    def test_adicionar_remover_e_alterar_itens(self):
        idc = self._nova([{"id_produto": self.p_un, "quantidade": 2, "preco_unitario": 10},
                          {"id_produto": self.p_cx, "quantidade": 1, "preco_unitario": 120}])
        # remove o 1.º, mantém a caixa com outra quantidade e adiciona kg com desconto
        r = self.c.post(f"/cotacoes/{idc}/atualizar", follow_redirects=True, data={
            "cliente_nome": "Cliente Novo", "cliente_email": "", "data_validade": "2026-12-31",
            "id_produto[]": [str(self.p_cx), str(self.p_kg), ""],
            "quantidade[]": ["3", "2,5", ""], "preco_unitario[]": ["120", "30", ""],
            "desconto[]": ["0", "10", ""]})
        self.assertIn("atualizada com sucesso", " ".join(self.flashes(r)))
        itens = q("SELECT id_produto, quantidade, desconto_percentual FROM itens_cotacao WHERE id_cotacao=?", idc)
        self.assertEqual(sorted(i[0] for i in itens), sorted([self.p_cx, self.p_kg]))
        self.assertEqual(um("SELECT nome_cliente FROM cotacoes WHERE id_cotacao=?", idc), "Cliente Novo")
        self.assertAlmostEqual(um("SELECT valor_total FROM cotacoes WHERE id_cotacao=?", idc),
                               3 * 120 + 2.5 * 30 * 0.9, places=2)
        self.assertEqual(um("SELECT COUNT(*) FROM cotacoes WHERE nome_cliente='Cliente Novo'"), 1,
                         "editar não pode criar uma cotação nova")

    def test_edicao_sem_itens_e_recusada_e_nao_apaga_nada(self):
        idc = self._nova([{"id_produto": self.p_un, "quantidade": 2, "preco_unitario": 10}])
        r = self.c.post(f"/cotacoes/{idc}/atualizar", follow_redirects=True, data={
            "cliente_nome": "X", "id_produto[]": [""], "quantidade[]": [""], "preco_unitario[]": [""]})
        self.assertIn("pelo menos um item", " ".join(self.flashes(r)))
        self.assertEqual(um("SELECT COUNT(*) FROM itens_cotacao WHERE id_cotacao=?", idc), 1)

    def test_cotacao_convertida_nao_pode_ser_editada(self):
        idc = self._nova([{"id_produto": self.p_un, "quantidade": 1, "preco_unitario": 10}])
        conn = get_conn(); conn.execute("UPDATE cotacoes SET status='Convertida' WHERE id_cotacao=?", (idc,))
        conn.commit(); conn.close()
        r = self.c.post(f"/cotacoes/{idc}/atualizar", follow_redirects=True, data={
            "cliente_nome": "X", "id_produto[]": [str(self.p_un)], "quantidade[]": ["1"],
            "preco_unitario[]": ["10"]})
        self.assertNotIn("atualizada com sucesso", " ".join(self.flashes(r)))


class TestContasReceber(Base):
    def test_cancelar_fatura_repoe_stock(self):
        s = self.saldo(self.p_un)
        self.venda([(self.p_un, 3, "Unidade", 10)])
        idc = um("SELECT MAX(id_conta) FROM contas_receber")
        r = self.c.post(f"/contas-receber/{idc}/eliminar", data={}, follow_redirects=True)
        self.assertFalse(any("FOREIGN KEY" in f for f in self.flashes(r)), self.flashes(r))
        self.assertEqual(self.saldo(self.p_un), s)
        self.assertEqual(um("SELECT COUNT(*) FROM contas_receber WHERE id_conta=?", idc), 0)

    def test_pdf_do_recibo_e_gerado(self):
        self.venda([(self.p_un, 1, "Unidade", 10)])
        idc = um("SELECT MAX(id_conta) FROM contas_receber")
        self.c.post(f"/contas-receber/{idc}/liquidar", data={"valor": "2", "forma_pagamento": "M-Pesa"})
        idr = um("SELECT MAX(id_recibo) FROM recibos WHERE id_conta=?", idc)
        r = self.c.get(f"/recibos/{idr}.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_fatura_com_recibo_nao_pode_ser_eliminada(self):
        self.venda([(self.p_un, 1, "Unidade", 10)])
        idc = um("SELECT MAX(id_conta) FROM contas_receber")
        self.c.post(f"/contas-receber/{idc}/liquidar", data={"valor": "1", "forma_pagamento": "Dinheiro"})
        self.assertEqual(um("SELECT COUNT(*) FROM recibos WHERE id_conta=?", idc), 1)
        self.c.post(f"/contas-receber/{idc}/eliminar", data={}, follow_redirects=True)
        self.assertEqual(um("SELECT COUNT(*) FROM contas_receber WHERE id_conta=?", idc), 1)
        self.assertEqual(um("SELECT COUNT(*) FROM recibos WHERE id_conta=?", idc), 1)
        self.assertGreaterEqual(um("SELECT COUNT(*) FROM auditoria WHERE tabela='recibos'"), 1)


class TestZReset(Base):   # "Z": corre por último
    def _reset(self, nivel, senha="admin123"):
        return self.c.post("/configuracoes/formatar", follow_redirects=True,
                           data={"nivel": nivel, "confirmacao": "APAGAR", "ciencia": "1",
                                 "senha_admin": senha})

    def test_zz_reset_exige_senha_e_funciona_com_faturas(self):
        self.venda([(self.p_un, 1, "Unidade", 10)])
        antes = um("SELECT COUNT(*) FROM movimentos")
        self._reset("movimentos", senha="errada")
        self.assertEqual(um("SELECT COUNT(*) FROM movimentos"), antes, "senha errada não pode apagar")
        r = self._reset("movimentos")
        self.assertFalse(any("FOREIGN KEY" in f for f in self.flashes(r)), self.flashes(r))
        for t in ("movimentos", "contas_receber", "contas_receber_itens", "recibos", "cotacoes"):
            self.assertEqual(um(f"SELECT COUNT(*) FROM {t}"), 0, t)
        self.assertGreater(um("SELECT COUNT(*) FROM produtos"), 0, "produtos ficam")


if __name__ == "__main__":
    unittest.main(verbosity=2)
