/* pesquisa_produto.js — pesquisa de produtos para as vendas.
 *
 * Escreve-se parte do nome ou o código, escolhe-se (rato ou ↑ ↓ + Enter) e o produto
 * entra na lista da venda. Funciona 100% offline: o catálogo vem das opções do
 * <select> da primeira linha, por isso a lista suspensa de cada linha continua a servir.
 *
 *   PesquisaProduto.iniciar({ input:'#pesqInput', lista:'#pesqLista',
 *                             linha:'.venda-item-row', adicionar:'#adicionar-linha' })
 *
 * Leitores de código de barras funcionam: digitam o código e Enter.
 */
(function () {
  'use strict';

  function norm(t) {
    return String(t || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  }
  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var ROT = { Unidade: 'un', Caixa: 'cx', Embalagem: 'emb', kg: 'kg' };

  function iniciar(o) {
    var input = document.querySelector(o.input);
    var lista = document.querySelector(o.lista);
    var botaoAdd = document.querySelector(o.adicionar);
    if (!input || !lista || !botaoAdd) return;

    function linhas() { return Array.prototype.slice.call(document.querySelectorAll(o.linha)); }

    // Catálogo a partir do <select> da primeira linha
    var catalogo = [];
    var primeiro = document.querySelector(o.linha + ' .produto-select');
    if (primeiro) {
      Array.prototype.forEach.call(primeiro.options, function (op) {
        if (!op.value) return;
        var txt = op.textContent.replace(/\s+/g, ' ').trim();       // "COD · Nome [kg]"
        var partes = txt.split(' · ');
        var codigo = partes.length > 1 ? partes.shift() : '';
        var nome = partes.join(' · ').replace(/\s*\[kg\]\s*$/, '');
        catalogo.push({
          id: op.value, codigo: codigo, nome: nome,
          unidade: op.dataset.unidade || 'Unidade',
          preco: parseFloat(op.dataset.preco) || 0,
          chave: norm(codigo + ' ' + nome)
        });
      });
    }

    var resultados = [], ativo = -1;

    function procurar(termo) {
      var t = norm(termo).trim();
      if (!t) return [];
      var palavras = t.split(/\s+/);
      var achados = [];
      catalogo.forEach(function (p) {
        for (var i = 0; i < palavras.length; i++) if (p.chave.indexOf(palavras[i]) === -1) return;
        var cod = norm(p.codigo), nome = norm(p.nome);
        var rank = cod === t ? 0 : nome.indexOf(t) === 0 ? 1 : cod.indexOf(t) === 0 ? 2 : nome.indexOf(t) > -1 ? 3 : 4;
        achados.push({ p: p, rank: rank });
      });
      achados.sort(function (a, b) { return a.rank - b.rank || a.p.nome.localeCompare(b.p.nome, 'pt'); });
      return achados.slice(0, 8).map(function (a) { return a.p; });
    }

    function fechar() { lista.hidden = true; ativo = -1; input.setAttribute('aria-expanded', 'false'); }

    function desenhar() {
      if (!input.value.trim()) { fechar(); return; }
      if (!resultados.length) {
        lista.innerHTML = '<div class="pesq-vazio">Nenhum produto encontrado para “' + esc(input.value.trim()) + '”.</div>';
      } else {
        lista.innerHTML = resultados.map(function (p, i) {
          return '<div class="pesq-item' + (i === ativo ? ' ativo' : '') + '" role="option" data-i="' + i + '">' +
            '<span class="pesq-nome">' + esc(p.nome) + '</span>' +
            '<span class="pesq-meta">' + esc(p.codigo) + ' · ' + (ROT[p.unidade] || p.unidade) + '</span>' +
            '<span class="pesq-preco">' + p.preco.toFixed(2) + '</span></div>';
        }).join('');
      }
      lista.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    function disparar(el, tipo) { el.dispatchEvent(new Event(tipo, { bubbles: true })); }

    function destacar(row) {
      row.classList.add('linha-destacada');
      row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      setTimeout(function () { row.classList.remove('linha-destacada'); }, 1400);
    }

    function adicionar(p) {
      var existente = null;
      linhas().forEach(function (r) {
        var s = r.querySelector('.produto-select'), u = r.querySelector('.unidade-select');
        if (s.value === p.id && u.value === p.unidade) existente = r;
      });
      if (existente) {                                   // já está na lista: +1
        var q = existente.querySelector('.qtd-input');
        q.value = parseFloat((parseFloat(q.value) || 0) + 1).toString();
        disparar(q, 'input');
        destacar(existente);
        return;
      }
      var vazia = null;
      linhas().forEach(function (r) { if (!vazia && !r.querySelector('.produto-select').value) vazia = r; });
      if (!vazia) {
        botaoAdd.click();
        var todas = linhas();
        vazia = todas[todas.length - 1];
      }
      var sel = vazia.querySelector('.produto-select');
      sel.value = p.id;
      disparar(sel, 'change');                           // preenche unidade, preço e total
      var qtd = vazia.querySelector('.qtd-input');
      qtd.value = 1;
      disparar(qtd, 'input');
      destacar(vazia);
    }

    function escolher(i) {
      var p = resultados[i];
      if (!p) return;
      adicionar(p);
      input.value = '';
      resultados = [];
      fechar();
      input.focus();
    }

    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.addEventListener('input', function () {
      resultados = procurar(input.value);
      ativo = resultados.length ? 0 : -1;
      desenhar();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!resultados.length) return;
        e.preventDefault();
        ativo = (ativo + (e.key === 'ArrowDown' ? 1 : -1) + resultados.length) % resultados.length;
        desenhar();
        var el = lista.querySelector('.ativo'); if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') {
        e.preventDefault();                              // nunca submete a venda por engano
        if (resultados.length) escolher(ativo < 0 ? 0 : ativo);
      } else if (e.key === 'Escape') {
        input.value = ''; resultados = []; fechar();
      }
    });
    lista.addEventListener('mousedown', function (e) {   // mousedown: antes do blur do input
      var el = e.target.closest('.pesq-item');
      if (el) { e.preventDefault(); escolher(parseInt(el.dataset.i, 10)); }
    });
    input.addEventListener('blur', function () { setTimeout(fechar, 120); });
    input.addEventListener('focus', function () { if (input.value.trim()) { resultados = procurar(input.value); desenhar(); } });
  }

  window.PesquisaProduto = { iniciar: iniciar };
})();
