/* Embeds the static About page into the "About" dashboard view. */
require(['splunkjs/mvc/simplexml/ready!'], function () {
  var app = 'splunk_docs_search';
  var locale = (window.$C && window.$C.LOCALE) ? window.$C.LOCALE : 'en-US';
  function mount() {
    var f = document.createElement('iframe');
    f.src = '/' + locale + '/static/app/' + app + '/about.html';
    f.setAttribute('title', 'About Splunk Docs Search');
    f.style.cssText = 'position:fixed;left:0;right:0;bottom:0;top:74px;width:100%;height:calc(100vh - 74px);border:0;background:transparent';
    document.body.appendChild(f);
  }
  if (document.readyState === 'complete') mount();
  else window.addEventListener('load', mount);
});
