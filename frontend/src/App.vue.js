import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
const API = 'http://localhost:9000/api/v1';
const fileInputRef = ref(null);
const selectedFile = ref(null);
const dragOver = ref(false);
const pdfName = ref('');
const localPdfUrl = ref('');
const serverPdfUrl = ref('');
const page = ref(1);
const currentSearchText = ref('');
const locatedSide = ref('');
const pdfNonce = ref(0);
const progress = ref(0);
const progressText = ref('未开始');
const running = ref(false);
const currentTaskId = ref('');
const selectedModules = ref(['price_fluctuation']);
const modulesParallel = ref(true);
const panelOpen = ref({ price: false, shareholder: false, pledge: false });
const moduleCards = [
    { key: 'price_fluctuation', title: '价格波动披露', desc: '按相邻事件时间窗（<6个月 / ≥6个月）使用不同阈值识别异常并定位页码' },
    { key: 'shareholder_5pct', title: '5%股东披露', desc: '核查5%以上股东披露完整性并定位页码' },
    { key: 'pledge_freeze_decl', title: '质押冻结声明', desc: '核查5%股东与董监高核心技术人员质押冻结声明及未解除事件' },
];
function toggleModule(key, checked) {
    const s = new Set(selectedModules.value);
    if (checked)
        s.add(key);
    else
        s.delete(key);
    selectedModules.value = Array.from(s);
}
function isModuleSelected(key) {
    return selectedModules.value.includes(key);
}
const pollTimer = ref(null);
const listRefreshTimer = ref(null);
const tasks = ref([]);
const results = ref([]);
const selectedResult = ref('');
const currentResult = ref(null);
const moduleResults = ref({
    price_fluctuation: null,
    shareholder_5pct: null,
    pledge_freeze_decl: null,
});
const activeAlertIndex = ref(-1);
const alerts = computed(() => currentResult.value?.alerts ?? []);
const is5pctResult = computed(() => !!currentResult.value?.summary);
const fivePctAlerts = computed(() => {
    if (!currentResult.value)
        return [];
    if ((currentResult.value.alerts ?? []).length)
        return currentResult.value.alerts;
    return (currentResult.value.issues ?? []);
});
const priceAlerts = computed(() => (moduleResults.value.price_fluctuation?.alerts ?? []));
const shareholderAlerts = computed(() => {
    const r = moduleResults.value.shareholder_5pct;
    if (!r)
        return [];
    if ((r.alerts ?? []).length)
        return r.alerts;
    return (r.issues ?? []);
});
const pledgeAlerts = computed(() => {
    const r = moduleResults.value.pledge_freeze_decl;
    if (!r)
        return [];
    if ((r.alerts ?? []).length)
        return r.alerts;
    return (r.issues ?? []);
});
const pdfBaseUrl = computed(() => localPdfUrl.value || serverPdfUrl.value || '');
const pdfFrameSrc = computed(() => {
    if (!pdfBaseUrl.value)
        return '';
    const hash = [`page=${page.value}`, 'zoom=page-fit'];
    if (currentSearchText.value)
        hash.push(`search=${encodeURIComponent(currentSearchText.value)}`);
    hash.push(`v=${pdfNonce.value}`);
    return `${pdfBaseUrl.value}#${hash.join('&')}`;
});
const locateHint = computed(() => currentSearchText.value ? `已尝试高亮：${currentSearchText.value}` : '未启用高亮关键词');
function setProgress(p, text) {
    progress.value = Math.max(0, Math.min(100, p));
    progressText.value = text;
}
function resetLocalPdfUrl() {
    if (localPdfUrl.value)
        URL.revokeObjectURL(localPdfUrl.value);
    localPdfUrl.value = '';
}
function parsePage(raw) {
    if (typeof raw === 'number' && Number.isFinite(raw))
        return Math.max(1, Math.floor(raw));
    const m = String(raw ?? '').match(/\d+/);
    return m ? Math.max(1, Number(m[0])) : 1;
}
function getAlertPages(a) {
    return {
        previous: parsePage(a.previous_event_page ?? a.page),
        current: parsePage(a.current_event_page ?? a.page),
    };
}
function pickBestSearchText(a, side) {
    const own = side === 'previous' ? a.previous_event_text : a.current_event_text;
    if (own?.trim())
        return own.replace(/\s+/g, ' ').slice(0, 48);
    return (a.message || '').replace(/\s+/g, ' ').slice(0, 36);
}
function clickAlert(i, a, side = 'current') {
    activeAlertIndex.value = i;
    locatedSide.value = side;
    page.value = side === 'previous' ? getAlertPages(a).previous : getAlertPages(a).current;
    currentSearchText.value = pickBestSearchText(a, side);
    pdfNonce.value += 1;
}
function triggerFilePicker() {
    fileInputRef.value?.click();
}
function resetForNextFile() {
    selectedResult.value = '';
    currentResult.value = null;
    activeAlertIndex.value = -1;
    locatedSide.value = '';
    currentSearchText.value = '';
    resetLocalPdfUrl();
    serverPdfUrl.value = '';
    pdfName.value = '';
    page.value = 1;
    setProgress(0, '未开始');
    if (fileInputRef.value)
        fileInputRef.value.value = '';
    selectedFile.value = null;
    pdfNonce.value += 1;
}
function applyPickedFile(f) {
    selectedFile.value = f;
    activeAlertIndex.value = -1;
    locatedSide.value = '';
    currentSearchText.value = '';
    if (!f)
        return;
    if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) {
        selectedFile.value = null;
        alert('请上传 PDF 文件');
        return;
    }
    pdfName.value = f.name;
    resetLocalPdfUrl();
    localPdfUrl.value = URL.createObjectURL(f);
    page.value = 1;
    pdfNonce.value += 1;
}
function onPickFile(e) {
    const f = e.target.files?.[0] ?? null;
    applyPickedFile(f);
}
function onDragOver(e) {
    e.preventDefault();
    dragOver.value = true;
}
function onDragLeave() {
    dragOver.value = false;
}
function onDropFile(e) {
    e.preventDefault();
    dragOver.value = false;
    const f = e.dataTransfer?.files?.[0] ?? null;
    applyPickedFile(f);
}
async function fetchTasks() {
    const r = await fetch(`${API}/tasks`);
    const d = await r.json();
    tasks.value = d.items ?? [];
}
async function refreshLists() {
    await Promise.all([fetchTasks(), fetchResults()]);
}
function toPosixPath(p) {
    return String(p || '').replace(/\\/g, '/');
}
async function fetchResults() {
    const r = await fetch(`${API}/results`);
    const d = await r.json();
    const items = (d.items ?? []);
    const grouped = new Map();
    for (const it of items) {
        const pRaw = String(it.result_path || '');
        const p = toPosixPath(pRaw);
        const m = p.match(/(.*\/task_[^/]+)\/[^/]+\/result\.json$/);
        if (m) {
            const taskRoot = m[1];
            const taskName = taskRoot.split('/').pop() || taskRoot;
            if (!grouped.has(taskRoot))
                grouped.set(taskRoot, { name: taskName, result_path: taskRoot });
        }
        else {
            grouped.set(p, { ...it, result_path: p });
        }
    }
    results.value = Array.from(grouped.values());
}
function syncPdfByResultPath(path) {
    const target = toPosixPath(path);
    const task = tasks.value.find((x) => {
        if (!x.pdf_path)
            return false;
        if (toPosixPath(x.result_path || '') === target)
            return true;
        const mods = x.modules || {};
        return Object.values(mods).some((m) => {
            const rp = toPosixPath(m?.result_path || '');
            return rp === target || (target.includes('/task_') && rp.startsWith(target));
        });
    });
    if (!task?.pdf_path)
        return;
    serverPdfUrl.value = `${API}/file?path=${encodeURIComponent(task.pdf_path)}`;
    page.value = 1;
    pdfNonce.value += 1;
}
async function tryLoadResult(path) {
    const r = await fetch(`${API}/result?path=${encodeURIComponent(path)}`);
    if (!r.ok)
        return null;
    return await r.json();
}
async function loadResultByPath(path) {
    const normalizedPath = toPosixPath(path);
    const primary = normalizedPath.endsWith('/result.json') ? await tryLoadResult(normalizedPath) : null;
    currentResult.value = primary;
    moduleResults.value = { price_fluctuation: null, shareholder_5pct: null, pledge_freeze_decl: null };
    const m = normalizedPath.match(/(.*\/task_[^/]+)\/(price_fluctuation|shareholder_5pct)\/result\.json$/);
    const taskRoot = m ? m[1] : (normalizedPath.includes('/task_') && !normalizedPath.endsWith('/result.json') ? normalizedPath : null);
    if (taskRoot) {
        moduleResults.value.price_fluctuation = await tryLoadResult(`${taskRoot}/price_fluctuation/result.json`);
        moduleResults.value.shareholder_5pct = await tryLoadResult(`${taskRoot}/shareholder_5pct/result.json`);
        moduleResults.value.pledge_freeze_decl = await tryLoadResult(`${taskRoot}/pledge_freeze_decl/result.json`);
        currentResult.value = moduleResults.value.price_fluctuation || moduleResults.value.shareholder_5pct || moduleResults.value.pledge_freeze_decl;
    }
    else {
        // 兼容旧结构
        moduleResults.value.price_fluctuation = primary && !primary.summary ? primary : null;
        moduleResults.value.shareholder_5pct = primary && primary.summary ? primary : null;
    }
    activeAlertIndex.value = -1;
    currentSearchText.value = '';
    locatedSide.value = '';
    syncPdfByResultPath(taskRoot || path);
}
async function loadSelectedResult() {
    if (!selectedResult.value)
        return;
    resetLocalPdfUrl();
    await loadResultByPath(selectedResult.value);
}
async function setRunMode(parallel) {
    modulesParallel.value = parallel;
    try {
        await fetch(`${API}/runtime`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ modules_parallel: parallel }),
        });
    }
    catch { }
}
async function startTask() {
    if (!selectedFile.value || running.value)
        return;
    running.value = true;
    setProgress(10, '任务已创建');
    const fd = new FormData();
    fd.append('file', selectedFile.value);
    fd.append('modules', JSON.stringify(selectedModules.value));
    fd.append('modules_parallel', String(modulesParallel.value));
    const resp = await fetch(`${API}/tasks`, { method: 'POST', body: fd });
    const task = await resp.json();
    currentTaskId.value = task.id;
    if (task.pdf_path)
        serverPdfUrl.value = `${API}/file?path=${encodeURIComponent(task.pdf_path)}`;
    await fetchTasks();
    if (task.status === 'success' && task.result_path) {
        await loadResultByPath(task.result_path);
        await fetchResults();
        setProgress(100, '命中缓存结果，分析完成');
        running.value = false;
        return;
    }
    startPolling();
}
function startPolling() {
    if (pollTimer.value)
        clearInterval(pollTimer.value);
    pollTimer.value = window.setInterval(async () => {
        if (!currentTaskId.value)
            return;
        const r = await fetch(`${API}/tasks/${currentTaskId.value}`);
        const t = await r.json();
        await fetchTasks();
        if (t.status === 'queued')
            setProgress(20, '排队中...');
        if (t.status === 'running')
            setProgress(70, '分析中（抽取/清洗/判定）...');
        if (t.status === 'success') {
            if (pollTimer.value)
                clearInterval(pollTimer.value);
            pollTimer.value = null;
            if (t.result_path) {
                await loadResultByPath(t.result_path);
                await fetchResults();
            }
            setProgress(100, '分析完成');
            running.value = false;
        }
        if (t.status === 'failed') {
            if (pollTimer.value)
                clearInterval(pollTimer.value);
            pollTimer.value = null;
            setProgress(100, '分析失败');
            running.value = false;
            alert('任务失败，请看后端 /api/v1/tasks 中的 log 字段');
        }
    }, 2000);
}
onMounted(async () => {
    await refreshLists();
    listRefreshTimer.value = window.setInterval(() => {
        refreshLists().catch(() => { });
    }, 5000);
    try {
        const r = await fetch(`${API}/runtime`);
        const d = await r.json();
        modulesParallel.value = !!d.modules_parallel;
    }
    catch { }
});
onBeforeUnmount(() => {
    if (pollTimer.value)
        clearInterval(pollTimer.value);
    if (listRefreshTimer.value)
        clearInterval(listRefreshTimer.value);
    resetLocalPdfUrl();
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "page-wrap" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "topbar card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onChange: (__VLS_ctx.onPickFile) },
    ref: "fileInputRef",
    ...{ class: "file-input" },
    type: "file",
    accept: "application/pdf",
});
/** @type {typeof __VLS_ctx.fileInputRef} */ ;
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.refreshLists) },
    ...{ class: "btn secondary" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    ...{ onChange: (__VLS_ctx.loadSelectedResult) },
    value: (__VLS_ctx.selectedResult),
    ...{ class: "select" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
for (const [x] of __VLS_getVForSourceType((__VLS_ctx.results))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (x.result_path),
        value: (x.result_path),
    });
    (x.name);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "module-cards" },
});
for (const [m] of __VLS_getVForSourceType((__VLS_ctx.moduleCards))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        key: (m.key),
        ...{ class: "module-card" },
        ...{ class: ({ on: __VLS_ctx.isModuleSelected(m.key) }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "module-title" },
    });
    (m.title);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "module-desc-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "module-desc" },
    });
    (m.desc);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "info-dot" },
        'data-tip': (m.desc),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "switch" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onChange: (...[$event]) => {
                __VLS_ctx.toggleModule(m.key, $event.target.checked);
            } },
        type: "checkbox",
        checked: (__VLS_ctx.isModuleSelected(m.key)),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "slider" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "run-mode-toggle" },
    title: "执行模式",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setRunMode(false);
        } },
    ...{ class: "mode-btn" },
    ...{ class: ({ on: !__VLS_ctx.modulesParallel }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setRunMode(true);
        } },
    ...{ class: "mode-btn" },
    ...{ class: ({ on: __VLS_ctx.modulesParallel }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.startTask) },
    ...{ class: "btn" },
    disabled: (!__VLS_ctx.selectedFile || __VLS_ctx.running || !__VLS_ctx.selectedModules.length),
});
(__VLS_ctx.running ? '执行中...' : `运行已选模块（${__VLS_ctx.selectedModules.length}）`);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.resetForNextFile) },
    ...{ class: "btn secondary" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "progress card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "progress-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.progressText);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.progress);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "bar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ style: ({ width: `${__VLS_ctx.progress}%` }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "layout" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ onDragover: (__VLS_ctx.onDragOver) },
    ...{ onDragleave: (__VLS_ctx.onDragLeave) },
    ...{ onDrop: (__VLS_ctx.onDropFile) },
    ...{ class: "left card" },
    ...{ class: ({ 'drop-over': __VLS_ctx.dragOver }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "title-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
(__VLS_ctx.pdfName ? `（${__VLS_ctx.pdfName}）` : '');
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
(__VLS_ctx.locatedSide || '-');
(__VLS_ctx.locateHint);
if (!__VLS_ctx.pdfBaseUrl) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pdf-empty" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pdf-empty-inner" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pdf-empty-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "muted" },
        ...{ style: {} },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-actions" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.triggerFilePicker) },
        ...{ class: "btn" },
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.iframe, __VLS_intrinsicElements.iframe)({
        key: (__VLS_ctx.pdfFrameSrc),
        ...{ class: "pdf" },
        src: (__VLS_ctx.pdfFrameSrc),
        title: "pdf",
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "right" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "card queue-card" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "queue-head" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "muted" },
});
(__VLS_ctx.tasks.length);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "queue-scroll" },
});
if (!__VLS_ctx.tasks.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "muted" },
    });
}
for (const [q] of __VLS_getVForSourceType((__VLS_ctx.tasks))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        key: (q.id),
        ...{ class: "queue-item" },
        ...{ style: {} },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ style: {} },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "queue-file" },
        title: (q.filename),
    });
    (q.filename);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "pill" },
    });
    (q.status);
    if (q.modules) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "module-status-row" },
        });
        for (const [m, name] of __VLS_getVForSourceType((q.modules))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "module-badge" },
                key: (name),
            });
            (name);
            (m.status);
        }
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "results-stack" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "card alerts-card integrated" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.price }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.panelOpen.price = !__VLS_ctx.panelOpen.price;
        } },
    ...{ class: "integrated-head" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "collapse-head-main" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "collapse-title" },
});
(__VLS_ctx.priceAlerts.length);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "collapse-subtitle" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "collapse-arrow" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.price }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "collapse-body" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.price }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "alerts-scroll" },
});
if (!__VLS_ctx.priceAlerts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-block" },
    });
}
for (const [a, i] of __VLS_getVForSourceType((__VLS_ctx.priceAlerts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.clickAlert(i, a, 'current');
            } },
        key: (`pf-${i}-${a.page}-${a.previous_event_page}-${a.current_event_page}`),
        ...{ class: "alert-item" },
        ...{ class: ({ active: __VLS_ctx.activeAlertIndex === i }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (a.message);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "meta-btns" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.clickAlert(i, a, 'previous');
            } },
    });
    (__VLS_ctx.getAlertPages(a).previous);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.clickAlert(i, a, 'current');
            } },
    });
    (__VLS_ctx.getAlertPages(a).current);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "card alerts-card integrated" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.shareholder }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.panelOpen.shareholder = !__VLS_ctx.panelOpen.shareholder;
        } },
    ...{ class: "integrated-head" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "collapse-head-main" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "collapse-title" },
});
(__VLS_ctx.shareholderAlerts.length);
if (__VLS_ctx.moduleResults.shareholder_5pct?.summary) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "collapse-subtitle" },
    });
    (__VLS_ctx.moduleResults.shareholder_5pct.summary.status);
    (__VLS_ctx.moduleResults.shareholder_5pct.summary.missing_count);
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "collapse-subtitle" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "collapse-arrow" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.shareholder }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "collapse-body" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.shareholder }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "alerts-scroll" },
});
if (!__VLS_ctx.shareholderAlerts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-block" },
    });
}
for (const [a, i] of __VLS_getVForSourceType((__VLS_ctx.shareholderAlerts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.clickAlert(i, a, 'current');
            } },
        key: (`s5-${i}-${a.page}-${a.message}`),
        ...{ class: "alert-item" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (a.message);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "meta-btns" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.clickAlert(i, a, 'current');
            } },
    });
    (__VLS_ctx.parsePage(a.page));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "card alerts-card integrated" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.pledge }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.panelOpen.pledge = !__VLS_ctx.panelOpen.pledge;
        } },
    ...{ class: "integrated-head" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "collapse-head-main" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "collapse-title" },
});
(__VLS_ctx.pledgeAlerts.length);
if (__VLS_ctx.moduleResults.pledge_freeze_decl?.summary) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "collapse-subtitle" },
    });
    (__VLS_ctx.moduleResults.pledge_freeze_decl.summary.status);
    (__VLS_ctx.moduleResults.pledge_freeze_decl.summary.event_count || 0);
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "collapse-subtitle" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "collapse-arrow" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.pledge }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "collapse-body" },
    ...{ class: ({ open: __VLS_ctx.panelOpen.pledge }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "alerts-scroll" },
});
if (!__VLS_ctx.pledgeAlerts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-block" },
    });
}
for (const [a, i] of __VLS_getVForSourceType((__VLS_ctx.pledgeAlerts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.clickAlert(i, a, 'current');
            } },
        key: (`pfd-${i}-${a.page}-${a.message}`),
        ...{ class: "alert-item" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (a.message);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "meta-btns" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.clickAlert(i, a, 'current');
            } },
    });
    (__VLS_ctx.parsePage(a.page));
}
/** @type {__VLS_StyleScopedClasses['page-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['topbar']} */ ;
/** @type {__VLS_StyleScopedClasses['card']} */ ;
/** @type {__VLS_StyleScopedClasses['actions']} */ ;
/** @type {__VLS_StyleScopedClasses['file-input']} */ ;
/** @type {__VLS_StyleScopedClasses['btn']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['select']} */ ;
/** @type {__VLS_StyleScopedClasses['module-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['module-card']} */ ;
/** @type {__VLS_StyleScopedClasses['module-title']} */ ;
/** @type {__VLS_StyleScopedClasses['module-desc-row']} */ ;
/** @type {__VLS_StyleScopedClasses['module-desc']} */ ;
/** @type {__VLS_StyleScopedClasses['info-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['switch']} */ ;
/** @type {__VLS_StyleScopedClasses['slider']} */ ;
/** @type {__VLS_StyleScopedClasses['run-mode-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['mode-btn']} */ ;
/** @type {__VLS_StyleScopedClasses['mode-btn']} */ ;
/** @type {__VLS_StyleScopedClasses['btn']} */ ;
/** @type {__VLS_StyleScopedClasses['btn']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['progress']} */ ;
/** @type {__VLS_StyleScopedClasses['card']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-row']} */ ;
/** @type {__VLS_StyleScopedClasses['bar']} */ ;
/** @type {__VLS_StyleScopedClasses['layout']} */ ;
/** @type {__VLS_StyleScopedClasses['left']} */ ;
/** @type {__VLS_StyleScopedClasses['card']} */ ;
/** @type {__VLS_StyleScopedClasses['title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['pdf-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['pdf-empty-inner']} */ ;
/** @type {__VLS_StyleScopedClasses['pdf-empty-title']} */ ;
/** @type {__VLS_StyleScopedClasses['muted']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['btn']} */ ;
/** @type {__VLS_StyleScopedClasses['pdf']} */ ;
/** @type {__VLS_StyleScopedClasses['right']} */ ;
/** @type {__VLS_StyleScopedClasses['card']} */ ;
/** @type {__VLS_StyleScopedClasses['queue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['queue-head']} */ ;
/** @type {__VLS_StyleScopedClasses['muted']} */ ;
/** @type {__VLS_StyleScopedClasses['queue-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['muted']} */ ;
/** @type {__VLS_StyleScopedClasses['queue-item']} */ ;
/** @type {__VLS_StyleScopedClasses['queue-file']} */ ;
/** @type {__VLS_StyleScopedClasses['pill']} */ ;
/** @type {__VLS_StyleScopedClasses['module-status-row']} */ ;
/** @type {__VLS_StyleScopedClasses['module-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['results-stack']} */ ;
/** @type {__VLS_StyleScopedClasses['card']} */ ;
/** @type {__VLS_StyleScopedClasses['alerts-card']} */ ;
/** @type {__VLS_StyleScopedClasses['integrated']} */ ;
/** @type {__VLS_StyleScopedClasses['integrated-head']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-head-main']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-title']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-arrow']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-body']} */ ;
/** @type {__VLS_StyleScopedClasses['alerts-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-block']} */ ;
/** @type {__VLS_StyleScopedClasses['alert-item']} */ ;
/** @type {__VLS_StyleScopedClasses['meta-btns']} */ ;
/** @type {__VLS_StyleScopedClasses['card']} */ ;
/** @type {__VLS_StyleScopedClasses['alerts-card']} */ ;
/** @type {__VLS_StyleScopedClasses['integrated']} */ ;
/** @type {__VLS_StyleScopedClasses['integrated-head']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-head-main']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-title']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-arrow']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-body']} */ ;
/** @type {__VLS_StyleScopedClasses['alerts-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-block']} */ ;
/** @type {__VLS_StyleScopedClasses['alert-item']} */ ;
/** @type {__VLS_StyleScopedClasses['meta-btns']} */ ;
/** @type {__VLS_StyleScopedClasses['card']} */ ;
/** @type {__VLS_StyleScopedClasses['alerts-card']} */ ;
/** @type {__VLS_StyleScopedClasses['integrated']} */ ;
/** @type {__VLS_StyleScopedClasses['integrated-head']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-head-main']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-title']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-arrow']} */ ;
/** @type {__VLS_StyleScopedClasses['collapse-body']} */ ;
/** @type {__VLS_StyleScopedClasses['alerts-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-block']} */ ;
/** @type {__VLS_StyleScopedClasses['alert-item']} */ ;
/** @type {__VLS_StyleScopedClasses['meta-btns']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            fileInputRef: fileInputRef,
            selectedFile: selectedFile,
            dragOver: dragOver,
            pdfName: pdfName,
            locatedSide: locatedSide,
            progress: progress,
            progressText: progressText,
            running: running,
            selectedModules: selectedModules,
            modulesParallel: modulesParallel,
            panelOpen: panelOpen,
            moduleCards: moduleCards,
            toggleModule: toggleModule,
            isModuleSelected: isModuleSelected,
            tasks: tasks,
            results: results,
            selectedResult: selectedResult,
            moduleResults: moduleResults,
            activeAlertIndex: activeAlertIndex,
            priceAlerts: priceAlerts,
            shareholderAlerts: shareholderAlerts,
            pledgeAlerts: pledgeAlerts,
            pdfBaseUrl: pdfBaseUrl,
            pdfFrameSrc: pdfFrameSrc,
            locateHint: locateHint,
            parsePage: parsePage,
            getAlertPages: getAlertPages,
            clickAlert: clickAlert,
            triggerFilePicker: triggerFilePicker,
            resetForNextFile: resetForNextFile,
            onPickFile: onPickFile,
            onDragOver: onDragOver,
            onDragLeave: onDragLeave,
            onDropFile: onDropFile,
            refreshLists: refreshLists,
            loadSelectedResult: loadSelectedResult,
            setRunMode: setRunMode,
            startTask: startTask,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
