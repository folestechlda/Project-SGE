/* modal.js — Diálogos modais próprios (sem browser alert/confirm)
   API pública: AppModal.erro(msg, detalhes) / .aviso(msg) / .info(msg) /
                .sucesso(msg) / .confirmar(msg, opcoes) → Promise<boolean>
   Delegação automática: qualquer <form data-confirmar="texto"> passa a
   pedir confirmação antes de submeter.
*/
(function () {
  'use strict';

  var TIPOS = {
    erro:    { icone: '❌', cor: 'var(--perigo, #B00020)',  titulo: 'Erro' },
    aviso:   { icone: '⚠️', cor: 'var(--aviso, #C79100)',   titulo: 'Aviso' },
    info:    { icone: 'ℹ️', cor: 'var(--info, #1976D2)',    titulo: 'Informação' },
    sucesso: { icone: '✓',  cor: 'var(--sucesso, #2F5233)', titulo: 'Sucesso' }
  };

  var _root = null;
  var _fecharAtual = null;

  function _criarBase() {
    if (_root) return _root;
    _root = document.createElement('div');
    _root.id = 'app-modal-root';
    _root.className = 'modal-overlay';
    _root.style.display = 'none';
    _root.innerHTML =
      '<div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="modalTitle">' +
        '<div class="modal-head">' +
          '<span class="modal-icon" aria-hidden="true"></span>' +
          '<span class="modal-title" id="modalTitle"></span>' +
        '</div>' +
        '<div class="modal-body"></div>' +
        '<details class="modal-details" style="display:none">' +
          '<summary>Ver detalhes técnicos</summary>' +
          '<pre></pre>' +
        '</details>' +
        '<div class="modal-actions"></div>' +
      '</div>';
    document.body.appendChild(_root);
    return _root;
  }

  function _escape(txt) {
    var div = document.createElement('div');
    div.textContent = String(txt == null ? '' : txt);
    return div.innerHTML;
  }

  function _fechar() {
    if (!_root) return;
    _root.style.display = 'none';
    if (_fecharAtual) {
      var cb = _fecharAtual;
      _fecharAtual = null;
      try { cb(); } catch (e) { /* ignora */ }
    }
  }

  function _abrir(opcoes) {
    _criarBase();
    var tipo = TIPOS[opcoes.tipo] ? opcoes.tipo : 'info';
    var cfg  = TIPOS[tipo];

    var box    = _root.querySelector('.modal-box');
    var icone  = _root.querySelector('.modal-icon');
    var titulo = _root.querySelector('.modal-title');
    var body   = _root.querySelector('.modal-body');
    var det    = _root.querySelector('.modal-details');
    var pre    = det.querySelector('pre');
    var acoes  = _root.querySelector('.modal-actions');

    box.className = 'modal-box modal-' + tipo;
    box.style.borderTopColor = cfg.cor;
    icone.textContent = cfg.icone;
    icone.style.color = cfg.cor;
    titulo.textContent = opcoes.titulo || cfg.titulo;
    body.innerHTML = opcoes.html || ('<p>' + _escape(opcoes.mensagem || '') + '</p>');

    if (opcoes.detalhes) {
      pre.textContent = opcoes.detalhes;
      det.style.display = 'block';
      det.open = false;
    } else {
      det.style.display = 'none';
      pre.textContent = '';
    }

    acoes.innerHTML = '';

    var botoes = opcoes.botoes || [
      { label: 'OK', valor: 'ok', tipo: 'primary', autofocus: true }
    ];
    var resolvido = null;
    var resultado = new Promise(function (res) { resolvido = res; });

    botoes.forEach(function (b) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn ' + (b.tipo === 'danger' ? 'btn-danger'
                              : b.tipo === 'ghost'  ? 'btn-ghost'
                              : 'btn-primary');
      btn.textContent = b.label;
      if (b.autofocus) btn.setAttribute('data-autofocus', '1');
      btn.addEventListener('click', function () {
        _root.style.display = 'none';
        _fecharAtual = null;
        resolvido(b.valor);
      });
      acoes.appendChild(btn);
    });

    _fecharAtual = function () {
      var neg = 'cancel';
      for (var i = 0; i < botoes.length; i++) {
        if (botoes[i].tipo !== 'primary' && botoes[i].valor) {
          neg = botoes[i].valor;
          break;
        }
      }
      resolvido(neg);
    };

    _root.style.display = 'flex';
    var foco = acoes.querySelector('[data-autofocus]') || acoes.querySelector('button');
    if (foco) setTimeout(function () { foco.focus(); }, 10);

    return resultado;
  }

  /* ---------------- API pública ---------------- */
  window.AppModal = {
    abrir: _abrir,
    fechar: _fechar,
    escape: _escape,
    erro: function (msg, detalhes) {
      return _abrir({ tipo: 'erro', mensagem: msg, detalhes: detalhes });
    },
    aviso: function (msg, detalhes) {
      return _abrir({ tipo: 'aviso', mensagem: msg, detalhes: detalhes });
    },
    info: function (msg, detalhes) {
      return _abrir({ tipo: 'info', mensagem: msg, detalhes: detalhes });
    },
    sucesso: function (msg) {
      return _abrir({ tipo: 'sucesso', mensagem: msg });
    },
    confirmar: function (msg, opcoes) {
      opcoes = opcoes || {};
            return _abrir({
        tipo: opcoes.tipo || 'aviso',
        titulo: opcoes.titulo || 'Confirmação',
        mensagem: msg,
        botoes: [
          { label: opcoes.labelCancelar || 'Não', valor: 'cancel', tipo: 'ghost' },
          { label: opcoes.labelConfirmar || 'Sim', valor: 'ok',
            tipo: opcoes.tipo === 'erro' ? 'danger' : 'primary', autofocus: true }
        ]
      }).then(function (r) { return r === 'ok'; });
    }
  };

  /* ---------------- Teclado ---------------- */
  document.addEventListener('keydown', function (ev) {
    if (!_root || _root.style.display === 'none') return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      _fechar();
    } else if (ev.key === 'Enter') {
      var foco = _root.querySelector('[data-autofocus]');
      if (foco) { ev.preventDefault(); foco.click(); }
    }
  });

  /* ---------------- Delegação global de [data-confirmar] ---------------- */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || !form.getAttribute) return;
    var msg = form.getAttribute('data-confirmar');
    if (!msg) return;
    if (form.getAttribute('data-confirmado') === '1') {
      form.removeAttribute('data-confirmado');
      return;
    }
    ev.preventDefault();
    var tipo  = form.getAttribute('data-confirmar-tipo') || 'aviso';
    var label = form.getAttribute('data-confirmar-ok') || 'Confirmar';
    window.AppModal.confirmar(msg, { tipo: tipo, labelConfirmar: label })
      .then(function (ok) {
        if (ok) {
          form.setAttribute('data-confirmado', '1');
          form.submit();
        }
      });
  }, true);

  /* fecha ao clicar fora */
  document.addEventListener('click', function (ev) {
    if (!_root || _root.style.display === 'none') return;
    if (ev.target === _root) _fechar();
  });
})();