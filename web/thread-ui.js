'use strict';
const ThreadUI = (() => {
  function reasoningParts(message) {
    const summary = String(message.summary ?? message.text ?? '').trim();
    const lines = summary.split(/\n/);
    const headline = (lines.find(line => line.trim()) || '').replace(/^\s*#+\s*|\*\*|__/g, '').trim().replace(/^[*_](.*)[*_]$/, '$1');
    const remainder = lines.slice(lines.findIndex(line => line.trim()) + 1).join('\n').trim();
    return { headline, detail: String(message.detail || remainder).trim() };
  }
  function durationText(ms) {
    if (!Number.isFinite(ms) || ms < 0) return '耗时未知';
    const seconds = Math.round(ms / 1000);
    if (seconds < 60) return seconds + ' 秒';
    const minutes = Math.floor(seconds / 60);
    return minutes < 60 ? minutes + ' 分 ' + seconds % 60 + ' 秒'
      : Math.floor(minutes / 60) + ' 时 ' + minutes % 60 + ' 分';
  }
  function completedText(turn) {
    const date = Number.isFinite(turn.completedAt) ? new Date(turn.completedAt) : null;
    return durationText(turn.durationMs) + ' · ' + (date && !Number.isNaN(date.getTime())
      ? date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '完成时间未知');
  }
  function isCompacting(view) {
    return Boolean(view.compactionPending || view.connected && view.status === 'active' && view.turns?.some(turn => turn.status === 'inProgress'
      && turn.messages?.some(message => message.kind === 'contextCompaction' && message.status !== 'completed')));
  }
  function finalMessage(turn) {
    const replies = turn.messages.filter(message => message.role === 'assistant' && message.text?.trim());
    return [...replies].reverse().find(message => message.phase === 'final_answer') || replies.at(-1);
  }
  function completedTurn(turn, view) {
    return turn.status === 'completed' || turn.status === 'interrupted'
      || view.turns.at(-1)?.id !== turn.id && turn.status !== 'process';
  }
  function revealStep(current, target, elapsed, rate) {
    let end = Math.min(target.length, current + Math.max(0, Math.floor(elapsed * rate / 1000)));
    if (end < target.length && end > 0 && /[\uD800-\uDBFF]/.test(target[end - 1])) end--;
    return end;
  }
  function settledPrefix(text) {
    let fence = null, offset = 0, boundary = 0;
    for (const line of text.split(/(?<=\n)/)) {
      const marker = /^\s{0,3}(`{3,}|~{3,})/.exec(line)?.[1];
      if (marker && !fence) fence = marker;
      else if (marker && marker[0] === fence?.[0] && marker.length >= fence.length) fence = null;
      offset += line.length;
      if (!fence && /^\s*\n$/.test(line)) boundary = offset;
    }
    return boundary;
  }
  return { reasoningParts, durationText, completedText, isCompacting, finalMessage, completedTurn, revealStep, settledPrefix };
})();
