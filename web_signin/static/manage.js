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
const logLabels = { success: "签到成功", error: "执行出错", skipped: "已跳过" };
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

function describePeriod(period, state) {
  if (!state) return `${period.name}：今日暂无计划`;
  if (state.today_succeeded) return `${period.name}：已签到，跳过时段内剩余尝试`;
  if (state.today_fallback_done) return `${period.name}：兜底签到结束，尚未签到`;
  return `${period.name}：普通连续失败 ${state.today_regular_failures || 0} 次，兜底 ${period.fallback_time}`;
}

function renderTasks() {
  byId("enabledCount").textContent = tasksCache.filter(task => task.enabled).length;
  byId("runningCount").textContent = tasksCache.filter(task => task.runtime_status === "running").length;
  byId("scheduledCount").textContent = tasksCache.filter(task => task.runtime_status === "scheduled").length;
  const name = byId("filterName").value.trim().toLowerCase();
  const classId = byId("filterClass").value.trim().toLowerCase();
  const enabled = byId("filterEnabled").value;
  const runtime = byId("filterRuntime").value;
  const visible = tasksCache.filter(task =>
    (!name || task.name.toLowerCase().includes(name)) &&
    (!classId || task.class_id.toLowerCase().includes(classId)) &&
    (!enabled || task.enabled === (enabled === "1")) &&
    (!runtime || task.runtime_status === runtime)
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
    addCell(row, task.lat + " " + task.lng, "坐标");
    const periods = task.periods || [];
    const times = addCell(row, periods.length ? `${periods.length} 个时段 · 随机 ±${task.random_minutes} 分钟` : task.times.join(", "), "签到计划");
    if (periods.length) {
      for (const period of periods) {
        addDetail(times, `${period.name}：${period.times.join("、")} · 兜底 ${period.fallback_time}`);
        const plan = (task.today_plan || []).filter(entry => entry.period === period.name);
        if (plan.length) addDetail(times, "今日计划：" + plan.map(entry => entry.run_at.slice(11, 19)).join("、"));
      }
    } else if (task.random_enabled) {
      addDetail(times, `随机 ±${task.random_minutes} 分钟 · 兜底 ${task.fallback_time}`);
      addDetail(times, "今日计划：" + (task.today_plan || []).map(entry => entry.run_at.slice(11, 19)).join(", "));
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
    if (periods.length) {
      for (const period of periods) {
        addDetail(result, describePeriod(period, (task.today_periods || []).find(state => state.name === period.name)));
      }
    } else if (task.random_enabled) {
      addDetail(result, task.today_succeeded ? "今日已成功，跳过其余尝试" : `普通连续失败 ${task.today_regular_failures || 0} 次`);
    }
    const actions = addCell(row, "", "操作");
    const button = document.createElement("button");
    button.className = "btn btn-ghost";
    button.textContent = "详情 / 日志";
    button.addEventListener("click", () => selectTask(task.id));
    actions.appendChild(button);
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
      addDetail(byId("currentDayState"), describePeriod(period, (task.today_periods || []).find(state => state.name === period.name)));
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
  periodEditor.set([]);
  updateScheduleMode();
  byId("currentDayState").textContent = "";
  logList.textContent = "请选择左侧任务查看日志。";
}

function selectTask(id) {
  const task = tasksCache.find(item => item.id === id);
  if (!task) return;
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
  periodEditor.set(task.periods);
  updateScheduleMode();
  updateDayState();
  renderTasks();
  loadLogsForCurrent().catch(error => { if (signedIn) logList.textContent = error.message; });
  if (window.matchMedia("(max-width: 640px)").matches) {
    byId("taskEditor").scrollIntoView({ behavior: "smooth", block: "start" });
  }
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
["filterEnabled", "filterRuntime"].forEach(id => byId(id).addEventListener("change", renderTasks));

async function initialize() {
  checkHealth();
  try {
    const admin = await window.siteAuthReady;
    showDashboard(admin.username);
  } catch (_) { showLogin(); }
}
initialize();
