/* Embeds the static docs browser into the "Documentation" dashboard view.
 * Injecting the iframe via JS avoids Simple XML's HTML sanitizer. */
require(['splunkjs/mvc/simplexml/ready!'], function () {
  var app = 'splunk_docs_search';
  var locale = (window.$C && window.$C.LOCALE) ? window.$C.LOCALE : 'en-US';
  var src = '/' + locale + '/static/app/' + app + '/docs.html';
  function mount() {
    var host = document.querySelector('.dashboard-body') || document.body;
    var f = document.createElement('iframe');
    f.src = src;
    f.setAttribute('title', 'Splunk Documentation');
    f.style.cssText = 'position:fixed;left:0;right:0;bottom:0;top:74px;width:100%;height:calc(100vh - 74px);border:0;background:#fff';
    document.body.appendChild(f);
    if (host && host.style) { host.style.padding = '0'; }
  }
  if (document.readyState === 'complete') mount();
  else window.addEventListener('load', mount);
});
