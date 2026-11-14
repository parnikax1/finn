const navigation = document.getElementById('app-nav');
const navStatus = document.getElementById('navigation-status');
const destinations = {brief: '/', research: '/research', network: '/network'};
const tabLinks = [...navigation.querySelectorAll('[data-tab]')];
const drafts = new Map();
const views = new Map();
let pendingTab = null;
let pendingRequest = null;

function screenMarker() {
    return document.querySelector('#app-content [data-screen]');
}

function locationKey() {
    return location.pathname + location.search;
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
    const name = {brief: 'Brief', research: 'Research', network: 'Network'}[tab];
    document.getElementById('app-content').setAttribute('aria-label', name);
    document.title = name + ' · FINN';
}

function rememberDraft() {
    const form = document.querySelector('[data-tab-draft]');
    if (!form) return;
    const draft = Object.fromEntries(new FormData(form));
    drafts.delete(locationKey());
    drafts.set(locationKey(), draft);
    if (drafts.size > 20) drafts.delete(drafts.keys().next().value);
}

function rememberView() {
    const key = locationKey();
    const disclosures = [...document.querySelectorAll('#app-content details')];
    views.delete(key);
    views.set(key, {scroll: window.scrollY, disclosures: disclosures.map(el => el.open)});
    if (views.size > 20) views.delete(views.keys().next().value);
}

function restoreView() {
    const view = views.get(locationKey());
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
    const values = drafts.get(locationKey());
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

function clearPending() {
    pendingTab?.removeAttribute('data-pending');
    document.getElementById('app-content').removeAttribute('aria-busy');
    pendingTab = pendingRequest = null;
    navStatus.textContent = '';
}

navigation.addEventListener('click', (event) => {
    const link = event.target.closest('[data-tab]');
    if (!link || !window.htmx || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    rememberDraft();
    rememberView();
    if (pendingRequest) pendingRequest.abort();
    clearPending();
    if (new URL(link.href).pathname + new URL(link.href).search === locationKey()) return;
    pendingTab = link;
    link.dataset.pending = 'true';
    document.getElementById('app-content').setAttribute('aria-busy', 'true');
    navStatus.textContent = 'Loading ' + link.dataset.tab;
    htmx.ajax('GET', link.getAttribute('href'), {
        source: navigation, target: '#app-content', swap: 'outerHTML show:none',
        headers: {'X-FINN-Destination': 'true'},
    }).catch(() => {});
});

document.addEventListener('htmx:beforeSend', (event) => {
    if (event.detail.elt === navigation) pendingRequest = event.detail.xhr;
});
document.addEventListener('htmx:afterRequest', (event) => {
    if (event.detail.xhr !== pendingRequest || event.detail.successful) return;
    const aborted = event.detail.xhr.status === 0;
    clearPending();
    if (!aborted) navStatus.textContent = 'Could not open that destination. Tap again to retry.';
});
document.addEventListener('htmx:sendError', (event) => {
    if (event.detail.xhr !== pendingRequest) return;
    clearPending();
    navStatus.textContent = 'Could not open that destination. Tap again to retry.';
});
document.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.elt.id !== 'app-content' && event.detail.elt.id !== 'feed') return;
    updateTabs();
    if (event.detail.elt.id === 'app-content') {
        clearPending();
        restoreDraft();
        restoreView();
        focusTitle();
    }
});
window.addEventListener('popstate', () => {
    if (pendingRequest) pendingRequest.abort();
    clearPending();
}, {capture: true});
document.addEventListener('htmx:historyRestore', () => {
    updateTabs();
    restoreDraft();
    focusTitle();
    // The filter control may have changed just before HTMX cached the previous feed.
    const form = document.querySelector('#filing-filters');
    if (form) {
        const params = new URLSearchParams(location.search);
        for (const name of ['window', 'company', 'priority', 'q']) {
            form.elements[name].value = params.get(name) ?? (name === 'window' ? '7' : '');
        }
        form.elements.evidence.checked = ['1', 'true', 'on', 'yes'].includes(params.get('evidence'));
    }
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

document.addEventListener('click', (event) => {
    if (event.target.closest('[data-open-sources]')) document.getElementById('sources-export').open = true;
});
