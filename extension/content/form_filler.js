// Injected on demand by the service worker.
// Exposes window._jarvis.fillForm(fields, submitSelector, waitMs).
// Handles React/Angular controlled inputs via native prototype setters + synthetic events.

window._jarvis = window._jarvis || {};

window._jarvis.fillForm = async function (fields, submitSelector, waitMs = 500) {
    const results = [];

    for (const { selector, value } of fields) {
        const el = document.querySelector(selector);
        if (!el) {
            results.push({ selector, ok: false, error: 'element not found' });
            continue;
        }

        try {
            el.focus();
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            await sleep(80);

            if (el.contentEditable === 'true') {
                fillContentEditable(el, value);
            } else {
                fillInput(el, value);
            }

            results.push({ selector, ok: true });
        } catch (err) {
            results.push({ selector, ok: false, error: err.message });
        }
    }

    // Submit
    let submitted = false;
    if (submitSelector) {
        const btn = document.querySelector(submitSelector);
        if (btn) {
            await sleep(waitMs);
            btn.click();
            submitted = true;
        }
    }

    return { fields: results, submitted };
};

// ── Fill strategies ───────────────────────────────────────────────────────

function fillInput(el, value) {
    // React / Vue / Angular track value via native prototype setters.
    // Setting el.value directly bypasses their change detection.
    const tag  = el.tagName.toLowerCase();
    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;

    if (nativeSetter) {
        nativeSetter.call(el, value);
    } else {
        el.value = value;
    }

    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
}

function fillContentEditable(el, value) {
    el.focus();
    // Select all existing content then replace
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, value);
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
}

// ── Utilities ─────────────────────────────────────────────────────────────

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
