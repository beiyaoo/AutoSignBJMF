const byId = id => document.getElementById(id);
const adminPanel = byId("adminPanel");
const taskTableBody = byId("taskTableBody");
const logList = byId("logList");
const periodEditor = createPeriodEditor({
  mode: byId("editPeriodMode"),
  panel: byId("editPeriodPanel"),
  legacy: byId("editLegacySchedule"),
  list: byId("editPeriodList"),
  addButton: byId("btnEditAddPeriod")
});
const runtimeLabels = {
  running: "正在执行", scheduled: "等待调度", pending: "尚未生效",
  expired: "已到期", disabled: "已禁用", unscheduled: "未注册调度"
};
const periodStatusLabels = {
  success: "已签到", running: "执行中", scheduled: "待执行",
  failed: "兜底失败", missed: "已错过", unscheduled: "未调度"
};
const logLabels = { success: "签到成功", error: "执行出错", skipped: "已跳过" };
const beijingClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hourCycle: "h23", hour: "2-digit", minute: "2-digit", second: "2-digit"
});
let signedIn = false;
let sessionVersion = 0;
let refreshTimer = null;
let refreshing = false;
let currentTaskId = null;
let tasksCache = [];

function formatTime(value, utc = false) {
  if (!value) return "—";
  const normalized = utc && !/(Z|[+-]\d{2}:\d{2})$/.test(value) ? value + "Z" : value;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", hour12: false
  });
}

function showLogin() {
  clearInterval(refreshTimer);
  refreshTimer = null;
  signedIn = false;
  sessionVersion += 1;
  tasksCache = [];
  clearSelection();
  taskTableBody.replaceChildren();
  adminPanel.hidden = true;
  location.assign("/login?next=/admin");
}

function showDashboard(username) {
  signedIn = true;
  adminPanel.hidden = false;
  byId("adminIdentity").textContent = "管理员：" + username;
  byId("adminIdentity").hidden = false;
  byId("btnLogout").hidden = false;
  clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshDashboard, 10000);
  refreshDashboard();
}

async function apiRequest(path, options = {}) {
  const version = sessionVersion;
  if (!signedIn) throw new Error("请先登录");
  const response = await fetch(path, {
    ...options, cache: "no-store",
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers }
  });
  const data = await response.json();
  if (!signedIn || version !== sessionVersion) throw new Error("登录状态已改变");
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(data.error || "请求失败");
  }
  return data;
}

async function checkHealth() {
  let online = false;
  try {
    const response = await fetch("/health", { cache: "no-store" });
    online = response.ok && (await response.json()).status === "ok";
  } catch (_) {}
  byId("healthDot").className = "status-dot " + (online ? "ok" : "bad");
  byId("healthText").textContent = online ? "后端状态：在线" : "后端状态：不可用";
}

function tableMessage(message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 7;
  cell.textContent = message;
  row.appendChild(cell);
  taskTableBody.replaceChildren(row);
}

function addCell(row, value, label) {
  const cell = document.createElement("td");
  cell.dataset.label = label;
  cell.textContent = value;
  row.appendChild(cell);
  return cell;
}

function addDetail(cell, value) {
  const detail = document.createElement("div");
  detail.className = "muted";
  detail.textContent = value;
  cell.appendChild(detail);
}

function scheduleMode(task) {
  if (task.periods?.length) return "periods";
  return task.random_enabled ? "daily" : "fixed";
}

function periodStatus(state, plan) {
  if (Object.hasOwn(periodStatusLabels, state?.status)) return state.status;
  if (state?.today_succeeded) return "success";
  if (state?.today_fallback_done) return "failed";
  return plan.length ? "scheduled" : "unscheduled";
}

function clockTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : beijingClock.format(date);
}

function renderPeriodCards(cell, task) {
  const list = document.createElement("ul");
  list.className = "task-periods";
  for (const period of task.periods) {
    const state = (task.today_periods || []).find(item => item.name === period.name);
    const plan = (task.today_plan || []).filter(entry => entry.period === period.name);
    const status = periodStatus(state, plan);
    const item = document.createElement("li");
    item.className = "task-period";
    const heading = document.createElement("div");
    heading.className = "task-period-heading";
    const name = document.createElement("strong");
    name.textContent = period.name;
    const badge = document.createElement("span");
    badge.className = `period-status period-status-${status}`;
    badge.textContent = periodStatusLabels[status];
    heading.append(name, badge);
    item.appendChild(heading);
    const location = period.location;
    addDetail(item, location ? `地点：${location.name}（${location.lat}, ${location.lng}）` : "地点：沿用任务默认地点");
    addDetail(item, `普通 ${period.times.join("、")} · 兜底 ${period.fallback_time}`);
    const plannedTimes = plan.filter(entry => entry.kind !== "fallback").map(entry => clockTime(entry.run_at)).filter(Boolean);
    if (plannedTimes.length) addDetail(item, `今日普通 ${plannedTimes.join("、")}`);
    const next = clockTime(state?.next_run_at);
    if (next) addDetail(item, `下次 ${next}`);
    if (state?.today_regular_failures) addDetail(item, `普通签到连续失败 ${state.today_regular_failures} 次`);
    list.appendChild(item);
  }
  cell.appendChild(list);
}

