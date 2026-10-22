let apiBase = 'http://127.0.0.1';
let apiPort = 0;
let state = { domains: [], selectedDomain: null, papers: [], domainOverview: null,
  domainConfig: null, outline: null, domainsSig: '', papersSig: '' };

function api(path) { return `${apiBase}:${apiPort}${path}`; }

let apiBannerTimer = null;
function showApiBanner(status, message) {
  const el = document.getElementById('api-banner');
  if (!el) return;
  el.textContent = `⚠ API ${status || ''}：${message || '请求失败'}（点击关闭）`;
  el.classList.remove('hidden');
  clearTimeout(apiBannerTimer);
  apiBannerTimer = setTimeout(() => el.classList.add('hidden'), 6000);
}
document.getElementById('api-banner').addEventListener('click', () => {
  clearTimeout(apiBannerTimer);
  document.getElementById('api-banner').classList.add('hidden');
});

let silentRequests = 0;

async function req(path, opts) {
  let res;
  try {
    res = await fetch(api(path), {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch (e) {
    if (!silentRequests) showApiBanner('网络', '连接失败（后端未运行或端口变化）');
    throw e;
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const msg = body.detail || `${res.status} ${res.statusText}`;
    if (!silentRequests) showApiBanner(res.status, msg);
    throw new Error(msg);
  }
  return res.json();
}

function openTool(title, html) {
  document.getElementById('tool-title').textContent = title;
  document.getElementById('tool-body').innerHTML = html;
  document.getElementById('tool-mask').classList.remove('hidden');
}
function closeTool() { document.getElementById('tool-mask').classList.add('hidden'); }
document.getElementById('tool-close').addEventListener('click', closeTool);
document.getElementById('tool-mask').addEventListener('click', (e) => {
  if (e.target.id === 'tool-mask') closeTool();
});

let topModalZ = 100;
const modalLayerObserver = new MutationObserver((entries) => {
  for (const entry of entries) {
    const el = entry.target;
    if (el.classList.contains('modal-mask') && !el.classList.contains('hidden')) {
      topModalZ += 1;
      el.style.zIndex = String(topModalZ);
    }
  }
});
document.querySelectorAll('.modal-mask').forEach((el) => {
  modalLayerObserver.observe(el, { attributes: true, attributeFilter: ['class'] });
});

function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename || 'export.md';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function mdEscape(s) {
  return String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.tab-body').forEach((b) => b.classList.add('hidden'));
    tab.classList.add('active');
    document.getElementById(`tab-${tab.dataset.tab}`).classList.remove('hidden');
  });
});

function setStatus(status, port) {
  const label = document.getElementById('status-backend');
  const portEl = document.getElementById('status-port');
  if (status === 'ok') {
    label.innerHTML = '<span class="dot dot-ok"></span>后端运行中';
    portEl.textContent = `port ${port}`;
  } else {
    label.innerHTML = '<span class="dot dot-off"></span>后端离线';
    portEl.textContent = '—';
  }
}

let healthPollActive = false;
async function pollHealth() {
  if (healthPollActive) return;
  healthPollActive = true;
  try {
    for (;;) {
      if (!apiPort) {
        await new Promise((r) => setTimeout(r, 1000));
        continue;
      }
      try {
        const res = await fetch(api(`/health`), { cache: 'no-store' });
        if (res.ok) {
          setStatus('ok', apiPort);
          const q = (await res.json()).queue || {};
          const qEl = document.getElementById('status-queue');
          if (q.current) qEl.textContent = `解析中: ${q.current}`;
          else if (q.queued > 0) qEl.textContent = `队列: ${q.queued}`;
          else qEl.textContent = '';
        } else {
          setStatus('offline');
        }
      } catch (_) {
        setStatus('offline');
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
  } finally {
    healthPollActive = false;
  }
}

async function loadDomains(render = true) {
  const next = await req('/domains');
  const sig = JSON.stringify(next);
  state.domains = next;
  if (sig === state.domainsSig) return;
  state.domainsSig = sig;
  if (render) renderDomains();
  else if (!state.selectedDomain) updateDomainCounts();
}

function updateDomainCounts() {
  for (const d of state.domains) {
    const item = document.querySelector(`.domain-item[data-id="${d.domain_id}"]`);
    if (!item) { renderDomains(); return; }
    const count = item.querySelector('.domain-count');
    if (count) count.textContent = `${d.paper_count} 篇`;
    const weight = item.querySelector('.domain-weight');
    if (weight) {
      weight.textContent = Number(d.weight) >= 0.9 ? '★' : Number(d.weight) >= 0.45 ? '◈' : '○';
      item.dataset.weight = d.weight == null ? 1 : d.weight;
    }
  }
}

function renderDomains() {
  const el = document.getElementById('domain-list');
  const search = document.getElementById('domain-search-wrap');
  if (search) search.remove();
  if (!state.domains.length) {
    el.innerHTML = '<div class="empty-hint">尚无领域<br /><span class="sub">导入论文后会自动创建领域</span></div>';
    return;
  }
  const known = new Set(state.domains.map((d) => d.domain_id));
  const byParent = new Map();
  state.domains.forEach((d) => {
    const p = (d.parent_domain_id && d.parent_domain_id !== d.domain_id
      && known.has(d.parent_domain_id)) ? d.parent_domain_id : 0;
    if (!byParent.has(p)) byParent.set(p, []);
    byParent.get(p).push(d);
  });
  const rows = [];
  const seen = new Set();
  const walk = (parent, depth) => {
    (byParent.get(parent) || []).sort((a, b) => a.domain_id - b.domain_id).forEach((d) => {
      if (seen.has(d.domain_id)) return;
      seen.add(d.domain_id);
      rows.push({ d, depth });
      walk(d.domain_id, depth + 1);
    });
  };
  walk(0, 0);
  state.domains.forEach((d) => { if (!seen.has(d.domain_id)) rows.push({ d, depth: 0 }); });

  const weightIcon = (w) => (Number(w) >= 0.9 ? '★' : Number(w) >= 0.45 ? '◈' : '○');
  const weightName = (w) => (Number(w) >= 0.9 ? '主攻' : Number(w) >= 0.45 ? '交叉' : '了解');
  el.innerHTML = rows.map(({ d, depth }) => `
    <div class="domain-item ${state.selectedDomain === d.domain_id ? 'active' : ''} ${depth ? 'domain-child' : ''}"
         data-id="${d.domain_id}" data-weight="${d.weight == null ? 1 : d.weight}"
         draggable="true" style="margin-left:${depth * 14}px">
      <div class="domain-top">
        <div class="domain-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</div>
        <button class="domain-weight" data-act="weight" title="权重：${weightName(d.weight)}（点击切换 主攻/交叉/了解，影响跨领域排序）">${weightIcon(d.weight)}</button>
        <div class="domain-count">${d.paper_count} 篇</div>
      </div>
      ${(d.open_questions || d.pending_reviews) ? `<div class="domain-flags">
        ${d.open_questions ? `<span title="未解决问题">未决 ${d.open_questions}</span>` : ''}
        ${d.pending_reviews ? `<span title="待复核引用">待核 ${d.pending_reviews}</span>` : ''}
      </div>` : ''}
      <div class="domain-actions">
        <button class="btn btn-mini" data-act="knowledge" title="领域知识沉淀视图">知</button>
        <button class="btn btn-mini" data-act="dqa" title="领域问答（跨论文窗口）">问</button>
        <button class="btn btn-mini" data-act="batch" title="按领域批量扩充下载（术语表检索）">批</button>
        ${d.parent_domain_id ? '<button class="btn btn-mini" data-act="detach" title="移出父领域（回到顶层）">↥</button>' : ''}
        <button class="btn btn-mini" data-act="rename" title="重命名">改</button>
        <button class="btn btn-mini" data-act="merge" title="合并到其他领域">合</button>
        <button class="btn btn-mini" data-act="split" title="选中论文拆分为新领域">拆</button>
        <button class="btn btn-mini btn-danger" data-act="delete" title="删除（论文将重新归属）">删</button>
      </div>
      <div class="merge-box hidden" data-id="${d.domain_id}"></div>
      <div class="split-box hidden" data-id="${d.domain_id}"></div>
    </div>`).join('');
  el.querySelectorAll('.domain-item').forEach((item) => {
    const id = Number(item.dataset.id);
    item.querySelector('.domain-top').addEventListener('click', () => selectDomain(id));
    item.querySelector('[data-act="knowledge"]').addEventListener('click', (e) => { e.stopPropagation(); openKnowledge(id); });
    item.querySelector('[data-act="dqa"]').addEventListener('click', (e) => { e.stopPropagation(); openDomainQa(id); });
    item.querySelector('[data-act="batch"]').addEventListener('click', (e) => {
      e.stopPropagation();
      document.getElementById('btn-import').click();
      document.getElementById('import-domain-select').value = String(id);
      document.getElementById('batch-query').value = '';
      document.getElementById('batch-result').classList.add('hidden');
      document.getElementById('batch-query').focus();
    });
    item.querySelector('[data-act="rename"]').addEventListener('click', (e) => { e.stopPropagation(); renameDomain(id); });
    item.querySelector('[data-act="delete"]').addEventListener('click', (e) => { e.stopPropagation(); deleteDomain(id); });
    item.querySelector('[data-act="merge"]').addEventListener('click', (e) => { e.stopPropagation(); toggleMergeBox(id); });
    item.querySelector('[data-act="split"]').addEventListener('click', (e) => { e.stopPropagation(); toggleSplitBox(id); });
    item.querySelector('[data-act="weight"]').addEventListener('click', async (e) => {
      e.stopPropagation();
      const cur = Number(item.dataset.weight || 1);
      const next = cur >= 0.9 ? 0.6 : (cur >= 0.45 ? 0.3 : 1.0);
      try {
        await req(`/domains/${id}/weight`, { method: 'PUT', body: JSON.stringify({ weight: next }) });
        await loadDomains();
        showToast(`权重：${weightName(next)}`);
      } catch (err) { showToast(`权重修改失败：${err.message}`); }
    });
    const detach = item.querySelector('[data-act="detach"]');
    if (detach) detach.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        await req(`/domains/${id}/parent`, { method: 'POST', body: JSON.stringify({ parent_id: null }) });
        await loadDomains();
        showToast('已移出父领域');
      } catch (err) { showToast(`操作失败：${err.message}`); }
    });
    item.addEventListener('dragstart', (e) => {
      if (e.target.closest && e.target.closest('.paper-item')) return;
      e.dataTransfer.setData('application/x-scholar-domain', String(id));
      e.dataTransfer.setData('text/plain', String(id));
      e.dataTransfer.effectAllowed = 'move';
    });
    item.addEventListener('dragover', (e) => { e.preventDefault(); item.classList.add('drop-target'); });
    item.addEventListener('dragleave', () => item.classList.remove('drop-target'));
    item.addEventListener('drop', async (e) => {
      e.preventDefault();
      item.classList.remove('drop-target');
      const paperRaw = e.dataTransfer.getData('application/x-scholar-paper');
      if (paperRaw) {
        const pid = Number(paperRaw);
        const target = state.domains.find((d) => d.domain_id === id);
        if (!pid || !target) return;
        if (!confirm(`把这篇论文移动到「${target.name}」？`)) return;
        try {
          await req(`/papers/${pid}/move`, { method: 'POST', body: JSON.stringify({ domain_id: id }) });
          await loadDomains();
          if (state.selectedDomain) await selectDomain(state.selectedDomain);
          showToast(`已移动到「${target.name}」`);
        } catch (err) { showToast(`移动失败：${err.message}`); }
        return;
      }
      const src = Number(e.dataTransfer.getData('application/x-scholar-domain')
                          || e.dataTransfer.getData('text/plain'));
      if (!src || src === id) return;
      try {
        await req(`/domains/${src}/parent`, { method: 'POST', body: JSON.stringify({ parent_id: id }) });
        await loadDomains();
        showToast('已调整领域层级');
      } catch (err) { showToast(`层级调整失败：${err.message}`); }
    });
  });
}

let knDomainId = null;

async function openKnowledge(domainId) {
  knDomainId = domainId;
  let k;
  try {
    k = await req(`/domains/${domainId}/knowledge`);
  } catch (e) { alert(`加载领域知识失败: ${e.message}`); return; }
  const [questions, reviews, concepts] = await Promise.all([
    req(`/domains/${domainId}/questions?status=open`).catch(() => []),
    req(`/domains/${domainId}/reviews?status=pending`).catch(() => []),
    req(`/domains/${domainId}/concepts`).then((r) => r.concepts || []).catch(() => []),
  ]);
  document.getElementById('kn-title').textContent = `领域知识 · ${k.name}`;
  document.getElementById('kn-stats').innerHTML =
    `${k.n_papers} 篇论文 · 未决 ${questions.length} · 待核 ${reviews.length}` +
    ` · <span class="kn-c kn-background">背景 ${k.categories.background || 0}</span>` +
    ` · <span class="kn-c kn-approach">解决思路 ${k.categories.approach || 0}</span>` +
    ` · <span class="kn-c kn-limitation">局限 ${k.categories.limitation || 0}</span>` +
    ` · 批注 ${k.n_notes} · 问答 ${k.n_qa}`;
  document.getElementById('kn-view-list').classList.add('active');
  document.getElementById('kn-view-graph').classList.remove('active');
  document.getElementById('kn-body').classList.remove('hidden');
  document.getElementById('kn-graph').classList.add('hidden');
  const body = document.getElementById('kn-body');
  const jump = (paperId, page) => {
    if (!paperId) return;
    document.getElementById('knowledge-mask').classList.add('hidden');
    openPaper(paperId).then(() => { if (page) jumpToPage(page); });
  };
  const paperGroups = new Map();
  for (const h of k.highlights) {
    if (!paperGroups.has(h.paper_id)) paperGroups.set(h.paper_id, { paper: h.paper, items: [] });
    paperGroups.get(h.paper_id).items.push(h);
  }
  const catLabel = { background: '背景', approach: '解决思路', limitation: '局限', delete: '删除标记' };
  let html = '';
  if (!k.n_highlights && !k.n_notes && !k.n_qa && !questions.length && !reviews.length) {
    html = '<div class="empty-hint">该领域还没有阅读沉淀<br /><span class="sub">打开论文 → 自动识别 / 划选 / 批注 / 问答</span></div>';
  }
  if (questions.length) {
    html += '<div class="kn-section-title">未决问题</div>' + questions.map((q) => `
      <div class="kn-item kn-todo" data-p="${q.paper_id || ''}">
        <span class="badge">未决</span>
        <span class="kn-quote">${escapeHtml(q.text)}</span>
        <span class="kn-note">${escapeHtml(q.paper_title || '领域级')}</span>
        <button class="btn btn-mini" data-qid="${q.question_id}">已解决</button>
      </div>`).join('');
  }
  if (reviews.length) {
    html += '<div class="kn-section-title">待复核引用</div>' + reviews.map((r) => `
      <div class="kn-item kn-todo" data-p="${r.paper_id || ''}">
        <span class="badge badge-hl-limitation">待核</span>
        <span class="kn-quote">${escapeHtml((r.target_title || r.target_doi || '未命名引用').slice(0, 90))}</span>
        <span class="kn-note">来自《${escapeHtml((r.source_title || '').slice(0, 40))}》</span>
        <button class="btn btn-mini" data-rid="${r.item_id}" data-act="confirm">确认</button>
        <button class="btn btn-mini btn-danger" data-rid="${r.item_id}" data-act="reject">忽略</button>
      </div>`).join('');
  }
  if (concepts.length) {
    html += '<div class="kn-section-title">术语 / 概念</div>' + concepts.map((c) => `
      <div class="kn-item">
        <span class="badge">术语</span>
        <span class="kn-quote">${escapeHtml(c.display_en || '')}</span>
        <span class="kn-note">${escapeHtml(c.term_zh || c.display_zh || '')}${c.definition ? ' · ' + escapeHtml(c.definition) : ''}</span>
      </div>`).join('');
  }
  if (k.n_highlights) {
    html += '<div class="kn-section-title">高亮（按论文分组）</div>';
    for (const [, g] of paperGroups) {
      html += `<div class="kn-paper-title">${escapeHtml(g.paper)}</div>` + g.items.map((h) => `
        <div class="kn-item" data-p="${h.paper_id}" data-pg="${h.page || ''}">
          <span class="badge badge-hl-${h.category}">${catLabel[h.category] || h.category}</span>
          <span class="kn-quote">${escapeHtml(h.quote.slice(0, 150))}${h.quote.length > 150 ? '…' : ''}</span>
          <span class="kn-page">p.${h.page}</span>
        </div>`).join('');
    }
  }
  if (k.n_notes) {
    html += '<div class="kn-section-title">批注</div>' + k.notes.map((n) => `
      <div class="kn-item" data-p="${n.paper_id}" data-pg="${n.page || ''}">
        <span class="badge badge-parsing">批注</span>
        ${(n.tags || []).map((t) => `<span class="tag-badge">${escapeHtml(t)}</span>`).join('')}
        <span class="kn-quote">${escapeHtml((n.quote || '').slice(0, 60))}…</span>
        <span class="kn-note">${escapeHtml((n.text || '').slice(0, 120))}</span>
      </div>`).join('');
  }
  if (k.n_qa) {
    html += '<div class="kn-section-title">最近问答</div>' + k.qa.map((q) => `
      <div class="kn-item" data-p="${q.paper_id}">
        <span class="badge">问</span>
        <span class="kn-quote">${escapeHtml(q.question.slice(0, 80))}</span>
        <span class="kn-note">${escapeHtml((q.answer || '').slice(0, 100))}…</span>
      </div>`).join('');
  }
  body.innerHTML = html;
  body.querySelectorAll('.kn-item[data-p]').forEach((item) => {
    item.addEventListener('click', () => jump(Number(item.dataset.p), Number(item.dataset.pg) || null));
  });
  body.querySelectorAll('[data-qid]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        await req(`/questions/${btn.dataset.qid}/resolve`, {
          method: 'POST', body: JSON.stringify({ status: 'resolved' }),
        });
        await openKnowledge(knDomainId);
      } catch (err) { alert(`更新失败: ${err.message}`); }
    });
  });
  body.querySelectorAll('[data-rid]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const status = btn.dataset.act === 'confirm' ? 'confirmed' : 'rejected';
      try {
        await req(`/reviews/${btn.dataset.rid}/resolve`, {
          method: 'POST', body: JSON.stringify({ status }),
        });
        await openKnowledge(knDomainId);
      } catch (err) { alert(`更新失败: ${err.message}`); }
    });
  });
  document.getElementById('knowledge-mask').classList.remove('hidden');
}

