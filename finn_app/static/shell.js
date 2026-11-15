const navigation = document.getElementById('app-nav');
const navStatus = document.getElementById('navigation-status');
const statusText = navStatus.querySelector('[role="status"]');
const retryButton = navStatus.querySelector('button');
const screenCache = new Map();
const cacheKey = 'finn:recent-screens-v3';
const freshForMs = 90_000;
try {
    for (const [url, entry] of JSON.parse(sessionStorage.getItem(cacheKey) ?? '[]')) {
        if (Date.now() - entry.at < freshForMs) screenCache.set(url, entry);
    }
} catch {}
try {
    if (!localStorage.getItem('finn:brief-history-v3')) {
        localStorage.removeItem('htmx-history-cache');
        localStorage.setItem('finn:brief-history-v3', '1');
    }
} catch {}
let retryTab = null;
let restoringCached = false;
const destinations = {brief: '/', research: '/research', network: '/network'};
const tabNames = {brief: 'Brief', research: 'Research', network: 'Network'};
const tabLinks = [...navigation.querySelectorAll('[data-tab]')];
function sessionMap(key) {
    try { return new Map(JSON.parse(sessionStorage.getItem(key) ?? '[]')); }
    catch { return new Map(); }
}

const drafts = sessionMap('finn:tab-drafts');
const views = sessionMap('finn:tab-views');
let pendingTab = null;
let pendingRequest = null;
let filterRequest = null;

function screenMarker() {
    return document.querySelector('#app-content [data-screen]');
}

function locationKey() {
    return location.pathname + location.search;
}

function viewKey(path = locationKey()) {
    const url = new URL(path, location.origin);
    if (url.pathname !== '/') return path;
    const params = url.searchParams;
    const company = params.get('company') || '';
    const priority = params.get('priority') || '';
    const q = params.get('q') || '';
    const page = Number(params.get('page') || 1);
    const evidence = Number(['1', 'true', 'on', 'yes'].includes(params.get('evidence')));
    const view = params.get('view') || (company || priority || q || evidence || page > 1 ? 'all' : 'brief');
    return '/?' + new URLSearchParams({view, window: params.get('window') || '7', company, priority, q, page, evidence});
}

function savedUrl(tab) {
    try {
        const saved = sessionStorage.getItem('finn:' + tab);
        if (saved && saved.startsWith(destinations[tab] + '?')) return saved;
    } catch {}
    return destinations[tab];
}

function updateTabs() {
    const screen = screenMarker();
    const tab = screen.dataset.screen;
    const current = locationKey();
    try {
        if (location.pathname === destinations[tab]) sessionStorage.setItem('finn:' + tab, current);
        else if (screen.dataset.briefUrl) sessionStorage.setItem('finn:brief', screen.dataset.briefUrl);
    } catch {}
    for (const link of tabLinks) {
        const id = link.dataset.tab;
        link.href = savedUrl(id);
        if (id === tab) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    }
    const name = tabNames[tab];
    document.getElementById('app-content').setAttribute('aria-label', name);
    document.title = name + ' · FINN';
}

function rememberSession(map, storageKey, value) {
    const key = viewKey();
    map.delete(key);
    map.set(key, value);
    if (map.size > 20) map.delete(map.keys().next().value);
    try { sessionStorage.setItem(storageKey, JSON.stringify([...map])); } catch {}
}

function rememberDraft() {
    const form = document.querySelector('[data-tab-draft]');
    if (!form) return;
    rememberSession(drafts, 'finn:tab-drafts', Object.fromEntries(new FormData(form)));
}

function rememberView() {
    if (location.pathname.startsWith('/events/')) return;
    const disclosures = [...document.querySelectorAll('#app-content details')];
    rememberSession(views, 'finn:tab-views', {scroll: window.scrollY, disclosures: disclosures.map(el => el.open)});
}

function restoreView() {
    const view = views.get(viewKey());
    if (view) {
        const disclosures = [...document.querySelectorAll('#app-content details')];
        if (disclosures.length === view.disclosures.length) {
            disclosures.forEach((el, index) => { el.open = view.disclosures[index]; });
        }
    }
    window.scrollTo(0, view?.scroll ?? 0);
}

function restoreDraft() {
    const form = document.querySelector('[data-tab-draft]');
    const values = drafts.get(viewKey());
    if (!form || !values) return;
    for (const [name, value] of Object.entries(values)) {
        if (form.elements[name]) form.elements[name].value = value;
    }
    updateLookup(form);
}