function draftPeriods(task) {
  const times = [...new Set(task.times)].sort();
  const periods = Array.from({ length: Math.max(2, times.length) }, (_, index) => ({
    name: `时段 ${index + 1}`,
    times: times[index] ? [times[index]] : [],
    fallback_time: ""
  }));
  if (times.length >= 2 && task.fallback_time > times[times.length - 1]) {
    periods[periods.length - 1].fallback_time = task.fallback_time;
  }
  return periods;
}

function renderTasks() {
  byId("enabledCount").textContent = tasksCache.filter(task => task.enabled).length;
  byId("runningCount").textContent = tasksCache.filter(task => task.runtime_status === "running").length;
  byId("scheduledCount").textContent = tasksCache.filter(task => task.runtime_status === "scheduled").length;
  const name = byId("filterName").value.trim().toLowerCase();
  const classId = byId("filterClass").value.trim().toLowerCase();
  const enabled = byId("filterEnabled").value;
  const runtime = byId("filterRuntime").value;
  const modeFilter = byId("filterMode").value;
  const visible = tasksCache.filter(task =>
    (!name || task.name.toLowerCase().includes(name)) &&
    (!classId || task.class_id.toLowerCase().includes(classId)) &&
    (!enabled || task.enabled === (enabled === "1")) &&
    (!runtime || task.runtime_status === runtime) &&
    (!modeFilter || scheduleMode(task) === modeFilter)
  );
  if (!visible.length) {
    tableMessage(tasksCache.length ? "没有符合筛选条件的任务。" : "暂无签到任务，请先在用户配置页扫码创建。");
    return;
  }
  taskTableBody.replaceChildren();
  visible.forEach(task => {
    const row = document.createElement("tr");
    row.dataset.id = task.id;
    if (task.id === currentTaskId) row.className = "selected";
    addCell(row, "#" + task.id, "任务编号");
    addCell(row, task.name + " / " + (task.class_id || "无班级信息"), "姓名 / 班级");
    addCell(row, task.lat + " " + task.lng, "默认坐标");
    const mode = scheduleMode(task);
    const times = addCell(row, "", "签到计划");
    times.className = "task-schedule-cell";
    const modeBadge = document.createElement("span");
    modeBadge.className = "schedule-mode" + (mode === "periods" ? " schedule-mode-periods" : "");
    modeBadge.textContent = mode === "periods" ? `多时段签到 · ${task.periods.length} 个时段` :
      mode === "daily" ? "每日完成一次" : "固定时间签到";
    times.appendChild(modeBadge);
    if (mode === "periods") {
      addDetail(times, `普通时间随机 ±${task.random_minutes} 分钟`);
      renderPeriodCards(times, task);
    } else {
      addDetail(times, task.times.join("、"));
      if (mode === "daily") {
        addDetail(times, `随机 ±${task.random_minutes} 分钟 · 兜底 ${task.fallback_time}`);
        addDetail(times, "今日计划：" + (task.today_plan || []).map(entry => clockTime(entry.run_at)).join("、"));
      }
    }
    addDetail(times, (task.date_start?.slice(0, 10) || "不限") + " 至 " + (task.date_end?.slice(0, 10) || "长期"));
    const state = addCell(row, "", "执行状态");
    const tag = document.createElement("span");
    tag.className = "tag tag-" + task.runtime_status;
    tag.textContent = runtimeLabels[task.runtime_status] || "未知";
    state.appendChild(tag);
    if (!task.enabled && task.runtime_status === "running") addDetail(state, "已禁用，等待本次执行结束");
    if (task.running_since) addDetail(state, "开始：" + formatTime(task.running_since));
    addDetail(state, "下次：" + formatTime(task.next_run_at));
    const result = addCell(row, task.latest_run ? logLabels[task.latest_run.status] || task.latest_run.status : "尚未执行", "最近结果");
    if (task.latest_run) addDetail(result, formatTime(task.latest_run.run_at));
    if (mode === "daily") {
      addDetail(result, task.today_succeeded ? "今日已成功，跳过其余尝试" : `普通连续失败 ${task.today_regular_failures || 0} 次`);
    }
    const actions = addCell(row, "", "操作");
    const actionsGroup = document.createElement("div");
    actionsGroup.className = "task-actions";
    const periodsButton = document.createElement("button");
    periodsButton.type = "button";
    periodsButton.className = "btn btn-periods";
    periodsButton.textContent = mode === "periods" ? "编辑时段" : "多时段签到";
    periodsButton.addEventListener("click", () => selectTask(task.id, true));
    const details = document.createElement("button");
    details.type = "button";
    details.className = "btn btn-ghost";
    details.textContent = "详情 / 日志";
    details.addEventListener("click", () => selectTask(task.id));
    actionsGroup.append(periodsButton, details);
    actions.appendChild(actionsGroup);
    taskTableBody.appendChild(row);
  });
}