async function renderSurveyForm(panel, domainId) {
  const domains = (state.domains || []);
  const drafts = await req(`/domains/${domainId}/surveys`).catch(() => []);
  panel.innerHTML = `<div class="survey-form">
    <div class="survey-row"><b>领域范围</b><span class="sub">可跨领域；当前领域必选</span>
      <div class="survey-checks">${domains.map((d) => `<label class="survey-line">
        <input type="checkbox" data-sdom="${d.domain_id}" ${d.domain_id === domainId ? 'checked disabled' : ''}/>
        ${escapeHtml(d.name)}</label>`).join('')}</div></div>
    <div class="survey-row"><b>论文范围</b><span class="sub">不勾选 = 所选领域的全部论文</span>
      <div class="survey-checks" id="survey-papers"><div class="empty-hint">加载中…</div></div></div>
    <div class="survey-row survey-opts">
      <label>结构
        <select id="survey-group">
          <option value="topic">按主题分组</option>
          <option value="year">按时间线</option>
          <option value="method">按方法对比</option>
        </select></label>
      <label><input type="checkbox" id="survey-gaps" checked/> 研究空白</label>
      <label><input type="checkbox" id="survey-notes"/> 笔记计数</label>
      <label><input type="checkbox" id="survey-synth"/> 模型导言（需 Key）</label>
      <button class="btn btn-mini btn-primary" id="survey-go">生成</button>
    </div>
    ${drafts.length ? `<div class="survey-row"><b>已存草稿</b><div class="tool-actions">
      ${drafts.map((s) => `<button class="btn btn-mini" data-draft="${s.survey_id}">#${s.survey_id} ${escapeHtml((s.title || '').slice(0, 16))}</button>`).join('')}
    </div></div>` : ''}
  </div>
  <div id="survey-out"></div>`;

  const paperBox = document.getElementById('survey-papers');
  const selectedDomains = () => [...panel.querySelectorAll('[data-sdom]:checked')]
    .map((cb) => Number(cb.dataset.sdom));
  const loadPapers = async () => {
    const keep = new Set([...paperBox.querySelectorAll('[data-spid]:checked')]
      .map((cb) => Number(cb.dataset.spid)));
    const all = [];
    for (const did of selectedDomains()) {
      try {
        (await req(`/domains/${did}/papers`)).forEach((p) => all.push(p));
      } catch (_) { /* one bad domain must not break the form */ }
    }
    const seen = new Set();
    paperBox.innerHTML = all.filter((p) => {
      if (seen.has(p.paper_id)) return false;
      seen.add(p.paper_id);
      return true;
    }).map((p) => `<label class="survey-line"><input type="checkbox" data-spid="${p.paper_id}"
      ${keep.has(p.paper_id) ? 'checked' : ''}/> ${escapeHtml((p.title || '(untitled)').slice(0, 42))}</label>`)
      .join('') || '<span class="sub">所选领域没有论文</span>';
  };
  panel.querySelectorAll('[data-sdom]').forEach((cb) => cb.addEventListener('change', loadPapers));
  await loadPapers();

  const showDraft = (d) => {
    const out = document.getElementById('survey-out');
    out.innerHTML = `<div class="tool-actions">
        <button class="btn btn-mini btn-primary" id="survey-save">保存修改</button>
        <button class="btn btn-mini" id="survey-dl">下载 Markdown</button>
        <span class="sub">${d.survey_id ? `草稿 #${d.survey_id}` : '未保存'} · ${escapeHtml(d.title || '')}</span></div>
      <textarea id="survey-md" class="survey-md" spellcheck="false"></textarea>`;
    const ta = document.getElementById('survey-md');
    ta.value = d.markdown || '';
    document.getElementById('survey-save').onclick = async () => {
      if (!d.survey_id) { showToast('该结果没有保存为草稿'); return; }
      await req(`/surveys/${d.survey_id}`, { method: 'PUT', body: JSON.stringify({ markdown: ta.value }) });
      showToast('草稿已保存');
    };
    document.getElementById('survey-dl').onclick = () => downloadText(`${(d.title || 'survey')}.md`, ta.value);
  };

  document.getElementById('survey-go').onclick = async () => {
    const out = document.getElementById('survey-out');
    out.innerHTML = '<div class="empty-hint">生成中…</div>';
    try {
      const body = {
        domain_ids: selectedDomains(),
        paper_ids: [...paperBox.querySelectorAll('[data-spid]:checked')].map((cb) => Number(cb.dataset.spid)),
        group_by: document.getElementById('survey-group').value,
        include_gaps: document.getElementById('survey-gaps').checked,
        include_notes: document.getElementById('survey-notes').checked,
        synthesize: document.getElementById('survey-synth').checked,
        save: true,
      };
      const r = await req(`/domains/${domainId}/survey`, { method: 'POST', body: JSON.stringify(body) });
      if (r.warning) showToast(r.warning);
      showDraft({ survey_id: r.survey_id, title: r.title || '', markdown: r.markdown });
    } catch (e) {
      out.innerHTML = `<div class="empty-hint">生成失败：${escapeHtml(e.message || '')}</div>`;
    }
  };
  panel.querySelectorAll('[data-draft]').forEach((b) => b.onclick = async () => {
    try {
      showDraft(await req(`/surveys/${b.dataset.draft}`));
    } catch (e) { showToast(`打开草稿失败：${e.message}`); }
  });
}

async function renderGlossaryPanel(panel, domainId) {
  const run = async () => {
    panel.innerHTML = '<div class="empty-hint">提炼中…（需要模型 Key，约几秒）</div>';
    try {
      const r = await req(`/domains/${domainId}/glossary`, {
        method: 'POST', body: JSON.stringify({ update: true }),
      });
      const terms = r.terms || [];
      panel.innerHTML = `<div class="tool-actions">
          <button class="btn btn-mini btn-primary" id="glossary-rebuild">${terms.length ? '重新提炼' : '提炼术语表'}</button>
          <span class="sub">${terms.length} 个术语 · 已写入概念表</span></div>` +
        (terms.map((t) => `<div class="kn-item"><span class="badge">术语</span>
          <span class="kn-quote">${escapeHtml(t.en || '')}</span>
          <span class="kn-note">${escapeHtml(t.zh || '')}</span></div>`).join('')
          || '<div class="empty-hint">模型没有返回术语（可能论文太少），可重试</div>');
      if (document.getElementById('glossary-rebuild')) {
        document.getElementById('glossary-rebuild').onclick = run;
      }
    } catch (e) {
      panel.innerHTML = `<div class="empty-hint">${escapeHtml(e.message || '提炼失败')}</div>
        <div class="tool-actions"><button class="btn btn-mini" id="glossary-rebuild">重试</button></div>`;
      document.getElementById('glossary-rebuild').onclick = run;
    }
  };
  await run();
}

async function openDomainTools() {
  const domainId = knDomainId || state.selectedDomain;
  if (!domainId) return;
  const d = state.domains.find((x) => x.domain_id === domainId);
  openTool(`领域工具 · ${d ? d.name : domainId}`, `
    <div class="tool-actions">
      <button class="btn btn-mini" data-tool="report">学习报告</button>
      <button class="btn btn-mini" data-tool="survey">综述草稿</button>
      <button class="btn btn-mini" data-tool="snapshots">快照</button>
      <button class="btn btn-mini" data-tool="glossary">术语表</button>
      <button class="btn btn-mini" data-tool="log">操作日志</button>
      <button class="btn btn-mini" data-tool="concepts">概念/映射</button>
      <button class="btn btn-mini" data-tool="suggestions">跨领域建议</button>
      <button class="btn btn-mini" data-tool="subtopics">子主题</button>
      <button class="btn btn-mini" data-tool="index">物化索引</button>
    </div>
    <div id="tool-panel"><div class="empty-hint">选择一个工具</div></div>`);
  const panel = document.getElementById('tool-panel');
  const showMd = (title, r) => {
    panel.innerHTML = `<div class="tool-actions"><button class="btn btn-mini" id="tool-download">下载 Markdown</button></div>
      <pre class="md">${escapeHtml(r.markdown || '')}</pre>`;
    document.getElementById('tool-download').onclick = () => downloadText(r.filename, r.markdown);
  };
  document.querySelectorAll('#tool-body [data-tool]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const kind = btn.dataset.tool;
      panel.innerHTML = '<div class="empty-hint">加载中…</div>';
      try {
        if (kind === 'report') {
          showMd('report', await req(`/domains/${domainId}/report`));
        } else if (kind === 'survey') {
          await renderSurveyForm(panel, domainId);
        } else if (kind === 'glossary') {
          await renderGlossaryPanel(panel, domainId);
        } else if (kind === 'snapshots') {
          const rows = await req(`/domains/${domainId}/snapshots`);
          panel.innerHTML = `<div class="tool-actions"><input id="snap-name" class="inline-input" placeholder="快照名（可空）"/>
            <button class="btn btn-mini btn-primary" id="snap-create">创建快照</button></div>
            <div class="survey-row survey-opts">
              <label title="恢复后可能重新出现当时的笔记"><input type="checkbox" id="snap-notes"/> 同时恢复笔记</label>
              <label><input type="checkbox" id="snap-qa"/> 同时恢复问答历史</label>
              <span class="sub">不勾选时只补回归属/阅读位置/未决问题/待核引用/配置，不触碰现有笔记</span>
            </div>` +
            (rows.length ? rows.map((s) => `<div class="kn-item"><span class="badge">#${s.snapshot_id}</span>
              <span class="kn-quote">${escapeHtml(s.name || '')}</span>
              <span class="kn-note">seq ${s.last_seq} · ${escapeHtml(s.created_at || '')}</span>
              <button class="btn btn-mini" data-restore="${s.snapshot_id}">恢复</button></div>`).join('')
              : '<div class="empty-hint">暂无快照</div>');
          document.getElementById('snap-create').onclick = async () => {
            await req(`/domains/${domainId}/snapshots`, {
              method: 'POST', body: JSON.stringify({ name: document.getElementById('snap-name').value.trim() }),
            });
            openDomainTools();
          };
          panel.querySelectorAll('[data-restore]').forEach((b) => b.onclick = async () => {
            const body = {
              include_notes: document.getElementById('snap-notes').checked,
              include_qa: document.getElementById('snap-qa').checked,
            };
            if (!confirm('恢复该快照？将补回论文归属、阅读位置、未决问题、待复核引用与领域配置；不会删除现有数据。')) return;
            const r = await req(`/domains/${domainId}/snapshots/${b.dataset.restore}/restore`,
              { method: 'POST', body: JSON.stringify(body) });
            showToast(`已恢复：归属 +${r.links_added}/~${r.links_updated} · 问题 ${r.questions} · 待核 ${r.reviews}`
              + (body.include_notes ? ` · 笔记 ${r.notes}` : '')
              + (body.include_qa ? ` · 问答 ${r.qa_turns}` : ''));
            loadOverview().catch(() => {});
          });
        } else if (kind === 'log') {
          const rows = await req(`/operations?domain_id=${domainId}&limit=60`);
          panel.innerHTML = rows.length ? rows.map((r) => `<div class="kn-item"><span class="badge">#${r.seq}</span>
            <span class="kn-quote">${escapeHtml(r.op)}</span>
            <span class="kn-note">${escapeHtml(r.created_at || '')}</span></div>`).join('')
            : '<div class="empty-hint">暂无操作记录</div>';
        } else if (kind === 'concepts') {
          const res = await req(`/domains/${domainId}/concepts`);
          const maps = await req(`/concepts/mappings?domain_id=${domainId}`).catch(() => []);
          const others = (state.domains || []).filter((x) => x.domain_id !== domainId);
          panel.innerHTML = `<div class="tool-actions">
            <input id="c-en" class="inline-input" placeholder="英文术语"/>
            <input id="c-zh" class="inline-input" placeholder="中文"/>
            <input id="c-def" class="inline-input" placeholder="定义（可空）"/>
            <button class="btn btn-mini btn-primary" id="c-add">新增</button></div>` +
            (res.concepts || []).map((c) => `<div class="kn-item"><span class="badge">术语</span>
              <span class="kn-quote">${escapeHtml(c.display_en || '')}</span>
              <span class="kn-note">${escapeHtml(c.term_zh || '')}${c.definition ? ' · ' + escapeHtml(c.definition) : ''}</span></div>`).join('') +
            `<div class="tool-actions" style="margin-top:10px">
              <select id="m-c">${(res.concepts || []).map((c) => `<option value="${c.concept_id}">${escapeHtml(c.display_en)}</option>`).join('')}</select>
              <select id="m-d">${others.map((x) => `<option value="${x.domain_id}">${escapeHtml(x.name)}</option>`).join('')}</select>
              <select id="m-t"><option value="same">含义相同</option><option value="different">含义不同</option><option value="related">相关</option></select>
              <button class="btn btn-mini" id="m-add">建立映射</button></div>` +
            (maps.length ? maps.map((m) => `<div class="kn-item"><span class="badge">映射</span>
              <span class="kn-quote">${escapeHtml(m.display_en || '')}</span>
              <span class="kn-note">${escapeHtml(m.domain_a_name)} ↔ ${escapeHtml(m.domain_b_name)} · ${escapeHtml(m.mapping_type)}</span></div>`).join('')
              : '');
          document.getElementById('c-add').onclick = async () => {
            const term = document.getElementById('c-en').value.trim();
            if (!term) return;
            await req(`/domains/${domainId}/concepts`, {
              method: 'POST', body: JSON.stringify({
                term_en: term, term_zh: document.getElementById('c-zh').value.trim(),
                definition: document.getElementById('c-def').value.trim(),
              }),
            });
            openDomainTools();
          };
          const mAdd = document.getElementById('m-add');
          if (mAdd) mAdd.onclick = async () => {
            const cid = Number(document.getElementById('m-c').value);
            const other = Number(document.getElementById('m-d').value);
            if (!cid || !other) return;
            await req('/concepts/mappings', {
              method: 'POST', body: JSON.stringify({
                concept_id: cid, domain_a: domainId, domain_b: other,
                mapping_type: document.getElementById('m-t').value,
              }),
            });
            openDomainTools();
          };
        } else if (kind === 'suggestions') {
          const rows = await req(`/suggestions?domain_id=${domainId}`);
          panel.innerHTML = rows.length ? rows.map((s) => `<div class="kn-item">
            <span class="badge">${escapeHtml(s.status)}</span>
            <span class="kn-quote">${escapeHtml(s.description)}</span>
            <span class="kn-note">置信度 ${(Number(s.confidence) * 100).toFixed(0)}%</span>
            <button class="btn btn-mini" data-sug="${s.suggestion_id}" data-st="explored">探索</button>
            <button class="btn btn-mini" data-sug="${s.suggestion_id}" data-st="ignored">忽略</button>
            <button class="btn btn-mini" data-sug="${s.suggestion_id}" data-st="snoozed">稍后</button></div>`).join('')
            : '<div class="empty-hint">暂无建议（可由 API 写入）</div>';
          panel.querySelectorAll('[data-sug]').forEach((b) => b.onclick = async () => {
            await req(`/suggestions/${b.dataset.sug}/feedback`, {
              method: 'POST', body: JSON.stringify({ status: b.dataset.st }),
            });
            openDomainTools();
          });
        } else if (kind === 'subtopics') {
          const rows = await req(`/domains/${domainId}/subtopics`);
          panel.innerHTML = `<div class="tool-actions"><input id="st-name" class="inline-input" placeholder="子主题名"/>
            <button class="btn btn-mini btn-primary" id="st-add">新增</button></div>` +
            (rows.length ? rows.map((s) => `<div class="kn-item"><span class="badge">#${s.subtopic_id}</span>
              <span class="kn-quote">${escapeHtml(s.name)}</span>
              <span class="kn-note">${s.parent_id ? '父 #' + s.parent_id : '顶层'}</span></div>`).join('')
              : '<div class="empty-hint">暂无子主题</div>');
          document.getElementById('st-add').onclick = async () => {
            const name = document.getElementById('st-name').value.trim();
            if (!name) return;
            await req(`/domains/${domainId}/subtopics`, { method: 'POST', body: JSON.stringify({ name }) });
            openDomainTools();
          };
        } else if (kind === 'index') {
          panel.innerHTML = `<div class="empty-hint">把该领域的分块与向量写成独立 JSONL 快照，并把 index_mode 置为 physical。检索仍走 SQLite。</div>
            <div class="tool-actions"><button class="btn btn-mini btn-primary" id="idx-go">生成物化索引</button></div>`;
          document.getElementById('idx-go').onclick = async () => {
            const r = await req(`/domains/${domainId}/index/migrate`, { method: 'POST' });
            panel.innerHTML = `<pre class="md">${escapeHtml(JSON.stringify(r, null, 2))}</pre>`;
          };
        }
      } catch (e) { panel.innerHTML = `<div class="fc-error">${escapeHtml(e.message)}</div>`; }
    });
  });
}

document.getElementById('kn-close').addEventListener('click', () => {
  document.getElementById('knowledge-mask').classList.add('hidden');
});
document.getElementById('kn-tools').addEventListener('click', openDomainTools);

function saveGraphLayout(domainId, data) {
  try { localStorage.setItem(`graph:${domainId}`, JSON.stringify(data)); } catch (_) {}
  req(`/domains/${domainId}/config`).then((cfg) => {
    let extra = {};
    try { extra = JSON.parse(cfg.extra_json || '{}'); } catch (_) { extra = {}; }
    extra.graph_layout = data;
    return req(`/domains/${domainId}/config`, {
      method: 'PUT', body: JSON.stringify({ extra_json: JSON.stringify(extra) }),
    });
  }).catch(() => {});
}

document.getElementById('kn-view-graph').addEventListener('click', async () => {
  document.getElementById('kn-view-graph').classList.add('active');
  document.getElementById('kn-view-list').classList.remove('active');
  document.getElementById('kn-body').classList.add('hidden');
  const gEl = document.getElementById('kn-graph');
  gEl.classList.remove('hidden');
  gEl.innerHTML = '<div class="fc-loading">构建图谱…</div>';
  try {
    const g = await req(`/domains/${knDomainId}/graph`);
    const W = 820, H = 440, cx = W / 2, cy = H / 2;
    const others = g.domains.filter((d) => d.domain_id !== g.center);
    const domPos = { [g.center]: { x: cx, y: cy, r: 22 } };
    others.forEach((d, i) => {
      const ang = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(1, others.length);
      domPos[d.domain_id] = { x: cx + 250 * Math.cos(ang), y: cy + 165 * Math.sin(ang), r: 16 };
    });
    const papersByDom = {};
    for (const e of g.edges) {
      if (e.kind === 'primary') (papersByDom[e.domain_id] = papersByDom[e.domain_id] || []).push(e.paper_id);
    }
    const paperPos = {};
    for (const [did, pids] of Object.entries(papersByDom)) {
      const dp = domPos[did] || { x: cx, y: cy };
      pids.forEach((pid, i) => {
        const ang = (i * 2 * Math.PI) / pids.length + (dp === domPos[g.center] ? 0.3 : 0);
        const rad = dp.r + 55 + (pids.length > 12 ? 18 : 0);
        paperPos[pid] = { x: dp.x + rad * Math.cos(ang), y: dp.y + rad * 0.75 * Math.sin(ang) };
      });
    }
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(`graph:${g.center}`) || 'null'); } catch (_) { saved = null; }
    if (!saved) {
      try {
        const cfg = await req(`/domains/${g.center}/config`);
        const extra = JSON.parse(cfg.extra_json || '{}');
        saved = extra.graph_layout || null;
      } catch (_) { saved = null; }
    }
    saved = saved || {};
    for (const [pid, pos] of Object.entries(saved.papers || {})) {
      if (paperPos[pid]) Object.assign(paperPos[pid], pos);
    }
    for (const [did, pos] of Object.entries(saved.domains || {})) {
      if (domPos[did]) Object.assign(domPos[did], pos);
    }
    const titleOf = (pid) => (g.papers.find((p) => p.paper_id === pid) || {}).title || '';
    const scoreW = (s) => (s == null ? 1.4 : 1.2 + 3.2 * Math.max(0, Math.min(1, s)));
    const domainEdgesSvg = (g.domain_edges || []).map((e) => {
      const a = domPos[e.domain_a], b = domPos[e.domain_b];
      if (!a || !b) return '';
      return `<line data-dom-a="${e.domain_a}" data-dom-b="${e.domain_b}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="rgba(76,195,232,0.4)" stroke-width="${scoreW(e.score).toFixed(2)}"><title>领域关联 score=${e.score}</title></line>`;
    }).join('');
    const edgesSvg = g.edges.map((e) => {
      const a = paperPos[e.paper_id], b = domPos[e.domain_id];
      if (!a || !b) return '';
      if (e.kind === 'primary') {
        const w = scoreW(e.score).toFixed(2);
        const t = e.score == null ? '' : `<title>归属强度 score=${e.score}</title>`;
        return `<line data-paper="${e.paper_id}" data-dom="${e.domain_id}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="rgba(108,140,255,0.30)" stroke-width="${w}">${t}</line>`;
      }
      return `<line data-paper="${e.paper_id}" data-dom="${e.domain_id}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="rgba(224,179,76,0.5)" stroke-width="${scoreW(e.score).toFixed(2)}" stroke-dasharray="5 4"><title>弱关联 score=${e.score}</title></line>`;
    }).join('');
    const citationSvg = (g.citations || []).map((c) => {
      const a = paperPos[c.source_paper_id], b = paperPos[c.target_paper_id];
      if (!a || !b) return '';
      return `<line data-cite-src="${c.source_paper_id}" data-cite-dst="${c.target_paper_id}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="rgba(110,220,180,0.5)" stroke-width="1.4" marker-end="url(#cite-arrow)"><title>引用</title></line>`;
    }).join('');
    const domSvg = g.domains.map((d) => {
      const p = domPos[d.domain_id];
      const isCenter = d.domain_id === g.center;
      return `<g class="kn-node" data-dom="${d.domain_id}"><circle cx="${p.x}" cy="${p.y}" r="${p.r}" fill="${isCenter ? 'rgba(108,140,255,0.25)' : 'rgba(76,195,232,0.14)'}" stroke="rgba(108,140,255,0.6)" stroke-width="1.5" /><text x="${p.x}" y="${p.y + 4}" text-anchor="middle" font-size="${isCenter ? 11 : 9}">${escapeHtml(d.name.slice(0, 10))}</text></g>`;
    }).join('');
    const paperSvg = g.papers.map((p) => {
      const pos = paperPos[p.paper_id];
      if (!pos) return '';
      const w = Number(p.weight) || 1;
      const r = 4 + Math.min(4, Math.max(0, w - 1));
      return `<g class="kn-node" data-paper="${p.paper_id}"><circle cx="${pos.x}" cy="${pos.y}" r="${r.toFixed(1)}" fill="#7ea4ff" /><title>${escapeHtml(p.title)}${w !== 1 ? ' · 权重 ' + w : ''}</title></g>`;
    }).join('');
    gEl.innerHTML = `
      <div class="kn-pathbar">
        <span class="dqa-papers-label">路径：</span>
        <select id="path-src">${g.papers.map((p) => `<option value="${p.paper_id}">${escapeHtml((p.title || '').slice(0, 40))}</option>`).join('')}</select>
        <span class="dqa-papers-label">→</span>
        <select id="path-dst">${g.papers.map((p) => `<option value="${p.paper_id}">${escapeHtml((p.title || '').slice(0, 40))}</option>`).join('')}</select>
        <button class="btn btn-mini" id="path-find">路径发现</button>
        <span id="path-out" class="dqa-papers-label"></span>
      </div>
      <div class="kn-legend"><span class="kn-leg"><i class="solid"></i>主领域（线宽=归属强度）</span>
        <span class="kn-leg"><i class="dash"></i>弱关联（虚线，线宽=相似度）</span>
        <span class="kn-leg">青线=领域间关联（线宽=强度）</span>
        <span class="kn-leg">绿线=论文间引用</span>
        <span class="kn-leg">点节点=论文（悬停看标题），大节点=领域</span></div>
      <svg viewBox="0 0 ${W} ${H}" class="kn-svg"><defs><marker id="cite-arrow" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="rgba(110,220,180,0.6)"/></marker></defs>${domainEdgesSvg}${edgesSvg}${citationSvg}${domSvg}${paperSvg}</svg>`;
    const pathFind = document.getElementById('path-find');
    if (pathFind) {
      pathFind.addEventListener('click', async () => {
        const out = document.getElementById('path-out');
        out.textContent = '';
        const src = Number(document.getElementById('path-src').value);
        const dst = Number(document.getElementById('path-dst').value);
        if (!src || !dst || src === dst) { out.textContent = '请选择两篇不同论文'; return; }
        try {
          const p = await req(`/domains/${knDomainId}/path?src=${src}&dst=${dst}`);
          gEl.querySelectorAll('line.kn-path-hl').forEach((l) => l.classList.remove('kn-path-hl'));
          if (!p.found) { out.textContent = '两篇论文之间没有引用路径'; return; }
          for (const e of p.edges) {
            const line = gEl.querySelector(`line[data-cite-src="${e.source}"][data-cite-dst="${e.target}"],` +
              `line[data-cite-src="${e.target}"][data-cite-dst="${e.source}"]`);
            if (line) line.classList.add('kn-path-hl');
          }
          out.textContent = p.path.map((pid) => (titleOf(pid) || pid).slice(0, 24)).join(' → ');
        } catch (e) { out.textContent = e.message; }
      });
    }
    gEl.querySelectorAll('[data-paper]').forEach((n) => {
      n.addEventListener('dblclick', () => {
        document.getElementById('knowledge-mask').classList.add('hidden');
        openPaper(Number(n.dataset.paper));
      });
    });
    gEl.querySelectorAll('[data-dom]').forEach((n) => {
      const did = Number(n.dataset.dom);
      if (did !== knDomainId) {
        n.addEventListener('dblclick', () => openKnowledge(did));
      }
    });
    gEl.querySelectorAll('.kn-node').forEach((node) => {
      node.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const svg = gEl.querySelector('svg');
        const pt = svg.createSVGPoint();
        pt.x = ev.clientX; pt.y = ev.clientY;
        const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
        const circle = node.querySelector('circle');
        const isDom = node.dataset.dom !== undefined;
        const key = isDom ? node.dataset.dom : node.dataset.paper;
        const startCx = parseFloat(circle.getAttribute('cx'));
        const startCy = parseFloat(circle.getAttribute('cy'));
        const lines = [...svg.querySelectorAll(
          `line[data-paper="${key}"],line[data-dom="${key}"],line[data-dom-a="${key}"],line[data-dom-b="${key}"],line[data-cite-src="${key}"],line[data-cite-dst="${key}"]`)];
        const ends = new Map();
        for (const ln of lines) {
          const d1 = Math.hypot(parseFloat(ln.getAttribute('x1')) - startCx,
                                parseFloat(ln.getAttribute('y1')) - startCy);
          const d2 = Math.hypot(parseFloat(ln.getAttribute('x2')) - startCx,
                                parseFloat(ln.getAttribute('y2')) - startCy);
          ends.set(ln, d1 <= d2 ? 1 : 2);
        }
        const onMove = (mv) => {
          pt.x = mv.clientX; pt.y = mv.clientY;
          const l = pt.matrixTransform(svg.getScreenCTM().inverse());
          circle.setAttribute('cx', l.x);
          circle.setAttribute('cy', l.y);
          const label = node.querySelector('text');
          if (label) { label.setAttribute('x', l.x); label.setAttribute('y', l.y + 4); }
          for (const [ln, end] of ends) {
            ln.setAttribute(end === 1 ? 'x1' : 'x2', l.x);
            ln.setAttribute(end === 1 ? 'y1' : 'y2', l.y);
          }
        };
        const onUp = () => {
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          const data = { papers: {}, domains: {} };
          gEl.querySelectorAll('.kn-node').forEach((n) => {
            const c = n.querySelector('circle');
            if (!c) return;
            const pos = { x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')) };
            if (n.dataset.paper !== undefined) data.papers[n.dataset.paper] = pos;
            else if (n.dataset.dom !== undefined) data.domains[n.dataset.dom] = pos;
          });
          try { localStorage.setItem(`graph:${knDomainId}`, JSON.stringify(data)); } catch (_) {}
          saveGraphLayout(knDomainId, data);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    });
  } catch (e) {
    gEl.innerHTML = `<div class="fc-error">图谱加载失败: ${escapeHtml(e.message)}</div>`;
  }
});

let dqaDomainId = null;
let dqaSessions = [];
let dqaSessionId = null;
let dqaImageB64 = null;
let dqaScope = new Set();

function renderDqaScope() {
  const el = document.getElementById('dqa-scope');
  const domains = state.domains || [];
  el.innerHTML = '<span>范围：</span>' + domains.map((d) => `
    <label class="${dqaScope.has(d.domain_id) ? 'active' : ''}">
      <input type="checkbox" data-did="${d.domain_id}" ${dqaScope.has(d.domain_id) ? 'checked' : ''}/>${escapeHtml(d.name)}
    </label>`).join('') + '<button class="btn btn-mini" id="dqa-scope-all">全部领域</button>' +
    '<span class="dqa-papers-label">可多选（跨领域）</span>';
  document.getElementById('dqa-scope-all').addEventListener('click', () => {
    dqaScope = new Set(domains.map((d) => d.domain_id));
    renderDqaScope();
  });
  el.querySelectorAll('input[data-did]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const did = Number(cb.dataset.did);
      if (cb.checked) dqaScope.add(did); else dqaScope.delete(did);
      if (!dqaScope.size) { dqaScope.add(did); cb.checked = true; }
      renderDqaScope();
    });
  });
}

async function openDomainQa(domainId) {
  dqaDomainId = domainId;
  dqaScope = new Set([domainId]);
  dqaAnchorPid = await resolveDqaAnchor(domainId);
  const d = state.domains.find((x) => x.domain_id === domainId);
  document.getElementById('dqa-title').textContent = `领域问答 · ${d ? d.name : domainId}`;
  document.getElementById('dqa-papers').innerHTML = '';
  if (!dqaAnchorPid) {
    document.getElementById('dqa-messages').innerHTML = '<div class="empty-hint">该领域暂无论文<br /><span class="sub">先导入论文后再使用领域问答</span></div>';
    document.getElementById('dqa-mask').classList.remove('hidden');
    dqaSessions = [];
    dqaSessionId = null;
    document.getElementById('dqa-session-select').innerHTML = '';
    return;
  }
  document.getElementById('dqa-messages').innerHTML = '<div class="empty-hint">跨论文问答：领域内所有论文共同作为材料<br /><span class="sub">支持 🧠 思考模式与 📷 图片提问</span></div>';
  document.getElementById('dqa-mask').classList.remove('hidden');
  try {
    dqaSessions = (await req(`/papers/${firstPaperOfDomain(domainId)}/qa/sessions?domain_id=${domainId}`))
      .filter((s) => s.paper_id === null);
  } catch (_) { dqaSessions = []; }
  if (!dqaSessions.length) {
    try {
      const r = await req(`/papers/${firstPaperOfDomain(domainId)}/qa/sessions`, {
        method: 'POST', body: JSON.stringify({ name: '领域问答', domain_wide: true,
                                               domain_id: domainId,
                                               domain_ids: [domainId] }),
      });
      dqaSessions = [{ session_id: r.session_id, name: r.name, paper_id: null }];
    } catch (_) {}
  }
  dqaSessionId = dqaSessions.length ? dqaSessions[0].session_id : null;
  document.getElementById('dqa-session-select').innerHTML = dqaSessions.map((s) =>
    `<option value="${s.session_id}">${escapeHtml(s.name)}</option>`).join('');
  renderDqaScope();
  dqaLoadHistory();
  renderDqaPapers();
}

async function renderDqaPapers() {
  const el = document.getElementById('dqa-papers');
  if (!dqaSessionId) { el.innerHTML = ''; return; }
  const cur = dqaSessions.find((s) => s.session_id === dqaSessionId);
  let selected = [];
  try { selected = JSON.parse(cur?.paper_ids_json || 'null') || []; } catch (_) { selected = []; }
  let papers = [];
  try { papers = await req(`/papers?domain_id=${dqaDomainId}`); } catch (_) {}
  el.innerHTML = '<span class="dqa-papers-label">对比范围（点击切换，全不选 = 全领域）：</span>' +
    papers.map((p) =>
      `<button class="dqa-paper-chip ${selected.includes(p.paper_id) ? 'active' : ''}" data-pid="${p.paper_id}" title="${escapeHtml(p.title || '')}">${escapeHtml((p.title || '').slice(0, 14))}</button>`).join('') +
    (selected.length ? `<span class="dqa-papers-label">已选 ${selected.length} 篇</span>` : '');
  el.querySelectorAll('.dqa-paper-chip').forEach((chip) => {
    chip.addEventListener('click', async () => {
      const pid = Number(chip.dataset.pid);
      const now = selected.includes(pid) ? selected.filter((x) => x !== pid) : [...selected, pid];
      try {
        await req(`/papers/${firstPaperOfDomain(dqaDomainId)}/qa/sessions/${dqaSessionId}`,
          { method: 'PUT', body: JSON.stringify({ paper_ids: now }) });
        dqaSessions = (await req(`/papers/${firstPaperOfDomain(dqaDomainId)}/qa/sessions?domain_id=${dqaDomainId}`))
          .filter((s) => s.paper_id === null);
        renderDqaPapers();
        showToast(now.length ? `对比范围：指定 ${now.length} 篇` : '对比范围：全领域');
      } catch (e) { showApiBanner('对比范围', e.message); }
    });
  });
}

let dqaAnchorPid = null;

async function resolveDqaAnchor(domainId) {
  const cachedOk = (state.papers || []).some((p) => p.paper_id === dqaAnchorPid && state.selectedDomain === domainId);
  if (cachedOk) return dqaAnchorPid;
  try {
    const papers = await req(`/papers?domain_id=${domainId}`);
    dqaAnchorPid = papers.length ? papers[0].paper_id : null;
  } catch (_) { dqaAnchorPid = null; }
  return dqaAnchorPid;
}

function firstPaperOfDomain() {
  return dqaAnchorPid;
}

document.getElementById('dqa-close').addEventListener('click', () => {
  document.getElementById('dqa-mask').classList.add('hidden');
});
document.getElementById('dqa-input').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.getElementById('dqa-mask').classList.add('hidden');
});
document.getElementById('dqa-session-select').addEventListener('change', (e) => {
  dqaSessionId = Number(e.target.value);
  const cur = dqaSessions.find((s) => s.session_id === dqaSessionId);
  try {
    const ids = JSON.parse(cur?.domain_ids_json || 'null');
    if (Array.isArray(ids) && ids.length) { dqaScope = new Set(ids); renderDqaScope(); }
  } catch (_) { /* keep current scope */ }
  dqaLoadHistory();
  renderDqaPapers();
});
document.getElementById('dqa-session-new').addEventListener('click', () => {
  const bar = document.getElementById('dqa-session-select').parentNode;
  if (document.getElementById('dqa-session-input')) return;
  const input = document.createElement('input');
  input.id = 'dqa-session-input';
  input.className = 'inline-input';
  input.placeholder = '会话名称…';
  input.style.maxWidth = '150px';
  const ok = document.createElement('button');
  ok.className = 'btn btn-mini btn-primary'; ok.textContent = '✓';
  const cancel = document.createElement('button');
  cancel.className = 'btn btn-mini'; cancel.textContent = '×';
  const sel = document.getElementById('dqa-session-select');
  sel.style.display = 'none';
  bar.insertBefore(input, sel); bar.insertBefore(ok, sel); bar.insertBefore(cancel, sel);
  input.focus();
  const cleanup = () => { input.remove(); ok.remove(); cancel.remove(); sel.style.display = ''; };
  const create = async () => {
    const name = input.value.trim() || '新会话';
    cleanup();
    try {
      const r = await req(`/papers/${firstPaperOfDomain(dqaDomainId)}/qa/sessions`, {
        method: 'POST', body: JSON.stringify({ name, domain_wide: true,
                                               domain_id: dqaDomainId,
                                               domain_ids: [...dqaScope] }),
      });
      dqaSessionId = r.session_id;
      dqaSessions = (await req(`/papers/${firstPaperOfDomain(dqaDomainId)}/qa/sessions?domain_id=${dqaDomainId}`))
        .filter((s) => s.paper_id === null);
      document.getElementById('dqa-session-select').innerHTML = dqaSessions.map((s) =>
        `<option value="${s.session_id}" ${s.session_id === dqaSessionId ? 'selected' : ''}>${escapeHtml(s.name)}</option>`).join('');
      dqaLoadHistory();
    } catch (e) { alert(`创建会话失败: ${e.message}`); }
  };
  ok.addEventListener('click', create);
  cancel.addEventListener('click', cleanup);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') create();
    if (e.key === 'Escape') cleanup();
  });
});
document.getElementById('dqa-think').addEventListener('click', (e) => e.currentTarget.classList.toggle('active'));
document.getElementById('dqa-img').addEventListener('click', () => document.getElementById('dqa-img-file').click());
document.getElementById('dqa-img-file').addEventListener('change', (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    dqaImageB64 = String(reader.result).split(',')[1];
    document.getElementById('dqa-img').classList.add('active');
    document.getElementById('dqa-img').textContent = '📷 已附图';
  };
  reader.readAsDataURL(f);
});

