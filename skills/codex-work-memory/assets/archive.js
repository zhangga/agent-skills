(function () {
  'use strict';

  const normalise = value => String(value || '').normalize('NFKC').toLocaleLowerCase();
  const words = query => normalise(query).trim().split(/\s+/).filter(Boolean);
  function validDay(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
    const date = new Date(value + 'T00:00:00Z');
    return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
  }
  function dateError(filters) {
    if ((filters.from && !validDay(filters.from)) || (filters.to && !validDay(filters.to))) return '请输入有效日期。';
    if (filters.from && filters.to && filters.from > filters.to) return '开始日期不能晚于结束日期。';
    return '';
  }
  function matchesRevision(revision, filters) {
    return (!filters.from || revision.day >= filters.from) &&
      (!filters.to || revision.day <= filters.to) &&
      (!filters.status || revision.status === filters.status) &&
      words(filters.query).every(word => normalise(revision.search).includes(word));
  }
  function selectItems(items, filters) {
    if (dateError(filters)) return [];
    return items.flatMap(item => {
      if (filters.project && item.project !== filters.project) return [];
      const matches = item.revisions.map((revision, index) => ({ revision, index }))
        .filter(entry => matchesRevision(entry.revision, filters));
      if (!matches.length) return [];
      // Revisions are stored newest first; display the latest matching revision.
      return [{ item, index: matches[0].index, revision: matches[0].revision, matches: matches.length }];
    }).sort((a, b) => {
      if (filters.sort === 'project') {
        const byProject = a.item.projectName.localeCompare(b.item.projectName, 'zh-CN');
        if (byProject) return byProject;
      }
      return (filters.sort === 'oldest' ? a.revision.stamp - b.revision.stamp : b.revision.stamp - a.revision.stamp) || a.item.id.localeCompare(b.item.id);
    });
  }
  function presetBounds(preset, today) {
    if (!validDay(today) || preset === 'all') return { from: '', to: '' };
    const end = new Date(today + 'T00:00:00Z');
    const start = new Date(end);
    if (preset === '7') start.setUTCDate(start.getUTCDate() - 6);
    else if (preset === '30') start.setUTCDate(start.getUTCDate() - 29);
    else if (preset === 'week') start.setUTCDate(start.getUTCDate() - (start.getUTCDay() + 6) % 7);
    else return { from: '', to: '' };
    return { from: start.toISOString().slice(0, 10), to: today };
  }
  const helpers = { normalise, validDay, dateError, matchesRevision, selectItems, presetBounds };
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
  if (typeof document === 'undefined') return;

  const data = JSON.parse(document.getElementById('archive-data').textContent);
  const controls = Object.fromEntries(['search', 'project', 'from', 'to', 'status', 'sort'].map(id => [id, document.getElementById(id)]));
  const cards = document.getElementById('cards');
  const entries = new Map(data.items.map(item => [item.id, document.getElementById(item.id).closest('.card')]));
  let activePreset = 'all';
  let currentResults = [];
  const readFilters = () => ({ query: controls.search.value.trim(), project: controls.project.value,
    from: controls.from.value, to: controls.to.value, status: controls.status.value, sort: controls.sort.value });
  const statusClass = status => status === '受阻' ? 'blocked' : ['进行中', '已修改'].includes(status) ? 'progress' : ['已完成', '已发布'].includes(status) ? 'complete' : 'neutral';

  function highlight(root, query) {
    root.querySelectorAll('mark').forEach(mark => mark.replaceWith(document.createTextNode(mark.textContent)));
    root.normalize();
    const terms = query.trim().split(/\s+/).filter(Boolean).sort((a, b) => b.length - a.length);
    if (!terms.length) return;
    const pattern = new RegExp(terms.map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      if (!walker.currentNode.parentElement.closest('button, code, script, template')) nodes.push(walker.currentNode);
    }
    for (const node of nodes) {
      const text = node.nodeValue;
      pattern.lastIndex = 0;
      const found = [...text.matchAll(pattern)];
      if (!found.length) continue;
      const fragment = document.createDocumentFragment();
      let offset = 0;
      for (const match of found) {
        fragment.append(document.createTextNode(text.slice(offset, match.index)));
        const mark = document.createElement('mark');
        mark.textContent = match[0];
        fragment.append(mark);
        offset = match.index + match[0].length;
      }
      fragment.append(document.createTextNode(text.slice(offset)));
      node.replaceWith(fragment);
    }
  }

  function drawRevision(result, query) {
    const { item, revision, index } = result;
    const card = entries.get(item.id);
    const latest = item.revisions[0];
    card.querySelector('h3').textContent = revision.title;
    card.querySelector('.category-label').textContent = revision.category;
    card.querySelector('time').textContent = revision.time;
    card.querySelector('time').dateTime = revision.occurredAt;
    const status = card.querySelector('.status');
    status.textContent = revision.status;
    status.className = 'status ' + statusClass(revision.status);
    card.querySelector('.preview').textContent = revision.summary;
    card.querySelector('.revision-count').textContent = item.revisions.length + ' 条进展';
    const next = card.querySelector('.next');
    next.hidden = !revision.next;
    next.querySelector('span').textContent = revision.next || '';
    const note = card.querySelector('.match-note');
    note.hidden = index === 0;
    note.textContent = index > 0 ? `命中历史记录 · ${revision.day}；当前最新：${latest.status} · ${latest.day}` : '';
    const excerpt = card.querySelector('.search-excerpt');
    const terms = words(query);
    const overview = normalise([revision.title, revision.summary, revision.next].join(' '));
    const detailTerm = terms.find(term => !overview.includes(term));
    const position = detailTerm ? normalise(revision.search).indexOf(detailTerm) : -1;
    excerpt.hidden = position < 0;
    excerpt.textContent = position >= 0 ? '匹配内容：…' + revision.search.slice(Math.max(0, position - 24), position + 100).replace(/\s+/g, ' ') + '…' : '';
    if (card.dataset.selected !== String(index)) {
      const selected = card.querySelector('.selected-revision');
      selected.replaceChildren(document.getElementById(revision.template).content.cloneNode(true));
      const history = card.querySelector('.history-list');
      history.replaceChildren();
      item.revisions.forEach((old, oldIndex) => {
        if (oldIndex === index) return;
        const detail = document.createElement('details');
        detail.className = 'history-entry';
        const heading = document.createElement('summary');
        heading.textContent = `${old.time} · ${old.status}${oldIndex === 0 ? ' · 当前最新' : ''}`;
        detail.append(heading, document.getElementById(old.template).content.cloneNode(true));
        history.append(detail);
      });
      card.querySelector('.history-heading').textContent = (index > 0 ? '其他进展（含当前最新）' : '历史进展') + ' · ' + (item.revisions.length - 1) + ' 条';
      card.dataset.selected = String(index);
    }
    highlight(card, query);
    return card;
  }

  function renderChips(filters) {
    const target = document.getElementById('active-filters');
    target.replaceChildren();
    const pairs = [
      ['project', filters.project && controls.project.selectedOptions[0].textContent],
      ['search', filters.query && '关键词：' + filters.query],
      ['date', (filters.from || filters.to) && `${filters.from || '最早'} 至 ${filters.to || '最新'}`],
      ['status', filters.status && '记录状态：' + filters.status]
    ];
    for (const [key, label] of pairs) if (label) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'filter-chip';
      chip.textContent = label + ' ×';
      chip.setAttribute('aria-label', '清除' + label);
      chip.onclick = () => {
        if (key === 'date') { controls.from.value = ''; controls.to.value = ''; activePreset = 'all'; }
        else controls[key].value = '';
        filter();
      };
      target.append(chip);
    }
  }

  function persist(filters) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value && !(key === 'sort' && value === 'newest')) params.set(key, value);
    const fragment = params.toString() ? '#?' + params.toString() : '';
    try { window.history.replaceState(null, '', location.pathname + location.search + fragment); } catch (_) { /* Some file viewers disallow history updates. */ }
  }

  function filter() {
    const filters = readFilters();
    const error = dateError(filters);
    document.getElementById('date-error').textContent = error;
    controls.from.setAttribute('aria-invalid', String(Boolean(error)));
    controls.to.setAttribute('aria-invalid', String(Boolean(error)));
    currentResults = selectItems(data.items, filters);
    for (const card of entries.values()) card.hidden = true;
    const fragment = document.createDocumentFragment();
    for (const result of currentResults) {
      const card = drawRevision(result, filters.query);
      card.hidden = false;
      fragment.append(card);
    }
    cards.append(fragment);
    const matches = currentResults.reduce((sum, result) => sum + result.matches, 0);
    document.getElementById('count').textContent = error ? '请调整日期范围' : `${currentResults.length} 个事项 · ${matches} 条匹配进展`;
    document.getElementById('results-title').textContent = filters.project ? controls.project.selectedOptions[0].textContent : '全部记录';
    document.getElementById('empty').hidden = currentResults.length > 0 || Boolean(error);
    document.getElementById('expand').disabled = currentResults.length === 0;
    document.getElementById('print').disabled = currentResults.length === 0;
    document.querySelectorAll('.project-link').forEach(button => {
      const active = button.dataset.project === filters.project;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('.range-chip').forEach(button => {
      const active = button.dataset.range === activePreset;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    renderChips(filters);
    persist(filters);
  }

  document.getElementById('filters').addEventListener('submit', event => event.preventDefault());
  controls.search.addEventListener('input', filter);
  for (const [key, control] of Object.entries(controls)) if (key !== 'search') control.addEventListener('change', () => {
    if (key === 'from' || key === 'to') activePreset = controls.from.value || controls.to.value ? 'custom' : 'all';
    filter();
  });
  document.querySelectorAll('.project-link').forEach(button => button.addEventListener('click', () => { controls.project.value = button.dataset.project; filter(); }));
  document.querySelectorAll('.range-chip').forEach(button => button.addEventListener('click', () => {
    const today = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
    const range = presetBounds(button.dataset.range, today);
    controls.from.value = range.from; controls.to.value = range.to;
    activePreset = button.dataset.range;
    filter();
  }));
  function reset() {
    // The reset button's id participates in form named-property lookup.
    HTMLFormElement.prototype.reset.call(document.getElementById('filters'));
    activePreset = 'all'; filter(); controls.search.focus();
  }
  document.getElementById('reset').onclick = reset;
  document.getElementById('empty-reset').onclick = reset;
  document.getElementById('expand').onclick = () => currentResults.forEach(result => entries.get(result.item.id).querySelector('.item').open = true);
  document.getElementById('collapse').onclick = () => document.querySelectorAll('.card details').forEach(detail => detail.open = false);

  let toastTimer;
  document.addEventListener('click', async event => {
    const button = event.target.closest('.copy-path');
    if (!button) return;
    const value = button.dataset.copy;
    let copied = false;
    try { await navigator.clipboard.writeText(value); copied = true; } catch (_) {
      const input = document.createElement('textarea'); input.value = value; input.style.position = 'fixed'; input.style.opacity = '0';
      document.body.append(input); input.select();
      try { copied = document.execCommand('copy'); } catch (_) { /* Manual selection remains available. */ }
      input.remove(); button.focus();
    }
    const toast = document.getElementById('toast');
    toast.textContent = copied ? '路径已复制' : '无法自动复制，请选中路径手动复制。';
    toast.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.hidden = true, 2400);
  });
  let printState = [];
  window.addEventListener('beforeprint', () => {
    printState = [...document.querySelectorAll('.card:not([hidden]) details')].map(detail => [detail, detail.open]);
    printState.forEach(([detail]) => detail.open = true);
  });
  window.addEventListener('afterprint', () => printState.forEach(([detail, open]) => detail.open = open));
  document.getElementById('print').onclick = () => window.print();

  if (location.hash.startsWith('#?')) {
    const saved = new URLSearchParams(location.hash.slice(2));
    for (const key of ['project', 'from', 'to', 'status', 'sort']) if (saved.has(key)) controls[key].value = saved.get(key);
    if (saved.has('query')) controls.search.value = saved.get('query');
    if (controls.from.value || controls.to.value) activePreset = 'custom';
    if (!controls.sort.value) controls.sort.value = 'newest';
  }
  const anchor = location.hash.startsWith('#item-') ? location.hash.slice(1) : '';
  filter();
  if (anchor && document.getElementById(anchor)) {
    document.getElementById(anchor).open = true;
    document.getElementById(anchor).scrollIntoView({block: 'start'});
  }
})();
