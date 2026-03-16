<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

const DEFAULT_API = (import.meta as any).env?.VITE_API_BASE_URL || 'http://localhost:9010/api/v1';
const apiBaseOverride = ref('');
const API = computed(() => apiBaseOverride.value || DEFAULT_API);

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
type ThemeMode = 'auto' | 'light' | 'dark';
const themeMode = ref<ThemeMode>('auto');
const darkMode = ref(false);
const showMoreMenu = ref(false);
const moreBtnRef = ref<HTMLElement | null>(null);
const moreMenuPos = ref({ top: 0, right: 0 });

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
const manageTasksMode = ref(false);
const selectedTaskIds = ref<string[]>([]);
const results = ref<ResultItem[]>([]);
const manageResultsMode = ref(false);
const selectedResultPaths = ref<string[]>([]);
const showSettingsModal = ref(false);
const settingsValidationMsg = ref('');
const settingsConnMsg = ref('');
const settings = ref({
  apiBaseUrl: '',
  llmBaseUrl: '',
  llmApiKey: '',
  llmModel: '',
  redisUrl: '',
  redisQueueKey: 'prospectus:task_queue',
  flaskPort: '9010',
  modulesParallel: 'true',
});
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

function normalizeUserFacingPassText(raw: string) {
  const s = String(raw || '').trim();
  if (!s) return '未发现明显异常，请继续人工复核关键章节。';
  return s
    .replace(/阴性样本[:：]?/g, '')
    .replace(/非阴性样本[:：]?/g, '')
    .replace(/未发现问题/g, '未发现明显异常')
    .replace(/检测到漏披露/g, '发现潜在漏披露')
    .replace(/\s+/g, ' ')
    .trim();
}

function getNegativeMessage(r: OutputResult | null | undefined) {
  if (!r) return '';
  const anyR = r as any;
  if (anyR?.negative_output?.is_negative === true) {
    return normalizeUserFacingPassText(anyR?.negative_output?.message || anyR?.summary?.negative_note || '未发现明显异常，请继续人工复核关键章节。');
  }
  if (anyR?.summary?.negative_sample === true) {
    return normalizeUserFacingPassText(anyR?.summary?.negative_note || '未发现明显异常，请继续人工复核关键章节。');
  }
  return '';
}

type RiskLevel = 'low' | 'medium' | 'high';

function summarizeConclusion(title: string, r: OutputResult | null | undefined) {
  if (!r) return null;
  const anyR = r as any;
  const summaryStatus = String(anyR?.summary?.status || '').toLowerCase();
  const alertsCount = Array.isArray(anyR?.alerts) ? anyR.alerts.length : 0;
  const issuesCount = Array.isArray(anyR?.issues) ? anyR.issues.length : 0;
  const total = alertsCount + issuesCount;

  let level: RiskLevel = 'low';
  let text = getNegativeMessage(r) || '未发现明显异常，请继续人工复核关键章节。';

  if (summaryStatus === 'fail' || total >= 3) {
    level = 'high';
    text = `发现${total}项潜在异常，建议优先复核。`;
  } else if (total > 0) {
    level = 'medium';
    text = `发现${total}项待人工确认内容。`;
  }

  return { key: title, title, level, text };
}