async function dqaLoadHistory() {
  const box = document.getElementById('dqa-messages');
  box.innerHTML = '';
  if (!dqaSessionId) return;
  try {
    const hist = await req(`/papers/${firstPaperOfDomain(dqaDomainId)}/qa/history?session_id=${dqaSessionId}`);
    for (const turn of hist) {
      const u = document.createElement('div');
      u.className = 'qa-msg qa-user';
      u.textContent = turn.question;
      const a = document.createElement('div');
      a.className = 'qa-msg qa-assistant';
      a.textContent = turn.answer;
      box.appendChild(u); box.appendChild(a);
      if (turn.refs) a.appendChild(makeRefChips(turn.refs, null));
    }
    box.scrollTop = box.scrollHeight;
  } catch (_) {}
}

document.getElementById('dqa-send').addEventListener('click', async () => {
  const input = document.getElementById('dqa-input');
  const question = input.value.trim();
  if (!question && !dqaImageB64) return;
  const thinking = document.getElementById('dqa-think').classList.contains('active');
  input.value = '';
  const box = document.getElementById('dqa-messages');
  box.querySelector('.empty-hint')?.remove();
  const userBubble = document.createElement('div');
  userBubble.className = 'qa-msg qa-user';
  userBubble.textContent = question + (dqaImageB64 ? '　[📷]' : '') + (thinking ? '　[🧠]' : '');
  const bubble = document.createElement('div');
  bubble.className = 'qa-msg qa-assistant streaming';
  box.appendChild(userBubble); box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
  try {
    const resp = await fetch(api(`/papers/${firstPaperOfDomain(dqaDomainId)}/qa`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question || '请解释这张图片。', session_id: dqaSessionId,
                             thinking, image_b64: dqaImageB64,
                             domain_id: dqaDomainId,
                             domain_ids: [...dqaScope],
                             scope: dqaScope.size > 1 ? 'domains' : 'domain' }),
    });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      throw new Error(errBody.detail || `${resp.status}`);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (document.getElementById('dqa-mask').classList.contains('hidden')) { await reader.cancel(); break; }
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!line.startsWith('data:')) continue;
        const obj = JSON.parse(line.slice(5));
        if (obj.token) { bubble.textContent += obj.token; box.scrollTop = box.scrollHeight; }
        if (obj.refs) bubble.appendChild(makeRefChips(obj.refs, null));
        if (obj.error) bubble.textContent += `\n[错误] ${obj.error}`;
        if (obj.done) bubble.classList.remove('streaming');
      }
    }
  } catch (e) {
    bubble.textContent = `请求失败: ${e.message}`;
    showApiBanner('', e.message);
  } finally {
    dqaImageB64 = null;
    const imgBtn = document.getElementById('dqa-img');
    imgBtn.classList.remove('active');
    imgBtn.textContent = '📷 图片';
    bubble.classList.remove('streaming');
  }
});

let searchTimer = null;
function attachDomainSearch(domainId) {
  const old = document.getElementById('domain-search-wrap');
  if (old) old.remove();
  const wrap = document.createElement('div');
  wrap.id = 'domain-search-wrap';
  wrap.innerHTML = '<input id="domain-search" class="inline-input" placeholder="域内全文搜索（≥2 字符）…" style="margin-bottom:8px" />';
  const list = document.getElementById('domain-list');
  list.parentNode.insertBefore(wrap, list);
  const input = wrap.querySelector('input');
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (q.length < 2) { renderPapers({ keepSearch: true }); return; }
    searchTimer = setTimeout(async () => {
      try {
        const results = await req(`/domains/${domainId}/search?q=${encodeURIComponent(q)}`);
        const el = document.getElementById('domain-list');
        el.innerHTML = (results.length ? results.map((r) => `
          <div class="paper-item" data-pid="${r.paper_id}" data-pg="${r.page}">
            <div class="paper-item-title">${escapeHtml(r.title)}</div>
            <div class="hl-quote">${escapeHtml(r.snippet)}</div>
          </div>`).join('') : '<div class="empty-hint">无结果</div>');
        el.querySelectorAll('[data-pid]').forEach((item) => {
          item.addEventListener('click', () => {
            const pid = Number(item.dataset.pid), pg = Number(item.dataset.pg);
            openPaper(pid).then(() => { if (pg) jumpToPage(pg); });
          });
        });
      } catch (_) { /* transient */ }
    }, 400);
  });
}

async function renameDomain(id) {
  const item = document.querySelector(`.domain-item[data-id="${id}"]`);
  if (!item) return;
  item.querySelector('.merge-box').classList.add('hidden');
  const nameEl = item.querySelector('.domain-name');
  const old = nameEl.textContent;
  nameEl.innerHTML = `<input class="inline-input" value="${escapeHtml(old)}" />`;
  const input = nameEl.querySelector('input');
  input.focus();
  input.select();
  let renameDone = false;
  const commit = async () => {
    if (renameDone) return;
    renameDone = true;
    const v = input.value.trim();
    if (v && v !== old) {
      try { await req(`/domains/${id}/rename`, { method: 'POST', body: JSON.stringify({ name: v }) }); }
      catch (e) { alert(`重命名失败: ${e.message}`); }
    }
    await loadDomains();
    if (state.selectedDomain) await selectDomain(state.selectedDomain);
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') loadDomains(); });
  input.addEventListener('blur', commit);
  req(`/domains/${id}/auto-name`, { method: 'POST' })
    .then((r) => {
      if (!renameDone && input.value === old) { input.value = r.name; input.select(); }
    })
    .catch(() => {});
  const autoBtn = document.createElement('button');
  autoBtn.className = 'btn btn-mini';
  autoBtn.textContent = '自动';
  autoBtn.title = '按领域论文自动命名（类别众数/高频词）';
  autoBtn.addEventListener('mousedown', (e) => e.preventDefault());
  autoBtn.addEventListener('click', async () => {
    try {
      const r = await req(`/domains/${id}/auto-name`, { method: 'POST' });
      input.value = r.name;
      await commit();
      showToast(`已自动命名：${r.name}`);
    } catch (e) { alert(e.message); }
  });
  nameEl.appendChild(autoBtn);
}

function toggleMergeBox(id) {
  document.querySelectorAll('.merge-box').forEach((b) => { if (b.dataset.id !== String(id)) b.classList.add('hidden'); });
  const item = document.querySelector(`.domain-item[data-id="${id}"]`);
  const box = item.querySelector('.merge-box');
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  const others = state.domains.filter((d) => d.domain_id !== id);
  if (!others.length) { box.innerHTML = '<div class="domain-meta">没有可合并的目标领域</div>'; box.classList.remove('hidden'); return; }
  box.innerHTML = `
    <select class="inline-input">${others.map((o) => `<option value="${o.domain_id}">${escapeHtml(o.name)} (${o.paper_count})</option>`).join('')}</select>
    <button class="btn btn-mini btn-primary">确认合并</button>`;
  box.classList.remove('hidden');
  box.querySelector('button').addEventListener('click', async (e) => {
    e.stopPropagation();
    const dstName = box.querySelector('select').selectedOptions[0].textContent;
    if (!confirm(`合并「${state.domains.find((d) => d.domain_id === id)?.name}」→「${dstName}」？此操作不可撤销。`)) return;
    if (!confirm(`再次确认：合并不可撤销，论文将直接改变领域归属。继续？`)) return;
    const dst = Number(box.querySelector('select').value);
    try {
      await req('/domains/merge', { method: 'POST', body: JSON.stringify({ src_id: id, dst_id: dst }) });
    } catch (err) { alert(`合并失败: ${err.message}`); }
    state.selectedDomain = null;
    await loadDomains();
  });
}

async function toggleSplitBox(id) {
  document.querySelectorAll('.split-box').forEach((b) => { if (b.dataset.id !== String(id)) b.classList.add('hidden'); });
  const item = document.querySelector(`.domain-item[data-id="${id}"]`);
  const box = item.querySelector('.split-box');
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  box.innerHTML = '<div class="domain-meta">加载论文…</div>';
  box.classList.remove('hidden');
  try {
    const papers = await req(`/domains/${id}/papers`);
    if (!papers.length) { box.innerHTML = '<div class="domain-meta">该领域暂无论文</div>'; return; }
    box.innerHTML = `
      <input class="inline-input split-name" placeholder="新领域名称" />
      <div class="split-list">
        <label class="split-line split-all"><input type="checkbox" data-split-all checked /> 全选</label>
        ${papers.map((p) => `<label class="split-line"><input type="checkbox" data-split-paper="${p.paper_id}" checked /> ${escapeHtml((p.title || '(untitled)').slice(0, 42))}</label>`).join('')}
      </div>
      <button class="btn btn-mini btn-primary">确认拆分</button>`;
    const all = box.querySelector('[data-split-all]');
    all.addEventListener('change', () => {
      box.querySelectorAll('[data-split-paper]').forEach((cb) => { cb.checked = all.checked; });
    });
    box.querySelector('button').addEventListener('click', async (e) => {
      e.stopPropagation();
      const name = box.querySelector('.split-name').value.trim();
      const ids = [...box.querySelectorAll('[data-split-paper]:checked')].map((cb) => Number(cb.dataset.splitPaper));
      if (!name) { alert('请填写新领域名称'); return; }
      if (!ids.length) { alert('请至少选择一篇论文'); return; }
      if (!confirm(`将 ${ids.length} 篇论文拆分为「${name}」？`)) return;
      try {
        const r = await req(`/domains/${id}/split`, {
          method: 'POST', body: JSON.stringify({ name, paper_ids: ids }),
        });
        showToast(`已拆分为「${r.name}」`);
        await loadDomains();
      } catch (err) { alert(`拆分失败: ${err.message}`); }
    });
  } catch (err) {
    box.innerHTML = `<div class="fc-error">加载失败: ${escapeHtml(err.message)}</div>`;
  }
}

async function deleteDomain(id) {
  const d = state.domains.find((x) => x.domain_id === id);
  if (!confirm(`删除领域「${d.name}」？其中 ${d.paper_count} 篇论文将由系统重新自动归属。`)) return;
  if (!confirm(`再次确认：删除操作不可撤销，该领域的质心与弱关联记录将被清除。确定删除？`)) return;
  try {
    await req(`/domains/${id}`, { method: 'DELETE' });
    state.selectedDomain = null;
    await loadDomains();
  } catch (e) { alert(`删除失败: ${e.message}`); }
}

async function selectDomain(id) {
  const changed = state.selectedDomain !== id;
  state.selectedDomain = id;
  state.papers = await req(`/domains/${id}/papers`);
  state.papersSig = JSON.stringify(state.papers);
  try {
    state.domainOverview = await req(`/domains/${id}/overview`);
  } catch (_) { state.domainOverview = null; }
  if (changed || !state.domainConfig) {
    state.domainConfig = null;
    try {
      const cfg = await req(`/domains/${id}/config`);
      state.domainConfig = cfg;
      loadHlColors(cfg);
    } catch (_) { /* defaults are fine */ }
    hlFilter = 'all';
  }
  renderPapers();
  if (changed && currentPaperId) {
    if (state.papers.some((p) => p.paper_id === currentPaperId)) {
      await openPaper(currentPaperId);
    } else {
      closeAllTabs();
    }
  }
}

function domainOverviewHtml(ov) {
  if (!ov) return '';
  const goals = ['', '写 related work', '找 idea', '快速了解', '审稿', '教学备课'];
  const core = new Set(ov.core_papers || []);
  const coreChips = (ov.core_papers || []).map((pid) => {
    const p = state.papers.find((x) => x.paper_id === pid);
    return p ? `<button class="ov-chip" data-nav="${pid}">★ ${escapeHtml((p.title || '').slice(0, 16))}</button>` : '';
  }).join('');
  const nav = (ov.navigation || []).slice(0, 4).map((n) =>
    `<button class="ov-chip" data-nav="${n.paper_id}" title="${escapeHtml(n.reason)}">${escapeHtml((n.title || '').slice(0, 18))}</button>`).join('');
  const subs = (ov.subtopics || []).slice(0, 6).map((s) =>
    `<span class="ov-chip" title="论文数">${escapeHtml(s.name)}<em>${s.n_papers || 0}</em></span>`).join('');
  const qs = (ov.open_questions || []).slice(0, 5).map((q) =>
    `<div class="ov-line" title="${escapeHtml(q.text || '')}">· ${escapeHtml((q.text || '').slice(0, 44))}</div>`).join('');
  const rs = (ov.pending_reviews || []).slice(0, 5).map((r) =>
    `<div class="ov-line" title="${escapeHtml(r.source_title || '')}">· ${escapeHtml((r.target_title || r.target_doi || '未命名').slice(0, 40))}</div>`).join('');
  const bridges = (ov.concept_overlaps || []).slice(0, 4).map((c) =>
    `<span class="ov-chip ov-bridge" title="${escapeHtml((c.definitions || []).map((d) => `${d.domain}：${d.definition || '未填写'}`).join('；'))}">${escapeHtml(c.term)} ↔ ${escapeHtml((c.definitions || []).map((d) => d.domain).join(' / '))}</span>`).join('');
  const lv = ov.last_view || null;
  let last = null;
  try { last = Number(localStorage.getItem(`lastPaper:${ov.domain.domain_id}`)) || null; } catch (_) {}
  const lastId = (lv && lv.paper_id) || last;
  const lastPaper = lastId
    ? (state.papers.find((p) => p.paper_id === lastId)
       || (lv && lv.paper_id === lastId ? { paper_id: lv.paper_id, title: lv.title } : null))
    : null;
  const lastPage = lv && lv.paper_id === lastId ? lv.last_page : null;
  return `<div class="domain-overview">
    <div class="ov-stats"><b>${ov.n_papers}</b> 篇 · 未决 ${(ov.open_questions || []).length} · 待核 ${(ov.pending_reviews || []).length}</div>
    <label class="ov-row">研究目标
      <select id="ov-goal">${goals.map((g) => `<option value="${escapeHtml(g)}" ${g === (ov.domain.research_goal || '') ? 'selected' : ''}>${g || '未设置'}</option>`).join('')}</select>
    </label>
    ${subs ? `<div class="ov-row">子主题：${subs}</div>` : ''}
    ${coreChips ? `<div class="ov-row">核心：${coreChips}</div>` : ''}
    ${nav ? `<div class="ov-row">导航：${nav}</div>` : ''}
    ${bridges ? `<div class="ov-row">跨领域概念：${bridges}</div>` : ''}
    ${qs ? `<div class="ov-block"><b>未决问题</b>${qs}</div>` : ''}
    ${rs ? `<div class="ov-block"><b>待复核引用</b>${rs}</div>` : ''}
    ${lastPaper ? `<div class="ov-resume" data-nav="${lastPaper.paper_id}">继续上次：${escapeHtml((lastPaper.title || '').slice(0, 26))}${lastPage ? ` · p.${lastPage}` : ''}</div>` : ''}
  </div>`;
}

function sortedPapers() {
  return state.papers.slice().sort((a, b) => {
    const ap = Number(a.is_primary ? 1 : 0);
    const bp = Number(b.is_primary ? 1 : 0);
    if (ap !== bp) return bp - ap;
    return (b.paper_id || 0) - (a.paper_id || 0);
  });
}

function paperListHtml(domain) {
  const back = `<div class="domain-item active" id="btn-back"><div class="domain-name">← ${escapeHtml(domain.name)}</div><div class="domain-meta">返回领域列表</div></div>`;
  const ov = state.domainOverview;
  const core = new Set(ov?.core_papers || []);
  const tools = `<div class="paper-domain-targets">拖到领域：
    ${state.domains.filter((d) => d.domain_id !== domain.domain_id).map((d) =>
      `<button class="domain-target btn btn-mini" data-domain-target="${d.domain_id}" title="把论文拖到这里，移动主领域">${escapeHtml(d.name)}</button>`).join('')}
  </div>`;
  const rows = sortedPapers();
  const items = rows.length
    ? rows.map((p) => `
      <div class="paper-item" data-id="${p.paper_id}" draggable="true">
        <div class="paper-item-title">${escapeHtml(p.title || '(untitled)')}${p.is_primary ? '' : '<span class="badge badge-secondary" title="兼属：该领域不是这篇论文的主领域">兼属</span>'}</div>
        ${p.abstract ? `<div class="paper-item-abstract">${escapeHtml(p.abstract.slice(0, 120))}${p.abstract.length > 120 ? '…' : ''}</div>` : ''}
        <div class="paper-item-meta">
          <span class="badge badge-${p.parse_status}">${parseStatusText(p.parse_status)}</span>
          ${(p.parse_status === 'parsing' || p.parse_status === 'queued') && p.progress > 0
            ? `<span class="progress-badge">${Math.round(p.progress)}%</span>` : ''}
          <span>${p.year || ''}</span>
          ${p.arxiv_id ? `<span>${p.arxiv_id}</span>` : ''}
          <span class="cited-count" title="库内被引次数">被引 <span class="cited-num">${p.cited_by || 0}</span></span>
          ${p.pending_reviews ? `<span class="badge badge-hl-limitation" title="有未处理的待复核引用">待核 ${p.pending_reviews}</span>` : ''}
          ${p.parse_error ? `<span class="parse-error" title="${escapeHtml(p.parse_error)}">!</span>` : ''}
          ${p.parse_status === 'failed' ? `<button class="btn btn-mini" data-retry="${p.paper_id}" title="重新排队解析">重试</button>` : ''}
          <button class="btn btn-mini" data-core-paper="${p.paper_id}" title="${core.has(p.paper_id) ? '取消核心论文' : '设为核心论文'}">${core.has(p.paper_id) ? '★' : '☆'}</button>
          <button class="btn btn-mini btn-move" data-move="${p.paper_id}" title="移动到其他领域（交互式归属，重新归属时跳过）">移</button>
        </div>
        <div class="move-box hidden" data-id="${p.paper_id}"></div>
      </div>`).join('')
    : '<div class="empty-hint">该领域暂无论文<br /><span class="sub">点击「导入论文」试试</span></div>';
  return back + domainOverviewHtml(ov) + tools + items;
}

function renderPapers(opts = {}) {
  const el = document.getElementById('domain-list');
  const domain = state.domains.find((d) => d.domain_id === state.selectedDomain);
  if (!domain) return;
  el.innerHTML = paperListHtml(domain);
  document.getElementById('btn-back').addEventListener('click', () => { state.selectedDomain = null; renderDomains(); });
  el.querySelectorAll('[data-domain-target]').forEach((target) => {
    target.addEventListener('dragover', (e) => {
      if (e.dataTransfer.types.includes('application/x-scholar-paper')) {
        e.preventDefault();
        target.classList.add('drop-target');
      }
    });
    target.addEventListener('dragleave', () => target.classList.remove('drop-target'));
    target.addEventListener('drop', async (e) => {
      target.classList.remove('drop-target');
      const pid = Number(e.dataTransfer.getData('application/x-scholar-paper'));
      const did = Number(target.dataset.domainTarget);
      if (!pid || !did) return;
      e.preventDefault();
      try {
        await req(`/papers/${pid}/move`, { method: 'POST', body: JSON.stringify({ domain_id: did }) });
        const name = state.domains.find((d) => d.domain_id === did)?.name || did;
        showToast(`已移动到「${name}」`);
        await loadDomains();
        await selectDomain(state.selectedDomain);
      } catch (err) { showToast(`移动失败：${err.message}`); }
    });
  });
  const goalSel = document.getElementById('ov-goal');
  if (goalSel) goalSel.addEventListener('change', async () => {
    try {
      await req(`/domains/${domain.domain_id}/goal`, {
        method: 'PUT',
        body: JSON.stringify({ research_goal: goalSel.value,
                               core_papers: state.domainOverview?.core_papers || [] }),
      });
      domain.research_goal = goalSel.value;
      showToast(`研究目标：${goalSel.value || '未设置'}`);
    } catch (e) { showApiBanner('研究目标', e.message); }
  });
  el.querySelectorAll('[data-nav]').forEach((b) => {
    b.addEventListener('click', () => openPaper(Number(b.dataset.nav)));
  });
  el.querySelectorAll('[data-core-paper]').forEach((b) => {
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const pid = Number(b.dataset.corePaper);
      const core = new Set(state.domainOverview?.core_papers || []);
      if (core.has(pid)) core.delete(pid); else core.add(pid);
      try {
        await req(`/domains/${domain.domain_id}/goal`, {
          method: 'PUT',
          body: JSON.stringify({ research_goal: domain.research_goal || '',
                                 core_papers: [...core] }),
        });
        if (state.domainOverview) state.domainOverview.core_papers = [...core];
        renderPapers();
      } catch (err) { showApiBanner('核心论文', err.message); }
    });
  });
  if (!opts.keepSearch) attachDomainSearch(state.selectedDomain);
  el.querySelectorAll('.paper-item').forEach((item) => {
    item.addEventListener('click', (e) => {
      if (e.target.dataset.move || e.target.dataset.status) return;
      if (item.dataset.dragged === '1') return;
      openPaper(Number(item.dataset.id));
    });
    item.addEventListener('dragstart', (e) => {
      e.stopPropagation();
      e.dataTransfer.setData('application/x-scholar-paper', String(item.dataset.id));
      e.dataTransfer.effectAllowed = 'move';
      item.dataset.dragged = '1';
      item.classList.add('dragging');
    });
    item.addEventListener('dragend', () => {
      item.classList.remove('dragging');
      setTimeout(() => { item.dataset.dragged = '0'; }, 50);
    });
  });
  el.querySelectorAll('[data-move]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleMoveBox(Number(btn.dataset.move));
    });
  });
  el.querySelectorAll('[data-retry]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        await req(`/papers/${btn.dataset.retry}/retry`, { method: 'POST' });
        showToast('已重新排队解析');
        await loadDomains();
        if (state.selectedDomain) await selectDomain(state.selectedDomain);
      } catch (err) { alert(`重试失败: ${err.message}`); }
    });
  });
}

function toggleMoveBox(paperId) {
  document.querySelectorAll('.move-box').forEach((b) => {
    if (b.dataset.id !== String(paperId)) b.classList.add('hidden');
  });
  const box = document.querySelector(`.move-box[data-id="${paperId}"]`);
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  const others = state.domains.filter((d) => d.domain_id !== state.selectedDomain);
  if (!others.length) { box.innerHTML = '<div class="domain-meta">没有其他领域可移动</div>'; box.classList.remove('hidden'); return; }
  box.innerHTML = `
    <select class="inline-input">${others.map((o) => `<option value="${o.domain_id}">${escapeHtml(o.name)}</option>`).join('')}</select>
    <button class="btn btn-mini btn-primary">确认移动</button>`;
  box.classList.remove('hidden');
  box.querySelector('button').addEventListener('click', async (e) => {
    e.stopPropagation();
    const dst = Number(box.querySelector('select').value);
    const dstName = state.domains.find((d) => d.domain_id === dst)?.name;
    if (!confirm(`将这篇论文移动到「${dstName}」？\n之后「全部重新归属」会跳过它（可视为你的归属判断）。`)) return;
    try {
      await req(`/papers/${paperId}/move`, { method: 'POST', body: JSON.stringify({ domain_id: dst }) });
      box.classList.add('hidden');
      await loadDomains();
      showToast(`已移动到「${dstName}」`);
    } catch (err) { alert(`移动失败: ${err.message}`); }
  });
}

function parseStatusText(s) {
  return { queued: '排队', parsing: '解析中', done: '完成', failed: '失败' }[s] || s;
}

const modal = document.getElementById('modal-mask');
document.getElementById('btn-discover').addEventListener('click', openDiscover);

async function openDiscover() {
  try {
    const r = await req('/domains/discover');
    const threshold = Number(r.assign_threshold || 0.5);
    const domainOpts = state.domains.map((d) =>
      `<option value="${d.domain_id}">调整到 ${escapeHtml(d.name)}</option>`).join('');
    const cards = (r.clusters || []).map((c, i) => {
      const papers = (c.papers || c.paper_ids.map((pid, k) => ({
        paper_id: pid, title: (c.titles || [])[k] || '', confidence: c.confidence,
      })));
      const rows = papers.map((p) => {
        const low = Number(p.confidence) < threshold;
        return `<div class="cluster-paper ${low ? 'low' : ''}" data-paper-row="${p.paper_id}">
          <input type="checkbox" data-paper="${p.paper_id}" ${low ? '' : 'checked'} />
          <span class="cp-title">${escapeHtml((p.title || '(untitled)').slice(0, 56))}</span>
          <span class="cp-conf">${(Number(p.confidence) * 100).toFixed(0)}%</span>
          ${low ? '<span class="cp-pending">待确认</span>' : ''}
          <select data-adjust="${p.paper_id}"><option value="">接受</option>${domainOpts}</select>
        </div>`;
      }).join('');
      return `<div class="cluster-card" data-cluster="${i}">
        <label class="cluster-head">
          <input type="checkbox" data-cluster-all="${i}" checked />
          <input class="cl-name inline-input" value="${escapeHtml(c.name)}" />
          <span class="dqa-papers-label">${c.size} 篇 · 平均置信度 ${(c.confidence * 100).toFixed(0)}%</span>
        </label>
        <div class="cluster-papers">${rows}</div>
        <div class="cluster-actions">
          <button class="btn btn-mini" data-accept-all="${i}">接受全部（含待确认）</button>
          <span class="kn-note">低置信度默认不勾选；可逐篇接受或用右侧下拉调整到已有领域。</span>
        </div>
      </div>`;
    }).join('') || '<div class="empty-hint">没有可聚类的论文</div>';
    openTool('领域发现 · 预览', `
      <div class="kn-note" style="margin-bottom:8px">基于论文向量做 k-means 聚类。勾选=加入新领域；下拉选择=调整到已有领域（作为主领域）。置信度低于 ${(threshold * 100).toFixed(0)}% 的论文标为待确认。</div>
      <div class="cluster-list">${cards}</div>
      <div class="tool-actions" style="margin-top:10px">
        <button class="btn btn-mini btn-primary" id="discover-apply">应用选中聚类</button>
      </div>`);
    document.querySelectorAll('[data-cluster-all]').forEach((all) => {
      all.addEventListener('change', () => {
        const card = all.closest('.cluster-card');
        card.querySelectorAll('[data-paper]').forEach((cb) => { cb.checked = all.checked; });
      });
    });
    document.querySelectorAll('[data-accept-all]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const card = btn.closest('.cluster-card');
        card.querySelectorAll('[data-paper]').forEach((cb) => { cb.checked = true; });
        card.querySelector('[data-cluster-all]').checked = true;
      });
    });
    document.getElementById('discover-apply').addEventListener('click', async () => {
      const clusters = [];
      const adjustments = [];
      document.querySelectorAll('.cluster-card').forEach((card) => {
        const name = card.querySelector('.cl-name').value.trim();
        const ids = [];
        card.querySelectorAll('[data-paper]').forEach((cb) => {
          const pid = Number(cb.dataset.paper);
          const adj = card.querySelector(`[data-adjust="${pid}"]`)?.value;
          if (adj) {
            adjustments.push({ paper_id: pid, domain_id: Number(adj) });
            return;
          }
          if (cb.checked) ids.push(pid);
        });
        if (ids.length) clusters.push({ name, paper_ids: ids });
      });
      if (!clusters.length && !adjustments.length) {
        showToast('没有选中的论文');
        return;
      }
      try {
        const out = await req('/domains/discover/apply', {
          method: 'POST', body: JSON.stringify({ clusters, adjustments }),
        });
        closeTool();
        showToast(`创建 ${out.created.length} · 调整 ${out.adjusted.length} · 关联 ${out.linked}`);
        await loadDomains();
      } catch (e) { showApiBanner('领域发现', e.message); }
    });
  } catch (e) { showApiBanner('领域发现', e.message); }
}

