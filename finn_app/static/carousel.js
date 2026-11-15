(() => {
    const delay = 7000;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    let current;

    function mount(root) {
        const track = root.querySelector('[data-track]');
        track.querySelector('[data-clone]')?.remove();
        const slides = [...track.querySelectorAll('[data-slide]')];
        if (slides.length < 2) return () => {};

        const controls = root.querySelector('[data-controls]');
        const pause = controls.querySelector('[data-pause]');
        const position = controls.querySelector('[data-position]');
        const dots = [...controls.querySelectorAll('.carousel-dots i')];
        const listeners = new AbortController();
        const listen = (el, event, callback) => el.addEventListener(event, callback, {signal: listeners.signal});
        const copy = slides[0].cloneNode(true);
        copy.dataset.clone = '';
        copy.inert = true;
        copy.setAttribute('aria-hidden', 'true');
        track.append(copy);
        let index = 0;
        let timer;
        let settled;
        let inView = false;
        let paused = false;
        let touching = false;
        let hover = false;
        let focused = false;

        const offset = slide => slide.offsetLeft;

        function render() {
            position.textContent = `${index + 1} / ${slides.length}`;
            for (const [i, slide] of slides.entries()) {
                slide.inert = i !== index;
                dots[i].classList.toggle('current', i === index);
            }
            pause.textContent = paused || motion.matches ? 'Play' : 'Pause';
            pause.setAttribute('aria-label', paused || motion.matches ? 'Play rotation' : 'Pause rotation');
            pause.disabled = motion.matches;
            pause.title = motion.matches ? 'Rotation is disabled by reduced motion' : '';
        }

        function schedule() {
            clearTimeout(timer);
            if (!paused && !motion.matches && !touching && !hover && !focused && inView && !document.hidden) {
                timer = setTimeout(() => move(1), delay);
            }
        }

        function settle() {
            clearTimeout(settled);
            if (!root.isConnected) return;
            const all = [...slides, copy];
            const closest = all.reduce((best, slide, i) =>
                Math.abs(track.scrollLeft - offset(slide)) < Math.abs(track.scrollLeft - offset(all[best])) ? i : best, 0);
            index = closest % slides.length;
            if (closest === slides.length) track.scrollTo({left: offset(slides[0]), behavior: 'instant'});
            render();
            schedule();
        }

        function move(step) {
            clearTimeout(timer);
            const next = index + step;
            const slide = next < 0 ? slides.at(-1) : (slides[next] || copy);
            track.scrollTo({left: offset(slide), behavior: motion.matches || next < 0 ? 'instant' : 'smooth'});
            if (motion.matches || next < 0) settle();
        }

        listen(track, 'scroll', () => {
            clearTimeout(timer);
            clearTimeout(settled);
            settled = setTimeout(settle, 120);
        });
        listen(track, 'scrollend', settle);
        listen(controls.querySelector('[data-prev]'), 'click', () => move(-1));
        listen(controls.querySelector('[data-next]'), 'click', () => move(1));
        listen(pause, 'click', () => {
            paused = !paused;
            if (!paused) {
                // Clicking Play explicitly resumes the timer while keeping keyboard focus.
                focused = hover = false;
            }
            render();
            schedule();
        });
        listen(root, 'pointerenter', event => { if (event.pointerType === 'mouse') { hover = true; schedule(); } });
        listen(root, 'pointerleave', event => { if (event.pointerType === 'mouse') { hover = false; schedule(); } });
        listen(root, 'pointerdown', () => { touching = true; schedule(); });
        for (const event of ['pointerup', 'pointercancel']) {
            listen(window, event, () => { if (touching) { touching = false; schedule(); } });
        }
        listen(root, 'focusin', () => { focused = true; schedule(); });
        listen(root, 'focusout', event => { if (!root.contains(event.relatedTarget)) { focused = false; schedule(); } });
        listen(document, 'visibilitychange', schedule);
        listen(motion, 'change', () => { render(); schedule(); });

        const observer = new IntersectionObserver(([entry]) => {
            inView = entry.isIntersecting && entry.intersectionRatio >= 0.5;
            schedule();
        }, {threshold: 0.5});
        observer.observe(root);
        controls.hidden = false;
        render();
        return () => {
            clearTimeout(timer);
            clearTimeout(settled);
            observer.disconnect();
            listeners.abort();
        };
    }

    function refresh() {
        const root = document.querySelector('[data-carousel]');
        if (root === current?.root) return;
        current?.dispose();
        current = root ? {root, dispose: mount(root)} : null;
    }

    function openSignal(hash) {
        const id = hash.slice(1);
        if (!id.startsWith('signal-')) return;
        const details = document.getElementById(id);
        if (!details?.classList.contains('market-alert')) return;
        details.closest('.market-summary').open = true;
        details.open = true;
    }

    document.addEventListener('click', event => {
        const link = event.target.closest('a[href^="#signal-"]');
        if (link && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
            openSignal(link.hash);
        }
    });
    window.addEventListener('hashchange', () => openSignal(location.hash));
    for (const event of ['htmx:afterSwap', 'htmx:historyRestore']) document.addEventListener(event, refresh);
    refresh();
    openSignal(location.hash);
})();
