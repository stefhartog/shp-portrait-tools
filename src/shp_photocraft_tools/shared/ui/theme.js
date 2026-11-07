(() => {
  const key = 'shp-tools-theme';
  const valid = value => value === 'current' || value === 'photocraft';
  let theme = 'photocraft', picker = null, storageError = '';
  function storageFailure(error) {
    storageError = 'Theme preference could not be saved/read: '+error.message;
    console.warn(storageError);
    if (picker) picker.title = storageError;
  }
  try {
    const saved = localStorage.getItem(key);
    if (valid(saved)) theme = saved;
  } catch (error) { storageFailure(error); }
  function broadcast() {
    document.querySelectorAll('iframe').forEach(frame =>
      frame.contentWindow.postMessage({type:'shp-tools-theme', theme}, location.origin));
  }
  function apply(value, persist = false) {
    if (!valid(value)) return;
    theme = value;
    document.documentElement.dataset.theme = theme;
    if (picker) picker.value = theme;
    if (persist) {
      try { localStorage.setItem(key, theme); }
      catch (error) { storageFailure(error); }
    }
    broadcast();
  }
  apply(theme);
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || !event.data || typeof event.data !== 'object') return;
    if (parent !== window && event.source === parent && event.data.type === 'shp-tools-theme') {
      apply(event.data.theme, true);
    } else if (parent === window && event.data.type === 'shp-tools-theme-ready' &&
               [...document.querySelectorAll('iframe')].some(frame => event.source === frame.contentWindow)) {
      event.source.postMessage({type:'shp-tools-theme', theme}, location.origin);
    }
  });
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply(valid(event.newValue) ? event.newValue : 'photocraft');
  });
  document.addEventListener('DOMContentLoaded', () => {
    if (parent !== window) {
      parent.postMessage({type:'shp-tools-theme-ready'}, location.origin);
      return;
    }
    const host = document.querySelector('.shell-header') || document.getElementById('bar');
    const label = document.createElement('label');
    label.className = 'theme-picker';
    label.textContent = 'Theme';
    picker = document.createElement('select');
    picker.id = 'themeSelect';
    picker.setAttribute('aria-label', 'Global colour theme');
    for (const [value, text] of [['current', 'Current'], ['photocraft', 'PhotoCraft']]) {
      const option = document.createElement('option');
      option.value = value; option.textContent = text; picker.append(option);
    }
    picker.value = theme;
    picker.title = storageError || 'Colour theme for all tools; saved in this browser.';
    picker.onchange = () => apply(picker.value, true);
    label.append(picker);
    const presets = host.querySelector('#presetBar');
    if (presets) presets.insertBefore(label, presets.firstChild);
    else host.insertBefore(label, null);
  });
})();
