/* venda_validacao.js: validação prévia de vendas, popup por produto e envio único.
   VendaValidacao.iniciar(form, { linha: '.venda-item-row', confirmar: 'texto', ok: 'Registar' })
   Fluxo: 1) pede ao servidor para validar (sem gravar); 2) se houver erros mostra um popup
   com o produto e o problema de cada linha, e o formulário fica como estava; 3) se estiver
   tudo certo (e o utilizador confirmar avisos) grava numa só transacção. */
(function () {
  'use strict';
  var CAMPO = { produto: '.produto-select', quantidade: '.qtd-input',
                unidade: '.unidade-select', preco: '.preco-input' };

  function esc(t) { return window.AppModal.escape(t); }

  function iniciar(form, opts) {
    if (!form) return;
    var SEL = opts.linha;
    var ocupado = false, navegando = false;
    var botao = form.querySelector('button[type="submit"]');

    function linhas() { return Array.prototype.slice.call(form.querySelectorAll(SEL)); }
    function limparLinha(r) {
      r.classList.remove('linha-erro');
      var m = r.querySelector('.linha-erro-msg'); if (m) m.remove();
    }
    form.addEventListener('input', function (e) { var r = e.target.closest(SEL); if (r) limparLinha(r); });
    form.addEventListener('change', function (e) { var r = e.target.closest(SEL); if (r) limparLinha(r); });

    function enviar(acao, extra) {
      var fd = new FormData(form);
      fd.set('acao', acao);
      Object.keys(extra || {}).forEach(function (k) { fd.set(k, extra[k]); });
      return fetch(form.getAttribute('action') || window.location.pathname, {
        method: 'POST', body: fd, credentials: 'same-origin',
        headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
      }).then(function (r) { return r.json(); }).catch(function () { return null; });
    }

    function etiqueta(e) {
      if (e.linha == null) return 'Venda';
      return 'Linha ' + e.linha + (e.produto ? ' · ' + e.produto : '');
    }

    function marcar(snapshot, erros) {
      var primeiro = null;
      erros.forEach(function (e) {
        var r = e.linha != null ? snapshot[e.linha - 1] : null;
        if (!r || !document.body.contains(r)) return;
        r.classList.add('linha-erro');
        var m = r.querySelector('.linha-erro-msg');
        if (!m) { m = document.createElement('div'); m.className = 'linha-erro-msg'; r.appendChild(m); }
        m.textContent = e.mensagem;
        if (!primeiro) primeiro = r.querySelector(CAMPO[e.campo] || '.qtd-input');
      });
      if (primeiro) { primeiro.scrollIntoView({ block: 'center' }); primeiro.focus(); }
    }

    function mostrarErros(erros, snapshot) {
      var itens = erros.map(function (e, i) {
        var acoes = (e.linha != null)
          ? '<br><a href="#" data-acao="ir" data-i="' + i + '">Corrigir</a>' +
            '<a href="#" data-acao="retirar" data-i="' + i + '">Retirar linha</a>' : '';
        return '<li><strong>' + esc(etiqueta(e)) + '</strong><br>' + esc(e.mensagem) + acoes + '</li>';
      }).join('');
      var html = '<p>A venda <strong>não foi registada</strong>. O formulário continua como estava: ' +
                 'corrija o que está indicado e registe de novo.</p><ul class="venda-problemas">' + itens + '</ul>';
      var p = window.AppModal.abrir({
        tipo: 'erro',
        titulo: erros.length === 1 ? 'A venda tem 1 problema' : 'A venda tem ' + erros.length + ' problemas',
        html: html,
        botoes: [{ label: 'Corrigir e tentar de novo', valor: 'ok', tipo: 'primary', autofocus: true }]
      });
      var corpo = document.querySelector('#app-modal-root .modal-body');
      if (corpo) corpo.addEventListener('click', function (ev) {
        var a = ev.target.closest('a[data-acao]'); if (!a) return;
        ev.preventDefault();
        var e = erros[parseInt(a.getAttribute('data-i'), 10)];
        var r = snapshot[e.linha - 1];
        window.AppModal.fechar();
        if (a.getAttribute('data-acao') === 'retirar' && r && linhas().length > 1) {
          r.remove();
          var q = form.querySelector('.qtd-input'); if (q) q.dispatchEvent(new Event('input', { bubbles: true }));
        } else if (r) {
          var campo = r.querySelector(CAMPO[e.campo] || '.qtd-input'); if (campo) { campo.scrollIntoView({ block: 'center' }); campo.focus(); }
        }
      });
      return p.then(function () { marcar(snapshot, erros); });
    }

    function confirmar(avisos) {
      var html;
      if (avisos && avisos.length) {
        html = '<p>Confirme antes de registar:</p><ul class="venda-problemas">' + avisos.map(function (a) {
          return '<li class="aviso"><strong>' + esc(etiqueta(a)) + '</strong><br>' + esc(a.mensagem) + '</li>';
        }).join('') + '</ul>';
      } else {
        html = '<p>' + esc(opts.confirmar) + '</p>';
      }
      return window.AppModal.abrir({
        tipo: 'aviso', titulo: 'Confirmação', html: html,
        botoes: [{ label: avisos && avisos.length ? 'Voltar e corrigir' : 'Não', valor: 'cancel', tipo: 'ghost' },
                 { label: avisos && avisos.length ? 'Registar mesmo assim' : (opts.ok || 'Registar'), valor: 'ok', tipo: 'primary', autofocus: true }]
      }).then(function (r) { return r === 'ok'; });
    }

    function falhaRede() {
      return window.AppModal.erro('Não foi possível comunicar com o servidor. A venda NÃO foi registada ' +
        'e os dados continuam no formulário. Verifique a sessão e tente de novo.');
    }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault(); ev.stopImmediatePropagation();
      if (ocupado || navegando) return;
      ocupado = true; if (botao) botao.disabled = true;
      var snapshot = linhas();
      linhas().forEach(limparLinha);
      var fim = function () { ocupado = false; if (botao && !navegando) botao.disabled = false; };

      enviar('validar').then(function (v) {
        if (!v) return falhaRede();
        if (v.erros && v.erros.length) return mostrarErros(v.erros, snapshot);
        return confirmar(v.avisos).then(function (ok) {
          if (!ok) return;
          return enviar('gravar', { confirmar_avisos: '1' }).then(function (g) {
            if (!g) return falhaRede();
            if (g.ok) { navegando = true; window.location.href = g.redirect; return; }
            if (g.estado === 'duplicado') {
              navegando = true;
              return window.AppModal.aviso(g.mensagem).then(function () { window.location.href = g.redirect; });
            }
            if (g.erros && g.erros.length) return mostrarErros(g.erros, snapshot);
            return window.AppModal.erro(g.mensagem || 'A venda não foi registada.');
          });
        });
      }).then(fim, fim);
    }, true);
  }
  window.VendaValidacao = { iniciar: iniciar };
})();