function updateLookup(form) {
    if (!form?.matches('[data-network-form]')) return;
    const sector = form.elements.kind.value === 'sector';
    form.elements.term.setAttribute('list', sector ? 'network-sectors' : 'network-companies');
    form.elements.term.placeholder = sector ? 'IT services' : 'MARUTI';
}

function focusTitle() {
    const title = document.querySelector('#app-content h1');
    if (title) {
        title.tabIndex = -1;
        title.focus({preventScroll: true});
    }
}

function setFilterMessage(root, busy, failed = false) {
    const loading = root.querySelector('#loading');
    const error = root.querySelector('#filter-error');
    if (loading) loading.hidden = !busy;
    if (error) error.hidden = !failed;
}

function restoreAppliedFilters(root = document) {
    const state = root.querySelector('#brief-state');
    const form = state?.querySelector('#filing-filters');
    if (!form) return;
    const params = new URLSearchParams(state.dataset.applied);
    for (const name of ['window', 'company', 'priority', 'q']) {
        if (form.elements[name]) form.elements[name].value = params.get(name) ?? '';
    }
    if (form.elements.evidence) form.elements.evidence.checked = params.get('evidence') === '1';
}

function finishFilter(xhr, failed) {
    if (filterRequest !== xhr) return;
    filterRequest = null;
    restoreAppliedFilters();
    setFilterMessage(document, false, failed);
}

function cancelFilter() {
    if (!filterRequest) return;
    const xhr = filterRequest;
    finishFilter(xhr, false);
    xhr.abort();
}

function cacheScreen() {
    const tab = screenMarker().dataset.screen;
    if (location.pathname !== destinations[tab] || !screenMarker().hasAttribute('data-cacheable')) return;
    const main = document.getElementById('app-content').cloneNode(true);
    main.removeAttribute('aria-busy');
    setFilterMessage(main, false);
    const html = main.outerHTML;
    if (html.length > 160_000) return;
    const key = viewKey();
    screenCache.delete(key);
    screenCache.set(key, {at: Date.now(), html});
    if (screenCache.size > 6) screenCache.delete(screenCache.keys().next().value);
    try { sessionStorage.setItem(cacheKey, JSON.stringify([...screenCache])); } catch {}
}

function restoreCached(key, push) {
    const entry = screenCache.get(viewKey(key));
    if (!entry || Date.now() - entry.at >= freshForMs) return false;
    if (push) history.pushState({htmx: true}, '', key);
    restoringCached = true;
    try {
        htmx.swap('#app-content', entry.html, {swapStyle: 'outerHTML', settleDelay: 0});
    } finally {
        restoringCached = false;
    }
    updateTabs();
    restoreAppliedFilters();
    setFilterMessage(document, false);
    restoreDraft();
    restoreView();
    focusTitle();
    return true;
}

function showStatus(message = '', failed = false) {
    statusText.textContent = message;
    navStatus.hidden = !message;
    retryButton.hidden = !failed;
}

function clearPending() {
    pendingTab?.removeAttribute('data-pending');
    document.getElementById('app-content').removeAttribute('aria-busy');
    pendingTab = pendingRequest = null;
    retryTab = null;
    showStatus();
}

function failNavigation() {
    const link = pendingTab;
    clearPending();
    if (!link) return;
    retryTab = link;
    showStatus('Could not open ' + tabNames[link.dataset.tab] + '.', true);
}

function openDestination(link, force = false) {
    rememberDraft();
    rememberView();
    if (pendingRequest) {
        const request = pendingRequest;
        pendingRequest = null;
        request.abort();
    }
    clearPending();
    const url = new URL(link.href);
    const key = url.pathname + url.search;
    if (key === locationKey()) return;
    cancelFilter();
    if (!force && restoreCached(key, true)) return;
    pendingTab = link;
    link.dataset.pending = 'true';
    document.getElementById('app-content').setAttribute('aria-busy', 'true');
    showStatus('Opening ' + tabNames[link.dataset.tab] + '…');
    htmx.ajax('GET', key, {
        source: navigation, target: '#app-content', swap: 'outerHTML show:none',
        headers: {'X-FINN-Destination': 'true'},
    }).catch(() => {});
}

