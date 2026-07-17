/*
 * pdf_viewer.js — embeds the selected doc's PDF inline in the Splunk Docs
 * Search dashboard. Simple XML sanitizes <iframe> out of <html> panels, so we
 * inject it here (JS runs with full DOM access). PDFs are served as app static
 * assets from appserver/static/pdfs/<pdf_file>.
 */
require([
    'splunkjs/mvc',
    'splunkjs/mvc/searchmanager',
    'splunkjs/mvc/simplexml/ready!'
], function (mvc, SearchManager) {
    'use strict';

    var APP = 'splunk_docs_search';
    var tokens = mvc.Components.get('default');

    function staticBase() {
        var locale = (window.$C && window.$C.LOCALE) ? window.$C.LOCALE : 'en-US';
        return '/' + locale + '/static/app/' + APP + '/pdfs/';
    }

    function renderPdf() {
        var el = document.getElementById('pdfview');
        if (!el) { return; }

        var pf = tokens.get('pdf_file');
        var title = tokens.get('doc_title') || '';
        var url = tokens.get('doc_url') || '';

        var titleEl = document.getElementById('pdf-title');
        if (titleEl) { titleEl.textContent = title || 'Select a document to read'; }

        var openEl = document.getElementById('pdf-open');
        if (openEl) {
            if (url && url.indexOf('$') !== 0) {
                openEl.style.display = '';
                openEl.setAttribute('href', url);
            } else {
                openEl.style.display = 'none';
            }
        }

        if (!pf || pf.indexOf('$') === 0) {
            el.innerHTML = '<div class="pdf-empty">Select a document in the list ' +
                'to view its full PDF here.</div>';
            return;
        }
        var src = staticBase() + encodeURIComponent(pf);
        el.innerHTML = '<iframe class="pdf-frame" title="' +
            title.replace(/"/g, '&quot;') + '" src="' + src + '"></iframe>';
    }

    tokens.on('change:pdf_file change:doc_title change:doc_url', renderPdf);
    renderPdf();

    // Total-pages chip.
    var totalSM = new SearchManager({
        id: 'sds_total_search',
        search: '| tstats count where index=splunk_docs',
        earliest_time: '0',
        latest_time: '',
        autostart: true,
        preview: false
    }, { tokens: false });

    totalSM.on('search:done', function () {
        var results = totalSM.data('results', { count: 1 });
        results.on('data', function () {
            try {
                var rows = results.data().rows;
                if (rows && rows.length) {
                    var el = document.getElementById('sds-total');
                    if (el) { el.textContent = Number(rows[0][0]).toLocaleString(); }
                }
            } catch (e) { /* ignore */ }
        });
    });
});
