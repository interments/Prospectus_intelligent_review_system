<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

const API = 'http://localhost:9000/api/v1';

type TaskStatus = 'queued' | 'running' | 'success' | 'failed' | string;
interface TaskItem {
  id: string;
  filename: string;
  status: TaskStatus;
  pdf_path?: string;
  result_path?: string;
  selected_modules?: string[];
  modules?: Record<string, { status: string; result_path?: string }>;
}
interface ResultItem { name: string; result_path: string }
interface AlertItem {
  message: string;
  page?: number | string;
  previous_event_page?: number | string;
  current_event_page?: number | string;
  previous_event_text?: string;
  current_event_text?: string;
}
interface OutputResult {
  alerts?: AlertItem[]
  issues?: AlertItem[]
  summary?: { status?: string; disclosed_count?: number; expected_count?: number; missing_count?: number; event_count?: number }
}

const fileInputRef = ref<HTMLInputElement | null>(null);
const selectedFile = ref<File | null>(null);
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
const selectedModules = ref<string[]>([]);
const modulesParallel = ref(true);
const panelOpen = ref({ price: false, shareholder: false, pledge: false });
const darkMode = ref(false);

const moduleCards = [
  { key: 'price_fluctuation', title: '价格波动披露', desc: '按相邻事件时间窗（<6个月 / ≥6个月）使用不同阈值识别异常并定位页码' },
  { key: 'shareholder_5pct', title: '5%股东披露', desc: '核查5%以上股东披露完整性并定位页码' },
  { key: 'pledge_freeze_decl', title: '质押冻结声明', desc: '核查5%股东与董监高核心技术人员质押冻结声明及未解除事件' },
] as const;

function toggleModule(key: string, checked: boolean) {
  const s = new Set(selectedModules.value);
  if (checked) s.add(key);
  else s.delete(key);
  selectedModules.value = Array.from(s);
}

function toggleModuleByCard(key: string) {
  toggleModule(key, !isModuleSelected(key));
}

function isModuleSelected(key: string) {
  return selectedModules.value.includes(key);
}
const pollTimer = ref<number | null>(null);
const listRefreshTimer = ref<number | null>(null);

const tasks = ref<TaskItem[]>([]);
const results = ref<ResultItem[]>([]);
const selectedResult = ref('');
const currentResult = ref<OutputResult | null>(null);
const moduleResults = ref<Record<string, OutputResult | null>>({
  price_fluctuation: null,
  shareholder_5pct: null,
  pledge_freeze_decl: null,
});
const activeAlertIndex = ref(-1);

const alerts = computed(() => currentResult.value?.alerts ?? []);
const is5pctResult = computed(() => !!currentResult.value?.summary);
const fivePctAlerts = computed(() => {
  if (!currentResult.value) return [] as AlertItem[];
  if ((currentResult.value.alerts ?? []).length) return currentResult.value.alerts as AlertItem[];
  return (currentResult.value.issues ?? []) as AlertItem[];
});

const priceAlerts = computed(() => (moduleResults.value.price_fluctuation?.alerts ?? []) as AlertItem[]);
const shareholderAlerts = computed(() => {
  const r = moduleResults.value.shareholder_5pct;
  if (!r) return [] as AlertItem[];
  if ((r.alerts ?? []).length) return r.alerts as AlertItem[];
  return (r.issues ?? []) as AlertItem[];
});
const pledgeAlerts = computed(() => {
  const r = moduleResults.value.pledge_freeze_decl;
  if (!r) return [] as AlertItem[];
  if ((r.alerts ?? []).length) return r.alerts as AlertItem[];
  return (r.issues ?? []) as AlertItem[];
});
const pdfBaseUrl = computed(() => localPdfUrl.value || serverPdfUrl.value || '');
const pdfFrameSrc = computed(() => {
  if (!pdfBaseUrl.value) return '';
  const hash: string[] = [`page=${page.value}`, 'zoom=page-fit'];
  if (currentSearchText.value) hash.push(`search=${encodeURIComponent(currentSearchText.value)}`);
  hash.push(`v=${pdfNonce.value}`);
  return `${pdfBaseUrl.value}#${hash.join('&')}`;
});

