/* Entrada: mostra o valor da compra (quantidade × preço). Só apresentação. */
(function () {
  var q = document.getElementById('quantidade-entrada'), p = document.getElementById('preco_unitario'), v = document.getElementById('en-valor');
  if (!q || !p || !v) return;
  function calc() { v.textContent = ((parseFloat(q.value) || 0) * (parseFloat(p.value) || 0)).toFixed(2); }
  q.addEventListener('input', calc); p.addEventListener('input', calc);
  // o preço é preenchido por script ao escolher o produto
  new MutationObserver(calc).observe(document.getElementById('produto-info'), { attributes: true });
  document.getElementById('id_produto').addEventListener('change', calc);
  setInterval(calc, 400);
})();
