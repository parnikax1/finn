(() => {
    let opened = null;

    function sync(picker) {
        const select = picker.querySelector('select');
        const selected = select.selectedOptions[0];
        if (!selected) return;
        const label = selected.textContent;
        picker.querySelector('[data-window-label]').textContent = label;
        picker.querySelector('[data-window-trigger]').setAttribute('aria-label', 'Filing window: ' + label);
        for (const option of picker.querySelectorAll('[data-window-value]')) {
            option.setAttribute('aria-selected', String(option.dataset.windowValue === select.value));
        }
    }

    function close(restoreFocus) {
        if (!opened) return;
        const picker = opened;
        opened = null;
        picker.querySelector('[data-window-menu]').hidden = true;
        const trigger = picker.querySelector('[data-window-trigger]');
        trigger.setAttribute('aria-expanded', 'false');
        if (restoreFocus && trigger.isConnected) trigger.focus({preventScroll: true});
    }

    function enhance() {
        if (opened && !opened.isConnected) opened = null;
        for (const picker of document.querySelectorAll('[data-window-picker]')) {
            picker.querySelector('[data-window-trigger]').hidden = false;
            picker.classList.add('window-enhanced');
            sync(picker);
        }
    }

    function open(picker) {
        close(false);
        opened = picker;
        sync(picker);
        const menu = picker.querySelector('[data-window-menu]');
        const trigger = picker.querySelector('[data-window-trigger]');
        const nav = document.getElementById('app-nav').getBoundingClientRect();
        const availableBottom = nav.top > innerHeight / 2 ? nav.top : innerHeight;
        menu.hidden = false;
        const anchor = trigger.getBoundingClientRect();
        const openAbove = availableBottom - anchor.bottom < menu.offsetHeight + 6 && anchor.top > menu.offsetHeight + 6;
        picker.toggleAttribute('data-menu-above', openAbove);
        trigger.setAttribute('aria-expanded', 'true');
        picker.querySelector('[aria-selected="true"]').focus({preventScroll: true});
    }

    function choose(option) {
        const picker = option.closest('[data-window-picker]');
        const select = picker.querySelector('select');
        const changed = select.value !== option.dataset.windowValue;
        select.value = option.dataset.windowValue;
        sync(picker);
        close(true);
        if (changed) select.dispatchEvent(new Event('change', {bubbles: true}));
    }

    document.addEventListener('click', event => {
        const button = event.target.closest('[data-window-trigger]');
        const option = event.target.closest('[data-window-value]');
        if (button) {
            const picker = button.closest('[data-window-picker]');
            if (opened === picker) close(true);
            else open(picker);
        } else if (option) choose(option);
        else if (opened && !opened.contains(event.target)) close(true);
    });

    document.addEventListener('keydown', event => {
        const trigger = event.target.closest('[data-window-trigger]');
        if (trigger && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
            event.preventDefault();
            open(trigger.closest('[data-window-picker]'));
            return;
        }
        if (!opened || !opened.contains(event.target)) return;
        const options = [...opened.querySelectorAll('[data-window-value]')];
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
        }
    });

    for (const event of ['htmx:afterSwap', 'htmx:historyRestore', 'finn:filters']) {
        document.addEventListener(event, enhance);
    }
    enhance();
})();
