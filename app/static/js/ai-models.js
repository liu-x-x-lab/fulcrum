(() => {
  'use strict';
  document.querySelectorAll('[data-ai-model-picker]').forEach((picker) => {
    const provider = picker.querySelector('[data-ai-provider]');
    const model = picker.querySelector('[data-ai-model]');
    const list = picker.querySelector('[data-ai-model-list]');
    const refresh = (changed) => {
      let names = [];
      try { names = JSON.parse(provider.selectedOptions[0]?.dataset.models || '[]'); } catch (_) { names = []; }
      if (!Array.isArray(names)) names = [];
      list.replaceChildren();
      names.forEach((name) => {
        const option = document.createElement('option');
        option.value = String(name);
        list.appendChild(option);
      });
      if (changed) {
        model.value = '';
        model.dispatchEvent(new Event('input', { bubbles: true }));
      }
      // 目录随供应商更换；共享搜索框只刷新候选，不改写当前手填或继承值。
      list.closest('[data-config-suggestions]')?.dispatchEvent(new Event('config-suggestions:refresh'));
    };
    provider.addEventListener('change', () => refresh(true));
    provider.form?.addEventListener('reset', () => setTimeout(() => refresh(false), 0));
    refresh(false);
  });
})();
