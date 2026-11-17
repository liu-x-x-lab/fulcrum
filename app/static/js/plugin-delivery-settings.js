/* 公共撤回区间：关闭时收起数字输入但保留值，群保存/恢复的 change 回填也走同一路径。 */
(function () {
  "use strict";
  document.querySelectorAll("[data-revoke-settings]").forEach(function (section) {
    const enabled = section.querySelector('input[type="checkbox"][name="delivery__revoke_enabled"]');
    const range = section.querySelector("[data-revoke-range]");
    const minimum = section.querySelector('[name="delivery__revoke_min_seconds"]');
    const maximum = section.querySelector('[name="delivery__revoke_max_seconds"]');
    function sync() {
      range.hidden = !enabled.checked;
      // 关闭开关时不让折叠输入阻止提交；服务端仍严格校验并保存整个策略。
      minimum.required = maximum.required = enabled.checked;
      maximum.setCustomValidity(enabled.checked && Number(minimum.value) > Number(maximum.value)
        ? "最长等待不能小于最短等待" : "");
    }
    section.addEventListener("change", sync);
    section.addEventListener("input", sync);
    sync();
  });
})();
