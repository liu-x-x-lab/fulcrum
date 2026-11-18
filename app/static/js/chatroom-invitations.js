/* 邀请图谱只展示服务端的明确关系。布局、搜索与放大共用成员 ID 和一份 DOM，不写回邀请证据。 */
(() => {
  const panel = document.querySelector('[data-invitation-graph]');
  if (!panel) return;
  const find = (selector) => panel.querySelector(selector);
  const viewport = find('[data-invitation-viewport]');
  const scene = find('[data-invitation-scene]');
  const svg = find('[data-invitation-svg]');
  const edges = find('[data-invitation-edges]');
  const inspector = find('[data-invitation-inspector]');
  const search = find('[data-invitation-search]');
  const results = find('[data-invitation-results]');
  const searchStatus = find('[data-invitation-search-status]');
  const dialog = find('[data-invitation-dialog]');
  const workspace = find('[data-invitation-workspace]');
  const fullscreen = find('[data-invitation-fullscreen]');
  const memberBrowser = find('[data-invitation-member-browser]');
  const memberFilter = find('[data-invitation-member-filter]');
  const memberGrid = find('[data-invitation-member-grid]');
  const nodes = Array.from(panel.querySelectorAll('[data-invitation-node]'), (element) => ({
    key: element.dataset.invitationNode, parentKey: element.dataset.parent,
    connected: element.dataset.connected === 'true', element, children: [],
    select: element.querySelector('[data-invitation-select]'),
    toggle: element.querySelector('[data-invitation-toggle]'),
    data: element.dataset,
  }));
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const graph = nodes.filter((node) => node.connected);
  graph.forEach((node) => {
    node.parent = byKey.get(node.parentKey);
    if (node.parent) node.parent.children.push(node);
  });
  const roots = graph.filter((node) => !node.parent);
  const byExternalId = new Map(nodes.map((node) => [node.data.externalId, node]));
  nodes.forEach((node) => { node.directMembers = []; });
  // 成员网格使用原始邀请证据，循环关系为展示断边后也不能漏掉被邀请人。
  nodes.forEach((node) => {
    const inviter = byExternalId.get(node.data.inviterId);
    inviter?.directMembers.push(node);
  });
  nodes.forEach((node) => {
    node.directMembers.sort((left, right) => Number(right.data.directInvites) - Number(left.data.directInvites));
  });
  const summaries = new Map();
  graph.filter((node) => node.children.length > 12).forEach((parent) => {
    const element = document.createElement('li');
    element.className = 'invitation-graph-node invitation-group-summary';
    element.dataset.invitationSummary = parent.key;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'invitation-member';
    button.dataset.invitationMembers = parent.key;
    const avatars = document.createElement('span');
    avatars.className = 'invitation-group-avatars';
    avatars.setAttribute('aria-hidden', 'true');
    const title = document.createElement('strong');
    const description = document.createElement('span');
    description.className = 'invitation-member-note';
    const action = document.createElement('span');
    action.className = 'invitation-group-action';
    action.textContent = '查看成员 →';
    button.append(avatars, title, description, action);
    element.append(button);
    parent.element.parentElement.append(element);
    summaries.set(parent.key, {key: `summary-${parent.key}`, parent, element, avatars, title, description, children: []});
  });
  // 首屏展开一层，大分支只保留邀请链骨架，普通成员汇总后按需查看。
  const expanded = new Set(roots.map((node) => node.key));
  const view = {x: 0, y: 0, scale: 1};
  const cardWidth = 248;
  const gap = 32;
  const padding = 24;
  let bounds = {width: 1, height: 1};
  let selectedKey = '';
  let matches = [];
  let autoFit = true;
  let previousSize = {width: viewport.clientWidth, height: viewport.clientHeight};
  let drag = null;
  let focusKey = '';
  let overviewState = null;
  let memberKey = '';
  let memberPage = 0;
  let memberOpener = null;
  const pageSize = 24;

  function applyView() {
    if (!scene) return;
    scene.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
    const percentage = view.scale * 100;
    find('[data-invitation-scale]').textContent = `${percentage < 1 ? percentage.toFixed(1) : Math.round(percentage)}%`;
  }

  function fitScale() {
    return Math.max(0.00001, Math.min(1, (viewport.clientWidth - 32) / bounds.width, (viewport.clientHeight - 32) / bounds.height));
  }

  function fit() {
    if (!scene || !viewport.clientWidth) return;
    autoFit = true;
    // 超宽或超深的图不再被整体缩成细线；保持可阅读尺寸，余下部分由拖动查看。
    view.scale = Math.max(Math.min(0.75, (viewport.clientWidth - 32) / cardWidth), fitScale());
    const focused = byKey.get(focusKey);
    const anchor = focused?.visible ? focused : roots.find((node) => node.visible);
    view.x = bounds.width * view.scale <= viewport.clientWidth
      ? (viewport.clientWidth - bounds.width * view.scale) / 2
      : viewport.clientWidth / 2 - (anchor.x + cardWidth / 2) * view.scale;
    view.y = bounds.height * view.scale <= viewport.clientHeight
      ? (viewport.clientHeight - bounds.height * view.scale) / 2
      : focused?.visible ? viewport.clientHeight / 2 - (focused.y + focused.height / 2) * view.scale : padding;
    applyView();
  }

  function zoom(factor, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) {
    if (!scene) return;
    // 最小缩放随图谱范围确定，展开大群后仍能完整归位；主动定位时会恢复可阅读比例。
    const next = Math.max(Math.min(0.2, fitScale()), Math.min(2, view.scale * factor));
    const ratio = next / view.scale;
    view.x = x - (x - view.x) * ratio;
    view.y = y - (y - view.y) * ratio;
    view.scale = next;
    autoFit = false;
    applyView();
  }

  function highlight() {
    const related = new Set();
    let cursor = byKey.get(selectedKey);
    while (cursor && !related.has(cursor.key)) {
      related.add(cursor.key);
      cursor = cursor.parent;
    }
    const selected = byKey.get(selectedKey);
    selected?.children.forEach((child) => related.add(child.key));
    nodes.forEach((node) => {
      node.element.classList.toggle('is-selected', node.key === selectedKey);
      node.element.classList.toggle('is-related', related.has(node.key));
      node.select.setAttribute('aria-pressed', String(node.key === selectedKey));
    });
    memberGrid.querySelectorAll('[data-invitation-grid-select]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.invitationGridSelect === selectedKey));
    });
    edges?.querySelectorAll('path').forEach((edge) => {
      edge.classList.toggle('is-highlighted', related.has(edge.dataset.from) && related.has(edge.dataset.to));
    });
  }

  function layout() {
    if (!scene) return;
    const levelHeights = [];
    const branchLimit = Math.min(6, Math.max(0, Math.floor((viewport.clientWidth - padding * 2) / (cardWidth + gap)) - 1));
    const focused = byKey.get(focusKey);
    const scope = new Set();
    if (focused) {
      let cursor = focused;
      while (cursor) {
        scope.add(cursor.key);
        cursor = cursor.parent;
      }
      focused.children.forEach((child) => scope.add(child.key));
    }
    graph.forEach((node) => {
      const candidates = node.children.filter((child) => !focused || scope.has(child.key));
      const inviters = candidates.filter((child) => child.children.length);
      node.shownChildren = candidates.length > 12
        ? inviters.slice(0, branchLimit) : candidates;
      const summary = summaries.get(node.key);
      if (summary) {
        summary.omitted = candidates.filter((child) => !node.shownChildren.includes(child));
        summary.title.textContent = `${node.shownChildren.length ? '另有 ' : ''}${summary.omitted.length} 位直属成员`;
        summary.description.textContent = node.shownChildren.length
          ? `已优先展示 ${node.shownChildren.length} 位继续邀请的成员`
          : inviters.length ? `其中 ${inviters.length} 位成员继续邀请，点击查看` : '同一邀请人的成员，集中查看更清楚';
        summary.avatars.replaceChildren(...summary.omitted.slice(0, 4).map((child) =>
          child.element.querySelector('.identity-avatar').cloneNode(true)));
      }
    });
    // 服务端已输出父节点在前的森林，正序确定可见性，逆序计算子树宽度，全程无递归。
    graph.forEach((node) => {
      node.visible = (!focused || scope.has(node.key)) && (!node.parent ||
        node.parent.visible && expanded.has(node.parent.key) && node.parent.shownChildren.includes(node));
      node.element.hidden = !node.visible;
      node.level = node.parent ? node.parent.level + 1 : 0;
      if (node.toggle) {
        const open = expanded.has(node.key);
        node.toggle.setAttribute('aria-expanded', String(open));
        node.toggle.setAttribute('aria-label', `${open ? '收起' : '展开'} ${node.data.name} 的邀请成员`);
        node.toggle.querySelector('[data-invitation-branch-label]').textContent = open ? '收起' : '展开';
      }
    });
    const visible = graph.filter((node) => node.visible);
    summaries.forEach((summary) => {
      summary.visible = summary.parent.visible && expanded.has(summary.parent.key) && summary.omitted.length > 0;
      summary.element.hidden = !summary.visible;
      summary.level = summary.parent.level + 1;
      if (summary.visible) visible.push(summary);
    });
    // 一起读取真实卡片高度后再写坐标，长中文昵称、ID 和来源正常换行，不裁切资料。
    visible.forEach((node) => {
      node.height = node.element.offsetHeight;
      levelHeights[node.level] = Math.max(levelHeights[node.level] || 0, node.height);
      node.visibleChildren = node.children.filter((child) => child.visible);
      const summary = summaries.get(node.key);
      if (summary?.visible) node.visibleChildren.push(summary);
    });
    for (let index = visible.length - 1; index >= 0; index -= 1) {
      const node = visible[index];
      node.span = Math.max(cardWidth, node.visibleChildren.reduce((total, child) => total + child.span + gap, -gap));
    }
    const levelY = [];
    let bottom = padding;
    levelHeights.forEach((height, index) => {
      levelY[index] = bottom;
      bottom += height + 64;
    });
    let left = padding;
    roots.filter((node) => node.visible).forEach((node) => {
      node.start = left;
      left += node.span + gap * 2;
    });
    visible.forEach((node) => {
      node.x = node.start + (node.span - cardWidth) / 2;
      node.y = levelY[node.level];
      node.element.style.left = `${node.x}px`;
      node.element.style.top = `${node.y}px`;
      let childStart = node.start + (node.span - node.visibleChildren.reduce((total, child) => total + child.span + gap, -gap)) / 2;
      node.visibleChildren.forEach((child) => {
        child.start = childStart;
        childStart += child.span + gap;
      });
    });
    bounds = {width: left - gap * 2 + padding, height: bottom - 64 + padding};
    scene.style.width = `${bounds.width}px`;
    scene.style.height = `${bounds.height}px`;
    svg.setAttribute('width', String(bounds.width));
    svg.setAttribute('height', String(bounds.height));
    const fragment = document.createDocumentFragment();
    visible.forEach((node) => {
      if (!node.parent) return;
      const fromX = node.parent.x + cardWidth / 2;
      const fromY = node.parent.y + node.parent.height;
      const toX = node.x + cardWidth / 2;
      const toY = node.y - 5;
      const middleY = (fromY + toY) / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${fromX} ${fromY} C ${fromX} ${middleY}, ${toX} ${middleY}, ${toX} ${toY}`);
      path.setAttribute('marker-end', 'url(#invitation-graph-arrow)');
      path.dataset.from = node.parent.key;
      path.dataset.to = node.key;
      fragment.append(path);
    });
    edges.replaceChildren(fragment);
    highlight();
  }

  function select(node) {
    selectedKey = node.key;
    const fields = {name: node.data.name, id: node.data.externalId,
      status: `${node.data.role} · ${node.data.status}`, inviter: node.data.inviter,
      source: node.data.source || '未知', joined: node.data.joinedAt,
      time: node.data.fetchedAt, 'time-label': node.data.timeLabel,
      description: node.data.description || '暂无入群说明', count: `${node.data.directInvites} 人`};
    Object.entries(fields).forEach(([key, value]) => {
      find(`[data-invitation-detail-${key}]`).textContent = value;
    });
    inspector.hidden = false;
    find('[data-invitation-focus]').disabled = !node.connected;
    find('[data-invitation-detail-members]').disabled = !node.directMembers.length;
    highlight();
  }

  function renderMembers() {
    const parent = byKey.get(memberKey);
    if (!parent) return;
    const query = memberFilter.value.trim().toLowerCase();
    const members = parent.directMembers.filter((node) =>
      `${node.data.name}\n${node.data.externalId}`.toLowerCase().includes(query));
    const pages = Math.max(1, Math.ceil(members.length / pageSize));
    memberPage = Math.min(memberPage, pages - 1);
    const items = members.slice(memberPage * pageSize, (memberPage + 1) * pageSize).map((node) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.invitationGridSelect = node.key;
      button.title = `${node.data.name}（${node.data.externalId}）`;
      button.setAttribute('aria-pressed', String(node.key === selectedKey));
      button.append(node.element.querySelector('.identity-cell').cloneNode(true));
      const id = document.createElement('small');
      id.textContent = node.data.externalId;
      const status = document.createElement('span');
      status.className = 'muted-text';
      status.textContent = `${node.data.status}${Number(node.data.directInvites) ? ` · 邀请 ${node.data.directInvites} 人` : ''}`;
      button.append(id, status);
      item.append(button);
      return item;
    });
    memberGrid.replaceChildren(...items);
    memberGrid.scrollTop = 0;
    find('[data-invitation-member-page]').textContent = query
      ? `匹配 ${members.length} / ${parent.directMembers.length} 人 · 第 ${memberPage + 1} / ${pages} 页`
      : `共 ${members.length} 人 · 第 ${memberPage + 1} / ${pages} 页`;
    find('[data-invitation-member-empty]').hidden = members.length > 0;
    find('[data-invitation-member-prev]').disabled = memberPage === 0;
    find('[data-invitation-member-next]').disabled = memberPage >= pages - 1;
  }

  function openMembers(key) {
    const parent = byKey.get(key);
    if (!parent) return;
    memberOpener = document.activeElement;
    memberKey = key;
    memberPage = 0;
    memberFilter.value = '';
    find('[data-invitation-member-title]').textContent = `${parent.data.name} 直接邀请的成员`;
    find('[data-invitation-member-subtitle]').textContent = `共 ${parent.directMembers.length} 人 · 点击成员查看详情，可继续聚焦邀请关系`;
    memberBrowser.hidden = false;
    renderMembers();
    memberBrowser.scrollIntoView({block: 'nearest'});
    memberFilter.focus({preventScroll: true});
  }

  function focusMember(node) {
    if (!node.connected) return;
    if (!focusKey) overviewState = {expanded: new Set(expanded), view: {...view}, autoFit};
    focusKey = node.key;
    let cursor = node;
    while (cursor) {
      expanded.add(cursor.key);
      cursor = cursor.parent;
    }
    find('[data-invitation-focus-bar]').hidden = false;
    find('[data-invitation-focus-label]').textContent = `正在查看：${node.data.name} 的上级邀请链与直属成员`;
    memberBrowser.hidden = true;
    layout();
    center(node);
    viewport.scrollIntoView({block: 'nearest'});
    node.select.focus({preventScroll: true});
  }

  function returnOverview() {
    if (!focusKey) return;
    focusKey = '';
    find('[data-invitation-focus-bar]').hidden = true;
    memberBrowser.hidden = true;
    expanded.clear();
    overviewState.expanded.forEach((key) => expanded.add(key));
    layout();
    Object.assign(view, overviewState.view);
    autoFit = overviewState.autoFit;
    overviewState = null;
    if (autoFit) fit();
    else applyView();
  }

  function center(node) {
    autoFit = false;
    // 定位恢复可阅读的卡片尺寸，不让“适应整图”产生的小比例妨碍查看目标成员。
    view.scale = Math.max(view.scale, Math.min(1, (viewport.clientWidth - 40) / cardWidth));
    view.x = viewport.clientWidth / 2 - (node.x + cardWidth / 2) * view.scale;
    view.y = viewport.clientHeight / 2 - (node.y + node.height / 2) * view.scale;
    applyView();
  }

  function locate(key) {
    const node = byKey.get(key);
    if (!node) return;
    select(node);
    if (node.connected) {
      focusMember(node);
    } else {
      returnOverview();
      find('[data-invitation-unlinked]').open = true;
      node.element.scrollIntoView({block: 'nearest'});
    }
    results.hidden = true;
    searchStatus.textContent = `已定位：${node.data.name}（${node.data.externalId}）`;
    node.select.focus({preventScroll: true});
  }

  function searchMembers() {
    const query = search.value.trim().toLowerCase();
    results.replaceChildren();
    matches = query ? nodes.filter((node) => `${node.data.name}\n${node.data.externalId}`.toLowerCase().includes(query)) : [];
    matches.slice(0, 20).forEach((node) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.invitationResult = node.key;
      const name = document.createElement('strong');
      name.textContent = node.data.name;
      const id = document.createElement('small');
      id.textContent = node.data.externalId;
      button.append(name, id);
      item.append(button);
      results.append(item);
    });
    results.hidden = !matches.length;
    searchStatus.textContent = query ? (matches.length ? `找到 ${matches.length} 位成员${matches.length > 20 ? '，显示前 20 位，请缩小搜索范围' : '，选择后定位'}` : '没有匹配的成员') : '';
  }

  panel.addEventListener('click', (event) => {
    const target = event.target.closest('button');
    if (!target) return;
    if (target.hasAttribute('data-invitation-members')) return openMembers(target.dataset.invitationMembers);
    if (target.hasAttribute('data-invitation-grid-select')) {
      select(byKey.get(target.dataset.invitationGridSelect));
      inspector.scrollIntoView({block: 'nearest'});
      return;
    }
    if (target.hasAttribute('data-invitation-member-close')) {
      memberBrowser.hidden = true;
      if (memberOpener?.getClientRects().length) memberOpener.focus({preventScroll: true});
      else viewport.focus({preventScroll: true});
      return;
    }
    if (target.hasAttribute('data-invitation-member-prev') || target.hasAttribute('data-invitation-member-next')) {
      memberPage += target.hasAttribute('data-invitation-member-prev') ? -1 : 1;
      renderMembers();
      memberFilter.focus({preventScroll: true});
      return;
    }
    if (target.hasAttribute('data-invitation-focus')) return focusMember(byKey.get(selectedKey));
    if (target.hasAttribute('data-invitation-detail-members')) return openMembers(selectedKey);
    if (target.hasAttribute('data-invitation-overview')) {
      returnOverview();
      viewport.focus({preventScroll: true});
      return;
    }
    if (target.hasAttribute('data-invitation-result')) return locate(target.dataset.invitationResult);
    if (target.hasAttribute('data-invitation-select')) return select(byKey.get(target.closest('[data-invitation-node]').dataset.invitationNode));
    if (target.hasAttribute('data-invitation-toggle')) {
      const node = byKey.get(target.closest('[data-invitation-node]').dataset.invitationNode);
      const anchor = {x: view.x + (node.x + cardWidth / 2) * view.scale, y: view.y + node.y * view.scale};
      if (expanded.has(node.key)) expanded.delete(node.key);
      else expanded.add(node.key);
      layout();
      // 在原位置展开分支，避免父卡片突然跳离正在操作的指针或键盘焦点。
      view.x = anchor.x - (node.x + cardWidth / 2) * view.scale;
      view.y = anchor.y - node.y * view.scale;
      autoFit = false;
      return applyView();
    }
    if (target.hasAttribute('data-invitation-zoom-in')) return zoom(1.25);
    if (target.hasAttribute('data-invitation-zoom-out')) return zoom(0.8);
    if (target.hasAttribute('data-invitation-fit')) return fit();
    if (target.hasAttribute('data-invitation-expand') || target.hasAttribute('data-invitation-collapse')) {
      expanded.clear();
      if (target.hasAttribute('data-invitation-expand')) graph.forEach((node) => expanded.add(node.key));
      layout();
      return fit();
    }
    if (target.hasAttribute('data-invitation-clear-selection')) {
      const selected = byKey.get(selectedKey);
      selectedKey = '';
      inspector.hidden = true;
      highlight();
      if (selected?.select.getClientRects().length) selected.select.focus({preventScroll: true});
      else viewport.focus({preventScroll: true});
    }
    if (target.hasAttribute('data-invitation-fullscreen')) {
      // 原生 dialog 继续使用共享关闭/遮罩处理；工作区仅移动，始终保留唯一事件与选择状态。
      results.hidden = true;
      find('[data-invitation-dialog-slot]').append(workspace);
      fullscreen.hidden = true;
      dialog.showModal();
      viewport.focus({preventScroll: true});
    }
  });

  memberFilter.addEventListener('input', () => {
    memberPage = 0;
    renderMembers();
  });
  search.addEventListener('input', searchMembers);
  search.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && matches.length) {
      event.preventDefault();
      locate(matches[0].key);
    } else if (event.key === 'ArrowDown' && !results.hidden) {
      event.preventDefault();
      results.querySelector('button').focus();
    } else if (event.key === 'Escape' && !results.hidden) {
      event.preventDefault();
      results.hidden = true;
    }
  });
  results.addEventListener('keydown', (event) => {
    const buttons = Array.from(results.querySelectorAll('button'));
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length]?.focus();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      results.hidden = true;
      search.focus();
    }
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.invitation-search')) results.hidden = true;
  });

  viewport.addEventListener('pointerdown', (event) => {
    if (!scene || event.button !== 0 || event.target.closest('button')) return;
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY, originX: view.x, originY: view.y};
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add('is-dragging');
    viewport.focus({preventScroll: true});
  });
  viewport.addEventListener('pointermove', (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    autoFit = false;
    view.x = drag.originX + event.clientX - drag.x;
    view.y = drag.originY + event.clientY - drag.y;
    applyView();
  });
  const endDrag = () => {
    drag = null;
    viewport.classList.remove('is-dragging');
  };
  viewport.addEventListener('pointerup', endDrag);
  viewport.addEventListener('pointercancel', endDrag);
  viewport.addEventListener('lostpointercapture', endDrag);
  viewport.addEventListener('wheel', (event) => {
    // 画布内直接用纵向滚轮缩放；空图谱和纯横向输入不拦截页面原有滚动。
    if (!scene || event.deltaY === 0) return;
    event.preventDefault();
    // 鼠标可能按行或页上报，触控板通常按像素上报；统一单位并限制单次幅度，
    // 让上滚放大、下滚缩小保持平滑，避免一次滚动跨越过大的缩放范围。
    const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
      : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? viewport.clientHeight : 1;
    const delta = Math.max(-120, Math.min(120, event.deltaY * unit));
    const rect = viewport.getBoundingClientRect();
    // 扣除画布边框，以鼠标所指的图谱位置为锚点；弹窗继续复用同一工作区和视图。
    zoom(Math.exp(-delta * 0.002), event.clientX - rect.left - viewport.clientLeft,
      event.clientY - rect.top - viewport.clientTop);
  }, {passive: false});
  viewport.addEventListener('keydown', (event) => {
    if (event.target !== viewport || !scene) return;
    const directions = {ArrowLeft: [48, 0], ArrowRight: [-48, 0], ArrowUp: [0, 48], ArrowDown: [0, -48]};
    if (directions[event.key]) {
      event.preventDefault();
      autoFit = false;
      view.x += directions[event.key][0];
      view.y += directions[event.key][1];
      applyView();
    } else if (['+', '=', '-', '0', 'Home'].includes(event.key)) {
      event.preventDefault();
      if (event.key === '0' || event.key === 'Home') fit();
      else zoom(event.key === '-' ? 0.8 : 1.25);
    }
  });
  viewport.addEventListener('focusin', (event) => {
    const node = byKey.get(event.target.closest('[data-invitation-node]')?.dataset.invitationNode);
    if (!node || !node.visible) return;
    const x = view.x + node.x * view.scale;
    const y = view.y + node.y * view.scale;
    if (x < 0 || y < 0 || x + cardWidth * view.scale > viewport.clientWidth || y + node.height * view.scale > viewport.clientHeight) center(node);
    // 聚焦画布外的卡片时由视图平移接管，不允许浏览器再累积一个隐藏的滚动偏移。
    viewport.scrollLeft = 0;
    viewport.scrollTop = 0;
  });

  function resizeView() {
    const size = {width: viewport.clientWidth, height: viewport.clientHeight};
    if (!size.width || !size.height) return;
    if (size.width !== previousSize.width) layout();
    if (autoFit) fit();
    else {
      view.x += (size.width - previousSize.width) / 2;
      view.y += (size.height - previousSize.height) / 2;
      applyView();
    }
    previousSize = size;
  }
  dialog.addEventListener('close', () => {
    find('[data-invitation-inline-slot]').append(workspace);
    fullscreen.hidden = false;
    // 共享关闭逻辑执行后再返回可见入口，不能把焦点留在已关闭 dialog 的内部。
    requestAnimationFrame(() => {
      resizeView();
      fullscreen.focus({preventScroll: true});
    });
  });

  panel.classList.add('is-ready');
  search.disabled = !nodes.length;
  panel.querySelectorAll('.invitation-controls button').forEach((button) => { button.disabled = !graph.length; });
  layout();
  fit();
  new ResizeObserver(resizeView).observe(viewport);
  document.fonts.ready.then(() => {
    layout();
    if (autoFit) fit();
    else applyView();
  });
})();

/* 邀请同步只操作已有进度区域。手机任务由服务端续排，关闭页面不影响执行；
 * 新关系由“更新图谱”明确载入，避免同步完成时突然打断正在拖动或查看的成员。 */
(() => {
  const panel = document.querySelector('[data-invitation-graph][data-invitation-sync-url]');
  if (!panel) return;
  const find = (selector) => panel.querySelector(selector);
  const form = find('[data-invitation-sync-form]');
  const start = find('[data-invitation-sync-start]');
  const box = find('[data-invitation-sync]');
  const summary = find('[data-invitation-sync-summary]');
  const counts = find('[data-invitation-sync-counts]');
  const progress = find('[data-invitation-sync-progress]');
  const refresh = find('[data-invitation-sync-refresh]');
  const url = panel.dataset.invitationSyncUrl;
  let run = panel.dataset.syncRun;
  let baseline = Number(panel.dataset.syncCompleted);
  let running = panel.dataset.syncStatus === 'running';
  let timer;
  let stopped = false;
  let controller;

  function render(data) {
    if (data.run_id !== run) {
      run = data.run_id;
      baseline = 0;
    }
    running = data.status === 'running';
    // 使用 textContent 接收查询结果，昵称、说明和错误文本都不能进入 HTML 执行上下文。
    summary.textContent = data.summary;
    counts.textContent = `本轮查到邀请人 ${data.known} 人 · 未知 ${data.unknown} 人 · 失败 ${data.failed} 人`;
    counts.hidden = data.status === 'idle';
    progress.max = Math.max(1, data.total);
    progress.value = data.completed;
    progress.hidden = !running;
    refresh.hidden = data.completed <= baseline;
    if (start) {
      start.disabled = running || box.dataset.unavailable === 'true';
      start.textContent = running ? '正在同步…' : '重新同步邀请关系';
    }
    if (!running && !refresh.hidden) summary.textContent += '，点击“更新图谱”查看新资料';
  }

  async function request(options = {}) {
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store',
        ...options, headers: {Accept: 'application/json'}, signal: controller.signal});
      if (response.redirected) throw new Error('登录状态已过期，请刷新页面后重试');
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('暂时无法读取同步进度');
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.summary || data.detail || '邀请资料同步失败');
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  function schedule(delay = 2000) {
    clearTimeout(timer);
    if (running && !stopped) timer = setTimeout(poll, delay);
  }

  async function poll() {
    try {
      render(await request());
      schedule();
    } catch (error) {
      if (stopped) return;
      summary.textContent = `${error.message}；5 秒后重试，后台同步会继续`;
      schedule(5000);
    }
  }

  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (running || start.disabled) return;
    start.disabled = true;
    summary.textContent = '正在提交邀请资料同步…';
    try {
      render(await request({method: 'POST', body: new FormData(form)}));
      schedule();
    } catch (error) {
      summary.textContent = error.message;
      start.disabled = false;
    }
  });
  refresh.addEventListener('click', () => window.location.reload());
  window.addEventListener('pagehide', () => {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
  });
  // 从放大弹窗返回或浏览器恢复页面时无需重新开始同步，继续读取同一服务端轮次即可。
  window.addEventListener('pageshow', () => { stopped = false; schedule(); });
  schedule();
})();
