const byId = id => document.getElementById(id);
const loginForm = byId("loginForm");
const loginError = byId("loginError");
const adminPanel = byId("adminPanel");
const taskTableBody = byId("taskTableBody");
const logList = byId("logList");
const runtimeLabels = {
  running: "正在执行", scheduled: "等待调度", pending: "尚未生效",
  expired: "已到期", disabled: "已禁用", unscheduled: "未注册调度"
};
const logLabels = { success: "签到成功", error: "执行出错", skipped: "已跳过" };
let adminToken = sessionStorage.getItem("bjmf_admin_token") || "";
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

function showLogin(message = "") {
  clearInterval(refreshTimer);
  refreshTimer = null;
  adminToken = "";
  sessionStorage.removeItem("bjmf_admin_token");
  tasksCache = [];
  clearSelection();
  taskTableBody.replaceChildren();
  adminPanel.hidden = true;
  loginForm.hidden = false;
  byId("adminIdentity").hidden = true;
  byId("btnLogout").hidden = true;
  byId("adminPassword").value = "";
  loginError.textContent = message;
}

function showDashboard(username) {
  loginForm.hidden = true;
  adminPanel.hidden = false;
  byId("adminIdentity").textContent = "管理员：" + username;
  byId("adminIdentity").hidden = false;
  byId("btnLogout").hidden = false;
  clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshDashboard, 10000);
  refreshDashboard();
}

async function apiRequest(path, options = {}, authenticated = true) {
  const requestToken = adminToken;
  if (authenticated && !requestToken) throw new Error("请先登录");
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
      ...(authenticated ? { "X-Admin-Token": requestToken } : {})
    }
  });
  const data = await response.json();
  // Discard requests from a session that was logged out while fetching.
  if (authenticated && requestToken !== adminToken) throw new Error("登录状态已改变");
  if (!response.ok) {
    if (authenticated && response.status === 401) showLogin("登录已失效，请重新登录。");
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

function addCell(row, value) {
  const cell = document.createElement("td");
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
    addCell(row, "#" + task.id);
    addCell(row, task.name + " / " + (task.class_id || "无班级信息"));
    addCell(row, task.lat + " " + task.lng);
    const times = addCell(row, task.times.join(", "));
    addDetail(times, (task.date_start?.slice(0, 10) || "不限") + " 至 " + (task.date_end?.slice(0, 10) || "长期"));
    const state = addCell(row, "");
    const tag = document.createElement("span");
    tag.className = "tag tag-" + task.runtime_status;
    tag.textContent = runtimeLabels[task.runtime_status] || "未知";
    state.appendChild(tag);
    if (!task.enabled && task.runtime_status === "running") addDetail(state, "已禁用，等待本次执行结束");
    if (task.running_since) addDetail(state, "开始：" + formatTime(task.running_since));
    addDetail(state, "下次：" + formatTime(task.next_run_at));
    const result = addCell(row, task.latest_run ? logLabels[task.latest_run.status] || task.latest_run.status : "尚未执行");
    if (task.latest_run) addDetail(result, formatTime(task.latest_run.run_at));
    const actions = addCell(row, "");
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
}

async function refreshDashboard() {
  if (!adminToken || refreshing) return;
  refreshing = true;
  try {
    await loadTasks();
    if (currentTaskId) await loadLogsForCurrent();
    byId("refreshStatus").textContent = "上次刷新：" + formatTime(new Date().toISOString()) + " · 每 10 秒自动刷新 · 北京时间";
  } catch (error) {
    if (adminToken) byId("refreshStatus").textContent = "刷新失败：" + error.message;
  } finally {
    refreshing = false;
  }
}

function clearSelection() {
  currentTaskId = null;
  byId("currentTaskLabel").textContent = "无";
  ["editCoord", "editWxKey", "editQqKey", "editTimes", "editDateStart", "editDateEnd"].forEach(id => byId(id).value = "");
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
  renderTasks();
  loadLogsForCurrent().catch(error => { if (adminToken) logList.textContent = error.message; });
}

async function saveTask() {
  if (!currentTaskId) return alert("请先选择一个任务。");
  const taskId = currentTaskId;
  const coordinates = byId("editCoord").value.trim().replace(/[,，|、]+/g, " ").split(/\s+/);
  if (coordinates.length !== 2 || coordinates.some(value => !value || !Number.isFinite(Number(value)))) {
    return alert("请输入有效的纬度和经度。");
  }
  const payload = {
    lat: coordinates[0], lng: coordinates[1],
    wx_key: byId("editWxKey").value.trim(), qq_key: byId("editQqKey").value.trim(),
    times: byId("editTimes").value.split(/[,，]/).map(value => value.trim()).filter(Boolean),
    enabled: byId("editEnabled").value === "1",
    date_start: byId("editDateStart").value || null, date_end: byId("editDateEnd").value || null
  };
  byId("btnSaveTask").disabled = true;
  try {
    await apiRequest("/api/admin/tasks/" + taskId, { method: "PATCH", body: JSON.stringify(payload) });
    await loadTasks();
    if (currentTaskId === taskId) selectTask(taskId);
    alert("保存成功，签到调度已更新。");
  } catch (error) {
    if (adminToken) alert("保存失败：" + error.message);
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
    if (adminToken) alert("删除失败：" + error.message);
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

loginForm.addEventListener("submit", async event => {
  event.preventDefault();
  loginError.textContent = "";
  byId("btnLogin").disabled = true;
  try {
    const data = await apiRequest("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({ username: byId("adminUsername").value.trim(), password: byId("adminPassword").value })
    }, false);
    adminToken = data.token;
    sessionStorage.setItem("bjmf_admin_token", adminToken);
    byId("adminPassword").value = "";
    showDashboard(data.username);
  } catch (error) {
    loginError.textContent = error.message;
  } finally {
    byId("btnLogin").disabled = false;
  }
});

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
byId("btnSaveTask").addEventListener("click", saveTask);
byId("btnDeleteTask").addEventListener("click", deleteTask);
byId("btnReloadLogs").addEventListener("click", () => {
  loadLogsForCurrent().catch(error => { if (adminToken) logList.textContent = error.message; });
});
["filterName", "filterClass"].forEach(id => byId(id).addEventListener("input", renderTasks));
["filterEnabled", "filterRuntime"].forEach(id => byId(id).addEventListener("change", renderTasks));

async function initialize() {
  checkHealth();
  if (!adminToken) return showLogin();
  try {
    const admin = await apiRequest("/api/admin/me");
    showDashboard(admin.username);
  } catch (error) { showLogin(error.message); }
}
initialize();