const locateHint = computed(() =>
  currentSearchText.value ? `已尝试高亮：${currentSearchText.value}` : '未启用高亮关键词',
);

function setProgress(p: number, text: string) {
  progress.value = Math.max(0, Math.min(100, p));
  progressText.value = text;
}
function resetLocalPdfUrl() {
  if (localPdfUrl.value) URL.revokeObjectURL(localPdfUrl.value);
  localPdfUrl.value = '';
}
function parsePage(raw: number | string | undefined) {
  if (typeof raw === 'number' && Number.isFinite(raw)) return Math.max(1, Math.floor(raw));
  const m = String(raw ?? '').match(/\d+/);
  return m ? Math.max(1, Number(m[0])) : 1;
}
function getAlertPages(a: AlertItem) {
  return {
    previous: parsePage(a.previous_event_page ?? a.page),
    current: parsePage(a.current_event_page ?? a.page),
  };
}
function pickBestSearchText(a: AlertItem, side: 'previous' | 'current') {
  const own = side === 'previous' ? a.previous_event_text : a.current_event_text;
  if (own?.trim()) return own.replace(/\s+/g, ' ').slice(0, 48);
  return (a.message || '').replace(/\s+/g, ' ').slice(0, 36);
}

function clickAlert(i: number, a: AlertItem, side: 'previous' | 'current' = 'current') {
  activeAlertIndex.value = i;
  locatedSide.value = side;
  page.value = side === 'previous' ? getAlertPages(a).previous : getAlertPages(a).current;
  currentSearchText.value = pickBestSearchText(a, side);
  pdfNonce.value += 1;
}

function triggerFilePicker() {
  fileInputRef.value?.click();
}

function toggleDarkMode() {
  darkMode.value = !darkMode.value;
  try {
    localStorage.setItem('ui.darkMode', darkMode.value ? '1' : '0');
  } catch {}
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
  if (fileInputRef.value) fileInputRef.value.value = '';
  selectedFile.value = null;
  pdfNonce.value += 1;
}

function applyPickedFile(f: File | null) {
  selectedFile.value = f;
  activeAlertIndex.value = -1;
  locatedSide.value = '';
  currentSearchText.value = '';
  if (!f) return;
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

function onPickFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0] ?? null;
  applyPickedFile(f);
}

function onDragOver(e: DragEvent) {
  e.preventDefault();
  dragOver.value = true;
}

function onDragLeave() {
  dragOver.value = false;
}

function onDropFile(e: DragEvent) {
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

function toPosixPath(p: string) {
  return String(p || '').replace(/\\/g, '/');
}

async function fetchResults() {
  const r = await fetch(`${API}/results`);
  const d = await r.json();
  const items = (d.items ?? []) as ResultItem[];

  const grouped = new Map<string, ResultItem>();
  for (const it of items) {
    const pRaw = String(it.result_path || '');
    const p = toPosixPath(pRaw);
    const m = p.match(/(.*\/task_[^/]+)\/[^/]+\/result\.json$/);
    if (m) {
      const taskRoot = m[1];
      const taskName = taskRoot.split('/').pop() || taskRoot;
      if (!grouped.has(taskRoot)) grouped.set(taskRoot, { name: taskName, result_path: taskRoot });
    } else {
      grouped.set(p, { ...it, result_path: p });
    }
  }
  results.value = Array.from(grouped.values());
}
function syncPdfByResultPath(path: string) {
  const target = toPosixPath(path);
  const task = tasks.value.find((x) => {
    if (!x.pdf_path) return false;
    if (toPosixPath(x.result_path || '') === target) return true;
    const mods = x.modules || {};
    return Object.values(mods).some((m) => {
      const rp = toPosixPath(m?.result_path || '');
      return rp === target || (target.includes('/task_') && rp.startsWith(target));
    });
  });
  if (!task?.pdf_path) return;
  serverPdfUrl.value = `${API}/file?path=${encodeURIComponent(task.pdf_path)}`;
  page.value = 1;
  pdfNonce.value += 1;
}
async function tryLoadResult(path: string) {
  const r = await fetch(`${API}/result?path=${encodeURIComponent(path)}`);
  if (!r.ok) return null;
  return await r.json() as OutputResult;
}

async function loadResultByPath(path: string) {
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
  } else {
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
  if (!selectedResult.value) return;
  resetLocalPdfUrl();
  await loadResultByPath(selectedResult.value);
}

async function setRunMode(parallel: boolean) {
  modulesParallel.value = parallel;
  try {
    await fetch(`${API}/runtime`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ modules_parallel: parallel }),
    });
  } catch {}
}

