(function () {
  var entrada = document.getElementById("arquivos");
  var zona = document.getElementById("zona");
  var lista = document.getElementById("selecionados");
  var formulario = document.getElementById("form-envio");
  var botao = document.getElementById("botao-enviar");
  if (!entrada || !zona) return;

  function mostrar() {
    lista.textContent = "";
    Array.prototype.forEach.call(entrada.files, function (arquivo) {
      var item = document.createElement("li");
      item.textContent = arquivo.name + " (" + Math.max(1, Math.round(arquivo.size / 1024)) + " KB)";
      lista.appendChild(item);
    });
  }

  entrada.addEventListener("change", mostrar);

  ["dragenter", "dragover"].forEach(function (nome) {
    zona.addEventListener(nome, function (evento) {
      evento.preventDefault();
      zona.classList.add("sobre");
    });
  });
  ["dragleave", "drop"].forEach(function (nome) {
    zona.addEventListener(nome, function (evento) {
      evento.preventDefault();
      zona.classList.remove("sobre");
    });
  });
  zona.addEventListener("drop", function (evento) {
    if (evento.dataTransfer && evento.dataTransfer.files.length) {
      entrada.files = evento.dataTransfer.files;
      mostrar();
    }
  });

  // OCR pode levar alguns segundos: evita envio duplo e avisa o usuario.
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      var enviar = form.querySelector("button[type=submit]");
      if (enviar) {
        enviar.disabled = true;
        enviar.textContent = "Processando…";
      }
    });
  });
})();