const conclusionRows = computed(() => {
  const rows = [
    summarizeConclusion('价格波动', moduleResults.value.price_fluctuation),
    summarizeConclusion('5%股东', moduleResults.value.shareholder_5pct),
    summarizeConclusion('质押冻结声明', moduleResults.value.pledge_freeze_decl),
  ].filter(Boolean) as Array<{ key: string; title: string; level: RiskLevel; text: string }>;
  return rows;
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

function applyThemeMode(mode: ThemeMode) {
  if (mode === 'auto') {
    try {
      darkMode.value = !!window.matchMedia?.('(prefers-color-scheme: dark)')?.matches;
    } catch {
      darkMode.value = false;
    }
    return;
  }
  darkMode.value = mode === 'dark';
}

function setThemeMode(mode: ThemeMode) {
  themeMode.value = mode;
  applyThemeMode(mode);
  try {
    localStorage.setItem('ui.themeMode', mode);
  } catch {}
}

const themeThumbTransform = computed(() => {
  if (themeMode.value === 'light') return 'translateX(34px)';
  if (themeMode.value === 'dark') return 'translateX(68px)';
  return 'translateX(0px)';
});

function toggleManageTasksMode() {
  manageTasksMode.value = !manageTasksMode.value;
  if (!manageTasksMode.value) selectedTaskIds.value = [];
}

function toggleMoreMenu() {
  if (showMoreMenu.value) {
    showMoreMenu.value = false;
    return;
  }
  const el = moreBtnRef.value;
  if (el) {
    const r = el.getBoundingClientRect();
    moreMenuPos.value = { top: r.bottom + 6, right: window.innerWidth - r.right };
  }
  showMoreMenu.value = true;
}

function loadSettingsDraft() {
  try {
    const raw = localStorage.getItem('app.settingsDraft');
    if (!raw) return;
    const parsed = JSON.parse(raw);
    settings.value = { ...settings.value, ...(parsed || {}) };
  } catch {}
  apiBaseOverride.value = settings.value.apiBaseUrl || '';
}

function validateSettings() {
  const s = settings.value;
  const miss: string[] = [];
  if (!String(s.llmBaseUrl || '').trim()) miss.push('ARK_BASE_URL');
  if (!String(s.llmApiKey || '').trim()) miss.push('ARK_API_KEY');
  if (!String(s.llmModel || '').trim()) miss.push('ARK_MODEL');

  const api = String(s.apiBaseUrl || '').trim();
  const llm = String(s.llmBaseUrl || '').trim();

  const badUrl: string[] = [];
  const isHttp = (u: string) => /^https?:\/\//i.test(u);
  if (api && !isHttp(api)) badUrl.push('前端 API Base URL');
  if (llm && !isHttp(llm)) badUrl.push('ARK_BASE_URL');

  if (miss.length || badUrl.length) {
    settingsValidationMsg.value = [
      miss.length ? `缺少必填：${miss.join('、')}` : '',
      badUrl.length ? `URL 格式错误：${badUrl.join('、')}` : '',
    ].filter(Boolean).join('；');
    return false;
  }

  settingsValidationMsg.value = '配置校验通过';
  return true;
}

function saveSettingsDraft() {
  if (!validateSettings()) {
    alert(settingsValidationMsg.value || '配置校验未通过');
    return;
  }
  localStorage.setItem('app.settingsDraft', JSON.stringify(settings.value));
  apiBaseOverride.value = settings.value.apiBaseUrl || '';
  alert('设置草稿已保存（仅本地浏览器）');
}

function buildEnvTemplate() {
  const s = settings.value;
  return [
    '# ===== Required =====',
    `ARK_BASE_URL=${s.llmBaseUrl || '<fill-me>'}`,
    `ARK_API_KEY=${s.llmApiKey || '<fill-me>'}`,
    `ARK_MODEL=${s.llmModel || '<fill-me>'}`,
    '',
    '# ===== Optional =====',
    `FLASK_PORT=${s.flaskPort || '9010'}`,
    `MODULES_PARALLEL=${s.modulesParallel || 'true'}`,
    `REDIS_URL=${s.redisUrl || ''}`,
    `REDIS_QUEUE_KEY=${s.redisQueueKey || 'prospectus:task_queue'}`,
  ].join('\n');
}

async function copyEnvTemplate() {
  const text = buildEnvTemplate();
  try {
    await navigator.clipboard.writeText(text);
    alert('已复制 .env 模板，可直接粘贴到 backend/.env');
  } catch {
    alert('复制失败，请手动复制弹窗内容');
  }
}

async function testSettingsConnection() {
  settingsConnMsg.value = '连接测试中...';
  const apiBase = (settings.value.apiBaseUrl || DEFAULT_API).trim();
  if (!/^https?:\/\//i.test(apiBase)) {
    settingsConnMsg.value = '连接失败：API Base URL 格式错误';
    return;
  }
  try {
    const [h, r] = await Promise.all([
      fetch(`${apiBase}/health`),
      fetch(`${apiBase}/runtime`),
    ]);
    if (!h.ok || !r.ok) {
      settingsConnMsg.value = `连接失败：health=${h.status}, runtime=${r.status}`;
      return;
    }
    const hd = await h.json();
    settingsConnMsg.value = `连接成功：后端可达，llm_configured=${String(hd?.llm_configured)}`;
  } catch (e: any) {
    settingsConnMsg.value = `连接失败：${e?.message || '网络异常'}`;
  }
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
  const r = await fetch(`${API.value}/tasks`);
  const d = await r.json();
  tasks.value = d.items ?? [];
  const idSet = new Set(tasks.value.map((t) => t.id));
  selectedTaskIds.value = selectedTaskIds.value.filter((id) => idSet.has(id));
}

async function refreshLists() {
  await Promise.all([fetchTasks(), fetchResults()]);
}

function isTaskFinished(status: string) {
  return ['success', 'failed', 'cancelled'].includes(String(status || ''));
}

const selectedDeletableCount = computed(() => {
  const map = new Map(tasks.value.map((t) => [t.id, t] as const));
  return selectedTaskIds.value.filter((id) => {
    const t = map.get(id);
    return !!t && isTaskFinished(t.status);
  }).length;
});

function isTaskSelected(taskId: string) {
  return selectedTaskIds.value.includes(taskId);
}

function toggleTaskSelection(taskId: string, checked: boolean) {
  const s = new Set(selectedTaskIds.value);
  if (checked) s.add(taskId);
  else s.delete(taskId);
  selectedTaskIds.value = Array.from(s);
}

function onQueueItemClick(task: TaskItem) {
  if (!manageTasksMode.value) return;
  if (!isTaskFinished(task.status)) return;
  toggleTaskSelection(task.id, !isTaskSelected(task.id));
}

function toggleSelectAllFinishedTasks(checked: boolean) {
  if (!checked) {
    selectedTaskIds.value = [];
    return;
  }
  selectedTaskIds.value = tasks.value.filter((t) => isTaskFinished(t.status)).map((t) => t.id);
}

async function deleteSelectedTasks() {
  const ids = selectedTaskIds.value.slice();
  if (!ids.length) {
    alert('请先选择历史任务');
    return;
  }
  const ok = confirm(`确认删除已选任务记录吗？共 ${ids.length} 条（仅删除任务列表记录，不删除结果文件）`);
  if (!ok) return;

  const resp = await fetch(`${API.value}/tasks/batch-delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_ids: ids }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    alert(data?.error || '批量删除失败');
    return;
  }
  selectedTaskIds.value = [];
  await fetchTasks();
}

function toggleResultSelection(path: string, checked: boolean) {
  const s = new Set(selectedResultPaths.value);
  if (checked) s.add(path);
  else s.delete(path);
  selectedResultPaths.value = Array.from(s);
}

function toggleSelectAllResults(checked: boolean) {
  if (!checked) {
    selectedResultPaths.value = [];
    return;
  }
  selectedResultPaths.value = results.value.map((x) => x.result_path);
}

async function deleteSelectedResults() {
  const paths = selectedResultPaths.value.slice();
  if (!paths.length) {
    alert('请先选择历史分析记录');
    return;
  }
  const c1 = confirm(`将删除 ${paths.length} 条历史分析记录及其对应结果文件，是否继续？`);
  if (!c1) return;
  const c2 = confirm('二次确认：该操作不可恢复，确认删除吗？');
  if (!c2) return;

  const resp = await fetch(`${API.value}/results/batch-delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ result_paths: paths }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    alert(data?.error || '删除历史分析记录失败');
    return;
  }

  selectedResultPaths.value = [];
  selectedResult.value = '';
  currentResult.value = null;
  manageResultsMode.value = false;
  await refreshLists();
}

function resolveTaskResultPath(task: TaskItem) {
  if (task.result_path) return task.result_path;
  const mods = task.modules || {};
  for (const m of Object.values(mods)) {
    if (m?.result_path) return m.result_path;
  }
  return '';
}

async function loadResultByTask(task: TaskItem) {
  moduleResults.value = { price_fluctuation: null, shareholder_5pct: null, pledge_freeze_decl: null };

  const mods = task.modules || {};
  const pPrice = mods.price_fluctuation?.result_path;
  const pS5 = mods.shareholder_5pct?.result_path;
  const pPledge = mods.pledge_freeze_decl?.result_path;

  if (pPrice) moduleResults.value.price_fluctuation = await tryLoadResult(toPosixPath(pPrice));
  if (pS5) moduleResults.value.shareholder_5pct = await tryLoadResult(toPosixPath(pS5));
  if (pPledge) moduleResults.value.pledge_freeze_decl = await tryLoadResult(toPosixPath(pPledge));

  // 若 task.modules 里没有完整路径，再走历史兼容逻辑
  if (!pPrice && !pS5 && !pPledge) {
    const p = resolveTaskResultPath(task);
    if (p) await loadResultByPath(toPosixPath(p));
  } else {
    currentResult.value = moduleResults.value.price_fluctuation || moduleResults.value.shareholder_5pct || moduleResults.value.pledge_freeze_decl;
    activeAlertIndex.value = -1;
    currentSearchText.value = '';
    locatedSide.value = '';
    syncPdfByResultPath(resolveTaskResultPath(task));
  }
}

function toPosixPath(p: string) {
  return String(p || '').replace(/\\/g, '/');
}

async function fetchResults() {
  const r = await fetch(`${API.value}/results`);
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
  const set = new Set(results.value.map((x) => x.result_path));
  selectedResultPaths.value = selectedResultPaths.value.filter((x) => set.has(x));
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
  serverPdfUrl.value = `${API.value}/file?path=${encodeURIComponent(task.pdf_path)}`;
  page.value = 1;
  pdfNonce.value += 1;
}
async function tryLoadResult(path: string) {
  const r = await fetch(`${API.value}/result?path=${encodeURIComponent(path)}`);
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
    await fetch(`${API.value}/runtime`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ modules_parallel: parallel }),
    });
  } catch {}
}