async function loadTasks() {
  const tasks = await apiRequest("/api/admin/tasks");
  tasksCache = Array.isArray(tasks) ? tasks : [];
  if (currentTaskId && !tasksCache.some(task => task.id === currentTaskId)) clearSelection();
  renderTasks();
  updateDayState();
}

function updateDayState() {
  const task = tasksCache.find(item => item.id === currentTaskId);
  if (task?.periods?.length) {
    byId("currentDayState").replaceChildren();
    for (const period of task.periods) {
      const state = (task.today_periods || []).find(item => item.name === period.name);
      const plan = (task.today_plan || []).filter(entry => entry.period === period.name);
      addDetail(byId("currentDayState"), `${period.name}：${periodStatusLabels[periodStatus(state, plan)]}`);
    }
    return;
  }
  byId("currentDayState").textContent = task?.random_enabled
    ? (task.today_succeeded ? "今日已签到成功，跳过剩余普通尝试和兜底。" :
       `今日普通连续失败 ${task.today_regular_failures || 0} 次；${task.today_fallback_done ? "兜底已执行" : "等待最后兜底 " + task.fallback_time}。`)
    : "";
}

async function refreshDashboard() {
  if (!signedIn || refreshing) return;
  refreshing = true;
  try {
    await loadTasks();
    if (currentTaskId) await loadLogsForCurrent();
    byId("refreshStatus").textContent = "上次刷新：" + formatTime(new Date().toISOString()) + " · 每 10 秒自动刷新 · 北京时间";
  } catch (error) {
    if (signedIn) byId("refreshStatus").textContent = "刷新失败：" + error.message;
  } finally {
    refreshing = false;
  }
}

function clearSelection() {
  currentTaskId = null;
  byId("currentTaskLabel").textContent = "无";
  ["editCoord", "editWxKey", "editQqKey", "editTimes", "editDateStart", "editDateEnd", "editFallbackTime"].forEach(id => byId(id).value = "");
  byId("editRandomEnabled").checked = false;
  byId("editPeriodDraft").hidden = true;
  periodEditor.set([]);
  updateScheduleMode();
  byId("currentDayState").textContent = "";
  logList.textContent = "请选择左侧任务查看日志。";
}