document.getElementById('btn-import').addEventListener('click', () => {
    modal.classList.remove('hidden');
    document.getElementById('modal-error').classList.add('hidden');
    document.getElementById('batch-result').classList.add('hidden');
    const sel = document.getElementById('import-domain-select');
    sel.innerHTML = '<option value="">默认：自动归属（按阈值）</option>' +
      state.domains.map((d) => `<option value="${d.domain_id}">手动归属 → ${escapeHtml(d.name)}</option>`).join('');
  });
document.getElementById('btn-batch-import').addEventListener('click', async () => {
  const btn = document.getElementById('btn-batch-import');
  const resEl = document.getElementById('batch-result');
  const q = document.getElementById('batch-query').value.trim();
  const domSel = document.getElementById('import-domain-select').value;
  if (!q && !domSel) {
    resEl.textContent = '留空关键词时必须选择目标领域（术语表扩充模式）';
    resEl.classList.remove('hidden');
    return;
  }
  btn.disabled = true; btn.textContent = '检索中…';
  resEl.textContent = '正在检索 arXiv（限速 3s/请求，请稍候）…';
  resEl.classList.remove('hidden');
  try {
    const r = await req('/arxiv/batch-import', {
      method: 'POST',
      body: JSON.stringify({ query: q,
        max_results: Number(document.getElementById('batch-count').value),
        domain_id: domSel ? Number(domSel) : null }),
    });
    resEl.textContent = `查询「${r.query}」：命中 ${r.found}，新导入 ${r.queued}，跳过已入库 ${r.skipped_existing}（后台解析中）`;
    document.getElementById('batch-query').value = '';
    await loadDomains();
    if (state.selectedDomain) await selectDomain(state.selectedDomain);
    if (apiPort) pollHealth(apiPort);
  } catch (e) {
    resEl.textContent = `批量导入失败: ${e.message}`;
  } finally {
    btn.disabled = false; btn.textContent = '搜索并导入';
  }
});

document.getElementById('btn-cancel-import').addEventListener('click', () => modal.classList.add('hidden'));
document.getElementById('btn-browse').addEventListener('click', async () => {
  if (!window.scholarlens) {
    document.getElementById('modal-error').textContent = '文件选择仅在应用内可用，浏览器预览请手动粘贴路径';
    document.getElementById('modal-error').classList.remove('hidden');
    return;
  }
  const paths = await window.scholarlens.selectPdfs();
  if (paths && paths.length) {
    const ta = document.getElementById('input-local');
    ta.value = [...ta.value.split('\n').map((s) => s.trim()).filter(Boolean), ...paths].join('\n');
  }
});
document.getElementById('btn-submit-import').addEventListener('click', async () => {
  const arxivLines = document.getElementById('input-arxiv').value.split('\n').map((s) => s.trim()).filter(Boolean);
  const localLines = document.getElementById('input-local').value.split('\n').map((s) => s.trim()).filter(Boolean);
  try {
    const r = await req('/papers/import', {
      method: 'POST',
      body: JSON.stringify({ arxiv_ids: arxivLines, local_paths: localLines,
                             domain_id: document.getElementById('import-domain-select').value ? Number(document.getElementById('import-domain-select').value) : null }),
    });
    modal.classList.add('hidden');
    document.getElementById('input-arxiv').value = '';
    document.getElementById('input-local').value = '';
    await loadDomains();
    if (state.selectedDomain) await selectDomain(state.selectedDomain);
    if (apiPort) pollHealth(apiPort);
  } catch (e) {
    const errEl = document.getElementById('modal-error');
    errEl.textContent = `导入失败: ${e.message}`;
    errEl.classList.remove('hidden');
  }
});

document.getElementById('btn-new-domain').addEventListener('click', async () => {
  try {
    await req('/domains', { method: 'POST', body: JSON.stringify({ name: '' }) });
    await loadDomains();
  } catch (e) {
    alert(`创建领域失败: ${e.message}`);
  }
});

const settingsMask = document.getElementById('settings-mask');
const CFG_FIELDS = {
  assign_threshold: 'cfg-assign-th', link_threshold: 'cfg-link-th',
  domain_dispersion_weight: 'cfg-dispersion',
  max_tokens_default: 'cfg-max-tokens', max_output_limit: 'cfg-max-limit',
  prompt_summary: 'cfg-prompt-summary', prompt_qa: 'cfg-prompt-qa',
  prompt_highlight: 'cfg-prompt-highlight',
  prompt_translate: 'cfg-prompt-translate', prompt_explain: 'cfg-prompt-explain',
};
let editProfiles = [];
let editProfileIdx = 0;
let activeProfileName = '';

document.querySelectorAll('.set-tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.set-tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.settings-page').forEach((p) => p.classList.add('hidden'));
    tab.classList.add('active');
    document.querySelector(`.settings-page[data-page="${tab.dataset.set}"]`).classList.remove('hidden');
  });
});

function renderProfileList() {
  const el = document.getElementById('profile-list');
  el.innerHTML = editProfiles.map((p, i) => `
    <div class="profile-row-item ${i === editProfileIdx ? 'active' : ''}" data-idx="${i}">
      <span class="profile-name">${escapeHtml(p.name || `profile-${i + 1}`)}</span>
      ${p.name === activeProfileName ? '<span class="profile-active">使用中</span>' : ''}
      <span class="profile-models">${escapeHtml(p.model_fast || '')}</span>
    </div>`).join('');
  el.querySelectorAll('.profile-row-item').forEach((row) => {
    row.addEventListener('click', () => {
      collectProfileFields();
      editProfileIdx = Number(row.dataset.idx);
      renderProfileList();
      renderProfileEditor();
    });
  });
}

function renderProfileEditor() {
  const p = editProfiles[editProfileIdx] || {};
  document.getElementById('pf-name').value = p.name || '';
  document.getElementById('pf-api-base').value = p.api_base || '';
  const keyInput = document.getElementById('pf-api-key');
  keyInput.value = '';
  keyInput.placeholder = p.clear_key ? '保存后清除已保存的 Key'
    : (p.has_key ? '已保存（留空则不修改）' : 'sk-…');
  document.getElementById('pf-model-fast').value = p.model_fast || '';
  document.getElementById('pf-model-pro').value = p.model_pro || '';
  document.getElementById('pf-thinking').value = p.thinking || 'disabled';
}

function collectProfileFields() {
  const old = editProfiles[editProfileIdx] || {};
  const key = document.getElementById('pf-api-key').value.trim();
  const next = {
    ...old,
    name: document.getElementById('pf-name').value.trim(),
    api_base: document.getElementById('pf-api-base').value.trim(),
    api_key: key,
    model_fast: document.getElementById('pf-model-fast').value.trim(),
    model_pro: document.getElementById('pf-model-pro').value.trim(),
    thinking: document.getElementById('pf-thinking').value,
  };
  if (key) next.clear_key = false;
  editProfiles[editProfileIdx] = next;
}

document.getElementById('pf-clear-key').addEventListener('click', () => {
  collectProfileFields();
  const p = editProfiles[editProfileIdx] || {};
  p.api_key = '';
  p.has_key = false;
  p.clear_key = true;
  editProfiles[editProfileIdx] = p;
  renderProfileEditor();
  showToast('保存后清除该档案的 Key');
});

document.getElementById('pf-api-key').addEventListener('input', () => {
  const p = editProfiles[editProfileIdx];
  if (p && p.clear_key && document.getElementById('pf-api-key').value.trim()) {
    p.clear_key = false;
  }
});

document.getElementById('btn-profile-add').addEventListener('click', () => {
  collectProfileFields();
  editProfiles.push({ name: `Vendor-${editProfiles.length + 1}`, api_base: 'https://api.deepseek.com',
    api_key: '', model_fast: 'deepseek-flash', model_pro: 'deepseek-flash', thinking: 'disabled' });
  editProfileIdx = editProfiles.length - 1;
  renderProfileList();
  renderProfileEditor();
});
document.getElementById('btn-profile-del').addEventListener('click', () => {
  if (editProfiles.length <= 1) { alert('至少保留一个厂商档案'); return; }
  editProfiles.splice(editProfileIdx, 1);
  editProfileIdx = 0;
  renderProfileList();
  renderProfileEditor();
});

document.getElementById('btn-settings').addEventListener('click', async () => {
  try {
    const [cfg, prof] = await Promise.all([req('/config'), req('/llm/profiles')]);
    for (const [key, id] of Object.entries(CFG_FIELDS)) {
      document.getElementById(id).value = cfg[key] ?? '';
    }
    editProfiles = prof.profiles || [];
    activeProfileName = prof.active || '';
    editProfileIdx = Math.max(0, editProfiles.findIndex((p) => p.name === activeProfileName));
    renderProfileList();
    renderProfileEditor();
    document.getElementById('settings-error').classList.add('hidden');
    settingsMask.classList.remove('hidden');
  } catch (e) {
    alert(`读取配置失败: ${e.message}`);
  }
});
document.getElementById('btn-cancel-settings').addEventListener('click', () => settingsMask.classList.add('hidden'));

document.getElementById('btn-th-auto').addEventListener('click', async () => {
  try {
    const r = await req('/domains/auto-threshold', { method: 'POST' });
    document.getElementById('cfg-assign-th').value = r.assign_threshold;
    document.getElementById('cfg-link-th').value = r.link_threshold;
    alert(`已按 ${r.n_papers} 篇论文的同域相似度分布估计：\n` +
      `assign_threshold = ${r.assign_threshold}（P10，同域分布 ${r.same_domain_sim_min}–${r.same_domain_sim_max}）\n` +
      `link_threshold = ${r.link_threshold}（assign × 0.7）\n保存设置后生效；可点「全部重新归属」应用。`);
  } catch (e) { alert(`自动调整失败: ${e.message}`); }
});
document.getElementById('btn-th-reset').addEventListener('click', async () => {
  try {
    const r = await req('/domains/reset-threshold', { method: 'POST' });
    document.getElementById('cfg-assign-th').value = r.assign_threshold;
    document.getElementById('cfg-link-th').value = r.link_threshold;
    showToast('已恢复默认阈值 0.5 / 0.35');
  } catch (e) { alert(`恢复默认失败: ${e.message}`); }
});
document.getElementById('btn-th-reassign').addEventListener('click', async () => {
  if (!confirm('按当前阈值对所有论文重新归属？\n（用户手动「移」过的论文会被跳过）\n此操作会改变多篇论文的领域归属。')) return;
  try {
    const r = await req('/domains/reassign', { method: 'POST' });
    await loadDomains();
    showToast(`重新归属完成：${r.moved.length} 篇论文移动`);
  } catch (e) { alert(`重新归属失败: ${e.message}`); }
});
document.getElementById('btn-save-settings').addEventListener('click', async () => {
  try {
    collectProfileFields();
    const activeName = editProfiles[editProfileIdx]?.name;
    await req('/llm/profiles', {
      method: 'PUT',
      body: JSON.stringify({ profiles: editProfiles, active: activeName }),
    });
    const values = {};
    for (const [key, id] of Object.entries(CFG_FIELDS)) {
      values[key] = document.getElementById(id).value;
    }
    await req('/config', { method: 'PUT', body: JSON.stringify({ values }) });
    settingsMask.classList.add('hidden');
  } catch (e) {
    const errEl = document.getElementById('settings-error');
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
});

window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 16);
pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdf.worker.min.js';
let currentPaperId = null;
let currentPaperDomainId = null;

function withDomain(path) {
  if (!currentPaperDomainId) return path;
  return path.includes('?')
    ? `${path}&domain_id=${currentPaperDomainId}`
    : `${path}?domain_id=${currentPaperDomainId}`;
}
let currentPdfDoc = null;
let currentHighlights = [];
let viewerScale = 1.4;
let totalPages = 0;
let renderGen = 0;
const openTabs = [];        // [{paper_id, title}]
let activeTabId = null;
const pdfDocCache = new Map();

function cachePdfDoc(paperId, doc) {
  if (pdfDocCache.has(paperId)) pdfDocCache.delete(paperId);
  pdfDocCache.set(paperId, doc);
  while (pdfDocCache.size > 3) {
    const oldest = pdfDocCache.keys().next().value;
    if (oldest === paperId) break;
    const old = pdfDocCache.get(oldest);
    pdfDocCache.delete(oldest);
    try { old.destroy(); } catch (_) {}
  }
}

function renderTabs() {
  const bar = document.getElementById('reader-tabs');
  bar.classList.toggle('hidden', openTabs.length === 0);
  bar.innerHTML = openTabs.map((t) => `
    <div class="reader-tab ${t.paper_id === activeTabId ? 'active' : ''}" data-id="${t.paper_id}">
      <span class="tab-title" title="${escapeHtml(t.title)}">${escapeHtml(t.title || '加载中…')}</span>
      <button class="tab-close" data-close="${t.paper_id}" title="关闭">×</button>
    </div>`).join('');
  bar.querySelectorAll('.reader-tab').forEach((tab) => {
    const id = Number(tab.dataset.id);
    tab.addEventListener('click', (e) => {
      if (e.target.dataset.close) return;
      if (id !== activeTabId) openPaper(id);
    });
  });
  bar.querySelectorAll('[data-close]').forEach((btn) => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); closeTab(Number(btn.dataset.close)); });
  });
}

function closeTab(id) {
  const idx = openTabs.findIndex((t) => t.paper_id === id);
  if (idx < 0) return;
  openTabs.splice(idx, 1);
  if (activeTabId === id) {
    const next = openTabs[idx] || openTabs[idx - 1];
    if (next) openPaper(next.paper_id);
    else closeAllTabs();
  } else {
    renderTabs();
  }
}

function closeAllTabs() {
  saveReadingPos();
  ++renderGen;
  exitCropMode();
  openTabs.length = 0;
  activeTabId = null;
  currentPaperId = null;
  currentPdfDoc = null;
  currentHighlights = [];
  document.getElementById('paper-title').textContent = '未打开论文';
  document.getElementById('tab-summary').innerHTML = '<div class="empty-hint">打开论文后显示五段式摘要</div>';
  document.getElementById('tab-highlights').innerHTML = '<div class="empty-hint">三类高亮：背景蓝 / 解决思路绿 / 局限红</div>';
  renderTabs();
  showReaderLayer('placeholder');
  loadOverview();
  renderQaHistory({});
}

async function loadOverview() {
  try {
    const o = await req('/overview');
    document.getElementById('home-stats').innerHTML = `
      <div class="stat-card"><div class="stat-num">${o.n_domains}</div><div class="stat-label">研究领域</div></div>
      <div class="stat-card"><div class="stat-num">${o.n_papers}</div><div class="stat-label">论文总量</div></div>
      <div class="stat-card"><div class="stat-num">${o.n_parsing}</div><div class="stat-label">处理队列中</div></div>
      <div class="stat-card"><div class="stat-num">${o.n_highlights}</div><div class="stat-label">累计高亮</div></div>`;
    const recent = document.getElementById('home-recent');
    if (!o.recent.length) {
      recent.innerHTML = '<div class="empty-hint" style="padding:16px">还没有论文 — 点击下方「导入论文」开始</div>';
    } else {
      recent.innerHTML = o.recent.map((r) => `
        <div class="recent-item" data-id="${r.paper_id}">
          <span class="badge badge-${r.parse_status}">${parseStatusText(r.parse_status)}</span>
          <span class="recent-title" title="${escapeHtml(r.title)}">${escapeHtml(r.title)}</span>
          <span class="recent-meta">${escapeHtml(r.domain_name || '未归属')} · ${Number(r.year) || ''}</span>
        </div>`).join('');
      recent.querySelectorAll('.recent-item').forEach((item) => {
        item.addEventListener('click', () => openPaper(Number(item.dataset.id)));
      });
    }
    if (o.queue) updateBatchLine(o.queue.batch_total, o.queue.batch_done);
    return true;
  } catch (_) {
    return false;
  }
}

function showReaderLayer(which) {  // 'placeholder' | 'progress' | 'pdf' | 'error'
  const layers = {
    placeholder: document.getElementById('reader-placeholder'),
    progress: document.getElementById('reader-progress'),
    pdf: document.getElementById('pdf-container'),
    error: document.getElementById('reader-error'),
  };
  for (const [k, el] of Object.entries(layers)) el.classList.toggle('hidden', k !== which);
  document.getElementById('pager').classList.toggle('hidden', which !== 'pdf');
  ['btn-skim', 'btn-outline', 'btn-card', 'btn-export-paper'].forEach((id) => {
    const b = document.getElementById(id);
    if (b) b.classList.toggle('hidden', which !== 'pdf');
  });
}

function showReaderError(title, message, retry) {
  showReaderLayer('error');
  document.getElementById('reader-error-title').textContent = title;
  document.getElementById('reader-error-msg').textContent = message || '';
  const btn = document.getElementById('reader-error-retry');
  btn.onclick = retry || null;
  btn.classList.toggle('hidden', !retry);
}

function stageText(pct) {
  if (pct < 5) return '下载中…（arXiv 限流时最长约 2-3 分钟，请耐心等待）';
  if (pct < 50) return '下载中…';
  if (pct < 90) return '解析中（阅读顺序 / 分块 / 索引）…';
  return '构建领域索引…';
}

function showProgressCard(p) {
  showReaderLayer('progress');
  const bar = document.getElementById('reader-progress-bar');
  const pctEl = document.getElementById('reader-progress-pct');
  const stage = document.getElementById('reader-progress-stage');
  const pct = p.progress || 0;
  bar.style.width = `${pct}%`;
  pctEl.textContent = `本篇 ${Math.round(pct)}%`;
  stage.textContent = stageText(pct);
}
document.getElementById('reader-progress-cancel').addEventListener('click', async () => {
  if (!currentPaperId) return;
  try {
    await req(`/papers/${currentPaperId}/cancel`, { method: 'POST' });
    showToast('已请求取消（解析中会在下一个阶段点退出）');
  } catch (e) { showApiBanner('取消', e.message); }
});

function updateProgressCard(pct) {
  document.getElementById('reader-progress-bar').style.width = `${pct}%`;
  document.getElementById('reader-progress-pct').textContent = `本篇 ${Math.round(pct)}%`;
  document.getElementById('reader-progress-stage').textContent = stageText(pct);
}

function updateBatchLine(total, done) {
  const el = document.getElementById('reader-progress-batch');
  const qEl = document.getElementById('status-queue');
  if (total > 0 && done < total) {
    el.textContent = `本批：已完成 ${done} / ${total} 篇`;
    qEl.textContent = `本批 ${done}/${total}`;
  } else {
    el.textContent = '';
    if (total > 0 && done >= total) qEl.textContent = '';
  }
}

const openPaperInflight = new Map();

function openPaper(paperId) {
  if (openPaperInflight.has(paperId)) return openPaperInflight.get(paperId);
  const p = doOpenPaper(paperId);
  openPaperInflight.set(paperId, p);
  p.finally(() => openPaperInflight.delete(paperId)).catch(() => {});
  return p;
}

async function doOpenPaper(paperId) {
  saveReadingPos();
  exitCropMode();
  const gen = ++renderGen;
  activeTabId = paperId;
  if (!openTabs.find((t) => t.paper_id === paperId)) openTabs.push({ paper_id: paperId, title: '' });
  const sel = state.selectedDomain ? `?domain_id=${state.selectedDomain}` : '';
  const p = await req(`/papers/${paperId}${sel}`);
  if (gen !== renderGen) return;
  currentPaperId = paperId;
  const viewed = (p.domains || []).find((d) => d.domain_id === state.selectedDomain);
  currentPaperDomainId = p.view_domain_id
    || (viewed && viewed.domain_id) || p.domain_id || null;
  state.readingPosition = p.position
    || (p.positions || []).find((x) => x.domain_id === currentPaperDomainId) || null;
  state.outline = null;
  if (currentPaperDomainId) {
    try { localStorage.setItem(`lastPaper:${currentPaperDomainId}`, String(paperId)); } catch (_) {}
  }
  req(`/papers/${paperId}/touch`, { method: 'POST' }).then(loadOverview).catch(() => {});
  if (currentPaperDomainId) {
    try {
      const cfg = await req(`/domains/${currentPaperDomainId}/config`);
      state.domainConfig = cfg;
      loadHlColors(cfg);
      hlFilter = 'all';
      const stored = Number(cfg.highlight_density);
      if (Number.isFinite(stored) && stored >= 0) hlDensity = stored;
    } catch (_) { /* keep the local preference */ }
  }
  const tab = openTabs.find((t) => t.paper_id === paperId);
  if (tab && tab.title !== p.title) { tab.title = p.title || '(untitled)'; }
  renderTabs();
  document.getElementById('paper-title').textContent = p.title || '(untitled)';
  document.querySelector('.tab[data-tab="summary"]').click();
  const safe = async (elId, what, fn) => {
    try {
      await fn();
    } catch (e) {
      console.error(`[open] ${what} failed:`, e);
      const el = document.getElementById(elId);
      if (el) el.innerHTML = `<div class="empty-hint">${what}加载失败<br /><span class="sub">${escapeHtml(e.message || '')}</span></div>`;
    }
  };
  await safe('tab-summary', '摘要', () => renderSummaryTab(p));
  await safe('tab-highlights', '高亮', () => renderHighlightsTab(p));
  await safe('qa-messages', '问答会话', () => renderQaSessions(paperId));
  await safe('tab-notes', '笔记', () => renderNotesTab(p));
  await safe('qa-messages', '问答历史', () => renderQaHistory(p));
  if (p.parse_status === 'done') {
    try {
      await renderPdf(p);
    } catch (e) {
      console.error('[open] pdf failed:', e);
      showReaderError('PDF 打开失败', e.message || '文件缺失或已损坏。',
        () => openPaper(p.paper_id));
    }
  } else if (p.parse_status === 'failed') {
    showReaderError('论文解析失败', p.parse_error || '解析过程中断。可以重新排队解析。',
      async () => {
        try {
          await req(`/papers/${p.paper_id}/retry`, { method: 'POST' });
          showToast('已重新排队解析');
          await openPaper(p.paper_id);
        } catch (err) { alert(`重试失败: ${err.message}`); }
      });
  } else {
    showProgressCard(p);
  }
}

async function renderPdf(p) {
  const gen = ++renderGen;
  showReaderLayer('pdf');
  const container = document.getElementById('pdf-container');
  container.innerHTML = '';
  if (pdfDocCache.has(p.paper_id)) {
    currentPdfDoc = pdfDocCache.get(p.paper_id);
    pdfDocCache.delete(p.paper_id);
    cachePdfDoc(p.paper_id, currentPdfDoc);
  } else {
    currentPdfDoc = await pdfjsLib.getDocument({ url: api(`/papers/${p.paper_id}/pdf`) }).promise;
    cachePdfDoc(p.paper_id, currentPdfDoc);
  }
  if (gen !== renderGen) return;
  totalPages = currentPdfDoc.numPages;
  updatePageIndicator(1);
  let pageFailures = 0;
  for (let i = 1; i <= totalPages; i++) {
    try {
      await Promise.race([
        renderPage(i),
        new Promise((_, rej) => setTimeout(() => rej(new Error('render timeout')), 20000)),
      ]);
    } catch (e) {
      console.warn(`page ${i} render failed:`, e.message);
      pageFailures += 1;
    }
    if (gen !== renderGen) return;
  }
  if (pageFailures) {
    console.warn(`[reader] ${pageFailures}/${totalPages} pages failed to render`);
  }
  try {
    currentHighlights = await req(withDomain(`/papers/${p.paper_id}/highlights`));
    if (gen !== renderGen) return;
    await applyAllHighlights();
  } catch (e) {
    console.warn('[reader] highlight layer failed:', e.message);
  }
  if (gen !== renderGen) return;
  restoreReadingPos(p.paper_id);
  try {
    await buildLinkHotspots();
  } catch (e) {
    console.warn('[reader] link hotspot layer failed:', e.message);
  }
}

let cropMode = null;  // {wrap, box, x0, y0, page, moved}

function enterCropMode() {
  if (!currentPdfDoc) return false;
  exitCropMode();
  cropMode = { phase: 'anchor' };
  document.getElementById('pdf-container').classList.add('crop-mode');
  const hint = document.createElement('div');
  hint.className = 'crop-hint';
  hint.innerHTML = '框选截图：单击定起点 → 移动 → 再单击锁定 · <button class="btn btn-mini" id="crop-exit">取消（Esc）</button>';
  document.body.appendChild(hint);
  document.getElementById('crop-exit').addEventListener('click', exitCropMode);
  return true;
}

function exitCropMode() {
  cropMode = null;
  document.getElementById('pdf-container')?.classList.remove('crop-mode');
  document.querySelector('.crop-hint')?.remove();
  document.querySelectorAll('.crop-box, .crop-confirm').forEach((el) => el.remove());
}

async function attachCrop(b64) {
  qaImageB64 = b64;
  const imgBtn = document.getElementById('qa-img');
  imgBtn.classList.add('active');
  imgBtn.textContent = '📷 已框选';
  exitCropMode();
  document.querySelector('.tab[data-tab="qa"]').click();
  document.getElementById('qa-input')?.focus();
  showToast('已附上截图，可输入问题后发送（可留空）');
}

