/* Шкала прогресса на каждой кнопке и полоса фоновых заданий.
 *
 * Кнопка формы (POST) отправляет форму в фоне и показывает, как идёт дело:
 *  - файл: проценты отправки по байтам;
 *  - сервер: то, что о себе сообщает операция (`/progress/op/<номер>`, см.
 *    finapp/progress.py), а если она молчит — плавное «выполняется».
 * Ответ-переход сервер отдаёт JSON с адресом (заголовок X-Progress), и страница
 * переходит туда сама — сообщение об итоге при этом не теряется.
 * Ссылки-выгрузки (data-download) скачиваются с процентами по байтам.
 * Переходы по ссылкам и формы-фильтры (GET) — та же шкала до открытия страницы.
 *
 * Полоса над страницей (#jobs-panel) показывает фоновые дела: загрузки по API,
 * задания 1С, отчёты в 1С (`/progress/jobs`) — пока идут и итог, когда кончились.
 */
(function () {
    "use strict";
    // При замене страницы ответом (document.write) скрипт выполняется заново;
    // действует только последний — иначе форма ушла бы дважды.
    var generation = (window.__mpfinProgress = (window.__mpfinProgress || 0) + 1);
    function current() { return generation === window.__mpfinProgress; }

    // ------------------------------------------------------------ шкала
    var box, fill, title, detail, pct, closeBtn, creepTimer = null, level = 0;

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text) node.textContent = text;
        return node;
    }

    function ensureBox() {
        if (box && document.body.contains(box)) return;
        box = el("div", "progress-box");
        box.setAttribute("role", "status");
        box.setAttribute("aria-live", "polite");
        var head = el("div", "progress-box__head");
        title = el("div", "progress-box__title");
        closeBtn = el("button", "progress-box__close", "×");
        closeBtn.type = "button";
        closeBtn.setAttribute("aria-label", "Закрыть");
        closeBtn.addEventListener("click", hide);
        head.appendChild(title);
        head.appendChild(closeBtn);
        var track = el("div", "progress-track");
        fill = el("div", "progress-track__fill");
        track.appendChild(fill);
        var row = el("div", "progress-box__row");
        detail = el("span", "progress-box__detail");
        pct = el("span", "progress-box__pct");
        row.appendChild(detail);
        row.appendChild(pct);
        box.appendChild(head);
        box.appendChild(track);
        box.appendChild(row);
        document.body.appendChild(box);
    }

    function show(label) {
        ensureBox();
        stopCreep();
        box.classList.remove("progress-box--error", "progress-box--done");
        box.hidden = false;
        closeBtn.hidden = true;
        title.textContent = label;
        level = 0;
        set(0, "");
    }

    function set(fraction, text) {
        level = Math.max(0, Math.min(1, fraction));
        fill.style.width = (level * 100).toFixed(1) + "%";
        pct.textContent = Math.floor(level * 100) + "%";
        if (text !== undefined && text !== null) detail.textContent = text;
    }

    // Сколько займёт работа сервера, заранее неизвестно: шкала идёт к `to`,
    // замедляясь, и не упирается в него, пока не придёт ответ.
    function creep(to, seconds) {
        stopCreep();
        var tau = Math.max(0.5, seconds) * 1000 / 2;
        creepTimer = setInterval(function () {
            set(level + (to - level) * (1 - Math.exp(-200 / tau)));
        }, 200);
    }

    function stopCreep() {
        if (creepTimer) { clearInterval(creepTimer); creepTimer = null; }
    }

    function hide() {
        stopCreep();
        if (box) box.hidden = true;
    }

    function done(text) {
        stopCreep();
        set(1, text || "готово");
        box.classList.add("progress-box--done");
    }

    function fail(text) {
        ensureBox();
        stopCreep();
        box.hidden = false;
        box.classList.add("progress-box--error");
        closeBtn.hidden = false;
        detail.textContent = text;
        pct.textContent = "";
    }

    function mb(bytes) { return (bytes / 1048576).toFixed(1).replace(".", ","); }

    function randomId() {
        var s = "";
        var bytes = new Uint8Array(12);
        (window.crypto || window.msCrypto).getRandomValues(bytes);
        for (var i = 0; i < bytes.length; i++) s += ("0" + bytes[i].toString(16)).slice(-2);
        return s;
    }

    function labelOf(button, fallback) {
        var text = button ? (button.textContent || button.value || "").trim() : "";
        return text || fallback;
    }

    // ------------------------------------------------------------ формы

    function lock(form, on) {
        var buttons = form.querySelectorAll("button[type=submit], button:not([type]), input[type=submit]");
        for (var i = 0; i < buttons.length; i++) {
            if (on) {
                buttons[i].dataset.progressWasDisabled = buttons[i].disabled ? "1" : "";
                buttons[i].disabled = true;
            } else if (!buttons[i].dataset.progressWasDisabled) {
                buttons[i].disabled = false;
            }
        }
    }

    function replaceDocument(html) {
        document.open();
        document.write(html);
        document.close();
    }

    function sendForm(form, submitter, label) {
        var data = new FormData(form);
        if (submitter && submitter.name) data.append(submitter.name, submitter.value);
        var withFile = false;
        var inputs = form.querySelectorAll("input[type=file]");
        for (var i = 0; i < inputs.length; i++) {
            if (inputs[i].files && inputs[i].files.length) withFile = true;
        }
        var opId = randomId();
        // Доли шкалы: отправка файла — первая треть, работа сервера — остальное.
        var serverFrom = withFile ? 0.35 : 0.03;
        var serverSpan = 0.97 - serverFrom;
        var polling = null, reported = false;

        function poll() {
            fetch("/progress/op/" + opId, {headers: {"Accept": "application/json"}, cache: "no-store"})
                .then(function (r) {
                    var type = r.headers.get("content-type") || "";
                    return r.ok && type.indexOf("json") >= 0 ? r.json() : null;
                })
                .then(function (d) {
                    if (!d || !polling) return;
                    if (d.total && d.total > 0) {
                        if (!reported) { reported = true; stopCreep(); }
                        var share = Math.min(1, (d.done || 0) / d.total);
                        set(Math.max(level, serverFrom + serverSpan * share),
                            (d.note ? d.note + " · " : "") + (d.done || 0) + " из " + d.total);
                    } else if (d.note) {
                        detail.textContent = d.note + (d.done ? " · " + d.done : "");
                    }
                })
                .catch(function () { /* опрос хода — необязательная часть */ });
        }

        function serverPhase() {
            set(Math.max(level, serverFrom), withFile ? "файл отправлен, программа его разбирает…" : "выполняется…");
            creep(serverFrom + serverSpan * 0.9, withFile ? 20 : 6);
            polling = setInterval(poll, 600);
        }

        function finish() {
            if (polling) { clearInterval(polling); polling = null; }
        }

        var xhr = new XMLHttpRequest();
        xhr.open("POST", form.action || location.href);
        xhr.setRequestHeader("X-Progress", "1");
        xhr.setRequestHeader("X-Progress-Id", opId);
        show(label);
        lock(form, true);
        if (withFile) {
            set(0, "отправка файла…");
            xhr.upload.onprogress = function (ev) {
                if (ev.lengthComputable && ev.total) {
                    set(serverFrom * ev.loaded / ev.total,
                        "отправка файла: " + mb(ev.loaded) + " из " + mb(ev.total) + " МБ");
                }
            };
            xhr.upload.onload = serverPhase;
        } else {
            serverPhase();
        }
        xhr.onload = function () {
            finish();
            var type = xhr.getResponseHeader("content-type") || "";
            if (type.indexOf("application/json") >= 0) {
                var body = null;
                try { body = JSON.parse(xhr.responseText); } catch (e) { body = null; }
                if (body && body.location) {
                    done("готово, открываю страницу…");
                    var target = new URL(body.location, location.href);
                    if (target.pathname === location.pathname && target.search === location.search) {
                        // Тот же адрес (часто с якорем: #settings, #money) — переход
                        // только прокрутил бы страницу: шкала повисла бы на «открываю»,
                        // а итог всплыл бы на следующей странице. Перезагружаем.
                        if (target.hash) location.hash = target.hash;
                        location.reload();
                    } else {
                        location.assign(body.location);
                    }
                    return;
                }
            }
            if (type.indexOf("text/html") >= 0) {
                done();
                replaceDocument(xhr.responseText);
                return;
            }
            lock(form, false);
            fail("Сервер ответил кодом " + xhr.status + ". Обновите страницу и повторите.");
        };
        xhr.onerror = function () {
            finish();
            lock(form, false);
            fail("Нет связи с программой. Проверьте, что служба «mpfinance» запущена, и повторите.");
        };
        xhr.send(data);
    }

    document.addEventListener("submit", function (e) {
        if (!current() || e.defaultPrevented) return;
        var form = e.target;
        if (!form || form.tagName !== "FORM" || form.hasAttribute("data-no-progress")) return;
        var method = (form.getAttribute("method") || "get").toLowerCase();
        var label = labelOf(e.submitter, "Выполняется");
        if (method !== "post") {
            // Фильтр: обычный переход, шкала — пока не открылась страница.
            show(label);
            set(0.05, "открываю страницу…");
            creep(0.9, 2);
            return;
        }
        e.preventDefault();
        sendForm(form, e.submitter, label);
    });

    // ------------------------------------------------------------ ссылки

    function fileName(header, fallback) {
        if (!header) return fallback;
        var star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
        if (star) { try { return decodeURIComponent(star[1].replace(/"/g, "")); } catch (e) { /* дальше */ } }
        var plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
        return plain ? plain[1] : fallback;
    }

    function download(link) {
        var label = labelOf(link, "Выгрузка");
        show(label);
        set(0.02, "готовлю файл…");
        creep(0.6, 8);
        var xhr = new XMLHttpRequest();
        xhr.open("GET", link.href);
        xhr.responseType = "blob";
        xhr.onprogress = function (ev) {
            if (ev.lengthComputable && ev.total) {
                stopCreep();
                set(0.6 + 0.4 * ev.loaded / ev.total, "скачивание: " + mb(ev.loaded) + " из " + mb(ev.total) + " МБ");
            }
        };
        xhr.onload = function () {
            var type = xhr.getResponseHeader("content-type") || "";
            if (xhr.status !== 200 || type.indexOf("text/html") >= 0) {
                // Не файл (вход истёк, ошибка) — открыть как обычную ссылку.
                location.href = link.href;
                return;
            }
            var url = URL.createObjectURL(xhr.response);
            var a = document.createElement("a");
            a.href = url;
            // Служебный клик для сохранения файла — не переход: шкала его не трогает.
            a.setAttribute("data-no-progress", "");
            a.download = fileName(xhr.getResponseHeader("content-disposition"), "выгрузка.xlsx");
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
            done("файл сохранён: " + a.download);
            setTimeout(hide, 2500);
        };
        xhr.onerror = function () {
            fail("Нет связи с программой — файл не скачан. Повторите.");
        };
        xhr.send();
    }

    document.addEventListener("click", function (e) {
        if (!current() || e.defaultPrevented || e.button !== 0) return;
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
        var link = e.target && e.target.closest ? e.target.closest("a[href]") : null;
        if (!link || link.target === "_blank" || link.hasAttribute("data-no-progress")) return;
        var href = link.getAttribute("href") || "";
        if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
        if (link.origin && link.origin !== location.origin) return;
        if (link.hasAttribute("data-download")) {
            e.preventDefault();
            download(link);
            return;
        }
        show(labelOf(link, "Переход"));
        set(0.05, "открываю страницу…");
        creep(0.9, 2);
    });

    // Возврат кнопкой «Назад» достаёт страницу из кэша вместе со шкалой.
    window.addEventListener("pageshow", function (e) { if (e.persisted) hide(); });

    // ------------------------------------------------------------ фоновые дела

    var panel = document.getElementById("jobs-panel");
    var SEEN = "mpfin-jobs-seen";

    function seenKeys() {
        try { return JSON.parse(sessionStorage.getItem(SEEN) || "{}") || {}; } catch (e) { return {}; }
    }

    function saveSeen(keys) {
        try { sessionStorage.setItem(SEEN, JSON.stringify(keys)); } catch (e) { /* без памяти — без итогов */ }
    }

    function minutes(seconds) {
        var m = Math.ceil(seconds / 60);
        return m <= 1 ? "меньше минуты" : "≈ " + m + " мин";
    }

    function jobRow(item, finished) {
        var row = el("div", "job" + (finished ? " job--" + item.status : ""));
        var head = el("div", "job__head");
        var name = el(item.href ? "a" : "span", "job__label", item.label);
        if (item.href) name.href = item.href;
        head.appendChild(name);
        var state = finished ? (item.status === "done" ? "готово" : "ошибка")
            : (item.status === "pending" ? "в очереди" : "идёт");
        head.appendChild(el("span", "job__state", state));
        if (finished) {
            var close = el("button", "job__close", "×");
            close.type = "button";
            close.setAttribute("aria-label", "Скрыть");
            close.addEventListener("click", function () {
                var keys = seenKeys();
                delete keys[item.key];
                saveSeen(keys);
                row.remove();
                if (!panel.querySelector(".job")) panel.hidden = true;
            });
            head.appendChild(close);
        }
        row.appendChild(head);
        var track = el("div", "progress-track" + (!finished && !item.total ? " progress-track--busy" : ""));
        var bar = el("div", "progress-track__fill");
        var share = finished ? 1 : (item.total ? Math.min(1, (item.done || 0) / item.total) : 0.3);
        bar.style.width = (share * 100).toFixed(1) + "%";
        track.appendChild(bar);
        row.appendChild(track);
        var info = item.note || "";
        if (!finished && item.total) info = (info ? info + " · " : "") + (item.done || 0) + " из " + item.total;
        if (!finished && item.eta_seconds) info += " · осталось " + minutes(item.eta_seconds);
        if (info) row.appendChild(el("div", "job__note", info));
        if (finished) {
            var reload = el("a", "job__reload", "Обновить страницу");
            reload.href = location.pathname + location.search;
            row.appendChild(reload);
        }
        return row;
    }

    function renderJobs(data) {
        if (!data) return;
        var keys = seenKeys();
        (data.active || []).forEach(function (item) { keys[item.key] = 1; });
        saveSeen(keys);
        var finished = (data.finished || []).filter(function (item) { return keys[item.key]; });
        while (panel.firstChild) panel.removeChild(panel.firstChild);
        (data.active || []).forEach(function (item) { panel.appendChild(jobRow(item, false)); });
        finished.forEach(function (item) { panel.appendChild(jobRow(item, true)); });
        panel.hidden = !panel.firstChild;
        if ((data.active || []).length) setTimeout(pollJobs, 3000);
    }

    function pollJobs() {
        if (!current()) return;
        fetch("/progress/jobs", {headers: {"Accept": "application/json"}, cache: "no-store"})
            .then(function (r) {
                var type = r.headers.get("content-type") || "";
                return r.ok && type.indexOf("json") >= 0 ? r.json() : null;
            })
            .then(renderJobs)
            .catch(function () { setTimeout(pollJobs, 15000); });
    }

    if (panel) pollJobs();
})();
