/* Индикатор загрузки — бегущая полоса вверху страницы и подпись, что именно идёт.
   Показывается при любом переходе по ссылке, отправке формы и выгрузке Excel:
   загрузка каталога или цен на большом кабинете идёт минутами, и без индикатора
   человек жмёт кнопку второй раз или решает, что программа зависла.
   Кнопки отправленной формы гаснут — повторное нажатие не отправит второй раз. */
(function () {
  var bar, label, timer, started, showTimer, fileTimer;

  function build() {
    if (bar) return;
    bar = document.createElement("div");
    bar.id = "busy";
    bar.innerHTML = '<div class="busy-line"></div><div class="busy-label"></div>';
    document.body.appendChild(bar);
    label = bar.querySelector(".busy-label");
  }

  function tick(text) {
    var s = Math.round((Date.now() - started) / 1000);
    label.textContent = text + (s >= 2 ? " · " + s + " с" : "");
  }

  function show(text, delay) {
    build();
    clearTimeout(showTimer);
    showTimer = setTimeout(function () {
      started = Date.now();
      bar.classList.add("on");
      tick(text);
      clearInterval(timer);
      timer = setInterval(function () { tick(text); }, 1000);
    }, delay || 0);
  }

  function hide() {
    clearTimeout(showTimer);
    clearInterval(timer);
    clearInterval(fileTimer);
    if (bar) bar.classList.remove("on");
    document.querySelectorAll("button[data-busy-off]").forEach(function (b) {
      b.disabled = false; b.removeAttribute("data-busy-off");
    });
  }

  function what(el, fallback) {
    var t = (el && (el.getAttribute("data-busy") || el.textContent || el.value || "")).replace(/\s+/g, " ").trim();
    return t ? "«" + t.replace(/[«»…]/g, "") + "» — выполняется" : fallback;
  }

  // Выгрузка Excel страницу не покидает: ждём, пока сервер поставит cookie
  // «файл отдан» (excel.xlsx_response), и гасим индикатор.
  function waitFile() {
    document.cookie = "download_done=; path=/; max-age=0";
    clearInterval(fileTimer);
    fileTimer = setInterval(function () {
      if (document.cookie.indexOf("download_done=1") >= 0) {
        document.cookie = "download_done=; path=/; max-age=0";
        hide();
      }
    }, 400);
    setTimeout(hide, 180000);
  }

  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;            // отменили подтверждением
    var form = e.target, btn = e.submitter;
    show(what(btn, "Выполняется"), 150);
    setTimeout(function () {                   // после того, как форма ушла
      form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (b) {
        if (!b.disabled) { b.disabled = true; b.setAttribute("data-busy-off", "1"); }
      });
    }, 0);
  });

  // Фильтры, отправляющие форму сами (onchange="this.form.submit()"), события
  // submit не порождают — ловим сам вызов.
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    show("Обновляю страницу", 150);
    return nativeSubmit.apply(this, arguments);
  };

  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#" || a.target === "_blank" || /^(mailto|tel|javascript):/.test(href)) return;
    if (a.origin && a.origin !== location.origin) return;
    if (/export/.test(href)) {
      show("Готовлю файл Excel", 0);
      waitFile();
      return;
    }
    show("Открываю страницу", 250);
  });

  window.addEventListener("pageshow", hide);   // «назад» в браузере — страница из кэша
})();