async function cropHighRes(pageNo, rect) {
  try {
    const page = await currentPdfDoc.getPage(pageNo);
    const scale = 3.0;
    const viewport = page.getViewport({ scale });
    const off = document.createElement('canvas');
    off.width = viewport.width;
    off.height = viewport.height;
    await page.render({ canvasContext: off.getContext('2d'), viewport }).promise;
    const k = scale / viewerScale;
    const out = document.createElement('canvas');
    out.width = Math.max(1, Math.round(rect.w * k));
    out.height = Math.max(1, Math.round(rect.h * k));
    out.getContext('2d').drawImage(off, rect.x * k, rect.y * k, rect.w * k, rect.h * k,
      0, 0, out.width, out.height);
    return out.toDataURL('image/png').split(',')[1];
  } catch (e) {
    const canvas = pageWrap(pageNo)?.querySelector('canvas');
    if (!canvas) throw e;
    const out = document.createElement('canvas');
    out.width = Math.max(1, Math.round(rect.w));
    out.height = Math.max(1, Math.round(rect.h));
    out.getContext('2d').drawImage(canvas, rect.x, rect.y, rect.w, rect.h, 0, 0, out.width, out.height);
    return out.toDataURL('image/png').split(',')[1];
  }
}

const pdfContainerEl = document.getElementById('pdf-container');
pdfContainerEl.addEventListener('mousedown', (e) => {
  if (!cropMode || cropMode.phase === 'confirm') return;
  const wrap = e.target.closest('.pdf-page');
  if (!wrap) return;
  e.preventDefault();
  e.stopPropagation();
  const canvas = wrap.querySelector('canvas');
  if (!canvas) return;
  const cr = canvas.getBoundingClientRect();
  const x = Math.min(Math.max(0, e.clientX - cr.left), cr.width);
  const y = Math.min(Math.max(0, e.clientY - cr.top), cr.height);
  if (!cropMode.box) {
    cropMode = { wrap, page: Number(wrap.dataset.page), cr, phase: 'draw',
                 x0: x, y0: y, box: null, rect: null };
    const box = document.createElement('div');
    box.className = 'crop-box';
    wrap.appendChild(box);
    cropMode.box = box;
  } else {
    const rect = cropMode.rect;
    if (!rect || rect.w < 16 || rect.h < 16) {
      cropMode.box.remove();
      cropMode.box = null;
      cropMode.rect = null;
      cropMode.phase = 'anchor';
      return;
    }
    cropMode.phase = 'confirm';
    const confirmEl = document.createElement('div');
    confirmEl.className = 'crop-confirm';
    confirmEl.style.left = `${Math.min(rect.x + rect.w, wrap.clientWidth - 130)}px`;
    confirmEl.style.top = `${Math.min(rect.y + rect.h + 6, wrap.clientHeight - 40)}px`;
    confirmEl.innerHTML = `<button class="btn btn-mini btn-primary" data-ok>✓ 截取</button>
      <button class="btn btn-mini" data-no>重选</button>`;
    wrap.appendChild(confirmEl);
    confirmEl.querySelector('[data-ok]').addEventListener('click', async (ev) => {
      ev.stopPropagation();
      confirmEl.remove();
      cropMode.box?.remove();
      try {
        const b64 = await cropHighRes(cropMode.page, rect);
        await attachCrop(b64);
      } catch (err) {
        showApiBanner('截取', err.message);
        exitCropMode();
      }
    });
    confirmEl.querySelector('[data-no]').addEventListener('click', (ev) => {
      ev.stopPropagation();
      confirmEl.remove();
      cropMode.box?.remove();
      cropMode.box = null;
      cropMode.rect = null;
      cropMode.phase = 'anchor';
    });
  }
});
document.addEventListener('mousemove', (e) => {
  if (!cropMode || cropMode.phase !== 'draw' || !cropMode.box) return;
  const { cr, x0, y0, box } = cropMode;
  const x1 = Math.min(Math.max(0, e.clientX - cr.left), cr.width);
  const y1 = Math.min(Math.max(0, e.clientY - cr.top), cr.height);
  const bx = Math.min(x0, x1), by = Math.min(y0, y1);
  cropMode.rect = { x: bx, y: by, w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
  box.style.left = `${bx}px`;
  box.style.top = `${by}px`;
  box.style.width = `${cropMode.rect.w}px`;
  box.style.height = `${cropMode.rect.h}px`;
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && cropMode) exitCropMode();
});

async function renderPage(pageNo) {
  pageLinkRects.delete(pageNo);
  const page = await currentPdfDoc.getPage(pageNo);
  const viewport = page.getViewport({ scale: viewerScale });
  const wrap = document.createElement('div');
  wrap.className = 'pdf-page';
  wrap.dataset.page = pageNo;
  wrap.style.setProperty('--scale-factor', viewport.scale);
  const canvas = document.createElement('canvas');
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  wrap.appendChild(canvas);
  document.getElementById('pdf-container').appendChild(wrap);
  await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
  const tc = await page.getTextContent();
  const tl = document.createElement('div');
  tl.className = 'textLayer';
  wrap.appendChild(tl);
  const task = pdfjsLib.renderTextLayer({ textContentSource: tc, container: tl, viewport, textDivs: [] });
  await task.promise;
  linkifyTextLayer(tl, wrap);
}

function linkifyTextLayer(tl, wrap) {
  const re = /https?:\/\/[^\s"'<>]+/g;
  const base = wrap.getBoundingClientRect();
  const pg = Number(wrap.dataset.page);
  const rects = pageLinkRects.get(pg) || [];
  pageLinkRects.set(pg, rects);
  const nodes = [];
  let full = '';
  const walker = document.createTreeWalker(tl, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = node.nodeValue || '';
    if (!text) continue;
    nodes.push({ node, start: full.length });
    full += text;
  }
  if (!/https?:\/\//.test(full)) return;
  const locate = (pos) => {
    let lo = 0, hi = nodes.length - 1, found = nodes[0];
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (nodes[mid].start <= pos) { found = nodes[mid]; lo = mid + 1; }
      else hi = mid - 1;
    }
    return { node: found.node, offset: Math.max(0, pos - found.start) };
  };
  const seen = new Set();
  let m;
  re.lastIndex = 0;
  while ((m = re.exec(full)) !== null) {
    const url = m[0].replace(/[.,;:)\]]+$/, '');
    if (url.length < 8) continue;
    const a = locate(m.index);
    const b = locate(m.index + url.length);
    const range = document.createRange();
    try { range.setStart(a.node, a.offset); range.setEnd(b.node, b.offset); } catch (_) { continue; }
    for (const r of range.getClientRects()) {
      if (r.width < 6 || r.height < 6) continue;
      const left = r.left - base.left, top = r.top - base.top;
      const key = `${url}|${Math.round(left)}|${Math.round(top)}|${Math.round(r.width)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const el = document.createElement('div');
      el.className = 'link-hotspot';
      el.dataset.url = url;
      el.title = url;
      el.style.left = `${left}px`;
      el.style.top = `${top}px`;
      el.style.width = `${r.width}px`;
      el.style.height = `${r.height}px`;
      wrap.appendChild(el);
      rects.push({ x: left, y: top, w: r.width, h: r.height, url });
    }
  }
}

function pageWrap(pageNo) {
  return document.querySelector(`.pdf-page[data-page="${pageNo}"]`);
}

function updatePageIndicator(pageNo) {
  document.getElementById('page-indicator').textContent = `${pageNo} / ${totalPages}`;
}

function currentPage() {
  const container = document.getElementById('pdf-container');
  const mid = container.scrollTop + container.clientHeight / 2;
  let best = 1;
  for (const w of container.querySelectorAll('.pdf-page')) {
    if (w.offsetTop <= mid) best = Number(w.dataset.page);
  }
  return best;
}

function jumpToPage(pageNo) {
  const wrap = pageWrap(pageNo);
  if (wrap) {
    document.getElementById('pdf-container').scrollTop = wrap.offsetTop - 8;
    updatePageIndicator(pageNo);
  }
}

async function renderSummaryTab(p) {
  const el = document.getElementById('tab-summary');
  const s = await req(withDomain(`/papers/${p.paper_id}/summary`));
  const sum = s.summaries || {};
  if (p.parse_status !== 'done') {
    const pct = Math.round(p.progress || 0);
    el.innerHTML = `<div class="empty-hint">解析中 ${pct}%<br /><span class="sub">${stageText(pct)}</span></div>`;
    return;
  }
  const secs = (p.meta.sections || []).map((x) => `<li>${escapeHtml(x.title)}</li>`).join('');
  const attrAll = p.attribution || [];
  const attr = attrAll.slice(0, 8);
  const attrHtml = attr.length ? `
    <div class="attr-block" id="attr-block">
      <div class="attr-title">领域归属相似度</div>
      ${attr.map((a) => `
        <div class="attr-row" title="${escapeHtml(a.name)}">
          <span class="attr-name">${escapeHtml(a.name)}</span>
          <span class="attr-bar"><i style="width:${(Math.max(0, a.score) * 100).toFixed(1)}%"></i></span>
          <span class="attr-val">${a.score.toFixed(2)}</span>
        </div>`).join('')}
      <div class="attr-edit-actions"><button id="attr-edit" class="btn btn-mini">调整归属</button></div>
    </div>` : '';
  const bridges = (p.concept_overlaps || []).slice(0, 4).map((c) => {
    const defs = c.definitions || [];
    const other = defs.find((d) => d.domain_id !== p.domain_id);
    if (!other) return '';
    return `<button class="concept-bridge" data-bridge="${other.domain_id}" title="${escapeHtml(defs.map((d) => `${d.domain}：${d.definition || '未填写'}`).join('；'))}">${escapeHtml(c.term)} · ${escapeHtml(defs.map((d) => d.domain).join(' / '))}</button>`;
  }).join('');
  let html = `
    <div class="detail-row"><b>年份</b> ${Number(p.year) || '—'} | <b>arXiv</b> ${escapeHtml(p.arxiv_id || '本地导入')}</div>
    <div class="detail-row"><b>作者</b> ${escapeHtml(p.authors || '—')}</div>
    ${bridges ? `<div class="concept-bridge-row">跨领域概念：${bridges}</div>` : ''}
    ${attrHtml}
    <details class="abstract-fold"><summary>原始摘要</summary>
      <div class="sum-text">${escapeHtml(p.abstract || '—')}</div></details>
    <div class="sum-toolbar">
      <button class="btn btn-mini" data-g="five">五段式</button>
      <button class="btn btn-mini" data-g="para">一段话</button>
      <button class="btn btn-mini" data-g="onesent">一句话</button>
      ${sum.five_dropped_evidence ? `<span class="sum-warn" title="LLM 给出的佐证句未通过逐字校验，已丢弃">佐证校验丢弃 ${sum.five_dropped_evidence} 条</span>` : ''}
    </div>`;
  if (s.cache && s.cache.stale) {
    html += `<div class="sum-stale-warn">当前领域的研究目标为「${escapeHtml(s.cache.research_goal || '未设置')}」，
      下方摘要是按「${escapeHtml(s.cache.generated_for || '其他目标')}」生成的：
      <button class="btn btn-mini" data-g="five">按当前目标重新生成</button></div>`;
  }
  if (sum.five && sum.five.length) {
    html += sum.five.map((sec) => `
      <div class="sum-section">
        <div class="sum-section-title">${escapeHtml(sec.section)}</div>
        <div class="sum-text">${escapeHtml(sec.text)}</div>
        ${sec.evidence ? `<button class="evidence-chip" data-page="${sec.page || ''}"
            data-evidence="${escapeHtml(sec.evidence || '')}"
            title="${escapeHtml(sec.evidence)}">原文佐证${sec.page ? ` · p.${sec.page}` : ''} ▸</button>` : ''}
      </div>`).join('');
  } else {
    html += '<div class="empty-hint" style="padding:14px">点击上方「五段式」生成摘要（逐段生成，可实时查看进度）</div>';
    if (s.summary_error) {
      html += `<div class="sum-warn" style="margin:0 0 8px">上次自动摘要失败：${escapeHtml(s.summary_error)}</div>`;
    }
  }
  for (const [key, label] of [['para', '一段话'], ['onesent', '一句话']]) {
    if (sum[key]) {
      html += `<div class="sum-section"><div class="sum-section-title">${label}</div>
        <div class="sum-text">${escapeHtml(sum[key])}</div></div>`;
    }
  }
  el.innerHTML = html;
  el.querySelectorAll('[data-bridge]').forEach((b) => b.addEventListener('click', () => openKnowledge(Number(b.dataset.bridge))));
  el.querySelectorAll('[data-g]').forEach((b) => b.addEventListener('click', () => genSummary(p.paper_id, b.dataset.g, b)));
  el.querySelectorAll('.evidence-chip').forEach((b) => b.addEventListener('click', () => {
    const pg = Number(b.dataset.page);
    if (pg) { jumpToQuote(pg, b.dataset.evidence || ''); document.querySelector('.tab[data-tab="summary"]').blur(); }
  }));
  el.querySelector('#attr-edit')?.addEventListener('click', () => {
    const block = el.querySelector('#attr-block');
    block.innerHTML = `
      <div class="attr-title">调整归属 <span class="attr-sub">○ 主领域（唯一） · ☑ 兼属（数量不限）</span></div>
      ${attrAll.map((a) => `
        <div class="attr-row" data-did="${a.domain_id}">
          <input type="radio" name="attr-primary" data-role="primary" ${a.domain_id === p.domain_id ? 'checked' : ''} />
          <input type="checkbox" data-role="sec" ${a.linked ? 'checked' : ''} ${a.domain_id === p.domain_id ? 'disabled' : ''} />
          <span class="attr-name">${escapeHtml(a.name)}</span>
          <span class="attr-val">${a.score.toFixed(2)}</span>
        </div>`).join('')}
      <div class="attr-edit-actions">
        <button class="btn btn-mini" id="attr-cancel">取消</button>
        <button class="btn btn-mini btn-primary" id="attr-save">保存</button>
      </div>`;
    block.querySelector('#attr-cancel').addEventListener('click', () => renderSummaryTab(p));
    block.querySelector('#attr-save').addEventListener('click', async () => {
      const primary = Number(block.querySelector('[data-role="primary"]:checked').closest('.attr-row').dataset.did);
      const secondary = [...block.querySelectorAll('[data-role="sec"]:checked')]
        .map((c) => Number(c.closest('.attr-row').dataset.did));
      try {
        await req(`/papers/${p.paper_id}/domains`, {
          method: 'POST',
          body: JSON.stringify({ primary, secondary }),
        });
        showToast('归属已更新（人工判断，自动重归属不会覆盖）');
        await loadDomains();
        if (state.selectedDomain) await selectDomain(state.selectedDomain);
        await renderSummaryTab(await req(withDomain(`/papers/${p.paper_id}`)));
      } catch (e) { showApiBanner('归属', e.message); }
    });
  });
}

async function genSummary(paperId, granularity, btn) {
  const old = btn.textContent;
  btn.disabled = true;
  let progEl = document.getElementById('sum-progress');
  if (!progEl) {
    progEl = document.createElement('div');
    progEl.id = 'sum-progress';
    progEl.className = 'sum-progress';
    const anchor = btn.closest('.sum-toolbar') || btn.closest('.sum-stale-warn')
      || btn.parentElement;
    anchor.after(progEl);
  }
  progEl.classList.remove('hidden');
  progEl.innerHTML = '<div class="sum-progress-text">准备中…</div><div class="progress-track"><div id="sum-progress-bar"></div></div>';
  const setProg = (done, total, section) => {
    progEl.querySelector('.sum-progress-text').textContent = `生成中 ${done}/${total} · ${section}`;
    const bar = progEl.querySelector('#sum-progress-bar');
    if (bar) bar.style.width = `${Math.round((done / total) * 100)}%`;
  };
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn btn-mini btn-danger';
  cancelBtn.textContent = '取消';
  btn.after(cancelBtn);
  let cancelled = false;
  cancelBtn.addEventListener('click', () => {
    cancelled = true;
    try { req(`/papers/${paperId}/summary/cancel`, { method: 'POST' }); } catch (_) {}
  });
  try {
    const resp = await fetch(api(`/papers/${paperId}/summary/generate`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ granularity, domain_id: currentPaperDomainId }),
    });
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    outer: for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!line.startsWith('data:')) continue;
        const obj = JSON.parse(line.slice(5));
        if (obj.progress) setProg(obj.progress.done, obj.progress.total, obj.progress.section);
        if (obj.canceled) { cancelled = true; break outer; }
        if (obj.error) throw new Error(obj.error);
        if (obj.done) {
          progEl.classList.add('hidden');
          await renderSummaryTab(await req(withDomain(`/papers/${paperId}`)));
          break outer;
        }
      }
    }
    if (cancelled) showToast('已取消（已完成的段落留在缓存，重新生成近乎免费）');
  } catch (e) {
    if (!cancelled) alert(`摘要生成失败: ${e.message}`);
    progEl.classList.add('hidden');
  } finally {
    cancelBtn.remove();
    btn.textContent = old;
    btn.disabled = false;
  }
}

let qaSessions = [];
let qaSessionId = null;

async function renderQaSessions(paperId) {
  try {
    qaSessions = await req(withDomain(`/papers/${paperId}/qa/sessions`));
  } catch (_) { qaSessions = []; }
  if (qaSessionId && !qaSessions.find((s) => s.session_id === qaSessionId)) {
    qaSessionId = qaSessions.length ? qaSessions[0].session_id : null;
  }
  const sel = document.getElementById('qa-session-select');
  sel.innerHTML = qaSessions.map((s) =>
    `<option value="${s.session_id}" ${s.session_id === qaSessionId ? 'selected' : ''}>` +
    `${escapeHtml(s.name)}${s.paper_id === null ? '（跨论文）' : ''}</option>`).join('');
  const bindEl = document.getElementById('qa-binding');
  const cur = qaSessions.find((s) => s.session_id === qaSessionId);
  const pTitle = openTabs.find((t) => t.paper_id === paperId)?.title || '(untitled)';
  if (bindEl) bindEl.textContent = cur && cur.paper_id === null
    ? '🌐 跨论文比较 · 材料：本领域全部论文'
    : `📄 绑定论文：《${pTitle}》`;
}

document.getElementById('qa-session-select').addEventListener('change', async (e) => {
  qaSessionId = Number(e.target.value);
  if (currentPaperId) await renderQaHistory({ paper_id: currentPaperId });
});
document.getElementById('qa-session-new').addEventListener('click', () => {
  if (!currentPaperId) return;
  const bar = document.querySelector('.qa-session-bar');
  if (document.getElementById('qa-session-input')) return;
  const input = document.createElement('input');
  input.id = 'qa-session-input';
  input.className = 'inline-input';
  input.placeholder = '会话名称…';
  input.title = '直接输入 = 本论文会话；以 @ 开头 = 跨论文比较会话（材料为你选定的领域内论文）';
  input.style.flex = '1';
  const hint = document.createElement('span');
  hint.className = 'qa-session-hint';
  hint.textContent = '@ 开头 = 跨论文比较';
  const ok = document.createElement('button');
  ok.className = 'btn btn-mini btn-primary'; ok.textContent = '✓';
  const cancel = document.createElement('button');
  cancel.className = 'btn btn-mini'; cancel.textContent = '×';
  const sel = document.getElementById('qa-session-select');
  sel.style.display = 'none';
  bar.insertBefore(input, sel); bar.insertBefore(hint, sel); bar.insertBefore(ok, sel); bar.insertBefore(cancel, sel);
  input.focus();
  const cleanup = () => {
    input.remove(); ok.remove(); cancel.remove(); hint.remove();
    sel.style.display = '';
  };
  const create = async () => {
    const name = input.value.trim();
    const domainWide = name.startsWith('@');
    const finalName = (domainWide ? name.slice(1) : name).trim() || '新会话';
    cleanup();
    try {
      const r = await req(`/papers/${currentPaperId}/qa/sessions`, {
        method: 'POST',
        body: JSON.stringify({ name: finalName, domain_wide: domainWide,
                               domain_id: currentPaperDomainId }),
      });
      qaSessionId = r.session_id;
      await renderQaSessions(currentPaperId);
      await renderQaHistory({ paper_id: currentPaperId });
      showToast(domainWide ? '已创建跨论文会话' : '已创建会话');
    } catch (e) { alert(`创建会话失败: ${e.message}`); }
  };
  ok.addEventListener('click', create);
  cancel.addEventListener('click', cleanup);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') create();
    if (e.key === 'Escape') cleanup();
  });
});
document.getElementById('qa-session-del').addEventListener('click', async () => {
  if (!currentPaperId || !qaSessionId) return;
  const s = qaSessions.find((x) => x.session_id === qaSessionId);
  if (!confirm(`删除会话「${s.name}」及其全部问答记录？不可撤销。`)) return;
  try {
    await req(`/papers/${currentPaperId}/qa/sessions/${qaSessionId}`, { method: 'DELETE' });
    qaSessionId = null;
    await renderQaSessions(currentPaperId);
    await renderQaHistory({ paper_id: currentPaperId });
  } catch (e) { alert(`删除会话失败: ${e.message}`); }
});

function qaMessagesEl() { return document.getElementById('qa-messages'); }

function appendMsg(role, text) {
  const box = qaMessagesEl();
  box.querySelector('.empty-hint')?.remove();
  const div = document.createElement('div');
  div.className = `qa-msg qa-${role}`;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

function makeRefChips(refs, homePaperId) {
  const div = document.createElement('div');
  div.className = 'qa-refs';
  for (const r of refs || []) {
    const chip = document.createElement('button');
    chip.className = 'ref-chip';
    const note = r.source === 'note';
    chip.textContent = note
      ? `[${r.n}] 笔记 p.${r.page}`
      : (r.title ? `[${r.n}] ${r.title.slice(0, 12)}… p.${r.page}` : `[${r.n}] p.${r.page}`);
    chip.title = note ? `用户笔记\n${r.preview}` : (r.title ? `${r.title}\n${r.preview}` : r.preview);
    chip.addEventListener('click', () => {
      if (r.paper_id && r.paper_id !== homePaperId) openPaper(r.paper_id);
      else jumpToPage(r.page);
    });
    div.appendChild(chip);
  }
  return div;
}

function renderRefs(bubble, refs) {
  if (!refs || !refs.length) return;
  bubble.appendChild(makeRefChips(refs, currentPaperId));
}

function renderRefsCheck(bubble, check) {
  const invalid = (check && check.invalid) || [];
  if (!invalid.length) return;
  const div = document.createElement('div');
  div.className = 'qa-refs-warn';
  div.textContent = `⚠ 引用编号越界：[${invalid.join(', ')}]（本次材料只有 ${check.total} 条），` +
    '这些编号没有对应的原文证据，引用它们时请核对。';
  bubble.appendChild(div);
}

function renderImportanceButton(bubble, interactionId, important) {
  if (!interactionId) return;
  let wrap = bubble.querySelector(':scope > .qa-important');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'qa-important';
    wrap.appendChild(document.createElement('button'));
    bubble.appendChild(wrap);
  }
  const btn = wrap.querySelector('button');
  btn.className = `btn btn-mini${important ? ' active' : ''}`;
  btn.textContent = important ? '★ 已标重要（已同步笔记）' : '☆ 标为重要';
  btn.title = '标记后该轮问答会同步到本领域笔记汇总';
  btn.onclick = async () => {
    try {
      const r = await req(`/papers/${currentPaperId}/qa/turns/${interactionId}/important`, {
        method: 'POST', body: JSON.stringify({ important: !important }),
      });
      important = !!r.important;
      renderImportanceButton(bubble, interactionId, important);
      await renderNotesTab({ paper_id: currentPaperId });
      showToast(important ? '已标记重要并同步到笔记' : '已取消重要标记');
    } catch (e) { showApiBanner('标记重要', e.message); }
  };
}

async function renderQaHistory(p) {
  const box = qaMessagesEl();
  if (!currentPaperId) {
    document.querySelector('.qa-session-bar').classList.add('hidden');
    document.getElementById('qa-scope-row').classList.add('hidden');
    document.getElementById('qa-suggest').innerHTML = '';
    const bind = document.getElementById('qa-binding');
    if (bind) bind.textContent = '🗂 全部问答历史 · 只读（打开论文后按会话提问）';
    box.innerHTML = '';
    try {
      const hist = await req('/qa/history-all?limit=50');
      if (currentPaperId) return;
      if (!hist.length) {
        box.innerHTML = '<div class="empty-hint">还没有问答记录<br /><span class="sub">打开论文后提问，历史保存在各会话中</span></div>';
        return;
      }
      for (const turn of hist) {
        appendMsg('user', `【${(turn.paper_title || '').slice(0, 18)} · ${turn.session || '未命名'}】${turn.question}`);
        const bubble = appendMsg('assistant', turn.answer);
        renderRefs(bubble, turn.refs);
      }
    } catch (_) { box.innerHTML = '<div class="empty-hint">历史加载失败</div>'; }
    document.querySelector('.qa-input-row').classList.add('qa-readonly');
    return;
  }
  document.querySelector('.qa-input-row').classList.remove('qa-readonly');
  document.querySelector('.qa-session-bar').classList.remove('hidden');
  qaScopeOverride = null;
  qaScopeInferred = null;
  document.getElementById('qa-scope-row').classList.add('hidden');
  box.innerHTML = '<div class="empty-hint">基于当前论文的多轮问答<br /><span class="sub">结论自动附引用编号，可点击跳原文</span></div>';
  try {
    const hist = await req(`/papers/${p.paper_id}/qa/history${qaSessionId ? `?session_id=${qaSessionId}` : ''}`);
    if (p.paper_id !== currentPaperId) return;
    for (const turn of hist) {
      appendMsg('user', turn.question);
      const bubble = appendMsg('assistant', turn.answer);
      renderRefs(bubble, turn.refs);
      renderIncomplete(bubble, turn.incomplete);
      if (Array.isArray(turn.invalid_refs) && turn.invalid_refs.length) {
        renderRefsCheck(bubble, { total: (turn.refs || []).length, invalid: turn.invalid_refs });
      }
      renderImportanceButton(bubble, turn.interaction_id, !!turn.importance);
    }
  } catch (_) { /* ignore */ }
  loadQaSuggestions(p.paper_id);
}


let qaScopeInferred = null;
let qaScopeOverride = null;   // null = auto; {scope, domain_ids, label} = user-corrected
let qaLastAsk = null;         // {question, userBubble, bubble} for one-click correction

const SCOPE_NAMES = { paper: '当前论文', domain: '当前领域', domains: '跨领域', all: '全部领域' };

async function loadQaSuggestions(paperId) {
  const box = document.getElementById('qa-suggest');
  if (!box) return;
  box.innerHTML = '';
  try {
    const r = await req(withDomain(`/papers/${paperId}/qa/suggestions`));
    if (paperId !== currentPaperId || !r.suggestions || !r.suggestions.length) return;
    box.innerHTML = r.suggestions.map((s) =>
      `<button class="qa-suggest-chip" title="点击提问">${escapeHtml(s)}</button>`).join('');
    box.querySelectorAll('.qa-suggest-chip').forEach((b) => b.addEventListener('click', () => {
      document.getElementById('qa-input').value = b.textContent;
      sendQa();
    }));
  } catch (_) { /* suggestions are optional */ }
}

function renderIncomplete(bubble, incomplete) {
  if (!incomplete || !incomplete.length) return;
  const div = document.createElement('div');
  div.className = 'qa-incomplete';
  div.textContent = `⚠ 证据不完整：${incomplete.map((x) =>
    `${x.name || '#' + x.domain_id}（${x.reason}）`).join('、')}`;
  bubble.appendChild(div);
}

function renderScopeChip(info) {
  const row = document.getElementById('qa-scope-row');
  const btn = document.getElementById('qa-scope');
  if (!row || !btn || !info) return;
  qaScopeInferred = info;
  row.classList.remove('hidden');
  const label = info.label || SCOPE_NAMES[info.scope] || info.scope || '自动';
  btn.textContent = `范围：${label}`;
  btn.title = `${info.reason || ''}（点击修正）`;
  btn.classList.toggle('manual', !!(info.manual));
}

function scopeChoices() {
  const cur = currentPaperDomainId;
  const list = [
    { key: 'auto', label: '自动推断（默认）', value: null },
    { key: 'paper', label: '当前论文', value: { scope: 'paper', domain_ids: [], label: '当前论文' } },
  ];
  if (qaScopeInferred && qaScopeInferred.scope === 'domains') {
    list.push({ key: 'inferred', label: qaScopeInferred.label,
                value: { scope: 'domains', domain_ids: qaScopeInferred.domain_ids || [],
                         label: qaScopeInferred.label } });
  }
  list.push({ key: 'domain', label: '当前领域',
              value: { scope: 'domain', domain_ids: cur ? [cur] : [], label: '当前领域' } });
  list.push({ key: 'all', label: '全部领域',
              value: { scope: 'all', domain_ids: [], label: '全部领域' } });
  return list;
}

function toggleScopeMenu() {
  const menu = document.getElementById('qa-scope-menu');
  if (!menu.classList.contains('hidden')) { menu.classList.add('hidden'); return; }
  menu.innerHTML = scopeChoices().map((c) =>
    `<button class="qa-scope-opt" data-key="${c.key}">${escapeHtml(c.label)}</button>`).join('');
  menu.querySelectorAll('.qa-scope-opt').forEach((b) => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    menu.classList.add('hidden');
    const choice = scopeChoices().find((c) => c.key === b.dataset.key);
    qaScopeOverride = choice ? choice.value : null;
    if (qaScopeOverride) {
      renderScopeChip({ ...qaScopeOverride, manual: true, reason: '已手动修正' });
    } else {
      qaScopeInferred = null;
      document.getElementById('qa-scope-row').classList.add('hidden');
    }
    const last = qaLastAsk;
    if (last) {
      try { qaAbort?.abort(); } catch (_) { /* already finished */ }
      if (last.interactionId && currentPaperId) {
        try {
          await req(`/papers/${currentPaperId}/qa/turns/${last.interactionId}`,
                    { method: 'DELETE' });
        } catch (_) { /* keep the re-ask even if retraction failed */ }
      }
      if (last.userBubble && last.userBubble.parentNode) last.userBubble.remove();
      if (last.bubble && last.bubble.parentNode) last.bubble.remove();
      document.getElementById('qa-input').value = last.question;
      await sendQa();
    }
  }));
  menu.classList.remove('hidden');
}

document.getElementById('qa-scope').addEventListener('click', (e) => {
  e.stopPropagation();
  toggleScopeMenu();
});
document.getElementById('qa-scope-menu').addEventListener('click', (e) => e.stopPropagation());
document.addEventListener('click', () => document.getElementById('qa-scope-menu').classList.add('hidden'));

let qaAbort = null;
let qaThinkOn = false;
let qaImageB64 = null;

document.getElementById('qa-think').addEventListener('click', (e) => {
  qaThinkOn = !qaThinkOn;
  e.currentTarget.classList.toggle('active', qaThinkOn);
});
document.getElementById('qa-img').addEventListener('click', () => {
  if (cropMode) { exitCropMode(); return; }
  if (!enterCropMode()) document.getElementById('qa-img-file').click();
});
document.getElementById('qa-img-file').addEventListener('change', (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    qaImageB64 = String(reader.result).split(',')[1];
    document.getElementById('qa-img').classList.add('active');
    document.getElementById('qa-img').textContent = '📷 已附图';
  };
  reader.readAsDataURL(f);
});

async function sendQa() {
  const input = document.getElementById('qa-input');
  const question = input.value.trim();
  if (!question && !qaImageB64) return;
  if (!currentPaperId) return;
  input.value = '';
  const userBubble = appendMsg('user', question + (qaImageB64 ? '　[📷]' : '') + (qaThinkOn ? '　[🧠]' : ''));
  const bubble = appendMsg('assistant', '');
  const textEl = document.createElement('span');
  textEl.className = 'qa-text';
  bubble.appendChild(textEl);
  bubble.classList.add('streaming');
  qaLastAsk = { question, userBubble, bubble, interactionId: null };
  const ask = qaLastAsk;
  document.getElementById('qa-send').disabled = true;
  document.getElementById('qa-stop').classList.remove('hidden');
  qaAbort = new AbortController();
  const myAbort = qaAbort;
  let scopeFields = {};
  if (!qaImageB64) {
    try {
      if (qaScopeOverride) {
        scopeFields = { scope: qaScopeOverride.scope,
                        domain_ids: qaScopeOverride.domain_ids || [],
                        persist_scope: false };
      } else {
        const inf = await req(`/papers/${currentPaperId}/qa/scope/infer`,
          { method: 'POST', body: JSON.stringify({ question,
                                                    domain_id: currentPaperDomainId }) });
        if (currentPaperId && inf) {
          scopeFields = { scope: inf.scope, domain_ids: inf.domain_ids || [],
                          persist_scope: false };
          renderScopeChip(inf);
        }
      }
    } catch (_) { scopeFields = {}; /* 推断失败 → 后端按会话默认范围回答 */ }
  }
  try {
    const resp = await fetch(api(`/papers/${currentPaperId}/qa`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question || '请解释这张图片（来自论文）。', session_id: qaSessionId,
                             thinking: qaThinkOn, image_b64: qaImageB64,
                             domain_id: currentPaperDomainId, ...scopeFields }),
      signal: qaAbort.signal,
    });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      throw new Error(errBody.detail || `${resp.status}`);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let refsCheck = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!line.startsWith('data:')) continue;
        const obj = JSON.parse(line.slice(5));
        if (obj.token) {
          textEl.textContent += obj.token;
          qaMessagesEl().scrollTop = qaMessagesEl().scrollHeight;
        }
        if (obj.noanswer) textEl.textContent = `⛔ ${obj.reason}`;
        if (obj.refs) {
          renderRefs(bubble, obj.refs);
          renderIncomplete(bubble, obj.incomplete);
        }
        if (obj.refs_check) refsCheck = obj.refs_check;
        if (obj.error) textEl.textContent += `\n[错误] ${obj.error}`;
        if (obj.done) {
          bubble.classList.remove('streaming');
          if (obj.done.interaction_id) ask.interactionId = obj.done.interaction_id;
          if (refsCheck) renderRefsCheck(bubble, refsCheck);
          if (ask.interactionId) renderImportanceButton(bubble, ask.interactionId, false);
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      userBubble.remove();
      bubble.remove();
      if (qaLastAsk === ask) qaLastAsk = null;
      if (!qaMessagesEl().children.length) {
        qaMessagesEl().innerHTML = '<div class="empty-hint">问答已停止</div>';
      }
    } else {
      bubble.textContent = `请求失败: ${e.message}`;
      showApiBanner('', e.message);
    }
  } finally {
    bubble.classList.remove('streaming');
    if (qaAbort === myAbort) {
      qaAbort = null;
      qaImageB64 = null;
      const imgBtn = document.getElementById('qa-img');
      imgBtn.classList.remove('active');
      imgBtn.textContent = '📷 图片';
      document.getElementById('qa-img-file').value = '';
      document.getElementById('qa-send').disabled = false;
      document.getElementById('qa-stop').classList.add('hidden');
      qaMessagesEl().scrollTop = qaMessagesEl().scrollHeight;
    }
  }
}

document.getElementById('qa-stop').addEventListener('click', () => {
  if (qaAbort) qaAbort.abort();
});

document.getElementById('qa-send').addEventListener('click', sendQa);
document.getElementById('qa-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendQa();
});

function destTop(d) {
  if (!Array.isArray(d)) return null;
  const mode = d[1] && d[1].name;
  if (mode === 'XYZ') return typeof d[3] === 'number' ? d[3] : null;
  if (mode === 'FitH' || mode === 'FitBH') return typeof d[2] === 'number' ? d[2] : null;
  if (d[1] && typeof d[1].y === 'number') return d[1].y;
  return typeof d[3] === 'number' ? d[3] : null;
}

function jumpToDest(pageNo, y) {
  const wrap = pageWrap(pageNo);
  if (!wrap) return;
  const container = document.getElementById('pdf-container');
  if (typeof y === 'number' && currentPdfDoc) {
    currentPdfDoc.getPage(pageNo).then((page) => {
      const viewport = page.getViewport({ scale: viewerScale });
      const yPix = viewport.convertToViewportPoint(0, y)[1];
      container.scrollTop = wrap.offsetTop + Math.max(0, yPix) - 40;
    });
  } else {
    jumpToPage(pageNo);
  }
}

async function jumpToQuote(pageNo, quote) {
  jumpToPage(pageNo);
  if (!quote || !currentPdfDoc) return;
  try {
    for (let i = 0; i < 10 && !pageWrap(pageNo)?.querySelector('.textLayer span'); i++) {
      await new Promise((r) => setTimeout(r, 300));
    }
    const wrap = pageWrap(pageNo);
    const tl = wrap?.querySelector('.textLayer');
    if (!tl) return;
    const head = canonText(quote).slice(0, 30);
    if (!head) return;
    let target = null;
    for (const sp of tl.querySelectorAll('span')) {
      const c = canonText(sp.textContent || '');
      if (c.includes(head)) { target = sp; break; }
    }
    if (!target) return;
    const container = document.getElementById('pdf-container');
    const y = target.getBoundingClientRect().top
      - container.getBoundingClientRect().top + container.scrollTop - 90;
    container.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
  } catch (_) { /* 页级跳转已生效 */ }
}

let posSaveTimer = null;
document.getElementById('pdf-container').addEventListener('scroll', () => {
  updatePageIndicator(currentPage());
  clearTimeout(posSaveTimer);
  posSaveTimer = setTimeout(saveReadingPos, 500);
});

document.getElementById('btn-prev-page').addEventListener('click', () => jumpToPage(Math.max(1, currentPage() - 1)));
document.getElementById('btn-next-page').addEventListener('click', () => jumpToPage(Math.min(totalPages, currentPage() + 1)));
document.getElementById('btn-zoom-in').addEventListener('click', () => setViewerScale(viewerScale + 0.2));
document.getElementById('btn-zoom-out').addEventListener('click', () => setViewerScale(Math.max(0.6, viewerScale - 0.2)));

async function openSkim() {
  if (!currentPaperId) return;
  try {
    const r = await req(withDomain(`/papers/${currentPaperId}/skim`));
    const prefs = readingPathPrefs();
    saveReadingPathPrefs({ skim_goal: r.goal || '', skim_paper_id: currentPaperId });
    const prev = prefs.skim_paper_id === currentPaperId && prefs.skim_goal
      ? `<div class="empty-hint">上次跳读目标：${escapeHtml(prefs.skim_goal)}</div>` : '';
    const items = (r.path || []).map((p) => `
      <div class="kn-item" data-page="${p.page || 1}">
        <span class="badge">${p.page ? 'p.' + p.page : '—'}</span>
        <span class="kn-quote">${escapeHtml(p.title)}</span>
        <span class="kn-note">${escapeHtml(p.reason || '')}</span>
      </div>`).join('') || '<div class="empty-hint">没有可推荐的章节</div>';
    openTool(`跳读路径${r.goal ? ' · ' + r.goal : ''}`, prev + items);
    document.querySelectorAll('#tool-body .kn-item').forEach((item) => {
      item.addEventListener('click', () => {
        closeTool();
        jumpToPage(Number(item.dataset.page) || 1);
      });
    });
  } catch (e) { showApiBanner('跳读', e.message); }
}

function readingPathPrefs() {
  try {
    return JSON.parse((state.domainConfig && state.domainConfig.reading_path_json) || '{}') || {};
  } catch (_) { return {}; }
}

async function saveReadingPathPrefs(patch) {
  const merged = { ...readingPathPrefs(), ...patch };
  if (state.domainConfig) state.domainConfig.reading_path_json = JSON.stringify(merged);
  const did = currentPaperDomainId || state.selectedDomain;
  if (!did) return;
  try {
    await req(`/domains/${did}/config`, {
      method: 'PUT', body: JSON.stringify({ reading_path_json: JSON.stringify(merged) }),
    });
  } catch (_) { /* keep the in-memory value; retried on the next toggle */ }
}

function outlineTreeHtml(nodes, depth, collapsed) {
  return nodes.map((n) => {
    const kids = n.children || [];
    const isCollapsed = collapsed.has(Number(n.index)) || collapsed.has(String(n.index));
    return `<div class="outline-node" data-depth="${depth}">
      <div class="kn-item outline-sec${kids.length ? ' has-kids' : ''}" data-page="${n.page || 1}"
           style="padding-left:${Math.min(46, 6 + depth * 14)}px">
        ${kids.length
          ? `<button class="outline-toggle" data-toggle="${n.index}" title="折叠 / 展开">${isCollapsed ? '▸' : '▾'}</button>`
          : '<span class="outline-dot"></span>'}
        <span class="badge">${n.page ? 'p.' + n.page : '—'}</span>
        <span class="kn-quote">${escapeHtml(n.title || '')}</span>
      </div>
      ${kids.length ? `<div class="outline-children${isCollapsed ? ' hidden' : ''}" data-children="${n.index}">
        ${outlineTreeHtml(kids, depth + 1, collapsed)}</div>` : ''}
    </div>`;
  }).join('');
}

async function openOutline() {
  if (!currentPaperId) return;
  try {
    const r = (state.outline && state.outline.paper_id === currentPaperId)
      ? state.outline
      : await req(`/papers/${currentPaperId}/outline`);
    state.outline = r;
    const collapsed = new Set((readingPathPrefs().outline_collapsed || []).map(String));
    const tree = (r.section_tree && r.section_tree.length)
      ? r.section_tree
      : (r.sections || []).map((s, i) => ({ ...s, index: i, children: [] }));
    const secs = tree.length
      ? outlineTreeHtml(tree, 0, collapsed)
      : '<div class="empty-hint">未识别到章节标题</div>';
    const floatRow = (f, opts = {}) => `
      <div class="kn-item outline-float" data-page="${f.page || 1}" data-kind="${f.kind}" data-num="${escapeHtml(String(f.num || ''))}">
        <span class="badge">p.${f.page || '—'}</span>
        <span class="kn-quote">${escapeHtml(f.label || f.text || '')}</span>
        ${f.text && f.text !== f.label ? `<span class="kn-note">${escapeHtml(f.text.slice(0, 120))}</span>` : ''}
        ${opts.zoom ? '<button class="btn btn-mini outline-act" data-zoom="1" title="放大查看">放大</button>' : ''}
        <button class="btn btn-mini outline-act" data-textref="1" title="跳到正文中引用它的位置">正文引用</button>
      </div>`;
    const figs = (r.figures || []).map((f) => floatRow({
      page: f.page, kind: 'figure', num: f.num || '',
      label: f.label || `Figure (p.${f.page})`, text: f.caption || '' }, { zoom: true })).join('');
    const others = (r.floats || []).filter((f) => f.kind !== 'figure').map((f) => floatRow(f)).join('');
    const nFigs = (r.figures || []).length;
    const nOthers = (r.floats || []).filter((f) => f.kind !== 'figure').length;
    const body = `<div class="outline-wrap">
      <div class="outline-block"><div class="kn-section-title">章节大纲
        <span class="outline-tools"><button class="btn btn-mini" id="outline-expand">全部展开</button>
        <button class="btn btn-mini" id="outline-collapse">全部折叠</button></span></div>${secs}</div>
      <div class="outline-block"><div class="kn-section-title">图表（${nFigs}）</div>${figs || '<div class="empty-hint">未识别到图像区域</div>'}</div>
      <div class="outline-block"><div class="kn-section-title">表格与算法（${nOthers}）</div>${others || '<div class="empty-hint">未识别到表格/算法标题</div>'}</div>
    </div>`;
    openTool('结构 · 章节 / 图表 / 算法', body);

    document.querySelectorAll('#tool-body .outline-toggle').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const idx = Number(btn.dataset.toggle);
        const box = document.querySelector(`#tool-body [data-children="${idx}"]`);
        if (!box) return;
        const hidden = box.classList.toggle('hidden');
        btn.textContent = hidden ? '▸' : '▾';
        const set = new Set((readingPathPrefs().outline_collapsed || []).map(Number));
        if (hidden) set.add(idx); else set.delete(idx);
        saveReadingPathPrefs({ outline_collapsed: [...set] });
      });
    });
    const setAll = (hide) => {
      const all = [];
      document.querySelectorAll('#tool-body .outline-toggle').forEach((btn) => {
        const idx = Number(btn.dataset.toggle);
        const box = document.querySelector(`#tool-body [data-children="${idx}"]`);
        if (box) box.classList.toggle('hidden', hide);
        btn.textContent = hide ? '▸' : '▾';
        all.push(idx);
      });
      saveReadingPathPrefs({ outline_collapsed: hide ? all : [] });
    };
    document.getElementById('outline-expand').onclick = () => setAll(false);
    document.getElementById('outline-collapse').onclick = () => setAll(true);

    document.querySelectorAll('#tool-body .outline-sec, #tool-body .outline-float').forEach((item) => {
      item.addEventListener('click', (e) => {
        if (e.target.closest('.outline-act') || e.target.closest('.outline-toggle')) return;
        const page = Number(item.dataset.page) || 1;
        const kind = item.dataset.kind;
        const num = item.dataset.num;
        closeTool();
        jumpToPage(page);
        if (kind === 'figure') flashFigure(page, num);
      });
    });
    document.querySelectorAll('#tool-body [data-zoom]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const item = btn.closest('.outline-float');
        zoomFigure(Number(item.dataset.page) || 1, item.dataset.num || '');
      });
    });
    document.querySelectorAll('#tool-body [data-textref]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const item = btn.closest('.outline-float');
        closeTool();
        jumpToTextRef(item.dataset.kind || 'figure', item.dataset.num || '');
      });
    });
  } catch (e) { showApiBanner('结构', e.message); }
}

