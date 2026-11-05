(() => {
  const names = {crop: 'Crop', dnb: 'D&B'};
  const $ = id => document.getElementById(id);
  const frames = new Map();
  let modules = {};
  let active = 'crop';

  function report(message, error = false) {
    $('status').textContent = message;
    $('status').classList.toggle('err', error);
  }
  function select(name) {
    active = name;
    for (const key of Object.keys(names)) {
      const selected = key === active;
      $(`tab-${key}`).setAttribute('aria-selected', String(selected));
      $(`tab-${key}`).tabIndex = selected ? 0 : -1;
      const panel = $(`panel-${key}`);
      panel.classList.toggle('inactive', !selected);
      panel.setAttribute('aria-hidden', String(!selected));
      panel.inert = !selected;
    }
  }
  document.querySelectorAll('[role="tab"]').forEach(button => {
    button.onclick = () => select(button.dataset.module);
    button.onkeydown = event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        select(active === 'crop' ? 'dnb' : 'crop');
        $(`tab-${active}`).focus();
      }
    };
  });
  function render() {
    for (const [name, state] of Object.entries(modules)) {
      const badge = $(`badge-${name}`);
      badge.textContent = state.busy && state.phase === 'running' ? 'busy' : state.phase;
      badge.dataset.phase = state.phase;
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
        $(`panel-${name}`).append(next);
        frames.set(name, next);
      } else if (['stopped', 'error'].includes(state.phase)) {
        frame?.remove();
        frames.delete(name);
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
