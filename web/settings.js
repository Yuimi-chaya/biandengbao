'use strict';
const ModelSettings = (() => {
  const labels = { none: '无 · none', minimal: '最少 · minimal', low: '低 · low',
    medium: '中 · medium', high: '高 · high', xhigh: '更高 · xhigh',
    max: '最高 · max', ultra: '极高 · ultra' };
  function efforts(model, current, useCurrent = true) {
    const supported = [...new Set(model?.efforts || [])].filter(value => value in labels);
    // Unknown custom providers must supply an explicit value, never a guessed tier.
    if (!supported.length) return { values: Object.keys(labels), value: useCurrent ? current || '' : '' };
    const value = useCurrent && !supported.includes(current) ? ''
      : useCurrent && supported.includes(current) ? current
      : supported.includes(model.defaultEffort) ? model.defaultEffort : supported[0];
    return { values: supported, value };
  }
  function fill(select, model, current, useCurrent = true) {
    const result = efforts(model, current, useCurrent);
    select.replaceChildren(...result.values.map(value => new Option(labels[value] || value, value)));
    if (!result.value) select.prepend(new Option(model?.efforts?.length ? '请选择模型支持的档位' : '请选择自定义档位', ''));
    select.value = result.value;
    return result;
  }
  return { efforts, fill };
})();
if (typeof module !== 'undefined') module.exports = ModelSettings;
