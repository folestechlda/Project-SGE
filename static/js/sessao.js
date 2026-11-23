/* sessao.js: termina a sessão por inatividade e deixa só uma janela do SGE ativa de cada vez.

   - Inatividade: rato, teclado, rolagem e toque contam como atividade (em qualquer janela).
     1 minuto antes do fim aparece um aviso com contagem decrescente. Sem resposta, a sessão
     termina e todas as janelas voltam ao ecrã de entrada.
   - O servidor é quem decide: este script só avisa e mantém o servidor a par da atividade.
   - Janela única: se o SGE já estiver aberto noutro separador/janela, esta fica em pausa até
     o utilizador escolher "Usar esta janela". Assim não há dois formulários em conflito. */
(function () {
  'use strict';
  var D = document.body.dataset;
  if (!D.sessaoLimite) return;

  var LIMITE = parseInt(D.sessaoLimite, 10) * 1000;
  var AVISO = Math.min(60000, Math.max(10000, Math.floor(LIMITE / 4)));
  var JANELA_UNICA = D.janelaUnica === '1';
  var PING_MIN = 20000;          // no máximo um registo de atividade no servidor por 20 s
  var ESTADO_CADA = 30000;       // confirma no servidor se a sessão continua válida
  var DONO_CADA = 3000, DONO_VALIDADE = 8000;
  var K_ATIV = 'sge_ultima_atividade', K_DONO = 'sge_janela_ativa', K_SAIR = 'sge_sair';

  var docId = Math.random().toString(36).slice(2) + Date.now().toString(36);
  var ultimaLocal = Date.now(), ultimoPing = Date.now(), pingPendente = false;
  var encerrando = false, bloqueada = false, avisoAberto = false, relogioAviso = null;
  var canal = ('BroadcastChannel' in window) ? new BroadcastChannel('sge-sessao') : null;

  function ler(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function gravar(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* sem armazenamento */ } }
  function agora() { return Date.now(); }
  function ultimaGlobal() { return Math.max(ultimaLocal, parseInt(ler(K_ATIV) || '0', 10) || 0); }

  /* ---------------------------------------------------------------- atividade */
  function atividade() {
    var t = agora();
    if (t - ultimaLocal < 1000) return;
    ultimaLocal = t;
    pingPendente = true;
    gravar(K_ATIV, String(t));
    if (avisoAberto && window.AppModal) { fecharAviso(); }
  }
  ['mousemove', 'mousedown', 'keydown', 'scroll', 'wheel', 'touchstart', 'input'].forEach(function (ev) {
    window.addEventListener(ev, atividade, { passive: true, capture: true });
  });

  function ping() {
    pingPendente = false; ultimoPing = agora();
    return fetch(D.atividadeUrl, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
    }).then(function (r) { return r.status === 401 ? r.json().then(function (j) { throw j; }) : r.json(); })
      .catch(function (j) { if (j && j.estado === 'sessao_expirada') terminadaNoServidor(j); });
  }

  /* ------------------------------------------------------------------- saída */
  function irParaLogin() { encerrando = true; window.location.href = D.loginUrl; }

  function avisarOutras(motivo) {
    var msg = { t: 'sair', motivo: motivo, de: docId };
    try { if (canal) canal.postMessage(msg); } catch (e) { /* ignorar */ }
    gravar(K_SAIR, JSON.stringify({ ts: agora(), motivo: motivo, de: docId }));
  }

  function terminar(motivo) {
    if (encerrando) return;
    encerrando = true;
    avisarOutras(motivo);
    window.location.href = D.logoutUrl + '?motivo=' + (motivo === 'inatividade' ? 'inatividade' : 'manual');
  }

  function terminadaNoServidor(j) {
    if (encerrando) return;
    encerrando = true;
    var ir = function () { window.location.href = (j && j.redirect) || D.loginUrl; };
    if (window.AppModal) { window.AppModal.aviso((j && j.mensagem) || 'A sessão terminou.').then(ir, ir); } else { ir(); }
  }

  document.addEventListener('click', function (ev) {
    var a = ev.target.closest && ev.target.closest('a[href]');
    if (a && a.getAttribute('href').indexOf(D.logoutUrl) === 0) { avisarOutras('manual'); }
  }, true);

  if (canal) {
    canal.onmessage = function (ev) {
      if (ev.data && ev.data.t === 'sair' && ev.data.de !== docId) { irParaLogin(); }
    };
  }

  /* -------------------------------------------------------------------- aviso */
  function restanteMs() { return LIMITE - (agora() - ultimaGlobal()); }

  function fecharAviso() {
    avisoAberto = false;
    if (relogioAviso) { clearInterval(relogioAviso); relogioAviso = null; }
    if (window.AppModal && window.AppModal.fechar) { window.AppModal.fechar(); }
  }

  function mostrarAviso() {
    if (avisoAberto || bloqueada || !window.AppModal || !window.AppModal.abrir) return;
    avisoAberto = true;
    window.AppModal.abrir({
      tipo: 'aviso',
      titulo: 'A sua sessão vai terminar',
      html: '<p>Não houve atividade há algum tempo. Por segurança, a sessão termina em ' +
            '<strong id="sessao-contagem">' + Math.max(0, Math.ceil(restanteMs() / 1000)) + '</strong> segundos.</p>',
      // Um só botão: Esc, clique fora ou qualquer movimento fecham o aviso sem terminar a sessão
      // (o popup devolve o valor do botão "negativo" nesses casos).
      botoes: [
        { label: 'Continuar a trabalhar', valor: 'ok', tipo: 'primary', autofocus: true }
      ]
    }).then(function (valor) {
      var estavaAberto = avisoAberto;
      avisoAberto = false;
      if (relogioAviso) { clearInterval(relogioAviso); relogioAviso = null; }
      if (valor === 'ok' && estavaAberto) { ultimaLocal = 0; atividade(); ping(); }
    });
    relogioAviso = setInterval(function () {
      var el = document.getElementById('sessao-contagem');
      if (el) { el.textContent = String(Math.max(0, Math.ceil(restanteMs() / 1000))); }
    }, 1000);
  }

  function tick() {
    if (encerrando) return;
    var restante = restanteMs();
    if (restante <= 0) { terminar('inatividade'); return; }
    if (restante <= AVISO) { mostrarAviso(); }
    if (pingPendente && agora() - ultimoPing >= PING_MIN) { ping(); }
  }
  setInterval(tick, 2000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); consultarEstado(); } });

  function consultarEstado() {
    if (encerrando) return;
    fetch(D.estadoUrl, {
      credentials: 'same-origin', cache: 'no-store',
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
    }).then(function (r) { return r.status === 401 ? r.json().then(function (j) { throw j; }) : r.json(); })
      .catch(function (j) { if (j && j.estado === 'sessao_expirada') terminadaNoServidor(j); });
  }
  setInterval(consultarEstado, ESTADO_CADA);

  /* ------------------------------------------------------------ janela única */
  function lerDono() { try { return JSON.parse(ler(K_DONO) || 'null'); } catch (e) { return null; } }
  function donoVivo(d) { return !!d && (agora() - d.ts) < DONO_VALIDADE; }
  function reclamar() { gravar(K_DONO, JSON.stringify({ doc: docId, ts: agora() })); }
  function outraJanelaAtiva() { var d = lerDono(); return !!d && d.doc !== docId && donoVivo(d); }

  function desbloquear() {
    var ov = document.getElementById('sessao-bloqueio');
    if (ov) { ov.remove(); }
    Array.prototype.forEach.call(document.body.children, function (el) { el.removeAttribute('inert'); });
    bloqueada = false;
    reclamar();
    ultimaLocal = 0; atividade();
  }

  function bloquear(tomada) {
    if (bloqueada || encerrando) return;
    bloqueada = true;
    if (avisoAberto) { fecharAviso(); }
    var ov = document.createElement('div');
    ov.id = 'sessao-bloqueio';
    ov.setAttribute('role', 'alertdialog');
    ov.setAttribute('aria-modal', 'true');
    ov.innerHTML =
      '<div class="sb-caixa"><h3>' + (tomada ? 'O SGE passou para outra janela' : 'O SGE já está aberto noutra janela') + '</h3>' +
      '<p>' + (tomada
        ? 'Esta janela ficou em pausa para evitar registos em conflito.'
        : 'Para evitar registos em conflito, trabalhe numa janela de cada vez.') + '</p>' +
      '<div class="sb-acoes"><button type="button" class="btn btn-primary" data-a="usar">Usar esta janela</button>' +
      '<button type="button" class="btn btn-secondary" data-a="fechar">Fechar esta janela</button></div></div>';
    Array.prototype.forEach.call(document.body.children, function (el) { el.setAttribute('inert', ''); });
    document.body.appendChild(ov);
    ov.addEventListener('click', function (ev) {
      var b = ev.target.closest('button[data-a]');
      if (!b) return;
      if (b.getAttribute('data-a') === 'usar') { desbloquear(); }
      else {
        window.close();
        setTimeout(function () {
          var p = ov.querySelector('p');
          if (p) { p.textContent = 'Feche este separador manualmente (o navegador não permite fechá-lo daqui).'; }
        }, 300);
      }
    });
    var primeiro = ov.querySelector('button'); if (primeiro) { primeiro.focus(); }
  }

  if (JANELA_UNICA) {
    if (outraJanelaAtiva()) { bloquear(false); } else { reclamar(); }
    setInterval(function () {
      if (bloqueada) return;
      if (outraJanelaAtiva()) { bloquear(true); } else { reclamar(); }
    }, DONO_CADA);
    window.addEventListener('pagehide', function () {
      var d = lerDono();
      if (d && d.doc === docId) { gravar(K_DONO, JSON.stringify({ doc: docId, ts: 0 })); }
    });
  }

  window.addEventListener('storage', function (ev) {
    if (ev.key === K_SAIR && ev.newValue) {
      try { if (JSON.parse(ev.newValue).de !== docId) { irParaLogin(); } } catch (e) { irParaLogin(); }
    } else if (ev.key === K_DONO && JANELA_UNICA && !bloqueada && outraJanelaAtiva()) {
      bloquear(true);
    }
  });
})();
