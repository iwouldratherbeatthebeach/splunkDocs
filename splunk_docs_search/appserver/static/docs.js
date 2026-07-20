/* Splunk Docs — in-app documentation browser.
 * Loads docdata/nav.json + docdata/search_index.json (written by the scraper)
 * and renders product tabs, a version dropdown, a topic list, client-side
 * search, and an iframe topic reader. Fully offline once docdata/ is present.
 */
(function () {
  'use strict';
  var DATA = 'docdata/';
  var nav = {};          // productTitle -> version -> [ {title,file,section} ]
  var index = [];        // search index
  var products = [];     // productTitle[]
  var state = { product: null, version: null, file: null };

  var $ = function (id) { return document.getElementById(id); };
  function esc(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

  function applyTheme() {
    var dark = localStorage.getItem('sdx-dark') === '1';
    document.body.classList.toggle('sdx-dark', dark);
  }

  function loadJSON(name) {
    return fetch(DATA + name, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error(name + ' HTTP ' + r.status);
      return r.json();
    });
  }

  function versionsFor(p) {
    var vs = Object.keys(nav[p] || {});
    vs.sort(function (a, b) {
      var na = a.split('.').map(Number), nb = b.split('.').map(Number);
      for (var i = 0; i < Math.max(na.length, nb.length); i++) {
        if ((nb[i] || 0) !== (na[i] || 0)) return (nb[i] || 0) - (na[i] || 0);
      }
      return 0;
    });
    return vs;
  }

  function renderTabs() {
    $('sdx-tabs').innerHTML = products.map(function (p) {
      return '<button class="sdx-tab' + (p === state.product ? ' active' : '') +
        '" data-p="' + esc(p) + '">' + esc(p) + '</button>';
    }).join('');
    Array.prototype.forEach.call(document.querySelectorAll('.sdx-tab'), function (b) {
      b.onclick = function () { selectProduct(b.getAttribute('data-p')); };
    });
  }

  function renderVersions() {
    var vs = versionsFor(state.product);
    $('sdx-version').innerHTML = vs.map(function (v) {
      return '<option value="' + esc(v) + '"' + (v === state.version ? ' selected' : '') + '>' +
        (v === '—' ? 'Unversioned' : 'v' + esc(v)) + '</option>';
    }).join('');
    $('sdx-version').onchange = function () { state.version = this.value; renderList(); };
  }

  function renderList() {
    var items = (nav[state.product] && nav[state.product][state.version]) || [];
    items = items.slice().sort(function (a, b) {
      return (a.section || '').localeCompare(b.section || '') || a.title.localeCompare(b.title);
    });
    var html = '', lastSec = null;
    items.forEach(function (it) {
      var sec = it.section || '';
      if (sec !== lastSec) { html += '<div class="sdx-sec">' + esc(sec || 'General') + '</div>'; lastSec = sec; }
      html += '<a class="sdx-item' + (it.file === state.file ? ' active' : '') +
        '" data-f="' + esc(it.file) + '">' + esc(it.title) + '</a>';
    });
    $('sdx-list').innerHTML = html || '<div class="sdx-sec">No topics</div>';
    bindItems();
  }

  function renderSearch(q) {
    q = q.toLowerCase();
    var hits = index.filter(function (r) {
      return r.title.toLowerCase().indexOf(q) >= 0 || (r.text || '').toLowerCase().indexOf(q) >= 0;
    }).slice(0, 200);
    var html = '<div class="sdx-sec">' + hits.length + ' result' + (hits.length === 1 ? '' : 's') + '</div>';
    hits.forEach(function (r) {
      html += '<a class="sdx-item" data-f="' + esc(r.file) + '" data-p="' + esc(r.product_title) +
        '" data-v="' + esc(r.version || '—') + '">' + esc(r.title) +
        '<span class="sdx-vtag">' + esc(r.product_title) + (r.version ? ' · v' + esc(r.version) : '') +
        '</span></a>';
    });
    $('sdx-list').innerHTML = html;
    bindItems(true);
  }

  function bindItems(fromSearch) {
    Array.prototype.forEach.call(document.querySelectorAll('.sdx-item'), function (a) {
      a.onclick = function () {
        if (fromSearch) {
          var p = a.getAttribute('data-p'), v = a.getAttribute('data-v');
          if (p && nav[p]) { state.product = p; state.version = v; renderTabs(); renderVersions(); }
        }
        openTopic(a.getAttribute('data-f'));
      };
    });
  }

  function openTopic(file) {
    if (!file) return;
    state.file = file;
    localStorage.setItem('sdx-last', JSON.stringify(state));
    var f = $('sdx-frame');
    f.src = DATA + 'topics/' + file;
    f.hidden = false;
    $('sdx-status').style.display = 'none';
    Array.prototype.forEach.call(document.querySelectorAll('.sdx-item'), function (a) {
      a.classList.toggle('active', a.getAttribute('data-f') === file);
    });
  }

  function selectProduct(p) {
    state.product = p;
    var vs = versionsFor(p);
    state.version = vs[0] || '—';
    renderTabs(); renderVersions(); renderList();
  }

  function boot() {
    applyTheme();
    $('sdx-theme').onclick = function () {
      localStorage.setItem('sdx-dark', document.body.classList.contains('sdx-dark') ? '0' : '1');
      applyTheme();
    };
    $('sdx-search').oninput = function () {
      var q = this.value.trim();
      if (q.length >= 2) renderSearch(q); else renderList();
    };

    Promise.all([loadJSON('nav.json'), loadJSON('search_index.json')])
      .then(function (res) {
        nav = res[0]; index = res[1];
        products = Object.keys(nav);
        if (!products.length) { $('sdx-status').textContent = 'No documentation yet. Run a download from the Configuration page.'; return; }
        var last = null;
        try { last = JSON.parse(localStorage.getItem('sdx-last') || 'null'); } catch (e) {}
        if (last && nav[last.product]) {
          state = last; renderTabs(); renderVersions(); renderList();
          if (last.file) openTopic(last.file); else $('sdx-status').textContent = 'Select a topic.';
        } else {
          selectProduct(products[0]);
          $('sdx-status').textContent = 'Select a topic from the left.';
        }
      })
      .catch(function (e) {
        $('sdx-status').innerHTML = 'Could not load documentation data (<code>' + esc(e.message) +
          '</code>).<br>Run a download from the Configuration page.';
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}());
