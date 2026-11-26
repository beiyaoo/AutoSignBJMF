function createPeriodEditor({ mode, panel, legacy, list, addButton }) {
  const periodTemplate = document.createElement("template");
  periodTemplate.innerHTML = `
    <section class="period-card" aria-label="签到时段">
      <div class="period-control">
        <label>时段名称</label>
        <input class="input period-name" type="text" placeholder="例如：早上" maxlength="40" />
      </div>
      <div class="period-control">
        <span>普通签到时间</span>
        <div class="period-times"></div>
        <button class="btn btn-ghost period-add-time" type="button">添加普通时间</button>
      </div>
      <div class="period-control">
        <label>固定兜底时间</label>
        <input class="input period-fallback" type="time" step="1" />
      </div>
      <button class="btn btn-ghost period-remove" type="button">删除时段</button>
    </section>`;
  let nextId = 0;

  function addTime(container, value = "") {
    const row = document.createElement("div");
    row.className = "period-time-row";
    const input = document.createElement("input");
    input.className = "input";
    input.type = "time";
    input.step = "1";
    input.value = value;
    input.setAttribute("aria-label", "普通签到时间");
    const remove = document.createElement("button");
    remove.className = "btn btn-ghost";
    remove.type = "button";
    remove.textContent = "移除";
    remove.setAttribute("aria-label", "移除普通时间");
    remove.addEventListener("click", () => {
      if (container.children.length === 1) input.value = "";
      else row.remove();
    });
    row.append(input, remove);
    container.appendChild(row);
    return input;
  }

  function addPeriod({ name = "", times = [], fallback_time = "" } = {}) {
    const card = periodTemplate.content.firstElementChild.cloneNode(true);
    const id = ++nextId;
    const nameInput = card.querySelector(".period-name");
    const fallback = card.querySelector(".period-fallback");
    for (const [input, suffix] of [[nameInput, "name"], [fallback, "fallback"]]) {
      input.id = `sign-period-${id}-${suffix}`;
      input.previousElementSibling.htmlFor = input.id;
    }
    nameInput.value = name;
    fallback.value = fallback_time || "";
    const ordinary = card.querySelector(".period-times");
    (times.length ? times : [""]).forEach(value => addTime(ordinary, value));
    card.querySelector(".period-add-time").addEventListener("click", () => addTime(ordinary).focus());
    card.querySelector(".period-remove").addEventListener("click", () => {
      if (list.children.length > 2) card.remove();
      refreshRemoveButtons();
    });
    list.appendChild(card);
    refreshRemoveButtons();
    return nameInput;
  }

  function refreshRemoveButtons() {
    list.querySelectorAll(".period-remove").forEach(button => {
      button.hidden = list.children.length <= 2;
    });
  }

  function updateMode() {
    const enabled = mode.checked;
    panel.hidden = !enabled;
    legacy.hidden = enabled;
    if (enabled && !list.children.length) {
      addPeriod({ name: "早上" });
      addPeriod({ name: "晚上" });
    }
  }

  function set(periods) {
    list.replaceChildren();
    mode.checked = Boolean(periods?.length);
    if (mode.checked) periods.forEach(addPeriod);
    updateMode();
  }

  function normalizeTime(value) {
    return value.length === 5 ? value + ":00" : value;
  }

  function read() {
    if (!mode.checked) return [];
    const periods = [...list.children].map((card, index) => {
      const name = card.querySelector(".period-name").value.trim();
      const times = [...new Set([...card.querySelectorAll(".period-times input")]
        .map(input => normalizeTime(input.value)).filter(Boolean))].sort();
      const fallback = normalizeTime(card.querySelector(".period-fallback").value);
      if (!name) throw new Error(`请填写第 ${index + 1} 个时段的名称。`);
      if (!times.length) throw new Error(`「${name}」至少需要一个普通签到时间。`);
      if (!fallback || times[times.length - 1] >= fallback) {
        throw new Error(`「${name}」的兜底时间必须晚于该时段所有普通时间。`);
      }
      return { name, times, fallback_time: fallback };
    });
    if (new Set(periods.map(period => period.name)).size !== periods.length) {
      throw new Error("时段名称不能重复。");
    }
    for (let i = 1; i < periods.length; i++) {
      if (periods[i - 1].fallback_time >= periods[i].times[0]) {
        throw new Error(`「${periods[i].name}」的第一个普通时间必须晚于「${periods[i - 1].name}」的兜底时间。`);
      }
    }
    return periods;
  }

  mode.addEventListener("change", updateMode);
  addButton.addEventListener("click", () => addPeriod().focus());
  updateMode();
  return { read, set };
}
