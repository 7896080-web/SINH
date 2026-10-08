/* Отметки строк, которые переживают действие, смену фильтра и обновление страницы.
 *
 * Зачем. Основной путь оператора на «Ценах товаров» — «отметить → Установить →
 * проверить новую цену → Передать». Каждое действие кончается редиректом, и
 * отметки пропадали: те же строки приходилось отмечать второй раз, и вторая
 * отметка легко выходила не той (аудит 08.10).
 *
 * Как. Форма с атрибутом data-sel-key хранит отмеченные значения своих чекбоксов
 * (имя — data-sel-name) в sessionStorage по этому ключу. Ключ включает «где» и вид:
 * артикулы одной площадки не путаются с категориями или с другим кабинетом.
 * Перед отправкой отмеченные, которых сейчас НЕТ на экране (отмечены при другом
 * отборе), дописываются скрытыми полями — иначе форма отправила бы только видимые.
 *
 * Обратная сторона, известная по sync_admin: отбор, переживший смену фильтра,
 * обязан спрашивать про строки вне экрана — человек нашёл поиском один товар,
 * отметил его и не помнит про сорок отмеченных вчера. Поэтому `offscreen()`
 * отдаётся подтверждению, а счётчик называет оба числа.
 *
 * «Весь отбор» запоминается только для ТОГО ЖЕ отбора (адреса): после смены фильтра
 * под ним было бы другое множество строк, а галочка осталась бы стоять.
 */
(function () {
  function init(form) {
    var key = "sel:" + form.dataset.selKey;
    var name = form.dataset.selName;
    var allBox = form.querySelector("input[name=all_filtered]");
    var counter = document.querySelector(".sel-count[data-for='" + form.id + "']");
    var st = { ids: [], all: "" };
    try { st = JSON.parse(sessionStorage.getItem(key) || "null") || st; } catch (e) { /* без хранения — как раньше */ }
    var set = new Set(st.ids || []);

    function boxes() {
      return Array.prototype.filter.call(document.querySelectorAll("input[name='" + name + "']"),
        function (b) { return b.form === form && b.type === "checkbox"; });
    }
    function save() {
      try {
        sessionStorage.setItem(key, JSON.stringify({ ids: Array.from(set),
          all: allBox && allBox.checked ? location.search : "" }));
      } catch (e) { /* приватное окно: отметки живут до перезагрузки */ }
    }
    function onscreen() { return boxes().filter(function (b) { return b.checked; }).length; }
    function offscreen() {
      var vis = {};
      boxes().forEach(function (b) { vis[b.value] = 1; });
      var n = 0;
      set.forEach(function (v) { if (!vis[v]) n++; });
      return n;
    }
    function render() {
      if (!counter) return;
      var off = offscreen();
      if (allBox && allBox.checked) {
        counter.textContent = "Действие — по всему отбору: " + form.dataset.selTotal + " " + (form.dataset.selUnit || "шт") + ".";
      } else if (set.size) {
        counter.textContent = "Отмечено: " + set.size + (off ? " — из них на экране " + (set.size - off) +
          ", не видно при этом отборе " + off : "") + ".";
      } else {
        counter.textContent = "Ничего не отмечено.";
      }
      counter.classList.toggle("warn", off > 0 && !(allBox && allBox.checked));
    }

    boxes().forEach(function (b) { b.checked = set.has(b.value); });
    if (allBox) allBox.checked = !!st.all && st.all === location.search;

    document.addEventListener("change", function (e) {
      var t = e.target;
      if (t.form === form && t.name === name) {
        if (t.checked) set.add(t.value); else set.delete(t.value);
      } else if (t === allBox) {
        /* noop: сохранится ниже */
      } else if (t.classList && t.classList.contains("sel-all") && t.dataset.for === form.id) {
        boxes().forEach(function (b) { b.checked = t.checked; if (t.checked) set.add(b.value); else set.delete(b.value); });
      } else {
        return;
      }
      save();
      render();
    });
    document.querySelectorAll(".sel-clear[data-for='" + form.id + "']").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        set.clear();
        boxes().forEach(function (b) { b.checked = false; });
        if (allBox) allBox.checked = false;
        document.querySelectorAll(".sel-all[data-for='" + form.id + "']").forEach(function (h) { h.checked = false; });
        save();
        render();
      });
    });
    form.addEventListener("submit", function (e) {
      form.querySelectorAll("input[data-sel-extra]").forEach(function (x) { x.remove(); });
      if (!(allBox && allBox.checked)) {
        var vis = {};
        boxes().forEach(function (b) { vis[b.value] = 1; });
        set.forEach(function (v) {
          if (vis[v]) return;
          var h = document.createElement("input");
          h.type = "hidden"; h.name = name; h.value = v; h.setAttribute("data-sel-extra", "1");
          form.appendChild(h);
        });
      }
      // Кнопка, после которой отмечать больше нечего (отправка, отклонение), — снимает отметки.
      var btn = e.submitter;
      if (btn && btn.hasAttribute("data-sel-clear")) {
        try { sessionStorage.removeItem(key); } catch (err) { /* */ }
      }
    });

    form._sel = {
      count: function () { return set.size; },
      onscreen: onscreen,
      offscreen: offscreen,
      all: function () { return !!(allBox && allBox.checked); },
      total: function () { return parseInt(form.dataset.selTotal || "0", 10); }
    };
    render();
  }
  document.querySelectorAll("form[data-sel-key]").forEach(init);

  /* Подтверждение массового действия с числами. `what` — что делаем, `unit` —
   * чего («артикулов»). `always` — спрашивать даже без строк вне экрана. */
  window.selConfirm = function (formId, what, unit, always) {
    var s = document.getElementById(formId)._sel;
    if (s.all()) {
      return confirm(what + ": ВЕСЬ отбор — " + s.total() + " " + unit + ", включая строки вне экрана?");
    }
    var n = s.count(), off = s.offscreen();
    if (!n) { alert("Ничего не отмечено."); return false; }
    if (!off && !always) return true;
    return confirm(what + ": " + n + " " + unit + "?" + (off ? "\n\nИЗ НИХ " + off + " СЕЙЧАС НЕ ВИДНО — отмечены при другом " +
      "отборе. Если они не нужны, нажмите «Отмена» и «Снять отметки»." : ""));
  };
})();
