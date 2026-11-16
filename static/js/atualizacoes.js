(function () {
  "use strict";

  var input = document.getElementById("inputPacote");
  var btnValidar = document.getElementById("btnValidar");
  var btnAplicar = document.getElementById("btnAplicar");
  var btnCancelar = document.getElementById("btnCancelar");
  var painelAplicar = document.getElementById("painelAplicar");
  var info = document.getElementById("infoPacote");
  var wrap = document.getElementById("progressoWrap");
  var bar = document.getElementById("progressoBar");
  var txt = document.getElementById("progressoTexto");
  var det = document.getElementById("progressoDetalhe");

  var tempPath = null;
  var timerEstado = null;

  input.addEventListener("change", function () {
    btnValidar.disabled = !input.files.length;
    painelAplicar.style.display = "none";
    info.style.display = "none";
    tempPath = null;
  });

  // ---- Validar (upload + check server-side) ----
  btnValidar.addEventListener("click", function () {
    if (!input.files.length) return;
    var file = input.files[0];
    if (!file.name.toLowerCase().endsWith(".sgeu")) {
      alert("O ficheiro deve ter extensao .sgeu");
      return;
    }

    btnValidar.disabled = true;
    info.style.display = "block";
    info.innerHTML = "<p>A enviar...</p>";

    var xhr = new XMLHttpRequest();
    var fd = new FormData();
    fd.append("pacote", file);

    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var pct = Math.round((e.loaded / e.total) * 100);
        info.innerHTML = "<p>A enviar: " + pct + "%</p>";
      }
    };

    xhr.onload = function () {
      btnValidar.disabled = false;
      var r;
      try { r = JSON.parse(xhr.responseText); }
      catch (e) {
        info.innerHTML = "<p class='text-danger'>Resposta invalida do servidor.</p>";
        return;
      }
      if (!r.ok) {
        info.innerHTML = "<p class='text-danger'>&#10060; " + r.erro + "</p>";
        return;
      }
      tempPath = r.temp_path;
      var m = r.manifest;
      var linhas = [
        "<tr><td>Versao nova</td><td><strong>" + m.app_version + "</strong></td></tr>",
        "<tr><td>Versao minima</td><td>" + (m.min_app_version || "0.0.0") + "</td></tr>",
        "<tr><td>Data de lancamento</td><td>" + (m.release_date || "-") + "</td></tr>",
        "<tr><td>Ficheiros</td><td>" + m.files.length + "</td></tr>"
      ];
      if (m.changelog) {
        linhas.push("<tr><td>Changelog</td><td>" + m.changelog + "</td></tr>");
      }
      info.innerHTML =
        "<p class='text-success'>&#10003; Pacote valido</p>" +
        "<table class='mini-table'><tbody>" + linhas.join("") + "</tbody></table>";
      painelAplicar.style.display = "block";
    };

    xhr.onerror = function () {
      btnValidar.disabled = false;
      info.innerHTML = "<p class='text-danger'>Falha de rede.</p>";
    };

    xhr.open("POST", "/api/atualizacoes/validar");
    xhr.send(fd);
  });

  // ---- Aplicar ----
  btnAplicar.addEventListener("click", function () {
    if (!tempPath) return;
    btnAplicar.disabled = true;
    btnCancelar.disabled = true;
    wrap.style.display = "block";
    bar.style.width = "0%";
    txt.textContent = "0%";
    det.textContent = "A preparar...";

    fetch("/api/atualizacoes/aplicar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ temp_path: tempPath })
    })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (!r.ok) {
          alert("Erro: " + r.erro);
          btnAplicar.disabled = false;
          btnCancelar.disabled = false;
          return;
        }
        timerEstado = setInterval(verificarEstado, 400);
      })
      .catch(function (e) {
        det.innerHTML = "<span class='text-danger'>" + e + "</span>";
        btnAplicar.disabled = false;
        btnCancelar.disabled = false;
      });
  });

  function verificarEstado() {
    fetch("/api/atualizacoes/estado")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.state === "applying") {
          var pct = s.total ? Math.round((s.current / s.total) * 100) : 5;
          bar.style.width = pct + "%";
          txt.textContent = pct + "%";
          det.textContent = s.file || "";
        } else if (s.state === "done") {
          clearInterval(timerEstado);
          bar.style.width = "100%";
          txt.textContent = "100%";
          det.textContent = "A reiniciar...";
          setTimeout(reiniciar, 600);
        } else if (s.state === "error") {
          clearInterval(timerEstado);
          det.innerHTML = "<span class='text-danger'>" + s.error + "</span>";
          btnAplicar.disabled = false;
          btnCancelar.disabled = false;
        }
      })
      .catch(function () { /* server momentaneamente offline */ });
  }

    function reiniciar() {
    fetch("/api/atualizacoes/reiniciar", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (r.manual_restart) {
          bar.style.width = "100%";
          txt.textContent = "100%";
          det.innerHTML =
            "<div style='padding:16px;background:#FFF8E1;border-radius:6px'>" +
            "<strong>Actualização pronta.</strong><br><br>" +
            "1. Feche esta janela do SGE.<br>" +
            "2. Abra o SGE novamente.<br>" +
            "3. A nova versão é aplicada automaticamente no arranque." +
            "</div>";
          btnAplicar.style.display = "none";
          btnCancelar.style.display = "none";
        } else {
          esperarServidor();
        }
      });
  }

  function esperarServidor() {
    var tentativas = 0;
    var maxTentativas = 90;
    det.textContent = "A aguardar reinicio do servidor...";
    var timer = setInterval(function () {
      tentativas++;
      fetch("/login", { method: "GET", cache: "no-store" })
        .then(function (r) {
          if (r.ok) {
            clearInterval(timer);
            window.location.reload();
          }
        })
        .catch(function () { /* ainda offline */ });
      if (tentativas > maxTentativas) {
        clearInterval(timer);
        det.innerHTML = "<span class='text-danger'>Servidor nao respondeu em 90 segundos. Reinicie o SGE manualmente.</span>";
      }
    }, 1000);
  }

  btnCancelar.addEventListener("click", function () {
    if (timerEstado) clearInterval(timerEstado);
    painelAplicar.style.display = "none";
    info.style.display = "none";
    input.value = "";
    btnValidar.disabled = true;
    btnAplicar.disabled = false;
    btnCancelar.disabled = false;
    wrap.style.display = "none";
    tempPath = null;
  });
})();