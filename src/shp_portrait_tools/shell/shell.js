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
    if (state.phase !== 'unavailable') tab.hidden = state.phase === 'stopped';
    const indicator = tab.querySelector?.('.cache-indicator');
    if (indicator && state.phase !== 'running') {
      indicator.dataset.state = 'unavailable';
      tab.dataset.cacheLabel = 'Cache status unavailable while Skin Tools is not running';
    }
    const cacheLabel = tab.dataset?.cacheLabel;
    tab.title = label+(cacheLabel ? '; '+cacheLabel : '');
    tab.setAttribute('aria-label', tab.textContent.trim()+': '+tab.title);
    tab.setAttribute('aria-busy', String(busy));
  }
  const names = {skin: 'Skin Tools'};
  const views = ['mask', 'frequency', 'dnb', 'combine', 'settings'];
  const modes = ['mask', 'frequency', 'dnb', 'combine'];
  function viewAvailable(name) {
    return name === 'settings' || modules.skin?.phase !== 'stopped';
  }
  const $ = id => document.getElementById(id);
  const frames = new Map();
  let modules = {};
  let active = 'mask';
  let initialSelection = true;
  let skinMode = 'mask';
  let sequence = 0, pending = null;
  const ready = new WeakSet();
  let skinBusy = false;
  function setCacheTabs(tabs) {
    for (const mode of modes) {
      const tab = $(`tab-${mode}`), indicator = tab.querySelector('.cache-indicator');
      const status = tabs?.[mode];
      if (!status || !['idle','warming','ready','unavailable'].includes(status.state) ||
          typeof status.label !== 'string' || status.label.length > 300) continue;
      indicator.dataset.state = status.state;
      tab.dataset.cacheLabel = status.label;
      renderTabStatus(tab, modules.skin || {phase:'unknown'});
    }
  }
  function clearCacheTabs() {
    setCacheTabs(Object.fromEntries(modes.map(mode => [mode,{state:'idle',label:'Cache not checked'}])));
  }
  function navigationLocked() { return skinBusy || !!modules.skin?.busy; }
  function syncNavigation() {
    const locked = navigationLocked();
    window.__shpNavigationLocked = locked;
    document.querySelectorAll('[role="tab"]').forEach(tab => {
      tab.disabled = locked;
      tab.setAttribute('aria-disabled', String(locked));
    });
  }

  function requestMode() {
    const frame = frames.get('skin');
    if (!frame || !ready.has(frame)) return;
    pending = ++sequence;
    frame.contentWindow.postMessage({type:'shp-tools-mode', mode:skinMode, request:pending}, location.origin);
  }

  function report(message, error = false) {
    $('status').textContent = message;
    $('status').classList.toggle('err', error);
  }
  function select(name, notify = true, internal = false) {
    if (navigationLocked() && !internal) return;
    if (name === 'skintone') name = 'frequency';
    if (!views.includes(name) || !viewAvailable(name)) return;
    initialSelection = false;
    active = name;
    for (const key of views) {
      const selected = key === active;
      $(`tab-${key}`).setAttribute('aria-selected', String(selected));
      $(`tab-${key}`).tabIndex = selected ? 0 : -1;
    }
    for (const key of [...Object.keys(names), 'settings']) {
      const selected = key === (active === 'settings' ? 'settings' : 'skin');
      const panel = $(`panel-${key}`);
      panel.classList.toggle('inactive', !selected);
      panel.setAttribute('aria-hidden', String(!selected));
      panel.inert = !selected;
      if (selected) panel.setAttribute('aria-labelledby', `tab-${active}`);
    }
    if (modes.includes(name)) { skinMode = name; if (notify) requestMode(); }
  }
  function mountGeneralSettings(frame) {
    const source = frame.contentDocument.getElementById('generalSettingsContent');
    if (!source) return report('General settings are unavailable in this skin-tools view. Refresh Tools when work is saved.', true);
    // Restore native availability setters before controls leave the iframe's panel gating.
    source.querySelectorAll('button,input,select,textarea').forEach(control => { delete control.disabled; });
    $('generalSettingsHost').replaceChildren(...source.children);
    $('generalSettingsHost').hidden = false;
    frame.contentDocument.getElementById('generalSettings').hidden = true;
    $('settingsAvailability').hidden = true;
  }
  window.addEventListener('message', event => {
    const frame = frames.get('skin'), data = event.data;
    if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin ||
        !data || typeof data !== 'object') return;
    if (data.type === 'shp-skin-ready') {
      if (!ready.has(frame)) mountGeneralSettings(frame);
      ready.add(frame);
      frame.contentWindow.postMessage({type:'shp-tools-connect'}, location.origin);
      if (modes.includes(active)) requestMode();
    } else if (data.type === 'shp-watch-loaded') {
      pending = null;
      select('mask', false, true);
    } else if (data.type === 'shp-skin-busy' && typeof data.busy === 'boolean') {
      skinBusy = data.busy;
      if (skinBusy) pending = null;
      syncNavigation();
    } else if (data.type === 'shp-skin-cache' && data.tabs && typeof data.tabs === 'object') {
      setCacheTabs(data.tabs);
    } else if (data.type === 'shp-skin-mode' && (modes.includes(data.mode) || data.mode === 'skintone') &&
               (data.request === null || Number.isSafeInteger(data.request))) {
      if (navigationLocked()) return;
      if (data.request !== null && data.request !== pending) return;
      if (pending !== null && data.request !== pending) return;
      pending = null;
      skinMode = data.mode === 'skintone' ? 'frequency' : data.mode;
      if (modes.includes(active)) select(skinMode, false);
    }
  });
  document.querySelectorAll('[role="tab"]').forEach(button => {
    button.onclick = () => select(button.dataset.view);
    button.onkeydown = event => {
      if (navigationLocked()) { event.preventDefault(); return; }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        const direction = event.key === 'ArrowRight' ? 1 : -1;
        const available = views.filter(viewAvailable);
        select(available[(available.indexOf(active) + direction + available.length) % available.length]);
        $(`tab-${active}`).focus();
      }
    };
  });
  function render() {
    if (modules.skin?.phase !== 'running') skinBusy = false;
    syncNavigation();
    if (initialSelection || !viewAvailable(active)) {
      select(['mask', 'settings'].find(viewAvailable), true, true);
    }
    for (const [name, state] of Object.entries(modules)) {
      if (!(name in names)) continue;
      document.querySelectorAll(`[role="tab"][data-service="${name}"]`).forEach(tab => {
        renderTabStatus(tab, state);
      });
      const placeholder = $(`panel-${name}`).querySelector('.placeholder');
      placeholder.hidden = state.phase === 'running';
      placeholder.textContent = state.phase === 'stopped'
        ? `${names[name]} is unloaded. Use Start in the Tools tray menu to load it again.`
          + (state.startup_enabled === false ? ' Load on startup is disabled; manual Start does not change it.' : '')
          + ' Skin Mask, FS & Tone, Dodge & Burn and Combine share this service.'
        : `${names[name]}: ${state.error || state.phase}. Use the Tools tray menu for service controls.`;
      const frame = frames.get(name);
      if (name === 'skin') {
        const available = state.phase === 'running' && frame?.dataset.instance === state.instance && ready.has(frame);
        $('settingsAvailability').hidden = available;
        $('generalSettingsHost').hidden = !available;
      }
      if (frame) frame.hidden = state.phase !== 'running';
      if (state.phase === 'running' && (!frame || frame.dataset.instance !== state.instance)) {
        frame?.remove();
        const next = document.createElement('iframe');
        next.title = `SHP Portrait Tools — ${names[name]}`;
        next.src = `${name}/`;
        next.dataset.instance = state.instance;
        pending = null;
        clearCacheTabs();
        next.onload = () => next.contentWindow.postMessage({type:'shp-tools-probe'}, location.origin);
        $(`panel-${name}`).append(next);
        frames.set(name, next);
      } else if (['stopping', 'stopped', 'error'].includes(state.phase)) {
        frame?.remove();
        frames.delete(name);
        pending = null; clearCacheTabs();
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
      report(`Supervisor connection lost: ${error.message}. Reopen SHP Portrait Tools if it does not reconnect.`, true);
    }
    setTimeout(poll, 2000);
  }
  poll();
})();
