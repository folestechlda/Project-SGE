/* dashboard.js — gráficos do Painel.
 *
 * Lê os dados do bloco <script type="application/json" id="dashboard-data">
 * no HTML. Isto evita pôr {{ }} dentro do JS (que o linter marca como erro)
 * e mantém o código JS cacheável pelo browser.
 */
(function () {
  "use strict"; 

  var dataEl = document.getElementById("dashboard-data");
  if (!dataEl) return;
 
  var data;
  try {
    data = JSON.parse(dataEl.dataset.payload);
  } catch (e) {
    // Se o JSON estiver corrompido, não rebenta a página — só não desenha.
    if (window.console) console.error("dashboard.js: JSON inválido", e);
    return;
  }

  var dias        = data.dias        || [];
  var compras     = data.compras     || [];
  var vendas      = data.vendas      || [];
  var lucroSerie  = data.lucroSerie  || [];
  var entradasQtd = data.entradasQtd || [];
  var saidasQtd   = data.saidasQtd   || [];
  var catLabels   = data.catLabels   || [];
  var catData     = data.catData     || [];
  var fatLabels   = data.fatLabels   || [];
  var fatValores  = data.fatValores  || [];
  var fatQtds     = data.fatQtds     || [];
  var tdLabels    = data.tdLabels    || [];
  var tdSaldos    = data.tdSaldos    || [];
  var urls        = data.urls        || {};

  // Aspecto comum a todos os gráficos
  if (window.Chart) {
    Chart.defaults.font.family = '"Inter", "Segoe UI", "Helvetica Neue", Arial, sans-serif';
    Chart.defaults.font.size = 12;
    Chart.defaults.color = "#5b6b82";
    Chart.defaults.borderColor = "#edf0f5";
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.legend.labels.padding = 14;
    Chart.defaults.elements.bar.borderSkipped = false;
    Chart.defaults.datasets.bar.maxBarThickness = 44;
  }

  var totalLucro = (lucroSerie || []).reduce(function (a, b) {
    return a + (b || 0);
  }, 0);

  function fmtPT(v) {
    return Number(v || 0).toLocaleString("pt-PT", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  // ---- 1. Donut Lucro / Prejuízo ----
  var elD = document.getElementById("chartLucroPrejuizo");
  if (elD) {
    var lp = Math.max(totalLucro, 0);
    var pj = totalLucro < 0 ? Math.abs(totalLucro) : 0;
    var sem = (lp + pj) === 0 ? 1 : 0;

    new Chart(elD, {
      type: "doughnut",
      data: {
        labels: sem ? ["Sem dados"] : ["Lucro", "Prejuízo"],
        datasets: [{
          data: sem ? [1] : [lp, pj],
          backgroundColor: sem ? ["#cfd8dc"] : ["#1f8a5b", "#c2410c"],
          borderWidth: 2,
          borderColor: "#fff"
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "55%",
        onClick: function (e, el) {
          if (!el.length || sem) return;
          window.location.href = totalLucro >= 0
            ? urls.relatoriosLucratividade
            : urls.relatoriosVendas;
        },
        plugins: { legend: { position: "bottom" } }
      }
    });

    var lblLP = document.getElementById("lblLucroPrejuizo");
    if (lblLP) {
      lblLP.textContent =
        (totalLucro >= 0 ? "Lucro: " : "Prejuízo: ") +
        fmtPT(Math.abs(totalLucro)) + " MZN";
    }
  }

  // ---- 2. Tendência de Lucro ----
  var elT = document.getElementById("chartTendLucro");
  if (elT) {
    new Chart(elT, {
      type: "bar",
      data: {
        labels: dias,
        datasets: [{
          label: "Lucro (MZN)",
          data: lucroSerie,
          backgroundColor: lucroSerie.map(function (v) {
            return v >= 0 ? "#1f8a5b" : "#c2410c";
          }),
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: function (e, el) {
          if (!el.length) return;
          var d = dias[el[0].index];
          window.location.href = urls.relatoriosLucratividade +
            "&data_ini=" + d + "&data_fim=" + d;
        },
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } }
      }
    });
  }

  // ---- 3. Receita x Despesa ----
  var elRD = document.getElementById("chartReceitaDespesa");
  if (elRD) {
    new Chart(elRD, {
      type: "bar",
      data: {
        labels: dias,
        datasets: [
          { label: "Vendas",  data: vendas,  backgroundColor: "#1e4e8c", borderRadius: 4 },
          { label: "Compras", data: compras, backgroundColor: "#e3a23b", borderRadius: 4 }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: function (e, el) {
          if (!el.length) return;
          var d = dias[el[0].index];
          var v = el[0].datasetIndex === 0;
          window.location.href = urls.movimentosList +
            "?tipo=" + (v ? "Saida" : "Entrada") +
            "&data_ini=" + d + "&data_fim=" + d;
        },
        plugins: { legend: { position: "bottom" } },
        scales: { y: { beginAtZero: true } }
      }
    });
  }

  // ---- 4. Top vendidos (doughnut) ----
  var elC = document.getElementById("chartCatVendas");
  if (elC) {
    var pal = ["#1e4e8c", "#3b78c2", "#7fb0ea", "#b9d3f3", "#e3a23b", "#f0c778", "#5b6b82", "#9aa8bd", "#1f8a5b", "#c2410c"];
    var tem = catData.length > 0;

    new Chart(elC, {
      type: "doughnut",
      data: {
        labels: tem ? catLabels : ["Sem dados"],
        datasets: [{
          data: tem ? catData : [1],
          backgroundColor: tem ? pal : ["#cfd8dc"],
          borderWidth: 2,
          borderColor: "#fff"
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "55%",
        onClick: function (e, el) {
          if (!el.length || !tem) return;
          window.location.href = urls.produtosList +
            "?nome=" + encodeURIComponent(catLabels[el[0].index]);
        },
        plugins: { legend: { position: "bottom" } }
      }
    });
  }

  // ---- 5. Fluxo de quantidades ----
  var elF = document.getElementById("chartFluxoQtd");
  if (elF) {
    new Chart(elF, {
      type: "line",
      data: {
        labels: dias,
        datasets: [
          {
            label: "Entradas",
            data: entradasQtd,
            borderColor: "#1e4e8c",
            backgroundColor: "rgba(47,82,51,0.10)",
            tension: 0.3,
            fill: true,
            pointRadius: 3
          },
          {
            label: "Saídas",
            data: saidasQtd,
            borderColor: "#e3a23b",
            backgroundColor: "rgba(193,68,14,0.10)",
            tension: 0.3,
            fill: true,
            pointRadius: 3
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "bottom" } },
        scales: { y: { beginAtZero: true } }
      }
    });
  }

  // ---- 6. Faturas a Crédito por Estado ----
  var elFE = document.getElementById("chartFaturasEstado");
  if (elFE) {
    var coresPorLabel = {
      "Em aberto":          "#c2410c",
      "Parcialmente pagas": "#e3a23b",
      "Pagas":              "#1f8a5b"
    };
    var coresFat = fatLabels.map(function (l) {
      return coresPorLabel[l] || "#999";
    });
    var temFat = fatLabels.length > 0;

    new Chart(elFE, {
      type: "doughnut",
      data: {
        labels: temFat ? fatLabels : ["Sem dados"],
        datasets: [{
          data: temFat ? fatValores : [1],
          backgroundColor: temFat ? coresFat : ["#cfd8dc"],
          borderWidth: 2,
          borderColor: "#fff"
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "55%",
        onClick: function (e, el) {
          if (!el.length || !temFat) return;
          var statusMap = ["Aberta", "Parcial", "Liquidada"];
          window.location.href = urls.contasReceberList +
            "?status=" + statusMap[el[0].index];
        },
        plugins: {
          legend: { position: "bottom" },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var i = ctx.dataIndex;
                var v = fatValores[i] || 0;
                var q = fatQtds[i] || 0;
                return ctx.label + ": " + fmtPT(v) + " MZN (" +
                  q + " fatura" + (q !== 1 ? "s" : "") + ")";
              }
            }
          }
        }
      }
    });

    var totalFat = fatValores.reduce(function (a, b) { return a + (b || 0); }, 0);
    var totalQtd = fatQtds.reduce(function (a, b) { return a + (b || 0); }, 0);
    var lblFE = document.getElementById("lblFaturasEstado");
    if (lblFE) {
      lblFE.textContent =
        "Total: " + fmtPT(totalFat) + " MZN · " + totalQtd + " fatura(s)";
    }
  }

  // ---- 7. Top 10 clientes em dívida ----
  var elTD = document.getElementById("chartTopDivida");
  if (elTD) {
    var temTD = tdSaldos.length > 0;

    new Chart(elTD, {
      type: "bar",
      data: {
        labels: temTD ? tdLabels : ["Sem dívidas"],
        datasets: [{
          label: "Saldo em dívida (MZN)",
          data: temTD ? tdSaldos : [0],
          backgroundColor: "#c2410c",
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: "y",
        onClick: function (e, el) {
          if (!el.length || !temTD) return;
          var nome = tdLabels[el[0].index];
          window.location.href = urls.extratoClientes +
            "?cliente=" + encodeURIComponent(nome);
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                return "Dívida: " + fmtPT(ctx.parsed.x) + " MZN";
              }
            }
          }
        },
        scales: {
          x: {
            beginAtZero: true,
            ticks: { callback: function (v) { return fmtPT(v); } }
          }
        }
      }
    });
  }
})();