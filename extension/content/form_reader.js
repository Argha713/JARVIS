// Injected on demand by the service worker.
// Exposes window._jarvis.readForm() — returns structured form data for LLM field mapping.

window._jarvis = window._jarvis || {};

window._jarvis.readForm = function () {
    const fields = [];

    // Collect all interactive form elements
    const elements = document.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select, [contenteditable="true"]'
    );

    for (const el of elements) {
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || tag;

        fields.push({
            selector:  bestSelector(el),
            label:     findLabel(el),
            type,
            name:      el.name      || '',
            id:        el.id        || '',
            placeholder: el.placeholder || '',
            value:     el.value     || el.innerText || '',
        });
    }

    // Also capture submit buttons so the LLM / filler knows what to click
    const submits = [];
    document.querySelectorAll('button[type=submit], input[type=submit], button:not([type])').forEach(btn => {
        submits.push({ selector: bestSelector(btn), text: btn.innerText || btn.value || '' });
    });

    return { url: location.href, fields, submits };
};

// ── Helpers ──────────────────────────────────────────────────────────────

function bestSelector(el) {
    if (el.id)   return `#${CSS.escape(el.id)}`;
    if (el.name) return `[name="${CSS.escape(el.name)}"]`;
    // Fall back to tag + positional index among same-tag siblings
    const tag = el.tagName.toLowerCase();
    const siblings = Array.from(document.querySelectorAll(tag));
    const idx = siblings.indexOf(el);
    return idx > 0 ? `${tag}:nth-of-type(${idx + 1})` : tag;
}

function findLabel(el) {
    // 1. <label for="id">
    if (el.id) {
        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lbl) return lbl.innerText.trim();
    }
    // 2. Wrapping <label>
    const parent = el.closest('label');
    if (parent) return parent.innerText.replace(el.value || '', '').trim();
    // 3. aria-label
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    // 4. Preceding sibling/label text
    const prev = el.previousElementSibling;
    if (prev && ['label', 'span', 'div', 'p'].includes(prev.tagName.toLowerCase())) {
        return prev.innerText.trim();
    }
    return el.placeholder || el.name || '';
}