navigation.addEventListener('click', (event) => {
    const link = event.target.closest('[data-tab]');
    if (!link || !window.htmx || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    openDestination(link);
});
retryButton.addEventListener('click', () => {
    if (retryTab) openDestination(retryTab, true);
});

document.addEventListener('htmx:beforeSend', (event) => {
    if (event.detail.elt === navigation) pendingRequest = event.detail.xhr;
    if (event.detail.elt.id === 'filing-filters') {
        filterRequest = event.detail.xhr;
        setFilterMessage(document, true);
    }
});
document.addEventListener('htmx:beforeOnLoad', (event) => {
    const {elt, xhr} = event.detail;
    if (elt.id === 'research-fetch-status' && (pendingTab || !elt.isConnected)) {
        event.preventDefault();
    } else if (elt.id === 'filing-filters' && (xhr !== filterRequest || !elt.isConnected)) {
        event.preventDefault();
    } else if (xhr === filterRequest && xhr.status >= 200 && xhr.status < 300 &&
        !xhr.responseText.includes('id="brief-state"')) {
        event.preventDefault();
        finishFilter(xhr, true);
    } else if (xhr === pendingRequest && xhr.status >= 200 && xhr.status < 300 &&
        !xhr.responseText.includes('data-screen="' + pendingTab.dataset.tab + '"')) {
        event.preventDefault();
        failNavigation();
    }
});
document.addEventListener('htmx:afterRequest', (event) => {
    const {xhr, elt, failed} = event.detail;
    // A replacement request can abort its predecessor after collecting new form values.
    if (xhr === filterRequest && xhr.status !== 0) finishFilter(xhr, failed);
    if (elt?.id === 'research-fetch-status' && failed) showPollError(elt);
    if (event.detail.xhr === pendingRequest && (event.detail.failed || !event.detail.successful)) failNavigation();
});
function showPollError(panel) {
    const message = panel.querySelector('[data-poll-error]');
    if (panel.isConnected && message) {
        message.hidden = false;
        panel.querySelector('[data-poll-normal]').hidden = true;
    }
}
for (const type of ['htmx:sendError', 'htmx:timeout', 'htmx:swapError']) {
    document.addEventListener(type, (event) => {
        const {elt, xhr} = event.detail;
        if (xhr === filterRequest) finishFilter(xhr, true);
        if (elt?.id === 'research-fetch-status') showPollError(elt);
        if (event.detail.xhr === pendingRequest) failNavigation();
    });
}
document.addEventListener('htmx:sendAbort', (event) => {
    if (event.detail.xhr === filterRequest) {
        filterRequest = null;
        setFilterMessage(document, false);
    }
});
document.addEventListener('htmx:afterSwap', (event) => {
    if (restoringCached || (event.detail.elt.id !== 'app-content' && event.detail.elt.id !== 'brief-state')) return;
    updateTabs();
    if (event.detail.elt.id === 'app-content') {
        clearPending();
        restoreDraft();
        restoreView();
        focusTitle();
    } else {
        restoreAppliedFilters();
        setFilterMessage(document, false);
    }
    cacheScreen();
});
window.addEventListener('popstate', (event) => {
    cancelFilter();
    if (pendingRequest) {
        const request = pendingRequest;
        pendingRequest = null;
        request.abort();
    }
    clearPending();
    if (event.state?.htmx && restoreCached(locationKey(), false)) event.stopImmediatePropagation();
}, {capture: true});
document.addEventListener('htmx:historyRestore', () => {
    updateTabs();
    restoreDraft();
    focusTitle();
    restoreAppliedFilters();
    setFilterMessage(document, false);
});
window.addEventListener('pageshow', (event) => {
    if (event.persisted && screenMarker().dataset.screen === 'research' && !screenMarker().hasAttribute('data-cacheable')) location.reload();
});
window.addEventListener('pagehide', () => {
    rememberDraft();
    rememberView();
});
document.addEventListener('input', (event) => {
    if (event.target.closest('[data-tab-draft]')) rememberDraft();
});
document.addEventListener('change', (event) => {
    if (event.target.matches('[data-network-form] select[name="kind"]')) {
        const form = event.target.form;
        form.elements.term.value = '';
        updateLookup(form);
        rememberDraft();
    }
});
updateTabs();
cacheScreen();
if (window.htmx) document.documentElement.classList.add("has-shell");
if (location.pathname === '/' && document.referrer.startsWith(location.origin + '/events/')) restoreView();

document.addEventListener('click', (event) => {
    if (event.target.closest('[data-open-sources]')) document.getElementById('sources-export').open = true;
});
