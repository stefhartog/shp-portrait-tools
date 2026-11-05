/* A pending request keeps background tabs alive without relying on throttled timers. */
(() => {
  if (window.__shpOwnedDesktop === true) return; // Native controller owns its hidden-window lease.
  let client;
  let active = false;
  let controller;
  let retry;
  let generation = 0;
  const report = error => {
    console.error('SHP PhotoCraft Tools tab connection:', error);
    const status = document.getElementById('status');
    if (status) {
      status.textContent = 'Server connection lost. Reopen SHP PhotoCraft Tools if it does not reconnect.';
      status.classList.add('err');
    }
  };
  async function heartbeat(epoch) {
    while (active && epoch === generation) {
      controller = new AbortController();
      try {
        const response = await fetch('api/ui/heartbeat', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({client}), signal: controller.signal,
        });
        if (!response.ok) throw Error(`Heartbeat returned ${response.status}`);
        const result = await response.json();
        if (!result.managed) return;
      } catch (error) {
        if (!active || epoch !== generation) return;
        report(error);
        retry = setTimeout(() => heartbeat(epoch), 5000);
        return;
      }
    }
  }
  function start() {
    if (active) return;
    active = true;
    client = crypto.randomUUID();
    heartbeat(++generation);
  }
  window.addEventListener('pagehide', () => {
    active = false;
    generation++;
    clearTimeout(retry);
    controller?.abort();
    navigator.sendBeacon('api/ui/close', new Blob([JSON.stringify({client})], {type:'application/json'}));
  });
  window.addEventListener('pageshow', start);
  start();
})();
