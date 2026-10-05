(function () {
  "use strict";
  var doc = document.documentElement, body = document.body;
  var data = {};
  try { data = JSON.parse(document.getElementById("wl-data").textContent || "{}"); } catch (e) {}
  var t = data.t || {};
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var fine = window.matchMedia("(pointer: fine)").matches;
  var fx = function (name) { return body.classList.contains("fx-" + name); };
  doc.classList.add("js");

  /* тема: выбор посетителя запоминается, если браузер разрешает хранилище */
  var themeBtn = document.querySelector("[data-theme-toggle]");
  try { if (localStorage.getItem("wl-theme-" + data.slug) === "alt") doc.classList.add("alt"); } catch (e) {}
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var swap = function () {
      doc.classList.toggle("alt");
      try { localStorage.setItem("wl-theme-" + data.slug, doc.classList.contains("alt") ? "alt" : "base"); } catch (e) {}
    };
    if (document.startViewTransition && !reduce) document.startViewTransition(swap); else swap();
  });

  /* меню на телефоне */
  var nav = document.querySelector(".nav");
  var burger = document.querySelector(".burger");
  if (burger) burger.addEventListener("click", function () {
    var open = nav.classList.toggle("is-open");
    burger.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.querySelectorAll(".links a").forEach(function (a) {
    a.addEventListener("click", function () { nav.classList.remove("is-open"); if (burger) burger.setAttribute("aria-expanded", "false"); });
  });

  /* прокрутка: полоса прогресса, шапка прячется вниз и появляется вверх, линия шагов, параллакс */
  var lastY = 0, ticking = false;
  var steps = document.querySelector(".steps");
  var parallax = fx("parallax") && !reduce ? document.querySelectorAll("[data-parallax]") : [];
  function onScroll() {
    var y = window.scrollY, max = doc.scrollHeight - window.innerHeight;
    doc.style.setProperty("--p", max > 0 ? (y / max).toFixed(4) : 0);
    if (nav) {
      nav.classList.toggle("is-scrolled", y > 30);
      nav.classList.toggle("is-hidden", y > lastY && y > 400 && !nav.classList.contains("is-open"));
    }
    if (steps) {
      var r = steps.getBoundingClientRect(), vh = window.innerHeight;
      var fill = Math.min(1, Math.max(0, (vh * 0.6 - r.top) / r.height));
      steps.style.setProperty("--fill", fill.toFixed(3));
    }
    for (var i = 0; i < parallax.length; i++) {
      var el = parallax[i], rect = el.getBoundingClientRect();
      var k = parseFloat(el.getAttribute("data-parallax")) || 0.12;
      el.style.setProperty("--py", ((rect.top + rect.height / 2 - window.innerHeight / 2) * -k).toFixed(1) + "px");
    }
    lastY = y; ticking = false;
  }
  window.addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();

  /* появление блоков и счётчики */
  function countUp(el) {
    var raw = el.getAttribute("data-count") || "";
    var m = raw.match(/^([^\d]*)([\d\s.,]+)(.*)$/);
    if (!m || reduce) { el.textContent = raw; return; }
    var num = parseFloat(m[2].replace(/\s/g, "").replace(",", "."));
    if (isNaN(num)) { el.textContent = raw; return; }
    var decimals = (m[2].split(/[.,]/)[1] || "").length, start = null, dur = 1600;
    function frame(ts) {
      if (!start) start = ts;
      var p = Math.min(1, (ts - start) / dur), eased = 1 - Math.pow(1 - p, 4);
      var v = (num * eased).toFixed(decimals);
      el.textContent = m[1] + (decimals ? v.replace(".", ",") : Number(v).toLocaleString("ru-RU")) + m[3];
      if (p < 1) requestAnimationFrame(frame); else el.textContent = raw;
    }
    requestAnimationFrame(frame);
  }
  var revealEls = document.querySelectorAll("[data-reveal],[data-count]");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        en.target.classList.add("in");
        if (en.target.hasAttribute("data-count")) countUp(en.target);
        io.unobserve(en.target);
      });
    }, { threshold: 0.15, rootMargin: "0px 0px -40px 0px" });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("in"); });
  }

  /* подсветка карточек за курсором и лёгкий 3D-наклон */
  if (fine) {
    document.querySelectorAll(".card").forEach(function (card) {
      var tilt = fx("tilt") && !reduce;
      card.addEventListener("pointermove", function (e) {
        var r = card.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
        card.style.setProperty("--mx", x + "px"); card.style.setProperty("--my", y + "px");
        if (tilt) {
          card.style.setProperty("--ry", ((x / r.width - 0.5) * 7).toFixed(2) + "deg");
          card.style.setProperty("--rx", ((0.5 - y / r.height) * 7).toFixed(2) + "deg");
        }
      });
      card.addEventListener("pointerleave", function () { card.style.setProperty("--rx", "0deg"); card.style.setProperty("--ry", "0deg"); });
    });
  }

  /* магнитные кнопки */
  if (fine && fx("magnetic") && !reduce) {
    document.querySelectorAll(".btn").forEach(function (btn) {
      btn.addEventListener("pointermove", function (e) {
        var r = btn.getBoundingClientRect();
        btn.style.setProperty("--bx", ((e.clientX - r.left - r.width / 2) * 0.25).toFixed(1) + "px");
        btn.style.setProperty("--by", ((e.clientY - r.top - r.height / 2) * 0.35).toFixed(1) + "px");
      });
      btn.addEventListener("pointerleave", function () { btn.style.setProperty("--bx", "0px"); btn.style.setProperty("--by", "0px"); });
    });
  }

  /* прожектор на первом экране */
  var spot = document.querySelector(".hero-spotlight");
  if (spot && fine) spot.addEventListener("pointermove", function (e) {
    var r = spot.getBoundingClientRect();
    spot.style.setProperty("--x", (e.clientX - r.left) + "px"); spot.style.setProperty("--y", (e.clientY - r.top) + "px");
  });

  /* свой курсор */
  if (fine && fx("cursor") && !reduce) {
    var ring = document.querySelector(".cursor"), dot = document.querySelector(".cursor-dot");
    var mx = 0, my = 0, rx = 0, ry = 0;
    window.addEventListener("pointermove", function (e) {
      mx = e.clientX; my = e.clientY; body.classList.add("has-cursor");
      dot.style.transform = "translate(" + mx + "px," + my + "px)";
    });
    document.addEventListener("pointerleave", function () { body.classList.remove("has-cursor"); });
    document.querySelectorAll("a,button,summary,.card").forEach(function (el) {
      el.addEventListener("pointerenter", function () { ring.classList.add("is-hover"); });
      el.addEventListener("pointerleave", function () { ring.classList.remove("is-hover"); });
    });
    (function loop() {
      rx += (mx - rx) * 0.16; ry += (my - ry) * 0.16;
      ring.style.transform = "translate(" + rx.toFixed(1) + "px," + ry.toFixed(1) + "px)";
      requestAnimationFrame(loop);
    })();
  }

  /* лента работ: тянуть мышью */
  document.querySelectorAll(".track").forEach(function (track) {
    var down = false, startX = 0, startLeft = 0, moved = false;
    track.addEventListener("pointerdown", function (e) {
      if (e.pointerType !== "mouse") return;
      down = true; moved = false; startX = e.clientX; startLeft = track.scrollLeft; track.classList.add("is-drag");
    });
    window.addEventListener("pointermove", function (e) {
      if (!down) return;
      var dx = e.clientX - startX; if (Math.abs(dx) > 4) moved = true;
      track.scrollLeft = startLeft - dx;
    });
    window.addEventListener("pointerup", function () { down = false; track.classList.remove("is-drag"); });
    track.addEventListener("click", function (e) { if (moved) { e.preventDefault(); e.stopPropagation(); } }, true);
  });

  /* форма заявки: на сервере студии уходит владельцу сайта в Telegram, без сервера — открывает Telegram или почту */
  var form = document.querySelector("[data-lead-form]");
  if (form) form.addEventListener("submit", function (e) {
    e.preventDefault();
    var note = form.querySelector(".form-note"), btn = form.querySelector("button[type=submit]");
    var payload = { name: form.name.value.trim(), phone: form.phone.value.trim(), message: form.message.value.trim(), website: form.website.value };
    if (!payload.name || payload.phone.replace(/\D/g, "").length < 7) {
      note.textContent = t.invalid || "Укажите имя и телефон"; note.className = "form-note"; return;
    }
    var text = payload.name + ", " + payload.phone + (payload.message ? "\n" + payload.message : "");
    var fallback = function () {
      if (data.telegram) window.open("https://t.me/" + data.telegram + "?text=" + encodeURIComponent(text), "_blank", "noopener");
      else if (data.email) location.href = "mailto:" + data.email + "?body=" + encodeURIComponent(text);
      note.textContent = t.sent || "Спасибо!"; note.className = "form-note ok";
    };
    if (!data.lead || location.protocol === "file:") { fallback(); return; }
    btn.disabled = true;
    fetch(data.lead, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function () { note.textContent = t.sent || "Спасибо!"; note.className = "form-note ok"; form.reset(); })
      .catch(function () { note.textContent = t.failed || "Не получилось отправить"; note.className = "form-note"; fallback(); })
      .then(function () { btn.disabled = false; });
  });
})();
