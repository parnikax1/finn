const button = document.querySelector('[data-copy-question]');
if (button) {
    const question = document.getElementById('contact-question');
    const status = document.querySelector('[data-copy-status]');
    button.hidden = false;
    button.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(question.textContent.trim());
            status.textContent = 'Question copied.';
        } catch {
            status.textContent = 'Copy is unavailable. Select and copy the question text.';
            const range = document.createRange();
            range.selectNodeContents(question);
            const selection = window.getSelection();
            if (selection) {
                selection.removeAllRanges();
                selection.addRange(range);
            }
        }
    });
}
