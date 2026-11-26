function parseCoordinateInput(value) {
  const coordinates = value.trim().split(/[\s,，|、]+/);
  const [lng, lat] = coordinates;
  const numeric = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
  if (coordinates.length !== 2 || coordinates.some(part => part.length > 32 || !numeric.test(part)) ||
      Math.abs(Number(lng)) > 180 || Math.abs(Number(lat)) > 90) {
    throw new Error("请按经度、纬度填写坐标，例如 113.58,23.31。经度范围为 -180 到 180，纬度范围为 -90 到 90。");
  }
  return { lng, lat };
}

function formatCoordinateInput({ lng, lat }) {
  return `${lng},${lat}`;
}

function createPeriodEditor({ mode, panel, legacy, list, addButton }) {
  const periodTemplate = document.createElement("template");
  periodTemplate.innerHTML = `
    <section class="period-card" aria-label="签到时段">
      <div class="period-control">
        <label>时段名称</label>
        <input class="input period-name" type="text" placeholder="例如：晚课后" maxlength="40" />
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
      <label class="period-location-toggle">
        <input class="period-location-enabled" type="checkbox" />
        <span>使用此时段的专用地点</span>
      </label>
      <div class="period-location" hidden>
        <div class="period-control">
          <label>地点名称</label>
          <input class="input period-location-name" type="text" placeholder="例如：教学楼" maxlength="40" />
        </div>
        <div class="period-control">
          <label>经度、纬度</label>
          <input class="input period-location-coords" type="text" placeholder="例如：113.58,23.31" maxlength="80" />
        </div>
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

  function addPeriod({ name = "", times = [], fallback_time = "", location = null } = {}) {
    const card = periodTemplate.content.firstElementChild.cloneNode(true);
    const id = ++nextId;
    const nameInput = card.querySelector(".period-name");
    const fallback = card.querySelector(".period-fallback");
    const locationEnabled = card.querySelector(".period-location-enabled");
    const locationFields = card.querySelector(".period-location");
    const locationName = card.querySelector(".period-location-name");
    const locationCoords = card.querySelector(".period-location-coords");
    for (const [input, suffix] of [[nameInput, "name"], [fallback, "fallback"],
      [locationName, "location-name"], [locationCoords, "location-coords"]]) {
      input.id = `sign-period-${id}-${suffix}`;
      input.previousElementSibling.htmlFor = input.id;
    }
    nameInput.value = name;
    fallback.value = fallback_time || "";
    locationFields.id = `sign-period-${id}-location`;
    locationEnabled.setAttribute("aria-controls", locationFields.id);
    locationEnabled.checked = Boolean(location);
    locationFields.hidden = !location;
    if (location) {
      locationName.value = location.name;
      locationCoords.value = formatCoordinateInput(location);
    }
    locationEnabled.addEventListener("change", () => {
      locationFields.hidden = !locationEnabled.checked;
    });
    const ordinary = card.querySelector(".period-times");
    (times.length ? times : [""]).forEach(value => addTime(ordinary, value));
    card.querySelector(".period-add-time").addEventListener("click", () => addTime(ordinary).focus());
    card.querySelector(".period-remove").addEventListener("click", () => {
      const adjacent = card.nextElementSibling || card.previousElementSibling;
      card.remove();
      (adjacent?.querySelector(".period-name") || addButton).focus();
    });
    list.appendChild(card);
    return nameInput;
  }

  function addNumberedPeriod() {
    const used = new Set([...list.querySelectorAll(".period-name")].map(input => input.value.trim()));
    let number = 1;
    while (used.has(`时段 ${number}`)) number += 1;
    return addPeriod({ name: `时段 ${number}` });
  }

  function updateMode() {
    const enabled = mode.checked;
    panel.hidden = !enabled;
    legacy.hidden = enabled;
    if (enabled && !list.children.length) {
      addNumberedPeriod();
      addNumberedPeriod();
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

  function readLocation(card, periodName) {
    if (!card.querySelector(".period-location-enabled").checked) return null;
    const name = card.querySelector(".period-location-name").value.trim();
    if (!name) throw new Error(`请填写「${periodName}」的地点名称。`);

    try {
      return { name, ...parseCoordinateInput(card.querySelector(".period-location-coords").value) };
    } catch (error) {
      throw new Error(`「${periodName}」${error.message}`);
    }
  }

  function read() {
    if (!mode.checked) return [];
    if (!list.children.length) throw new Error("请至少添加一个时段，或关闭「每天分时段签到」。");
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
      const location = readLocation(card, name);
      return { name, times, fallback_time: fallback, ...(location ? { location } : {}) };
    });
    if (new Set(periods.map(period => period.name)).size !== periods.length) {
      throw new Error("时段名称不能重复。");
    }
    periods.sort((first, second) => first.times[0].localeCompare(second.times[0]));
    for (let i = 1; i < periods.length; i++) {
      if (periods[i - 1].fallback_time >= periods[i].times[0]) {
        throw new Error(`「${periods[i].name}」的第一个普通时间必须晚于「${periods[i - 1].name}」的兜底时间。`);
      }
    }
    return periods;
  }

  mode.addEventListener("change", updateMode);
  addButton.addEventListener("click", () => {
    const name = addNumberedPeriod();
    name.focus();
    name.select();
  });
  updateMode();
  return { read, set };
}
