chrome.storage.local.get(['jarvis_connected'], ({ jarvis_connected }) => {
    const dot   = document.getElementById('dot');
    const label = document.getElementById('label');
    const sub   = document.getElementById('sub');

    if (jarvis_connected) {
        dot.className   = 'dot on';
        label.textContent = 'Connected';
        sub.textContent   = 'JARVIS is active';
    } else {
        dot.className   = 'dot off';
        label.textContent = 'Disconnected';
        sub.textContent   = 'Start JARVIS to connect';
    }
});
