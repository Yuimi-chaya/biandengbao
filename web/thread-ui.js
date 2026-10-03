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
    const latest = view.turns?.at(-1);
    return Boolean(view.compactionPending || view.connected && view.status === 'active' && latest?.status === 'inProgress'
      && latest.messages?.some(message => message.kind === 'contextCompaction' && message.status === 'inProgress'));
  }
  function compactionRecords(view) {
    const visible = new Map();
    let run = [];
    function flush(pending = false) {
      if (run.length && !pending) {
        const last = run.at(-1);
        if (!visible.has(last.turn)) visible.set(last.turn, new Map());
        visible.get(last.turn).set(last.index, run.length);
      }
      run = [];
    }
    for (const turn of view.turns || []) {
      for (const [index, message] of (turn.messages || []).entries()) {
        if (message.kind === 'contextCompaction') run.push({ turn: turn.id, index });
        else flush();
      }
    }
    // The live indicator owns the trailing operation while it is pending.
    flush(isCompacting(view));
    return visible;
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
  function parseValue(value) {
    if (typeof value !== 'string') return value;
    try { return JSON.parse(value); } catch { return value; }
  }
  function readable(value, depth = 0) {
    if (value == null) return '';
    if (typeof value !== 'object') return String(value);
    if (depth > 3) return '';
    if (Array.isArray(value)) {
      const text = value.slice(0, 40).map(item => readable(item, depth + 1)).filter(Boolean).join('\n');
      return text + (value.length > 40 ? '\n其余 ' + (value.length - 40) + ' 项已省略' : '');
    }
    if (typeof value.text === 'string') return value.text;
    const lines = [];
    for (const [key, item] of Object.entries(value).slice(0, 40)) {
      if (/^(type|id|call_id|input_schema|schema|encrypted_content|password|api[_-]?key|authorization|cookie|token)$/i.test(key)) continue;
      const text = readable(item, depth + 1);
      if (text) lines.push(typeof item === 'object' ? text : key + ': ' + text);
    }
    if (Object.keys(value).length > 40) lines.push('其余字段已省略');
    return lines.join('\n');
  }
  function isPatch(text) {
    return typeof text === 'string' && (/^\*\*\* Begin Patch/m.test(text) || /^@@[ @+-]/m.test(text)
      || /^diff --git /m.test(text) || /^--- .+\n\+\+\+ /m.test(text));
  }
  function activityIcon(message, name = '') {
    if (message.kind === 'mcpToolCall' || /(^mcp[_:.]|(?:^|[._])(?:read|list)_mcp_)/i.test(name)) return 'PlugZap';
    if (message.kind === 'skill' || /(?:^|[._])(?:use_skill|load_skill|skill)$/.test(name)) return 'Sparkles';
    if (/(?:^|[._])(?:exec_command|write_stdin|shell|run_command)$/.test(name)) return 'Terminal';
    if (/(?:^|[._])(?:apply_patch|edit_file|write_file)$/.test(name)) return 'FileDiff';
    return ({ fileChange: 'FileDiff', contextCompaction: 'Archive', commandExecution: 'Terminal',
      webSearch: 'Search', imageView: 'Image', collabToolCall: 'Users' })[message.kind] || 'Wrench';
  }
  function filePresentation(change) {
    const kind = change.kind?.type ?? change.kind;
    const created = kind === 'add' || kind === 'create';
    const removed = kind === 'delete' || kind === 'remove';
    const original = String(change.diff ?? '');
    const unified = /^(?:diff --git |--- \/dev\/null|@@ -0,0 \+)/m.test(original);
    const lines = original.split('\n');
    if (original.endsWith('\n')) lines.pop();
    const diff = created && original && !unified ? lines.map(line => '+' + line).join('\n') : original;
    return { path: String(change.path || ''), label: created ? '创建文件' : removed ? '删除文件' : '修改文件',
      icon: created ? 'FilePlus2' : removed ? 'FileMinus2' : 'FileDiff', diff, created,
      action: created ? 'add' : removed ? 'remove' : 'update' };
  }
  function activityPresentation(message, depth = 0) {
    if (depth > 3) return { title: '工具调用', call: '嵌套调用摘要已省略', output: '', diff: '' };
    const raw = parseValue(message.text || ''), envelope = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    const args = parseValue(envelope.arguments ?? envelope.parameters ?? envelope.input ?? envelope.args ?? raw);
    const name = envelope.name || envelope.tool || envelope.toolName || message.title || message.kind || '工具';
    let title = ({ commandExecution: '执行命令', fileChange: '文件变更', webSearch: '搜索网页',
      imageView: '查看图片', collabToolCall: '协作任务' })[message.kind] || name;
    const files = message.kind === 'fileChange' && Array.isArray(message.changes) ? message.changes.map(filePresentation) : [];
    if (files.length && files.every(file => file.created)) title = '创建文件';
    const patch = message.kind === 'fileChange' ? String(message.text || '')
      : [args, args?.patch, args?.input].find(isPatch);
    if (!files.length && patch && /^\*\*\* Add File:/m.test(patch) && !/^\*\*\* (Update|Delete) File:/m.test(patch)) title = '创建文件';
    let call = '';
    if (files.length) call = title + ' · ' + files.length + ' 个';
    else if (patch) call = title === '创建文件' ? '创建文件' : message.kind === 'fileChange' ? '修改文件' : '应用补丁';
    else if (message.kind === 'commandExecution') call = readable(raw);
    else if (typeof args === 'string' && /^\s*[{[]/.test(args)) call = '正在接收调用信息…';
    else if (args && typeof args === 'object') {
      if (Array.isArray(args.tool_uses)) {
        call = args.tool_uses.slice(0, 40).map(tool => activityPresentation({
          kind: 'dynamicToolCall', title: tool.recipient_name, text: tool.parameters
        }, depth + 1).call).join('\n\n');
      } else {
        const command = args.cmd ?? args.command;
        const cwd = args.workdir ?? args.cwd;
        const query = args.query ?? args.q ?? args.search_query;
        const path = args.path ?? args.file_path ?? args.filename;
        if (command) call = (cwd ? '目录：' + cwd + '\n' : '') + readable(command);
        else if (query) call = '搜索：' + readable(query) + (path ? '\n范围：' + path : '');
        else if (path) call = '文件：' + path + (args.pattern ? '\n匹配：' + args.pattern : '');
        else if (envelope.output != null && !envelope.arguments && !envelope.input && !envelope.parameters) call = '调用结果';
        else call = readable(args);
      }
    } else call = readable(args);
    const result = toolResult(message.output ?? envelope.output ?? envelope.result ?? '');
    const output = result.text;
    const toolIcon = activityIcon(message, name);
    const command = message.kind === 'commandExecution' ? readable(raw) : readable(args?.cmd ?? args?.command);
    if (toolIcon === 'Terminal') title = '执行命令';
    const metadata = [];
    const cwd = message.cwd ?? args?.workdir ?? args?.cwd;
    if (cwd) metadata.push({ label: '目录', value: String(cwd) });
    if (message.kind === 'mcpToolCall' || toolIcon === 'PlugZap') metadata.push({ label: 'MCP', value: String(name) });
    else if (name && !['commandExecution','fileChange','执行命令','文件变更'].includes(name)) metadata.push({ label: '工具', value: String(name) });
    const durationMs = message.durationMs ?? result.durationMs;
    if (Number.isFinite(durationMs) && durationMs >= 0) metadata.push({ label: '耗时', value: durationText(durationMs) });
    return { title: String(title), call: call || '调用 ' + name, output, diff: files.length ? '' : patch || '',
      command, metadata, exitCode: message.exitCode ?? result.exitCode, failed: result.failed || message.status === 'failed',
      files, icon: toolIcon };
  }
  function toolResult(value, depth = 0) {
    const raw = parseValue(value);
    if (depth > 5) return { text: '嵌套结果过深，已省略' };
    if (raw == null) return { text: '' };
    if (typeof raw !== 'object') return { text: String(raw) };
    if (raw.type === 'image' || raw.type === 'audio') return { text: raw.type === 'image' ? '[图片结果]' : '[音频结果]' };
    if (raw.type === 'resource_link') return { text: [raw.title || raw.name, raw.uri].filter(Boolean).join(' · ') };
    if (raw.type === 'resource') return { text: raw.resource?.text || raw.resource?.uri || '[资源结果]' };
    if (Array.isArray(raw)) {
      const results = raw.slice(0, 80).map(item => toolResult(item, depth + 1));
      return { text: results.map(result => result.text).filter(Boolean).join('\n\n')
        + (raw.length > 80 ? '\n其余结果已省略' : ''),
        exitCode: results.find(result => result.exitCode != null)?.exitCode,
        durationMs: results.find(result => result.durationMs != null)?.durationMs,
        failed: results.some(result => result.failed) };
    }
    const nested = raw.text ?? raw.output ?? raw.content ?? raw.result;
    const result = nested !== undefined ? toolResult(nested, depth + 1) : { text: readable(raw) };
    return { ...result, exitCode: raw.exit_code ?? raw.exitCode ?? result.exitCode,
      durationMs: Number.isFinite(raw.wall_time_seconds) ? raw.wall_time_seconds * 1000 : raw.durationMs ?? result.durationMs,
      failed: Boolean(raw.isError || raw.error || result.failed) };
  }
  function diffLineKind(line) {
    if (/^(diff --git |index |--- |\+\+\+ |\*\*\* (Begin|End|Update|Add|Delete|Move)|\\ No newline)/.test(line)) return 'meta';
    if (line.startsWith('@@')) return 'hunk';
    if (line.startsWith('+')) return 'add';
    if (line.startsWith('-')) return 'remove';
    return 'context';
  }
  function inputMaxHeight(shellHeight, chromeHeight) {
    return Math.max(40, Math.min(150, shellHeight * .20, shellHeight - chromeHeight - 72));
  }
  return { reasoningParts, durationText, completedText, isCompacting, compactionRecords, finalMessage, completedTurn, revealStep,
    settledPrefix, activityPresentation, activityIcon, filePresentation, diffLineKind, inputMaxHeight, toolResult };
})();