async function cancelTask(taskId: string) {
  try {
    await fetch(`${API}/tasks/${taskId}/cancel`, { method: 'POST' });
    if (taskId === currentTaskId.value) {
      running.value = false;
      setProgress(100, '任务已终止');
    }
    await fetchTasks();
  } catch (e) {
    alert('终止失败，请稍后重试');
  }
}

async function startTask() {
  if (!selectedFile.value || running.value) return;
  running.value = true;
  setProgress(10, '任务已创建');

  const fd = new FormData();
  fd.append('file', selectedFile.value);
  fd.append('modules', JSON.stringify(selectedModules.value));
  fd.append('modules_parallel', String(modulesParallel.value));
  const resp = await fetch(`${API}/tasks`, { method: 'POST', body: fd });
  const task: TaskItem = await resp.json();
  currentTaskId.value = task.id;
  if (task.pdf_path) serverPdfUrl.value = `${API}/file?path=${encodeURIComponent(task.pdf_path)}`;

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
  if (pollTimer.value) clearInterval(pollTimer.value);
  pollTimer.value = window.setInterval(async () => {
    if (!currentTaskId.value) return;
    const r = await fetch(`${API}/tasks/${currentTaskId.value}`);
    const t: TaskItem = await r.json();
    await fetchTasks();

    if (t.status === 'queued') setProgress(20, '排队中...');
    if (t.status === 'running') setProgress(70, '分析中（抽取/清洗/判定）...');

    if (t.status === 'success') {
      if (pollTimer.value) clearInterval(pollTimer.value);
      pollTimer.value = null;
      if (t.result_path) {
        await loadResultByPath(t.result_path);
        await fetchResults();
      }
      setProgress(100, '分析完成');
      running.value = false;
    }

    if (t.status === 'failed') {
      if (pollTimer.value) clearInterval(pollTimer.value);
      pollTimer.value = null;
      setProgress(100, '分析失败');
      running.value = false;
      alert('任务失败，请看后端 /api/v1/tasks 中的 log 字段');
    }

    if (t.status === 'cancelled') {
      if (pollTimer.value) clearInterval(pollTimer.value);
      pollTimer.value = null;
      setProgress(100, '任务已终止');
      running.value = false;
    }
  }, 2000);
}

onMounted(async () => {
  try {
    darkMode.value = localStorage.getItem('ui.darkMode') === '1';
  } catch {}

  await refreshLists();
  listRefreshTimer.value = window.setInterval(() => {
    refreshLists().catch(() => {});
  }, 5000);
  try {
    const r = await fetch(`${API}/runtime`);
    const d = await r.json();
    modulesParallel.value = !!d.modules_parallel;
  } catch {}
});
onBeforeUnmount(() => {
  if (pollTimer.value) clearInterval(pollTimer.value);
  if (listRefreshTimer.value) clearInterval(listRefreshTimer.value);
  resetLocalPdfUrl();
});
</script>

