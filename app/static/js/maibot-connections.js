/* MaiBot 专用编辑器：密钥只留在本次页面内存中，绝不写入浏览器存储。 */
(function () {
  "use strict";
  document.querySelectorAll("[data-maibot-manager]").forEach(function (root) {
    var endpoint = root.dataset.endpoint;
    var canEdit = root.dataset.canEdit === "true";
    var connections = [], currentId = "", version = "", dirty = false, busy = false, loaded = false;
    var type = "group", page = 1, total = 0, hasMore = false, rows = [], cache = new Map();
    var requestSequence = 0, searchTimer;
    var pickerOpen = false, pickerTrigger = null, pickerAnchor = null;
    var states = { connected: "已连接", connecting: "连接中 / 断线重试", stopped: "已停止", waiting: "等待建立连接", disabled: "已停用", not_configured: "未配置连接地址" };
    var diagnosticLabels = {
      auth_rejected: "MaiBot 拒绝连接，请检查令牌与鉴权设置", path_not_found: "连接路径不存在", tls_failure: "证书校验失败",
      connect_timeout: "连接超时", network_failure: "无法连接，请检查地址、端口和网络", handshake_rejected: "服务端拒绝连接", connection_lost: "连接已断开",
      queued: "等待转发", waiting_media: "等待微信附件", forwarded: "已写入连接", expired: "消息等待连接超时", forward_failed: "转发失败，结果未确认",
      mention_required: "已跳过：这条消息没有 @ 机器人", queue_full: "发送队列繁忙，消息未入队",
      reply_queued: "已收到，回复已入队", reply_scope_changed: "已忽略：聊天归属或配置已变更", reply_disabled: "已收到，当前规则不允许回复",
      reply_no_text: "已收到，回复没有可发送的文字", reply_unsupported: "已收到，当前渠道没有可发送的内容", reply_duplicate: "重复回复，已忽略", reply_failed: "收到回复，处理失败",
      delivery_queued: "等待发送", delivery_running: "发送中", delivery_retry: "发送出错，等待重试", delivery_failed: "渠道发送失败",
      delivery_skipped: "发送任务已取消", delivery_expired: "发送任务已过期", delivery_scope_changed: "未发送：聊天权限或配置已变更",
      delivery_waiting_receipt: "已交给渠道，等待回执", delivery_sent: "渠道已确认发送", delivery_unconfirmed: "已交给渠道，发送结果未确认"
    };
    var formatTime = new Intl.DateTimeFormat(undefined, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    function get(name) { return root.querySelector("[data-mb-" + name + "]"); }
    function current() { return connections.find(function (item) { return item.id === currentId; }); }
    function key(target) { return JSON.stringify([Number(target.channel_id), target.chat_type, target.external_id]); }
    function targetOnly(target) { return { channel_id: Number(target.channel_id), chat_type: target.chat_type, external_id: target.external_id }; }
    function esc(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
    function feedback(message, error) { get("feedback").textContent = message; get("feedback").dataset.error = String(!!error); }
    function markDirty() { dirty = true; feedback("有未保存的更改，保存后统一应用。"); }
    function writeState() {
      root.querySelectorAll("[data-mb-write]").forEach(function (control) { control.disabled = busy || !canEdit || !loaded; });
      root.querySelectorAll("[data-mb-type]").forEach(function (control) { control.disabled = busy || !loaded; });
    }
    function positionPicker() {
      var panel = get("picker-panel");
      if (!pickerOpen || !pickerTrigger || !panel.showPopover) return;
      var rect = pickerTrigger.getBoundingClientRect(), viewport = window.visualViewport;
      var top = viewport ? viewport.offsetTop : 0, bottom = top + (viewport ? viewport.height : innerHeight);
      var width = Math.min(640, Math.max(rect.width, 360), innerWidth - 24);
      var below = bottom - rect.bottom - 18, above = rect.top - top - 18;
      var heightLimit = Math.min(440, bottom - top - 24);
      var up = below < heightLimit && above > below;
      panel.classList.toggle("mb-picker-compact", bottom - top < 480);
      panel.style.width = width + "px";
      // 固定可视高度，搜索或翻页不让面板跳动；短屏时始终留在视口内。
      panel.style.height = heightLimit + "px";
      panel.style.maxHeight = heightLimit + "px";
      panel.style.left = Math.max(12, Math.min(rect.left, innerWidth - width - 12)) + "px";
      var height = panel.getBoundingClientRect().height;
      panel.style.top = Math.max(top + 12, Math.min(up ? rect.top - height - 6 : rect.bottom + 6, bottom - height - 12)) + "px";
      pickerAnchor = rect;
    }
    function closePicker(restoreFocus) {
      if (!pickerOpen) return;
      pickerOpen = false;
      clearTimeout(searchTimer); requestSequence++;
      var panel = get("picker-panel");
      var accountTrigger = panel.querySelector('.ui-select-trigger[aria-expanded="true"]');
      if (accountTrigger) accountTrigger.click();
      if (panel.hidePopover && panel.matches(":popover-open")) panel.hidePopover();
      panel.hidden = true;
      root.querySelectorAll("[data-mb-type]").forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
      if (restoreFocus && pickerTrigger) pickerTrigger.focus({ preventScroll: true });
    }
    function openPicker(kind) {
      if (!loaded || busy || !current()) return;
      if (pickerOpen && type === kind) { closePicker(true); return; }
      closePicker(false);
      type = kind; page = 1; rows = []; total = 0; hasMore = false;
      get("search").value = "";
      pickerTrigger = root.querySelector('[data-mb-type="' + kind + '"]');
      var panel = get("picker-panel"), label = kind === "group" ? "群聊" : "私聊";
      get("picker-title").textContent = "选择" + label;
      panel.setAttribute("aria-label", "选择" + label);
      get("targets").setAttribute("aria-label", "可选择的" + label);
      get("search-label").textContent = kind === "group" ? "搜索群名称、备注或 ID" : "搜索好友名称、备注或 ID";
      pickerOpen = true; panel.hidden = false;
      // Popover 的顶层渲染避免功能卡片的 overflow 裁剪下拉；旧浏览器退化为普通面板。
      if (panel.showPopover) panel.showPopover();
      else panel.classList.add("mb-picker-fallback");
      renderTargets();
      get("search").focus({ preventScroll: true });
      loadTargets();
    }
    function owner(target) {
      var id = key(target);
      return connections.find(function (item) {
        var scope = item.scope;
        var explicit = scope.targets.some(function (t) { return key(t) === id; });
        var inherited = scope.mode === "legacy_all" && !scope.exclusions.some(function (t) { return key(t) === id; }) &&
          item[target.chat_type === "group" ? "collect_group_messages" : "collect_private_messages"] !== false;
        return explicit || inherited;
      });
    }
    function count(item) {
      return item.scope.targets.reduce(function (n, target) { n[target.chat_type === "group" ? "groups" : "direct"]++; return n; }, { groups: 0, direct: 0 });
    }
    function renderConnections() {
      get("connections").innerHTML = connections.map(function (item) {
        var n = count(item), scope = item.scope.mode === "legacy_all" ? "保留旧聊天范围" : n.groups + " 群 / " + n.direct + " 私聊";
        return '<button class="mb-connection" type="button" data-mb-connection="' + esc(item.id) + '" aria-pressed="' + (item.id === currentId) + '"><strong>' + esc(item.name || "未命名连接") + '</strong><small>' + esc(!item.enabled ? "已停用" : states[(item.runtime || {}).state] || "待保存") + " · " + scope + '</small></button>';
      }).join("");
      get("empty").hidden = connections.length !== 0;
      get("empty-actions").hidden = connections.length !== 0;
      get("editor").hidden = !current();
      renderDiagnostics();
    }
    function renderDiagnostics() {
      var item = current();
      if (!item) return;
      var runtime = item.runtime || {}, diagnostics = runtime.diagnostics || {}, link = diagnostics.connection;
      var connectionText = (link && diagnosticLabels[link.code]) || states[runtime.state] || "保存后显示状态";
      if (get("connection-detail").textContent !== connectionText) get("connection-detail").textContent = connectionText;
      root.querySelectorAll("[data-mb-stage]").forEach(function (node) {
        var stage = node.dataset.mbStage, entry = diagnostics[stage];
        var empty = stage === "forward" && runtime.no_targets ? "尚未分配聊天" : "暂无记录";
        var label = entry ? diagnosticLabels[entry.code] || empty : empty;
        if (entry && entry.code === "delivery_batch" && entry.counts) {
          label = Object.keys(entry.counts).map(function (code) { return (diagnosticLabels[code] || "未知状态") + " " + entry.counts[code] + " 条"; }).join(" · ");
        }
        var date = entry && entry.at ? new Date(entry.at) : null;
        if (date && !isNaN(date.getTime())) label += " · " + formatTime.format(date);
        if (node.textContent !== label) node.textContent = label;
      });
    }
    function renderTargets() {
      var active = current();
      if (!active) return;
      var focused = document.activeElement, focusedChat = focused && focused.dataset.mbChat;
      get("targets").innerHTML = rows.length ? rows.map(function (target) {
        var selectedOwner = owner(target), selected = selectedOwner && selectedOwner.id === currentId;
        var occupied = selectedOwner && !selected;
        var disabled = !canEdit || busy || (target.selectable === false && !selected) || occupied;
        var warning = target.warning ? '<small class="mb-warning">' + esc(target.warning) + '</small>' : "";
        return '<div class="mb-target"><label class="mb-target-label"><input type="checkbox" data-mb-chat="' + esc(key(target)) + '"' + (selected ? " checked" : "") + (disabled ? " disabled" : "") + '><span class="mb-target-copy"><strong>' + esc(target.name || target.external_id) + '</strong><small>' + esc(target.channel_name) + " · " + esc(target.remark || target.external_id) + '</small>' + warning + (occupied ? '<small>由「' + esc(selectedOwner.name) + '」负责' + (!selectedOwner.enabled ? "（连接已停用）" : "") + '</small>' : "") + '</span></label>' + (occupied ? '<button class="btn btn-secondary" type="button" data-mb-transfer="' + esc(key(target)) + '"' + (!canEdit || busy || target.selectable === false ? " disabled" : "") + '>转给当前连接</button>' : "") + '</div>';
      }).join("") : '<p class="muted-text">没有匹配的聊天。可先同步群聊或好友列表后再查找。</p>';
      get("result-count").textContent = total + " 项";
      get("page").textContent = "第 " + page + " 页";
      get("prev").disabled = page <= 1;
      get("next").disabled = !hasMore;
      var n = count(active);
      root.querySelectorAll("[data-mb-type]").forEach(function (button) {
        var kind = button.dataset.mbType, selected = kind === "group" ? n.groups : n.direct;
        button.setAttribute("aria-expanded", String(pickerOpen && kind === type));
        button.querySelector("[data-mb-picker-count]").textContent = active.scope.mode === "legacy_all" ? "沿用旧范围" : selected ? "已选 " + selected + " 个" : "尚未选择";
      });
      get("count").textContent = (active.scope.mode === "legacy_all" ? "沿用旧范围" : "已选 " + n.groups + " 个群聊、" + n.direct + " 个私聊") + (active[type === "group" ? "collect_group_messages" : "collect_private_messages"] === false ? " · 此类消息已关闭接收，可在更多设置开启" : "");
      get("legacy").hidden = active.scope.mode !== "legacy_all";
      get("exclusions").textContent = active.scope.exclusions.length ? "已排除 " + active.scope.exclusions.length + " 个聊天。" : "";
      get("selection").innerHTML = '<span class="muted-text">已明确选择：</span>' + (active.scope.targets.length ? active.scope.targets.map(function (target) {
        var info = cache.get(key(target)) || target;
        return '<button class="mb-selected-chip" type="button" data-mb-remove="' + esc(key(target)) + '" aria-label="移除 ' + esc(info.name || target.external_id) + '，' + esc(info.channel_name || "账号 #" + target.channel_id) + '"' + (!canEdit || busy ? " disabled" : "") + '>' + esc(info.name || target.external_id) + " · " + esc(info.channel_name || "账号 #" + target.channel_id) + (info.selectable === false ? "（已失效）" : "") + ' ×</button>';
      }).join("") : '<span class="muted-text">' + (active.scope.mode === "legacy_all" ? "无需逐个勾选；可直接转交其他连接。" : "尚未选择聊天") + '</span>');
      renderConnections();
      positionPicker();
      if (focusedChat && pickerOpen) {
        var replacement = Array.from(get("targets").querySelectorAll("[data-mb-chat]")).find(function (control) { return control.dataset.mbChat === focusedChat; });
        if (replacement && !replacement.disabled) replacement.focus({ preventScroll: true });
      }
    }
    function renderEditor() {
      closePicker(false);
      var item = current();
      renderConnections();
      if (!item) return;
      get("title").textContent = item.name || "未命名连接";
      get("name").value = item.name;
      get("address").value = item.api_server_ws_url || "";
      get("token").value = item.token || "";
      get("token").placeholder = item.token_configured ? "已配置，留空保持不变" : "输入连接令牌（如有）";
      get("token-status").textContent = item.token_configured ? "已配置；填写新令牌会替换原值。" : "未配置；MaiBot 开启鉴权时需要填写。";
      get("enabled").checked = item.enabled;
      get("mode").value = item.enable_reply_output ? "chat" : "collect";
      get("protocol").value = item.protocol || "maim";
      get("mode").dispatchEvent(new Event("change"));
      get("protocol").dispatchEvent(new Event("change"));
      get("mention").checked = !!item.mention_only;
      get("clear-token").checked = !!item.clear_token;
      root.querySelectorAll("[data-mb-flag]").forEach(function (control) { control.checked = item[control.dataset.mbFlag] !== false; });
      writeState();
      renderTargets();
    }
    function api(path, data) {
      var options = { credentials: "same-origin", headers: { Accept: "application/json" } };
      if (data !== undefined) {
        options.method = "POST";
        options.headers["Content-Type"] = "application/json";
        options.headers["X-CSRF-Token"] = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
        options.body = JSON.stringify(data);
      }
      return fetch(endpoint + path, options).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok || data.ok === false) throw new Error(typeof data.detail === "string" ? data.detail : data.notice || "操作失败，请稍后重试");
          return data;
        });
      });
    }
    function receiveState(data) {
      connections = data.connections;
      version = data.version;
      if (!connections.some(function (item) { return item.id === currentId; })) currentId = connections[0] ? connections[0].id : "";
      (data.selected_targets || []).forEach(function (item) { cache.set(key(item), item); });
      var account = get("account").value;
      get("account").innerHTML = '<option value="">全部接入账号</option>' + data.channels.map(function (item) { return '<option value="' + item.id + '">' + esc(item.name) + '</option>'; }).join("");
      get("account").value = data.channels.some(function (item) { return String(item.id) === account; }) ? account : "";
      get("binding-warning").hidden = data.binding_enabled;
      loaded = true;
      dirty = false;
      renderEditor();
      writeState();
    }
    function loadTargets() {
      if (!loaded || !current() || !pickerOpen) return Promise.resolve();
      var sequence = ++requestSequence;
      var query = new URLSearchParams({ chat_type: type, q: get("search").value, channel_id: get("account").value || "0", page: String(page) });
      get("targets").setAttribute("aria-busy", "true");
      if (!rows.length) get("targets").innerHTML = '<p class="muted-text">正在载入聊天列表…</p>';
      return api("/targets?" + query).then(function (data) {
        if (sequence !== requestSequence) return;
        rows = data.rows; page = data.page; total = data.total; hasMore = data.has_more;
        rows.forEach(function (item) { cache.set(key(item), item); });
        renderTargets();
      }).catch(function (error) { if (sequence === requestSequence) feedback(error.message, true); })
        .finally(function () { if (sequence === requestSequence) get("targets").removeAttribute("aria-busy"); });
    }
    async function reload() {
      if (busy) return;
      if (dirty && !await window.appDialog.confirm({ title: "重新载入配置？", message: "未保存的更改将会丢弃。", confirmText: "重新载入" })) return;
      closePicker(false);
      busy = true; writeState(); renderTargets();
      feedback("正在载入连接配置…");
      api("").then(function (data) { receiveState(data); feedback(canEdit ? "配置已载入。" : "只读模式，可搜索和查看配置。"); return loadTargets(); }).catch(function (error) { feedback(error.message, true); })
        .finally(function () { busy = false; writeState(); renderTargets(); });
    }
    function assign(target, selected) {
      var active = current(), id = key(target);
      if (!active || !canEdit || busy) return;
      if (selected) {
        connections.forEach(function (item) {
          item.scope.targets = item.scope.targets.filter(function (t) { return key(t) !== id; });
          if (item.scope.mode === "legacy_all" && item.id !== currentId && !item.scope.exclusions.some(function (t) { return key(t) === id; })) item.scope.exclusions.push(targetOnly(target));
        });
        active.scope.exclusions = active.scope.exclusions.filter(function (t) { return key(t) !== id; });
        if (active.scope.mode !== "legacy_all" || active[target.chat_type === "group" ? "collect_group_messages" : "collect_private_messages"] === false) active.scope.targets.push(targetOnly(target));
      } else {
        active.scope.targets = active.scope.targets.filter(function (t) { return key(t) !== id; });
        if (active.scope.mode === "legacy_all" && !active.scope.exclusions.some(function (t) { return key(t) === id; })) active.scope.exclusions.push(targetOnly(target));
      }
      markDirty(); renderTargets();
    }
    function save() {
      if (busy || !canEdit || !loaded) return;
      busy = true; writeState(); renderTargets(); feedback("正在保存并应用连接配置…");
      api("", { version: version, connections: connections }).then(function (data) {
        receiveState(data); feedback(data.notice); return loadTargets();
      }).catch(function (error) { feedback(error.message, true); })
        .finally(function () { busy = false; writeState(); renderTargets(); });
    }
    root.addEventListener("input", function (event) {
      var target = event.target, item = current();
      if (target === get("search")) {
        clearTimeout(searchTimer); requestSequence++; page = 1; searchTimer = setTimeout(loadTargets, 220); return;
      }
      if (!item || !canEdit || busy) return;
      if (target === get("name")) { item.name = target.value; get("title").textContent = item.name || "未命名连接"; renderConnections(); markDirty(); }
      if (target === get("address")) { item.api_server_ws_url = target.value; markDirty(); }
      if (target === get("token")) { item.token = target.value; markDirty(); }
    });
    root.addEventListener("change", function (event) {
      var target = event.target, item = current();
      if (target === get("account")) { page = 1; loadTargets(); return; }
      if (!item || !canEdit || busy) return;
      if (target.hasAttribute("data-mb-chat")) { var chat = cache.get(target.dataset.mbChat); if (chat) assign(chat, target.checked); return; }
      if (target === get("enabled")) item.enabled = target.checked;
      else if (target === get("mode")) item.enable_reply_output = target.value === "chat";
      else if (target === get("protocol")) item.protocol = target.value;
      else if (target === get("mention")) item.mention_only = target.checked;
      else if (target === get("clear-token")) item.clear_token = target.checked;
      else if (target.hasAttribute("data-mb-flag")) item[target.dataset.mbFlag] = target.checked;
      else return;
      markDirty(); renderTargets();
    });
    root.addEventListener("click", async function (event) {
      var button = event.target.closest("button");
      if (!button || button.disabled) return;
      if (button.hasAttribute("data-mb-connection")) { currentId = button.dataset.mbConnection; renderEditor(); loadTargets(); return; }
      if (button.hasAttribute("data-mb-type")) { openPicker(button.dataset.mbType); return; }
      if (button.hasAttribute("data-mb-picker-close")) { closePicker(true); return; }
      if (button.hasAttribute("data-mb-prev") || button.hasAttribute("data-mb-next")) { page += button.hasAttribute("data-mb-prev") ? -1 : 1; loadTargets(); return; }
      if (button.hasAttribute("data-mb-refresh")) { reload(); return; }
      if (!canEdit || busy || !loaded) return;
      if (button.hasAttribute("data-mb-save")) { save(); return; }
      if (button.hasAttribute("data-mb-add")) {
        if (connections.length >= 20) { feedback("最多添加 20 条连接。", true); return; }
        var id = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : "connection-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
        connections.push({ id: id, name: "新麦麦 " + (connections.length + 1), enabled: false, api_server_ws_url: "", protocol: "maim", enable_reply_output: true, mention_only: false, scope: { mode: "selected", targets: [], exclusions: [] } });
        currentId = id; markDirty(); renderEditor(); loadTargets(); get("name").focus(); return;
      }
      var item = current(); if (!item) return;
      if (button.hasAttribute("data-mb-transfer") || button.hasAttribute("data-mb-remove")) {
        var target = cache.get(button.dataset.mbTransfer || button.dataset.mbRemove);
        if (target) assign(target, button.hasAttribute("data-mb-transfer")); return;
      }
      if (button.hasAttribute("data-mb-selected-only")) {
        if (!await window.appDialog.confirm({ title: "调整聊天范围？", message: "改为只处理勾选的聊天后，未明确勾选的聊天将停止交给此连接。", confirmText: "调整范围" })) return;
        item.scope = { mode: "selected", targets: item.scope.targets, exclusions: [] }; markDirty(); renderEditor(); return;
      }
      if (button.hasAttribute("data-mb-delete")) {
        if (!await window.appDialog.confirm({ title: "删除「" + item.name + "」？", message: "保存后将断开这条连接，并取消它的聊天分配。", confirmText: "删除连接", tone: "danger" })) return;
        connections = connections.filter(function (c) { return c.id !== currentId; }); currentId = connections[0] ? connections[0].id : "";
        markDirty(); renderEditor(); writeState(); return;
      }
      if (button.hasAttribute("data-mb-restart") && dirty) { feedback("请先保存更改，再重连当前连接。", true); return; }
      var isTest = button.hasAttribute("data-mb-test");
      if (isTest || button.hasAttribute("data-mb-restart")) {
        busy = true; writeState(); renderTargets(); feedback(isTest ? "正在测试 WebSocket 连接…" : "正在重连当前连接…");
        api(isTest ? "/test" : "/restart", isTest ? { id: item.id, api_server_ws_url: item.api_server_ws_url, protocol: item.protocol, token: item.token || "", clear_token: !!item.clear_token } : { connection_id: item.id })
          .then(function (data) { feedback(data.notice); })
          .catch(function (error) { feedback(error.message, true); })
          .finally(function () { busy = false; writeState(); renderTargets(); });
      }
    });
    root.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && pickerOpen && event.target === get("search")) {
        event.preventDefault();
      } else if (event.key === "Escape" && pickerOpen && !event.defaultPrevented) {
        event.preventDefault(); event.stopPropagation(); closePicker(true);
      } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && event.target.hasAttribute("data-mb-type")) {
        event.preventDefault();
        if (pickerOpen && type === event.target.dataset.mbType) get("search").focus({ preventScroll: true });
        else openPicker(event.target.dataset.mbType);
      }
    });
    document.addEventListener("pointerdown", function (event) {
      if (pickerOpen && !event.composedPath().includes(get("picker"))) closePicker(false);
    });
    document.addEventListener("focusin", function (event) {
      if (pickerOpen && !get("picker").contains(event.target)) closePicker(false);
    });
    document.addEventListener("scroll", function (event) {
      if (!pickerOpen || !(event.target instanceof Node) || !event.target.contains(pickerTrigger)) return;
      var rect = pickerTrigger.getBoundingClientRect();
      if (pickerAnchor && (Math.abs(rect.top - pickerAnchor.top) > 1 || Math.abs(rect.left - pickerAnchor.left) > 1)) closePicker(false);
    }, true);
    window.addEventListener("resize", positionPicker);
    if (window.visualViewport) window.visualViewport.addEventListener("resize", positionPicker);
    var form = root.closest("form");
    if (form) form.addEventListener("submit", function (event) { event.preventDefault(); save(); });
    window.addEventListener("beforeunload", function (event) { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
    writeState(); reload();
    var poll = setInterval(function () {
      if (!root.isConnected) { clearInterval(poll); return; }
      if (!loaded || busy || document.hidden) return;
      api("/status").then(function (data) {
        if (data.version !== version) { feedback("配置已在其他页面更新，请重新载入后再保存。", true); return; }
        connections.forEach(function (item) { var saved = data.connections.find(function (c) { return c.id === item.id; }); if (saved) item.runtime = saved.runtime; });
        get("binding-warning").hidden = data.binding_enabled; renderConnections();
      }).catch(function () { /* 下次状态刷新重试，不覆盖正在编辑的草稿。 */ });
    }, 8000);
  });
})();
