/* Индикатор загрузки — бегущая полоса вверху страницы и подпись, что именно идёт.
   Показывается при любом переходе по ссылке, отправке формы и выгрузке Excel:
   загрузка каталога или цен на большом кабинете идёт минутами, и без индикатора
   человек жмёт кнопку второй раз или решает, что программа зависла.
   Кнопки отправленной формы гаснут — повторное нажатие не отправит второй раз. */
(function () {
  var bar, label, text, meter, fill, timer, started, showTimer, fileTimer, pollTimer;

  function build() {
    if (bar) return;
    bar = document.createElement("div");
    bar.id = "busy";
    bar.innerHTML = '<div class="busy-line"></div><div class="busy-label"><div class="busy-text"></div>' +
      '<div class="busy-meter" hidden><div class="busy-fill"></div></div></div>';
    document.body.appendChild(bar);
    label = bar.querySelector(".busy-label");
    text = bar.querySelector(".busy-text");
    meter = bar.querySelector(".busy-meter");
    fill = bar.querySelector(".busy-fill");
  }

  function tick(t) {
    var s = Math.round((Date.now() - started) / 1000);
    text.textContent = t + (s >= 2 ? " · " + s + " с" : "");
  }

  // Неразрывные пробелы: на узком экране «1 240» и «~40 с» не рвутся переносом.
  function num(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0"); }

  function left(sec) {
    if (sec < 60) return "~" + Math.max(1, Math.round(sec)) + "\u00a0с";
    return "~" + Math.round(sec / 60) + "\u00a0мин";
  }

  // Ход массового действия от сервера (priceapp/progress.py): этап, «N из M», процент и
  // сколько осталось. Счёта нет (этап без счёта) — шкала бежит, видно этап и время.
  function render(s) {
    clearInterval(timer);                     // время теперь пишет сервер
    if (s.total) {
      var pct = Math.min(100, Math.round(100 * s.done / s.total));
      meter.hidden = false;
      fill.style.width = pct + "%";
      var eta = "";
      if (s.done > 0 && s.stage_seconds > 1 && s.done < s.total) {
        eta = " · осталось " + left((s.total - s.done) * s.stage_seconds / s.done);
      }
      text.textContent = s.stage + ": " + num(s.done) + " из " + num(s.total) + " (" + pct + "%)" + eta +
        (s.detail ? " · " + s.detail : "");
    } else {
      meter.hidden = true;
      var sec = Math.round((Date.now() - started) / 1000);
      text.textContent = s.stage + (s.detail ? " · " + s.detail : "") + (sec >= 2 ? " · " + sec + " с" : "");
    }
  }

  function poll(token) {
    clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      fetch("/progress/" + token, {credentials: "same-origin", cache: "no-store"})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s || !s.stage || !bar.classList.contains("on")) return;
          if (s.finished) {
            clearInterval(pollTimer);
            meter.hidden = false;
            fill.style.width = "100%";
            text.textContent = "Готово — обновляю страницу";
            setTimeout(hide, 20000);          // ответ — файл, страница не сменится
            return;
          }
          render(s);
        })
        .catch(function () {});
    }, 600);
  }

  function token() {
    var a = new Uint8Array(8);
    (window.crypto || window.msCrypto).getRandomValues(a);
    return Array.prototype.map.call(a, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }

  function withToken(url, t) {
    var u = new URL(url || location.href, location.href);
    u.searchParams.set("_progress", t);
    return u.pathname + u.search;
  }

  function show(t, delay) {
    build();
    clearTimeout(showTimer);
    meter.hidden = true;
    fill.style.width = "0";
    showTimer = setTimeout(function () {
      started = Date.now();
      bar.classList.add("on");
      tick(t);
      clearInterval(timer);
      timer = setInterval(function () { tick(t); }, 1000);
    }, delay || 0);
  }

  function hide() {
    clearTimeout(showTimer);
    clearInterval(timer);
    clearInterval(fileTimer);
    clearInterval(pollTimer);
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
    // POST — действие: номер операции в адрес, ход — с сервера (шкала «N из M»).
    var method = ((btn && btn.getAttribute("formmethod")) || form.getAttribute("method") || "get").toLowerCase();
    if (method === "post" && !/\/login$/.test(form.getAttribute("action") || "")) {
      try {
        var t = token();
        if (btn && btn.hasAttribute("formaction")) {
          btn.setAttribute("formaction", withToken(btn.getAttribute("formaction"), t));
        } else {
          form.setAttribute("action", withToken(form.getAttribute("action"), t));
        }
        poll(t);
      } catch (err) { /* без шкалы — как раньше, бегущая полоса */ }
    }
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