function findFigure(page, num) {
  const r = state.outline;
  if (!r) return null;
  const figs = r.figures || [];
  return figs.find((f) => f.page === page && String(f.num || '') === String(num || ''))
    || figs.find((f) => f.page === page) || null;
}

async function zoomFigure(page, num) {
  const fig = findFigure(page, num);
  if (!fig || !currentPdfDoc) { showToast('该条目没有可放大的图像区域'); return; }
  const w = Math.max(24, (fig.x1 || 0) - (fig.x0 || 0));
  const h = Math.max(16, (fig.y1 || 0) - (fig.y0 || 0));
  if (!fig.x1 && !fig.y1) { showToast('该条目缺少图像坐标，无法放大'); return; }
  try {
    const p = await currentPdfDoc.getPage(page);
    const scale = Math.min(4, Math.max(1.2, 1100 / w));
    const viewport = p.getViewport({ scale });
    const box = SLFigureGeom.canvasBox(fig, scale);
    const cv = document.createElement('canvas');
    cv.width = box.width;
    cv.height = box.height;
    const ctx = cv.getContext('2d');
    ctx.translate(box.offsetX, box.offsetY);
    await p.render({ canvasContext: ctx, viewport }).promise;
    const mask = document.createElement('div');
    mask.className = 'zoom-mask';
    mask.innerHTML = `<div class="zoom-box">
      <div class="zoom-head">
        <span>${escapeHtml(fig.label || ('Figure p.' + page))}</span>
        <span class="zoom-actions">
          <button class="btn btn-mini" id="zoom-textref">定位正文引用</button>
          <button class="btn btn-mini" id="zoom-close">关闭</button>
        </span>
      </div>
      <div class="zoom-canvas"></div>
      <div class="zoom-foot">${escapeHtml((fig.caption || '').slice(0, 300))}</div>
    </div>`;
    mask.querySelector('.zoom-canvas').appendChild(cv);
    document.body.appendChild(mask);
    const close = () => mask.remove();
    mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
    mask.querySelector('#zoom-close').onclick = close;
    mask.querySelector('#zoom-textref').onclick = () => {
      close();
      jumpToTextRef('figure', num);
    };
    document.addEventListener('keydown', function onEsc(e) {
      if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onEsc); }
    });
  } catch (e) {
    showToast(`放大失败：${e.message || e}`);
  }
}

function jumpToTextRef(kind, num) {
  if (!num) { showToast('该条目没有编号'); return false; }
  const word = kind === 'table' ? 'Table' : (kind === 'algorithm' ? 'Algorithm' : 'Fig(?:ure)?');
  const safeNum = String(num).replace(/[^0-9A-Za-z.\-]/g, '');
  if (!safeNum) return false;
  const re = new RegExp(`\\b${word}\\.?\\s*${safeNum}\\b`, 'i');
  const spans = document.querySelectorAll('.textLayer span');
  for (const sp of spans) {
    if (!re.test(sp.textContent || '')) continue;
    const c = document.getElementById('pdf-container');
    const y = sp.getBoundingClientRect().top - c.getBoundingClientRect().top + c.scrollTop - 120;
    c.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
    updatePageIndicator(currentPage());
    sp.classList.add('float-ref-hit');
    setTimeout(() => sp.classList.remove('float-ref-hit'), 1800);
    return true;
  }
  showToast('正文里没有找到该编号的引用位置');
  return false;
}

function flashFigure(page, num) {
  const r = state.outline;
  const wrap = pageWrap(page);
  if (!r || !wrap) return;
  const canvas = wrap.querySelector('canvas');
  if (!canvas) return;
  const fig = findFigure(page, num);
  if (!fig) return;
  const s = canvas.clientWidth / (fig.w || canvas.clientWidth || 1);
  const geo = SLFigureGeom.overlayBox(fig, s);
  const box = document.createElement('div');
  box.className = 'fig-flash';
  box.style.left = `${geo.left}px`;
  box.style.top = `${geo.top}px`;
  box.style.width = `${geo.width}px`;
  box.style.height = `${geo.height}px`;
  wrap.appendChild(box);
  setTimeout(() => box.remove(), 1800);
}

function floatRefNear(span) {
  let text = span.textContent || '';
  let n = span.nextElementSibling;
  for (let i = 0; i < 4 && n && text.length < 48; i += 1) {
    text += ' ' + (n.textContent || '');
    n = n.nextElementSibling;
  }
  const m = text.match(/\b(Fig(?:ure)?\.?|Table|Algorithm)\s*([0-9]+)/i);
  if (!m) return null;
  const t = m[1].toLowerCase();
  const kind = t.startsWith('fig') ? 'figure' : (t.startsWith('tab') ? 'table' : 'algorithm');
  return { kind, num: m[2] };
}

async function jumpToFloatRef(kind, num) {
  if (!currentPaperId) return;
  try {
    if (!state.outline || state.outline.paper_id !== currentPaperId) {
      state.outline = await req(`/papers/${currentPaperId}/outline`);
    }
    const hit = (state.outline.floats || []).find((f) =>
      f.kind === kind && String(f.num) === String(num));
    if (!hit) { showToast(`结构索引中没有 ${kind} ${num}`); return; }
    await openOutline();
    const sel = `#tool-body .outline-float[data-kind="${kind}"][data-num="${num}"]`;
    const el = document.querySelector(sel);
    if (el) {
      el.classList.add('outline-hit');
      el.scrollIntoView({ block: 'center' });
      setTimeout(() => el.classList.remove('outline-hit'), 1600);
    }
  } catch (e) { showApiBanner('引用跳转', e.message); }
}

document.addEventListener('click', (e) => {
  const span = e.target && e.target.closest ? e.target.closest('.textLayer span') : null;
  if (!span) return;
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed) return;
  const ref = floatRefNear(span);
  if (!ref) return;
  jumpToFloatRef(ref.kind, ref.num);
});

async function openCard() {
  if (!currentPaperId) return;
  try {
    const r = await req(withDomain(`/papers/${currentPaperId}/card`));
    const c = r.card || {};
    const field = (key, label, rows = 3) => `
      <div class="card-field"><b>${label}</b>
        <textarea class="card-input" data-key="${key}" rows="${rows}">${escapeHtml(String(c[key] || ''))}</textarea>
      </div>`;
    const listField = (key, label) => `
      <div class="card-field"><b>${label}</b>
        <textarea class="card-input" data-key="${key}" rows="3">${escapeHtml((c[key] || []).join('\n'))}</textarea>
        <div class="kn-note">每行一条</div>
      </div>`;
    const body = `<div class="tool-actions">
        <button class="btn btn-mini btn-primary" id="card-save">保存卡片</button>
        <button class="btn btn-mini" id="card-export">下载 Markdown</button>
        <button class="btn btn-mini" id="card-reset" title="丢弃已保存的卡片，用当前摘要/笔记重新生成">重新生成</button>
      </div>
      <div class="kn-note">${r.saved ? '已保存的卡片（编辑后点保存）' : '自动生成（尚未保存）'}</div>
      ${field('research_question', '研究问题')}
      ${field('method', '方法')}
      ${field('findings', '核心结论')}
      ${field('limitations', '适用边界与局限')}
      ${listField('notes', '笔记')}
      ${listField('open_questions', '未决问题')}
      <div class="kn-note">高亮 ${c.n_highlights || 0} 条 · 引用 ${c.n_citations || 0} 条</div>`;
    openTool(`阅读卡片 · ${c.title || ''}`, body);
    const collect = () => {
      const out = { ...c };
      document.querySelectorAll('#tool-body .card-input').forEach((el) => {
        const key = el.dataset.key;
        out[key] = ['notes', 'open_questions'].includes(key)
          ? el.value.split('\n').map((s) => s.trim()).filter(Boolean)
          : el.value.trim();
      });
      return out;
    };
    document.getElementById('card-save').addEventListener('click', async () => {
      try {
        await req(`/papers/${currentPaperId}/card`, {
          method: 'PUT',
          body: JSON.stringify({ content: collect(), domain_id: currentPaperDomainId }),
        });
        showToast('阅读卡片已保存');
      } catch (e) { showApiBanner('卡片', e.message); }
    });
    document.getElementById('card-export').addEventListener('click', async () => {
      try {
        const rr = await req(withDomain(`/papers/${currentPaperId}/card/export`));
        downloadText(rr.filename, rr.markdown);
        showToast('已下载卡片 Markdown');
      } catch (e) { showApiBanner('卡片导出', e.message); }
    });
    document.getElementById('card-reset').addEventListener('click', async () => {
      if (!confirm('丢弃已保存的卡片并用当前摘要/笔记重新生成？未保存的编辑会丢失。')) return;
      try {
        await req(withDomain(`/papers/${currentPaperId}/card`), { method: 'DELETE' });
        closeTool();
        await openCard();
        showToast('已重新生成卡片');
      } catch (e) { showApiBanner('卡片', e.message); }
    });
  } catch (e) { showApiBanner('卡片', e.message); }
}

async function exportCurrentPaper() {
  if (!currentPaperId) return;
  try {
    const r = await req(withDomain(`/papers/${currentPaperId}/export`));
    downloadText(r.filename, r.markdown);
    showToast('已导出 Markdown');
  } catch (e) { showApiBanner('导出', e.message); }
}

