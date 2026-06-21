// Injected on demand by the service worker.
// Exposes window._jarvis.readDom() in the tab's isolated world.

window._jarvis = window._jarvis || {};

window._jarvis.readDom = function (maxChars = 8000) {
    const clone = document.body.cloneNode(true);

    // Strip non-content elements
    clone.querySelectorAll('script, style, nav, footer, header, iframe, noscript, svg').forEach(el => el.remove());

    const text = (clone.innerText || clone.textContent || '')
        .replace(/[ \t]+/g, ' ')       // collapse horizontal whitespace
        .replace(/\n{3,}/g, '\n\n')    // max two consecutive newlines
        .trim();

    return {
        url:   location.href,
        title: document.title,
        text:  text.slice(0, maxChars),
        truncated: text.length > maxChars,
    };
};
