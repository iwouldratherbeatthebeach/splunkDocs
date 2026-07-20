/* Configuration page: trigger downloads/updates and show live status.
 * Talks to the app's custom REST endpoint (bin/docs_handler.py) via the
 * splunkd web proxy. */
(function () {
  'use strict';
  var APP = 'splunk_docs_search';
  var ENDPOINT = 'docs_admin';
  var poll = null;

  function esc(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

  function apiBase() {
    var parts = window.location.pathname.split('/');
    var i = parts.indexOf('static');
    var prefix = i > 0 ? parts.slice(0, i).join('/') : '/en-US';
    return prefix + '/splunkd/__raw/servicesNS/nobody/' + APP + '/' + ENDPOINT;
  }
  function formKey() {
    try { var m = window.parent.document.querySelector('meta[name="splunk-form-key"]'); if (m) return m.content || ''; } catch (e) {}
    var l = document.querySelector('meta[name="splunk-form-key"]'); return l ? l.content : '';
  }
  function unwrap(d){ if(!d) return {}; if(typeof d==='string'){try{return JSON.parse(d);}catch(e){return {raw:d};}} if(d.payload) return unwrap(d.payload); return d; }

  function api(method, member, body) {
    var url = apiBase() + '/' + member + '?output_mode=json';
    var headers = { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' };
    var fk = formKey();
    if (fk && method !== 'GET') headers['X-Splunk-Form-Key'] = fk;
    if (body) headers['Content-Type'] = 'application/json';
    return fetch(url, { method: method, credentials: 'same-origin', headers: headers,
      body: body ? JSON.stringify(body) : undefined }).then(function (r) {
      return r.text().then(function (t) {
        var p = {}; try { p = t ? JSON.parse(t) : {}; } catch (e) { p = { raw: t }; }
        if (!r.ok) throw new Error(p.error || t || r.statusText);
        return unwrap(p);
      });
    });
  }

  function fmt(iso){ if(!iso) return '—'; try{return new Date(iso).toLocaleString();}catch(e){return iso;} }
  function badge(t,k){ return '<span class="badge '+k+'">'+t+'</span>'; }

  function render(d) {
    var b = d.bundle || {}, job = d.job || {}, st = d.settings || {};
    var js = job.status || 'idle';
    var jb = js === 'running' ? badge('Running','warn') : js === 'error' ? badge('Failed','err')
           : js === 'success' ? badge('Completed','ok') : badge('Idle','ok');
    var pct = job.total ? Math.round((job.done || 0) / job.total * 100) : 0;
    var log = (job.log_tail || []).length ? '<div class="log">'+esc((job.log_tail||[]).join('\n'))+'</div>'
            : '<p class="muted">No log yet.</p>';
    document.getElementById('app-root').innerHTML =
      '<h1>Documentation download &amp; updates</h1>'
      + '<p class="lead">Pull Splunk docs from help.splunk.com into this app. Runs on the Splunk host; internet needed only while downloading.</p>'
      + '<div class="grid">'
      + '<div class="card"><h2>Bundle</h2><dl>'
        + '<dt>App version</dt><dd>'+esc(b.app_version||'—')+'</dd>'
        + '<dt>Topics stored</dt><dd>'+((b.topic_count||0).toLocaleString())+'</dd>'
        + '<dt>Last download</dt><dd>'+fmt((b.meta||{}).last_sync_at)+'</dd></dl></div>'
      + '<div class="card"><h2>Job</h2><p>'+jb+'</p><dl>'
        + '<dt>Mode</dt><dd>'+esc(job.mode||'—')+'</dd>'
        + '<dt>Started</dt><dd>'+fmt(job.started_at)+'</dd>'
        + '<dt>Finished</dt><dd>'+fmt(job.finished_at)+'</dd>'
        + (job.total?('<dt>Progress</dt><dd>'+(job.done||0)+' / '+job.total+'</dd>'):'')
        + (job.error?('<dt>Error</dt><dd class="err">'+esc(job.error)+'</dd>'):'')
        + '</dl>' + (job.total?('<div class="bar"><i style="width:'+pct+'%"></i></div>'):'') + '</div>'
      + '<div class="card wide"><h2>Actions</h2><div class="actions">'
        + '<button class="btn primary" id="b-inc">Download / update (incremental)</button>'
        + '<button class="btn" id="b-full">Full refresh (all versions in config)</button>'
        + '<button class="btn" id="b-check">Check for updates</button>'
        + '</div><p class="muted" style="margin-top:8px">Incremental fetches only new topics. Full refresh re-scrapes everything per <code>scraper/products.yaml</code>.</p></div>'
      + '<div class="card wide"><h2>Activity log</h2>'+log+'</div>'
      + '</div>';
    bind(job);
  }

  function busy(v){ ['b-inc','b-full','b-check'].forEach(function(id){var e=document.getElementById(id); if(e)e.disabled=v;}); }

  function bind(job) {
    var inc = document.getElementById('b-inc'), full = document.getElementById('b-full'), chk = document.getElementById('b-check');
    if (inc) inc.onclick = function(){ start('incremental'); };
    if (full) full.onclick = function(){ if(confirm('Full refresh re-scrapes all configured products/versions. Continue?')) start('full'); };
    if (chk) chk.onclick = function(){ busy(true); api('POST','check').then(refresh).catch(err).then(function(){busy(false);}); };
    if (job && job.status === 'running') startPoll();
  }
  function start(mode){
    busy(true);
    api('POST','update',{mode:mode}).then(function(){ startPoll(); return refresh(); })
      .catch(err).then(function(){ busy(false); });
  }
  function refresh(){ return api('GET','status').then(function(d){ render(d); return d; }); }
  function startPoll(){
    if (poll) return;
    poll = setInterval(function(){ refresh().then(function(d){
      if (!d.job || d.job.status !== 'running'){ clearInterval(poll); poll=null; }
    }).catch(function(){ clearInterval(poll); poll=null; }); }, 4000);
  }
  function err(e){ document.getElementById('app-root').innerHTML =
    '<h1>Configuration</h1><p class="err">Could not reach the app backend:</p><div class="log">'+esc(e.message||String(e))+'</div>'
    + '<p class="muted">Ensure the app is installed on Splunk Enterprise (custom REST endpoints require an on-prem instance) and you have admin role.</p>'; }

  refresh().catch(err);
}());