async function cancelTask(taskId: string) {
  try {
    await fetch(`${API.value}/tasks/${taskId}/cancel`, { method: 'POST' });
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

  try {
    const fd = new FormData();
    fd.append('file', selectedFile.value);
    fd.append('modules', JSON.stringify(selectedModules.value));
    fd.append('modules_parallel', String(modulesParallel.value));
    const resp = await fetch(`${API.value}/tasks`, { method: 'POST', body: fd });
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`创建任务失败：HTTP ${resp.status} ${txt.slice(0, 200)}`);
    }
    const task: TaskItem = await resp.json();
    currentTaskId.value = task.id;
    if (task.pdf_path) serverPdfUrl.value = `${API.value}/file?path=${encodeURIComponent(task.pdf_path)}`;

    await fetchTasks();
    const immediateResultPath = resolveTaskResultPath(task);
    if (task.status === 'success' && immediateResultPath) {
      await loadResultByTask(task);
      await fetchResults();
      setProgress(100, '命中缓存结果，分析完成');
      running.value = false;
      return;
    }
    startPolling();
  } catch (e: any) {
    running.value = false;
    setProgress(0, '任务创建失败');
    alert(e?.message || '任务创建失败，请检查后端日志');
  }
}

function startPolling() {
  if (pollTimer.value) clearInterval(pollTimer.value);
  pollTimer.value = window.setInterval(async () => {
    if (!currentTaskId.value) return;
    try {
      const r = await fetch(`${API.value}/tasks/${currentTaskId.value}`);
      if (!r.ok) throw new Error(`轮询失败：HTTP ${r.status}`);
      const t: TaskItem = await r.json();
      await fetchTasks();

      if (t.status === 'queued') setProgress(20, '排队中...');
      if (t.status === 'running') setProgress(70, '分析中（抽取/清洗/判定）...');

      if (t.status === 'success') {
        if (pollTimer.value) clearInterval(pollTimer.value);
        pollTimer.value = null;
        const p = resolveTaskResultPath(t);
        if (p) {
          await loadResultByTask(t);
          await fetchResults();
          selectedResult.value = toPosixPath(p);
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
    } catch (e: any) {
      if (pollTimer.value) clearInterval(pollTimer.value);
      pollTimer.value = null;
      running.value = false;
      setProgress(0, '轮询失败');
      alert(e?.message || '任务状态轮询失败');
    }
  }, 2000);
}

onMounted(async () => {
  try {
    const saved = (localStorage.getItem('ui.themeMode') || 'auto') as ThemeMode;
    themeMode.value = ['auto', 'light', 'dark'].includes(saved) ? saved : 'auto';
  } catch {
    themeMode.value = 'auto';
  }
  applyThemeMode(themeMode.value);
  loadSettingsDraft();

  await refreshLists();
  listRefreshTimer.value = window.setInterval(() => {
    refreshLists().catch(() => {});
  }, 5000);
  try {
    const r = await fetch(`${API.value}/runtime`);
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
        <div class="topbar-sub">LangChain 多模块审核 · 风险分级结论 · 结果可追溯</div>
        <div class="topbar-meta">
          <span class="meta-pill">模块已选 {{ selectedModules.length }}</span>
          <span class="meta-pill">历史记录 {{ results.length }}</span>
          <span class="meta-pill">任务 {{ tasks.length }}</span>
        </div>
      </div>

      <div class="actions">
        <!-- hidden input, triggered by buttons -->
        <input ref="fileInputRef" class="file-input" type="file" accept="application/pdf" @change="onPickFile" />

        <div class="actions-row compact toolbar-row">
          <div class="toolbar-group toolbar-group-run">
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

          <div class="toolbar-group toolbar-group-right">
            <div class="theme-switch" role="group" aria-label="主题模式">
              <div class="theme-thumb" :style="{ transform: themeThumbTransform }"></div>
              <button class="theme-opt" :class="{ active: themeMode === 'auto' }" @click="setThemeMode('auto')" title="跟随系统">💻</button>
              <button class="theme-opt" :class="{ active: themeMode === 'light' }" @click="setThemeMode('light')" title="日间">☀️</button>
              <button class="theme-opt" :class="{ active: themeMode === 'dark' }" @click="setThemeMode('dark')" title="夜间">🌙</button>
            </div>

            <div class="more-menu-wrap">
              <button ref="moreBtnRef" class="btn secondary" @click="toggleMoreMenu">更多 ▾</button>
            </div>
          </div>
        </div>

        <div class="actions-row modules-row">
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

        <div v-if="!pdfBaseUrl" class="pdf-empty" :class="{ 'is-drag-over': dragOver }">
          <div class="pdf-empty-inner">
            <div class="pdf-empty-title">暂无招股书</div>
            <div class="muted" style="margin-top:6px;">支持拖拽 PDF 到此区域，或点击选择文件</div>
            <div class="empty-actions">
              <button class="btn upload-btn" @click="triggerFilePicker">选择 PDF 文件</button>
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
          <div v-if="manageTasksMode" style="margin:6px 0 10px;display:flex;align-items:center;gap:8px;">
            <label :style="{ display:'flex', alignItems:'center', gap:'6px', fontSize:'13px', color: darkMode ? '#e2e8f0' : '' }">
              <input
                type="checkbox"
                style="width:16px;height:16px;accent-color:#2563eb;cursor:pointer;"
                :checked="selectedTaskIds.length > 0 && selectedTaskIds.length === tasks.filter(t => isTaskFinished(t.status)).length"
                @change="toggleSelectAllFinishedTasks(($event.target as HTMLInputElement).checked)"
              />
              全选已完成任务
            </label>
          </div>
          <div class="queue-scroll">
            <div v-if="!tasks.length" class="muted">暂无任务</div>
            <div
              v-for="q in tasks"
              :key="q.id"
              class="queue-item"
              :style="{
                display: 'block',
                cursor: manageTasksMode && isTaskFinished(q.status) ? 'pointer' : 'default',
                background: manageTasksMode && isTaskSelected(q.id)
                  ? (darkMode ? 'rgba(30,58,138,.35)' : '#eff6ff')
                  : '',
                boxShadow: manageTasksMode && isTaskSelected(q.id)
                  ? (darkMode ? 'inset 0 0 0 2px #60a5fa' : 'inset 0 0 0 2px #60a5fa')
                  : 'none',
                borderRadius: manageTasksMode ? '10px' : '0',
                marginBottom: manageTasksMode ? '8px' : '0',
                padding: manageTasksMode ? '8px 10px' : '8px 0',
                overflow: manageTasksMode ? 'hidden' : 'visible',
                borderBottom: manageTasksMode ? 'none' : '1px dashed var(--line)'
              }"
              @click="onQueueItemClick(q)"
            >
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                <div style="display:flex;align-items:center;gap:8px;min-width:0;">
                  <input
                    v-if="manageTasksMode"
                    type="checkbox"
                    :disabled="!isTaskFinished(q.status)"
                    :checked="isTaskSelected(q.id)"
                    style="width:16px;height:16px;accent-color:#2563eb;cursor:pointer;"
                    @click.stop
                    @change="toggleTaskSelection(q.id, ($event.target as HTMLInputElement).checked)"
                  />
                  <span class="queue-file" :title="q.filename">{{ q.filename }}</span>
                </div>
                <div style="display:flex;align-items:center;gap:8px;">
                  <span class="pill">{{ q.status }}</span>
                  <button
                    v-if="q.status === 'queued' || q.status === 'running'"
                    class="btn secondary"
                    style="padding:4px 8px;font-size:12px;"
                    @click.stop="cancelTask(q.id)"
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
          <div v-if="conclusionRows.length" class="card" style="margin-bottom:10px;">
            <div style="display:flex;flex-direction:column;gap:8px;">
              <div
                v-for="x in conclusionRows"
                :key="x.key"
                :style="{
                  fontWeight: 600,
                  color: x.level === 'high' ? '#991b1b' : x.level === 'medium' ? '#92400e' : '#166534',
                  background: x.level === 'high' ? '#fef2f2' : x.level === 'medium' ? '#fffbeb' : '#f0fdf4',
                  border: '1px solid ' + (x.level === 'high' ? '#fecaca' : x.level === 'medium' ? '#fde68a' : '#86efac'),
                  borderRadius: '10px',
                  padding: '8px 10px'
                }"
              >
                {{ x.level === 'high' ? '🔴' : x.level === 'medium' ? '🟡' : '🟢' }} {{ x.title }}审核结论：{{ x.text }}
              </div>
            </div>
          </div>

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

    <div
      v-if="showMoreMenu"
      @click.self="showMoreMenu = false"
      style="position:fixed;inset:0;z-index:9998;"
    >
      <div
        class="card more-menu"
        :style="{
          position:'fixed',
          top: `${moreMenuPos.top}px`,
          right: `${moreMenuPos.right}px`,
          minWidth:'190px',
          padding:'6px',
          borderRadius:'12px',
          zIndex:'9999',
          display:'flex',
          flexDirection:'column',
          gap:'4px',
          background: darkMode ? 'rgba(15,23,42,.98)' : '#fff',
          border: darkMode ? '1px solid #334155' : '1px solid #dbe4f0'
        }"
      >
        <button class="more-item" @click="showSettingsModal = true; showMoreMenu = false">设置文件</button>
        <button class="more-item" @click="manageResultsMode = true; showMoreMenu = false">管理历史记录</button>
        <button class="more-item" @click="toggleManageTasksMode(); showMoreMenu = false">
          {{ manageTasksMode ? '退出任务管理' : '管理任务列表' }}
        </button>
        <button class="more-item" @click="refreshLists(); showMoreMenu = false">刷新历史</button>
        <button
          v-if="manageTasksMode"
          class="more-item"
          :disabled="selectedDeletableCount === 0"
          @click="deleteSelectedTasks(); showMoreMenu = false"
        >
          批量删除已选（{{ selectedDeletableCount }}）
        </button>
      </div>
    </div>

    <div
      v-if="showSettingsModal"
      @click.self="showSettingsModal = false"
      :style="{
        position:'fixed', inset:'0',
        background: darkMode
          ? 'linear-gradient(180deg, rgba(2,6,23,.76), rgba(2,6,23,.66))'
          : 'linear-gradient(180deg, rgba(15,23,42,.55), rgba(15,23,42,.45))',
        backdropFilter:'blur(2px)', display:'flex', alignItems:'center', justifyContent:'center', zIndex:'1200', padding:'16px'
      }"
    >
      <div class="card" :style="{ width:'min(820px,96vw)', maxHeight:'84vh', overflow:'auto', padding:'14px 16px', color: darkMode ? '#e5e7eb' : '' }">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
          <h3 style="font-size:18px;">设置文件（.env）</h3>
          <button class="modal-close-btn" @click="showSettingsModal = false">✕</button>
        </div>
        <div class="muted" :style="{ marginBottom:'10px', color: darkMode ? '#cbd5e1' : '' }">必填项用于模型接入，可选项用于队列与运行配置。你可以先保存草稿，再复制到 backend/.env。</div>

        <div class="settings-grid">
          <label>前端 API Base URL（可选）<input v-model="settings.apiBaseUrl" placeholder="http://localhost:9010/api/v1" /></label>
          <label>ARK_BASE_URL（必填）<input v-model="settings.llmBaseUrl" placeholder="https://.../api/v1 或 /api/v3" /></label>
          <label>ARK_API_KEY（必填）<input v-model="settings.llmApiKey" placeholder="sk-..." /></label>
          <label>ARK_MODEL（必填）<input v-model="settings.llmModel" placeholder="model-id" /></label>
          <label>REDIS_URL（可选）<input v-model="settings.redisUrl" placeholder="redis://127.0.0.1:6379/0" /></label>
          <label>REDIS_QUEUE_KEY（可选）<input v-model="settings.redisQueueKey" placeholder="prospectus:task_queue" /></label>
          <label>FLASK_PORT（可选）<input v-model="settings.flaskPort" placeholder="9010" /></label>
          <label>MODULES_PARALLEL（可选）<input v-model="settings.modulesParallel" placeholder="true / false" /></label>
        </div>

        <div style="margin-top:10px;display:flex;flex-direction:column;gap:6px;">
          <div v-if="settingsValidationMsg" class="muted" :style="{ color: settingsValidationMsg.includes('通过') ? (darkMode ? '#86efac' : '#166534') : (darkMode ? '#fca5a5' : '#991b1b') }">{{ settingsValidationMsg }}</div>
          <div v-if="settingsConnMsg" class="muted" :style="{ color: settingsConnMsg.includes('成功') ? (darkMode ? '#86efac' : '#166534') : (darkMode ? '#fca5a5' : '#991b1b') }">{{ settingsConnMsg }}</div>
        </div>

        <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;">
          <button class="btn secondary" @click="validateSettings">校验配置</button>
          <button class="btn secondary" @click="testSettingsConnection">测试连接</button>
          <button class="btn secondary" @click="saveSettingsDraft">保存草稿</button>
          <button class="btn" @click="copyEnvTemplate">复制 .env 模板</button>
        </div>
      </div>
    </div>

    <div
      v-if="manageResultsMode"
      @click.self="manageResultsMode = false"
      :style="{
        position:'fixed', inset:'0',
        background: darkMode
          ? 'linear-gradient(180deg, rgba(2,6,23,.76), rgba(2,6,23,.66))'
          : 'linear-gradient(180deg, rgba(15,23,42,.55), rgba(15,23,42,.45))',
        backdropFilter:'blur(2px)', display:'flex', alignItems:'center', justifyContent:'center', zIndex:'1200', padding:'16px'
      }"
    >
      <div class="card" :style="{ width:'min(780px,96vw)', maxHeight:'84vh', display:'flex', flexDirection:'column', borderRadius:'16px', overflow:'hidden', background: darkMode ? 'rgba(15,23,42,.96)' : '', color: darkMode ? '#e5e7eb' : '' }">
        <div :style="{ display:'flex', alignItems:'center', justifyContent:'space-between', gap:'8px', padding:'14px 16px', background: darkMode ? 'linear-gradient(180deg,#1e293b,#0f172a)' : 'linear-gradient(180deg,#fbfdff,#f2f7ff)', borderBottom: darkMode ? '1px solid #334155' : '1px solid #dbe4f0' }">
          <div>
            <h3 style="font-size:18px;">管理历史分析记录</h3>
            <div class="muted" :style="{ marginTop:'2px', color: darkMode ? '#cbd5e1' : '' }">可多选删除，删除后不可恢复</div>
          </div>
          <button class="modal-close-btn" @click="manageResultsMode = false" aria-label="关闭">✕</button>
        </div>

        <div :style="{ padding:'12px 16px 8px', display:'flex', alignItems:'center', justifyContent:'space-between', gap:'10px', borderBottom: darkMode ? '1px solid #1f2937' : 'none' }">
          <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;">
            <input
              type="checkbox"
              style="width:16px;height:16px;accent-color:#2563eb;cursor:pointer;"
              :checked="results.length > 0 && selectedResultPaths.length === results.length"
              @change="toggleSelectAllResults(($event.target as HTMLInputElement).checked)"
            />
            全选（{{ results.length }}）
          </label>
          <span class="muted" :style="{ color: darkMode ? '#cbd5e1' : '' }">已选 {{ selectedResultPaths.length }} 条</span>
        </div>

        <div style="overflow:auto;display:flex;flex-direction:column;gap:8px;max-height:50vh;padding:6px 16px 12px;">
          <div
            v-for="x in results"
            :key="`manage-${x.result_path}`"
            @click="toggleResultSelection(x.result_path, !selectedResultPaths.includes(x.result_path))"
            :style="{
              display:'flex',
              alignItems:'center',
              gap:'10px',
              cursor:'pointer',
              padding:'10px 12px',
              borderRadius:'12px',
              transition:'all .16s ease',
              background:selectedResultPaths.includes(x.result_path)
                ? (darkMode ? 'rgba(30,58,138,.35)' : '#eff6ff')
                : (darkMode ? '#0f172a' : '#ffffff'),
              boxShadow:selectedResultPaths.includes(x.result_path)
                ? (darkMode
                    ? 'inset 0 0 0 2px #60a5fa, 0 2px 10px rgba(2,6,23,.35)'
                    : 'inset 0 0 0 2px #60a5fa, 0 2px 10px rgba(37,99,235,.10)')
                : (darkMode ? 'inset 0 0 0 1px #334155' : 'inset 0 0 0 1px #e2e8f0')
            }"
          >
            <input
              type="checkbox"
              style="width:16px;height:16px;accent-color:#2563eb;cursor:pointer;"
              :checked="selectedResultPaths.includes(x.result_path)"
              @click.stop
              @change="toggleResultSelection(x.result_path, ($event.target as HTMLInputElement).checked)"
            />
            <div style="min-width:0;display:flex;flex-direction:column;gap:3px;">
              <span :style="{ fontSize:'13px', fontWeight:600, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis', color: darkMode ? '#e2e8f0' : '#0f172a' }">{{ x.name }}</span>
              <span class="muted" :title="x.result_path" :style="{ fontSize:'11px', whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis', color: darkMode ? '#94a3b8' : '' }">{{ x.result_path }}</span>
            </div>
          </div>
          <div v-if="!results.length" class="muted" :style="{ padding:'12px', border: darkMode ? '1px dashed #334155' : '1px dashed #dbe4f0', borderRadius:'10px', textAlign:'center', background: darkMode ? '#0f172a' : '', color: darkMode ? '#cbd5e1' : '' }">暂无历史分析记录</div>
        </div>

        <div :style="{ display:'flex', justifyContent:'flex-end', gap:'8px', padding:'12px 16px 14px', borderTop: darkMode ? '1px solid #334155' : '1px solid #e5e7eb', background: darkMode ? '#0b1220' : '#fafcff' }">
          <button class="btn secondary" @click="selectedResultPaths = []">清空选择</button>
          <button class="btn" :disabled="!selectedResultPaths.length" @click="deleteSelectedResults">
            删除已选记录（{{ selectedResultPaths.length }}）
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