function selectTask(id, editPeriods = false) {
  const task = tasksCache.find(item => item.id === id);
  if (!task) return;
  if (editPeriods && currentTaskId === id && byId("editPeriodMode").checked) {
    focusPeriodEditor();
    return;
  }
  const converting = editPeriods && !task.periods?.length;
  currentTaskId = id;
  byId("currentTaskLabel").textContent = `#${task.id} ${task.name} (${task.class_id || "无班级信息"})`;
  byId("editCoord").value = task.lat + " " + task.lng;
  byId("editWxKey").value = task.wx_key || "";
  byId("editQqKey").value = task.qq_key || "";
  byId("editTimes").value = task.times.join(",");
  byId("editEnabled").value = task.enabled ? "1" : "0";
  byId("editDateStart").value = task.date_start?.slice(0, 10) || "";
  byId("editDateEnd").value = task.date_end?.slice(0, 10) || "";
  byId("editRandomEnabled").checked = Boolean(task.random_enabled);
  byId("editRandomMinutes").value = task.random_minutes ?? 5;
  byId("editFallbackTime").value = task.fallback_time || "";
  periodEditor.set(converting ? draftPeriods(task) : task.periods);
  byId("editPeriodDraft").hidden = !converting;
  updateScheduleMode();
  updateDayState();
  renderTasks();
  loadLogsForCurrent().catch(error => { if (signedIn) logList.textContent = error.message; });
  if (editPeriods) {
    focusPeriodEditor();
  } else if (window.matchMedia("(max-width: 640px)").matches) {
    byId("taskEditor").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function focusPeriodEditor() {
  const panel = byId("editPeriodPanel");
  panel.focus({ preventScroll: true });
  panel.scrollIntoView({ block: "start" });
}

async function saveTask() {
  if (!currentTaskId) return alert("请先选择一个任务。");
  const taskId = currentTaskId;
  const coordinates = byId("editCoord").value.trim().replace(/[,，|、]+/g, " ").split(/\s+/);
  if (coordinates.length !== 2 || coordinates.some(value => !value || !Number.isFinite(Number(value)))) {
    return alert("请输入有效的纬度和经度。");
  }
  let periods;
  try {
    periods = periodEditor.read();
  } catch (error) {
    return alert(error.message);
  }
  const payload = {
    lat: coordinates[0], lng: coordinates[1],
    wx_key: byId("editWxKey").value.trim(), qq_key: byId("editQqKey").value.trim(),
    periods,
    times: periods.length ? [] : byId("editTimes").value.split(/[,，]/).map(value => value.trim()).filter(Boolean),
    enabled: byId("editEnabled").value === "1",
    random_enabled: periods.length > 0 || byId("editRandomEnabled").checked,
    random_minutes: Number(byId("editRandomMinutes").value),
    fallback_time: periods.length ? null : byId("editFallbackTime").value || null,
    date_start: byId("editDateStart").value || null, date_end: byId("editDateEnd").value || null
  };
  byId("btnSaveTask").disabled = true;
  try {
    await apiRequest("/api/admin/tasks/" + taskId, { method: "PATCH", body: JSON.stringify(payload) });
    await loadTasks();
    if (currentTaskId === taskId) selectTask(taskId);
    alert("保存成功，签到调度已更新。");
  } catch (error) {
    if (signedIn) alert("保存失败：" + error.message);
  } finally {
    byId("btnSaveTask").disabled = false;
  }
}

async function deleteTask() {
  if (!currentTaskId) return alert("请先选择一个任务。");
  const taskId = currentTaskId;
  if (!confirm("确定删除该任务及其所有日志吗？此操作不可恢复。")) return;
  try {
    await apiRequest("/api/admin/tasks/" + taskId, { method: "DELETE" });
    if (currentTaskId === taskId) clearSelection();
    await loadTasks();
  } catch (error) {
    if (signedIn) alert("删除失败：" + error.message);
  }
}

async function loadLogsForCurrent() {
  const taskId = currentTaskId;
  if (!taskId) return;
  const logs = await apiRequest("/api/admin/logs?task_id=" + taskId + "&limit=50");
  if (currentTaskId !== taskId) return;
  logList.replaceChildren();
  if (!logs.length) {
    logList.textContent = "暂无执行日志。";
    return;
  }
  logs.forEach(log => {
    const item = document.createElement("div");
    item.className = "log-item";
    const time = document.createElement("div");
    time.className = "log-time";
    time.textContent = formatTime(log.run_at, true);
    const status = document.createElement("div");
    status.className = "log-status" + (log.status === "error" ? " error" : "");
    status.textContent = logLabels[log.status] || log.status;
    const message = document.createElement("pre");
    message.className = "log-message";
    message.textContent = log.message || "";
    item.append(time, status, message);
    logList.appendChild(item);
  });
}

byId("btnLogout").addEventListener("click", async () => {
  byId("btnLogout").disabled = true;
  try { await apiRequest("/api/admin/logout", { method: "POST" }); }
  catch (_) {}
  finally {
    showLogin("已退出管理员登录。");
    byId("btnLogout").disabled = false;
  }
});
byId("btnReloadTasks").addEventListener("click", refreshDashboard);
function updateScheduleMode() {
  byId("editRandomOptions").hidden = !byId("editRandomEnabled").checked;
  byId("editRandomMinuteControl").hidden = !byId("editPeriodMode").checked && !byId("editRandomEnabled").checked;
}
byId("editRandomEnabled").addEventListener("change", updateScheduleMode);
byId("editPeriodMode").addEventListener("change", updateScheduleMode);
byId("btnSaveTask").addEventListener("click", saveTask);
byId("btnDeleteTask").addEventListener("click", deleteTask);
byId("btnReloadLogs").addEventListener("click", () => {
  loadLogsForCurrent().catch(error => { if (signedIn) logList.textContent = error.message; });
});
["filterName", "filterClass"].forEach(id => byId(id).addEventListener("input", renderTasks));
["filterEnabled", "filterRuntime", "filterMode"].forEach(id => byId(id).addEventListener("change", renderTasks));

async function initialize() {
  checkHealth();
  try {
    const admin = await window.siteAuthReady;
    showDashboard(admin.username);
  } catch (_) { showLogin(); }
}
initialize();
