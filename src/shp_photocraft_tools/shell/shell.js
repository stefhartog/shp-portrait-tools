(() => {
  function moduleStatus(state) {
    const phase = state.phase || 'unknown';
    const busy = ['starting', 'stopping'].includes(phase) || (phase === 'running' && !!state.busy);
    const status = busy ? phase === 'running' ? 'busy' : phase
      : ['running', 'error', 'unavailable', 'stopped'].includes(phase) ? phase : 'unknown';
    const label = {running:'Running — ready', busy:'Running — busy', starting:'Starting',
      stopping:'Stopping', error:'Error', unavailable:'Unavailable', stopped:'Stopped', unknown:'Unknown status'}[status];
    return {status, busy, label:label+(state.error ? ': '+state.error : '')};
  }
  function renderTabStatus(tab, state) {
    const {busy, label} = moduleStatus(state);
    tab.title = label;
    tab.setAttribute('aria-label', tab.textContent.trim()+': '+label);
    tab.setAttribute('aria-busy', String(busy));
  }
  const names = {crop: 'Crop', dnb: 'D&B'};
  const views = ['mask', 'frequency', 'dnb', 'crop'];
  const modes = views.filter(view => view !== 'crop');
  const $ = id => document.getElementById(id);
  const frames = new Map();
  let modules = {};
  let active = 'crop';
  let initialSelection = true;
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
    if (name === 'skintone') name = 'frequency';
    if (!views.includes(name)) return;
    initialSelection = false;
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
    } else if (data.type === 'shp-skin-mode' && (modes.includes(data.mode) || data.mode === 'skintone') &&
               (data.request === null || Number.isSafeInteger(data.request))) {
      if (pending !== null && data.request !== pending) return;
      pending = null;
      skinMode = data.mode === 'skintone' ? 'frequency' : data.mode;
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
    if (initialSelection && modules.crop?.startup_enabled === false) {
      select(modules.dnb?.phase === 'running' || modules.dnb?.startup_enabled !== false ? 'dnb' : 'crop');
    }
    for (const [name, state] of Object.entries(modules)) {
      document.querySelectorAll(`[role="tab"][data-service="${name}"]`).forEach(tab => {
        renderTabStatus(tab, state);
      });
      const placeholder = $(`panel-${name}`).querySelector('.placeholder');
      placeholder.hidden = state.phase === 'running';
      placeholder.textContent = state.phase === 'stopped'
        ? `${names[name]} is unloaded. Use Start in the Tools tray menu to load it again. Other modules remain available.`
          + (state.startup_enabled === false ? ' Load on startup is disabled; manual Start does not change it.' : '')
          + (name === 'dnb' ? ' Skin Mask, FS + Skintone and D&B share this service.' : '')
        : `${names[name]}: ${state.error || state.phase}. Use the Tools tray menu for service controls.`;
      const frame = frames.get(name);
      if (frame) frame.hidden = state.phase !== 'running';
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
      } else if (['stopping', 'stopped', 'error'].includes(state.phase)) {
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
      document.querySelectorAll('[role="tab"][data-service]').forEach(tab =>
        renderTabStatus(tab, {phase:'unavailable', error:'Supervisor connection lost: '+error.message}));
      report(`Supervisor connection lost: ${error.message}. Reopen SHP PhotoCraft Tools if it does not reconnect.`, true);
    }
    setTimeout(poll, 2000);
  }
  poll();
})();
