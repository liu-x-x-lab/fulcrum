/* 复用有界搜索面板：select 只提交已选值，带 datalist 的文本框允许直接提交手填值。 */
(() => {
  "use strict";
  const LIMIT = 12;
  let sequence = 0;
  let opened = null;

  class ConfigSuggestions {
    constructor(root) {
      this.root = root;
      this.value = root.querySelector("select, input");
      this.editable = this.value.tagName === "INPUT";
      this.catalog = this.editable ? root.querySelector("datalist") : this.value;
      this.readOptions();
      this.input = this.editable ? this.value : document.createElement("input");
      if (this.editable) {
        // 模型 ID 保持为唯一提交字段，只替换浏览器无界候选面板，不复制或隐藏真实输入。
        this.input.removeAttribute("list");
      } else {
        this.input.type = "text";
        this.input.placeholder = root.dataset.placeholder || "输入关键词筛选后选择";
        root.prepend(this.input);
        this.value.hidden = true;
      }
      this.input.autocomplete = "off";
      this.input.disabled = this.value.disabled;
      this.panel = document.createElement("ul");
      this.panel.id = `config-suggestions-${++sequence}`;
      this.panel.className = "ui-select-panel config-suggestions-panel";
      this.panel.setAttribute("role", "listbox");
      this.panel.hidden = true;
      root.append(this.panel);
      if (!this.editable) {
        this.summary = document.createElement("small");
        this.summary.className = "field-hint config-suggestions-summary";
        this.summary.setAttribute("aria-live", "polite");
        root.after(this.summary);
      }
      this.input.setAttribute("role", "combobox");
      this.input.setAttribute("aria-autocomplete", "list");
      this.input.setAttribute("aria-expanded", "false");
      this.input.setAttribute("aria-controls", this.panel.id);
      this.input.setAttribute("aria-label", root.closest("label")?.querySelector("span")?.textContent.trim() || "搜索配置选项");
      this.showValue();
      this.value.addEventListener("change", () => { this.close(); this.showValue(); });
      root.addEventListener("config-suggestions:refresh", () => {
        this.readOptions();
        this.close();
        this.showValue();
      });
      this.input.addEventListener("focus", () => {
        this.input.select();
        this.open("");
      });
      this.input.addEventListener("click", () => {
        if (this.panel.hidden) this.open("");
      });
      this.input.addEventListener("input", () => {
        if (document.activeElement === this.input) this.open(this.input.value.trim());
      });
      this.input.addEventListener("keydown", event => this.onKey(event));
      // 保持输入焦点，并阻止外层 label 将候选项点击转发给输入框。
      this.panel.addEventListener("mousedown", event => event.preventDefault());
      this.panel.addEventListener("click", event => {
        event.preventDefault();
        const item = event.target.closest("[data-value]");
        if (item) this.choose(item.dataset.value);
      });
      root.addEventListener("focusout", () => setTimeout(() => {
        if (!root.contains(document.activeElement)) { this.close(); this.showValue(); }
      }, 0));
      this.input.form?.addEventListener("reset", () => setTimeout(() => { this.close(); this.showValue(); }, 0));
      root.closest("dialog")?.addEventListener("close", () => this.close());
    }

    readOptions() {
      this.options = Array.from(this.catalog.options, option => ({
        value: option.value, label: option.label || option.value,
      }));
    }

    showValue() {
      if (this.editable) return;
      const selected = this.options.find(option => option.value === this.value.value);
      this.input.value = selected ? selected.label : this.value.value;
      this.summary.textContent = selected ? `ID：${selected.value}` : "请选择内置来源";
    }

    open(query) {
      if (this.input.matches(":disabled") || this.input.readOnly) return;
      if (opened && opened !== this) opened.close();
      opened = this;
      const words = query.toLowerCase().split(/\s+/).filter(Boolean);
      const matches = this.options.filter(option => words.every(word => `${option.label} ${option.value}`.toLowerCase().includes(word)));
      // 初次展开将当前模型放在前面，避免它被长目录的前 12 项截掉。
      const selected = matches.find(option => option.value === this.value.value);
      const visible = this.editable && !query && selected ? [selected, ...matches.filter(option => option !== selected)] : matches;
      this.panel.replaceChildren();
      for (const [index, option] of visible.slice(0, LIMIT).entries()) {
        const item = document.createElement("li");
        item.id = `${this.panel.id}-${index}`;
        item.className = "ui-select-option";
        item.dataset.value = option.value;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(option.value === this.value.value));
        item.classList.toggle("is-selected", option.value === this.value.value);
        const name = document.createElement("span");
        name.textContent = option.label;
        item.append(name);
        if (!this.editable) {
          const id = document.createElement("small");
          id.textContent = option.value;
          item.append(id);
        }
        this.panel.append(item);
      }
      const hint = document.createElement("li");
      hint.className = "config-suggestions-count";
      hint.setAttribute("role", "presentation");
      hint.textContent = matches.length ? `${query ? "匹配" : "共"} ${matches.length} 项${matches.length > LIMIT ? `，显示前 ${LIMIT} 项，继续输入筛选` : ""}` :
        (this.editable ? "没有匹配项，可直接使用输入的模型 ID" : "没有匹配来源，请换个关键词搜索");
      this.panel.append(hint);
      this.panel.hidden = false;
      this.input.setAttribute("aria-expanded", "true");
      // 自由输入模式不把前缀自动替换成第一项，只有点击或方向键确认才选候选。
      this.setActive(this.panel.querySelector(".is-selected") || (!this.editable && this.panel.querySelector("[data-value]")));
      this.positionPanel();
    }

    positionPanel() {
      const rect = this.input.getBoundingClientRect();
      const card = this.root.closest(".app-modal-card")?.getBoundingClientRect();
      const below = Math.max(0, Math.min(innerHeight, card?.bottom ?? innerHeight) - rect.bottom - 14);
      const above = Math.max(0, rect.top - Math.max(0, card?.top ?? 0) - 14);
      const up = below < Math.min(280, this.panel.scrollHeight) && above > below;
      this.root.classList.toggle("config-suggestions-up", up);
      this.panel.style.maxHeight = `${Math.min(280, up ? above : below)}px`;
    }

    close() {
      this.panel.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
      this.input.removeAttribute("aria-activedescendant");
      if (opened === this) opened = null;
    }

    setActive(item) {
      this.panel.querySelector(".is-active")?.classList.remove("is-active");
      if (!item) { this.input.removeAttribute("aria-activedescendant"); return; }
      item.classList.add("is-active");
      this.input.setAttribute("aria-activedescendant", item.id);
      // 只滚动候选面板，避免长表单或外层弹窗被 scrollIntoView 带走。
      if (item.offsetTop < this.panel.scrollTop) this.panel.scrollTop = item.offsetTop;
      else if (item.offsetTop + item.offsetHeight > this.panel.scrollTop + this.panel.clientHeight)
        this.panel.scrollTop = item.offsetTop + item.offsetHeight - this.panel.clientHeight;
    }

    choose(value) {
      if (!this.options.some(option => option.value === value)) return;
      this.value.value = value;
      this.showValue();
      this.close();
      this.value.dispatchEvent(new Event("input", { bubbles: true }));
      this.value.dispatchEvent(new Event("change", { bubbles: true }));
    }

    onKey(event) {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); this.close(); this.showValue(); }
      else if (event.key === "Tab") this.close();
      else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (this.panel.hidden) { this.open(""); return; }
        const items = Array.from(this.panel.querySelectorAll("[data-value]"));
        const index = items.indexOf(this.panel.querySelector(".is-active"));
        this.setActive(items[Math.min(Math.max(index + (event.key === "ArrowDown" ? 1 : -1), 0), items.length - 1)]);
      } else if (event.key === "Enter" && !this.panel.hidden) {
        event.preventDefault();
        const active = this.panel.querySelector(".is-active");
        if (active) this.choose(active.dataset.value);
        else this.close();
      }
    }
  }

  function enhance(root) {
    root.querySelectorAll("[data-config-suggestions]:not([data-enhanced])").forEach(element => {
      element.dataset.enhanced = "true";
      new ConfigSuggestions(element);
    });
  }
  document.addEventListener("click", event => {
    if (opened && !event.composedPath().includes(opened.root)) opened.close();
  });
  document.addEventListener("scroll", event => {
    if (opened && !opened.panel.contains(event.target)) opened.positionPanel();
  }, true);
  window.addEventListener("resize", () => opened?.close());
  function start() {
    enhance(document);
    new MutationObserver(records => {
      for (const record of records) for (const node of record.addedNodes) {
        if (node.nodeType === 1 && node.querySelector?.("[data-config-suggestions]")) enhance(node);
      }
    }).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
