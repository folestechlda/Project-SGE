/* UI das vendas: botões −/+, botões de pagamento e talão. Só apresentação:
   não altera cálculos nem validação (que usam os mesmos campos). */
(function () {
  var form = document.getElementById('form-venda-multipla');
  if (!form) return;
  var lista = document.getElementById('vd-linhas');
  var sel = form.querySelector('.vd-pag-select');

  function ev(el, t) { el.dispatchEvent(new Event(t, { bubbles: true })); }

  // − / +
  form.addEventListener('click', function (e) {
    var b = e.target.closest('.vd-menos, .vd-mais'); if (!b) return;
    var inp = b.parentNode.querySelector('.qtd-input');
    var un = b.closest('.venda-item-row').querySelector('.unidade-select').value;
    var passo = un === 'kg' ? 0.5 : 1;
    var v = (parseFloat(inp.value) || 0) + (b.classList.contains('vd-mais') ? passo : -passo);
    var min = parseFloat(inp.min) || 0;
    if (v < min) v = min;
    inp.value = String(Math.round(v * 1000) / 1000);
    ev(inp, 'input'); ev(inp, 'change');
  });

  // Pagamento: botões escolhem a opção do select (que continua a ser o campo enviado)
  var chips = [].slice.call(form.querySelectorAll('.vd-chip'));
  form.classList.add('js-chips');
  function marcar() { chips.forEach(function (c) { c.setAttribute('aria-pressed', c.dataset.pag === sel.value ? 'true' : 'false'); }); }
  chips.forEach(function (c) { c.addEventListener('click', function () { sel.value = c.dataset.pag; ev(sel, 'change'); marcar(); }); });
  sel.addEventListener('change', marcar); marcar();
  form.addEventListener('invalid', function (e) { if (e.target === sel) { var c = chips[0]; if (c) c.focus(); } }, true);

  // Talão
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function talao() {
    var html = '';
    [].slice.call(form.querySelectorAll('.venda-item-row')).forEach(function (r) {
      var s = r.querySelector('.produto-select'); if (!s.value) return;
      var nome = s.options[s.selectedIndex].text.replace(/^\s*[^·]*·\s*/, '').replace(/\s+/g, ' ').trim();
      var q = r.querySelector('.qtd-input').value || '0';
      html += '<div class="vd-tl"><span>' + esc(q + ' × ' + nome) + '</span><span>' + esc(r.querySelector('.subtotal-display').textContent) + '</span></div>';
    });
    lista.innerHTML = html || '<p class="vd-vazio">Pesquise e adicione produtos.</p>';
  }
  function depois() { setTimeout(talao, 0); }
  form.addEventListener('input', depois); form.addEventListener('change', depois); form.addEventListener('click', depois);
  new MutationObserver(depois).observe(document.getElementById('itens-venda'), { childList: true });
  var d = form.querySelector('input[name="data_venda"]'), dt = document.getElementById('vd-data');
  if (d && dt) d.addEventListener('change', function () { var p = d.value.split('-'); if (p.length === 3) dt.textContent = p[2] + '/' + p[1] + '/' + p[0]; });
  if (d && dt) { var p0 = d.value.split('-'); if (p0.length === 3) dt.textContent = p0[2] + '/' + p0[1] + '/' + p0[0]; }
  talao();
})();
