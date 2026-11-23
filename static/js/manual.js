/* manual.js — pesquisa no manual, índice que acompanha a leitura e ampliação de imagens. */
(function () {
  'use strict';
  function norm(t) { return String(t || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, ''); }

  // ---------- pesquisa ----------
  var busca = document.getElementById('mnBusca');
  var secoes = Array.prototype.slice.call(document.querySelectorAll('.mn-secao'));
  var vazio = document.getElementById('mnVazio');
  var atalhos = document.getElementById('mnAtalhos');
  var indice = Array.prototype.slice.call(document.querySelectorAll('#mnToc a'));
  secoes.forEach(function (s) { s._txt = norm((s.getAttribute('data-busca') || '') + ' ' + s.textContent); });

  function filtrar() {
    var q = norm(busca.value).trim();
    var palavras = q ? q.split(/\s+/) : [];
    var visiveis = 0;
    secoes.forEach(function (s) {
      var ok = palavras.every(function (p) { return s._txt.indexOf(p) > -1; });
      s.hidden = !ok; if (ok) visiveis++;
      if (palavras.length) s.querySelectorAll('details').forEach(function (d) { d.open = ok && s.id === 'faq' && norm(d.textContent).indexOf(palavras[0]) > -1; });
    });
    indice.forEach(function (a) {
      var alvo = document.getElementById(a.getAttribute('href').slice(1));
      a.style.display = (alvo && alvo.hidden) ? 'none' : '';
    });
    if (atalhos) atalhos.hidden = !!q;
    if (vazio) vazio.hidden = visiveis > 0;
  }
  if (busca) { busca.addEventListener('input', filtrar); }

  // ---------- índice acompanha a leitura ----------
  if ('IntersectionObserver' in window && indice.length) {
    var mapa = {};
    indice.forEach(function (a) { mapa[a.getAttribute('href').slice(1)] = a; });
    var visivel = {};
    var obs = new IntersectionObserver(function (entradas) {
      entradas.forEach(function (e) { visivel[e.target.id] = e.isIntersecting ? e.boundingClientRect.top : null; });
      var melhor = null, topo = Infinity;
      Object.keys(visivel).forEach(function (id) {
        if (visivel[id] !== null && Math.abs(visivel[id]) < topo) { topo = Math.abs(visivel[id]); melhor = id; }
      });
      if (melhor) indice.forEach(function (a) { a.classList.toggle('ativo', a === mapa[melhor]); });
    }, { rootMargin: '0px 0px -60% 0px', threshold: [0, 1] });
    secoes.forEach(function (s) { obs.observe(s); });
  }

  // ---------- ampliar imagem ----------
  var box = document.getElementById('mnLightbox');
  if (box) {
    var big = box.querySelector('img');
    function abrir(src, alt) { big.src = src; big.alt = alt || ''; box.hidden = false; }
    function fechar() { box.hidden = true; big.removeAttribute('src'); }
    document.addEventListener('click', function (e) {
      var im = e.target.closest && e.target.closest('img[data-zoom]');
      if (im) { abrir(im.currentSrc || im.src, im.alt); return; }
      if (!box.hidden && (e.target === box || e.target.tagName === 'BUTTON')) fechar();
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !box.hidden) fechar(); });
  }

  // ---------- abrir pergunta da FAQ vinda de #âncora ----------
  if (location.hash) {
    var alvo = document.querySelector(location.hash);
    if (alvo) setTimeout(function () { alvo.scrollIntoView({ block: 'start' }); }, 50);
  }
})();