<template>
  <div class="page-wrap" :class="{ dark: darkMode }">
    <header class="topbar card">
      <div class="topbar-title">
        <h1>招股书智能审核系统</h1>
      </div>

      <div class="actions">
        <!-- hidden input, triggered by buttons -->
        <input ref="fileInputRef" class="file-input" type="file" accept="application/pdf" @change="onPickFile" />

        <div class="actions-row compact">
          <button class="btn secondary" @click="toggleDarkMode">{{ darkMode ? '☀️ 日间' : '🌙 夜间' }}</button>
          <button class="btn secondary" @click="refreshLists">刷新历史</button>

          <select v-model="selectedResult" @change="loadSelectedResult" class="select">
            <option value="">-- 历史分析记录 --</option>
            <option v-for="x in results" :key="x.result_path" :value="x.result_path">{{ x.name }}</option>
          </select>

          <div class="run-mode-toggle" title="执行模式">
            <button class="mode-btn" :class="{ on: !modulesParallel }" @click="setRunMode(false)">串行</button>
            <button class="mode-btn" :class="{ on: modulesParallel }" @click="setRunMode(true)">并行</button>
          </div>

          <span class="muted selected-modules">已选：{{ selectedModules.join('、') || '未选择' }}</span>

          <button class="btn" :disabled="!selectedFile || running || !selectedModules.length" @click="startTask">
            {{ running ? '执行中...' : `运行已选模块（${selectedModules.length}）` }}
          </button>

          <button class="btn secondary" @click="resetForNextFile">
            重新选择文件
          </button>
        </div>

        <div class="actions-row">
          <div class="module-cards">
            <div v-for="m in moduleCards" :key="m.key" class="module-card" :class="{ on: isModuleSelected(m.key) }" @click="toggleModuleByCard(m.key)">
              <div>
                <div class="module-title">{{ m.title }}</div>
                <div class="module-desc-row">
                  <div class="module-desc">{{ m.desc }}</div>
                  <span class="info-dot" :data-tip="m.desc">!</span>
                </div>
              </div>
              <label class="switch" @click.stop>
                <input
                  type="checkbox"
                  :checked="isModuleSelected(m.key)"
                  @change="toggleModule(m.key, ($event.target as HTMLInputElement).checked)"
                />
                <span class="slider"></span>
              </label>
            </div>
          </div>
        </div>


      </div>
    </header>

    <main class="layout">
      <section
        class="left card"
        :class="{ 'drop-over': dragOver }"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDropFile"
      >
        <div class="title-row">
          <h2>PDF 预览 {{ pdfName ? `（${pdfName}）` : '' }}</h2>
          <small>当前定位：{{ locatedSide || '-' }}；{{ locateHint }}</small>
        </div>

        <div v-if="!pdfBaseUrl" class="pdf-empty">
          <div class="pdf-empty-inner">
            <div class="pdf-empty-title">暂无招股书</div>
            <div class="muted" style="margin-top:6px;">支持拖拽 PDF 到此区域，或点击选择文件</div>
            <div class="empty-actions">
              <button class="btn" @click="triggerFilePicker">选择文件</button>
            </div>
          </div>
        </div>

        <iframe v-else :key="pdfFrameSrc" class="pdf" :src="pdfFrameSrc" title="pdf"></iframe>
      </section>

      <section class="right">
        <div class="card progress queue-card">
          <div class="queue-head">
            <h3>任务进度</h3>
            <span class="muted">{{ progress }}%</span>
          </div>
          <div class="progress-row">
            <span>{{ progressText }}</span>
          </div>
          <div class="bar"><div :style="{ width: `${progress}%` }"></div></div>
        </div>

        <div class="card queue-card">
          <div class="queue-head">
            <h3>任务队列</h3>
            <span class="muted">{{ tasks.length }} 条</span>
          </div>
          <div class="queue-scroll">
            <div v-if="!tasks.length" class="muted">暂无任务</div>
            <div v-for="q in tasks" :key="q.id" class="queue-item" style="display:block;">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                <span class="queue-file" :title="q.filename">{{ q.filename }}</span>
                <div style="display:flex;align-items:center;gap:8px;">
                  <span class="pill">{{ q.status }}</span>
                  <button
                    v-if="q.status === 'queued' || q.status === 'running'"
                    class="btn secondary"
                    style="padding:4px 8px;font-size:12px;"
                    @click="cancelTask(q.id)"
                  >终止</button>
                </div>
              </div>
              <div class="module-status-row" v-if="q.modules">
                <span class="module-badge" v-for="(m, name) in q.modules" :key="name">
                  {{ name }} · {{ m.status }}
                </span>
              </div>
            </div>
          </div>
        </div>

        <div class="results-stack">
          <div class="card alerts-card integrated" :class="{ open: panelOpen.price }">
            <div class="integrated-head" @click="panelOpen.price = !panelOpen.price">
              <div class="collapse-head-main">
                <span class="collapse-title">价格波动披露结果（{{ priceAlerts.length }}）</span>
                <div class="collapse-subtitle">按相邻事件时间窗（&lt;6个月 / ≥6个月）应用不同阈值识别异常波动</div>
              </div>
              <span class="collapse-arrow" :class="{ open: panelOpen.price }">></span>
            </div>
            <div class="collapse-body" :class="{ open: panelOpen.price }">
              <div class="alerts-scroll">
                <div v-if="!priceAlerts.length" class="empty-block">暂无披露结果</div>
                <div
                  v-for="(a, i) in priceAlerts"
                  :key="`pf-${i}-${a.page}-${a.previous_event_page}-${a.current_event_page}`"
                  class="alert-item"
                  :class="{ active: activeAlertIndex === i }"
                  @click="clickAlert(i, a, 'current')"
                >
                  <p>{{ a.message }}</p>
                  <div class="meta-btns">
                    <button @click.stop="clickAlert(i, a, 'previous')">前事件 P{{ getAlertPages(a).previous }}</button>
                    <button @click.stop="clickAlert(i, a, 'current')">后事件 P{{ getAlertPages(a).current }}</button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="card alerts-card integrated" :class="{ open: panelOpen.shareholder }">
            <div class="integrated-head" @click="panelOpen.shareholder = !panelOpen.shareholder">
              <div class="collapse-head-main">
                <span class="collapse-title">5%股东披露结果（{{ shareholderAlerts.length }})</span>
                <div class="collapse-subtitle" v-if="moduleResults.shareholder_5pct?.summary">
                  状态：{{ moduleResults.shareholder_5pct.summary.status }} / 缺失：{{ moduleResults.shareholder_5pct.summary.missing_count }}
                </div>
                <div class="collapse-subtitle" v-else>核查5%以上股东披露完整性与页码溯源</div>
              </div>
              <span class="collapse-arrow" :class="{ open: panelOpen.shareholder }">></span>
            </div>
            <div class="collapse-body" :class="{ open: panelOpen.shareholder }">
              <div class="alerts-scroll">
                <div v-if="!shareholderAlerts.length" class="empty-block">暂无披露结果</div>
                <div
                  v-for="(a, i) in shareholderAlerts"
                  :key="`s5-${i}-${a.page}-${a.message}`"
                  class="alert-item"
                  @click="clickAlert(i, a, 'current')"
                >
                  <p>{{ a.message }}</p>
                  <div class="meta-btns">
                    <button @click.stop="clickAlert(i, a, 'current')">定位页码 P{{ parsePage(a.page) }}</button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="card alerts-card integrated" :class="{ open: panelOpen.pledge }">
            <div class="integrated-head" @click="panelOpen.pledge = !panelOpen.pledge">
              <div class="collapse-head-main">
                <span class="collapse-title">质押冻结声明结果（{{ pledgeAlerts.length }})</span>
                <div class="collapse-subtitle" v-if="moduleResults.pledge_freeze_decl?.summary">
                  状态：{{ moduleResults.pledge_freeze_decl.summary.status }} / 事件：{{ moduleResults.pledge_freeze_decl.summary.event_count || 0 }}
                </div>
                <div class="collapse-subtitle" v-else>核查5%股东与董监高核心技术人员质押冻结声明</div>
              </div>
              <span class="collapse-arrow" :class="{ open: panelOpen.pledge }">></span>
            </div>
            <div class="collapse-body" :class="{ open: panelOpen.pledge }">
              <div class="alerts-scroll">
                <div v-if="!pledgeAlerts.length" class="empty-block">暂无披露结果</div>
                <div
                  v-for="(a, i) in pledgeAlerts"
                  :key="`pfd-${i}-${a.page}-${a.message}`"
                  class="alert-item"
                  @click="clickAlert(i, a, 'current')"
                >
                  <p>{{ a.message }}</p>
                  <div class="meta-btns">
                    <button @click.stop="clickAlert(i, a, 'current')">定位页码 P{{ parsePage(a.page) }}</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>
