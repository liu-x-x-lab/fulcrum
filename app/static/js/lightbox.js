/* 全站图片/视频查看器。图片以原始像素为缩放基准，适应窗口与 1:1 明确分开；
 * 只改变显示，不生成图片副本。消息、朋友圈和媒体库沿用同一入口。 */
(function () {
  "use strict";

  var root, stage, counter, toolbar, zoomLabel, prevButton, nextButton;
  var returnFocus = null;
  var current = null;
  var drag = null;
  var state = { items: [], index: 0 };
  var view = { scale: 1, fit: 1, x: 0, y: 0, mode: "fit", ready: false };

  function build() {
    root = document.createElement("div");
    root.className = "app-lightbox";
    root.hidden = true;
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", "媒体查看");
    root.innerHTML =
      '<button type="button" class="app-lightbox-close" aria-label="关闭">×</button>' +
      '<button type="button" class="app-lightbox-nav is-prev" aria-label="上一张">‹</button>' +
      '<div class="app-lightbox-stage"></div>' +
      '<button type="button" class="app-lightbox-nav is-next" aria-label="下一张">›</button>' +
      '<div class="app-lightbox-toolbar" role="group" aria-label="图片缩放" hidden>' +
      '<button type="button" data-zoom="out" aria-label="缩小">−</button>' +
      '<output class="app-lightbox-zoom" aria-label="缩放比例" aria-live="polite">100%</output>' +
      '<button type="button" data-zoom="in" aria-label="放大">＋</button>' +
      '<button type="button" data-zoom="actual" aria-label="原始尺寸 1:1">1:1</button>' +
      '<button type="button" data-zoom="fit" aria-label="复原（适应窗口）">复原</button></div>' +
      '<span class="app-lightbox-counter"></span>';
    document.body.appendChild(root);
    stage = root.querySelector(".app-lightbox-stage");
    counter = root.querySelector(".app-lightbox-counter");
    toolbar = root.querySelector(".app-lightbox-toolbar");
    zoomLabel = root.querySelector(".app-lightbox-zoom");
    prevButton = root.querySelector(".is-prev");
    nextButton = root.querySelector(".is-next");
    root.querySelector(".app-lightbox-close").addEventListener("click", close);
    prevButton.addEventListener("click", function () { go(-1); });
    nextButton.addEventListener("click", function () { go(1); });
    toolbar.addEventListener("click", function (event) {
      var button = event.target.closest("[data-zoom]");
      if (button) action(button.dataset.zoom);
    });
    root.addEventListener("click", function (event) {
      if (event.target === root || event.target === stage) close();
    });
    stage.addEventListener("wheel", function (event) {
      if (!view.ready) return;
      event.preventDefault();
      var delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? stage.clientHeight : 1);
      zoom(view.scale * Math.exp(-Math.max(-240, Math.min(240, delta)) * 0.002), event.clientX, event.clientY);
    }, { passive: false });
    stage.addEventListener("pointerdown", function (event) {
      if (!view.ready || event.target !== current || event.button !== 0 || drag) return;
      event.preventDefault();
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, startX: view.x, startY: view.y };
      current.setPointerCapture(event.pointerId);
      stage.classList.add("is-dragging");
    });
    stage.addEventListener("pointermove", function (event) {
      if (!drag || drag.id !== event.pointerId) return;
      view.x = drag.startX + event.clientX - drag.x;
      view.y = drag.startY + event.clientY - drag.y;
      paint();
    });
    ["pointerup", "pointercancel", "lostpointercapture"].forEach(function (name) {
      stage.addEventListener(name, function (event) {
        if (!drag || drag.id !== event.pointerId) return;
        drag = null;
        stage.classList.remove("is-dragging");
        if (current && current.hasPointerCapture(event.pointerId)) current.releasePointerCapture(event.pointerId);
      });
    });
  }

  function fitScale() {
    return Math.min(1, stage.clientWidth / current.naturalWidth, stage.clientHeight / current.naturalHeight);
  }

  function paint() {
    if (!view.ready) return;
    // 每个方向只能拖动超出可视区域的部分，避免把图片拖到屏幕外找不回来。
    var limitX = Math.max(0, (current.naturalWidth * view.scale - stage.clientWidth) / 2);
    var limitY = Math.max(0, (current.naturalHeight * view.scale - stage.clientHeight) / 2);
    view.x = Math.max(-limitX, Math.min(limitX, view.x));
    view.y = Math.max(-limitY, Math.min(limitY, view.y));
    current.style.transform = "translate(-50%, -50%) translate(" + view.x + "px," + view.y + "px) scale(" + view.scale + ")";
    stage.classList.toggle("is-pannable", limitX > 0 || limitY > 0);
    zoomLabel.textContent = Math.round(view.scale * 100) + "%";
    toolbar.querySelector('[data-zoom="out"]').disabled = view.scale <= Math.min(0.1, view.fit) + 0.00001;
    toolbar.querySelector('[data-zoom="in"]').disabled = view.scale >= 8;
  }

  function zoom(scale, clientX, clientY) {
    if (!view.ready) return;
    var next = Math.max(Math.min(0.1, view.fit), Math.min(8, scale));
    var rect = stage.getBoundingClientRect();
    var x = clientX === undefined ? 0 : clientX - rect.left - rect.width / 2;
    var y = clientY === undefined ? 0 : clientY - rect.top - rect.height / 2;
    // 鼠标下的图像像素保持原位；按钮则以当前视口中心缩放。
    view.x = x - (x - view.x) * next / view.scale;
    view.y = y - (y - view.y) * next / view.scale;
    view.scale = next;
    view.mode = "custom";
    paint();
  }

  function action(name) {
    if (!view.ready) return;
    if (name === "in" || name === "out") zoom(view.scale * (name === "in" ? 1.25 : 0.8));
    else {
      view.fit = fitScale();
      view.mode = name;
      view.scale = name === "actual" ? 1 : view.fit;
      view.x = view.y = 0;
      paint();
    }
  }

  function clearMedia() {
    // 显式停止视频，释放媒体资源；旧图片迟到的 load/error 不得覆盖下一项。
    if (current && current.tagName === "VIDEO") {
      current.pause();
      current.removeAttribute("src");
      current.load();
    }
    current = null;
    drag = null;
    view.ready = false;
    stage.classList.remove("is-pannable", "is-dragging");
    stage.replaceChildren();
  }

  function render() {
    clearMedia();
    var item = state.items[state.index];
    var video = item.type === "video";
    var element = document.createElement(video ? "video" : "img");
    current = element;
    toolbar.hidden = video;
    toolbar.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
    zoomLabel.textContent = "—";
    if (video) {
      element.controls = true;
      element.autoplay = true;
      element.playsInline = true;
    } else {
      element.alt = "图片";
      element.draggable = false;
      element.addEventListener("load", function () {
        if (current !== element) return;
        element.style.width = element.naturalWidth + "px";
        element.style.height = element.naturalHeight + "px";
        view.ready = true;
        toolbar.querySelectorAll("button").forEach(function (button) { button.disabled = false; });
        action("fit");
      }, { once: true });
    }
    element.addEventListener("error", function () {
      if (current !== element) return;
      clearMedia();
      toolbar.hidden = true;
      stage.innerHTML = '<p class="app-lightbox-error" role="status">加载失败，请关闭后重试</p>';
    }, { once: true });
    stage.appendChild(element);
    element.src = item.url;
    var many = state.items.length > 1;
    prevButton.hidden = nextButton.hidden = counter.hidden = !many;
    prevButton.disabled = state.index === 0;
    nextButton.disabled = state.index === state.items.length - 1;
    counter.textContent = (state.index + 1) + " / " + state.items.length;
  }

  function go(step) {
    var next = state.index + step;
    if (next < 0 || next >= state.items.length) return;
    state.index = next;
    render();
    // 切换后隐藏或禁用的按钮不能继续持有模态框内的焦点。
    if (root.contains(document.activeElement) && (document.activeElement.disabled || !document.activeElement.getClientRects().length)) {
      root.querySelector(".app-lightbox-close").focus();
    }
  }

  function close() {
    if (!root || root.hidden) return;
    clearMedia();
    root.hidden = true;
    document.documentElement.classList.remove("has-lightbox");
    var target = returnFocus;
    returnFocus = null;
    if (target && target.isConnected) {
      if (!target.getClientRects().length) {
        var disclosure = target.closest("details");
        target = disclosure ? disclosure.querySelector("summary") : null;
      }
      if (target && target.getClientRects().length) target.focus({ preventScroll: true });
    }
  }

  function open(items, index) {
    items = (items || []).filter(function (item) { return item && item.url; });
    if (!items.length) return;
    if (!root) build();
    if (root.hidden) returnFocus = document.activeElement;
    state.items = items;
    state.index = Math.min(Math.max(index || 0, 0), items.length - 1);
    root.hidden = false;
    document.documentElement.classList.add("has-lightbox");
    render();
    root.querySelector(".app-lightbox-close").focus();
  }

  window.addEventListener("resize", function () {
    if (!root || root.hidden || !view.ready) return;
    view.fit = fitScale();
    if (view.mode === "fit") action("fit");
    else paint();
  });
  document.addEventListener("keydown", function (event) {
    if (!root || root.hidden) return;
    if (event.key === "Escape") close();
    else if (event.key === "ArrowLeft") go(-1);
    else if (event.key === "ArrowRight") go(1);
    else if (!event.ctrlKey && !event.metaKey && ["+", "=", "-", "0", "1"].includes(event.key) && view.ready) {
      event.preventDefault();
      action({ "+": "in", "=": "in", "-": "out", "0": "fit", "1": "actual" }[event.key]);
    } else if (event.key === "Tab") {
      var controls = Array.from(root.querySelectorAll("button, video[controls]")).filter(function (control) {
        return !control.disabled && control.getClientRects().length;
      });
      var first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    }
  });
  window.appLightbox = { open: open, close: close };
})();
