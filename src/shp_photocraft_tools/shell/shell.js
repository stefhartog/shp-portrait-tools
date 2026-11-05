(() => {
  const names = {crop: 'Crop', dnb: 'D&B'};
  const views = ['crop', 'mask', 'frequency', 'dnb'];
  const modes = views.slice(1);
  const $ = id => document.getElementById(id);
  const frames = new Map();
  let modules = {};
  let active = 'crop';
  let skinMode = 'dnb';
  let sequence = 0, pending = null;
  const ready = new WeakSet();

  function requestMode() {
    const frame = frames.get('dnb');
    if (!frame || !ready.has(frame)) return;
    pending = ++sequence;
    frame.contentWindow.postMessage({type:'shp-tools-mode', mode:skinMode, request:pending}, location.origin);
  }

  function report(message, error = false) {
    $('status').textContent = message;
    $('status').classList.toggle('err', error);
  }
  function select(name, notify = true) {
    if (!views.includes(name)) return;
    active = name;
    for (const key of views) {
      const selected = key === active;
      $(`tab-${key}`).setAttribute('aria-selected', String(selected));
      $(`tab-${key}`).tabIndex = selected ? 0 : -1;
    }
    for (const key of Object.keys(names)) {
      const selected = key === (active === 'crop' ? 'crop' : 'dnb');
      const panel = $(`panel-${key}`);
      panel.classList.toggle('inactive', !selected);
      panel.setAttribute('aria-hidden', String(!selected));
      panel.inert = !selected;
      if (selected) panel.setAttribute('aria-labelledby', `tab-${active}`);
    }
    if (name !== 'crop') { skinMode = name; if (notify) requestMode(); }
  }
  window.addEventListener('message', event => {
    const frame = frames.get('dnb'), data = event.data;
    if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin ||
        !data || typeof data !== 'object') return;
    if (data.type === 'shp-skin-ready') {
      ready.add(frame);
      frame.contentWindow.postMessage({type:'shp-tools-connect'}, location.origin);
      if (active !== 'crop') requestMode();
    } else if (data.type === 'shp-skin-mode' && modes.includes(data.mode) &&
               (data.request === null || Number.isSafeInteger(data.request))) {
      if (pending !== null && data.request !== pending) return;
      pending = null;
      skinMode = data.mode;
      if (active !== 'crop') select(skinMode, false);
    }
  });
  document.querySelectorAll('[role="tab"]').forEach(button => {
    button.onclick = () => select(button.dataset.view);
    button.onkeydown = event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        const direction = event.key === 'ArrowRight' ? 1 : -1;
        select(views[(views.indexOf(active) + direction + views.length) % views.length]);
        $(`tab-${active}`).focus();
      }
    };
  });
  function render() {
    for (const [name, state] of Object.entries(modules)) {
      document.querySelectorAll(`.badge[data-service="${name}"]`).forEach(badge => {
        badge.textContent = state.busy && state.phase === 'running' ? 'busy' : state.phase;
        badge.dataset.phase = state.phase;
      });
      const placeholder = $(`panel-${name}`).querySelector('.placeholder');
      placeholder.hidden = state.phase === 'running';
      placeholder.textContent = `${names[name]}: ${state.error || state.phase}. Use the Tools tray menu for service controls.`;
      const frame = frames.get(name);
      if (state.phase === 'running' && (!frame || frame.dataset.instance !== state.instance)) {
        frame?.remove();
        const next = document.createElement('iframe');
        next.title = `SHP PhotoCraft ${names[name]}`;
        next.src = `${name}/`;
        next.dataset.instance = state.instance;
        if (name === 'dnb') {
          pending = null;
          next.onload = () => next.contentWindow.postMessage({type:'shp-tools-probe'}, location.origin);
        }
        $(`panel-${name}`).append(next);
        frames.set(name, next);
      } else if (['stopped', 'error'].includes(state.phase)) {
        frame?.remove();
        frames.delete(name);
        if (name === 'dnb') pending = null;
      }
    }
  }
  async function poll() {
    try {
      const response = await fetch('api/status');
      if (!response.ok) throw Error(`Status returned ${response.status}`);
      const status = await response.json();
      modules = status.modules;
      render();
      report('');
    } catch (error) {
      report(`Supervisor connection lost: ${error.message}. Reopen SHP PhotoCraft Tools if it does not reconnect.`, true);
    }
    setTimeout(poll, 2000);
  }
  poll();
})();
