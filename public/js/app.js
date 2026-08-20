/* Restkollen. No frameworks, no cookies, no tracking.
   Renders only facts present in Läkemedelsverket's open data JSON.
   Missing values render as "uppgift saknas / not reported". */

(function () {
  "use strict";

  // ---------------- language ----------------
  var LANG_KEY = "restkollen-lang";
  function getLang() {
    try { return localStorage.getItem(LANG_KEY) || "sv"; } catch (e) { return "sv"; }
  }
  function setLang(l) {
    try { localStorage.setItem(LANG_KEY, l); } catch (e) { /* fine */ }
    document.documentElement.setAttribute("data-lang", l);
    document.documentElement.setAttribute("lang", l);
    var btn = document.getElementById("lang-toggle");
    if (btn) btn.textContent = l === "sv" ? "EN" : "SV";
    renderAll();
  }
  document.documentElement.setAttribute("data-lang", getLang());
  document.documentElement.setAttribute("lang", getLang());

  var T = {
    missing: { sv: "uppgift saknas", en: "not reported" },
    expectedBack: { sv: "förväntas åter", en: "expected back" },
    prevDate: { sv: "tidigare", en: "previously" },
    upcoming: { sv: "kommande", en: "upcoming" },
    active: { sv: "pågående", en: "ongoing" },
    substance: { sv: "Substans", en: "Substance" },
    company: { sv: "Företag", en: "Company" },
    cause: { sv: "Orsak (enligt företaget)", en: "Cause (as reported)" },
    firstPublished: { sv: "Först publicerad", en: "First published" },
    packages: { sv: "Förpackningar", en: "Packages" },
    activeShortages: { sv: "pågående restanmälningar", en: "ongoing shortage reports" },
    andUpcoming: { sv: "kommande", en: "upcoming" },
    noResults: { sv: "Inga träffar bland aktuella restanmälningar.", en: "No matches among current shortage reports." },
    searchPlaceholder: { sv: "Namn, substans eller ATC-kod", en: "Name, substance or ATC code" },
    typeShortage: { sv: "Restanmälan", en: "Shortage report" },
    typeCessation: { sv: "försäljning upphör", en: "sales ending" },
    started: { sv: "start", en: "from" },
    ended: { sv: "avslutad", en: "ended" }
  };
  function t(key) { return T[key][getLang()]; }

  // ---------------- data ----------------
  var DATA = { current: null, diff: null, meta: null };

  function fetchJSON(path) {
    return fetch(path, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(path + " -> " + r.status);
      return r.json();
    });
  }

  function load() {
    var wants = [fetchJSON("/data/meta.json"), fetchJSON("/data/current.json")];
    var isHome = !!document.getElementById("today");
    if (isHome) wants.push(fetchJSON("/data/diff.json"));
    return Promise.all(wants).then(function (res) {
      DATA.meta = res[0];
      DATA.current = res[1];
      if (isHome) DATA.diff = res[2];
      renderAll();
    }).catch(function (err) {
      var el = document.getElementById("load-error");
      if (el) el.style.display = "block";
      if (window.console) console.error("Restkollen: data load failed", err);
    });
  }

  // ---------------- helpers ----------------
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function val(v) { return v === null || v === undefined || v === "" ? null : v; }
  function show(v) { return val(v) === null ? t("missing") : esc(v); }

  function activeRecords() {
    if (!DATA.current) return [];
    return DATA.current.records.filter(function (r) {
      return r.status === "active" || r.status === "upcoming";
    });
  }

  function matchesCategory(r, prefixes) {
    if (!r.atc) return false;
    return prefixes.some(function (p) { return r.atc.indexOf(p) === 0; });
  }

  function sortByBack(a, b) {
    var A = a.expectedBack, B = b.expectedBack;
    if (A === null && B === null) return (a.product || "").localeCompare(b.product || "");
    if (A === null) return 1;
    if (B === null) return -1;
    if (A !== B) return A < B ? -1 : 1;
    return (a.product || "").localeCompare(b.product || "");
  }

  // ---------------- rendering: stamp + stale banner ----------------
  function renderStamp() {
    if (!DATA.meta) return;
    var stampEls = document.querySelectorAll(".js-verified");
    var d = DATA.meta.lastVerified ? DATA.meta.lastVerified.slice(0, 10) : null;
    stampEls.forEach(function (el) { el.textContent = d || "\u2013"; });
    if (d) {
      var ageDays = Math.floor((Date.now() - new Date(d + "T12:00:00").getTime()) / 864e5);
      var banner = document.getElementById("stale-banner");
      if (banner && ageDays >= 2) {
        banner.classList.add("show");
        banner.querySelectorAll(".js-verified").forEach(function (el) { el.textContent = d; });
      }
    }
  }

  // ---------------- rendering: home ----------------
  function changeLine(ev, type) {
    var meta = [];
    if (val(ev.substance)) meta.push(esc(ev.substance));
    if (val(ev.atc)) meta.push("ATC " + esc(ev.atc));
    var back = "";
    if (type === "new" || type === "date_changed") {
      back = " \u00b7 " + t("expectedBack") + " <strong>" +
        (val(ev.expectedBack) ? esc(ev.expectedBack) : t("missing")) + "</strong>";
      if (type === "date_changed" && val(ev.previousExpectedBack)) {
        back += " (" + t("prevDate") + " " + esc(ev.previousExpectedBack) + ")";
      }
    }
    var cess = ev.typeOfShortage === "CESSATION"
      ? ' <span class="badge b-cessation">' + t("typeCessation") + "</span>" : "";
    return '<li><span class="p-name">' + show(ev.product) + "</span>" + cess + back +
      (meta.length ? '<div class="p-meta">' + meta.join(" · ") + "</div>" : "") + "</li>";
  }

  function renderGroup(id, events, type) {
    var group = document.getElementById(id);
    if (!group) return;
    group.querySelector(".change-count").textContent = events.length;
    var list = group.querySelector(".change-list");
    var empty = group.querySelector(".change-empty");
    if (!events.length) {
      list.innerHTML = "";
      if (empty) empty.style.display = "block";
      return;
    }
    if (empty) empty.style.display = "none";
    list.innerHTML = events.map(function (ev) { return changeLine(ev, type); }).join("");
  }

  function renderHome() {
    if (!DATA.diff) return;
    var dateEl = document.getElementById("today-date");
    if (dateEl) dateEl.textContent = DATA.diff.date;
    renderGroup("g-new", DATA.diff.new, "new");
    renderGroup("g-back", DATA.diff.back, "back");
    renderGroup("g-changed", DATA.diff.dateChanged, "date_changed");
    renderCategoryCounts();
  }

  function renderCategoryCounts() {
    var cards = document.querySelectorAll("[data-cat-prefixes]");
    cards.forEach(function (card) {
      var prefixes = card.getAttribute("data-cat-prefixes").split(",");
      var n = activeRecords().filter(function (r) { return matchesCategory(r, prefixes); }).length;
      var el = card.querySelector(".js-cat-count");
      if (el) el.textContent = n;
    });
  }

  // ---------------- rendering: category page ----------------
  function recordItem(r) {
    var badge = r.status === "upcoming"
      ? ' <span class="badge b-upcoming">' + t("upcoming") + "</span>" : "";
    if (r.typeOfShortage === "CESSATION") {
      badge += ' <span class="badge b-cessation">' + t("typeCessation") + "</span>";
    }
    var backHtml = val(r.expectedBack)
      ? '<span class="s-back">' + t("expectedBack") + " " + esc(r.expectedBack) + "</span>"
      : '<span class="s-back missing">' + t("expectedBack") + ": " + t("missing") + "</span>";
    var pkgs = (r.packages || []).map(function (p) {
      var bits = [];
      if (val(p.description)) bits.push(esc(p.description));
      if (val(p.start)) bits.push(t("started") + " " + esc(p.start));
      if (val(p.actualEnd)) bits.push(t("ended") + " " + esc(p.actualEnd));
      else bits.push(t("expectedBack") + " " + (val(p.expectedBack) ? esc(p.expectedBack) : t("missing")));
      return '<div class="pkg">' + bits.join(" · ") + "</div>";
    }).join("");
    return '<li class="shortage-item" id="npl' + esc(r.nplId) + '"><details>' +
      "<summary>" +
      '<span class="s-name">' + show(r.product) + badge + "</span>" +
      '<span class="s-sub">' + show(r.substance) + " · ATC " + show(r.atc) + "</span>" +
      backHtml +
      "</summary>" +
      '<div class="s-detail"><dl>' +
      "<dt>" + t("company") + "</dt><dd>" + show(r.mah) + "</dd>" +
      "<dt>" + t("cause") + "</dt><dd>" + show(r.cause || r.causeCategory) + "</dd>" +
      "<dt>" + t("firstPublished") + "</dt><dd>" + show(r.firstPublished) + "</dd>" +
      "<dt>" + t("packages") + "</dt><dd>" + (pkgs || t("missing")) + "</dd>" +
      "</dl></div></details></li>";
  }

  function renderCategory() {
    var cfg = window.RK_CATEGORY;
    var list = document.getElementById("cat-list");
    if (!cfg || !list || !DATA.current) return;
    var recs = activeRecords().filter(function (r) { return matchesCategory(r, cfg.prefixes); });
    recs.sort(sortByBack);
    var nUp = recs.filter(function (r) { return r.status === "upcoming"; }).length;
    var line = document.getElementById("cat-count");
    if (line) {
      line.textContent = (recs.length - nUp) + " " + t("activeShortages") +
        (nUp ? " · " + nUp + " " + t("andUpcoming") : "");
    }
    list.innerHTML = recs.map(recordItem).join("");
  }

  // ---------------- rendering: search ----------------
  function renderSearch() {
    var input = document.getElementById("search");
    var out = document.getElementById("search-results");
    if (!input || !out) return;
    var q = input.value.trim().toLowerCase();
    if (q.length < 2) {
      out.innerHTML = "";
      return;
    }
    var hits = activeRecords().filter(function (r) {
      return ["product", "substance", "atc", "atcTerm", "mah"].some(function (f) {
        return r[f] && r[f].toLowerCase().indexOf(q) !== -1;
      });
    });
    hits.sort(sortByBack);
    if (!hits.length) {
      out.innerHTML = '<p class="hint">' + t("noResults") + "</p>";
      return;
    }
    out.innerHTML = '<ul class="shortage-list">' +
      hits.slice(0, 50).map(recordItem).join("") + "</ul>";
  }

  // ---------------- copy feed URL ----------------
  function wireCopyButtons() {
    document.querySelectorAll(".btn-copy").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var url = new URL(btn.getAttribute("data-feed"), location.href).href;
        (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject())
          .then(function () {
            var old = btn.textContent;
            btn.textContent = "✓";
            setTimeout(function () { btn.textContent = old; }, 1500);
          })
          .catch(function () { window.prompt("URL:", url); });
      });
    });
  }

  function renderAll() {
    var si = document.getElementById("search");
    if (si) si.placeholder = t("searchPlaceholder");
    renderStamp();
    renderHome();
    renderCategory();
    renderSearch();
  }

  // ---------------- init ----------------
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("lang-toggle");
    if (btn) {
      btn.textContent = getLang() === "sv" ? "EN" : "SV";
      btn.addEventListener("click", function () {
        setLang(getLang() === "sv" ? "en" : "sv");
      });
    }
    var input = document.getElementById("search");
    if (input) input.addEventListener("input", renderSearch);
    wireCopyButtons();
    load();
  });
})();