document.getElementById('btn-skim').addEventListener('click', openSkim);
document.getElementById('btn-outline').addEventListener('click', openOutline);
document.getElementById('btn-card').addEventListener('click', openCard);
document.getElementById('btn-export-paper').addEventListener('click', exportCurrentPaper);
document.addEventListener('keydown', (e) => {
  if (!currentPdfDoc || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;
  if (e.key === 'PageUp') { jumpToPage(Math.max(1, currentPage() - 1)); e.preventDefault(); }
  if (e.key === 'PageDown') { jumpToPage(Math.min(totalPages, currentPage() + 1)); e.preventDefault(); }
});

async function setViewerScale(s) {
  const gen = ++renderGen;
  viewerScale = Math.min(3.0, Math.max(0.6, s));
  document.getElementById('zoom-indicator').textContent = `${Math.round(viewerScale * 100)}%`;
  const pageNo = currentPage();
  const container = document.getElementById('pdf-container');
  container.innerHTML = '';
  for (let i = 1; i <= totalPages; i++) await renderPage(i);
  if (gen !== renderGen) return;
  await applyAllHighlights();
  await buildLinkHotspots();
  jumpToPage(pageNo);
}

async function applyAllHighlights() {
  clearHighlightMarks();
  let ok = 0, fail = 0;
  const counts = {};
  for (const hl of currentHighlights) {
    if (hlDensity > 0) {
      counts[hl.category] = (counts[hl.category] || 0) + 1;
      if (counts[hl.category] > hlDensity) continue;
    }
    if (applyHighlight(hl)) ok += 1; else fail += 1;
  }
  if (fail) console.warn(`[reader] highlights rendered ${ok}/${ok + fail} (density cap may hide some)`);
  hlRenderStats = { ok, fail, total: currentHighlights.length };
  const note = document.getElementById('hl-anchor-note');
  if (note) {
    note.textContent = `渲染 ${ok}/${hlRenderStats.total}`;
    note.title = '通过归一化锚定渲染出的高亮 / 全部高亮（未渲染 = 引文跨页或未命中）';
    note.hidden = false;
  }
  return { ok, fail };
}

let hlRenderStats = { ok: 0, fail: 0, total: 0 };
let hlDensity = 5;  // 0 = all; persisted
let hlFilter = 'all';  // all | background | approach | limitation
const HL_COLOR_DEFAULTS = { background: '#60a5fa', approach: '#6ee794', limitation: '#ff7676' };
let hlColors = { ...HL_COLOR_DEFAULTS };
try {
  const storedDensity = Number(localStorage.getItem('hlDensity'));
  if (Number.isFinite(storedDensity) && storedDensity >= 0) hlDensity = storedDensity;
} catch (_) { /* storage unavailable → default */ }
if (!Number.isFinite(hlDensity) || hlDensity < 0) hlDensity = 5;

function applyHlColors() {
  const root = document.documentElement;
  root.style.setProperty('--hl-bg', hlColors.background || HL_COLOR_DEFAULTS.background);
  root.style.setProperty('--hl-approach', hlColors.approach || HL_COLOR_DEFAULTS.approach);
  root.style.setProperty('--hl-limitation', hlColors.limitation || HL_COLOR_DEFAULTS.limitation);
}

function loadHlColors(cfg) {
  const fallback = { ...HL_COLOR_DEFAULTS };
  try {
    const saved = JSON.parse((cfg && cfg.color_scheme_json) || '{}');
    hlColors = {
      background: saved.background || fallback.background,
      approach: saved.approach || fallback.approach,
      limitation: saved.limitation || fallback.limitation,
    };
  } catch (_) { hlColors = fallback; }
  applyHlColors();
}

async function saveHlColor(key, value) {
  hlColors[key] = value;
  applyHlColors();
  if (!currentPaperDomainId) return;
  try {
    await req(`/domains/${currentPaperDomainId}/config`, {
      method: 'PUT', body: JSON.stringify({ color_scheme_json: JSON.stringify(hlColors) }),
    });
  } catch (_) { /* local application already happened */ }
}

function clearHighlightMarks() {
  document.querySelectorAll('.textLayer span')
    .forEach((sp) => sp.classList.remove('hlbg-background', 'hlbg-approach', 'hlbg-limitation', 'hlbg-delete'));
}

function canonText(s) {
  return (s || '')
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '');
}

const pageCanonCache = new WeakMap();
function pageCanonIndex(wrap) {
  let idx = pageCanonCache.get(wrap);
  if (idx) return idx;
  const spans = Array.from(wrap.querySelectorAll('.textLayer span'));
  let canon = '';
  const ranges = spans.map((sp) => {
    const c = canonText(sp.textContent);
    const r = [canon.length, canon.length + c.length];
    canon += c;
    return r;
  });
  idx = { spans, canon, ranges };
  pageCanonCache.set(wrap, idx);
  return idx;
}

function applyQuoteOnPage(wrap, canonQuote, category) {
  if (!canonQuote || canonQuote.length < 8) return false;
  const { spans, canon, ranges } = pageCanonIndex(wrap);
  const idx = canon.indexOf(canonQuote);
  if (idx < 0) return false;
  const qEnd = idx + canonQuote.length;
  spans.forEach((sp, i) => {
    const [s0, s1] = ranges[i];
    if (s1 > idx && s0 < qEnd) sp.classList.add(`hlbg-${category}`);
  });
  return true;
}

function applyQuoteFragments(wrap, quote, category, depth = 0) {
  const cq = canonText(quote);
  if (cq.length < 30) return 0;
  if (applyQuoteOnPage(wrap, cq, category)) return 1;
  if (depth >= 4) return 0;
  const words = quote.trim().split(/\s+/);
  if (words.length < 6) return 0;
  const mid = Math.ceil(words.length / 2);
  return applyQuoteFragments(wrap, words.slice(0, mid).join(' '), category, depth + 1)
       + applyQuoteFragments(wrap, words.slice(mid).join(' '), category, depth + 1);
}

function applyHighlight(hl) {
  const q = canonText(hl.quote);
  const wrap = pageWrap(hl.page);
  if (wrap && applyQuoteOnPage(wrap, q, hl.category)) return true;
  for (const w of document.querySelectorAll('.pdf-page')) {
    if (w !== wrap && applyQuoteOnPage(w, q, hl.category)) {
      console.warn(`[hl] re-anchored quote (anchor said p.${hl.page})`);
      return true;
    }
  }
  for (const w of [wrap, ...document.querySelectorAll('.pdf-page')]) {
    if (w && applyQuoteFragments(w, hl.quote, hl.category) > 0) {
      console.warn('[hl] fragment-anchored cross-column quote');
      return true;
    }
  }
  return false;
}

async function renderHighlightsTab(p) {
  const el = document.getElementById('tab-highlights');
  const hs = await req(withDomain(`/papers/${p.paper_id}/highlights`));
  if (p.paper_id !== currentPaperId) return;
  currentHighlights = hs;
  if (!currentHighlights.length) {
    el.innerHTML = '<div class="empty-hint">三类高亮：背景蓝 / 解决思路绿 / 局限红<br /><span class="sub">划选自建，或让模型自动识别</span></div>' +
      '<div style="text-align:center"><button class="btn btn-primary" id="btn-hl-auto" title="让模型按 背景/解决思路/局限 三类标注（quote 逐字校验，宁漏不错）">自动识别</button></div>';
    document.getElementById('btn-hl-auto').addEventListener('click', () => runAutoHighlight(p.paper_id));
    return;
  }
  const label = { background: '背景', approach: '解决思路', limitation: '局限', delete: '删除标记' };
  const counts = {};
  for (const h of currentHighlights) counts[h.category] = (counts[h.category] || 0) + 1;
  const densityBtn = `<button class="btn btn-mini" id="btn-hl-density">${hlDensity > 0 ? `精简 (每类前 ${hlDensity})` : '显示全部'}</button>`;
  const hasAuto = currentHighlights.some((h) => h.source === 'auto');
  const autoBtn = hasAuto
    ? `<button class="btn btn-mini" id="btn-hl-auto" title="用编号句子引擎重新标注（删旧自动结果，手动高亮保留）">重新识别</button>`
    : `<button class="btn btn-mini btn-primary" id="btn-hl-auto" title="让模型按 背景/解决思路/局限 三类标注（宁漏不错）">自动识别</button>`;
  const anchorNote = '<span class="sum-warn" id="hl-anchor-note" hidden></span>';
  const filterBtn = (v, t) => `<button class="btn btn-mini${hlFilter === v ? ' active' : ''}" data-hl-filter="${v}">${t}</button>`;
  const colorInput = (k, t) => `<label class="hl-color">${t}<input type="color" data-hl-color="${k}" value="${hlColors[k] || HL_COLOR_DEFAULTS[k]}" /></label>`;
  const visible = currentHighlights.filter((h) => hlFilter === 'all' || h.category === hlFilter);
  el.innerHTML = `<div class="hl-controls">
      <div class="hl-filter">${filterBtn('all', '全部')}${filterBtn('background', '背景')}${filterBtn('approach', '解决思路')}${filterBtn('limitation', '局限')}</div>
      <div class="hl-colors">${colorInput('background', '背景')}${colorInput('approach', '思路')}${colorInput('limitation', '局限')}</div>
      <div class="hl-tools">${densityBtn}${autoBtn}</div>
    </div>
    <div class="sum-toolbar"><span class="detail-row" style="margin:0">
      蓝 ${counts.background || 0} · 绿 ${counts.approach || 0} · 红 ${counts.limitation || 0}${counts.delete ? ` · 旧删标 ${counts.delete}` : ''}</span>${anchorNote}</div>` +
    (visible.length ? visible.map((h) => `
    <div class="hl-item" data-id="${h.id}" data-page="${h.page}">
      <div class="hl-head"><span class="badge badge-hl-${h.category}">${label[h.category]}</span><span>p.${h.page}</span>
        <button class="btn btn-mini" data-note="${h.id}" title="为这条高亮写批注">注</button>
        <button class="btn btn-mini btn-danger" data-del="${h.id}">×</button></div>
      <div class="hl-quote">${escapeHtml(h.quote.slice(0, 140))}${h.quote.length > 140 ? '…' : ''}</div>
    </div>`).join('') : '<div class="empty-hint">该类别暂无可显示的高亮</div>');
  el.querySelectorAll('[data-hl-filter]').forEach((b) => b.addEventListener('click', () => {
    hlFilter = b.dataset.hlFilter;
    renderHighlightsTab({ paper_id: p.paper_id });
  }));
  el.querySelectorAll('[data-hl-color]').forEach((input) => input.addEventListener('change', () => {
    saveHlColor(input.dataset.hlColor, input.value);
  }));
  document.getElementById('btn-hl-density').addEventListener('click', async () => {
    hlDensity = hlDensity > 0 ? 0 : 5;
    localStorage.setItem('hlDensity', String(hlDensity));
    if (currentPaperDomainId) {
      req(`/domains/${currentPaperDomainId}/config`, {
        method: 'PUT',
        body: JSON.stringify({ highlight_density: String(hlDensity) }),
      }).catch(() => {});
    }
    await renderHighlightsTab({ paper_id: p.paper_id });
    await applyAllHighlights();
  });
  document.getElementById('btn-hl-auto').addEventListener('click', () => runAutoHighlight(p.paper_id, hasAuto));
  el.querySelectorAll('.hl-item').forEach((item) => {
    item.addEventListener('click', (e) => {
      if (e.target.dataset.del || e.target.dataset.note) return;
      jumpToPage(Number(item.dataset.page));
    });
  });
  el.querySelectorAll('[data-note]').forEach((btn) => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const h = currentHighlights.find((x) => x.id === Number(btn.dataset.note));
    if (h) openFloatCard('note', { quote: h.quote, page: h.page, hl_id: h.id });
  }));
  el.querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      await undoHighlight({ paperId: currentPaperId, id: Number(btn.dataset.del) });
      lastManualHl = null;
      hideToast();
      await renderNotesTab({ paper_id: currentPaperId });
    });
  });
}

async function runAutoHighlight(paperId, rerun = false) {
  const btn = document.getElementById('btn-hl-auto');
  const old = btn ? btn.textContent : '';
  if (rerun && !confirm('重新识别将删除该论文已有的自动标注（手动划选的保留），继续？')) return;
  if (btn) { btn.textContent = '识别中…'; btn.disabled = true; }
  try {
    const r = await req(`/papers/${paperId}/highlights/auto`, {
      method: 'POST',
      body: JSON.stringify({ rerun, domain_id: currentPaperDomainId }),
    });
    if (r.skipped) {
      showToast('该论文已有自动识别结果；点「重新识别」可替换');
    } else {
      showToast(`自动识别完成：新增 ${r.added} 条，跳过 ${r.dropped} 句（宁漏不错）`);
    }
    currentHighlights = await req(withDomain(`/papers/${paperId}/highlights`));
    await renderHighlightsTab({ paper_id: paperId });
    await applyAllHighlights();
  } catch (e) {
    if (String(e.message).includes('409')) alert('该论文的自动识别正在进行中，请稍候');
    else alert(`自动识别失败: ${e.message}`);
  } finally {
    if (btn) { btn.textContent = old; btn.disabled = false; }
  }
}

let lastManualHl = null;  // {paperId, id}
let toastTimer = null;

function showToast(text) {
  const t = document.getElementById('toast');
  document.getElementById('toast-text').textContent = text;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 6000);
}

function hideToast() {
  document.getElementById('toast').classList.add('hidden');
}

async function undoHighlight(target) {
  await req(`/papers/${target.paperId}/highlights/${target.id}`, { method: 'DELETE' });
  currentHighlights = currentHighlights.filter((h) => h.id !== target.id);
  await applyAllHighlights();
  await renderHighlightsTab({ paper_id: target.paperId });
}

const pageLinkRects = new Map();

async function buildLinkHotspots() {
  for (let i = 1; i <= currentPdfDoc.numPages; i++) {
    const page = await currentPdfDoc.getPage(i);
    const viewport = page.getViewport({ scale: viewerScale });
    const annots = await page.getAnnotations();
    const wrap = pageWrap(i);
    if (!wrap) continue;
    const rects = pageLinkRects.get(i) || [];
    for (const a of annots) {
      if (a.subtype !== 'Link') continue;
      let targetPage = null, targetY = null, isExternal = false;
      if (a.url || a.unsafeUrl) { targetPage = a.url || a.unsafeUrl; isExternal = true; }
      else if (a.dest) {
        try {
          const d = typeof a.dest === 'string' ? await currentPdfDoc.getDestination(a.dest) : a.dest;
          const ref = Array.isArray(d) ? d[0] : null;
          if (ref) {
            targetPage = (await currentPdfDoc.getPageIndex(ref)) + 1;
            targetY = destTop(d);
          }
        } catch (_) { continue; }
      }
      if (targetPage == null) continue;
      const [x1, y1, x2, y2] = viewport.convertToViewportRectangle(a.rect);
      const left = Math.min(x1, x2), top = Math.min(y1, y2);
      const width = Math.abs(x2 - x1), height = Math.abs(y2 - y1);
      const el = document.createElement('div');
      el.className = 'link-hotspot';
      el.style.left = `${left}px`;
      el.style.top = `${top}px`;
      el.style.width = `${width}px`;
      el.style.height = `${height}px`;
      if (isExternal) {
        el.dataset.url = targetPage;
        el.title = targetPage;
        rects.push({ x: left, y: top, w: width, h: height, url: targetPage });
      } else {
        el.dataset.goto = targetPage;
        if (targetY != null) el.dataset.gotoY = targetY;
        el.title = `跳转到第 ${targetPage} 页`;
      }
      wrap.appendChild(el);
    }
    pageLinkRects.set(i, rects);
  }
}

function openExternalLink(url) {
  if (!/^https?:\/\//i.test(url)) return;
  if (window.scholarlens && typeof window.scholarlens.openExternal === 'function') {
    window.scholarlens.openExternal(url);
  } else {
    window.open(url, '_blank');
  }
}

document.addEventListener('click', (e) => {
  const hs = e.target.closest('.link-hotspot');
  if (hs) {
    e.preventDefault(); e.stopPropagation();
    if (hs.dataset.url) openExternalLink(hs.dataset.url);
    else if (hs.dataset.goto) jumpToDest(Number(hs.dataset.goto), hs.dataset.gotoY ? Number(hs.dataset.gotoY) : undefined);
    return;
  }
  const page = e.target.closest('.pdf-page');
  if (page && !String(window.getSelection() || '').trim()) {
    const pr = page.getBoundingClientRect();
    const x = e.clientX - pr.left, y = e.clientY - pr.top;
    for (const l of pageLinkRects.get(Number(page.dataset.page)) || []) {
      if (x >= l.x && x <= l.x + l.w && y >= l.y && y <= l.y + l.h) {
        e.preventDefault();
        openExternalLink(l.url);
        return;
      }
    }
  }
  const a = e.target.closest('a[href^="http"]');
  if (a) { e.preventDefault(); openExternalLink(a.href); }
}, true);

const floatCard = document.getElementById('float-card');
let fcState = null;  // {kind, quote, page, hl_id?, noteId?, existingText?, mode}

const FC_TITLES = { translate: '翻译', explain: '解释', note: '批注' };
const FC_MODE_LABELS = { literal: '直译', free: '意译', academic: '学术优化' };

function openFloatCard(kind, opts) {
  ++fcGen;
  fcState = { kind, mode: 'literal', ...opts };
  document.getElementById('fc-title').textContent = FC_TITLES[kind];
  const tabs = document.getElementById('fc-mode-tabs');
  tabs.classList.toggle('hidden', kind !== 'translate');
  tabs.querySelectorAll('.fc-mode').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === fcState.mode));
  document.getElementById('fc-cache').textContent = '';
  floatCard.classList.remove('hidden');
  const left = Math.min(Math.max(8, parseFloat(selMenu.style.left) || 120), window.innerWidth - 420);
  const top = Math.min(Math.max(8, parseFloat(selMenu.style.top) || 120), window.innerHeight - 240);
  floatCard.style.left = `${left}px`;
  floatCard.style.top = `${top}px`;
  if (kind === 'translate') loadTranslation('literal');
  else if (kind === 'explain') loadExplanation();
  else renderNoteEditor();
}

function closeFloatCard() {
  ++fcGen;
  fcAbort?.abort();
  fcAbort = null;
  document.getElementById('fc-stop').classList.add('hidden');
  floatCard.classList.add('hidden');
  fcState = null;
}

function fcQuoteHtml() {
  return `<div class="fc-quote">${escapeHtml((fcState.quote || '').slice(0, 400))}</div>`;
}

function startElapsedTimer(elId, label) {
  const t0 = Date.now();
  const el = document.getElementById(elId);
  const iv = setInterval(() => {
    const target = document.getElementById(elId);
    if (!target || target.textContent.indexOf(label) !== 0) { clearInterval(iv); return; }
    target.textContent = `${label} ${Math.round((Date.now() - t0) / 1000)}s`;
  }, 500);
  return t0;
}

let fcGen = 0;
let fcAbort = null;

async function loadTranslation(mode) {
  fcState.mode = mode;
  fcAbort?.abort();
  const gen = ++fcGen;
  document.querySelectorAll('#fc-mode-tabs .fc-mode').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === mode));
  const body = document.getElementById('fc-body');
  const stopBtn = document.getElementById('fc-stop');
  document.getElementById('fc-cache').textContent = '';
  body.innerHTML = `${fcQuoteHtml()}<div class="fc-loading" id="fc-progress">翻译中… 0s</div>`;
  startElapsedTimer('fc-progress', '翻译中…');
  stopBtn.classList.remove('hidden');
  fcAbort = new AbortController();
  try {
    const r = await req(`/papers/${currentPaperId}/translate`, {
      method: 'POST',
      body: JSON.stringify({ quote: fcState.quote, mode }),
      signal: fcAbort.signal,
    });
    if (gen !== fcGen) return;
    fcState.parts = r.parts;
    document.getElementById('fc-cache').textContent =
      r.all_cached ? '全部命中本地缓存' : `新译 ${r.n_fresh} 句 · 复用缓存 ${r.n_cached} 句`;
    renderTranslationParas();
  } catch (e) {
    if (gen !== fcGen) return;
    if (e.name === 'AbortError') {
      body.innerHTML = `${fcQuoteHtml()}<div class="fc-loading">已终止（结果不保存）</div>`;
      return;
    }
    if (fcState && fcState.kind === 'translate') {
      body.innerHTML = `${fcQuoteHtml()}<div class="fc-error">翻译失败: ${escapeHtml(e.message)}</div>`;
    }
  } finally {
    if (gen === fcGen) {
      fcAbort = null;
      stopBtn.classList.add('hidden');
    }
  }
}

function renderTranslationParas() {
  const body = document.getElementById('fc-body');
  floatCard.classList.remove('wide');
  document.getElementById('fc-align').classList.remove('active');
  const paras = fcState.parts.map((p) => `<p class="fc-part">${escapeHtml(p.t)}</p>`).join('');
  body.innerHTML = `${fcQuoteHtml()}
    <div class="fc-label">${FC_MODE_LABELS[fcState.mode]}</div>
    ${paras}`;
}

function renderTranslationAligned() {
  const body = document.getElementById('fc-body');
  floatCard.classList.add('wide');
  document.getElementById('fc-align').classList.add('active');
  const rows = fcState.parts.map((p) => `
    <div class="fc-row" data-pg="${fcState.page}">
      <div class="fc-src" title="点击跳到原文">${escapeHtml(p.sentence)}</div>
      <div class="fc-dst">${escapeHtml(p.t)}</div>
    </div>`).join('');
  body.innerHTML = `<div class="fc-align-head"><div>原文</div><div>译文</div></div>${rows}`;
  body.querySelectorAll('.fc-src').forEach((el) => {
    el.addEventListener('click', () => {
      if (fcState.page) jumpToPage(fcState.page);
    });
  });
}

document.getElementById('fc-align').addEventListener('click', () => {
  if (!fcState || fcState.kind !== 'translate') return;
  if (!fcState.parts || !fcState.parts.length) {
    document.getElementById('fc-cache').textContent = '翻译完成后可对照';
    return;
  }
  if (document.getElementById('fc-align').classList.contains('active')) renderTranslationParas();
  else renderTranslationAligned();
});

async function loadExplanation() {
  const gen = ++fcGen;
  const body = document.getElementById('fc-body');
  document.getElementById('fc-cache').textContent = '';
  body.innerHTML = `${fcQuoteHtml()}<div class="fc-loading" id="fc-progress">解释中… 0s</div>`;
  startElapsedTimer('fc-progress', '解释中…');
  try {
    const r = await req(`/papers/${currentPaperId}/explain`, {
      method: 'POST',
      body: JSON.stringify({ quote: fcState.quote }),
    });
    if (gen !== fcGen) return;
    document.getElementById('fc-cache').textContent = r.cached ? '缓存命中' : `${r.latency_ms}ms`;
    const concepts = (r.concepts || []).map((c) =>
      `<div class="fc-concept"><b>${escapeHtml(c.term)}</b>${escapeHtml(c.def)}</div>`).join('');
    body.innerHTML = `${fcQuoteHtml()}
      <div class="fc-label">这句话在说什么</div><div>${escapeHtml(r.plain) || '—'}</div>
      ${concepts ? `<div class="fc-label">涉及概念</div><div class="fc-concepts">${concepts}</div>` : ''}
      ${r.why ? `<div class="fc-why"><div class="fc-label">为什么出现在这里</div>${escapeHtml(r.why)}</div>` : ''}`;
  } catch (e) {
    if (gen !== fcGen) return;
    body.innerHTML = `${fcQuoteHtml()}<div class="fc-error">解释失败: ${escapeHtml(e.message)}（可重试）</div>`;
  }
}

function renderNoteEditor() {
  const body = document.getElementById('fc-body');
  const picked = new Set(fcState.existingTags || []);
  body.innerHTML = `
    <textarea id="fc-note-text" placeholder="写下你的中文批注…"></textarea>
    <div class="fc-tags">${NOTE_TAGS.map((t) =>
      `<label class="tag-pick"><input type="checkbox" value="${t}"${picked.has(t) ? ' checked' : ''} />${t}</label>`).join('')}</div>
    <div class="fc-actions">
      <button class="btn btn-mini" id="fc-note-cancel">取消</button>
      <button class="btn btn-mini btn-primary" id="fc-note-save">保存批注</button>
    </div>`;
  const ta = document.getElementById('fc-note-text');
  ta.value = fcState.existingText || '';
  ta.focus();
  document.getElementById('fc-note-cancel').addEventListener('click', closeFloatCard);
  document.getElementById('fc-note-save').addEventListener('click', async () => {
    const text = ta.value.trim();
    if (!text) return;
    const tags = Array.from(body.querySelectorAll('.fc-tags input:checked')).map((i) => i.value);
    try {
      if (fcState.noteId) {
        await req(`/papers/${currentPaperId}/notes/${fcState.noteId}`, {
          method: 'PUT', body: JSON.stringify({ text, tags }),
        });
      } else {
        await req(`/papers/${currentPaperId}/notes`, {
          method: 'POST',
          body: JSON.stringify({ page: fcState.page, quote: fcState.quote, text,
                                 tags, hl_id: fcState.hl_id || null,
                                 rects: fcState.rects || [],
                                 occurrence: fcState.occurrence || 0,
                                 domain_id: currentPaperDomainId }),
        });
      }
      const wasEdit = !!fcState.noteId;
      closeFloatCard();
      showToast(wasEdit ? '批注已更新' : '批注已保存');
      await renderNotesTab({ paper_id: currentPaperId });
      await renderHighlightsTab({ paper_id: currentPaperId });
    } catch (e) { alert(`批注保存失败: ${e.message}`); }
  });
}

document.getElementById('fc-close').addEventListener('click', closeFloatCard);
document.getElementById('fc-stop').addEventListener('click', () => fcAbort?.abort());
document.querySelectorAll('#fc-mode-tabs .fc-mode').forEach((b) =>
  b.addEventListener('click', () => loadTranslation(b.dataset.mode)));

let fcDrag = null;
document.querySelector('.fc-head').addEventListener('mousedown', (e) => {
  if (e.target.closest('button')) return;
  const rect = floatCard.getBoundingClientRect();
  fcDrag = { dx: e.clientX - rect.left, dy: e.clientY - rect.top };
  e.preventDefault();
});
document.addEventListener('mousemove', (e) => {
  if (!fcDrag) return;
  const x = Math.min(Math.max(0, e.clientX - fcDrag.dx), window.innerWidth - 120);
  const y = Math.min(Math.max(0, e.clientY - fcDrag.dy), window.innerHeight - 60);
  floatCard.style.left = `${x}px`;
  floatCard.style.top = `${y}px`;
});
document.addEventListener('mouseup', () => { fcDrag = null; });

const NOTE_TAGS = ['问题', '方法', '结论', '局限', '想法'];
let noteTagFilter = '';

