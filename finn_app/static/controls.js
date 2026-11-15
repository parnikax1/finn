(() => {
    let opened = null;

    function sync(picker) {
        const select = picker.querySelector('select');
        const selected = select.selectedOptions[0];
        if (!selected) return;
        const label = selected.textContent;
        picker.querySelector('[data-picker-text]').textContent = label;
        picker.querySelector('[data-picker-trigger]').setAttribute('aria-label', picker.dataset.pickerLabel + ': ' + label);
        for (const option of picker.querySelectorAll('[data-picker-value]')) {
            option.setAttribute('aria-selected', String(option.dataset.pickerValue === select.value));
        }
    }

    function close(restoreFocus) {
        if (!opened) return;
        const picker = opened;
        opened = null;
        picker.querySelector('[data-picker-menu]').hidden = true;
        const trigger = picker.querySelector('[data-picker-trigger]');
        trigger.setAttribute('aria-expanded', 'false');
        if (restoreFocus && trigger.isConnected) trigger.focus({preventScroll: true});
    }

    function enhance() {
        if (opened && !opened.isConnected) opened = null;
        for (const picker of document.querySelectorAll('[data-picker]')) {
            picker.querySelector('[data-picker-trigger]').hidden = false;
            picker.classList.add('picker-enhanced');
            sync(picker);
        }
    }

    function positionMenu(picker) {
        const menu = picker.querySelector('[data-picker-menu]');
        const anchor = picker.querySelector('[data-picker-trigger]').getBoundingClientRect();
        const viewport = window.visualViewport;
        const inset = 8;
        const gap = 6;
        const left = (viewport?.offsetLeft ?? 0) + inset;
        const right = left + (viewport?.width ?? innerWidth) - 2 * inset;
        const top = (viewport?.offsetTop ?? 0) + inset;
        const nav = document.getElementById('app-nav');
        const bottom = Math.min(top + (viewport?.height ?? innerHeight) - 2 * inset,
            getComputedStyle(nav).position === 'fixed' ? nav.getBoundingClientRect().top - inset : Infinity);

        menu.style.setProperty('--picker-width', `${right - left}px`);
        menu.style.removeProperty('--picker-height');
        const below = bottom - anchor.bottom - gap;
        const above = anchor.top - top - gap;
        const openAbove = menu.offsetHeight > below && above > below;
        picker.toggleAttribute('data-menu-above', openAbove);
        menu.style.setProperty('--picker-height', `${Math.max(0, openAbove ? above : below)}px`);
        const width = menu.offsetWidth;
        const preferred = anchor.left + width <= right ? anchor.left : anchor.right - width;
        const x = Math.max(left, Math.min(preferred, right - width));
        menu.style.setProperty('--picker-left', `${x - picker.getBoundingClientRect().left}px`);
    }

    function open(picker) {
        close(false);
        opened = picker;
        sync(picker);
        const menu = picker.querySelector('[data-picker-menu]');
        menu.hidden = false;
        positionMenu(picker);
        picker.querySelector('[data-picker-trigger]').setAttribute('aria-expanded', 'true');
        const selected = picker.querySelector('[aria-selected="true"]');
        selected.focus({preventScroll: true});
        selected.scrollIntoView({block: 'nearest'});
    }

    function choose(option) {
        const picker = option.closest('[data-picker]');
        const select = picker.querySelector('select');
        const changed = select.value !== option.dataset.pickerValue;
        select.value = option.dataset.pickerValue;
        sync(picker);
        close(true);
        if (changed) {
            select.dispatchEvent(new Event('change', {bubbles: true}));
            if (picker.hasAttribute('data-picker-submit')) select.form.requestSubmit();
        }
    }

    document.addEventListener('click', event => {
        const button = event.target.closest('[data-picker-trigger]');
        const option = event.target.closest('[data-picker-value]');
        if (button) {
            const picker = button.closest('[data-picker]');
            if (opened === picker) close(true);
            else open(picker);
        } else if (option) choose(option);
        else if (opened && !opened.contains(event.target)) close(true);
    });

    document.addEventListener('keydown', event => {
        const trigger = event.target.closest('[data-picker-trigger]');
        if (trigger && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
            event.preventDefault();
            open(trigger.closest('[data-picker]'));
            return;
        }
        if (!opened || !opened.contains(event.target)) return;
        const options = [...opened.querySelectorAll('[data-picker-value]')];
        const index = options.indexOf(event.target);
        if (event.key === 'Escape') {
            event.preventDefault();
            close(true);
            return;
        }
        if (event.key === 'Tab') {
            close(true);
            return;
        }
        const target = {Home: 0, End: options.length - 1,
            ArrowDown: (index + 1) % options.length, ArrowUp: (index + options.length - 1) % options.length}[event.key];
        if (target !== undefined) {
            event.preventDefault();
            options[target].focus({preventScroll: true});
            options[target].scrollIntoView({block: 'nearest'});
        }
    });

    function reposition() {
        if (opened?.isConnected) positionMenu(opened);
    }

    window.addEventListener('resize', reposition);
    window.visualViewport?.addEventListener('resize', reposition);

    for (const event of ['htmx:afterSwap', 'htmx:historyRestore', 'finn:filters']) {
        document.addEventListener(event, enhance);
    }
    enhance();
})();