async function renderNotesTab(p) {
  const el = document.getElementById('tab-notes');
  let notes = [];
  const qs = noteTagFilter ? `?tag=${encodeURIComponent(noteTagFilter)}` : '';
  try { notes = await req(`/papers/${p.paper_id}/notes${qs}`); } catch (_) { return; }
  const chips = [`<button class="tag-chip ${noteTagFilter ? '' : 'active'}" data-tag="">全部</button>`]
    .concat(NOTE_TAGS.map((t) =>
      `<button class="tag-chip ${noteTagFilter === t ? 'active' : ''}" data-tag="${t}">${t}</button>`)).join('');
  const head = `<div class="tag-filter">${chips}</div>`;
  if (!notes.length) {
    el.innerHTML = head + `<div class="empty-hint">${noteTagFilter ? `没有「${noteTagFilter}」批注` : '还没有批注'}` +
      `<br /><span class="sub">阅读时划选文本 → 批注，或在高亮上点「注」</span></div>`;
    el.querySelectorAll('.tag-chip').forEach((b) => b.addEventListener('click', (e) => {
      e.stopPropagation();
      noteTagFilter = b.dataset.tag;
      renderNotesTab(p);
    }));
    return;
  }
  el.innerHTML = head + notes.map((n) => `
    <div class="hl-item note-item" data-id="${n.id}" data-page="${n.page}">
      <div class="hl-head"><span class="badge badge-parsing">批注</span><span>p.${n.page}</span>
        ${(n.tags || []).map((t) => `<span class="tag-badge">${escapeHtml(t)}</span>`).join('')}
        ${n.hl_id ? `<span title="关联的高亮">⇒ 高亮#${n.hl_id}</span>` : ''}
        <button class="btn btn-mini" data-edit="${n.id}">改</button>
        <button class="btn btn-mini btn-danger" data-del="${n.id}">×</button></div>
      <div class="hl-quote">${escapeHtml((n.quote || '').slice(0, 90))}${(n.quote || '').length > 90 ? '…' : ''}</div>
      <div class="note-text">${escapeHtml(n.text)}</div>
    </div>`).join('');
  el.querySelectorAll('.tag-chip').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    noteTagFilter = b.dataset.tag;
    renderNotesTab(p);
  }));
  el.querySelectorAll('.note-item').forEach((item) => {
    item.addEventListener('click', (e) => {
      if (e.target.dataset.del || e.target.dataset.edit) return;
      jumpToPage(Number(item.dataset.page));
    });
  });
  el.querySelectorAll('[data-edit]').forEach((btn) => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const n = notes.find((x) => x.id === Number(btn.dataset.edit));
    openFloatCard('note', { quote: n.quote, page: n.page, hl_id: n.hl_id, noteId: n.id,
                            existingText: n.text, existingTags: n.tags || [] });
  }));
  el.querySelectorAll('[data-del]').forEach((btn) => btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    await req(`/papers/${currentPaperId}/notes/${btn.dataset.del}`, { method: 'DELETE' });
    await renderNotesTab({ paper_id: currentPaperId });
  }));
}

function readingPosKey(paperId) {
  return currentPaperDomainId ? `pos:${currentPaperDomainId}:${paperId}` : `pos:${paperId}`;
}

let restoringPos = false;

function readingScrollRatio(pageNo) {
  const c = document.getElementById('pdf-container');
  const wrap = pageWrap(pageNo);
  if (!wrap || !wrap.offsetHeight) return 0;
  const ratio = (c.scrollTop - wrap.offsetTop) / Math.max(1, wrap.offsetHeight - 8);
  return Math.max(0, Math.min(1, ratio));
}

function saveReadingPos() {
  if (!currentPaperId) return;
  const c = document.getElementById('pdf-container');
  if (!c.scrollHeight) return;
  const page = currentPage();
  const ratio = readingScrollRatio(page);
  try { localStorage.setItem(readingPosKey(currentPaperId), String(Math.round(c.scrollTop))); } catch (_) { /* quota */ }
  if (currentPaperDomainId && !restoringPos) {
    req(`/papers/${currentPaperId}/reading`, {
      method: 'POST',
      body: JSON.stringify({ domain_id: currentPaperDomainId, page, scroll_ratio: ratio }),
    }).catch(() => { /* offline: localStorage still has it */ });
  }
}

function applyReadingPos(pos) {
  const page = Number(pos && pos.last_page) || 0;
  if (page <= 0) return false;
  const wrap = pageWrap(page);
  if (!wrap) return false;
  const ratio = Number(pos.scroll_ratio);
  const within = Number.isFinite(ratio) ? Math.max(0, (wrap.offsetHeight - 8) * ratio) : 0;
  document.getElementById('pdf-container').scrollTop = wrap.offsetTop + within;
  updatePageIndicator(page);
  return true;
}

function restoreReadingPos(paperId) {
  restoringPos = true;
  try {
    if (applyReadingPos(state.readingPosition)) return true;
    let saved = null;
    try {
      saved = localStorage.getItem(readingPosKey(paperId))
        || localStorage.getItem(`pos:${paperId}`);
    } catch (_) { /* storage unavailable */ }
    if (saved) {
      const top = Number(saved);
      document.getElementById('pdf-container').scrollTop = Number.isFinite(top) ? top : 0;
      updatePageIndicator(currentPage());
      return true;
    }
    return false;
  } finally {
    setTimeout(() => { restoringPos = false; }, 700);
  }
}

const placeholderEl = document.getElementById('reader-placeholder');
(function ambientGlow() {
  const st = { x: 0, y: 0, tx: 0, ty: 0, raf: 0, primed: false };
  const step = () => {
    st.raf = 0;
    if (document.hidden || placeholderEl.classList.contains('hidden')) return;
    st.x += (st.tx - st.x) * 0.06;
    st.y += (st.ty - st.y) * 0.06;
    const r = placeholderEl.getBoundingClientRect();
    placeholderEl.style.setProperty('--mx', `${(st.x - r.left).toFixed(1)}px`);
    placeholderEl.style.setProperty('--my', `${(st.y - r.top).toFixed(1)}px`);
    if (Math.abs(st.tx - st.x) > 0.3 || Math.abs(st.ty - st.y) > 0.3) {
      st.raf = requestAnimationFrame(step);
    }
  };
  const kick = () => { if (!st.raf) st.raf = requestAnimationFrame(step); };
  document.addEventListener('mousemove', (e) => {
    if (document.hidden) return;
    st.tx = e.clientX;
    st.ty = e.clientY;
    if (!st.primed) { st.x = st.tx; st.y = st.ty; st.primed = true; }
    if (!placeholderEl.classList.contains('hidden')) kick();
  }, { passive: true });
})();

const selMenu = document.getElementById('sel-menu');
document.addEventListener('mouseup', (e) => {
  if (selMenu.contains(e.target)) return;
  setTimeout(() => {
    const sel = window.getSelection();
    const text = sel ? sel.toString().trim() : '';
    if (!text || text.length < 3) { selMenu.classList.add('hidden'); return; }
    let node = sel.anchorNode;
    while (node && !(node.classList && node.classList.contains('pdf-page'))) node = node.parentNode;
    if (!node) { selMenu.classList.add('hidden'); return; }
    const page = Number(node.dataset.page);
    selMenu.dataset.page = page;
    selMenu.dataset.quote = text;
    const range = sel.getRangeAt(0);
    const base = node.getBoundingClientRect();
    const rects = [];
    for (const r of range.getClientRects()) {
      if (r.width < 2 || r.height < 2) continue;
      rects.push({ x: +((r.left - base.left) / base.width).toFixed(4),
                   y: +((r.top - base.top) / base.height).toFixed(4),
                   w: +(r.width / base.width).toFixed(4),
                   h: +(r.height / base.height).toFixed(4) });
      if (rects.length >= 40) break;
    }
    selMenu.dataset.rects = JSON.stringify(rects);
    let occ = 0;
    try {
      const tl = node.querySelector('.textLayer');
      if (tl) {
        const pre = document.createRange();
        pre.selectNodeContents(tl);
        pre.setEnd(range.startContainer, range.startOffset);
        const before = pre.toString();
        let at = 0;
        while (true) {
          const hit = before.indexOf(text, at);
          if (hit < 0) break;
          occ += 1;
          at = hit + text.length;
        }
      }
    } catch (_) { occ = 0; }
    selMenu.dataset.occurrence = String(occ);
    const rect = range.getBoundingClientRect();
    const left = Math.min(Math.max(8, rect.left), window.innerWidth - 300);
    const top = Math.min(rect.bottom + 6, window.innerHeight - 70);
    selMenu.style.left = `${left}px`;
    selMenu.style.top = `${top}px`;
    selMenu.classList.remove('hidden');
  }, 10);
});
document.addEventListener('mousedown', (e) => {
  if (!selMenu.contains(e.target)) selMenu.classList.add('hidden');
  if (floatCard && !floatCard.contains(e.target) && !selMenu.contains(e.target)) closeFloatCard();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    selMenu.classList.add('hidden');
    closeFloatCard();
    document.getElementById('knowledge-mask').classList.add('hidden');
    window.getSelection().removeAllRanges();
  }
});
selMenu.addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if (!btn || !btn.dataset.cat || btn.disabled) return;
  const cat = btn.dataset.cat;
  const quote = selMenu.dataset.quote;
  if (cat === '__copy') {
    try { await navigator.clipboard.writeText(quote); } catch (_) {}
    selMenu.classList.add('hidden');
    return;
  }
  const badChars = [...quote].filter((c) => {
    const cp = c.codePointAt(0);
    return (cp >= 0xE000 && cp <= 0xF8FF) || cp === 0xFFFD ||
           (cp >= 0x2200 && cp <= 0x22FF) || (cp >= 0x1D400 && cp <= 0x1D7FF);
  }).length;
  if (badChars / Math.max(1, quote.length) > 0.12) {
    selMenu.classList.add('hidden');
    alert('选区包含公式字符，文本提取不可靠——建议点击 [图示] 区域用 📷 图片模式提问');
    return;
  }
  if (cat === '__translate') {
    selMenu.classList.add('hidden');
    openFloatCard('translate', { quote, page: Number(selMenu.dataset.page) });
    return;
  }
  if (cat === '__explain') {
    selMenu.classList.add('hidden');
    openFloatCard('explain', { quote, page: Number(selMenu.dataset.page) });
    return;
  }
  if (cat === '__note') {
    selMenu.classList.add('hidden');
    const linked = currentHighlights.find((h) =>
      quote.includes(h.quote.slice(0, 40)) || h.quote.includes(quote.slice(0, 40)));
    openFloatCard('note', { quote, page: Number(selMenu.dataset.page), hl_id: linked ? linked.id : null,
      rects: JSON.parse(selMenu.dataset.rects || '[]'),
      occurrence: Number(selMenu.dataset.occurrence || 0) });
    return;
  }
  if (cat.startsWith('__')) return;
  const page = Number(selMenu.dataset.page);
  const selRects = JSON.parse(selMenu.dataset.rects || '[]');
  const selOcc = Number(selMenu.dataset.occurrence || 0);
  selMenu.classList.add('hidden');
  window.getSelection().removeAllRanges();
  try {
    const r = await req(`/papers/${currentPaperId}/highlights`, {
      method: 'POST',
      body: JSON.stringify({ page, quote, category: cat, rects: selRects, occurrence: selOcc,
                             domain_id: currentPaperDomainId }),
    });
    const hl = { id: r.id, page, quote, category: cat };
    currentHighlights.push(hl);
    applyHighlight(hl);
    await renderHighlightsTab({ paper_id: currentPaperId });
    lastManualHl = { paperId: currentPaperId, id: r.id };
    showToast('已添加高亮');
  } catch (err) { alert(`高亮保存失败: ${err.message}`); }
});

document.getElementById('btn-toast-undo').addEventListener('click', async () => {
  if (!lastManualHl) { hideToast(); return; }
  try {
    await undoHighlight(lastManualHl);
  } catch (e) { alert(`撤销失败: ${e.message}`); }
  lastManualHl = null;
  hideToast();
});
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && lastManualHl
      && lastManualHl.paperId === currentPaperId && !e.target.closest('input, textarea')) {
    e.preventDefault();
    document.getElementById('btn-toast-undo').click();
  }
});

document.getElementById('btn-audit').addEventListener('click', async () => {
  const body = document.getElementById('audit-body');
  body.innerHTML = '<div class="fc-loading">统计中…</div>';
  document.getElementById('audit-mask').classList.remove('hidden');
  try {
    const d = await req('/audit/report');
    const evPct = d.summary_evidence.total ? Math.round(100 * d.summary_evidence.with_page / d.summary_evidence.total) : 0;
    const hitPct = d.llm_stats.total_calls ? Math.round(100 * d.llm_stats.cache_hits / d.llm_stats.total_calls) : 0;
    const catLabel = { background: '背景', approach: '解决思路', limitation: '局限' };
    body.innerHTML = `
      <div class="sum-section"><div class="sum-section-title">LLM 调用总览</div>
        <div class="sum-text">总调用 ${d.llm_stats.total_calls} 次 · 缓存命中 ${d.llm_stats.cache_hits}（${hitPct}%）· 平均延迟 ${d.llm_stats.mean_latency_ms}ms</div></div>
      <div class="sum-section"><div class="sum-section-title">逐任务明细</div>
        <table class="audit-table"><tr><th>任务</th><th>次数</th><th>缓存命中</th><th>成功</th><th>平均延迟</th></tr>
        ${d.tasks.map((t) => `<tr><td>${escapeHtml(t.task)}</td><td>${t.n}</td><td>${t.cache_hits}</td><td>${t.ok_n}</td><td>${t.mean_ms}ms</td></tr>`).join('')}</table></div>
      <div class="sum-section"><div class="sum-section-title">严谨性闭环</div>
        <div class="sum-text">摘要 evidence 逐字校验通过并定位页码：${d.summary_evidence.with_page}/${d.summary_evidence.total}（${evPct}%）</div></div>
      <div class="sum-section"><div class="sum-section-title">沉淀总量</div>
        <div class="sum-text">高亮 ${Object.entries(d.highlights).map(([k, v]) => `${catLabel[k] || k} ${v}`).join(' · ')}
        · 批注 ${d.notes} · 问答轮次 ${d.qa_turns} · 翻译句缓存 ${d.translation_segments}</div></div>`;
  } catch (e) {
    body.innerHTML = `<div class="fc-error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
});
document.getElementById('audit-close').addEventListener('click', () => {
  document.getElementById('audit-mask').classList.add('hidden');
});

const themeBtn = document.getElementById('btn-theme');
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  document.body.dataset.theme = t;
  themeBtn.textContent = t === 'light' ? '☀' : '🌙';
  try { localStorage.setItem('ui.theme', t); } catch (_) { /* non-fatal */ }
}
themeBtn.addEventListener('click', () => {
  applyTheme(document.body.dataset.theme === 'light' ? 'dark' : 'light');
});
let initialTheme = 'dark';
try { initialTheme = localStorage.getItem('ui.theme') || 'dark'; } catch (_) { /* default */ }
applyTheme(initialTheme);

function attachSidebarDrag(stripId, targetId, edge, storeKey) {
  const strip = document.getElementById(stripId);
  const target = document.getElementById(targetId);
  strip.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = target.getBoundingClientRect().width;
    const onMove = (ev) => {
      const delta = edge === 'east' ? ev.clientX - startX : startX - ev.clientX;
      const w = Math.min(480, Math.max(180, startW + delta));
      target.style.width = `${w}px`;
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      localStorage.setItem(storeKey, String(target.getBoundingClientRect().width));
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}
attachSidebarDrag('left-drag', 'left-panel', 'east', 'ui.leftW');
attachSidebarDrag('right-drag', 'right-panel', 'west', 'ui.rightW');
for (const [key, id] of [['ui.leftW', 'left-panel'], ['ui.rightW', 'right-panel']]) {
  let w = 0;
  try { w = Number(localStorage.getItem(key)); } catch (_) { /* default width */ }
  if (w) document.getElementById(id).style.width = `${Math.min(480, Math.max(180, w))}px`;
}
function resetLayout() {
  try {
    localStorage.removeItem('ui.leftW');
    localStorage.removeItem('ui.rightW');
  } catch (_) { /* storage unavailable */ }
  document.getElementById('left-panel').style.width = '';
  document.getElementById('right-panel').style.width = '';
  showToast('已恢复初始布局');
}
document.getElementById('btn-layout').addEventListener('click', resetLayout);

const paletteMask = document.getElementById('palette-mask');
let allPapersCache = [];

function paletteOpen() {
  paletteMask.classList.remove('hidden');
  const input = document.getElementById('palette-input');
  input.value = '';
  input.focus();
  if (!allPapersCache.length) {
    req('/papers').then((rows) => { allPapersCache = rows; paletteRender(''); }).catch(() => {});
  }
  paletteRender('');
}

function paletteClose() {
  paletteMask.classList.add('hidden');
}

function paletteRender(q) {
  const box = document.getElementById('palette-results');
  q = q.trim().toLowerCase();
  const papers = allPapersCache
    .filter((p) => !q || (p.title || '').toLowerCase().includes(q))
    .slice(0, 8);
  const actions = [
    { label: '导入论文', act: () => document.getElementById('btn-import').click() },
    { label: '打开设置', act: () => document.getElementById('btn-settings').click() },
    { label: '恢复默认布局', act: resetLayout },
  ].filter((a) => q && a.label.toLowerCase().includes(q));
  box.innerHTML =
    actions.map((a, i) => `<div class="palette-item" data-act="${i}">⚙ ${escapeHtml(a.label)}</div>`).join('') +
    papers.map((p) => `<div class="palette-item" data-pid="${p.paper_id}">📄 ${escapeHtml(p.title || '(untitled)')}</div>`).join('');
  box.querySelectorAll('[data-pid]').forEach((el) => {
    el.addEventListener('click', () => { paletteClose(); openPaper(Number(el.dataset.pid)); });
  });
  box.querySelectorAll('[data-act]').forEach((el) => {
    el.addEventListener('click', () => { paletteClose(); actions[Number(el.dataset.act)].act(); });
  });
}

document.getElementById('palette-input').addEventListener('input', (e) => paletteRender(e.target.value));
document.getElementById('palette-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    const first = document.querySelector('#palette-results [data-pid]');
    if (first) first.click();
  }
});
paletteMask.addEventListener('mousedown', (e) => {
  if (!e.target.closest('.palette')) paletteClose();
});
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    paletteMask.classList.contains('hidden') ? paletteOpen() : paletteClose();
  }
});

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function initLogoPhase() {
  const now = new Date();
  const sec = now.getSeconds() + now.getMilliseconds() / 1000;
  const periods = { 'od-1': 11, 'od-2': 17, 'od-3': 29, 'arc-1': 19, 'arc-2': 31 };
  const breatheOffset = { 'od-1': 0.4, 'od-2': 0.15, 'od-3': 0.7 };
  document.querySelectorAll('.lens-logo').forEach((logo, i) => {
    const shift = i * 2.3;
    logo.style.animationDelay = `${(-((sec + shift) % 4.5)).toFixed(3)}s`;
    const glow = logo.querySelector('.logo-glow');
    if (glow) glow.style.animationDelay = `${(-((sec + shift) % 12)).toFixed(3)}s`;
    for (const [cls, p] of Object.entries(periods)) {
      logo.querySelectorAll(`.${cls}`).forEach((el) => {
        const phase = (sec + shift) % p;
        if (cls.startsWith('arc')) {
          const breathe = (phase + p * 0.37) % p;
          el.style.animationDelay = `${(-phase).toFixed(3)}s, ${(-breathe).toFixed(3)}s`;
        } else {
          el.style.animationDelay = `${(-phase).toFixed(3)}s`;
          const dot = el.tagName === 'g' ? el.querySelector('circle') : null;
          if (dot) dot.style.animationDelay = `${(-((phase + p * (breatheOffset[cls] || 0)) % p)).toFixed(3)}s`;
        }
      });
    }
  });
}

const bootEl = document.getElementById('boot');
const bootBarEl = document.getElementById('boot-bar');
const bootPctEl = document.getElementById('boot-pct');
const bootStageEl = document.getElementById('boot-stage');
const bootRetryEl = document.getElementById('boot-retry');
const bootSteps = new Map(
  Array.from(document.querySelectorAll('#boot-steps li')).map((li) => [li.dataset.step, li]),
);
let bootVisible = false;
let bootReady = false;
let bootShownAt = 0;
let bootPct = 0;

function bootShow() {
  bootVisible = true;
  bootShownAt = Date.now();
  if (bootEl) bootEl.classList.remove('hidden');
}
function bootSet(step, state) {
  if (!bootVisible) return;
  const li = bootSteps.get(step);
  if (!li) return;
  li.classList.remove('is-run', 'is-done', 'is-fail');
  if (state) li.classList.add(`is-${state}`);
}
function bootSay(text, isError = false) {
  if (!bootStageEl) return;
  bootStageEl.textContent = text;
  bootStageEl.classList.toggle('is-error', !!isError);
}
function bootProgress(pct) {
  bootPct = Math.max(bootPct, Math.min(100, pct));
  if (bootBarEl) bootBarEl.style.width = `${bootPct}%`;
  if (bootPctEl) bootPctEl.textContent = `${Math.round(bootPct)}%`;
}
function bootFinish() {
  if (!bootVisible) return;
  bootReady = true;
  bootProgress(100);
  bootSay('就绪');
  if (bootRetryEl) bootRetryEl.classList.add('hidden');
  const hold = Math.max(0, 650 - (Date.now() - bootShownAt));
  setTimeout(() => {
    bootVisible = false;
    if (bootEl) bootEl.classList.add('is-leaving');
    setTimeout(() => { if (bootEl) bootEl.classList.add('hidden'); }, 600);
  }, hold);
}
function bootFail(message) {
  bootSay(message, true);
  bootSteps.forEach((li) => {
    if (!li.classList.contains('is-done')) li.classList.add('is-fail');
  });
  if (bootRetryEl) bootRetryEl.classList.remove('hidden');
}
if (bootRetryEl) bootRetryEl.addEventListener('click', () => location.reload());

async function waitForHealth(port, timeoutMs) {
  const started = Date.now();
  for (;;) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/health`, { cache: 'no-store' });
      if (res.ok) return true;
    } catch (_) { /* not up yet */ }
    if (Date.now() - started > timeoutMs) return false;
    await new Promise((r) => setTimeout(r, 300));
  }
}

async function bootStep(step, label, fn) {
  bootSet(step, 'run');
  bootSay(label);
  try {
    await fn();
    bootSet(step, 'done');
    return true;
  } catch (e) {
    console.error(`[boot] ${step} failed:`, e);
    bootSet(step, 'fail');
    return false;
  }
}

function renderHomeSkeleton() {
  const stats = document.getElementById('home-stats');
  if (stats && !stats.children.length) {
    stats.innerHTML = ['研究领域', '论文总量', '处理队列中', '累计高亮'].map((label) => (
      `<div class="stat-card"><div class="stat-num stat-num--pending">—</div>` +
      `<div class="stat-label">${label}</div></div>`
    )).join('');
  }
  const recent = document.getElementById('home-recent');
  if (recent && !recent.children.length) {
    recent.innerHTML = '<div class="empty-hint" style="padding:16px">正在读取最近打开的论文…</div>';
  }
}

async function probeDevPorts() {
  for (let p = 21800; p < 21810; p++) {
    try {
      const r = await fetch(`http://127.0.0.1:${p}/health`, { cache: 'no-store' });
      if (r.ok) return p;
    } catch (_) { /* try next */ }
  }
  return 0;
}

(async function boot() {
  window.__slBootStarted = true;
  showReaderLayer('placeholder');
  renderHomeSkeleton();
  initLogoPhase();
  bootShow();
  bootProgress(5);

  let port = 0;
  const qPort = Number(new URLSearchParams(location.search).get('port'));
  if (qPort) {
    port = qPort;
  } else if (!window.scholarlens) {
    port = await probeDevPorts();
  } else {
    try {
      const info = await window.scholarlens.getBackendInfo();
      if (info && info.running && info.port) port = Number(info.port);
    } catch (_) { /* main process not ready — fall through to offline */ }
    window.scholarlens.onBackendStatus((status) => {
      if (status === 'offline') {
        setStatus('offline');
        return;
      }
      if (status.startsWith('running:')) {
        const p = Number(status.split(':')[1]);
        if (!p) return;
        if (p !== apiPort) {
          apiPort = p;
          if (bootReady) refreshAll();
        }
        pollHealth();
      }
    });
  }

  if (!port) {
    setStatus('offline');
    bootFail('未找到本地引擎');
    return;
  }

  apiPort = port;
  bootSet('engine', 'run');
  bootSay('正在连接本地引擎…');
  setTimeout(() => {
    if (!bootReady && bootRetryEl) {
      bootRetryEl.classList.remove('hidden');
      bootSay('初始化比预期慢… 可点「重新初始化」', true);
    }
  }, 8000);
  const healthy = await waitForHealth(port, 60000);
  pollHealth();
  if (!healthy) {
    setStatus('offline');
    bootFail('本地引擎未响应（60s）');
    return;
  }
  bootSet('engine', 'done');
  bootProgress(18);

  silentRequests += 1;
  const domainsOk = await bootStep('domains', '正在读取研究领域…', async () => {
    await loadDomains();
    if (state.selectedDomain) await selectDomain(state.selectedDomain);
  });
  bootProgress(42);
  await bootStep('overview', '正在载入工作台概览…', async () => {
    if (!(await loadOverview())) throw new Error('overview unavailable');
  });
  bootProgress(68);
  await bootStep('history', '正在整理问答历史…', () => renderQaHistory({}));
  silentRequests -= 1;
  bootProgress(88);

  connectEvents();
  if (!domainsOk) {
    const list = document.getElementById('domain-list');
    if (list && !list.children.length) {
      list.innerHTML = '<div class="empty-hint">领域载入失败<br /><span class="sub">可点启动页的「重新初始化」重试</span></div>';
    }
  }
  bootFinish();
})().catch((err) => {
  console.error('[boot] failed:', err);
  bootFail(`初始化失败：${err && err.message ? err.message : err}`);
});

let events = null;

let dataRefreshTimer = 0;

async function refreshDomainData() {
  if (!state.selectedDomain) return;
  const papers = await req(`/domains/${state.selectedDomain}/papers`);
  const sig = JSON.stringify(papers);
  if (sig === state.papersSig) return;
  state.papers = papers;
  state.papersSig = sig;
  renderPapers();
}

function scheduleDataRefresh() {
  clearTimeout(dataRefreshTimer);
  dataRefreshTimer = setTimeout(async () => {
    try {
      await loadDomains(false);
      await refreshDomainData();
      if (!currentPaperId) await loadOverview();
    } catch (e) {
      console.error('refresh failed:', e);
    }
  }, 400);
}

function connectEvents() {
  if (events) { try { events.close(); } catch (_) { /* already closed */ } events = null; }
  const es = new EventSource(api('/events'));
  events = es;
  es.onmessage = async (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.version) {
        document.getElementById('status-version').textContent = `ScholarLens v${d.version}`;
        return;
      }
      if (d.progress && d.progress.paper_id === currentPaperId) {
        if (d.progress.pct >= 100) {
          await openPaper(currentPaperId);
        } else {
          updateProgressCard(d.progress.pct);
        }
      }
      if (d.domains_dirty || d.queue_idle) scheduleDataRefresh();
      if (d.batch_total !== undefined) updateBatchLine(d.batch_total, d.batch_done);
    } catch (_) { /* ignore malformed events */ }
  };
  es.onerror = () => {
    try { es.close(); } catch (_) { /* already closed */ }
    if (events === es) events = null;
    setTimeout(() => { if (!events) refreshAll(); }, 5000);
  };
}

async function refreshAll() {
  silentRequests += 1;
  try {
    await loadDomains(false);
    await refreshDomainData();
    if (!currentPaperId) await loadOverview();
  } catch (e) {
    console.error('refresh failed:', e);
  } finally {
    silentRequests -= 1;
  }
  connectEvents();
}
